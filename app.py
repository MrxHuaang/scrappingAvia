"""
AD Scraper - Servidor Web
Interfaz web para busqueda de Airworthiness Directives.
Fuentes: Federal Register (USA), EASA (Europa), Transport Canada.
"""

import os
import io
import sys
import json
import threading
from datetime import datetime

from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd

# Agregar el directorio raiz al path para importar scrapers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.transport_canada import search_ads as tc_search
from scrapers.anac_brazil import search_ads as anac_brazil_search


app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ------------------------------------------------------------------
# Pagina principal
# ------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------------
# API: Busqueda de ADs
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

        # Federal Register (USA)
        if source in ("federal_register", "all"):
            try:
                results = fr_search(
                    keyword=keyword or None,
                    make=make or None,
                    model=model or None,
                    product_type=product_type or None,
                    date_from=date_from or None,
                    date_to=date_to or None,
                    max_pages=5,
                )
                all_results.extend(results)
            except Exception as e:
                errors.append(f"Federal Register: {str(e)}")

        # EASA (Europa)
        if source in ("easa", "all"):
            try:
                search_term = keyword or make or model
                if search_term:
                    results = easa_search(
                        model_keyword=search_term,
                        max_pages=5,
                    )
                else:
                    results = easa_main_page(max_pages=2)
                all_results.extend(results)
            except Exception as e:
                errors.append(f"EASA: {str(e)}")

        # ANAC Brasil
        if source in ("anac_brazil", "all"):
            try:
                results = anac_brazil_search(
                    keyword=keyword or None,
                    make=make or None,
                    model=model or None,
                    date_from=date_from or None,
                    date_to=date_to or None,
                    max_results=200,
                )
                all_results.extend(results)
            except Exception as e:
                errors.append(f"ANAC Brasil: {str(e)}")

        # Transport Canada
        if source in ("transport_canada", "all"):
            try:
                if make:
                    results = tc_search(
                        manufacturer_name=make,
                        model_name=model or None,
                        search_type=_map_product_type(product_type),
                    )
                    all_results.extend(results)
                elif source == "transport_canada":
                    errors.append("Transport Canada requiere un fabricante.")
            except Exception as e:
                errors.append(f"Transport Canada: {str(e)}")

        return jsonify({
            "results": all_results,
            "count": len(all_results),
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# API: Exportar resultados
# ------------------------------------------------------------------
@app.route("/api/export/<fmt>", methods=["POST"])
def api_export(fmt):
    try:
        data = request.get_json()
        results = data.get("results", [])

        if not results:
            return jsonify({"error": "No hay resultados para exportar."}), 400

        df = pd.DataFrame(results)

        # Ordenar columnas
        col_order = [
            "Source", "AD_Number", "Subject", "Make", "Model",
            "Effective_Date", "Status", "Product_Type", "Docket", "PDF_Link"
        ]
        existing_cols = [c for c in col_order if c in df.columns]
        extra_cols = [c for c in df.columns if c not in col_order]
        df = df[existing_cols + extra_cols]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if fmt == "csv":
            output = io.BytesIO()
            df.to_csv(output, index=False, encoding="utf-8-sig")
            output.seek(0)

            return send_file(
                output,
                mimetype="text/csv",
                as_attachment=True,
                download_name=f"ad_results_{timestamp}.csv",
            )

        elif fmt == "excel":
            output = io.BytesIO()
            df.to_excel(output, index=False, engine="openpyxl")
            output.seek(0)

            return send_file(
                output,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=f"ad_results_{timestamp}.xlsx",
            )

        else:
            return jsonify({"error": f"Formato no soportado: {fmt}"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
# Helpers
# ------------------------------------------------------------------
def _map_product_type(product_type):
    """Mapea tipo de producto al formato de Transport Canada."""
    mapping = {
        "Aircraft": "aircraft",
        "Engine": "engine",
        "Propeller": "propeller",
    }
    return mapping.get(product_type, "aircraft")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  AD Scraper - Interfaz Web")
    print("  Fuentes: Federal Register | EASA | Transport Canada")
    print("=" * 55)
    print(f"\n  Abre en tu navegador: http://127.0.0.1:5000\n")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True,
    )
