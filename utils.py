"""
AD Scraper - Utilidades compartidas
Helpers de mapeo, timeout, y funciones auxiliares.
"""

import concurrent.futures
import traceback


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
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            result = future.result(timeout=timeout_seconds)
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
