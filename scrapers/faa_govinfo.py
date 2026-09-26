"""
Scraper para GovInfo API - Airworthiness Directives (FAA via Federal Register collection)

Estrategia: iterar paquetes FR-YYYY-MM-DD dia a dia (solo dias habiles).
Para cada paquete: GET /packages/{pkgId}/granules?granuleClass=RULE
Filtrar granulos con "Airworthiness" en el titulo -> ADs de FAA.

Numero AD real: el titulo del granulo casi nunca lo trae. Se obtiene del
MODS XML de cada granulo (/packages/{pkgId}/granules/{granId}/mods), que
contiene "AD 2024-01-02", "Docket No. FAA-2024-0027" y "Amendment 39-22653".
Granulos sin numero AD en MODS ni en titulo se DESCARTAN (nunca usar
granuleId como ad_number: rompe el dedupe contra FR API).

Cache local en outputs/govinfo_mods_cache.json: granuleId -> resultado MODS
(incluye negativos) para que re-runs no repitan los ~1 fetch MODS por AD.

No usa el endpoint POST /search (los filtros estan rotos en la API v3).
Requiere API key gratuita: https://api.govinfo.gov/signup/
Configurar en .env: GOVINFO_API_KEY=...

Rate limit: 40 req/s, 36000 req/hr por key (docs usgpo/api). Usamos
sleep(0.025) y pausa de 60s si X-RateLimit-Remaining < 50.
"""

import os
import re
import time
from datetime import date, timedelta, datetime

import requests

from utils import normalize_ad_number
from scrapers.federal_register import classify_product_type
from scrapers.common.http import get_with_backoff
from scrapers.common.cache import load_json_cache, save_json_cache
from scrapers.common.fr_applicability import applicability_fields

from scrapers.common.log import get_logger

log = get_logger(__name__)


GOVINFO_BASE = "https://api.govinfo.gov"
START_YEAR = 1995
_REQ_DELAY = 0.025  # 40 req/s

_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs"
)
MODS_CACHE_PATH = os.path.join(_OUTPUTS_DIR, "govinfo_mods_cache.json")
_CACHE_SAVE_EVERY = 200  # fetches entre guardados incrementales

# Numeros AD: 4 digitos de anio (2024-01-02) o 2 (98-21-09, era pre-2000).
# –/— = guiones en/em (escapados para mantener el archivo ASCII-puro).
_AD_RE = re.compile("\\bAD\\s*(\\d{2,4}[-–—]\\d{2}[-–—]\\d{2,4})\\b", re.I)
# Dockets modernos (FAA-2024-0027) y antiguos (98-NM-123-AD)
_DOCKET_MODERN_RE = re.compile("Docket\\s*No\\.?\\s*[:,]?\\s*(FAA[-\\s]?\\d{4}[-–—]\\d{1,6})", re.I)
_DOCKET_LEGACY_RE = re.compile("Docket\\s*No\\.?\\s*[:,]?\\s*(\\d{2,4}[-–—][A-Z]{2}[-–—]\\d{1,4}[-–—]AD)", re.I)
_AMENDMENT_RE = re.compile("Amendment\\s*(?:No\\.?\\s*)?(39[-–—]\\d{3,6})", re.I)


# Session compartida: reusa la conexion TLS (sin esto cada request paga
# handshake ~1s y un backfill completo pasa de ~1h a varias horas).
_session = requests.Session()


def _get_key() -> str | None:
    return os.environ.get("GOVINFO_API_KEY", "").strip() or None


def _check_rate_headers(resp) -> None:
    """Pausa si la cuota horaria esta por agotarse."""
    try:
        remaining = int(resp.headers.get("X-RateLimit-Remaining", "9999"))
    except (TypeError, ValueError):
        return
    if remaining < 50:
        log.info("[GovInfo] Rate limit casi agotado, pausa de 60s...")
        time.sleep(60)


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    """GET JSON con backoff exponencial en 429/5xx. 404 retorna None sin reintentar."""
    key = _get_key()
    p = dict(params or {})
    if key:
        p["api_key"] = key

    url = path if path.startswith("http") else f"{GOVINFO_BASE}{path}"
    resp = get_with_backoff(url, session=_session, params=p, timeout=30,
                            retries=retries, tag="GovInfo")
    if resp is None or resp.status_code == 404:
        return None
    if resp.status_code == 403:
        log.info("[GovInfo] 403 - revisar GOVINFO_API_KEY")
        return None
    if resp.status_code >= 400:
        log.info(f"[GovInfo] HTTP {resp.status_code}")
        return None
    _check_rate_headers(resp)
    return resp.json()


def _get_text(path: str, params: dict | None = None, retries: int = 3) -> str | None:
    """GET texto plano (MODS XML). Mismo backoff que _get()."""
    key = _get_key()
    p = dict(params or {})
    if key:
        p["api_key"] = key

    url = path if path.startswith("http") else f"{GOVINFO_BASE}{path}"
    resp = get_with_backoff(url, session=_session, params=p, timeout=30,
                            retries=retries, tag="GovInfo")
    if resp is None or resp.status_code == 404:
        return None
    if resp.status_code == 403:
        log.info("[GovInfo] 403 en MODS - revisar GOVINFO_API_KEY")
        return None
    if resp.status_code >= 400:
        log.info(f"[GovInfo] HTTP {resp.status_code} en MODS")
        return None
    _check_rate_headers(resp)
    return resp.text


# ---------------------------------------------------------------------------
# MODS XML: extraccion de numero AD real, docket y amendment
# ---------------------------------------------------------------------------
def _parse_mods(xml_text: str) -> dict:
    """
    Extrae identidad del AD desde el MODS XML del granulo.
    Retorna {"ad": "AD 2024-01-02"|None, "docket": "FAA-2024-0027"|None,
             "amend": "39-22653"|None}.
    """
    ad_number = None
    m = _AD_RE.search(xml_text)
    if m:
        ad_number = "AD " + normalize_ad_number(m.group(1))

    docket = None
    m = _DOCKET_MODERN_RE.search(xml_text)
    if m:
        docket = normalize_ad_number(m.group(1)).replace(" ", "-").upper()
    else:
        m = _DOCKET_LEGACY_RE.search(xml_text)
        if m:
            docket = normalize_ad_number(m.group(1)).upper()

    amend = None
    m = _AMENDMENT_RE.search(xml_text)
    if m:
        amend = normalize_ad_number(m.group(1))

    return {"ad": ad_number, "docket": docket, "amend": amend}


def _load_cache() -> dict:
    data = load_json_cache(MODS_CACHE_PATH, default=None)
    if isinstance(data, dict) and data.get("version") == 1:
        return data
    return {"version": 1, "granules": {}}


def _save_cache(cache: dict) -> None:
    save_json_cache(MODS_CACHE_PATH, cache)


def _fetch_mods(pkg_id: str, granule_id: str, cache: dict, stats: dict) -> dict:
    """
    Resultado MODS para un granulo, con cache (incluye negativos: ad=None).
    stats: contador mutable {"hits": int, "fetches": int}.
    """
    granules = cache["granules"]
    if granule_id in granules:
        stats["hits"] += 1
        return granules[granule_id]

    xml_text = _get_text(f"/packages/{pkg_id}/granules/{granule_id}/mods")
    time.sleep(_REQ_DELAY)
    entry = _parse_mods(xml_text) if xml_text else {"ad": None, "docket": None, "amend": None}
    entry["ts"] = date.today().isoformat()
    granules[granule_id] = entry
    stats["fetches"] += 1
    if stats["fetches"] % _CACHE_SAVE_EVERY == 0:
        _save_cache(cache)
    return entry


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


def search_ads(year_from: int = START_YEAR, year_to: int | None = None,
               date_from: str | None = None, date_to: str | None = None,
               use_mods: bool = True) -> list[dict]:
    """
    Busca todos los ADs de FAA via GovInfo iterando paquetes FR dia a dia.
    date_from/date_to (YYYY-MM-DD) acotan el rango y habilitan runs
    incrementales (--since) y tests de muestra. Si se omiten, usa year_from/to.
    Sin GOVINFO_API_KEY -> retorna [] sin fallar el pipeline.
    """
    if not _get_key():
        log.info("[GovInfo] GOVINFO_API_KEY no configurada en .env - skip")
        return []

    year_to = year_to or datetime.now().year
    start_date = date.fromisoformat(date_from) if date_from else date(year_from, 1, 1)
    end_date = date.fromisoformat(date_to) if date_to else date(year_to, 12, 31)
    today = date.today()
    if end_date > today:
        end_date = today

    cache = _load_cache()
    stats = {"hits": 0, "fetches": 0}
    skipped_no_ad = 0

    all_results: list[dict] = []
    year_counts: dict[int, int] = {}

    current = start_date
    total_days = (end_date - start_date).days + 1
    checked = 0

    try:
        while current <= end_date:
            # FR solo se publica en dias habiles (lun-vie)
            if current.weekday() < 5:
                pkg_id = f"FR-{current.isoformat()}"
                granules = _granules_for_package(pkg_id)

                for g in granules:
                    title = g.get("title", "") or ""
                    if "airworthiness" not in title.lower():
                        continue
                    granule_id = g.get("granuleId", "") or ""
                    mods = None
                    if use_mods and granule_id:
                        mods = _fetch_mods(pkg_id, granule_id, cache, stats)
                    parsed = _parse_granule(g, pkg_id, mods)
                    if parsed is None:
                        skipped_no_ad += 1
                        continue
                    all_results.append(parsed)
                    yr = current.year
                    year_counts[yr] = year_counts.get(yr, 0) + 1

                time.sleep(_REQ_DELAY)

            current += timedelta(days=1)
            checked += 1
            if checked % 100 == 0:
                pct = checked / total_days * 100
                log.info(
                    f"[GovInfo] {current.isoformat()} ({pct:.0f}%) {len(all_results)} ADs | "
                    f"MODS cache hits: {stats['hits']}, fetches: {stats['fetches']}"
                )
    finally:
        _save_cache(cache)

    # Resumen por anio
    for yr in sorted(year_counts):
        log.info(f"[GovInfo] {yr}: {year_counts[yr]} ADs")
    log.info(
        f"[GovInfo] Total {start_date.isoformat()} a {end_date.isoformat()}: "
        f"{len(all_results)} ADs | skipped sin numero AD: {skipped_no_ad} | "
        f"MODS cache hits: {stats['hits']}, fetches: {stats['fetches']}"
    )
    return all_results


def _parse_granule(g: dict, pkg_id: str, mods: dict | None = None) -> dict | None:
    """
    Parsea un granulo del listado a formato estandar.
    Datos del listado: granuleId, title, dateIssued, granuleClass, granuleLink.
    mods (opcional): {"ad", "docket", "amend"} extraido del MODS XML.
    Retorna None si no hay numero AD real (ni MODS ni titulo): esos granulos
    se descartan para no contaminar el dedupe con granuleIds.
    """
    try:
        title = g.get("title", "") or ""
        granule_id = g.get("granuleId", "") or ""
        date_issued = g.get("dateIssued", "") or ""
        mods = mods or {}

        # PDF publico (no requiere API key, a diferencia de api.govinfo.gov)
        pdf_link = f"https://www.govinfo.gov/content/pkg/{pkg_id}/pdf/{granule_id}.pdf"

        # Numero AD: MODS primero (autoritativo), titulo como fallback
        ad_number = mods.get("ad") or ""
        if not ad_number:
            m = _AD_RE.search(title)
            if m:
                ad_number = "AD " + normalize_ad_number(m.group(1))

        ad_number = normalize_ad_number(ad_number)
        if not ad_number:
            return None  # sin numero AD real: descartar (nunca granuleId)

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

        # Misma regla que el Federal Register. La de aquí buscaba `\bengine\b`,
        # que no casa el plural con el que la FAA titula ("Turbofan Engines"):
        # los ADs de motor salían como Aircraft.
        product_type = classify_product_type(subject) or "Aircraft"

        return {
            "Source": "FAA (GovInfo)",
            "AD_Number": ad_number,
            "Subject": subject,
            # Misma extraccion que el Federal Register; el granulo no trae
            # abstract, asi que los modelos salen solo del titulo.
            **applicability_fields(title),
            "Effective_Date": date_issued,
            "Status": status,
            "Product_Type": product_type,
            "Docket": mods.get("docket") or pkg_id,
            "PDF_Link": pdf_link,
            "Amendment": mods.get("amend") or "",
            "GranuleId": granule_id,
        }
    except Exception as e:
        log.info(f"[GovInfo] Error parseando granule: {e}")
        return None
