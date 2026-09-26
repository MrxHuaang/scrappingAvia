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
"""

import re

import pytest

import app as app_module

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


def test_the_app_exposes_only_the_search_api_and_health():
    assert {r.rule for r in _rules()} == {"/api/health", "/api/search", "/api/search/stream"}


def test_there_is_something_to_check():
    # Si la enumeracion quedara vacia, el test de abajo pasaria sin probar nada.
    assert _CASES


@pytest.mark.parametrize("header", [None, "wrong-secret"], ids=["no-secret", "bad-secret"])
@pytest.mark.parametrize(
    "rule,method,path,kwargs", _CASES,
    ids=[f"{m} {p}{' ' + str(k['json']) if 'json' in k else ''}" for _, m, p, k in _CASES],
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
