"""
Zephr - Utilidades compartidas
Helpers de mapeo, timeout, y funciones auxiliares.
"""

import os
import re
import concurrent.futures
import traceback


# ------------------------------------------------------------------
# Configuración global
# ------------------------------------------------------------------
MAX_RESULTS_PER_SOURCE = 20000

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DEFAULT_TIMEOUT = 25 if IS_SERVERLESS else 45

VALID_SOURCES = frozenset([
    "federal_register", "easa", "transport_canada", "anac_brazil", "dgac_chile",
    "aerocivil_colombia", "anac_argentina", "casa_australia", "gcaa_uae",
    "uk_caa", "all"
])


# ------------------------------------------------------------------
# Mapeo de tipos de producto
# ------------------------------------------------------------------

_KNOWN_MAKES = [
    "The Boeing Company", "Boeing", "Airbus", "Airbus S.A.S.",
    "Bombardier", "Embraer", "Cessna", "Textron Aviation",
    "Gulfstream", "Dassault", "Pilatus", "Beechcraft",
    "Piper", "Mooney", "Cirrus", "Diamond",
    "Rolls-Royce", "General Electric", "Pratt & Whitney",
    "CFM International", "Honeywell", "Safran",
    "Bell", "Sikorsky", "Leonardo", "Robinson",
    "de Havilland", "ATR", "Saab", "BAE Systems",
    "McDonnell Douglas", "Douglas", "Lockheed", "Fokker",
    "Israel Aerospace", "Mitsubishi", "Hawker", "Learjet",
]

_MODEL_PATTERNS = [
    r"\b(737[-\s]?\d{0,4}(?:[-/]\d+)?(?:\s*(?:MAX|NG))?)\b",
    r"\b(A\s?3[0-9]{2}(?:[-/]\d+)?(?:neo)?)\b",
    r"\b(777[-\s]?\d{0,4})\b",
    r"\b(787[-\s]?\d{0,4})\b",
    r"\b(747[-\s]?\d{0,4})\b",
    r"\b(CL[-\s]?\d{3,4})\b",
    r"\b(DHC[-\s]?\d+)\b",
]


def extract_make_model(title: str, abstract: str = "") -> tuple[str, str]:
    """Extrae (make, model) del título y abstract de un AD."""
    text = f"{title or ''} {abstract or ''}"
    text_lower = text.lower()

    make = ""
    for known in _KNOWN_MAKES:
        if known.lower() in text_lower:
            make = known
            break

    model = ""
    m = re.search(r"Model\s+([\w\-/]+(?:\s+[\w\-/]+)?)", text)
    if m:
        model = m.group(1)
    else:
        for pat in _MODEL_PATTERNS:
            m = re.search(pat, text, re.I)
            if m:
                model = m.group(1)
                break

    return make, model


def normalize_ad_number(raw: str) -> str:
    """
    Normaliza ad_number a forma canónica BARE: strip espacios, guion unicode
    -> ASCII, y quita el prefijo "AD "/"A.D." (la fase GovInfo MODS lo agrega
    como "AD 2026-13-08"; el resto de fuentes guarda el número pelado). Sin
    esto, el mismo AD queda como "AD 2026-13-08" y "2026-13-08" -> rompe dedupe
    por (source, ad_number) y el título muestra el prefijo. Idempotente.
    """
    if not raw:
        return ""
    s = raw.strip().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^A\.?\s*D\.?\s+", "", s, flags=re.I)  # "AD " / "A.D. " -> ""
    return s.strip()


# ------------------------------------------------------------------
# Identidad del AD desde el header regulatorio del Federal Register.
#
# La FR API expone ese header como array estructurado `docket_ids`:
#   ["Docket No. FAA-2023-0927", "Project Identifier MCAI-2023-00013-T",
#    "Amendment 39-22461", "AD 2023-12-03"]
#
# Es la unica fuente confiable del numero AD. Minarlo del cuerpo del rule
# ("primer AD nnnn-nn-nn del texto") produce el numero del AD *superseded*,
# porque el rule cita al que reemplaza antes de nombrarse: asi la fila quedaba
# con el numero de otro AD sobre el PDF correcto.
#
# Formas legadas toleradas (1995-1999): el AD viaja pegado al amendment
# ("Amendment 39-9206, AD 95-09-02") y con espacios internos ("AD 95-01- 07").
# ------------------------------------------------------------------
# El sufijo "-AD" aparece en entradas legadas ("AD 98-18-12-AD"): se descarta.
_AD_NUM_LOOSE = r"(?:\d{4}|\d{2})\s*-\s*\d{2}\s*-\s*\d{2,4}(?:\s*R\s*\d+)?(?:\s*-\s*AD\b)?"
# "AD 2023-12-03" / "A.D. 95-09-02"
_DOCKET_ID_AD_RE = re.compile(rf"\bA\.?\s?D\.?\s*({_AD_NUM_LOOSE})(?![-\d])", re.I)
# Entrada que es SOLO el numero ("2023-12-03"): la FR API a veces omite el
# prefijo, y normalize_ad_number() lo quita cuando esta.
_DOCKET_ID_AD_BARE_RE = re.compile(rf"^({_AD_NUM_LOOSE})$")
_DOCKET_ID_DOCKET_RE = re.compile(r"Docket\s*(?:No\.?|Number)?\s*[:,]?\s*(.+)", re.I)
_DOCKET_ID_AMEND_RE = re.compile(r"Amendment\s*(?:No\.?\s*)?(39\s*-\s*\d{3,6})", re.I)
_DOCKET_ID_PROJECT_RE = re.compile(r"Project\s*Identifier\s*[:,]?\s*([\w\-]+)", re.I)


def ad_identity_from_docket_ids(docket_ids) -> dict:
    """
    {ad, docket, amendment, project} desde el array `docket_ids` de la FR API.
    Valores ausentes -> None. `ad` viene normalizado y sin prefijo ("2023-12-03").
    """
    out = {"ad": None, "docket": None, "amendment": None, "project": None}
    for raw in docket_ids or []:
        s = str(raw or "").replace("–", "-").replace("—", "-")
        s = re.sub(r"\s+", " ", s).strip().rstrip(";,.")
        if not s:
            continue
        m = _DOCKET_ID_AD_RE.search(s) or _DOCKET_ID_AD_BARE_RE.match(s)
        if m and not out["ad"]:
            num = re.sub(r"\s+", "", m.group(1)).upper()
            out["ad"] = re.sub(r"-AD$", "", num)
        m = _DOCKET_ID_AMEND_RE.search(s)
        if m and not out["amendment"]:
            out["amendment"] = re.sub(r"\s+", "", m.group(1))
        m = _DOCKET_ID_PROJECT_RE.search(s)
        if m and not out["project"]:
            out["project"] = m.group(1).strip()
            continue
        m = _DOCKET_ID_DOCKET_RE.search(s)
        if m and not out["docket"]:
            # "Docket No. FAA-2023-0927" -> FAA-2023-0927; corta el amendment
            # cuando ambos comparten la misma entrada.
            docket = re.split(r"[;,]|\bAmendment\b", m.group(1), maxsplit=1)[0]
            docket = docket.strip().rstrip(";,.").upper()
            if docket:
                out["docket"] = docket
    return out


# ------------------------------------------------------------------
# Patrones de numero de AD por autoridad (compartidos por scrapers y
# scripts de supersession/equivalents). Lookarounds anti-colision para
# no matchear dentro de dockets (FAA-2025-1364), fechas o SB numbers.
# ------------------------------------------------------------------
# FAA: 2024-01-02 o 98-21-09, sufijo de revision opcional (2018-23-51R1)
AD_NUM_FAA = r"(?<![-\d])(?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?(?![-\d])"
# EASA moderno: 2024-0195(R1); legado nacional con prefijo: F-1998-173, US-2002-08-03
AD_NUM_EASA = r"(?<![-\w])(?:[A-Z]{1,2}-)?\d{4}-\d{3,4}(?:R\d+)?(?![-\d])"
# Transport Canada: CF-2015-19(R1), legado CF-90-03
AD_NUM_TC = r"CF-\d{2,4}-\d{2,3}(?:R\d+)?(?![-\d])"


# ------------------------------------------------------------------
# Gate de filtros de busqueda
#
# Una busqueda sin filtros barre la autoridad entera: 18k ADs de EASA para
# alguien que escribio "fuel". El gate exige `make` + al menos otro filtro,
# con una excepcion deliberada: pegar un numero de AD (o un docket) es el caso
# de uso mas comun de todos y no tiene por que conocer el fabricante.
# ------------------------------------------------------------------
_AD_QUERY_RE = re.compile(
    rf"^(?:A\.?\s?D\.?\s*)?(?:{AD_NUM_FAA}|{AD_NUM_EASA}|{AD_NUM_TC})$", re.I
)
# Docket/proyecto: "FAA-2023-0927", "MCAI-2023-00013-T"
_DOCKET_QUERY_RE = re.compile(r"^[A-Z]{2,5}-\d{4}-\d{3,6}(?:-[A-Z]{1,3})?$", re.I)

SEARCH_FILTER_HINT = (
    "Add a make plus at least one more filter (model, product type or date range) "
    "— or search a specific AD number or docket."
)


def is_ad_identifier(term: str) -> bool:
    """True si `term` tiene forma de numero de AD (FAA/EASA/TC) o de docket."""
    s = (term or "").strip().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s)
    if not s:
        return False
    return bool(_AD_QUERY_RE.match(s) or _DOCKET_QUERY_RE.match(s))


#: Tope de entradas por filtro multivalor. Es el mismo numero que impone la RPC
#: `search_ads` (22023 si se pasa); rechazarlo aqui evita un viaje al servidor
#: para recibir el mismo no.
MAX_FILTER_VALUES = 40


def normalize_filter_values(values, cap: int = MAX_FILTER_VALUES) -> list[str]:
    """
    Limpia una lista de valores de filtro: descarta vacios, quita duplicados
    (sin distinguir mayusculas, que es como matchea el ILIKE) preservando el
    orden en que el usuario los eligio, y corta en `cap`.

    Un valor en blanco no se deja pasar: en la RPC se convertiria en '%%' y
    matchearia todo, o sea lo contrario de lo que significa agregar un filtro.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        v = (raw or "").strip()
        if not v:
            continue
        key = v.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) >= cap:
            break
    return out


def validate_search_filters(keyword="", make="", model="", product_type="",
                            date_from="", date_to="", makes=None,
                            models=None) -> str | None:
    """
    Mensaje de error si la combinacion de filtros no alcanza, o None si pasa.
    Unica fuente de la regla: la usan /api/search y /api/search/stream, que
    antes validaban cada uno lo suyo y no coincidian.

    `makes`/`models` son las listas multivalor. Cuentan exactamente igual que
    los escalares: lo que la regla pide es "algun fabricante mas otro filtro",
    no "un" fabricante — elegir tres Airbus no deja la busqueda mas abierta que
    elegir uno.
    """
    if is_ad_identifier(keyword):
        return None
    has_make = bool((make or "").strip()) or any((v or "").strip() for v in (makes or []))
    if not has_make:
        return SEARCH_FILTER_HINT
    has_model = bool((model or "").strip()) or any((v or "").strip() for v in (models or []))
    others = [keyword, product_type, date_from, date_to]
    if not (has_model or any((v or "").strip() for v in others)):
        return SEARCH_FILTER_HINT
    return None


def map_product_type(product_type):
    """Mapea tipo de producto al formato de Transport Canada."""
    mapping = {
        "Aircraft": "aircraft",
        "Engine": "engine",
        "Propeller": "propeller",
    }
    return mapping.get(product_type, "aircraft")


# ------------------------------------------------------------------
# Ejecución con timeout
# ------------------------------------------------------------------

def run_with_timeout(fn, timeout_seconds=None, label="scraper", max_results=None):
    """
    Ejecuta una función con un timeout estricto.
    Si la función no termina en el plazo, devuelve ([], error_msg).
    Si lanza excepción, devuelve ([], error_msg).
    En éxito devuelve (results, None).
    Aplica max_results (o MAX_RESULTS_PER_SOURCE) como tope.
    """
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_TIMEOUT

    limit = max_results if max_results else MAX_RESULTS_PER_SOURCE

    # NOTE: a running thread cannot be force-killed in CPython. On timeout we do NOT
    # use `with ... as executor` because its __exit__ calls shutdown(wait=True), which
    # would block on the hung worker and defeat the timeout. Instead we shut down with
    # wait=False so the request returns immediately; the orphaned (daemon) thread is
    # abandoned and reaped at process exit.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        result = future.result(timeout=timeout_seconds)

        truncated = False
        if isinstance(result, list) and len(result) > limit:
            result = result[:limit]
            truncated = True

        executor.shutdown(wait=False)
        if truncated:
            msg = f"{label}: resultados truncados a {limit}"
            print(f"[Truncated] {msg}")
            return result, msg

        return result, None
    except concurrent.futures.TimeoutError:
        msg = f"{label}: Tiempo de espera agotado ({timeout_seconds}s)"
        print(f"[Timeout] {msg}")
        executor.shutdown(wait=False, cancel_futures=True)
        return [], msg
    except Exception as e:
        msg = f"{label}: {str(e)}"
        print(f"[Error] {msg}")
        traceback.print_exc()
        executor.shutdown(wait=False, cancel_futures=True)
        return [], msg
