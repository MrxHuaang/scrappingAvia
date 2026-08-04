"""Estado de las fuentes: ping HEAD real a cada autoridad pingeable."""

from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as http_requests
from flask import Blueprint, jsonify

from webapp.core import RouteResult

bp = Blueprint("status", __name__)

_SOURCE_PING_URLS = {
    "federal_register": ("Federal Register (USA)", "https://www.federalregister.gov"),
    "easa":             ("EASA (Europa)",          "https://ad.easa.europa.eu"),
    "transport_canada": ("Transport Canada",       "https://tc.canada.ca"),
    "anac_brazil":      ("ANAC (Brasil)",          "https://sistemas.anac.gov.br"),
    "dgac_chile":       ("DGAC (Chile)",           "https://www.dgac.gob.cl"),
    "anac_argentina":   ("ANAC (Argentina)",       "https://www.argentina.gob.ar"),
    "uk_caa":           ("UK CAA",                 "https://www.caa.co.uk"),
}


def _ping_source(key) -> tuple[str, str, str]:
    name, url = _SOURCE_PING_URLS[key]
    try:
        resp = http_requests.head(
            url, timeout=3, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 Zephr-HealthCheck/1.0"},
        )
        status = "online" if resp.status_code < 400 else "error"
    except Exception:
        status = "error"
    return key, name, status


@bp.route("/api/status")
def api_status() -> RouteResult:
    results = {}
    with ThreadPoolExecutor(max_workers=len(_SOURCE_PING_URLS)) as executor:
        futures = [executor.submit(_ping_source, k) for k in _SOURCE_PING_URLS]
        for future in as_completed(futures):
            try:
                key, name, status = future.result(timeout=5)
                results[key] = {"name": name, "status": status}
            except Exception:
                pass

    for key in _SOURCE_PING_URLS:
        if key not in results:
            name = _SOURCE_PING_URLS[key][0]
            results[key] = {"name": name, "status": "unknown"}

    return jsonify({"sources": results})
