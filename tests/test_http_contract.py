"""
Suite de contrato HTTP (Fase 3b) — por ruta: metodo, status esperado, forma
del JSON, headers CORS y comportamiento sin auth. Es el criterio de que el
refactor a blueprints no cambia el comportamiento observable: debe pasar
identica antes y despues de cada commit del refactor.

Sin red ni servicios externos, con una excepcion deliberada y acotada:
/api/status (pings HEAD con timeout de 3s; solo se asserta la FORMA). Las
busquedas validas llegarian a Supabase; los tests sustituyen la consulta.
"""

import json

import pytest

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
    monkeypatch.setattr(search_mod, "search_supabase", lambda **k: truncated)

    r = client.post("/api/search", json={"source": "easa", "make": "Airbus", "keyword": "engine"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["truncated"] is True
    assert body["total_matched"] == 4297
    assert body["count"] == 1
    # Por fuente y con su total: la misma forma que trae cada evento del SSE, para
    # que el cliente pinte el mismo aviso venga por donde venga. El serverless usa
    # esta ruta y el resto usa el stream.
    assert body["truncated_sources"] == [{"source": "EASA", "total_matched": 4297}]
    # Y NO viaja por `errors`: no es un error, y ahi se volvia un toast rojo de 4s
    # entre fallos reales en vez de un aviso persistente.
    assert body["errors"] == []


def test_search_complete_result_is_not_flagged(client, monkeypatch):
    from webapp import search as search_mod

    whole = search_mod.DbResults([_ROW])
    whole.total = 1
    whole.truncated = False
    monkeypatch.setattr(search_mod, "search_supabase", lambda **k: whole)

    body = client.post("/api/search", json=_FILTERED_QUERY).get_json()
    assert body["truncated"] is False
    assert body["truncated_sources"] == []
    assert body["errors"] == []


def test_search_plain_list_carries_no_signal(client, monkeypatch):
    """
    Una fuente que fallo devuelve una `list` pelada: no sabe cuantas hay en la
    BD. La ruta debe leerlo con default y no inventarse un total.
    """
    from webapp import search as search_mod

    monkeypatch.setattr(search_mod, "search_supabase", lambda **k: [_ROW])
    body = client.post("/api/search", json=_FILTERED_QUERY).get_json()
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


def test_stream_and_post_report_truncation_the_same_way(client, monkeypatch):
    """
    Los dos caminos deben entregar la MISMA informacion por fuente.

    Este test existe porque divergieron: el evento SSE llevaba `truncated` y
    `total_matched` y la respuesta POST solo una lista de etiquetas, asi que el
    cliente pintaba el aviso persistente en streaming y no en serverless — que es
    justo el despliegue que usa POST.
    """
    from webapp import search as search_mod

    truncated = search_mod.DbResults([_ROW])
    truncated.total = 4297
    truncated.truncated = True
    monkeypatch.setattr(search_mod, "search_supabase", lambda **k: truncated)

    post = client.post("/api/search",
                       json={"source": "easa", "make": "Airbus", "keyword": "engine"}).get_json()
    events = _sse_events(
        client.get("/api/search/stream?source=easa&make=Airbus&keyword=engine"))
    payload = [e for e in events if e.get("source") == "easa"][0]

    assert post["truncated_sources"] == [{"source": "EASA", "total_matched": 4297}]
    assert payload["truncated"] is True
    assert payload["total_matched"] == 4297
    # El mismo par (esta truncado, cuantas casaron) por ambos canales.
    assert post["truncated_sources"][0]["total_matched"] == payload["total_matched"]


def test_search_stream_proxy_guard(client, proxied):
    r = client.get("/api/search/stream?source=all")
    assert r.status_code == 403


# ------------------------------------------------ search: contratos v1/v2 --
# La BD es un doble que registra cada RPC. v1 tiene que seguir mandando lo
# mismo a `search_ads` (que en la BD pasa a ser un shim sobre v2); v2 hace UNA
# llamada a `search_ads_v2` por pagina con las fuentes de la cabecera del proxy.

_MFR = "3f2b8c1e-0a4d-4c6e-9b7a-1d2e3f4a5b6c"


def _db_row(n, source="easa"):
    return {
        "source": source, "source_label": "EASA", "issuing_authority": source,
        "ad_number": f"2024-{n:04d}", "subject": "Fuel pump", "make": "AIRBUS S.A.S.",
        "model": "A320-214", "product_type": "Aircraft", "effective_date": "2024-01-15",
        "status": "New", "docket": None, "pdf_link": None, "superseded_by": None,
        "match_confidence": "high", "applicability_unknown": False, "has_text": False,
    }


class _FakeDb:
    """
    Doble de Supabase para las dos RPC de busqueda. `search_ads` pagina un
    corpus de `v1_total` filas (con `total_count` en cada fila, como la RPC);
    `search_ads_v2` devuelve el objeto `v2` tal cual. `error` hace fallar
    cualquier llamada.
    """

    def __init__(self, v1_total=0, v2=None, error=None):
        self.v1_total = v1_total
        self.v2 = v2
        self.error = error
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        self._pending = (name, params)
        return self

    def execute(self):
        if self.error:
            raise self.error
        name, params = self._pending
        if name == "search_ads":
            start = params["p_offset"]
            stop = min(start + params["p_limit"], self.v1_total)
            data = [{**_db_row(i), "total_count": self.v1_total} for i in range(start, stop)]
        else:
            data = self.v2
        return type("Resp", (), {"data": data})()


def _v1_db(monkeypatch, db):
    from webapp import search as search_mod

    monkeypatch.setattr(search_mod, "get_supabase_admin", lambda: db)
    return db


def _v2_db(monkeypatch, db):
    from webapp import search_v2

    monkeypatch.setattr(search_v2, "get_supabase_admin", lambda: db)
    return db


def _v2_page(total, results, source_counts=None):
    return {"v": 2, "total": total, "source_counts": source_counts or {"easa": total},
            "results": results}


_V2_QUERY = f"v=2&mfr={_MFR}&models=A320"
_V2_BODY = {"v": 2, "mfr": [_MFR], "models": ["A320"]}
_EASA = {"X-Effective-Sources": "easa"}


# --------------------------------------------------------------------- v1 --
def test_v1_stream_first_page_keeps_the_search_ads_call(client, monkeypatch):
    db = _v1_db(monkeypatch, _FakeDb(v1_total=3))
    events = _sse_events(client.get("/api/search/stream?source=easa&make=Airbus&model=A320&max_results=2"))

    assert [name for name, _ in db.calls] == ["search_ads"]
    params = db.calls[0][1]
    assert params == {
        "p_source": "easa", "p_term": "", "p_make": "Airbus", "p_model": "A320",
        "p_product_type": "", "p_date_from": None, "p_date_to": None,
        "p_limit": 2, "p_offset": 0,
    }
    assert events[0]["source"] == "easa"
    assert events[0]["count"] == 2
    assert events[0]["total_matched"] == 3
    assert events[0]["truncated"] is True
    assert events[0]["results"][0]["Source"] == "EASA"
    assert events[-1] == {"event": "done", "total": 2}


def test_v1_offset_is_per_source(client, monkeypatch):
    db = _v1_db(monkeypatch, _FakeDb(v1_total=3))
    events = _sse_events(client.get("/api/search/stream?source=easa&make=Airbus&model=A320&offset=2"))
    assert db.calls[0][1]["p_offset"] == 2
    assert events[0]["count"] == 1
    assert events[0]["truncated"] is False


def test_v1_empty_page(client, monkeypatch):
    _v1_db(monkeypatch, _FakeDb(v1_total=0))
    body = client.post("/api/search", json=_FILTERED_QUERY).get_json()
    assert body == {"results": [], "count": 0, "total_matched": None, "truncated": False,
                    "truncated_sources": [], "errors": []}


def test_v1_all_queries_each_authority_once(client, monkeypatch):
    db = _v1_db(monkeypatch, _FakeDb(v1_total=1))
    events = _sse_events(client.get("/api/search/stream?source=all&make=Airbus&model=A320"))
    sources = sorted(p["p_source"] for _, p in db.calls)
    assert sources == sorted(["federal_register", "easa", "anac_brazil", "transport_canada",
                              "dgac_chile", "anac_argentina", "uk_caa"])
    assert sorted(e["source"] for e in events if "source" in e) == sources


def test_v1_database_error_is_an_empty_source_not_a_crash(client, monkeypatch):
    # El contrato v1 de siempre: search_supabase registra el error y la fuente
    # llega vacia; el stream termina en `done`.
    _v1_db(monkeypatch, _FakeDb(error=RuntimeError("boom")))
    events = _sse_events(client.get("/api/search/stream?source=easa&make=Airbus&model=A320"))
    assert events[0]["results"] == [] and events[0]["count"] == 0
    assert events[-1]["event"] == "done"


def test_v1_requests_are_counted_and_v2_are_not(client, monkeypatch):
    from webapp import search as search_mod

    _v1_db(monkeypatch, _FakeDb(v1_total=0))
    _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    before = search_mod.search_v1_requests()
    client.get("/api/search/stream?source=easa&make=Airbus&model=A320").get_data()
    client.post("/api/search", json=_FILTERED_QUERY)
    client.get(f"/api/search/stream?{_V2_QUERY}", headers=_EASA).get_data()
    client.post("/api/search", json=_V2_BODY, headers=_EASA)
    assert search_mod.search_v1_requests() == before + 2


def test_unknown_contract_version_is_rejected(client):
    assert client.get("/api/search/stream?v=3").status_code == 400
    assert client.post("/api/search", json={"v": 3}).status_code == 400


# --------------------------------------------------------------------- v2 --
def test_v2_stream_first_page(client, monkeypatch):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(5108, [_db_row(1), _db_row(2)])))
    r = client.get(f"/api/search/stream?{_V2_QUERY}&limit=2", headers=_EASA)
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    events = _sse_events(r)

    assert len(db.calls) == 1                       # una RPC por pagina
    name, params = db.calls[0]
    assert name == "search_ads_v2"
    assert params["p_limit"] == 2 and params["p_offset"] == 0
    assert params["p_filters"]["sources"] == ["easa"]
    assert params["p_filters"]["manufacturer_ids"] == [_MFR]

    page, done = events
    assert list(page) == ["v", "event", "total", "source_counts", "limit", "offset", "has_more", "results"]
    assert page["v"] == 2 and page["event"] == "page"
    assert page["total"] == 5108 and page["source_counts"] == {"easa": 5108}
    assert page["has_more"] is True
    assert page["results"][0]["AD_Number"] == "2024-0001"
    assert page["results"][0]["Source"] == "EASA"
    assert page["results"][0]["Match_Confidence"] == "high"
    assert done == {"v": 2, "event": "done", "total": 5108}


def test_v2_offset_page_is_global(client, monkeypatch):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(102, [_db_row(101), _db_row(102)])))
    page = _sse_events(client.get(f"/api/search/stream?{_V2_QUERY}&offset=100", headers=_EASA))[0]
    assert db.calls[0][1]["p_offset"] == 100
    assert page["offset"] == 100
    # 100 + 2 == 102: era la ultima pagina.
    assert page["has_more"] is False


def test_v2_empty_page_keeps_the_total(client, monkeypatch):
    _v2_db(monkeypatch, _FakeDb(v2=_v2_page(5108, [])))
    events = _sse_events(client.get(f"/api/search/stream?{_V2_QUERY}&offset=6000", headers=_EASA))
    assert events == [
        {"v": 2, "event": "page", "total": 5108, "source_counts": {"easa": 5108},
         "limit": 100, "offset": 6000, "has_more": False, "results": []},
        {"v": 2, "event": "done", "total": 5108},
    ]


def test_v2_post_returns_the_page_without_event(client, monkeypatch):
    _v2_db(monkeypatch, _FakeDb(v2=_v2_page(1, [_db_row(1)])))
    r = client.post("/api/search", json={**_V2_BODY, "offset": 0}, headers=_EASA)
    assert r.status_code == 200
    body = r.get_json()
    assert "event" not in body
    assert body["v"] == 2 and body["total"] == 1 and body["has_more"] is False
    assert body["results"][0]["AD_Number"] == "2024-0001"


def test_v2_gate_error(client, monkeypatch):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    r = client.get("/api/search/stream?v=2&keyword=fuel", headers=_EASA)
    assert r.status_code == 200                     # el SSE ya esta abierto
    error, done = _sse_events(r)
    assert error["v"] == 2 and error["event"] == "error" and error["code"] == "filter_gate"
    assert "make" in error["message"]
    assert done == {"v": 2, "event": "done", "total": 0}

    r = client.post("/api/search", json={"v": 2, "keyword": "fuel"}, headers=_EASA)
    assert r.status_code == 400
    assert r.get_json()["code"] == "filter_gate"
    # El gate corta antes de la BD.
    assert db.calls == []


def test_v2_ad_number_passes_the_gate_alone(client, monkeypatch):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(1, [_db_row(1)])))
    r = client.post("/api/search", json={"v": 2, "keyword": "AD 2023-12-03"}, headers=_EASA)
    assert r.status_code == 200
    assert db.calls[0][1]["p_filters"]["term"] == "AD 2023-12-03"


def test_v2_upstream_error(client, monkeypatch):
    _v2_db(monkeypatch, _FakeDb(error=RuntimeError("connection reset by peer")))
    r = client.get(f"/api/search/stream?{_V2_QUERY}", headers=_EASA)
    assert r.status_code == 200
    error, done = _sse_events(r)
    assert error["event"] == "error" and error["code"] == "upstream"
    assert "reset" not in error["message"]          # el detalle queda en el log
    assert done["event"] == "done"

    r = client.post("/api/search", json=_V2_BODY, headers=_EASA)
    assert r.status_code == 502
    assert r.get_json()["code"] == "upstream"


# ------------------------------------------------ v2: fuentes del proxy --
def _v2_get(client, headers):
    return client.get(f"/api/search/stream?{_V2_QUERY}", headers=headers)


def _v2_post(client, headers):
    return client.post("/api/search", json=_V2_BODY, headers=headers)


@pytest.mark.parametrize("call", [_v2_get, _v2_post], ids=["sse", "post"])
def test_v2_invalid_secret_is_401_even_with_a_valid_header(client, proxied, monkeypatch, call):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    assert call(client, {"X-Effective-Sources": "easa"}).status_code == 401
    assert call(client, {"X-Internal-Secret": "wrong", "X-Effective-Sources": "easa"}).status_code == 401
    assert db.calls == []


@pytest.mark.parametrize("call", [_v2_get, _v2_post], ids=["sse", "post"])
@pytest.mark.parametrize("header", [None, "", "narnia", "easa,narnia", "all"])
def test_v2_valid_secret_with_a_bad_header_is_400(client, proxied, monkeypatch, call, header):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    headers = {"X-Internal-Secret": proxied}
    if header is not None:
        headers["X-Effective-Sources"] = header
    assert call(client, headers).status_code == 400
    assert db.calls == []


@pytest.mark.parametrize("call", [_v2_get, _v2_post], ids=["sse", "post"])
@pytest.mark.parametrize("header, sources", [
    ("*", None),
    ("easa", ["easa"]),
    ("federal_register,easa,easa", ["easa", "federal_register"]),
])
def test_v2_header_decides_the_sources(client, proxied, monkeypatch, call, header, sources):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    r = call(client, {"X-Internal-Secret": proxied, "X-Effective-Sources": header})
    assert r.status_code == 200
    assert db.calls[0][1]["p_filters"]["sources"] == sources


def test_v2_query_sources_are_ignored(client, proxied, monkeypatch):
    db = _v2_db(monkeypatch, _FakeDb(v2=_v2_page(0, [])))
    client.get(f"/api/search/stream?{_V2_QUERY}&sources=uk_caa",
               headers={"X-Internal-Secret": proxied, "X-Effective-Sources": "easa"})
    assert db.calls[0][1]["p_filters"]["sources"] == ["easa"]


def test_v1_keeps_its_403_guard(client, proxied):
    assert client.get("/api/search/stream?source=all").status_code == 403
    assert client.post("/api/search", json={"source": "all"}).status_code == 403


# ----------------------------------------------------------------- export --

# Consulta que satisface el gate de filtros (utils.validate_search_filters):
# make + otro filtro. Los tests de truncado no prueban el gate, solo necesitan
# pasarlo.
_FILTERED_QUERY = {"source": "easa", "make": "Airbus", "model": "A320"}

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
