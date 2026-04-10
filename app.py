"""
AD Search - Servidor Web
Interfaz web para busqueda de Airworthiness Directives.
Fuentes: Federal Register (USA), EASA (Europa), Transport Canada, ANAC (Brasil).
"""

import os
import io
import sys
import json
import zipfile
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file, Response
import pandas as pd

# Agregar el directorio raiz al path para importar scrapers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.transport_canada import search_ads as tc_search
from scrapers.anac_brazil import search_ads as anac_brazil_search
from utils import map_product_type, run_with_timeout


app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Timeout máximo por fuente (en segundos)
SCRAPER_TIMEOUT = 45


# ------------------------------------------------------------------
# Paginas
# ------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------
@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


# ------------------------------------------------------------------
# Helpers para construir las funciones de cada scraper
# ------------------------------------------------------------------

def _build_scrapers(source, keyword, make, model, product_type, date_from, date_to):
    """Devuelve lista de (source_key, label, fn) a ejecutar."""
    scrapers = []

    if source in ("federal_register", "all"):
        def _fr():
            return fr_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                product_type=product_type or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_pages=5,
            )
        scrapers.append(("federal_register", "Federal Register", _fr))

    if source in ("easa", "all"):
        def _easa():
            search_term = keyword or make or model
            if search_term:
                return easa_search(model_keyword=search_term, max_pages=5)
            else:
                return easa_main_page(max_pages=2)
        scrapers.append(("easa", "EASA", _easa))

    if source in ("anac_brazil", "all"):
        def _anac():
            return anac_brazil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=200,
            )
        scrapers.append(("anac_brazil", "ANAC Brasil", _anac))

    if source in ("transport_canada", "all"):
        if make:
            def _tc():
                return tc_search(
                    manufacturer_name=make,
                    model_name=model or None,
                    search_type=map_product_type(product_type),
                )
            scrapers.append(("transport_canada", "Transport Canada", _tc))
        elif source == "transport_canada":
            pass

    return scrapers


# ------------------------------------------------------------------
# API: Busqueda de ADs (POST — compatibilidad)
# ------------------------------------------------------------------
@app.route("/api/search", methods=["POST"])
def api_search():
    try:
        data = request.get_json()
        source = data.get("source", "federal_register")
        keyword = data.get("keyword", "").strip()
        make = data.get("make", "").strip()
        model = data.get("model", "").strip()
        product_type = data.get("product_type", "").strip()
        date_from = data.get("date_from", "").strip()
        date_to = data.get("date_to", "").strip()

        all_results = []
        errors = []

        scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to)

        if source == "transport_canada" and not make:
            errors.append("Transport Canada requiere un fabricante.")

        for _src_key, label, fn in scrapers:
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label)
            if err:
                errors.append(err)
            else:
                all_results.extend(results)

        return jsonify({
            "results": all_results,
            "count": len(all_results),
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# API: Busqueda SSE (GET — streaming)
# ------------------------------------------------------------------
@app.route("/api/search/stream")
def api_search_stream():
    source = request.args.get("source", "federal_register")
    keyword = request.args.get("keyword", "").strip()
    make = request.args.get("make", "").strip()
    model = request.args.get("model", "").strip()
    product_type = request.args.get("product_type", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to)

    def generate():
        total = 0

        if source in ("transport_canada", "all") and not make and source == "transport_canada":
            event = {
                "source": "transport_canada",
                "results": [],
                "count": 0,
                "error": "Transport Canada requiere un fabricante."
            }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        for src_key, label, fn in scrapers:
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label)
            count = len(results) if results else 0
            total += count

            event = {
                "source": src_key,
                "results": results if results else [],
                "count": count,
                "error": err,
            }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

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


# ------------------------------------------------------------------
# API: Exportar resultados
# ------------------------------------------------------------------
def _build_dataframe(results):
    """Construye un DataFrame ordenado a partir de resultados."""
    df = pd.DataFrame(results)
    col_order = [
        "Source", "AD_Number", "Subject", "Make", "Model",
        "Effective_Date", "Status", "Product_Type", "Docket", "PDF_Link"
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order]
    return df[existing_cols + extra_cols]


@app.route("/api/export/<fmt>", methods=["POST"])
def api_export(fmt):
    try:
        data = request.get_json()
        results = data.get("results", [])

        if not results:
            return jsonify({"error": "No hay resultados para exportar."}), 400

        df = _build_dataframe(results)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if fmt == "csv":
            output = io.BytesIO()
            df.to_csv(output, index=False, encoding="utf-8-sig")
            output.seek(0)
            return send_file(
                output, mimetype="text/csv",
                as_attachment=True, download_name=f"ad_results_{timestamp}.csv",
            )

        elif fmt == "excel":
            output = io.BytesIO()
            df.to_excel(output, index=False, engine="openpyxl")
            output.seek(0)
            return send_file(
                output,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True, download_name=f"ad_results_{timestamp}.xlsx",
            )

        elif fmt == "zip":
            return _export_zip(data, df, timestamp)

        else:
            return jsonify({"error": f"Formato no soportado: {fmt}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _export_zip(data, df, timestamp):
    """Genera un ZIP con CSV + XLSX + README.txt."""
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        csv_buf = io.BytesIO()
        df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
        zf.writestr("ad_results.csv", csv_buf.getvalue())

        xlsx_buf = io.BytesIO()
        df.to_excel(xlsx_buf, index=False, engine="openpyxl")
        zf.writestr("ad_results.xlsx", xlsx_buf.getvalue())

        filters = data.get("filters", {})
        readme_lines = [
            "AD Search - Export",
            "=" * 40,
            f"Fecha de exportacion: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Total de registros: {len(df)}",
            "",
            "Filtros utilizados:",
        ]
        if filters:
            for k, v in filters.items():
                if v:
                    readme_lines.append(f"  {k}: {v}")
        else:
            readme_lines.append("  (sin filtros registrados)")

        readme_lines += [
            "",
            "Archivos incluidos:",
            "  ad_results.csv   - Formato CSV (UTF-8 BOM)",
            "  ad_results.xlsx  - Formato Excel",
            "  README.txt       - Este archivo",
            "",
            "Fuentes: FAA Federal Register | EASA | Transport Canada | ANAC Brasil",
        ]
        zf.writestr("README.txt", "\n".join(readme_lines))

    zip_buffer.seek(0)
    return send_file(
        zip_buffer, mimetype="application/zip",
        as_attachment=True, download_name=f"ad_export_{timestamp}.zip",
    )


# ------------------------------------------------------------------
# API: Estado de las fuentes
# ------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    return jsonify({
        "sources": {
            "federal_register": {"name": "Federal Register (USA)", "status": "online"},
            "easa": {"name": "EASA (Europa)", "status": "online"},
            "transport_canada": {"name": "Transport Canada", "status": "online"},
            "anac_brazil": {"name": "ANAC (Brasil)", "status": "online"},
        }
    })


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  AD Search - Interfaz Web")
    print("  Fuentes: Federal Register | EASA | Transport Canada | ANAC")
    print("=" * 55)
    print(f"\n  Landing:  http://127.0.0.1:5000/")
    print(f"  App:      http://127.0.0.1:5000/app\n")

    app.run(host="127.0.0.1", port=5000, debug=True)
