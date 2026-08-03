"""
HTTP compartido para los scrapers: headers canonicos y GET con backoff.

Antes de esto habia 12 definiciones de HEADERS con 9 User-Agents distintos y
5 copias del mismo bucle de reintentos. Un solo UA de navegador y un solo UA
de API honesto ("AD-Scraper/1.0", para APIs publicas que no requieren
mimetizarse) hacen el comportamiento reproducible y auditable.
"""

import time

import requests

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Para APIs publicas (Federal Register, GovInfo): identificarse honestamente.
API_HEADERS = {
    "User-Agent": "AD-Scraper/1.0",
    "Accept": "application/json",
}


def browser_headers(extra: dict | None = None) -> dict:
    """Headers base de navegador; `extra` agrega o pisa claves por sitio
    (Accept-Language regional, Referer, etc.)."""
    h = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra:
        h.update(extra)
    return h


def get_with_backoff(
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
    retries: int = 3,
    delay: float = 2.0,
    tag: str = "http",
):
    """GET con backoff exponencial en 429/5xx y errores de red.

    Devuelve el Response final (cualquier status < 500 distinto de 429) o
    None si todos los intentos fallaron. Los 4xx NO se reintentan (no se
    curan solos): cada llamador conserva su propia semantica para ellos.
    """
    getter = session or requests
    for attempt in range(retries):
        try:
            resp = getter.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            print(f"[{tag}] GET error: {e} (intento {attempt + 1}/{retries})")
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            print(f"[{tag}] HTTP {resp.status_code} (intento {attempt + 1}/{retries})")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return None
