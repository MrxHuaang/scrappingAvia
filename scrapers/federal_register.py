"""
Scraper para Federal Register API - Airworthiness Directives (FAA)

La FR API tiene hard cap de 10000 resultados por query. Para superarlo,
fragmentamos la consulta por año via conditions[publication_date][gte]/[lte].
Cobertura: 1994-01-03 en adelante (la API no indexa documentos previos).

Endpoint: https://www.federalregister.gov/api/v1/documents.json
Docs:     https://www.federalregister.gov/developers/documentation/api/v1
"""

import re
import time
from datetime import datetime

import requests

from utils import extract_make_model, normalize_ad_number


FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_API_START_YEAR = 1994
PER_PAGE = 1000  # max soportado por la API

HEADERS = {
    "User-Agent": "AD-Scraper/1.0",
    "Accept": "application/json",
}

_FIELDS = [
    "title", "abstract", "document_number", "publication_date",
    "effective_on", "pdf_url", "html_url", "agencies",
    "cfr_references", "action", "type",
]


def _request_page(params: dict, retries: int = 3) -> dict | None:
    """Request con backoff exponencial en 429/5xx."""
    delay = 2
    for attempt in range(retries):
        try:
            resp = requests.get(FEDERAL_REGISTER_API, params=params, headers=HEADERS, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                print(f"[Federal Register] HTTP {resp.status_code} attempt {attempt + 1}, sleep {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"[Federal Register] Error: {e} attempt {attempt + 1}")
            time.sleep(delay)
            delay *= 2
    return None


def _search_year(year: int, max_pages: int = 50) -> list[dict]:
    """Trae todos los RULE docs de CFR 14/39 publicados en un año."""
    results = []
    page = 1

    while page <= max_pages:
        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": "RULE",
            "conditions[publication_date][gte]": f"{year}-01-01",
            "conditions[publication_date][lte]": f"{year}-12-31",
            "per_page": PER_PAGE,
            "page": page,
            "order": "oldest",
            "fields[]": _FIELDS,
        }

        data = _request_page(params)
        if data is None:
            break

        docs = data.get("results", [])
        total = data.get("count", 0)
        if not docs:
            break

        for doc in docs:
            ad = _parse_document(doc)
            if ad:
                results.append(ad)

        if len(docs) < PER_PAGE or len(results) >= total:
            break

        page += 1
        time.sleep(0.3)

    print(f"[Federal Register] {year}: {len(results)} ADs")
    return results


def search_ads_by_year_range(year_from: int, year_to: int) -> list[dict]:
    """Itera año por año y agrega resultados."""
    all_results = []
    for yr in range(year_from, year_to + 1):
        all_results.extend(_search_year(yr))
    print(f"[Federal Register] Total {year_from}-{year_to}: {len(all_results)} ADs")
    return all_results


def search_ads(keyword=None, make=None, model=None, product_type=None,
               date_from=None, date_to=None, max_pages=10):
    """
    Compat shim: si no se pasan filtros, hace el year-split completo.
    Mantiene la firma antigua para no romper callers.
    """
    if not any([keyword, make, model, product_type, date_from, date_to]):
        return search_ads_by_year_range(FR_API_START_YEAR, datetime.now().year)

    # Caso filtrado: una sola query (cap aplica si supera 10k, raro con filtros)
    search_terms = [t for t in [keyword, make, model, product_type] if t]
    search_query = " ".join(search_terms) if search_terms else None

    all_results = []
    page = 1
    while page <= max_pages:
        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": "RULE",
            "per_page": PER_PAGE,
            "page": page,
            "order": "newest",
            "fields[]": _FIELDS,
        }
        if search_query:
            params["conditions[term]"] = search_query
        if date_from:
            params["conditions[publication_date][gte]"] = date_from
        if date_to:
            params["conditions[publication_date][lte]"] = date_to

        data = _request_page(params)
        if data is None:
            break
        docs = data.get("results", [])
        if not docs:
            break
        for doc in docs:
            ad = _parse_document(doc)
            if ad:
                all_results.append(ad)
        if len(docs) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)

    return all_results


def _parse_document(doc) -> dict | None:
    """Parsea un documento del Federal Register a formato estandar."""
    try:
        title = doc.get("title", "") or ""
        abstract = doc.get("abstract", "") or ""

        ad_number = ""
        ad_match = re.search(r"(AD\s*\d{4}[-–]\d{2}[-–]\d{2,4})", title, re.I)
        if ad_match:
            ad_number = ad_match.group(1)
        else:
            ad_match = re.search(r"(\d{4}[-–]\d{2}[-–]\d{2,4})", title)
            if ad_match:
                ad_number = ad_match.group(1)
            else:
                ad_number = doc.get("document_number", "")

        ad_number = normalize_ad_number(ad_number)
        make, model = extract_make_model(title, abstract)

        subject = re.sub(r"^Airworthiness Directives[;:]\s*", "", title, flags=re.I)

        effective_date = doc.get("effective_on", "") or doc.get("publication_date", "")
        pdf_link = doc.get("pdf_url", "") or doc.get("html_url", "")

        doc_type = (doc.get("type") or "").upper()
        action_field = (doc.get("action") or "").lower()
        title_lower = title.lower()
        abstract_lower = abstract.lower()

        if doc_type == "PROPOSED_RULE" or "proposed" in title_lower or "nprm" in title_lower or "proposed" in action_field:
            status = "NPRM"
        elif "emergency" in title_lower or "emergency" in action_field:
            status = "Emergency AD"
        elif "correction" in title_lower or "correcting amendment" in title_lower or "correction" in action_field:
            status = "Correction"
        elif "interim" in title_lower or "interim" in action_field:
            status = "Interim Final Rule"
        elif doc_type == "RULE" or "final rule" in action_field:
            status = "Final Rule"
        else:
            status = "Final Rule"

        cfr_refs = doc.get("cfr_references") or []
        product_type = ""
        combined = f"{title_lower} {abstract_lower}"
        if re.search(r"\bengine\b", combined):
            product_type = "Engine"
        elif re.search(r"\bpropeller\b", combined):
            product_type = "Propeller"
        elif re.search(r"\bappliance\b", combined):
            product_type = "Appliance"
        elif re.search(r"\baircraft\b|\bairplane\b|\bhelicopter\b|\bairship\b|\bglider\b", combined):
            product_type = "Aircraft"
        if not product_type and cfr_refs:
            for ref in cfr_refs:
                part = str(ref.get("part", "") if isinstance(ref, dict) else "")
                if part == "39":
                    product_type = "Aircraft"
                    break

        return {
            "Source": "FAA (Federal Register)",
            "AD_Number": ad_number.strip(),
            "Subject": subject.strip(),
            "Make": make.strip(),
            "Model": model.strip(),
            "Effective_Date": effective_date,
            "Status": status,
            "Product_Type": product_type,
            "Docket": doc.get("document_number", ""),
            "PDF_Link": pdf_link,
        }
    except Exception as e:
        print(f"[Federal Register] Error parseando: {e}")
        return None


def get_recent_ads(days=30, max_results=200):
    """Obtiene los ADs mas recientes del Federal Register."""
    from datetime import timedelta
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return search_ads(date_from=date_from, max_pages=max_results // PER_PAGE + 1)
