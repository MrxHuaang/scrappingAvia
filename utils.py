"""
Zephr - Utilidades compartidas
Helpers de mapeo, timeout, y funciones auxiliares.
"""

import concurrent.futures
import traceback


# ------------------------------------------------------------------
# Configuración global
# ------------------------------------------------------------------
MAX_RESULTS_PER_SOURCE = 500

VALID_SOURCES = frozenset([
    "federal_register", "easa", "transport_canada", "anac_brazil", "all"
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

def run_with_timeout(fn, timeout_seconds=30, label="scraper"):
    """
    Ejecuta una función con un timeout estricto.
    Si la función no termina en el plazo, devuelve ([], error_msg).
    Si lanza excepción, devuelve ([], error_msg).
    En éxito devuelve (results, None).
    Aplica MAX_RESULTS_PER_SOURCE como tope.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            result = future.result(timeout=timeout_seconds)

            # Aplicar límite global de resultados por fuente
            truncated = False
            if isinstance(result, list) and len(result) > MAX_RESULTS_PER_SOURCE:
                result = result[:MAX_RESULTS_PER_SOURCE]
                truncated = True

            if truncated:
                msg = f"{label}: resultados truncados a {MAX_RESULTS_PER_SOURCE}"
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
