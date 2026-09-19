"""
Gate de filtros de busqueda (utils.validate_search_filters).

Una busqueda sin filtros barre la autoridad entera (18k ADs de EASA para alguien
que escribio "fuel"), asi que la regla es make + al menos otro filtro. La
excepcion por numero de AD no es una comodidad: pegar "2023-12-03" es el uso mas
comun de la app y nadie sabe de memoria que fabricante le corresponde.

Los dos endpoints (/api/search y /api/search/stream) tienen que aplicar la MISMA
regla: antes cada uno validaba lo suyo y no coincidian.
"""

import json

import pytest

from utils import (
    SEARCH_FILTER_HINT,
    is_ad_identifier,
    normalize_filter_values,
    validate_search_filters,
)


# ------------------------------------------------------- forma de identificador
@pytest.mark.parametrize("term", [
    "2023-12-03",          # FAA moderno
    "98-21-09",            # FAA legado (2 digitos de anio)
    "2018-23-51R1",        # FAA con revision
    "AD 2023-12-03",       # con prefijo, como lo pega el usuario del PDF
    "A.D. 95-09-02",
    "2024-0195",           # EASA moderno
    "2024-0195R1",
    "F-1998-173",          # EASA legado nacional
    "CF-2015-19",          # Transport Canada
    "CF-2015-19R1",
    "FAA-2023-0927",       # docket
    "MCAI-2023-00013-T",   # project identifier
])
def test_ad_identifiers_are_recognized(term):
    assert is_ad_identifier(term)


@pytest.mark.parametrize("term", [
    "", "   ", "fuel", "engine mount", "Boeing", "737", "2023",
    "corrosion inspection", "A320",
])
def test_free_text_is_not_an_identifier(term):
    assert not is_ad_identifier(term)


# ------------------------------------------------------------------ la regla --
@pytest.mark.parametrize("kwargs", [
    {"keyword": "2023-12-03"},                      # nº de AD suelto: pasa
    {"keyword": "AD 2023-12-03"},
    {"keyword": "FAA-2023-0927"},                   # docket suelto: pasa
    {"make": "Boeing", "model": "737"},
    {"make": "Airbus", "product_type": "Aircraft"},
    {"make": "Airbus", "date_from": "2026-01-01"},
    {"make": "Airbus", "date_to": "2026-06-30"},
    {"make": "Airbus", "keyword": "fuel"},
])
def test_accepted_combinations(kwargs):
    assert validate_search_filters(**kwargs) is None


@pytest.mark.parametrize("kwargs", [
    {},                                             # nada
    {"keyword": "fuel"},                            # texto libre solo
    {"make": "Boeing"},                             # make solo
    {"model": "737"},                               # model sin make
    {"product_type": "Engine"},
    {"date_from": "2026-01-01", "date_to": "2026-06-30"},
    {"make": "   ", "model": "737"},                # make en blanco no cuenta
])
def test_rejected_combinations(kwargs):
    msg = validate_search_filters(**kwargs)
    assert msg and "make" in msg


# --------------------------------------------------- los dos endpoints igual --
def _sse_events(response):
    text = response.get_data(as_text=True)
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_post_rejects_unfiltered_search(client):
    r = client.post("/api/search", json={"source": "easa", "keyword": "fuel"})
    assert r.status_code == 400
    assert "make" in r.get_json()["error"]


def test_post_rejects_browsing_a_whole_authority(client):
    """Barrer una autoridad entera sin filtros ya no esta permitido."""
    r = client.post("/api/search", json={"source": "easa"})
    assert r.status_code == 400


def test_stream_rejects_the_same_query_with_the_same_message(client):
    post = client.post("/api/search", json={"source": "easa", "keyword": "fuel"})
    events = _sse_events(client.get("/api/search/stream?source=easa&keyword=fuel"))

    assert post.status_code == 400
    # El stream no puede devolver 400 (ya abrio el SSE): manda el error como evento.
    assert events[0].get("error") == post.get_json()["error"]
    assert events[-1].get("event") == "done"


def test_ad_number_search_reaches_the_scrapers(client, monkeypatch):
    """Un nº de AD sin make no debe morir en el gate."""
    from webapp import search as search_mod

    monkeypatch.setattr(search_mod, "build_scrapers", lambda *a, **k: [])
    r = client.post("/api/search", json={"source": "all", "keyword": "2023-12-03"})
    assert r.status_code == 200


# ------------------------------------------------- filtros multivalor (makes) --
# Elegir tres Airbus no deja la busqueda mas abierta que elegir uno, asi que la
# regla cuenta las listas igual que los escalares.
@pytest.mark.parametrize("kwargs", [
    {"makes": ["Airbus"], "model": "A320"},
    {"makes": ["Textron", "Cessna"], "models": ["172"]},
    {"make": "Airbus", "models": ["A330", "A340"]},
    {"makes": ["Airbus"], "keyword": "fuel"},
])
def test_multi_value_accepted(kwargs):
    assert validate_search_filters(**kwargs) is None


@pytest.mark.parametrize("kwargs", [
    {"makes": ["Airbus"]},                 # fabricante solo: falta un filtro mas
    {"makes": [], "models": ["A320"]},     # modelo sin fabricante
    {"makes": ["", "   "], "model": "737"},  # entradas vacias no son fabricante
    {"models": ["A330", "A340"]},
])
def test_multi_value_rejected(kwargs):
    assert validate_search_filters(**kwargs) == SEARCH_FILTER_HINT


def test_normalize_drops_blanks_dedupes_and_caps():
    assert normalize_filter_values([" Airbus ", "airbus", "", "  ", "Boeing"]) == ["Airbus", "Boeing"]
    assert normalize_filter_values([f"m{i}" for i in range(60)]) == [f"m{i}" for i in range(40)]
    assert normalize_filter_values(None) == []
