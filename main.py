from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
import asyncio
import os
from typing import List, Optional

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

class TariffResult(BaseModel):
    anbieter: str
    tarifname: str
    preis_monat: float
    preis_jahr: float
    abschlussprovision: float
    sonderprovision: float
    gesamtprovision: float
    laufzeit: str
    kuendigungsfrist: str
    preisgarantie: str
    
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
    # Optional: Bankdaten
    iban: Optional[str] = None
    kontoinhaber: Optional[str] = None
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
            await page.goto("https://portal-energypartner.de/tarifvergleich")
            
            # Formular ausfüllen
            await page.fill('input[name="plz"]', request.plz)
            await page.fill('input[name="verbrauch"]', str(request.verbrauch))
            await page.select_option('select[name="personen"]', str(request.personen))
            
            # Suche starten
            await page.click('button[type="submit"]')
            await page.wait_for_selector('.tarif-result', timeout=10000)
            
            # Tarife auslesen
            tarife = []
            tarif_elements = await page.query_selector_all('.tarif-result')
            
            for element in tarif_elements:
                tarif = {
                    "anbieter": await element.query_selector('.anbieter').inner_text(),
                    "tarifname": await element.query_selector('.tarifname').inner_text(),
                    "preis_monat": float(await element.query_selector('.preis-monat').inner_text().replace('€', '').replace(',', '.')),
                    "preis_jahr": float(await element.query_selector('.preis-jahr').inner_text().replace('€', '').replace(',', '.')),
                    "laufzeit": await element.query_selector('.laufzeit').inner_text(),
                    "kuendigungsfrist": await element.query_selector('.kuendigung').inner_text(),
                    "preisgarantie": await element.query_selector('.preisgarantie').inner_text(),
                }
                tarife.append(tarif)
            
            await browser.close()
            
            return {"success": True, "tarife": tarife, "count": len(tarife)}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/apply")
async def submit_application(request: ApplicationRequest):
    """Antrag auf portal-energypartner.de einreichen"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # Direkt zur Antragsseite mit Tarif-ID
            await page.goto(f"https://portal-energypartner.de/antrag?tariff={request.tariff_id}")
            
            # Warten bis Formular geladen ist
            await page.wait_for_selector('form#antragsformular', timeout=10000)
            
            # Kundendaten einfüllen
            await page.select_option('select[name="anrede"]', request.anrede)
            await page.fill('input[name="vorname"]', request.vorname)
            await page.fill('input[name="nachname"]', request.nachname)
            await page.fill('input[name="strasse"]', request.strasse)
            await page.fill('input[name="hausnummer"]', request.hausnummer)
            await page.fill('input[name="plz"]', request.plz)
            await page.fill('input[name="ort"]', request.wohnort)
            await page.fill('input[name="geburtsdatum"]', request.geburtsdatum)
            await page.fill('input[name="telefon"]', request.telefon)
            await page.fill('input[name="email"]', request.email)
            
            # Bankdaten (falls vorhanden)
            if request.iban:
                await page.fill('input[name="iban"]', request.iban)
            if request.kontoinhaber:
                await page.fill('input[name="kontoinhaber"]', request.kontoinhaber)
            
            # Lieferbeginn
            if request.lieferbeginn == "schnellstmöglich":
                await page.check('input[name="lieferbeginn"][value="schnellstmoeglich"]')
            
            # AGB bestätigen
            await page.check('input[name="agb"]')
            await page.check('input[name="datenschutz"]')
            
            # Screenshot vor Absenden (für Debugging)
            await page.screenshot(path='before_submit.png')
            
            # Antrag absenden
            await page.click('button[type="submit"]')
            
            # Warten auf Bestätigungsseite
            await page.wait_for_url('**/bestaetigung', timeout=15000)
            
            # Antragsnummer extrahieren
            antragsnummer = await page.locator('.antragsnummer').inner_text()
            
            await browser.close()
            
            return {
                "success": True,
                "antragsnummer": antragsnummer,
                "message": "Antrag erfolgreich eingereicht",
                "details": {
                    "kunde": f"{request.vorname} {request.nachname}",
                    "email": request.email
                }
            }
            
    except Exception as e:
        return {
            "success": False,
            "antragsnummer": None,
            "message": f"Fehler beim Einreichen: {str(e)}",
            "details": None
        }
