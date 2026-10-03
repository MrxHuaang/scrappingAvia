"""
Scraper para DGAC Chile — Directivas de Aeronavegabilidad nacionales.

Fuente: el backend JSON del sitio de la DGAC.
    https://servicios.dgac.gob.cl/dgac_backend/api/v1/public/normativas/directivas-da/documentos

Hasta 2026 la DGAC publicaba las DA en una tabla HTML de WordPress
(/normativa/reglamentacion-aeronautica/directiva-aeronavegab/). El sitio se
rehizo como SPA Angular (www.dgac.gob.cl/dgac/#/normativa/directivas-da) y la
URL vieja devuelve 404; la pagina nueva no trae la tabla en el HTML, la arma
con este endpoint publico, que contesta a un GET liso sin browser.

Cada documento trae `estado` (vigente | derogada) y `valores` con nombre,
aplicacion, fecha_vigencia (texto libre: "30/ABR/61", "12 de julio 2013",
"Res N 0525 06 mar 2008 Cancelada ...") y el adjunto. Son directivas
nacionales chilenas, no FAA/EASA.
"""

import re

from scrapers.common.http import browser_headers, request_with_backoff
from scrapers.common.log import get_logger

log = get_logger(__name__)

DGAC_API_URL = (
    "https://servicios.dgac.gob.cl/dgac_backend/api/v1/public/normativas/"
    "directivas-da/documentos"
)

HEADERS = browser_headers({"Accept": "application/json"})

# Estado del backend -> status crudo que enrich.canonical_status entiende.
_STATUS_BY_ESTADO = {
    "vigente": "Current",
    "derogada": "Cancelled",
}


class DgacChileError(RuntimeError):
    """El listado no se pudo leer. Se levanta en vez de devolver [] para que
    populate_db registre la corrida como error: una lista vacia quedaba
    marcada `completed` y escondia que la fuente estaba muerta."""


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """Busca Directivas de Aeronavegabilidad en DGAC Chile."""
    resp = request_with_backoff("GET", DGAC_API_URL, headers=HEADERS, timeout=30, tag="DGAC Chile")
    if resp is None:
        raise DgacChileError(f"sin respuesta de {DGAC_API_URL}")
    if resp.status_code != 200:
        raise DgacChileError(f"HTTP {resp.status_code} en {DGAC_API_URL}")
    try:
        documents = resp.json()
    except ValueError as e:
        raise DgacChileError(f"respuesta no es JSON: {e}") from e
    if not isinstance(documents, list):
        raise DgacChileError(f"se esperaba una lista, llego {type(documents).__name__}")

    results = []
    for doc in documents:
        ad = _document_to_ad(doc)
        if ad and _matches_filters(ad, keyword, make, model):
            results.append(ad)

    if not results and not (keyword or make or model):
        raise DgacChileError("el listado no trajo ninguna DA")

    results = results[:max_results]
    log.info(f"[DGAC Chile] Total DAs obtenidas: {len(results)}")
    return results


def _document_to_ad(doc: dict) -> dict | None:
    if not isinstance(doc, dict) or doc.get("activo") is False:
        return None
    values = doc.get("valores") or {}
    ad_number = _clean(values.get("nombre"))
    if not ad_number or not re.match(r"DA\s*\d", ad_number, re.I):
        return None

    applicability = _clean(values.get("aplicacion"))
    date_text = _clean(values.get("fecha_vigencia"))
    estado = (doc.get("estado") or "").strip().lower()
    attachment = values.get("documento") or {}
    pdf_link = (attachment.get("url") or "").strip() if isinstance(attachment, dict) else ""
    if pdf_link.startswith("http://servicios.dgac.gob.cl/"):
        pdf_link = "https://" + pdf_link[len("http://"):]

    return {
        "Source": "DGAC Chile",
        "AD_Number": ad_number,
        "Make": _extract_make(applicability),
        "Model": "",
        "Subject": applicability,
        "Effective_Date": _parse_date(date_text),
        "PDF_Link": pdf_link,
        "Product_Type": "",
        "Status": _STATUS_BY_ESTADO.get(estado, "Current"),
        "Docket": "",
    }


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _extract_make(text):
    known = [
        "Boeing", "Cessna", "Piper", "Beechcraft", "Robinson",
        "Lycoming", "Continental", "McCauley", "Scheibe",
        "Czech Sport Aircraft", "PZL-Mielec", "Aeronca", "Champion",
    ]
    upper = text.upper().replace("MC CAULEY", "MCCAULEY")
    for m in known:
        if m.upper() in upper:
            return m
    return ""


_MONTHS = {
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "set": "09", "oct": "10", "nov": "11", "dic": "12",
}

# "02.sep.2015", "12 dic 2014", "15-jul-2013", "12 de julio 2013",
# "30/ABR/61", "13 SEP 1991". El mes va acotado a nombres de mes: con
# cualquier palabra, "Edicion 1 del 12 dic 1986" casaba "1 del 12" y se comia
# la fecha real.
_NAMED_DATE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[./\-]|\s(?:de\s+)?)\s*"
    r"((?:" + "|".join(_MONTHS) + r")[a-z]*)\.?\s*(?:[./\-]|\s(?:de\s+)?)\s*"
    r"(\d{4}|\d{2})(?!\d)",
    re.I,
)
# "01-08-1982". Solo con anio de 4 digitos: las resoluciones ("08/2/3/358")
# tambien son numeros separados por barras.
_NUMERIC_DATE = re.compile(r"(?<![\d/])(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})(?![\d/])")


def _parse_date(text):
    """Primera fecha completa del texto como YYYY-MM-DD, o "" si no hay.

    La primera es la de emision: las derogadas siguen con la fecha de la
    resolucion que las cancela. Sin dia y mes no se inventa una fecha."""
    candidates = []
    for m in _NAMED_DATE.finditer(text or ""):
        month = _MONTHS.get(m.group(2).lower()[:3])
        if month:
            candidates.append((m.start(), m.group(1), month, m.group(3)))
    for m in _NUMERIC_DATE.finditer(text or ""):
        candidates.append((m.start(), m.group(1), m.group(2).zfill(2), m.group(3)))
    for _, day, month, year in sorted(candidates):
        if len(year) == 2:
            year = ("20" if int(year) <= 30 else "19") + year
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            return f"{year}-{month}-{day.zfill(2)}"
    return ""


def _matches_filters(ad, keyword, make, model):
    if keyword:
        kl = keyword.lower()
        text = f"{ad['AD_Number']} {ad['Subject']} {ad['Make']}".lower()
        if kl not in text:
            return False
    if make:
        if make.lower() not in ad["Make"].lower() and make.lower() not in ad["Subject"].lower():
            return False
    if model:
        if model.lower() not in ad["Subject"].lower():
            return False
    return True
