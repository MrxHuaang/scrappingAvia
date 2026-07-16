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
    "aerocivil_colombia", "anac_argentina", "casa_australia", "gcaa_uae", "all"
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
