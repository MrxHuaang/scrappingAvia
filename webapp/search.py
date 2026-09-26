"""
Busqueda de ADs, solo contra la BD: el scraping vive en populate_db, que
carga las autoridades a diario; la busqueda no raspa sitios en vivo. Rutas:
/api/env, /api/health, /api/search (POST) y /api/search/stream (SSE).

Las dos rutas de busqueda hablan dos contratos: sin `v` (o v=1) el de siempre,
un evento por autoridad; con v=2 el paginado global de webapp.search_v2.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request, session

from utils import (
    MAX_RESULTS_PER_SOURCE,
    VALID_SOURCES,
    normalize_filter_values,
    run_with_timeout,
    validate_search_filters,
)

from webapp.core import (
    APP_VERSION,
    IS_SERVERLESS,
    SCRAPER_TIMEOUT,
    RouteResult,
    check_rate_limit,
    get_supabase_admin,
    logger,
    require_proxy,
)
from webapp import search_v2
from webapp.search_rows import row_to_result

bp = Blueprint("search", __name__)

_SUPABASE_PAGE_SIZE = 1000  # PostgREST max rows per request


# Peticiones del contrato v1 atendidas por este proceso. El contrato v1 se
# retira cuando deja de usarse; la senal es la linea de log
# `search_v1_requests=` (en serverless el contador vive lo que la instancia,
# lo que se agrega es el log).
_v1_requests = 0
_v1_requests_lock = threading.Lock()


def _count_v1_request(route: str) -> None:
    global _v1_requests
    with _v1_requests_lock:
        _v1_requests += 1
        n = _v1_requests
    logger.info(f"search_v1_requests={n} route={route}")


def search_v1_requests() -> int:
    """Cuantas busquedas v1 atendio este proceso desde que arranco."""
    return _v1_requests


class DbResults(list):
    """
    Lista de resultados que además carga cuántas filas casaron en la BD.

    Es una `list` a propósito: `run_with_timeout` la trata como tal y el
    evento v1 la serializa tal cual; las estadísticas viajan pegadas al
    resultado y quien no las mire sigue viendo una lista normal. Una fuente que
    falló devuelve `[]` pelada, por eso se lee con `getattr(x, "truncated",
    False)`.
    """

    total: int | None = None
    truncated: bool = False


# -------------------------------------------------------- service: DB search --
def search_supabase(source, keyword="", make="", model="", product_type="",
                    date_from="", date_to="", max_results=10000,
                    makes=None, models=None, offset=0) -> list[dict]:
    """
    Busca ADs en la tabla 'ads' de Supabase vía la RPC parametrizada `search_ads`.

    El input del usuario viaja como bind parameters — nunca se interpola en la
    gramática de filtros de PostgREST (eso permitía inyectar filtros arbitrarios
    con la service key). Contrato de la RPC (migración 20260803001500 en
    ad-scrapper): `%`/`_`/`\\` son literales, orden determinista
    (effective_date DESC, id DESC), p_limit <= 1000, filtros de texto <= 200
    chars (más largos → 22023, que aquí se reporta como error y retorna []).
    La RPC resuelve el self-join de supersesión y devuelve `superseded_by`.
    Retorna lista de dicts con el mismo formato que los scrapers.

    `makes`/`models` son los filtros multivalor (`p_makes`/`p_models`). Un AD
    casa si coincide CUALQUIER entrada; entre las dos listas se sigue sumando
    (make Y model), igual que con los escalares. Existen porque un mismo
    fabricante se escribe distinto en cada autoridad y las familias que más
    importan no comparten ni un token: la FAA archiva Cessna, Beechcraft y
    Hawker aparte de "Textron Aviation".

    `offset` es la fila desde la que empieza esta página, POR FUENTE. La RPC
    ordena de forma total (effective_date DESC, id DESC), que es justo lo que
    hace segura la paginación por offset: sin ese desempate la página 2 podría
    repetir filas y saltarse otras.
    """
    sb = get_supabase_admin()
    if not sb:
        return DbResults()

    try:
        all_rows = []
        start = max(int(offset or 0), 0)
        fetched = 0
        db_total = None

        while fetched < max_results:
            batch_size = min(_SUPABASE_PAGE_SIZE, max_results - fetched)
            resp = sb.rpc("search_ads", {
                "p_source": source or "all",
                "p_term": keyword or "",
                # El término ya matchea make; repetirlo como filtro AND lo restringiría.
                "p_make": make if (make and make != keyword) else "",
                "p_model": model or "",
                "p_product_type": product_type or "",
                "p_date_from": date_from or None,
                "p_date_to": date_to or None,
                "p_limit": batch_size,
                "p_offset": start + fetched,
                # Listas vacías no se mandan: la RPC trata `{}` como "sin
                # filtro", igual que `''`, pero enviar el parámetro sólo cuando
                # hay algo deja el payload idéntico al de antes en el caso de
                # una sola marca.
                **({"p_makes": makes} if makes else {}),
                **({"p_models": models} if models else {}),
            }).execute()
            rows = resp.data or []
            all_rows.extend(rows)

            # `total_count` es el nº de filas que casaron los filtros, antes de
            # LIMIT/OFFSET; viene en cada fila y basta leerlo una vez.
            if db_total is None and rows:
                db_total = rows[0].get("total_count")

            # Si recibimos menos de lo pedido, no hay más datos
            if len(rows) < batch_size:
                break

            fetched += len(rows)

        results = [row_to_result(row) for row in all_rows]

        out = DbResults(results)
        out.total = db_total
        # Truncado = la BD tenía más de lo que este tope permitió traer. Sin esta
        # señal el bucle de arriba devolvía exactamente `max_results` filas y
        # parecían el total; el contrato de INT-6 exigía justamente exponerlo.
        # Truncado = la BD tiene más filas DESPUÉS de esta página. Se cuenta
        # desde `start`, no desde cero: en la página 2 de un corpus de 5.108
        # filas, `len(results)` son 500 y compararlo contra el total diría
        # "truncado" incluso después de haberlo traído todo.
        out.truncated = bool(db_total is not None and db_total > start + len(results))
        if out.truncated:
            logger.warning(
                f"[DB] {source}: {len(results)} de {db_total} filas — resultado truncado por el tope {max_results}"
            )
        else:
            logger.info(f"[DB] {source}: {len(results)} resultados desde Supabase")
        return out

    except Exception as e:
        logger.error(f"[DB] Error buscando en Supabase ({source}): {e}")
        return DbResults()


# ------------------------------------------------- service: v1 per source --
#: Autoridades que barre `source=all` en v1, en el orden de la respuesta POST,
#: con la etiqueta que v1 pone en `truncated_sources`. Es el mismo conjunto de
#: siempre: las tres deshabilitadas del cron no entran en "all".
_V1_ALL_SOURCES = (
    ("federal_register", "Federal Register"),
    ("easa", "EASA"),
    ("anac_brazil", "ANAC Brasil"),
    ("transport_canada", "Transport Canada"),
    ("dgac_chile", "DGAC Chile"),
    ("anac_argentina", "ANAC Argentina"),
    ("uk_caa", "UK CAA"),
)
_V1_LABELS = dict(_V1_ALL_SOURCES) | {
    "aerocivil_colombia": "AEROCIVIL Colombia",
    "casa_australia": "CASA Australia",
    "gcaa_uae": "GCAA UAE",
}


def _v1_sources(source: str) -> list[tuple[str, str]]:
    """(source_key, label) a consultar para el `source` de una peticion v1."""
    if source == "all":
        return list(_V1_ALL_SOURCES)
    return [(source, _V1_LABELS[source])]


def _query_sources_concurrently(sources, max_results, **filters):
    """
    Una consulta `search_supabase` por fuente, cada una en su hilo, entregando
    `(src_key, label, results, err)` a medida que terminan, no en el orden de
    `sources`.

    En serie, "All authorities" son siete RPC y el usuario esperaba la SUMA de
    todas (cada una puede tardar varios segundos con la caché de Postgres
    fría). En paralelo espera sólo a la más lenta, y el stream pinta cada
    autoridad en cuanto contesta. Cada consulta corre con su propio timeout
    dentro de `run_with_timeout`, así que un hilo colgado no retiene a los demás.
    """
    if not sources:
        return
    pool = ThreadPoolExecutor(max_workers=len(sources))
    try:
        futures = {}
        for src_key, label in sources:
            def fn(src_key=src_key):
                return search_supabase(source=src_key, max_results=max_results, **filters)
            fut = pool.submit(run_with_timeout, fn, SCRAPER_TIMEOUT, label, max_results=max_results)
            futures[fut] = (src_key, label)
        for fut in as_completed(futures):
            src_key, label = futures[fut]
            results, err = fut.result()
            yield src_key, label, results, err
    finally:
        # wait=False por la misma razón que en run_with_timeout: no bloquear la
        # respuesta esperando a un hilo que ya se dio por perdido.
        pool.shutdown(wait=False)


# ------------------------------------------------------------------- rutas --
@bp.route("/api/env")
def api_env() -> RouteResult:
    return jsonify({"serverless": IS_SERVERLESS, "plan": session.get("plan", "anonymous")})


@bp.route("/api/health")
def api_health() -> RouteResult:
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "sources": ["federal_register", "easa", "transport_canada", "anac_brazil", "dgac_chile", "aerocivil_colombia", "anac_argentina", "casa_australia", "gcaa_uae", "uk_caa"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@require_proxy
def _unsupported_version() -> RouteResult:
    return jsonify({"error": "Unsupported search contract version."}), 400


@bp.route("/api/search", methods=["POST"])
def api_search() -> RouteResult:
    body = request.get_json(silent=True)
    version = search_v2.requested_version(body.get("v") if isinstance(body, dict) else None)
    if version == search_v2.VERSION:
        return search_v2.handle_post(body)
    if version is None:
        return _unsupported_version()
    return _api_search_v1()


@require_proxy
def _api_search_v1() -> RouteResult:
    _count_v1_request("/api/search")
    rl = check_rate_limit()
    if rl:
        return rl
    try:
        data = request.get_json()
        source = data.get("source", "federal_register")
        keyword = data.get("keyword", "").strip()
        make = data.get("make", "").strip()
        model = data.get("model", "").strip()
        # Multivalor. El escalar se rellena con la primera entrada cuando no
        # viene suelto; la RPC recibe los dos (`p_make` y `p_makes`).
        makes = normalize_filter_values(data.get("makes") or [])
        models = normalize_filter_values(data.get("models") or [])
        make = make or (makes[0] if makes else "")
        model = model or (models[0] if models else "")
        product_type = data.get("product_type", "").strip()
        date_from = data.get("date_from", "").strip()
        date_to = data.get("date_to", "").strip()
        max_results = min(int(data.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)
        offset = min(max(int(data.get("offset", 0) or 0), 0), 100000)

        if source not in VALID_SOURCES:
            return jsonify({"error": f"Invalid source: '{source}'."}), 400
        gate = validate_search_filters(keyword, make, model, product_type, date_from,
                                       date_to, makes=makes, models=models)
        if gate:
            return jsonify({"error": gate}), 400

        all_results = []
        errors = []
        truncated_sources = []
        total_matched = 0

        sources = _v1_sources(source)
        filters = dict(keyword=keyword, make=make, model=model, product_type=product_type,
                       date_from=date_from, date_to=date_to, makes=makes, models=models, offset=offset)

        # Concurrente, pero la respuesta se arma en el orden de `sources` para
        # que no cambie según qué autoridad contestó primero.
        by_key = {src_key: (label, results, err)
                  for src_key, label, results, err in _query_sources_concurrently(sources, max_results, **filters)}
        for src_key, _label in sources:
            label, results, err = by_key[src_key]
            if err:
                errors.append(err)
            # `getattr` con default: una fuente que falló devuelve `[]` pelada.
            src_total = getattr(results, "total", None)
            if getattr(results, "truncated", False):
                # Por fuente y con su total, igual que cada evento del SSE: los
                # dos caminos deben entregar lo mismo, porque el serverless usa
                # este y el resto usa el otro.
                truncated_sources.append({"source": label, "total_matched": src_total})
            if src_total is not None:
                total_matched += src_total
            if results:
                all_results.extend(results)

        # El truncado NO va en `errors`: no es un error, y mezclarlo ahí lo
        # convertía en un toast rojo de 4s entre fallos reales. El cliente lo
        # pinta como aviso persistente desde el campo estructurado.
        return jsonify({
            "results": all_results,
            "count": len(all_results),
            "total_matched": total_matched or None,
            "truncated": bool(truncated_sources),
            "truncated_sources": truncated_sources,
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/search/stream")
def api_search_stream() -> RouteResult:
    version = search_v2.requested_version(request.args.get("v"))
    if version == search_v2.VERSION:
        return search_v2.handle_stream()
    if version is None:
        return _unsupported_version()
    return _api_search_stream_v1()


@require_proxy
def _api_search_stream_v1() -> RouteResult:
    _count_v1_request("/api/search/stream")
    rl = check_rate_limit()
    if rl:
        return rl
    source = request.args.get("source", "federal_register")
    keyword = request.args.get("keyword", "").strip()
    make = request.args.get("make", "").strip()
    model = request.args.get("model", "").strip()
    # Repetido, no separado por comas: un fabricante del catálogo puede llevar
    # coma dentro ("AIRBUS S.A.S., ELBE FLUGZEUGWERKE GmbH", "REVO, Inc."), así
    # que partir por coma rompería justo los nombres que este filtro existe
    # para alcanzar.
    makes = normalize_filter_values(request.args.getlist("makes"))
    models = normalize_filter_values(request.args.getlist("models"))
    make = make or (makes[0] if makes else "")
    model = model or (models[0] if models else "")
    product_type = request.args.get("product_type", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    max_results = min(int(request.args.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)
    # Fila inicial de esta página, POR FUENTE: cada autoridad pagina su propio
    # corpus. Un offset único para todas es lo que permite que el cliente guarde
    # una sola lista plana; una fuente con menos filas que el offset simplemente
    # no aporta nada a esa página.
    offset = min(max(int(request.args.get("offset", 0) or 0), 0), 100000)

    # Validación de source
    if source not in VALID_SOURCES:
        def error_gen():
            event = {"source": source, "results": [], "count": 0,
                     "error": f"Fuente no válida: '{source}'. Opciones: {', '.join(sorted(VALID_SOURCES - {'all'}))}"}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(error_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Mismo gate que /api/search (utils.validate_search_filters): un make mas un
    # filtro, salvo que el keyword sea un numero de AD o un docket.
    gate = validate_search_filters(keyword, make, model, product_type, date_from,
                                   date_to, makes=makes, models=models)
    if gate:
        def empty_gen():
            event = {"source": source, "results": [], "count": 0, "error": gate}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(empty_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    sources = _v1_sources(source)
    filters = dict(keyword=keyword, make=make, model=model, product_type=product_type,
                   date_from=date_from, date_to=date_to, makes=makes, models=models, offset=offset)

    def generate():
        total = 0
        try:
            for src_key, _label, results, err in _query_sources_concurrently(sources, max_results, **filters):
                count = len(results) if results else 0
                total += count

                # Mismo par que /api/search: por fuente, cuántas casaron en la BD
                # y si lo enviado es solo una parte. `getattr` con default porque
                # una fuente que falló devuelve una lista pelada.
                event = {
                    "source": src_key,
                    "results": results if results else [],
                    "count": count,
                    "total_matched": getattr(results, "total", None),
                    "truncated": getattr(results, "truncated", False),
                    "error": err,
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'event': 'done', 'total': total})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"
        finally:
            yield f"data: {json.dumps({'event': 'done', 'total': total})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
