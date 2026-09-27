"""
Contrato v2 de busqueda: una pagina global en UNA llamada a `search_ads_v2`.

GET /api/search/stream?v=2 y POST /api/search {"v": 2}. A diferencia de v1
(un evento por autoridad, offset por fuente), v2 devuelve una sola pagina de
la lista ordenada de todas las fuentes permitidas, con `total` y
`source_counts` calculados por la BD antes de paginar.

Las fuentes NO salen del query: el proxy calcula el conjunto efectivo
(plan interseccion seleccion) y lo manda en `X-Effective-Sources`. Esa
cabecera solo se cree despues de validar el secreto compartido del proxy; si
se aceptara sin el, cualquiera que llegue directo a Flask se saltaria el plan.
"""

import json
import re
from datetime import date

from flask import Response, jsonify, request

from utils import VALID_SOURCES, normalize_filter_values, validate_search_filters_v2
from webapp.core import RouteResult, check_rate_limit, get_supabase_admin, logger, proxy_rejection
from webapp.search_rows import row_to_result

VERSION = 2
MAX_LIMIT = 100
MAX_OFFSET = 100000
EFFECTIVE_SOURCES_HEADER = "X-Effective-Sources"
#: Valor de la cabecera para "todas las fuentes" (plan ilimitado sin seleccion).
ALL_SOURCES = "*"

CONFIDENCE_LEVELS = ("high", "medium", "low")
#: Enum canonico de `ads.status`; se acepta sin distinguir mayusculas.
STATUSES = ("New", "Revision", "Superseded", "Cancelled")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}
#: SQLSTATE con el que `search_ads_v2` rechaza filtros invalidos.
_INVALID_PARAMETER = "22023"
_UPSTREAM_MESSAGE = "Search is temporarily unavailable."

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


class FilterError(ValueError):
    """Filtros invalidos o insuficientes: evento `filter_gate` / HTTP 400."""


class UpstreamError(RuntimeError):
    """La BD fallo o devolvio algo inesperado: HTTP 502 `upstream` (stream y POST)."""


# ---------------------------------------------------------------- version --
def requested_version(raw) -> int | None:
    """1 sin `v` (o v=1), 2 con v=2; None para cualquier otra version."""
    if raw is None or raw == "":
        return 1
    try:
        v = int(str(raw).strip())
    except ValueError:
        return None
    return v if v in (1, VERSION) else None


# ------------------------------------------------------ effective sources --
def parse_effective_sources(raw: str | None) -> list[str] | None:
    """
    Cabecera del proxy -> fuentes a buscar. `*` -> None (sin filtro de
    fuente); una lista separada por comas -> lista ordenada y sin duplicados.
    Ausente, vacia, `*` mezclado con claves o una clave fuera de
    VALID_SOURCES -> ValueError: el proxy nunca manda eso, asi que no se
    adivina un conjunto.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError(f"Missing {EFFECTIVE_SOURCES_HEADER} header.")
    if value == ALL_SOURCES:
        return None
    keys = [k.strip() for k in value.split(",")]
    if any(not k or k == "all" or k not in VALID_SOURCES for k in keys):
        raise ValueError(f"Invalid {EFFECTIVE_SOURCES_HEADER} header.")
    return sorted(set(keys))


def _authorize() -> tuple[list[str] | None, RouteResult | None]:
    """
    (fuentes, None) si la request viene del proxy con una cabecera valida;
    (None, respuesta) si no. El secreto va primero: sin el, la cabecera de
    fuentes no significa nada y ni se lee.
    """
    denied = proxy_rejection(401)
    if denied:
        return None, denied
    try:
        return parse_effective_sources(request.headers.get(EFFECTIVE_SOURCES_HEADER)), None
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 400)


# ----------------------------------------------------------------- params --
class Params:
    """Lectura uniforme de parametros: query string (GET) o cuerpo JSON (POST)."""

    def __init__(self, getlist, getone):
        self._getlist = getlist
        self._getone = getone

    @classmethod
    def from_args(cls, args) -> "Params":
        return cls(args.getlist, args.get)

    @classmethod
    def from_json(cls, body: dict) -> "Params":
        def getlist(key):
            v = body.get(key)
            if v is None:
                return []
            return v if isinstance(v, list) else [v]
        return cls(getlist, body.get)

    def values(self, key) -> list[str]:
        # Repetidos, nunca separados por coma: hay fabricantes con coma en el
        # nombre ("REVO, Inc.").
        return normalize_filter_values(str(v) for v in self._getlist(key) if v is not None)

    def text(self, key) -> str:
        v = self._getone(key)
        return "" if v is None else str(v).strip()

    def raw(self, key):
        return self._getone(key)


def _parse_bool(raw) -> bool | None:
    if raw is None or isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return None
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise FilterError("has_pdf must be true or false.")


def _parse_int(raw, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise FilterError(f"{name} must be an integer.") from None


def _parse_date(value: str, name: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise FilterError(f"{name} must be a date (YYYY-MM-DD).") from None


def build_filters(params: Params, sources: list[str] | None) -> tuple[dict, int, int]:
    """
    Parametros HTTP -> (p_filters, p_limit, p_offset) de `search_ads_v2`.
    FilterError si un valor no tiene forma valida o si no pasa el gate.
    Lo que solo la BD sabe validar (codigos de tipo/subtipo, topes de
    longitud) lo valida la BD y vuelve como 22023 -> FilterError.
    """
    manufacturer_ids = params.values("mfr")
    if any(not _UUID_RE.match(m) for m in manufacturer_ids):
        raise FilterError("mfr must be a manufacturer id (uuid).")

    canonical = {s.casefold(): s for s in STATUSES}
    statuses = []
    for s in params.values("statuses"):
        if s.casefold() not in canonical:
            raise FilterError(f"Unknown status: {s!r}.")
        statuses.append(canonical[s.casefold()])

    min_confidence = params.text("min_confidence").lower() or None
    if min_confidence and min_confidence not in CONFIDENCE_LEVELS:
        raise FilterError(f"min_confidence must be one of: {', '.join(CONFIDENCE_LEVELS)}.")

    # `limit` por encima del tope se recorta (la respuesta lo devuelve, el
    # cliente ve cuanto recibio); un `offset` fuera de rango no se puede
    # recortar sin devolver otra pagina, asi que se rechaza.
    limit = min(max(_parse_int(params.raw("limit"), "limit", MAX_LIMIT), 1), MAX_LIMIT)
    offset = _parse_int(params.raw("offset"), "offset", 0)
    if not 0 <= offset <= MAX_OFFSET:
        raise FilterError(f"offset must be between 0 and {MAX_OFFSET}.")

    filters = {
        "v": VERSION,
        # None = todas: la cabecera fue `*`.
        "sources": sources,
        "manufacturer_ids": manufacturer_ids,
        "make_texts": params.values("make"),
        "models": params.values("models"),
        "min_confidence": min_confidence,
        "product_types": params.values("product_type"),
        "product_subtypes": params.values("product_subtype"),
        "statuses": statuses,
        "has_pdf": _parse_bool(params.raw("has_pdf")),
        "date_from": _parse_date(params.text("date_from"), "date_from"),
        "date_to": _parse_date(params.text("date_to"), "date_to"),
        "term": params.text("keyword") or None,
    }
    gate = validate_search_filters_v2(
        keyword=filters["term"] or "",
        manufacturer_ids=filters["manufacturer_ids"],
        make_texts=filters["make_texts"],
        models=filters["models"],
        product_types=filters["product_types"],
        product_subtypes=filters["product_subtypes"],
        statuses=filters["statuses"],
        has_pdf=filters["has_pdf"],
        date_from=filters["date_from"] or "",
        date_to=filters["date_to"] or "",
    )
    if gate:
        raise FilterError(gate)
    return filters, limit, offset


# ------------------------------------------------------------------- page --
def fetch_page(filters: dict, limit: int, offset: int) -> dict:
    """
    Una llamada a `search_ads_v2` -> objeto `page` (sin `event`). `total` y
    `source_counts` vienen de la BD y no dependen de la pagina, asi que un
    offset mas alla del total devuelve `results: []` con el total correcto.
    """
    sb = get_supabase_admin()
    if not sb:
        raise UpstreamError("Search database is not configured.")
    try:
        resp = sb.rpc("search_ads_v2", {
            "p_filters": filters,
            "p_limit": limit,
            "p_offset": offset,
        }).execute()
    except Exception as e:
        if str(getattr(e, "code", "")) == _INVALID_PARAMETER:
            raise FilterError(getattr(e, "message", None) or str(e)) from e
        # El detalle va al log, no al cliente: puede traer SQL o la URL.
        logger.error(f"[DB] search_ads_v2 fallo: {e}")
        raise UpstreamError(_UPSTREAM_MESSAGE) from e

    data = resp.data
    if not isinstance(data, dict):
        logger.error(f"[DB] search_ads_v2 devolvio {type(data).__name__}, no un objeto")
        raise UpstreamError(_UPSTREAM_MESSAGE)

    results = [row_to_result(row) for row in data.get("results") or []]
    total = int(data.get("total") or 0)
    return {
        "v": VERSION,
        "total": total,
        "source_counts": data.get("source_counts") or {},
        "limit": limit,
        "offset": offset,
        # `offset` es global: hay mas si esta pagina no llega al total.
        "has_more": offset + len(results) < total,
        "results": results,
    }


def _search(params: Params, sources: list[str] | None) -> dict:
    filters, limit, offset = build_filters(params, sources)
    return fetch_page(filters, limit, offset)


def _error(code: str, message: str) -> dict:
    return {"v": VERSION, "code": code, "message": message}


def _done(total: int) -> dict:
    return {"v": VERSION, "event": "done", "total": total}


def stream_events(params: Params, sources: list[str] | None) -> list[dict]:
    """
    Eventos SSE de una busqueda v2: `page` + `done`, o `error` + `done` si
    los filtros no pasan. Siempre terminan en `done`, como en v1, para que
    el cliente cierre. UpstreamError no es un evento: sube al llamador.
    """
    try:
        page = _search(params, sources)
    except FilterError as e:
        return [{"v": VERSION, "event": "error", "code": "filter_gate", "message": str(e)}, _done(0)]
    event = {"v": VERSION, "event": "page"}
    event.update((k, v) for k, v in page.items() if k != "v")
    return [event, _done(page["total"])]


# ----------------------------------------------------------------- routes --
def handle_stream() -> RouteResult:
    sources, denied = _authorize()
    if denied:
        return denied
    rl = check_rate_limit()
    if rl:
        return rl
    # Una sola llamada: se resuelve antes de abrir el stream (el generador
    # correria fuera del contexto de la request), asi que ningun error puede
    # llegar con el stream ya empezado. Un error de filtros viaja como evento
    # con HTTP 200, igual que en v1. Un fallo de BD responde 502 con el mismo
    # cuerpo que el POST: el proxy de Next devuelve la cuota consumida solo
    # ante un 5xx, y con un evento `upstream` en un 200 la busqueda fallida
    # se descontaba igual.
    try:
        events = stream_events(Params.from_args(request.args), sources)
    except UpstreamError as e:
        return jsonify(_error("upstream", str(e))), 502
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return Response(body, mimetype="text/event-stream", headers=_SSE_HEADERS)


def handle_post(body: dict) -> RouteResult:
    """POST v2: el mismo objeto `page` sin `event`; 400 gate, 502 BD."""
    sources, denied = _authorize()
    if denied:
        return denied
    rl = check_rate_limit()
    if rl:
        return rl
    try:
        return jsonify(_search(Params.from_json(body), sources))
    except FilterError as e:
        return jsonify(_error("filter_gate", str(e))), 400
    except UpstreamError as e:
        return jsonify(_error("upstream", str(e))), 502
