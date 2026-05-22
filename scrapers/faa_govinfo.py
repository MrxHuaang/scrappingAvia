"""
Scraper para GovInfo API - Airworthiness Directives (FAA via Federal Register collection)

Estrategia: iterar paquetes FR-YYYY-MM-DD dia a dia (solo dias habiles).
Para cada paquete: GET /packages/{pkgId}/granules?granuleClass=RULE
Filtrar granulos con "Airworthiness" en el titulo -> ADs de FAA.

No usa el endpoint POST /search (los filtros estan rotos en la API v3).
Requiere API key gratuita: https://api.govinfo.gov/signup/
Configurar en .env: GOVINFO_API_KEY=...

Rate limit: 40 req/s. Usamos sleep(0.025) = 40 req/s max.
"""

import os
import re
import time
from datetime import date, timedelta, datetime

import requests

from utils import extract_make_model, normalize_ad_number


GOVINFO_BASE = "https://api.govinfo.gov"
START_YEAR = 1995
_REQ_DELAY = 0.025  # 40 req/s


def _get_key() -> str | None:
    return os.environ.get("GOVINFO_API_KEY", "").strip() or None


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """GET con backoff exponencial en 429/5xx. 404 retorna None sin reintentar."""
    key = _get_key()
    p = dict(params or {})
    if key:
        p["api_key"] = key

    url = path if path.startswith("http") else f"{GOVINFO_BASE}{path}"
    delay = 2
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=p, timeout=30)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 403:
                print("[GovInfo] 403 - revisar GOVINFO_API_KEY")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"[GovInfo] GET error: {e}")
            time.sleep(delay)
            delay *= 2
    return None


def _granules_for_package(pkg_id: str) -> list[dict]:
    """
    Retorna todos los granulos RULE de un paquete FR (con paginacion).
    """
    granules = []
    params = {"granuleClass": "RULE", "pageSize": 100, "offsetMark": "*"}
    data = _get(f"/packages/{pkg_id}/granules", params)
    if not data:
        return []

    granules.extend(data.get("granules") or [])

    # Paginacion por cursor URL
    next_url = data.get("nextPage")
    while next_url:
        page_data = _get(next_url)
        if not page_data:
            break
        granules.extend(page_data.get("granules") or [])
        next_url = page_data.get("nextPage")
        time.sleep(_REQ_DELAY)

    return granules


def search_ads(year_from: int = START_YEAR, year_to: int | None = None) -> list[dict]:
    """
    Busca todos los ADs de FAA via GovInfo iterando paquetes FR dia a dia.
    Sin GOVINFO_API_KEY -> retorna [] sin fallar el pipeline.
    """
    if not _get_key():
        print("[GovInfo] GOVINFO_API_KEY no configurada en .env - skip")
        return []

    year_to = year_to or datetime.now().year
    all_results: list[dict] = []
    year_counts: dict[int, int] = {}

    start_date = date(year_from, 1, 1)
    end_date = date(year_to, 12, 31)

    current = start_date
    total_days = (end_date - start_date).days + 1
    checked = 0

    while current <= end_date:
        # FR solo se publica en dias habiles (lun-vie)
        if current.weekday() < 5:
            pkg_id = f"FR-{current.isoformat()}"
            granules = _granules_for_package(pkg_id)

            for g in granules:
                title = g.get("title", "") or ""
                if "Airworthiness" not in title and "airworthiness" not in title:
                    continue
                parsed = _parse_granule(g, pkg_id)
                if parsed:
                    all_results.append(parsed)
                    yr = current.year
                    year_counts[yr] = year_counts.get(yr, 0) + 1

            time.sleep(_REQ_DELAY)

        current += timedelta(days=1)
        checked += 1
        if checked % 100 == 0:
            pct = checked / total_days * 100
            print(f"[GovInfo] {current.isoformat()} ({pct:.0f}%) {len(all_results)} ADs so far")

    # Resumen por año
    for yr in sorted(year_counts):
        print(f"[GovInfo] {yr}: {year_counts[yr]} ADs")
    print(f"[GovInfo] Total {year_from}-{year_to}: {len(all_results)} ADs")
    return all_results


def _parse_granule(g: dict, pkg_id: str) -> dict | None:
    """
    Parsea un granulo del listado a formato estandar.
    Los datos disponibles: granuleId, title, dateIssued, granuleClass, granuleLink.
    """
    try:
        title = g.get("title", "") or ""
        granule_id = g.get("granuleId", "") or ""
        date_issued = g.get("dateIssued", "") or ""

        # PDF link: patron conocido de GovInfo
        pdf_link = f"https://api.govinfo.gov/packages/{pkg_id}/granules/{granule_id}/pdf"

        # Extraer AD number del titulo primero
        ad_number = ""
        m = re.search(r"\bAD\s*(\d{4}[-–—]\d{2}[-–—]\d{2,4})\b", title, re.I)
        if m:
            ad_number = "AD " + m.group(1)
        else:
            # Patron numero sin prefijo
            m = re.search(r"\b(\d{4}[-–—]\d{2}[-–—]\d{2,4})\b", title)
            if m:
                ad_number = m.group(1)
            else:
                # Fallback al granuleId (ej. "2024-00228")
                ad_number = granule_id

        ad_number = normalize_ad_number(ad_number)
        if not ad_number:
            return None

        make, model = extract_make_model(title, "")

        # Quitar prefijo estandar del titulo
        subject = re.sub(r"^Airworthiness Directives[;:]\s*", "", title, flags=re.I).strip()
        if not subject:
            subject = title

        title_lower = title.lower()
        if "proposed" in title_lower or "nprm" in title_lower:
            status = "NPRM"
        elif "emergency" in title_lower:
            status = "Emergency AD"
        else:
            status = "Final Rule"

        if re.search(r"\bengine\b", title_lower):
            product_type = "Engine"
        elif re.search(r"\bpropeller\b", title_lower):
            product_type = "Propeller"
        else:
            product_type = "Aircraft"

        return {
            "Source": "FAA (GovInfo)",
            "AD_Number": ad_number,
            "Subject": subject,
            "Make": make.strip(),
            "Model": model.strip(),
            "Effective_Date": date_issued,
            "Status": status,
            "Product_Type": product_type,
            "Docket": pkg_id,
            "PDF_Link": pdf_link,
        }
    except Exception as e:
        print(f"[GovInfo] Error parseando granule: {e}")
        return None
