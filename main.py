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
    personen: int
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
    kuendigungsfrist: Optional[str]
    vertragslaufzeit: Optional[str]
    timing_geeignet: bool

class ScrapeResponse(BaseModel):
    success: bool
    tariffs: List[TariffResult]
    message: Optional[str]

# Zugangsdaten aus Environment Variables
PORTAL_USERNAME = os.getenv("PORTAL_USERNAME", "")
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD", "")

@app.get("/")
def read_root():
    return {
        "service": "Energy Partner Scraper API",
        "status": "running",
        "endpoints": {
            "POST /scrape": "Scrape tariff data from portal-energypartner.de"
        }
    }

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_tariffs(request: ScrapeRequest):
    """
    Scrapes tariff data from portal-energypartner.de
    """
    if not PORTAL_USERNAME or not PORTAL_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="Portal credentials not configured. Set PORTAL_USERNAME and PORTAL_PASSWORD environment variables."
        )
    
    try:
        tariffs = await scrape_portal(request)
        return ScrapeResponse(
            success=True,
            tariffs=tariffs,
            message=f"Successfully scraped {len(tariffs)} tariffs"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def scrape_portal(request: ScrapeRequest) -> List[TariffResult]:
    """
    Hauptfunktion für das Scraping
    """
    async with async_playwright() as p:
        # Browser starten
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        try:
            # 1. Login
            await page.goto("https://portal-energypartner.de/energie/login/")
            await page.fill('input[name="username"]', PORTAL_USERNAME)
            await page.fill('input[name="password"]', PORTAL_PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            # 2. Zum Tarifrechner
            await page.goto("https://portal-energypartner.de/energie/tarifrechner/")
            
            # 3. Suchformular ausfüllen
            await page.fill('input[name="plz"]', request.plz)
            await page.fill('input[name="verbrauch"]', str(request.verbrauch))
            await page.select_option('select[name="personen"]', str(request.personen))
            await page.select_option('select[name="vertragsart"]', request.vertragsart)
            
            # Suche starten
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            # 4. Tarife auslesen
            tariffs = []
            tariff_elements = await page.query_selector_all('.tariff-item')
            
            for tariff_elem in tariff_elements[:20]:  # Max 20 Tarife
                try:
                    # Basis-Daten
                    anbieter = await tariff_elem.query_selector('.anbieter')
                    tarifname = await tariff_elem.query_selector('.tarifname')
                    preis_monat = await tariff_elem.query_selector('.preis-monat')
                    preis_jahr = await tariff_elem.query_selector('.preis-jahr')
                    
                    anbieter_text = await anbieter.text_content() if anbieter else "Unbekannt"
                    tarifname_text = await tarifname.text_content() if tarifname else "Unbekannt"
                    preis_monat_value = float((await preis_monat.text_content()).replace('€', '').replace(',', '.').strip()) if preis_monat else 0.0
                    preis_jahr_value = float((await preis_jahr.text_content()).replace('€', '').replace(',', '.').strip()) if preis_jahr else 0.0
                    
                    # Merkmale-Tab öffnen für Provisionen
                    merkmale_button = await tariff_elem.query_selector('button:has-text("Merkmale")')
                    if merkmale_button:
                        await merkmale_button.click()
                        await page.wait_for_timeout(1000)
                        
                        # Provisionen auslesen
                        abschluss_elem = await page.query_selector('.abschlussprovision')
                        sonder_elem = await page.query_selector('.sonderprovision')
                        gesamt_elem = await page.query_selector('.gesamtprovision')
                        
                        abschlussprovision = float((await abschluss_elem.text_content()).replace('€', '').replace(',', '.').strip()) if abschluss_elem else 0.0
                        sonderprovision = float((await sonder_elem.text_content()).replace('€', '').replace(',', '.').strip()) if sonder_elem else 0.0
                        gesamtprovision = float((await gesamt_elem.text_content()).replace('€', '').replace(',', '.').strip()) if gesamt_elem else 0.0
                        
                        # Kündigungsfrist und Vertragslaufzeit
                        kuendigungsfrist_elem = await page.query_selector('.kuendigungsfrist')
                        vertragslaufzeit_elem = await page.query_selector('.vertragslaufzeit')
                        
                        kuendigungsfrist = (await kuendigungsfrist_elem.text_content()).strip() if kuendigungsfrist_elem else None
                        vertragslaufzeit = (await vertragslaufzeit_elem.text_content()).strip() if vertragslaufzeit_elem else None
                        
                        # Timing-Check
                        timing_geeignet = check_timing(kuendigungsfrist, vertragslaufzeit)
                        
                        tariffs.append(TariffResult(
                            anbieter=anbieter_text,
                            tarifname=tarifname_text,
                            preis_monat=preis_monat_value,
                            preis_jahr=preis_jahr_value,
                            abschlussprovision=abschlussprovision,
                            sonderprovision=sonderprovision,
                            gesamtprovision=gesamtprovision,
                            kuendigungsfrist=kuendigungsfrist,
                            vertragslaufzeit=vertragslaufzeit,
                            timing_geeignet=timing_geeignet
                        ))
                        
                except Exception as e:
                    print(f"Error scraping tariff: {e}")
                    continue
            
            return tariffs
            
        finally:
            await browser.close()

def check_timing(kuendigungsfrist: Optional[str], vertragslaufzeit: Optional[str]) -> bool:
    """
    Prüft, ob der Tarif timing-mäßig für Wechsel im Voraus geeignet ist
    """
    if not kuendigungsfrist:
        return False
    
    # Extrahiere Wochen/Monate aus dem String
    try:
        if "Woche" in kuendigungsfrist:
            wochen = int(kuendigungsfrist.split()[0])
            return wochen >= 6  # Mindestens 6 Wochen Vorlauf
        elif "Monat" in kuendigungsfrist:
            monate = int(kuendigungsfrist.split()[0])
            return monate >= 1  # Mindestens 1 Monat Vorlauf
    except:
        pass
    
    return False

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
