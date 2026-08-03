"""
Suite de contrato HTTP (Fase 3b) — por ruta: metodo, status esperado, forma
del JSON, headers CORS y comportamiento sin auth. Es el criterio de que el
refactor a blueprints no cambia el comportamiento observable: debe pasar
identica antes y despues de cada commit del refactor.

Sin red ni servicios externos, con dos excepciones deliberadas y acotadas:
/api/status (pings HEAD con timeout de 3s; solo se asserta la FORMA) y ningun
test invoca el camino real de scraping (las busquedas validas dispararian
scrapers en vivo — eso no es contrato HTTP, es integracion).
"""

import json

# ---------------------------------------------------------------- paginas --

def test_landing_ok(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.mimetype == "text/html"


def test_app_redirects_to_login_without_session(client):
    r = client.get("/app")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    assert "next=/app" in r.headers["Location"]


def test_app_renders_with_session(authed_client):
    r = authed_client.get("/app")
    assert r.status_code == 200
    assert r.mimetype == "text/html"


def test_login_and_register_pages(client):
    for path in ("/login", "/register"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.mimetype == "text/html"


def test_unknown_path_renders_404_page(client):
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert r.mimetype == "text/html"


# ------------------------------------------------------------------- auth --

def test_auth_login_unconfigured_is_explicit(client):
    r = client.post("/auth/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["configured"] is False
    assert "error" in body


def test_auth_register_unconfigured_is_explicit(client):
    r = client.post("/auth/register", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 200
    assert r.get_json()["configured"] is False


def test_auth_google_unconfigured(client):
    r = client.get("/auth/google")
    assert r.status_code == 200
    assert r.get_json()["configured"] is False


def test_auth_callback_unconfigured_redirects(client):
    r = client.get("/auth/callback")
    assert r.status_code == 302
    assert "error=auth_not_configured" in r.headers["Location"]


def test_auth_logout_always_ok(client):
    r = client.post("/auth/logout")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_auth_me_requires_session(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert "error" in r.get_json()


def test_auth_me_with_session(authed_client):
    r = authed_client.get("/auth/me")
    assert r.status_code == 200
    body = r.get_json()
    assert body["user"]["email"] == "test@example.com"
    assert body["plan"] == "free"
    assert body["configured"] is False


# ---------------------------------------------------------------- billing --

def test_stripe_config_shape(client):
    r = client.get("/api/stripe/config")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"publishable_key", "plans"}
    assert set(body["plans"]) == {"pro", "team"}
    for plan in body["plans"].values():
        assert {"price_id", "amount", "name"} <= set(plan)


def test_checkout_requires_auth(client):
    r = client.post("/api/stripe/create-checkout-session", json={"plan": "pro"})
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_checkout_unconfigured_stripe_is_500(authed_client):
    r = authed_client.post("/api/stripe/create-checkout-session", json={"plan": "pro"})
    assert r.status_code == 500
    assert r.get_json()["error"] == "Stripe no configurado"


def test_debug_stripe_hidden_by_default(client):
    r = client.get("/api/debug/stripe")
    assert r.status_code == 404


def test_stripe_webhook_is_gone(client):
    r = client.post("/api/stripe/webhook", data="{}")
    assert r.status_code == 410


def test_payment_pages_render(client):
    for path in ("/payment/success", "/payment/cancel"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.mimetype == "text/html"


# ----------------------------------------------------------------- search --

def test_api_env_shape(client):
    r = client.get("/api/env")
    assert r.status_code == 200
    body = r.get_json()
    assert body["serverless"] is False
    assert body["plan"] == "anonymous"


def test_api_health_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str)
    assert len(body["sources"]) == 10
    assert "timestamp" in body


def test_api_health_cors_echo_without_credentials(client):
    # Sin ALLOWED_ORIGINS, flask-cors ecoa el Origin (con Vary: Origin) pero
    # NUNCA con credenciales — esa combinacion es la que SEC-5 prohibe.
    r = client.get("/api/health", headers={"Origin": "https://example.com"})
    assert r.headers.get("Access-Control-Allow-Origin") == "https://example.com"
    assert "Access-Control-Allow-Credentials" not in r.headers


def test_search_rejects_invalid_source(client):
    r = client.post("/api/search", json={"source": "narnia_caa"})
    assert r.status_code == 400
    assert "Invalid source" in r.get_json()["error"]


def test_search_all_requires_a_term(client):
    r = client.post("/api/search", json={"source": "all"})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_search_proxy_guard(client, proxied):
    r = client.post("/api/search", json={"source": "all"})
    assert r.status_code == 403
    r = client.post("/api/search", json={"source": "all"},
                    headers={"X-Internal-Secret": "wrong"})
    assert r.status_code == 403
    # Con el secreto correcto pasa el guard y llega a la validacion (400).
    r = client.post("/api/search", json={"source": "all"},
                    headers={"X-Internal-Secret": proxied})
    assert r.status_code == 400


def test_search_reports_truncation_instead_of_hiding_it(client, monkeypatch):
    """
    Un resultado recortado por el tope debe decirlo. Antes la ruta devolvia una
    lista plana y el cliente no podia distinguir "estos son todos" de "estos son
    los primeros 10.000 de 30.000" — el defecto que INT-6 dejo abierto.
    """
    from webapp import search as search_mod

    truncated = search_mod.DbResults([_ROW])
    truncated.total = 4297
    truncated.truncated = True
    monkeypatch.setattr(search_mod, "build_scrapers",
                        lambda *a, **k: [("easa", "EASA", lambda: truncated)])

    r = client.post("/api/search", json={"source": "easa", "keyword": "engine"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["truncated"] is True
    assert body["total_matched"] == 4297
    assert body["truncated_sources"] == ["EASA"]
    assert body["count"] == 1
    # El aviso viaja tambien por `errors`, que es el canal que la UI ya muestra.
    assert any("Narrow the search" in e for e in body["errors"])


def test_search_complete_result_is_not_flagged(client, monkeypatch):
    from webapp import search as search_mod

    whole = search_mod.DbResults([_ROW])
    whole.total = 1
    whole.truncated = False
    monkeypatch.setattr(search_mod, "build_scrapers",
                        lambda *a, **k: [("easa", "EASA", lambda: whole)])

    body = client.post("/api/search", json={"source": "easa"}).get_json()
    assert body["truncated"] is False
    assert body["truncated_sources"] == []
    assert body["errors"] == []


def test_search_live_scraper_list_carries_no_signal(client, monkeypatch):
    """
    Un scraper en vivo devuelve una `list` pelada: no sabe cuantas hay en la BD.
    La ruta debe leerlo con default y no inventarse un total.
    """
    from webapp import search as search_mod

    monkeypatch.setattr(search_mod, "build_scrapers",
                        lambda *a, **k: [("easa", "EASA", lambda: [_ROW])])
    body = client.post("/api/search", json={"source": "easa"}).get_json()
    assert body["truncated"] is False
    assert body["total_matched"] is None


def _sse_events(response):
    text = response.get_data(as_text=True)
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_search_stream_invalid_source_streams_error(client):
    r = client.get("/api/search/stream?source=narnia_caa")
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    events = _sse_events(r)
    assert any(e.get("error") for e in events)
    assert events[-1].get("event") == "done"


def test_search_stream_all_without_terms_streams_error(client):
    r = client.get("/api/search/stream?source=all")
    assert r.status_code == 200
    events = _sse_events(r)
    assert any(e.get("error") for e in events)
    assert events[-1].get("event") == "done"


def test_search_stream_proxy_guard(client, proxied):
    r = client.get("/api/search/stream?source=all")
    assert r.status_code == 403


# ----------------------------------------------------------------- export --

_ROW = {
    "AD_Number": "2026-01-01", "Source": "FAA", "Subject": "Test subject",
    "Make": "Boeing", "Model": "737", "Effective_Date": "2026-01-01",
    "Status": "New", "Product_Type": "Aircraft", "Docket": "", "PDF_Link": "",
}


def test_export_without_results_is_400(client):
    r = client.post("/api/export/csv", json={"results": []})
    assert r.status_code == 400


def test_export_unknown_format_is_400(client):
    r = client.post("/api/export/parquet", json={"results": [_ROW]})
    assert r.status_code == 400
    assert "Formato no soportado" in r.get_json()["error"]


def test_export_csv_attachment(client):
    r = client.post("/api/export/csv", json={"results": [_ROW]})
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    assert b"2026-01-01" in r.get_data()


def test_export_excel_attachment(client):
    r = client.post("/api/export/excel", json={"results": [_ROW]})
    assert r.status_code == 200
    assert "spreadsheet" in r.mimetype
    assert r.get_data()[:2] == b"PK"


def test_export_zip_without_pdfs(client):
    # Sin PDF_Link no se descarga nada: el ZIP (csv+xlsx+readme) sale offline.
    r = client.post("/api/export/zip", json={"results": [_ROW]})
    assert r.status_code == 200
    assert r.mimetype in ("application/zip", "application/x-zip-compressed")
    assert r.get_data()[:2] == b"PK"


def test_export_proxy_guard(client, proxied):
    r = client.post("/api/export/csv", json={"results": [_ROW]})
    assert r.status_code == 403


# ----------------------------------------------------------------- status --

def test_api_status_shape(client):
    # Hace pings HEAD reales con timeout 3s; offline degrada a error/unknown.
    # El contrato es la FORMA: 200 + un dict con las 7 fuentes pingeables
    # (las 3 deshabilitadas del cron no se pingean).
    r = client.get("/api/status")
    assert r.status_code == 200
    sources = r.get_json()["sources"]
    assert len(sources) == 7
    for entry in sources.values():
        assert {"name", "status"} <= set(entry)
        assert entry["status"] in ("online", "error", "unknown")
