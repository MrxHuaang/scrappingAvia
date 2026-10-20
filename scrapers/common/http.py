"""
HTTP compartido para los scrapers: headers canonicos y GET con backoff.

Antes de esto habia 12 definiciones de HEADERS con 9 User-Agents distintos y
5 copias del mismo bucle de reintentos. Un solo UA de navegador y un solo UA
de API honesto ("AD-Scraper/1.0", para APIs publicas que no requieren
mimetizarse) hacen el comportamiento reproducible y auditable.
"""

import threading
import time
from collections.abc import Callable

import requests

from scrapers.common.log import get_logger

log = get_logger(__name__)

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


class RateLimiter:
    """Espacia el inicio de las peticiones de varios hilos a `per_second` por
    segundo como maximo. `wait()` reserva el siguiente hueco y duerme hasta
    el; el lock cubre la reserva, no la espera."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        if slot > now:
            time.sleep(slot - now)


def request_with_backoff(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict | None = None,
    json: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
    retries: int = 3,
    delay: float = 2.0,
    throttle: Callable[[], None] | None = None,
    failures: list[str] | None = None,
    tag: str = "http",
):
    """Peticion con backoff exponencial en 429/5xx y errores de red (timeouts
    incluidos).

    Devuelve el Response final (cualquier status < 500 distinto de 429) o
    None si todos los intentos fallaron. Los 4xx NO se reintentan (no se
    curan solos): cada llamador conserva su propia semantica para ellos.

    `throttle` (p. ej. `RateLimiter.wait`) corre antes de cada intento,
    reintentos incluidos: un reintento cuenta contra el mismo limite. En
    `failures`, si se pasa, cada intento fallido deja su motivo ("HTTP 429",
    "request failed: Timeout"), para que el llamador diga por que no hubo
    respuesta sin leer los logs.

    Existe aparte de `get_with_backoff` para las APIs que consultan por POST
    (el endpoint `filtered` de DRS): el bucle de reintentos es el mismo y no
    debe volver a copiarse. Los logs solo llevan el metodo y el status, nunca
    headers: por ahi viajan las API keys.
    """
    getter = session or requests
    call = getattr(getter, method.lower())
    kwargs: dict = {"params": params, "headers": headers, "timeout": timeout}
    if json is not None:
        kwargs["json"] = json
    for attempt in range(retries):
        if throttle is not None:
            throttle()
        try:
            resp = call(url, **kwargs)
        except requests.RequestException as e:
            log.warning(f"[{tag}] {method.upper()} error: {type(e).__name__}: {e} (intento {attempt + 1}/{retries})")
            if failures is not None:
                failures.append(f"request failed: {type(e).__name__}")
        else:
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            log.warning(f"[{tag}] HTTP {resp.status_code} (intento {attempt + 1}/{retries})")
            if failures is not None:
                failures.append(f"HTTP {resp.status_code}")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    return None


def get_with_backoff(
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
    retries: int = 3,
    delay: float = 2.0,
    throttle: Callable[[], None] | None = None,
    failures: list[str] | None = None,
    tag: str = "http",
):
    """GET con backoff exponencial en 429/5xx y errores de red; ver
    `request_with_backoff`."""
    return request_with_backoff(
        "GET", url, session=session, params=params, headers=headers,
        timeout=timeout, retries=retries, delay=delay, throttle=throttle,
        failures=failures, tag=tag,
    )
