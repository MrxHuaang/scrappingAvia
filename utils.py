"""
Zephr - Utilidades compartidas
Helpers de mapeo, timeout, y funciones auxiliares.
"""

import os
import concurrent.futures
import traceback


# ------------------------------------------------------------------
# Configuración global
# ------------------------------------------------------------------
MAX_RESULTS_PER_SOURCE = 2000

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DEFAULT_TIMEOUT = 25 if IS_SERVERLESS else 45

VALID_SOURCES = frozenset([
    "federal_register", "easa", "transport_canada", "anac_brazil", "dgac_chile",
    "aerocivil_colombia", "anac_argentina", "casa_australia", "gcaa_uae", "all"
])


# ------------------------------------------------------------------
# Mapeo de tipos de producto
# ------------------------------------------------------------------

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

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            result = future.result(timeout=timeout_seconds)

            truncated = False
            if isinstance(result, list) and len(result) > limit:
                result = result[:limit]
                truncated = True

            if truncated:
                msg = f"{label}: resultados truncados a {limit}"
                print(f"[Truncated] {msg}")
                return result, msg

            return result, None
        except concurrent.futures.TimeoutError:
            msg = f"{label}: Tiempo de espera agotado ({timeout_seconds}s)"
            print(f"[Timeout] {msg}")
            return [], msg
        except Exception as e:
            msg = f"{label}: {str(e)}"
            print(f"[Error] {msg}")
            traceback.print_exc()
            return [], msg
