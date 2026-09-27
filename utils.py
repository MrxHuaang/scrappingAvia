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
# "AD 2023-12-03" / "A.D. 95-09-02", y con prefijo pegado o "No.":
# "AD-2005-18-06" (3 documentos RULE de 1994-2026), "AD No. 2020-19-12" (1).
_DOCKET_ID_AD_RE = re.compile(rf"\bA\.?\s?D\.?\s*(?:-\s*|No\.\s*)?({_AD_NUM_LOOSE})(?![-\d])", re.I)
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


def _filter_gate(keyword, has_make: bool, has_other: bool) -> str | None:
    """La regla, una sola vez: fabricante + otro filtro, salvo identificador."""
    if is_ad_identifier(keyword):
        return None
    if not (has_make and has_other):
        return SEARCH_FILTER_HINT
    return None


def _any_value(values) -> bool:
    return any((v or "").strip() for v in (values or []))


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
    has_make = bool((make or "").strip()) or _any_value(makes)
    has_model = bool((model or "").strip()) or _any_value(models)
    others = [keyword, product_type, date_from, date_to]
    return _filter_gate(keyword, has_make, has_model or _any_value(others))


def validate_search_filters_v2(keyword="", manufacturer_ids=None, make_texts=None,
                               models=None, product_types=None,
                               product_subtypes=None, statuses=None,
                               has_pdf=None, date_from="",
                               date_to="") -> str | None:
    """
    La misma regla para el contrato v2 de busqueda: al menos un fabricante
    (`manufacturer_ids` resueltos o `make_texts` sin resolver) mas otro filtro.

    Cuentan como "otro filtro" los que acotan el conjunto de ADs: modelos,
    tipo/subtipo de producto, estados, tener PDF, fechas y keyword.
    `min_confidence` no cuenta: solo gradua cuan bien casa un modelo, sin
    modelos no acota nada. La excepcion por identificador acepta ademas lo
    que el SQL clasifica como numero de AD o docket (`search_term_kind`), asi
    un "Docket No. FAA-2023-0927" pegado tal cual tampoco pide fabricante.
    """
    if search_term_kind(keyword) != "text":
        return None
    has_make = _any_value(manufacturer_ids) or _any_value(make_texts)
    has_other = (
        _any_value(models) or _any_value(product_types)
        or _any_value(product_subtypes) or _any_value(statuses)
        or has_pdf is not None
        or _any_value([keyword, date_from, date_to])
    )
    return _filter_gate(keyword, has_make, has_other)


# ------------------------------------------------------------------
# Termino de busqueda: normalizacion y tipo.
#
# Espejo de `ad_search_term_normalize` / `ad_search_term_kind` en SQL: la BD
# decide con ellas si el termino se busca como numero de AD (prefijo), como
# docket (substring) o como texto libre, y el gate de aqui tiene que ver el
# termino igual que la BD. Las dos implementaciones se fijan con el mismo
# fixture, tests/fixtures/search_terms.json, identico en ambos repos.
# ------------------------------------------------------------------
# "AD " / "A.D. " solo delante de un numero: "Ad hoc repair" sigue siendo texto.
_TERM_AD_PREFIX_RE = re.compile(r"^A\.?\s?D\.?\s*(?=\d)", re.I)
# "Docket No. " / "Docket Number: " solo delante de algo con forma de docket.
_TERM_DOCKET_PREFIX_RE = re.compile(r"^Docket\s*(?:No\.?|Number)?\s*[:,]?\s*(?=[A-Z]{2,5}-\d)", re.I)
_TERM_AD_NUMBER_RE = re.compile(rf"^{_AD_NUM_LOOSE}$", re.I)


def search_term_normalize(term) -> str:
    """
    Guiones Unicode (– —) -> "-", espacios colapsados, sin bordes y sin el
    prefijo "AD"/"A.D." o "Docket No." cuando precede a un identificador.
    El resto (mayusculas incluidas) se deja como vino: la BD compara con ILIKE.
    """
    s = (term or "").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip()
    s = _TERM_AD_PREFIX_RE.sub("", s)
    s = _TERM_DOCKET_PREFIX_RE.sub("", s)
    return s.strip()


def search_term_kind(term) -> str:
    """'ad_number' | 'docket' | 'text' para el termino ya normalizado."""
    norm = search_term_normalize(term)
    if _TERM_AD_NUMBER_RE.match(norm):
        return "ad_number"
    if _DOCKET_QUERY_RE.match(norm):
        return "docket"
    return "text"


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
