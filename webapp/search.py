"""
Busqueda de ADs: BD primero (RPC parametrizada `search_ads`), scraping en
vivo como fallback. Rutas: /api/env, /api/health, /api/search (POST) y
/api/search/stream (SSE).
"""

import json
from datetime import datetime, timezone
from math import ceil

from flask import Blueprint, Response, jsonify, request, session

from scrapers.aerocivil_colombia import search_ads as aerocivil_search
from scrapers.anac_argentina import search_ads as anac_argentina_search
from scrapers.anac_brazil import search_ads as anac_brazil_search
from scrapers.casa_australia import search_ads as casa_search
from scrapers.dgac_chile import search_ads as dgac_chile_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.federal_register import search_ads as fr_search
from scrapers.gcaa_uae import search_ads as gcaa_search
from scrapers.transport_canada import search_ads as tc_search
from scrapers.uk_caa import search_ads as uk_caa_search
from utils import (
    MAX_RESULTS_PER_SOURCE,
    VALID_SOURCES,
    map_product_type,
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

bp = Blueprint("search", __name__)

_SUPABASE_PAGE_SIZE = 1000  # PostgREST max rows per request


class DbResults(list):
    """
    Lista de resultados que además carga cuántas filas casaron en la BD.

    Es una `list` a propósito: el contrato de los scrapers es "devuelve una
    lista", y `run_with_timeout` y `build_scrapers` la manipulan como tal. Un
    dict de estadísticas habría que pasarlo por los siete sitios que construyen
    scrapers; esto viaja pegado al resultado y quien no lo mire sigue viendo una
    lista normal. Los consumidores leen con `getattr(x, "truncated", False)`, así
    que un scraper en vivo (que devuelve `list` pelada) no rompe nada.
    """

    total: int | None = None
    truncated: bool = False


# -------------------------------------------------------- service: DB search --
def search_supabase(source, keyword="", make="", model="", product_type="",
                    date_from="", date_to="", max_results=10000) -> list[dict]:
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
    """
    sb = get_supabase_admin()
    if not sb:
        return DbResults()

    try:
        all_rows = []
        offset = 0
        db_total = None

        while offset < max_results:
            batch_size = min(_SUPABASE_PAGE_SIZE, max_results - offset)
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
                "p_offset": offset,
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

            offset += len(rows)

        # Map to scraper output format (PascalCase keys)
        source_labels = {
            "easa": "EASA",
            "federal_register": "FAA (Federal Register)",
            "transport_canada": "Transport Canada",
            "anac_brazil": "ANAC Brasil",
            "dgac_chile": "DGAC Chile",
            "anac_argentina": "ANAC Argentina",
            "aerocivil_colombia": "AEROCIVIL Colombia",
            "casa_australia": "CASA Australia",
            "gcaa_uae": "GCAA UAE",
            "uk_caa": "UK CAA",
        }

        results = []
        for row in all_rows:
            results.append({
                "Source": source_labels.get(row.get("source", ""), row.get("source_label", "")),
                "AD_Number": row.get("ad_number", ""),
                "Subject": row.get("subject", "") or "",
                "Make": row.get("make", "") or "",
                "Model": row.get("model", "") or "",
                "Effective_Date": row.get("effective_date", "") or "",
                "Status": row.get("status", "") or "",
                "Product_Type": row.get("product_type", "") or "",
                "Docket": row.get("docket", "") or "",
                "PDF_Link": row.get("pdf_link", "") or "",
                "Superseded_By": row.get("superseded_by", "") or "",
            })

        out = DbResults(results)
        out.total = db_total
        # Truncado = la BD tenía más de lo que este tope permitió traer. Sin esta
        # señal el bucle de arriba devolvía exactamente `max_results` filas y
        # parecían el total; el contrato de INT-6 exigía justamente exponerlo.
        out.truncated = bool(db_total is not None and db_total > len(results))
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


# --------------------------------------------------- service: scraper plan --
def _make_db_search_fn(src_key, keyword, make, model, product_type, date_from,
                       date_to, max_results, live_fn=None):
    """
    Crea una función que busca primero en Supabase.
    Si Supabase retorna resultados, los usa.  Si no, intenta live_fn como fallback.
    """
    def _db_search():
        db_results = search_supabase(
            source=src_key,
            keyword=keyword,
            make=make,
            model=model,
            product_type=product_type,
            date_from=date_from,
            date_to=date_to,
            max_results=max_results,
        )
        if db_results:
            return db_results
        # Fallback al scraper en vivo si BD no retorna nada
        if live_fn:
            logger.info(f"[DB] {src_key}: 0 resultados en BD, intentando scraping en vivo...")
            return live_fn()
        return []
    return _db_search


def build_scrapers(source, keyword, make, model, product_type, date_from,
                   date_to, max_results=20000) -> list[tuple]:
    """Devuelve lista de (source_key, label, fn) a ejecutar."""
    scrapers = []
    fr_max_pages = ceil(max_results / 100)

    if source in ("federal_register", "all"):
        def _fr_live():
            return fr_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                product_type=product_type or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_pages=fr_max_pages,
            )
        fn = _make_db_search_fn("federal_register", keyword, make, model,
                                product_type, date_from, date_to, max_results, _fr_live)
        scrapers.append(("federal_register", "Federal Register", fn))

    if source in ("easa", "all"):
        _easa_pages = max(2, ceil(max_results / 100))
        def _easa_live():
            search_term = keyword or make or model
            if search_term:
                return easa_search(model_keyword=search_term, max_pages=_easa_pages)
            else:
                return easa_main_page(max_pages=2)
        fn = _make_db_search_fn("easa", keyword, make, model,
                                product_type, date_from, date_to, max_results, _easa_live)
        scrapers.append(("easa", "EASA", fn))

    if source in ("anac_brazil", "all"):
        _anac_limit = min(max_results, 2000)
        def _anac_live():
            return anac_brazil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=_anac_limit,
            )
        fn = _make_db_search_fn("anac_brazil", keyword, make, model,
                                product_type, date_from, date_to, max_results, _anac_live)
        scrapers.append(("anac_brazil", "ANAC Brasil", fn))

    if source in ("transport_canada", "all"):
        if make:
            def _tc_live():
                return tc_search(
                    manufacturer_name=make,
                    model_name=model or None,
                    search_type=map_product_type(product_type),
                )
            fn = _make_db_search_fn("transport_canada", keyword, make, model,
                                    product_type, date_from, date_to, max_results, _tc_live)
            scrapers.append(("transport_canada", "Transport Canada", fn))
        else:
            # Sin make: buscar solo en BD (TC live requiere fabricante)
            fn = _make_db_search_fn("transport_canada", keyword, make, model,
                                    product_type, date_from, date_to, max_results, None)
            scrapers.append(("transport_canada", "Transport Canada", fn))

    if source in ("dgac_chile", "all"):
        def _dgac():
            return dgac_chile_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("dgac_chile", "DGAC Chile", _dgac))

    if source == "aerocivil_colombia":
        def _aerocivil():
            return aerocivil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("aerocivil_colombia", "AEROCIVIL Colombia", _aerocivil))

    if source in ("anac_argentina", "all"):
        def _anac_ar():
            return anac_argentina_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("anac_argentina", "ANAC Argentina", _anac_ar))

    if source == "casa_australia":
        def _casa():
            return casa_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("casa_australia", "CASA Australia", _casa))

    if source == "gcaa_uae":
        def _gcaa():
            return gcaa_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("gcaa_uae", "GCAA UAE", _gcaa))

    if source in ("uk_caa", "all"):
        def _uk_caa_live():
            return uk_caa_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        # DB-first: populate_db.py already loads the ~200-doc UK CAA corpus into
        # Supabase daily. Without this wrapper every search paid the cost of a
        # live scrape (each doc needs its own detail-page fetch for the PDF
        # link, ~60s+ for the full corpus) instead of an instant DB read.
        fn = _make_db_search_fn("uk_caa", keyword, make, model,
                                product_type, date_from, date_to, max_results, _uk_caa_live)
        scrapers.append(("uk_caa", "UK CAA", fn))

    return scrapers


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


@bp.route("/api/search", methods=["POST"])
@require_proxy
def api_search() -> RouteResult:
    rl = check_rate_limit()
    if rl:
        return rl
    try:
        data = request.get_json()
        source = data.get("source", "federal_register")
        keyword = data.get("keyword", "").strip()
        make = data.get("make", "").strip()
        model = data.get("model", "").strip()
        product_type = data.get("product_type", "").strip()
        date_from = data.get("date_from", "").strip()
        date_to = data.get("date_to", "").strip()
        max_results = min(int(data.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)

        if source not in VALID_SOURCES:
            return jsonify({"error": f"Invalid source: '{source}'."}), 400
        gate = validate_search_filters(keyword, make, model, product_type, date_from, date_to)
        if gate:
            return jsonify({"error": gate}), 400

        all_results = []
        errors = []
        truncated_sources = []
        total_matched = 0

        scrapers = build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

        if source == "transport_canada" and not make:
            errors.append("Transport Canada requiere un fabricante.")

        for _src_key, label, fn in scrapers:
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
            if err:
                errors.append(err)
            # `getattr` con default: solo la ruta de BD devuelve DbResults; un
            # scraper en vivo entrega una lista pelada y no sabe cuántas hay.
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
@require_proxy
def api_search_stream() -> RouteResult:
    rl = check_rate_limit()
    if rl:
        return rl
    source = request.args.get("source", "federal_register")
    keyword = request.args.get("keyword", "").strip()
    make = request.args.get("make", "").strip()
    model = request.args.get("model", "").strip()
    product_type = request.args.get("product_type", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    max_results = min(int(request.args.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)

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
    gate = validate_search_filters(keyword, make, model, product_type, date_from, date_to)
    if gate:
        def empty_gen():
            event = {"source": source, "results": [], "count": 0, "error": gate}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(empty_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    scrapers = build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

    def generate():
        total = 0
        try:
            if source in ("transport_canada", "all") and not make and source == "transport_canada":
                event = {
                    "source": "transport_canada",
                    "results": [],
                    "count": 0,
                    "error": "Transport Canada requiere un fabricante."
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            for src_key, label, fn in scrapers:
                results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
                count = len(results) if results else 0
                total += count

                # Mismo par que /api/search: por fuente, cuántas casaron en la BD
                # y si lo enviado es solo una parte. `getattr` con default porque
                # un scraper en vivo devuelve una lista pelada.
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
