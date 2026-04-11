"""
Zephr - Servidor Web
Interfaz web para busqueda de Airworthiness Directives.
Fuentes: Federal Register (USA), EASA (Europa), Transport Canada, ANAC (Brasil).
"""

import os
import io
import re
import sys
import json
import zipfile
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as http_requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, Response, redirect, session
import pandas as pd

# Agregar el directorio raiz al path para importar scrapers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.transport_canada import search_ads as tc_search
from scrapers.anac_brazil import search_ads as anac_brazil_search
from math import ceil
from utils import map_product_type, run_with_timeout, VALID_SOURCES, MAX_RESULTS_PER_SOURCE


app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

APP_VERSION = "1.1.0"

# Supabase Auth (optional — app works without it)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")

_supabase = None
def get_supabase():
    global _supabase
    if _supabase is None and SUPABASE_URL and SUPABASE_ANON_KEY:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _supabase

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Timeout máximo por fuente (en segundos)
SCRAPER_TIMEOUT = 45

PDF_DOWNLOAD_TIMEOUT = 15
PDF_MAX_WORKERS = 5
PDF_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ------------------------------------------------------------------
# Paginas
# ------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("auth.html", mode="login")


@app.route("/register")
def register_page():
    return render_template("auth.html", mode="register")


# ------------------------------------------------------------------
# Auth API
# ------------------------------------------------------------------
@app.route("/auth/login", methods=["POST"])
def auth_login():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        res = sb.auth.sign_in_with_password({"email": data.get("email", ""), "password": data.get("password", "")})
        session["access_token"] = res.session.access_token
        session["user"] = {"id": res.user.id, "email": res.user.email}
        return jsonify({"ok": True, "user": {"id": res.user.id, "email": res.user.email}})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/auth/register", methods=["POST"])
def auth_register():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        res = sb.auth.sign_up({"email": data.get("email", ""), "password": data.get("password", ""), "options": {"data": {"full_name": data.get("name", "")}}})
        return jsonify({"ok": True, "message": "Revisa tu email para confirmar tu cuenta."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/auth/google")
def auth_google():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado.", "configured": False}), 200
    try:
        res = sb.auth.sign_in_with_oauth({"provider": "google", "options": {"redirect_to": request.url_root.rstrip("/") + "/auth/callback"}})
        return redirect(res.url)
    except Exception as e:
        return redirect(f"/login?error={str(e)}")


@app.route("/auth/callback")
def auth_callback():
    sb = get_supabase()
    if not sb:
        return redirect("/login?error=auth_not_configured")
    try:
        code = request.args.get("code")
        if code:
            response = sb.auth.exchange_code_for_session({"auth_code": code})
            if response.session:
                session["access_token"] = response.session.access_token
                session["user"] = {
                    "id": response.user.id,
                    "email": response.user.email,
                    "name": response.user.user_metadata.get("full_name", ""),
                }
                return redirect("/app")
        return redirect("/login?error=callback_failed")
    except Exception as e:
        return redirect(f"/login?error={str(e)}")


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    user = session.get("user")
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"user": user, "configured": bool(get_supabase())})


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------
@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


# ------------------------------------------------------------------
# Helpers para construir las funciones de cada scraper
# ------------------------------------------------------------------

def _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=2000):
    """Devuelve lista de (source_key, label, fn) a ejecutar."""
    scrapers = []
    fr_max_pages = ceil(max_results / 100)

    if source in ("federal_register", "all"):
        def _fr():
            return fr_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                product_type=product_type or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_pages=fr_max_pages,
            )
        scrapers.append(("federal_register", "Federal Register", _fr))

    if source in ("easa", "all"):
        _easa_pages = max(2, ceil(max_results / 100))
        def _easa():
            search_term = keyword or make or model
            if search_term:
                return easa_search(model_keyword=search_term, max_pages=_easa_pages)
            else:
                return easa_main_page(max_pages=2)
        scrapers.append(("easa", "EASA", _easa))

    if source in ("anac_brazil", "all"):
        _anac_limit = min(max_results, 2000)
        def _anac():
            return anac_brazil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=_anac_limit,
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
# API: Health check
# ------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "sources": ["federal_register", "easa", "transport_canada", "anac_brazil"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


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
        max_results = min(int(data.get("max_results", MAX_RESULTS_PER_SOURCE)), 5000)

        all_results = []
        errors = []

        scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

        if source == "transport_canada" and not make:
            errors.append("Transport Canada requiere un fabricante.")

        for _src_key, label, fn in scrapers:
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
            if err:
                errors.append(err)
            if results:
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
    max_results = min(int(request.args.get("max_results", MAX_RESULTS_PER_SOURCE)), 5000)

    # Validación de source
    if source not in VALID_SOURCES:
        def error_gen():
            event = {"source": source, "results": [], "count": 0,
                     "error": f"Fuente no válida: '{source}'. Opciones: {', '.join(sorted(VALID_SOURCES - {'all'}))}"}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(error_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Validación de inputs
    if not keyword and not make and not model and source != "all":
        def empty_gen():
            event = {"source": source, "results": [], "count": 0,
                     "error": "Se requiere al menos un término de búsqueda (keyword, make o model)."}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(empty_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

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
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
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
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    col_order = [
        "Source", "AD_Number", "Subject", "Make", "Model",
        "Effective_Date", "Status", "Product_Type", "Docket", "PDF_Link"
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order]
    return df[existing_cols + extra_cols]


def _safe_filename(name):
    """Sanitiza un nombre para usar como archivo."""
    if not name:
        return "unknown"
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def _download_pdf(url, timeout=PDF_DOWNLOAD_TIMEOUT):
    """Descarga un PDF y devuelve (bytes, None) o (None, error_msg)."""
    try:
        resp = http_requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": PDF_USER_AGENT},
            allow_redirects=True,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        if len(resp.content) < 100:
            return None, "Archivo vacío o inválido"
        return resp.content, None
    except http_requests.Timeout:
        return None, f"Timeout ({timeout}s)"
    except Exception as e:
        return None, str(e)


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
            return _export_zip(results, df, timestamp, data.get("filters"))

        else:
            return jsonify({"error": f"Formato no soportado: {fmt}"}), 400

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _export_zip(results, df, timestamp, filters=None):
    """Genera un ZIP con CSV + XLSX + PDFs descargados + README.txt."""
    zip_buffer = io.BytesIO()

    # Descargar PDFs en paralelo
    pdf_tasks = []
    for r in results:
        link = r.get("PDF_Link")
        ad_num = r.get("AD_Number", "unknown")
        if link:
            pdf_tasks.append((ad_num, link))

    pdf_results = {}  # ad_num -> (bytes|None, error|None)

    if pdf_tasks:
        with ThreadPoolExecutor(max_workers=PDF_MAX_WORKERS) as executor:
            futures = {}
            for ad_num, link in pdf_tasks:
                safe_name = _safe_filename(ad_num)
                future = executor.submit(_download_pdf, link)
                futures[future] = (safe_name, link)

            for future in as_completed(futures):
                safe_name, link = futures[future]
                try:
                    content, error = future.result()
                    pdf_results[safe_name] = (content, error, link)
                except Exception as e:
                    pdf_results[safe_name] = (None, str(e), link)

    pdfs_ok = sum(1 for _, (c, e, _) in pdf_results.items() if c is not None)
    pdfs_fail = sum(1 for _, (c, e, _) in pdf_results.items() if c is None)

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # CSV
        csv_buf = io.BytesIO()
        df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
        zf.writestr("ad_results.csv", csv_buf.getvalue())

        # XLSX
        xlsx_buf = io.BytesIO()
        df.to_excel(xlsx_buf, index=False, engine="openpyxl")
        zf.writestr("ad_results.xlsx", xlsx_buf.getvalue())

        # PDFs
        for safe_name, (content, error, link) in pdf_results.items():
            if content:
                zf.writestr(f"PDFs/{safe_name}.pdf", content)
            else:
                error_text = f"No se pudo descargar el PDF.\nURL: {link}\nError: {error}\n"
                zf.writestr(f"PDFs/{safe_name}_NO_DISPONIBLE.txt", error_text)

        # README
        readme_lines = [
            "Zephr - Export",
            "=" * 50,
            f"Fecha de exportacion: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Total de ADs: {len(df)}",
            f"PDFs descargados: {pdfs_ok}",
            f"PDFs no disponibles: {pdfs_fail}",
            "",
        ]
        if filters:
            readme_lines.append("Filtros utilizados:")
            for k, v in filters.items():
                if v:
                    readme_lines.append(f"  {k}: {v}")
        else:
            readme_lines.append("Filtros: (sin filtros registrados)")

        readme_lines += [
            "",
            "Archivos incluidos:",
            "  ad_results.csv    - Formato CSV (UTF-8 BOM)",
            "  ad_results.xlsx   - Formato Excel",
            f"  PDFs/             - {pdfs_ok} PDFs descargados",
            "  README.txt        - Este archivo",
            "",
            "Fuentes: FAA Federal Register | EASA | Transport Canada | ANAC Brasil",
        ]
        zf.writestr("README.txt", "\n".join(readme_lines))

    zip_buffer.seek(0)

    n_docs = len(results)
    download_name = f"AD_Export_{timestamp}_{n_docs}_documentos.zip"

    return send_file(
        zip_buffer, mimetype="application/zip",
        as_attachment=True, download_name=download_name,
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
    print("  Zephr - Airworthiness Directives")
    print("  Fuentes: Federal Register | EASA | Transport Canada | ANAC")
    print("=" * 55)
    print(f"\n  Landing:  http://127.0.0.1:5000/")
    print(f"  App:      http://127.0.0.1:5000/app")
    print(f"  Health:   http://127.0.0.1:5000/api/health\n")

    app.run(host="127.0.0.1", port=5000, debug=True)
