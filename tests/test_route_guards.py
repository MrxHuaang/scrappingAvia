"""
Ninguna ruta de la app entrega datos sin el secreto del proxy.

El unico cliente legitimo es el proxy de Next.js (Ad-Scrapper), que firma
cada request con X-Internal-Secret despues de aplicar sesion, plan y cuota.
Una ruta que conteste sin el secreto se salta todo eso. Estos tests recorren
`app.url_map` entero, asi que una ruta nueva sin guard los pone en rojo sin
que nadie tenga que acordarse de anadirla aqui.

La unica excepcion es el health check, que no devuelve datos de ADs.
v2 rechaza con 401 y v1 conserva su 403 (test_v1_keeps_its_403_guard): los
dos son rechazo, y ninguno trae mas que un mensaje de error.

Sin secreto configurado el guard falla cerrado: 503 en toda ruta de datos,
salvo el opt-in ALLOW_UNAUTHENTICATED_LOCAL=1, que solo cuenta fuera de
Vercel y para requests de loopback.
"""

import logging
import re

import pytest

import app as app_module
from webapp import core

# Rutas publicas a proposito: health checks sin datos.
PUBLIC_RULES = {"/api/health"}

_REJECTED = {401, 403}

# Las rutas de busqueda despachan por version del contrato; se prueban todas.
_QUERY_VARIANTS = ("", "?v=2", "?v=1", "?v=99")
_BODY_VARIANTS = ({}, {"v": 2}, {"v": 1}, {"v": 99})


def _rules():
    return sorted(app_module.app.url_map.iter_rules(), key=lambda r: r.rule)


def _requests():
    """(rule, metodo, path, kwargs) por cada ruta no publica y variante."""
    for rule in _rules():
        if rule.rule in PUBLIC_RULES:
            continue
        path = re.sub(r"<[^>]+>", "x", rule.rule)
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            if method == "GET":
                for q in _QUERY_VARIANTS:
                    yield rule.rule, method, path + q, {}
            else:
                for body in _BODY_VARIANTS:
                    yield rule.rule, method, path, {"json": body}


_CASES = list(_requests())
_CASE_IDS = [f"{m} {p}{' ' + str(k['json']) if 'json' in k else ''}" for _, m, p, k in _CASES]


def test_the_app_exposes_only_the_search_api_and_health():
    assert {r.rule for r in _rules()} == {"/api/health", "/api/search", "/api/search/stream"}


def test_there_is_something_to_check():
    # Si la enumeracion quedara vacia, el test de abajo pasaria sin probar nada.
    assert _CASES


@pytest.mark.parametrize("header", [None, "wrong-secret"], ids=["no-secret", "bad-secret"])
@pytest.mark.parametrize(
    "rule,method,path,kwargs", _CASES, ids=_CASE_IDS,
)
def test_every_data_route_requires_the_proxy_secret(client, proxied, header, rule, method, path, kwargs):
    headers = {"X-Internal-Secret": header} if header else {}
    # Una cabecera de fuentes valida no abre nada sin el secreto.
    headers["X-Effective-Sources"] = "*"
    r = client.open(path, method=method, headers=headers, **kwargs)
    assert r.status_code in _REJECTED, f"{method} {path} answered {r.status_code} without the proxy secret"
    assert r.get_json() is not None and set(r.get_json()) == {"error"}


def test_the_correct_secret_passes_the_guard(client, proxied):
    # Contraprueba: el rechazo de arriba lo pone el guard, no una ruta rota.
    r = client.post("/api/search", json={"source": "all"}, headers={"X-Internal-Secret": proxied})
    assert r.status_code == 400


def test_health_is_public_and_carries_no_ad_data(client, proxied):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert set(r.get_json()) == {"status", "version", "sources", "timestamp"}


# ------------------------------------------- secreto sin configurar: 503 --


@pytest.mark.parametrize("rule,method,path,kwargs", _CASES, ids=_CASE_IDS)
def test_unset_secret_fails_closed_on_every_data_route(strict_client, rule, method, path, kwargs):
    r = strict_client.open(path, method=method, headers={"X-Effective-Sources": "*"}, **kwargs)
    assert r.status_code == 503, f"{method} {path} answered {r.status_code} with no proxy secret configured"
    assert r.get_json() == {"error": "Service unavailable"}


def test_unset_secret_still_serves_health(strict_client):
    assert strict_client.get("/api/health").status_code == 200


def test_opt_in_on_loopback_reaches_the_routes(client):
    # v1 y v2 llegan a la validacion (400), no al guard.
    assert client.post("/api/search", json={"source": "all"}).status_code == 400
    r = client.post("/api/search", json={"v": 2}, headers={"X-Effective-Sources": "*"})
    assert r.status_code == 400
    assert r.get_json()["code"] == "filter_gate"


@pytest.mark.parametrize("environ,headers", [
    ({"REMOTE_ADDR": "203.0.113.5"}, {}),
    # X-Forwarded-For falso desde fuera: el peer real no es loopback.
    ({"REMOTE_ADDR": "203.0.113.5"}, {"X-Forwarded-For": "127.0.0.1"}),
    # Un proxy local que reenvia trafico externo: el cliente real no es loopback.
    ({"REMOTE_ADDR": "127.0.0.1"}, {"X-Forwarded-For": "203.0.113.5"}),
], ids=["remote", "spoofed-forwarded-for", "local-proxy-for-remote"])
def test_opt_in_does_not_count_off_loopback(client, environ, headers):
    for path, kw in (("/api/search", {"method": "POST", "json": {"source": "all"}}),
                     ("/api/search?v=2", {"method": "POST", "json": {"v": 2}}),
                     ("/api/search/stream?source=all", {"method": "GET"})):
        r = client.open(path, environ_base=environ, headers={**headers, "X-Effective-Sources": "*"}, **kw)
        assert r.status_code == 503, path


def test_opt_in_does_not_count_on_vercel(client, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    assert client.post("/api/search", json={"source": "all"}).status_code == 503
    assert client.get("/api/search/stream?v=2").status_code == 503


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_opt_in_needs_exactly_1(client, monkeypatch, value):
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL", value)
    assert client.post("/api/search", json={"source": "all"}).status_code == 503


def test_a_configured_secret_ignores_the_opt_in(client, proxied):
    # `client` trae el opt-in y llega desde loopback: con secreto, no abre nada.
    assert client.post("/api/search", json={"source": "all"}).status_code == 403
    assert client.post("/api/search", json={"v": 2}).status_code == 401


# ----------------------------------------------------------- aviso inicial --
def test_startup_logs_an_error_without_secret_or_opt_in(monkeypatch, caplog):
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_LOCAL", raising=False)
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        core.log_proxy_guard_state()
    [rec] = caplog.records
    assert rec.levelno == logging.ERROR and "503" in rec.getMessage()


def test_startup_warns_when_running_on_the_opt_in(monkeypatch, caplog):
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL", "1")
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        core.log_proxy_guard_state()
    [rec] = caplog.records
    assert rec.levelno == logging.WARNING and "loopback" in rec.getMessage()


def test_startup_is_quiet_with_a_secret(proxied, caplog):
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        core.log_proxy_guard_state()
    assert caplog.records == []
