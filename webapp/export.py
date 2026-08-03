"""Export de resultados: CSV, Excel y ZIP (con PDFs de dominios permitidos)."""

import io
import ipaddress
import re
import socket
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import requests as http_requests
from flask import Blueprint, jsonify, request, send_file

from webapp.core import (
    PDF_DOWNLOAD_TIMEOUT,
    PDF_MAX_WORKERS,
    PDF_USER_AGENT,
    RouteResult,
    check_rate_limit,
    require_proxy,
)

bp = Blueprint("export", __name__)

MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB hard cap per PDF

_PDF_ALLOWED_SUFFIXES = (
    ".faa.gov", ".easa.europa.eu", ".tc.canada.ca", ".tc.gc.ca", ".gov.br",
    ".anac.gov.br", ".gcaa.gov.ae", ".dgac.gob.cl", ".anac.gov.ar",
    ".casa.gov.au", ".aerocivil.gov.co", ".federalregister.gov",
    ".govinfo.gov", ".argentina.gob.ar",
)
_PDF_ALLOWED_EXACT = {
    "faa.gov", "easa.europa.eu", "tc.canada.ca", "tc.gc.ca",
    "gcaa.gov.ae", "federalregister.gov",
}


# ---------------------------------------------------------------- services --
def _build_dataframe(results) -> pd.DataFrame:
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


def _safe_filename(name) -> str:
    """Sanitiza un nombre para usar como archivo."""
    if not name:
        return "unknown"
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def _host_resolves_private(host) -> bool:
    """True if the host resolves to any private/loopback/link-local/reserved IP (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _is_safe_pdf_url(url) -> bool:
    """Only allow http(s) URLs on known regulator domains that don't resolve to private IPs."""
    if not url or not isinstance(url, str):
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    allowed = host in _PDF_ALLOWED_EXACT or any(host.endswith(s) for s in _PDF_ALLOWED_SUFFIXES)
    if not allowed:
        return False
    return not _host_resolves_private(host)


def _download_pdf(url, timeout=PDF_DOWNLOAD_TIMEOUT) -> tuple[bytes | None, str | None]:
    """Descarga un PDF de una fuente permitida y devuelve (bytes, None) o (None, error_msg)."""
    if not _is_safe_pdf_url(url):
        return None, "URL no permitida"
    try:
        resp = http_requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": PDF_USER_AGENT},
            allow_redirects=False,  # block redirect-based SSRF
            stream=True,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        chunks = []
        total = 0
        for chunk in resp.iter_content(8192):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                return None, "Archivo demasiado grande"
            chunks.append(chunk)
        content = b"".join(chunks)
        if len(content) < 100:
            return None, "Archivo vacío o inválido"
        return content, None
    except http_requests.Timeout:
        return None, f"Timeout ({timeout}s)"
    except Exception as e:
        return None, str(e)


def _export_zip(results, df, timestamp, filters=None) -> RouteResult:
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
            "Fuentes activas (búsqueda múltiple): FAA | EASA | TC | ANAC BR | DGAC CL | ANAC AR",
        ]
        zf.writestr("README.txt", "\n".join(readme_lines))

    zip_buffer.seek(0)

    n_docs = len(results)
    download_name = f"AD_Export_{timestamp}_{n_docs}_documentos.zip"

    return send_file(
        zip_buffer, mimetype="application/zip",
        as_attachment=True, download_name=download_name,
    )


# ------------------------------------------------------------------- rutas --
@bp.route("/api/export/<fmt>", methods=["POST"])
@require_proxy
def api_export(fmt) -> RouteResult:
    rl = check_rate_limit()
    if rl:
        return rl
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
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
