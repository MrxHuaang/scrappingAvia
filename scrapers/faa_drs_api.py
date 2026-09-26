"""
Cliente de la API data-pull de FAA DRS (drs.faa.gov).

Dos tipos de documento: ADFRAWD (ADs finales) y ADFREAD (ADs de emergencia).
Solo lectura. Lo que este modulo da por hecho de la API, medido con la key
real:

- Paginas fijas de 750 documentos, con `offset`; el sobre es
  `{"summary": {count, hasMoreItems, totalItems, offset, ...}, "documents": []}`.
- `docLastModifiedDate` filtra por "estrictamente despues de", a resolucion de
  segundo, y solo entiende el ISO completo `YYYY-MM-DDTHH:mm:ss.SSSZ`. Una
  fecha sola (`2026-09-18`) no da error: devuelve 200 con `totalItems: 0`.
  Por eso el cursor se valida aqui, antes de salir: un fallo silencioso asi
  deja la sincronizacion en "no hay cambios" para siempre.
- `docLastModifiedDateSortOrder=ASC` + `offset` es estable (sin huecos ni
  duplicados en un barrido completo) y es el unico orden que se usa.
- Key invalida -> 403 con cuerpo vacio; filtro desconocido -> 400 con
  `errorMessage`. No hay cabeceras de cuota ni `Retry-After`: el ritmo lo
  pone el cliente (`min_interval`) y los 429/5xx/timeouts se reintentan con
  el bucle compartido de `scrapers.common.http`.

La key sale solo de la variable de entorno DRS_API_KEY y viaja solo en la
cabecera `x-api-key`: nunca en la URL, en un log ni en un mensaje de error.
El cliente no se construye si DRS_API_ENABLED no esta encendido.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from scrapers.common.http import API_HEADERS, request_with_backoff
from scrapers.common.log import get_logger

log = get_logger(__name__)

BASE_URL = "https://drs.faa.gov/api/drs/data-pull"
DOCTYPES = ("ADFRAWD", "ADFREAD")
PAGE_SIZE = 750

#: Numeros de AD por POST `filtered`. La API devolvio exactos los 10 de una
#: consulta de 10; no documenta un tope, asi que se queda holgado por debajo.
FILTER_CHUNK = 25

ENV_KEY = "DRS_API_KEY"
ENV_ENABLED = "DRS_API_ENABLED"
_TRUE = {"1", "true"}

_CURSOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class DrsError(Exception):
    """Cualquier fallo del cliente DRS. El mensaje nunca contiene la key."""


class DrsDisabledError(DrsError):
    """DRS_API_ENABLED no esta encendido."""


class DrsConfigError(DrsError):
    """Falta DRS_API_KEY."""


class DrsAuthError(DrsError):
    """La API rechazo la key (403)."""


class DrsRequestError(DrsError):
    """Respuesta inutilizable: 4xx, reintentos agotados o sobre invalido."""


def is_enabled(env: dict | None = None) -> bool:
    """DRS_API_ENABLED en "1" o "true" (sin distinguir mayusculas)."""
    value = (env if env is not None else os.environ).get(ENV_ENABLED, "")
    return value.strip().lower() in _TRUE


def format_cursor(moment: datetime) -> str:
    """`datetime` con zona -> `YYYY-MM-DDTHH:mm:ss.SSSZ` en UTC, la unica forma
    que el filtro entiende."""
    if moment.tzinfo is None:
        raise ValueError("cursor datetime must be timezone-aware")
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def validate_cursor(value: str | datetime) -> str:
    """Devuelve el cursor listo para la peticion o levanta ValueError.

    Se rechaza todo lo que no sea el ISO completo con milisegundos y `Z`: la
    API trata cualquier otra forma como "sin resultados" sin avisar."""
    if isinstance(value, datetime):
        return format_cursor(value)
    if not isinstance(value, str) or not _CURSOR_RE.match(value):
        raise ValueError(
            f"docLastModifiedDate must be YYYY-MM-DDTHH:mm:ss.SSSZ, got {value!r}"
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as e:
        raise ValueError(f"docLastModifiedDate is not a real date: {value!r}") from e
    return value


def _check_doctype(doctype: str) -> str:
    if doctype not in DOCTYPES:
        raise ValueError(f"unknown DRS doctype {doctype!r}; expected one of {DOCTYPES}")
    return doctype


@dataclass(frozen=True)
class Page:
    """Una pagina del data-pull, tal cual la devolvio la API."""

    doctype: str
    offset: int
    documents: list[dict]
    has_more: bool
    total_items: int | None


class DrsClient:
    """Cliente de solo lectura. `session`, `sleep` y `clock` son inyectables
    para los tests; `min_interval` es la pausa minima entre peticiones."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        min_interval: float = 2.0,
        timeout: float = 120,
        retries: int = 5,
        retry_delay: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key or not api_key.strip():
            raise DrsConfigError(f"{ENV_KEY} is empty")
        self._key = api_key.strip()
        self._session = session or requests.Session()
        self._base = base_url.rstrip("/")
        self._min_interval = min_interval
        self._timeout = timeout
        self._retries = retries
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests_made = 0

    def __repr__(self) -> str:  # la key no aparece ni en un traceback con locals
        return f"DrsClient(base_url={self._base!r})"

    @classmethod
    def from_env(cls, env: dict | None = None, **kwargs) -> DrsClient:
        """Construye el cliente desde el entorno; levanta DrsDisabledError si
        el flag esta apagado y DrsConfigError si falta la key."""
        env = env if env is not None else os.environ
        if not is_enabled(env):
            raise DrsDisabledError(f"{ENV_ENABLED} is off; the DRS sync does not run")
        key = (env.get(ENV_KEY) or "").strip()
        if not key:
            raise DrsConfigError(f"{ENV_ENABLED} is on but {ENV_KEY} is not set")
        return cls(key, **kwargs)

    # ------------------------------------------------------------------ HTTP --
    def _headers(self) -> dict:
        return {**API_HEADERS, "x-api-key": self._key}

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    def _request(self, method: str, url: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        self._throttle()
        try:
            resp = request_with_backoff(
                method, url, session=self._session, params=params, json=body,
                headers=self._headers(), timeout=self._timeout, retries=self._retries,
                delay=self._retry_delay, tag="DRS",
            )
        finally:
            self._last_request = self._clock()
            self.requests_made += 1
        if resp is None:
            raise DrsRequestError(f"{method} {url}: no usable response after {self._retries} attempts")
        if resp.status_code == 403:
            raise DrsAuthError(
                f"DRS rejected the API key (HTTP 403) on {method} {url}. Check {ENV_KEY}: "
                f"keys expire after 12 months and can be revoked; renew at https://drs.faa.gov/drs-api"
            )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json() or {}).get("errorMessage") or ""
            except ValueError:
                pass
            raise DrsRequestError(f"{method} {url}: HTTP {resp.status_code} {detail}".rstrip())
        try:
            data = resp.json()
        except ValueError as e:
            raise DrsRequestError(f"{method} {url}: response is not JSON") from e
        if not isinstance(data, dict) or not isinstance(data.get("summary"), dict) \
                or not isinstance(data.get("documents"), list):
            raise DrsRequestError(f"{method} {url}: unexpected envelope (no summary/documents)")
        return data

    @staticmethod
    def _page(doctype: str, offset: int, data: dict) -> Page:
        summary = data["summary"]
        total = summary.get("totalItems")
        return Page(
            doctype=doctype,
            offset=offset,
            documents=data["documents"],
            has_more=bool(summary.get("hasMoreItems")),
            total_items=total if isinstance(total, int) else None,
        )

    # ---------------------------------------------------------------- public --
    def fetch_page(self, doctype: str, *, offset: int = 0, modified_after: str | datetime | None = None) -> Page:
        """Una pagina de `doctype` en orden ASC por docLastModifiedDate.

        `modified_after` se valida antes de salir (ver `validate_cursor`)."""
        _check_doctype(doctype)
        if offset < 0:
            raise ValueError("offset must be >= 0")
        params: dict = {"offset": offset, "docLastModifiedDateSortOrder": "ASC"}
        if modified_after is not None:
            params["docLastModifiedDate"] = validate_cursor(modified_after)
        log.info(
            f"[DRS] {doctype} offset={offset}"
            + (f" modified_after={params['docLastModifiedDate']}" if modified_after is not None else "")
        )
        data = self._request("GET", f"{self._base}/{doctype}", params=params)
        return self._page(doctype, offset, data)

    def iter_pages(self, doctype: str, *, modified_after: str | datetime | None = None,
                   max_pages: int = 200) -> Iterator[Page]:
        """Pagina con `offset` hasta `hasMoreItems: false`.

        El cursor por fecha no parte los bloques de documentos con el mismo
        segundo (hay uno de 14.193), asi que el avance dentro de una corrida es
        siempre por offset. `max_pages` corta un bucle que no termina (una API
        que dijera hasMoreItems con paginas vacias)."""
        cursor = validate_cursor(modified_after) if modified_after is not None else None
        offset = 0
        for _ in range(max_pages):
            page = self.fetch_page(doctype, offset=offset, modified_after=cursor)
            yield page
            if not page.has_more:
                return
            if not page.documents:
                raise DrsRequestError(f"{doctype} offset={offset}: hasMoreItems with an empty page")
            offset += len(page.documents)
        raise DrsRequestError(f"{doctype}: more than {max_pages} pages, stopping")

    def find_by_ad_numbers(self, doctype: str, ad_numbers: Iterable[str]) -> list[dict]:
        """Documentos de `doctype` cuyo `drs:documentNumber` esta en
        `ad_numbers` (coincidencia exacta), via POST `filtered`. Para
        consultas sueltas: sale mucho mas barato que un barrido."""
        _check_doctype(doctype)
        numbers = list(dict.fromkeys(n.strip() for n in ad_numbers if n and n.strip()))
        found: list[dict] = []
        seen: set[str] = set()
        url = f"{self._base}/{doctype}/filtered"
        for i in range(0, len(numbers), FILTER_CHUNK):
            chunk = numbers[i:i + FILTER_CHUNK]
            offset = 0
            while True:
                body = {"offset": offset, "documentFilters": {"drs:documentNumber": chunk}}
                page = self._page(doctype, offset, self._request("POST", url, body=body))
                for doc in page.documents:
                    guid = doc.get("documentGuid")
                    if guid in seen:
                        continue
                    if guid:
                        seen.add(guid)
                    found.append(doc)
                if not page.has_more or not page.documents:
                    break
                offset += len(page.documents)
        return found
