from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright
import asyncio
import os
from typing import List, Optional, Dict, Any
import re
import logging

# Logging einrichten
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    brokerId: Optional[str] = None
    ort: Optional[str] = None
    strasse: Optional[str] = None
    hausnummer: Optional[str] = None
    include_provisions: bool = True  # Provisions-Daten mit auslesen

class TariffDetail(BaseModel):
    anbieter: str
    tarif: str
    preis_monat: Optional[str] = None
    preis_jahr: Optional[str] = None
    grundpreis: Optional[str] = None
    arbeitspreis: Optional[str] = None
    provision: Optional[str] = None  # Provisions-Wert
    provision_details: Optional[Dict[str, Any]] = None
    tariff_id: Optional[str] = None

class ScrapeResponse(BaseModel):
    success: bool
    tariffs: List[TariffDetail]
    count: int
    error: Optional[str] = None

@app.get("/")
async def root():
    return {
        "status": "online", 
        "service": "Energy Partner Scraper API",
        "version": "2.0"
    }

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tariffs(request: ScrapeRequest):
    """Scrape Tarifvergleich von portal-energypartner.de inkl. Provisions-Daten"""
    
    if not PORTAL_USERNAME or not PORTAL_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="Portal credentials not configured. Set PORTAL_USERNAME and PORTAL_PASSWORD env vars."
        )
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()
            
            logger.info(f"Starting scrape for PLZ {request.plz}, Verbrauch {request.verbrauch}")
            
            # Zur Login-Seite
            await page.goto("https://portal-energypartner.de/vp-buero/login/")
            await page.wait_for_load_state('networkidle')
            
            # Login durchführen
            logger.info("Performing login...")
            await page.fill('input[name="user"]', PORTAL_USERNAME)
            await page.fill('input[name="pass"]', PORTAL_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state('networkidle')
            
            # Prüfen ob Login erfolgreich
            if await page.query_selector('text="Login fehlgeschlagen"'):
                raise Exception("Login failed - check credentials")
            
            logger.info("Login successful, navigating to tariff calculator...")
            
            # Zur Tarifvergleichsseite
            await page.goto("https://portal-energypartner.de/energie/tarifrechner/")
            await page.wait_for_load_state('networkidle')
            
            # Warten bis egon geladen ist
            await page.wait_for_selector('#egon-embedded-ratecalc', timeout=10000)
            
            logger.info("Filling form...")
            
            # Formular ausfüllen
            # PLZ
            await page.click('#egon-embedded-ratecalc-form-field-zip')
            await page.fill('#egon-embedded-ratecalc-form-field-zip', '')
            await page.type('#egon-embedded-ratecalc-form-field-zip', request.plz, delay=50)
            await page.click('#egon-embedded-ratecalc-form-field-city')
            
            # Warten bis Ort geladen
            await page.wait_for_function(
                """() => {
                    const citySelect = document.querySelector('#egon-embedded-ratecalc-form-field-city');
                    return citySelect && citySelect.options.length > 1;
                }""",
                timeout=5000
            )
            
            # Ort auswählen
            if request.ort:
                await page.select_option('#egon-embedded-ratecalc-form-field-city', label=request.ort)
            else:
                await page.select_option('#egon-embedded-ratecalc-form-field-city', index=1)
            
            # Warten bis Straßen geladen
            await page.wait_for_function(
                """() => {
                    const streetSelect = document.querySelector('#egon-embedded-ratecalc-form-field-street');
                    return streetSelect && streetSelect.options.length > 1;
                }""",
                timeout=5000
            )
            
            # Straße auswählen
            if request.strasse:
                await page.select_option('#egon-embedded-ratecalc-form-field-street', label=request.strasse)
            else:
                await page.select_option('#egon-embedded-ratecalc-form-field-street', index=1)
            
            # Hausnummer
            hausnr = request.hausnummer if request.hausnummer else '1'
            await page.fill('#egon-embedded-ratecalc-form-field-street_number', hausnr)
            await page.click('body')
            
            # Warten bis Netzbetreiber geladen
            await page.wait_for_function(
                """() => {
                    const netzSelect = document.querySelector('#egon-embedded-ratecalc-form-field-netz_id');
                    return netzSelect && netzSelect.value !== 'Kein Netzbetreiber' && netzSelect.value !== '';
                }""",
                timeout=5000
            )
            
            # Verbrauch
            await page.fill('#egon-embedded-ratecalc-form-field-consum', str(request.verbrauch))
            
            logger.info("Submitting form...")
            
            # Formular absenden
            await page.click('button:has-text("jetzt Tarife berechnen")')
            
            # Warten bis Ergebnisse geladen - egon nutzt dynamisches Laden
            try:
                # Warten auf den Ergebnis-Container
                await page.wait_for_selector('.egon-ratecalc-result-item', timeout=15000)
                await page.wait_for_load_state('networkidle')
                
                # Kurz warten damit alle Items geladen sind
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Error waiting for results: {e}")
                # Screenshot für Debugging
                await page.screenshot(path='/tmp/error_screenshot.png')
                raise Exception(f"No tariff results found. Error: {str(e)}")
            
            logger.info("Extracting tariff data...")
            
            # Tarife extrahieren - angepasst an egon-Struktur
            tariffs_data = await page.evaluate("""
                () => {
                    const results = [];
                    const items = document.querySelectorAll('.egon-ratecalc-result-item');
                    
                    items.forEach((item, index) => {
                        try {
                            const tariff = {
                                anbieter: item.querySelector('.egon-provider-name')?.textContent?.trim() || 
                                         item.querySelector('[data-field="provider"]')?.textContent?.trim() || '',
                                tarif: item.querySelector('.egon-tariff-name')?.textContent?.trim() ||
                                      item.querySelector('[data-field="tariff"]')?.textContent?.trim() || '',
                                preis_monat: item.querySelector('.egon-price-month')?.textContent?.trim() ||
                                           item.querySelector('[data-field="price_month"]')?.textContent?.trim() || null,
                                preis_jahr: item.querySelector('.egon-price-year')?.textContent?.trim() ||
                                          item.querySelector('[data-field="price_year"]')?.textContent?.trim() || null,
                                grundpreis: item.querySelector('.egon-base-price')?.textContent?.trim() ||
                                          item.querySelector('[data-field="base_price"]')?.textContent?.trim() || null,
                                arbeitspreis: item.querySelector('.egon-work-price')?.textContent?.trim() ||
                                            item.querySelector('[data-field="work_price"]')?.textContent?.trim() || null,
                                tariff_id: item.getAttribute('data-tariff-id') || 
                                         item.getAttribute('data-id') ||
                                         `tariff_${index}`
                            };
                            results.push(tariff);
                        } catch (err) {
                            console.error('Error parsing tariff item:', err);
                        }
                    });
                    
                    return results;
                }
            """)
            
            logger.info(f"Found {len(tariffs_data)} tariffs")
            
            # Provisions-Daten auslesen wenn gewünscht
            if request.include_provisions and len(tariffs_data) > 0:
                logger.info("Extracting provision data...")
                
                for tariff in tariffs_data:
                    try:
                        # Provision-Tab/Button suchen und klicken
                        provision_selector = f'[data-tariff-id="{tariff["tariff_id"]}"'] .egon-provision-btn'
                        provision_element = await page.query_selector(provision_selector)
                        
                        if provision_element:
                            await provision_element.click()
                            await asyncio.sleep(0.5)  # Kurz warten bis Provision angezeigt wird
                            
                            # Provision-Wert auslesen
                            provision_value = await page.evaluate(f"""
                                () => {{
                                    const elem = document.querySelector('[data-tariff-id="{tariff['tariff_id']}"] .egon-provision-value');
                                    return elem ? elem.textContent.trim() : null;
                                }}
                            """)
                            
                            tariff['provision'] = provision_value
                            logger.info(f"Provision for {tariff['tarif']}: {provision_value}")
                            
                    except Exception as e:
                        logger.warning(f"Could not extract provision for tariff {tariff['tarif']}: {e}")
                        tariff['provision'] = None
            
            await browser.close()
            
            # Pydantic models erstellen
            tariffs = [TariffDetail(**t) for t in tariffs_data]
            
            return ScrapeResponse(
                success=True,
                tariffs=tariffs,
                count=len(tariffs)
            )
    
    except Exception as e:
        logger.error(f"Scraping error: {str(e)}")
        return ScrapeResponse(
            success=False,
            tariffs=[],
            count=0,
            error=str(e)
        )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
