"""
Pares (make, model) que van a `ad_applicability`.

El defecto que estos tests fijan: EASA lista N approval holders por AD y el
scraper los unia en un string (`", ".join`), que despues se volvia a partir por
coma. Dos consecuencias, las dos verificadas contra produccion:

  * makes de hasta 494 chars como opcion del filtro Make ("AEROSTAR AIRCRAFT
    CORPORATION, ANDERSON CLINTON, BOTICH, DAVID A., ...") — y los nombres traen
    comas propias ("REVO, Inc."), asi que re-partir no es reversible;
  * cada make cruzado con TODOS los modelos del AD, incluidos los de otros
    holders — combinaciones que el AD nunca declaro.
"""

from populate_db import _applicability_pairs


def test_structured_pairs_keep_each_holder_with_its_own_models():
    raw = {"Applicability": [
        {"make": "AIRBUS HELICOPTERS", "models": ["AS 350 B", "AS 350 BA"]},
        {"make": "SIKORSKY", "models": ["S-76"]},
    ]}
    assert _applicability_pairs(raw, "AIRBUS HELICOPTERS", "AS 350 B, AS 350 BA, S-76") == [
        ("AIRBUS HELICOPTERS", "AS 350 B"),
        ("AIRBUS HELICOPTERS", "AS 350 BA"),
        ("SIKORSKY", "S-76"),
    ]


def test_holder_without_models_still_gets_a_row():
    raw = {"Applicability": [{"make": "KAMOV", "models": []}]}
    assert _applicability_pairs(raw, "KAMOV", "") == [("KAMOV", None)]


def test_commas_inside_a_holder_name_survive():
    """El caso que hacia imposible el re-split: la coma es parte del nombre."""
    raw = {"Applicability": [
        {"make": "REVO, Inc.", "models": ["Lake LA-4"]},
        {"make": "McDermott 214 Holdings, LLC", "models": []},
    ]}
    assert _applicability_pairs(raw, "REVO, Inc.", "Lake LA-4") == [
        ("REVO, Inc.", "Lake LA-4"),
        ("McDermott 214 Holdings, LLC", None),
    ]


def test_blank_and_malformed_entries_are_skipped():
    raw = {"Applicability": [
        {"make": "  ", "models": ["X"]},
        "no soy un dict",
        {"make": "BOEING", "models": ["737-800", "  ", None]},
    ]}
    assert _applicability_pairs(raw, "BOEING", "737-800") == [("BOEING", "737-800")]


def test_legacy_path_splits_models_when_there_is_no_structure():
    """FAA/TC/ANAC: un make escalar, modelos separados por coma."""
    assert _applicability_pairs({}, "Boeing", "737-700, 737-800") == [
        ("Boeing", "737-700"),
        ("Boeing", "737-800"),
    ]


def test_legacy_make_without_model_is_medium_confidence_row():
    assert _applicability_pairs({}, "Cessna", "") == [("Cessna", None)]


def test_no_make_no_rows():
    assert _applicability_pairs({}, "", "737") == []


def test_empty_structure_falls_back_to_the_scalar_fields():
    assert _applicability_pairs({"Applicability": []}, "Embraer", "ERJ-190") == [
        ("Embraer", "ERJ-190"),
    ]
