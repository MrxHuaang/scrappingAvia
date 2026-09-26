"""
Sincronizacion incremental de FAA DRS (ADFRAWD y ADFREAD) hacia
`drs_documents`, por las RPCs `upsert_drs_documents`, `advance_drs_cursor` y
`link_ads_to_drs` (repo ad-scrapper).

Por que la primera carga no usa el filtro de fecha: el filtro
`docLastModifiedDate` es "estrictamente despues de" a resolucion de segundo, y
el 70 % de ADFRAWD (14.193 documentos) comparte un mismo segundo de una carga
masiva. Un cursor por fecha no puede partir ese bloque: o lo salta entero o
lo repite entero. Asi que la primera corrida de cada doctype es un barrido
por `offset` en orden ASC sin filtro, hasta `hasMoreItems: false`, y solo al
terminar se fija el cursor con `initial_scan_done = true`.

Las corridas siguientes piden `cursor - 1 h` en ASC y paginan por offset hasta
el final antes de avanzar: aunque DRS reescriba miles de documentos en el
mismo segundo, ninguna pagina se da por vista hasta haber recorrido todas.
El solapamiento de una hora vuelve a traer documentos ya guardados; el upsert
por `document_guid` es idempotente y la RPC no reescribe una fila sin cambios.

Garantias:
- Se sube pagina por pagina. Si una pagina o una llamada falla, la corrida se
  detiene y el cursor NO avanza: lo ya subido queda (es idempotente) y la
  corrida siguiente vuelve a empezar desde el mismo cursor.
- El cursor avanza al maximo `docLastModifiedDate` visto, acotado por la hora
  de inicio de la corrida: un documento que DRS modifique mientras se pagina
  puede correr el offset y quedar sin ver, pero su fecha sera posterior al
  inicio y la corrida siguiente lo trae.
- Dedupe por `documentGuid` dentro de la corrida (una misma llamada con dos
  veces la misma clave la rechazaria entera).
- `SupplementaryInfo` (el preambulo del Federal Register, ~60 % del volumen)
  nunca se envia. El texto regulatorio viaja en su columna, no repetido en
  `payload`.
- El numero de AD sale de `drs:documentNumber` normalizado con
  `utils.normalize_ad_number`: es la clave con la que `link_ads_to_drs` cruza
  con `ads`. Nunca el numero FR ni el guid.
- Un documento que la RPC rechazaria (sin guid, sin numero, estado o URL
  fuera de contrato, texto por encima del limite) no viaja: se cuenta y se
  reporta como error de la corrida, sin frenar al resto.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from scrapers.common.log import get_logger
from scrapers.faa_drs_api import DOCTYPES, DrsAuthError, DrsClient, format_cursor
from utils import normalize_ad_number

log = get_logger(__name__)

#: Solapamiento del incremental respecto al cursor guardado.
CURSOR_OVERLAP = timedelta(hours=1)

#: Documentos por llamada a `upsert_drs_documents`. La RPC admite 750 (una
#: pagina); una pagina de ADFRAWD reciente con texto regulatorio pesa varios
#: MB, asi que se parte en tres para no acercarse al limite de cuerpo.
UPSERT_BATCH = 250
#: Numeros de AD por llamada a `link_ads_to_drs`.
LINK_BATCH = 1000
#: Numeros de AD por lectura de `drs_documents`. Cada AD tiene tres
#: documentos como mucho, asi que la respuesta no llega al tope de 1000 filas
#: de PostgREST.
READ_BATCH = 250
#: Columnas de `drs_documents` que devuelve `DrsStore.documents`: las mismas
#: claves que `to_record`.
DOCUMENT_COLUMNS = (
    "document_guid, document_number, ad_number, status, doc_last_modified, publication_date, "
    "effective_date, document_url, main_download_url, makes, models, regulatory_text, payload"
)

# Reglas de `upsert_drs_documents`, copiadas una a una de su fuente de verdad:
# ad-scrapper, supabase/migrations/20260927211000_drs_writers.sql
# (max_text, max_payload, max_list, max_field, max_key, drs_prefix y la
# validacion por documento). Un solo documento que las incumpla aborta la
# llamada entera (hasta UPSERT_BATCH documentos) y deja el cursor parado, asi
# que `to_record` las comprueba antes y descarta solo ese documento. Si la
# migracion cambia, cambian aqui.
REGULATORY_TEXT_MAX = 1_000_000     # max_text, caracteres
PAYLOAD_MAX_BYTES = 256 * 1024      # max_payload, bytes del JSON
LIST_MAX_ITEMS = 2000               # max_list: makes / models
FIELD_MAX = 2048                    # max_field: URLs y cada make / model
KEY_MAX = 64                        # max_key: guid, document_number, ad_number
URL_PREFIX = "https://drs.faa.gov/"  # drs_prefix
STATUSES = frozenset({"Current", "Historical", "Pending"})
_GUID_RE = re.compile(r"^[A-Za-z0-9-]{1,%d}$" % KEY_MAX)
_LAST_MODIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WHITESPACE_RE = re.compile(r"\s")

#: Prefijo de los campos propios de cada doctype (`drs:adfrawdMake`, ...).
_FIELD_PREFIX = {"ADFRAWD": "adfrawd", "ADFREAD": "adfread"}
#: Valor centinela de DRS para un array vacio (`drs:subPart`, ...).
_EMPTY_SENTINEL = "_EMPTY_"


class InvalidDocument(ValueError):
    """El documento no cumple el contrato de `upsert_drs_documents`."""


# ---------------------------------------------------------------------------
# Documento DRS -> elemento de p_documents
# ---------------------------------------------------------------------------
def _string_list(value) -> list[str]:
    """Array de DRS -> lista de strings sin vacios, centinelas ni repetidos,
    en el orden de la fuente. Make y Model NO son paralelos: no se emparejan."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            item = item.strip()
            if item and item != _EMPTY_SENTINEL and item not in out:
                out.append(item)
    return out


def _iso_date(value) -> str | None:
    """`YYYY-MM-DD` valido o None (una fecha que la base no pudiera castear
    tumbaria la llamada entera; el valor crudo sigue en `payload`)."""
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def parse_last_modified(value) -> datetime:
    """`docLastModifiedDate` (o el timestamptz leido de la base) -> datetime UTC."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"not a timestamp: {value!r}")
    if moment.tzinfo is None:
        raise ValueError(f"timestamp without zone: {value!r}")
    return moment.astimezone(timezone.utc)


def _is_supplementary(key: str) -> bool:
    return key.endswith("SupplementaryInfo")


def _check_url(name: str, url) -> str:
    if not isinstance(url, str) or not url.startswith(URL_PREFIX):
        raise InvalidDocument(f"{name} outside {URL_PREFIX}")
    if _WHITESPACE_RE.search(url):
        raise InvalidDocument(f"{name} contains whitespace")
    if len(url) > FIELD_MAX:
        raise InvalidDocument(f"{name} of {len(url)} chars, limit {FIELD_MAX}")
    return url


def _check_key(name: str, value: str) -> str:
    if not value.strip():
        raise InvalidDocument(f"missing {name}")
    if len(value) > KEY_MAX:
        raise InvalidDocument(f"{name} of {len(value)} chars, limit {KEY_MAX}")
    return value


def _check_list(name: str, items: list[str]) -> list[str]:
    if len(items) > LIST_MAX_ITEMS:
        raise InvalidDocument(f"{len(items)} {name}, limit {LIST_MAX_ITEMS}")
    for item in items:
        if len(item) > FIELD_MAX:
            raise InvalidDocument(f"one of the {name} has {len(item)} chars, limit {FIELD_MAX}")
    return items


def to_record(doc: dict, doctype: str) -> dict:
    """Un documento de la API -> un elemento de `p_documents`.

    Levanta InvalidDocument si la RPC lo rechazaria (reglas arriba, junto a
    REGULATORY_TEXT_MAX). Una fecha de publicacion o efectiva que no sea
    YYYY-MM-DD viaja como null (el valor crudo sigue en `payload`)."""
    prefix = _FIELD_PREFIX[doctype]
    guid = doc.get("documentGuid")
    guid = guid.strip() if isinstance(guid, str) else ""
    if not _GUID_RE.match(guid):
        raise InvalidDocument(f"documentGuid must match {_GUID_RE.pattern}")
    raw_number = doc.get("drs:documentNumber")
    document_number = _check_key(
        "drs:documentNumber", raw_number.strip() if isinstance(raw_number, str) else "")
    ad_number = _check_key("ad_number", normalize_ad_number(document_number))
    status = doc.get("drs:status")
    if status not in STATUSES:
        raise InvalidDocument(f"status {status!r} outside {sorted(STATUSES)}")
    raw_modified = doc.get("docLastModifiedDate")
    if not isinstance(raw_modified, str) or not _LAST_MODIFIED_RE.match(raw_modified.strip()):
        raise InvalidDocument("docLastModifiedDate must start with YYYY-MM-DDTHH:MM")
    try:
        last_modified = parse_last_modified(raw_modified)
    except ValueError as e:
        raise InvalidDocument(f"docLastModifiedDate: {e}") from e
    url = _check_url("documentURL", doc.get("documentURL"))
    download = doc.get("mainDocumentDownloadURL")
    download = _check_url("mainDocumentDownloadURL", download) if download is not None else None
    makes = _check_list("makes", _string_list(doc.get(f"drs:{prefix}Make")))
    models = _check_list("models", _string_list(doc.get(f"drs:{prefix}Model")))

    reg_key = f"drs:{prefix}RegulatoryText"
    regulatory_text = doc.get(reg_key) if isinstance(doc.get(reg_key), str) else None
    regulatory_text = regulatory_text if regulatory_text and regulatory_text.strip() else None
    if regulatory_text and len(regulatory_text) > REGULATORY_TEXT_MAX:
        raise InvalidDocument(f"regulatory text of {len(regulatory_text)} chars, limit {REGULATORY_TEXT_MAX}")

    payload = {k: v for k, v in doc.items() if not _is_supplementary(k) and k != reg_key}
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if payload_bytes > PAYLOAD_MAX_BYTES:
        raise InvalidDocument(f"payload of {payload_bytes} bytes, limit {PAYLOAD_MAX_BYTES}")

    return {
        "document_guid": guid,
        "document_number": document_number,
        "ad_number": ad_number,
        "status": status,
        "doc_last_modified": format_cursor(last_modified),
        "publication_date": _iso_date(doc.get(f"drs:{prefix}CitationPublishDate")),
        "effective_date": _iso_date(doc.get("drs:effectiveDate")),
        "document_url": url,
        "main_download_url": download,
        "makes": makes,
        "models": models,
        "regulatory_text": regulatory_text,
        "payload": payload,
    }


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SyncState:
    last_modified: datetime | None
    initial_scan_done: bool


class DrsStore(Protocol):
    def read_state(self, doctype: str) -> SyncState | dict | None: ...
    def upsert(self, doctype: str, documents: list[dict]) -> dict | None: ...
    def advance(self, doctype: str, last_modified: str, initial_scan_done: bool) -> None: ...
    def link(self, ad_numbers: list[str]) -> int | None: ...
    def documents(self, doctype: str, ad_numbers: list[str]) -> list[dict]: ...


class SupabaseDrsStore:
    """Las RPCs de ad-scrapper sobre un cliente supabase con service key."""

    def __init__(self, supabase):
        self._sb = supabase

    def read_state(self, doctype: str) -> SyncState | None:
        resp = (self._sb.table("drs_sync_state")
                .select("last_modified, initial_scan_done")
                .eq("doctype", doctype)
                .execute())
        rows = resp.data or []
        return _coerce_state(rows[0]) if rows else None

    def upsert(self, doctype: str, documents: list[dict]) -> dict | None:
        resp = self._sb.rpc("upsert_drs_documents", {"p_doctype": doctype, "p_documents": documents}).execute()
        return resp.data if isinstance(resp.data, dict) else None

    def advance(self, doctype: str, last_modified: str, initial_scan_done: bool) -> None:
        self._sb.rpc("advance_drs_cursor", {
            "p_doctype": doctype, "p_last_modified": last_modified, "p_initial_scan_done": initial_scan_done,
        }).execute()

    def link(self, ad_numbers: list[str]) -> int | None:
        resp = self._sb.rpc("link_ads_to_drs", {"p_ad_numbers": ad_numbers}).execute()
        return resp.data if isinstance(resp.data, int) else None

    def documents(self, doctype: str, ad_numbers: list[str]) -> list[dict]:
        """Todos los documentos guardados de `doctype` con esos numeros de AD
        (no solo los de la corrida: un AD puede tener otro documento que no
        cambio)."""
        out: list[dict] = []
        numbers = sorted(set(ad_numbers))
        for chunk in _chunks(numbers, READ_BATCH):
            resp = (self._sb.table("drs_documents")
                    .select(DOCUMENT_COLUMNS)
                    .eq("doctype", doctype)
                    .in_("ad_number", chunk)
                    .execute())
            out.extend(resp.data or [])
        return out


class DryRunStore:
    """Sin base: sin cursor (barrido completo) y sin escrituras. Guarda en
    memoria lo que subiria, para que los pasos posteriores a la sync (ADs
    anteriores a 1994, texto) se puedan ensayar sin base."""

    def __init__(self):
        self._documents: dict[str, dict[str, dict]] = {}

    def read_state(self, doctype: str) -> None:
        return None

    def upsert(self, doctype: str, documents: list[dict]) -> dict:
        kept = self._documents.setdefault(doctype, {})
        for doc in documents:
            kept[doc["document_guid"]] = doc
        return {"inserted": 0, "updated": 0, "unchanged": 0, "dry_run": len(documents)}

    def advance(self, doctype: str, last_modified: str, initial_scan_done: bool) -> None:
        log.info(f"[DRS] {doctype} DRY-RUN: el cursor avanzaria a {last_modified}")

    def link(self, ad_numbers: list[str]) -> int:
        return 0

    def documents(self, doctype: str, ad_numbers: list[str]) -> list[dict]:
        wanted = set(ad_numbers)
        return [d for d in self._documents.get(doctype, {}).values() if d["ad_number"] in wanted]


def _coerce_state(raw) -> SyncState | None:
    if raw is None or isinstance(raw, SyncState):
        return raw
    lm = raw.get("last_modified")
    return SyncState(
        last_modified=parse_last_modified(lm) if lm else None,
        initial_scan_done=bool(raw.get("initial_scan_done")),
    )


# ---------------------------------------------------------------------------
# Corrida
# ---------------------------------------------------------------------------
@dataclass
class DoctypeResult:
    doctype: str
    mode: str = ""                    # initial | incremental
    modified_after: str | None = None
    pages: int = 0
    documents: int = 0                # enviados a la RPC
    duplicates: int = 0               # guids repetidos dentro de la corrida
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)   # "guid: motivo"
    advanced_to: str | None = None
    error: str | None = None
    auth_failed: bool = False
    ad_numbers: set[str] = field(default_factory=set)  # solo de paginas ya subidas


@dataclass
class SyncReport:
    results: list[DoctypeResult] = field(default_factory=list)
    linked: int | None = None
    link_error: str | None = None
    before_link_error: str | None = None

    def ad_numbers(self, doctype: str) -> set[str]:
        """Numeros de AD de las paginas de `doctype` que si se subieron."""
        return set().union(*(r.ad_numbers for r in self.results if r.doctype == doctype))

    @property
    def errors(self) -> list[str]:
        out = [f"{r.doctype}: {r.error}" for r in self.results if r.error]
        for r in self.results:
            out.extend(f"{r.doctype}: skipped {s}" for s in r.skipped)
        if self.before_link_error:
            out.append(f"after upsert: {self.before_link_error}")
        if self.link_error:
            out.append(f"link_ads_to_drs: {self.link_error}")
        return out


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def sync_doctype(client: DrsClient, store: DrsStore, doctype: str, *, now: datetime | None = None) -> DoctypeResult:
    """Una corrida de `doctype`: primera carga o incremental segun el cursor."""
    result = DoctypeResult(doctype)
    started = now or datetime.now(timezone.utc)
    try:
        state = _coerce_state(store.read_state(doctype))
    except Exception as e:
        result.error = f"could not read drs_sync_state: {e}"
        return result

    if state and state.initial_scan_done and state.last_modified:
        result.mode = "incremental"
        result.modified_after = format_cursor(state.last_modified - CURSOR_OVERLAP)
    else:
        result.mode = "initial"
    log.info(f"[DRS] {doctype}: {result.mode}"
             + (f" desde {result.modified_after}" if result.modified_after else " (barrido completo por offset)"))

    seen: set[str] = set()
    max_seen: datetime | None = None
    try:
        for page in client.iter_pages(doctype, modified_after=result.modified_after):
            result.pages += 1
            records: list[dict] = []
            for doc in page.documents:
                guid = doc.get("documentGuid")
                if isinstance(guid, str) and guid in seen:
                    result.duplicates += 1
                    continue
                try:
                    rec = to_record(doc, doctype)
                except InvalidDocument as e:
                    result.skipped.append(f"{guid or '?'}: {e}")
                    continue
                seen.add(rec["document_guid"])
                records.append(rec)
            for chunk in _chunks(records, UPSERT_BATCH):
                counts = store.upsert(doctype, chunk) or {}
                result.documents += len(chunk)
                result.inserted += int(counts.get("inserted") or 0)
                result.updated += int(counts.get("updated") or 0)
                result.unchanged += int(counts.get("unchanged") or 0)
                for rec in chunk:
                    result.ad_numbers.add(rec["ad_number"])
                    moment = parse_last_modified(rec["doc_last_modified"])
                    if max_seen is None or moment > max_seen:
                        max_seen = moment
    except DrsAuthError as e:
        result.error, result.auth_failed = str(e), True
        return result
    except Exception as e:
        result.error = f"page {result.pages} failed, cursor not advanced: {type(e).__name__}: {e}"
        return result

    if max_seen is None:
        if result.mode == "initial":
            log.warning(f"[DRS] {doctype}: la primera carga no trajo documentos; el cursor queda sin fijar")
        return result
    cursor = format_cursor(min(max_seen, started))
    try:
        store.advance(doctype, cursor, True)
        result.advanced_to = cursor
    except Exception as e:
        result.error = f"advance_drs_cursor failed: {type(e).__name__}: {e}"
    return result


def sync(client: DrsClient, store: DrsStore, doctypes: Iterable[str] = DOCTYPES, *,
         now: datetime | None = None,
         before_link: Callable[[SyncReport], None] | None = None) -> SyncReport:
    """Todas las `doctypes` en orden y luego `link_ads_to_drs` con los numeros
    de AD de las paginas que si se subieron (tambien si alguna fallo: lo
    subido ya esta en la base). Una key rechazada corta el resto.

    `before_link` corre entre las dos cosas, con el reporte: ahi
    `populate_db` crea las filas de `ads` de los ADs anteriores a 1994, que
    asi quedan enlazadas en la misma corrida. Si falla, se reporta y el
    enlace corre igual."""
    report = SyncReport()
    for doctype in doctypes:
        res = sync_doctype(client, store, doctype, now=now)
        report.results.append(res)
        log.info(
            f"[DRS] {doctype}: {res.mode}, {res.pages} paginas, {res.documents} enviados "
            f"(ins {res.inserted}, upd {res.updated}, sin cambio {res.unchanged}), "
            f"{res.duplicates} duplicados, {len(res.skipped)} descartados, cursor -> {res.advanced_to}"
        )
        if res.auth_failed:
            break

    if before_link is not None:
        try:
            before_link(report)
        except Exception as e:
            report.before_link_error = f"{type(e).__name__}: {e}"

    touched = sorted(set().union(*(r.ad_numbers for r in report.results)))
    if touched:
        linked = 0
        try:
            for chunk in _chunks(touched, LINK_BATCH):
                linked += int(store.link(chunk) or 0)
            report.linked = linked
        except Exception as e:
            report.link_error = f"{type(e).__name__}: {e}"
        log.info(f"[DRS] link_ads_to_drs: {len(touched)} numeros de AD, {report.linked} ADs enlazados")
    return report
