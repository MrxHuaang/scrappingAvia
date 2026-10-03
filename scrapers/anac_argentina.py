"""
Scraper para ANAC Argentina — Directivas y Advertencias de Aeronavegabilidad.
URL: https://www.argentina.gob.ar/anac/directivas-y-advertencias-de-aeronavegabilidad

ANAC Argentina publica DAs y ADVs como links a PDFs en paginas HTML estaticas:
la principal (solo lo ultimo) y el archivo de Advertencias (todas).
Los títulos contienen número de DA, fabricante, modelo y descripción.
Formato típico:
  "DA - 2024-05-01 R1 - LAVIA S.A. PA25 / PA25-235 / PA25-260 ... descripción"
  "ADV 338-DAG - Talleres ... modelo PA-25 ... descripción"
"""

import re
from bs4 import BeautifulSoup
from scrapers.common.http import browser_headers, request_with_backoff

from scrapers.common.log import get_logger

log = get_logger(__name__)

ANAC_AR_URL = "https://www.argentina.gob.ar/anac/directivas-y-advertencias-de-aeronavegabilidad"
# La pagina principal solo muestra las ultimas publicaciones (~11): una ADV
# sale de ahi en cuanto se publica otra. El archivo de Advertencias lista
# todas (~85) y es la unica forma de no perderlas.
ANAC_AR_ADV_URL = "https://www.argentina.gob.ar/anac/advertencias-de-aeronavegabilidad"
LISTING_URLS = (ANAC_AR_URL, ANAC_AR_ADV_URL)

HEADERS = browser_headers({"Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.5"})


class AnacArgentinaError(RuntimeError):
    """Un listado no se pudo leer. Se levanta en vez de devolver [] para que
    populate_db registre la corrida como error y no como `completed` vacia."""


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Directivas y Advertencias de Aeronavegabilidad en ANAC Argentina.
    Parsea la pagina principal y el archivo de Advertencias de argentina.gob.ar.
    """
    results = []
    for url in LISTING_URLS:
        found = _parse_listing(_fetch(url))
        if not found:
            raise AnacArgentinaError(f"{url} no trajo ninguna DA/ADV")
        log.info(f"[ANAC Argentina] {url}: {len(found)} documentos")
        results.extend(found)

    # El mismo documento aparece en las dos paginas (y a veces dos veces en
    # la misma): gana el primero que trae PDF.
    by_number = {}
    for r in results:
        prev = by_number.get(r["AD_Number"])
        if prev is None or (not prev["PDF_Link"] and r["PDF_Link"]):
            by_number[r["AD_Number"]] = r

    unique = [r for r in by_number.values() if _matches_filters(r, keyword, make, model)]
    unique = unique[:max_results]
    log.info(f"[ANAC Argentina] Total DAs obtenidas: {len(unique)}")
    return unique


def _fetch(url):
    resp = request_with_backoff("GET", url, headers=HEADERS, timeout=25, tag="ANAC Argentina")
    if resp is None:
        raise AnacArgentinaError(f"sin respuesta de {url}")
    if resp.status_code != 200:
        raise AnacArgentinaError(f"HTTP {resp.status_code} en {url}")
    return resp.text


def _parse_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)

        if not text or len(text) < 10:
            continue

        is_da = bool(re.search(r"\b(DA[\s-]|DAE[\s-]|ADV[\s-]|APDA[\s-]|RA[\s-])", text, re.I))
        is_pdf = href.lower().endswith(".pdf")

        if not is_da and not is_pdf:
            continue

        if not href.startswith("http"):
            href = "https://www.argentina.gob.ar" + href

        ad_number, matched = _extract_ad_number(text)
        if not ad_number:
            continue

        make_extracted, model_extracted = _extract_make_model(text)

        results.append({
            "Source": "ANAC Argentina",
            "AD_Number": ad_number,
            "Make": make_extracted,
            "Model": model_extracted,
            "Subject": _clean_subject(text, matched),
            "Effective_Date": _extract_date(ad_number),
            "PDF_Link": href if is_pdf else "",
            "Product_Type": "",
            "Status": "Final Rule",
            "Docket": "",
        })

    return results


# Advertencias: el sitio escribe el mismo numero de muchas formas
# ("ADV 335/DAG", "ADV 335-DAG", "ADV 128 R1/DAG", "ADV 299/DAGR1",
# "ADV-293-DAG", "ADV 310/DAG R02", "61/DAG R1", "ADV 300 - ..."). Todas se
# llevan a "ADV {n}/{org}" + " R{k}": sin forma canonica la misma ADV queda
# como dos filas en cuanto cambia de pagina.
_ADV = re.compile(
    r"^\s*(ADV[\s-]*)?0*(\d{1,3})(?:\s*R\s*0*(\d+))?"
    r"(?:\s*[/-]\s*(DAG|DOA|AG)(?:\s*-?\s*R\s*0*(\d+)(?!\d))?)?",
    re.I,
)


def _extract_adv_number(text):
    m = _ADV.match(text)
    if not m or not (m.group(1) or m.group(4)):
        return "", ""
    number, rev_before, org, rev_after = m.group(2), m.group(3), m.group(4), m.group(5)
    canonical = f"ADV {int(number)}/{(org or 'DAG').upper()}"
    rev = rev_before or rev_after
    if rev:
        canonical += f" R{int(rev)}"
    return canonical, m.group(0)


def _extract_ad_number(text):
    """(ad_number, texto que lo nombraba). El segundo sale del subject."""
    adv, matched = _extract_adv_number(text)
    if adv:
        return adv, matched
    patterns = [
        r"(DA[\s-]*\d{4}-\d{2}-\d{2}(?:\s*R\d+)?)",
        r"(DAE[\s-]*\d{4}-\d{2}-\d{2}(?:\s*R\d+)?)",
        r"(APDA[\s-]*\d{4}-\d{2}-\d{2}(?:\s*[-\s]rev\d+)?)",
        r"(RA\s*N?°?\s*\d{4}-\d{2}-\d{2})",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip(), m.group(1)
    return "", ""


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


def _clean_subject(text, matched):
    subject = text.replace(matched, "").strip()
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
