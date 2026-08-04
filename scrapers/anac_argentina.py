"""
Scraper para ANAC Argentina — Directivas y Advertencias de Aeronavegabilidad.
URL: https://www.argentina.gob.ar/anac/directivas-y-advertencias-de-aeronavegabilidad

ANAC Argentina publica DAs y ADVs como links a PDFs en una página HTML estática.
Los títulos contienen número de DA, fabricante, modelo y descripción.
Formato típico:
  "DA - 2024-05-01 R1 - LAVIA S.A. PA25 / PA25-235 / PA25-260 ... descripción"
  "ADV 338-DAG - Talleres ... modelo PA-25 ... descripción"
"""

import re
import requests
from bs4 import BeautifulSoup
from scrapers.common.http import browser_headers

from scrapers.common.log import get_logger

log = get_logger(__name__)

ANAC_AR_URL = "https://www.argentina.gob.ar/anac/directivas-y-advertencias-de-aeronavegabilidad"

HEADERS = browser_headers({"Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.5"})


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Directivas de Aeronavegabilidad en ANAC Argentina.
    Parsea la página HTML estática de argentina.gob.ar/anac.
    """
    try:
        resp = requests.get(ANAC_AR_URL, headers=HEADERS, timeout=25)
        resp.raise_for_status()
    except Exception as e:
        log.info(f"[ANAC Argentina] Error al conectar: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)

        if not text or len(text) < 10:
            continue

        is_da = bool(re.search(r"\b(DA[\s-]|DAE[\s-]|ADV[\s-]|APDA[\s-]|RA[\s-])", text, re.I))
        is_pdf = href.lower().endswith(".pdf")

        if not is_da and not is_pdf:
            continue

        if not href.startswith("http"):
            href = "https://www.argentina.gob.ar" + href

        ad_number = _extract_ad_number(text)
        if not ad_number:
            continue

        make_extracted, model_extracted = _extract_make_model(text)
        effective_date = _extract_date(ad_number)

        ad = {
            "Source": "ANAC Argentina",
            "AD_Number": ad_number,
            "Make": make_extracted,
            "Model": model_extracted,
            "Subject": _clean_subject(text, ad_number),
            "Effective_Date": effective_date,
            "PDF_Link": href if is_pdf else "",
            "Product_Type": "",
            "Status": "Final Rule",
            "Docket": "",
        }

        if _matches_filters(ad, keyword, make, model):
            results.append(ad)

    seen = set()
    unique = []
    for r in results:
        key = r["AD_Number"]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique = unique[:max_results]
    log.info(f"[ANAC Argentina] Total DAs obtenidas: {len(unique)}")
    return unique


def _extract_ad_number(text):
    patterns = [
        r"(DA[\s-]*\d{4}-\d{2}-\d{2}(?:\s*R\d+)?)",
        r"(DAE[\s-]*\d{4}-\d{2}-\d{2}(?:\s*R\d+)?)",
        r"(APDA[\s-]*\d{4}-\d{2}-\d{2}(?:\s*[-\s]rev\d+)?)",
        r"(ADV\s*\d{1,3}[\s/-]*DAG(?:\s*R\d+)?)",
        r"(RA\s*N?°?\s*\d{4}-\d{2}-\d{2})",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip()
    return ""


def _extract_date(ad_number):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", ad_number)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _extract_make_model(text):
    known_makes = {
        "Piper": ["PA-25", "PA-12", "PA-18", "PA-20", "PA-22", "PA-11", "J3", "J4", "J5"],
        "LAVIA": ["PA25", "PA25-235", "PA25-260"],
        "Cessna": ["C-172", "C-152", "C-182", "C-206"],
        "Boeing": ["737", "747", "757", "767", "777", "787"],
        "Airbus": ["A320", "A321", "A330", "A340"],
        "I.C.A BRASOV": ["IS-28B2"],
        "FAdeA": [],
        "CHINCUL": [],
    }

    make = ""
    model = ""
    text_upper = text.upper()

    for m, models in known_makes.items():
        if m.upper() in text_upper:
            make = m
            for mod in models:
                if mod.upper() in text_upper:
                    model = mod
                    break
            break

    if not model:
        m = re.search(r"\b(PA[-\s]?\d{2,3}(?:[-\s]\d{3})?)\b", text, re.I)
        if m:
            model = m.group(1)
        else:
            m = re.search(r"\b(IS[-\s]?\d{2,3}[A-Z]*\d*)\b", text, re.I)
            if m:
                model = m.group(1)

    return make, model


def _clean_subject(text, ad_number):
    subject = text.replace(ad_number, "").strip()
    subject = re.sub(r"^[\s\-–—]+", "", subject)
    return subject if subject else text


def _matches_filters(ad, keyword, make, model):
    if keyword:
        kl = keyword.lower()
        text = f"{ad['AD_Number']} {ad['Subject']} {ad['Make']} {ad['Model']}".lower()
        if kl not in text:
            return False
    if make:
        if make.lower() not in ad["Make"].lower() and make.lower() not in ad["Subject"].lower():
            return False
    if model:
        if model.lower() not in ad["Model"].lower() and model.lower() not in ad["Subject"].lower():
            return False
    return True


if __name__ == "__main__":
    results = search_ads(max_results=50)
    log.info(f"Encontrados: {len(results)}")
    for r in results[:10]:
        log.info(f"  {r['AD_Number']} | {r['Make']} | {r['Model']} | {r['Subject'][:60]}")
