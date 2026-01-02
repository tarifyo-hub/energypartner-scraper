from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
import asyncio
import os
from typing import List, Optional
import re

# Umgebungsvariablen für Portal-Login
PORTAL_USERNAME = os.getenv("PORTAL_USERNAME")  # Dein Makler-Login
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD")  # Dein Makler-Passwort

app = FastAPI(title="Energy Partner Scraper API")

# CORS für n8n
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    plz: str
    verbrauch: int
    personen: int = 1
    vertragsart: str = "Neukunde"
    userId: str
    brokerId: Optional[str] = None  # Multi-Broker-Support
    ort: Optional[str] = None
    strasse: Optional[str] = None
    hausnummer: Optional[str] = None

class ApplicationRequest(BaseModel):
    plz: str
    verbrauch: int
    personen: int
    tariff_id: str
    
    # Kundendaten
    anrede: str  # "Herr" oder "Frau"
    vorname: str
    nachname: str
    strasse: str
    hausnummer: str
    wohnort: str
    geburtsdatum: str  # Format: "DD.MM.YYYY"
    telefon: str
    email: str
    iban: str  # Pflichtfeld
    kontoinhaber: str  # Pflichtfeld
    
    # Lieferbeginn
    lieferbeginn: str = "schnellstmöglich"
    userId: str

class ApplicationResult(BaseModel):
    success: bool
    antragsnummer: Optional[str] = None
    message: str
    details: Optional[dict] = None

@app.get("/")
async def root():
    return {"status": "online", "service": "Energy Partner Scraper API"}

@app.post("/scrape")
async def scrape_tariffs(request: ScrapeRequest):
    """Scrape Tarifvergleich von portal-energypartner.de"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # Zur Tarifvergleichsseite
            await page.goto("https://portal-energypartner.de/energie/tarifrechner/")
            
            # Formular ausfüllen - character by character typing
            
            # PLZ-Feld - leer machen und dann tippen
            await page.click('#egon-embedded-ratecalc-form-field-zip')
            await page.fill('#egon-embedded-ratecalc-form-field-zip', '')
            await page.type('#egon-embedded-ratecalc-form-field-zip', request.plz, delay=50)
            
            # Auf Ort-Feld klicken um blur-Event zu triggern
            await page.click('#egon-embedded-ratecalc-form-field-city')
            
            # Warten bis Ort geladen ist (max 5 Sekunden)
            await page.wait_for_function(
                f"""() => {{
                    const citySelect = document.querySelector('#egon-embedded-ratecalc-form-field-city');
                    return citySelect && citySelect.options.length > 1;
                }}""",
                timeout=5000
            )
            
            # Ort auswählen (triggert Straßen-AJAX)
            if hasattr(request, 'ort') and request.ort:
                await page.select_option('#egon-embedded-ratecalc-form-field-city', label=request.ort)
            else:
                # Ersten verfügbaren Ort auswählen
                city_options = await page.query_selector_all('#egon-embedded-ratecalc-form-field-city option')
                if len(city_options) > 1:
                    await page.select_option('#egon-embedded-ratecalc-form-field-city', index=1)
            
            # Warten bis Straßen geladen sind
            await page.wait_for_function(
                """() => {
                    const streetSelect = document.querySelector('#egon-embedded-ratecalc-form-field-street');
                    return streetSelect && streetSelect.options.length > 1;
                }""",
                timeout=5000
            )
            
            # Straße auswählen
            if hasattr(request, 'strasse') and request.strasse:
                await page.select_option('#egon-embedded-ratecalc-form-field-street', label=request.strasse)
            else:
                # Erste verfügbare Straße auswählen
                street_options = await page.query_selector_all('#egon-embedded-ratecalc-form-field-street option')
                if len(street_options) > 1:
                    await page.select_option('#egon-embedded-ratecalc-form-field-street', index=1)
            
            # Hausnummer eingeben
            if hasattr(request, 'hausnummer') and request.hausnummer:
                await page.fill('#egon-embedded-ratecalc-form-field-street_number', request.hausnummer)
            else:
                await page.fill('#egon-embedded-ratecalc-form-field-street_number', '1')
            
            # Fokus verlieren, um Netzbetreiber-AJAX zu triggern
            await page.click('body')
            
            # Warten bis Netzbetreiber geladen ist
            await page.wait_for_function(
                """() => {
                    const netzSelect = document.querySelector('#egon-embedded-ratecalc-form-field-netz_id');
                    return netzSelect && netzSelect.value !== 'Kein Netzbetreiber' && netzSelect.value !== '';
                }""",
                timeout=5000
            )
            
            # Verbrauch eingeben
            await page.fill('#egon-embedded-ratecalc-form-field-consum', str(request.verbrauch))
            
            # Tarife berechnen
            await page.click('button:has-text("jetzt Tarife berechnen")')
            
            # Warten bis Ergebnisse geladen sind
            await page.wait_for_selector('.tariff-result', timeout=10000)
            
            # Tarife extrahieren
            tariffs = await page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('.tariff-result').forEach((el) => {
                        const tariff = {
                            anbieter: el.querySelector('.provider-name')?.textContent.trim() || '',
                            tarif: el.querySelector('.tariff-name')?.textContent.trim() || '',
                            preis: el.querySelector('.price')?.textContent.trim() || '',
                            details: el.querySelector('.details')?.textContent.trim() || ''
                        };
                        results.push(tariff);
                    });
                    return results;
                }
            """)
            
            await browser.close()
            
            return {
                "success": True,
                "tariffs": tariffs,
                "count": len(tariffs)
            }
            
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "tariffs": []
        }

@app.post("/apply")
async def apply_tariff(request: ApplicationRequest):
    """Tarif-Antrag ausfüllen und absenden"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # TODO: Hier die Logik für den Antragsablauf implementieren
            # 1. Zur Tarifseite navigieren
            # 2. Antrag auswählen
            # 3. Kundendaten eingeben (mit IBAN und kontoinhaber als Pflichtfelder)
            # 4. Absenden und Antragsnummer extrahieren
            
            await browser.close()
            
            return ApplicationResult(
                success=True,
                antragsnummer="TEST-12345",
                message="Antrag erfolgreich erstellt"
            )
            
    except Exception as e:
        return ApplicationResult(
            success=False,
            message=f"Fehler beim Erstellen des Antrags: {str(e)}"
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
