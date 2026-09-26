"""Titular y modelos de los ADs de la FAA (scrapers/common/fr_applicability.py).

Los casos de `fixtures/fr_applicability.json` son documentos REALES de la FR
API (título y abstract tal cual los devuelve
`federalregister.gov/api/v1/documents.json`, identificados por
`document_number`): aviones, helicópteros, motores, hélices, appliances,
"Type Certificate Previously Held by", listas con subseries, "Various
Aircraft" y títulos modernos cuyos modelos solo están en el abstract.
"""

import json
import os

import pytest

import populate_db
from scrapers import faa_govinfo, federal_register
from scrapers.common.fr_applicability import applicability_fields, extract_applicability

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "fr_applicability.json")
with open(_FIXTURES, encoding="utf-8") as _f:
    REAL_DOCS = json.load(_f)


def _by_doc(number: str) -> dict:
    return next(d for d in REAL_DOCS if d["document_number"] == number)


def test_there_are_enough_real_fixtures():
    assert len(REAL_DOCS) >= 20


@pytest.mark.parametrize("doc", REAL_DOCS, ids=[d["document_number"] for d in REAL_DOCS])
def test_real_fr_documents(doc):
    result = extract_applicability(doc["title"], doc["abstract"])
    expected = doc["expected"]
    assert result.holder == expected["holder"]
    assert list(result.models) == expected["models"]
    assert list(result.previously_held_by) == expected["previously_held_by"]


# ------------------------------------------------------------- casos que importan

def test_modern_title_takes_the_full_model_list_from_the_abstract():
    """"The Boeing Company Airplanes" no dice qué modelos: el abstract sí, y la
    lista entera viaja — la vieja extracción se quedaba con "737-8 and"."""
    r = extract_applicability(**_doc_args("2024-08105"))
    assert (r.holder, r.models) == ("The Boeing Company", ("737-8", "737-9"))


def test_subseries_are_kept_as_the_source_writes_them():
    """La base re-une "-700" con su tallo (ad_model_tokens); aquí no se inventa."""
    r = extract_applicability(**_doc_args("2015-23117"))
    assert r.models == ("737-600", "-700", "-700C", "-800", "-900", "-900ER series")


def test_type_certificate_previously_held_by_keeps_the_current_holder():
    doc = next(d for d in REAL_DOCS if "Textron Aviation Inc. (Type Certificate" in d["title"])
    r = extract_applicability(doc["title"], doc["abstract"])
    assert r.holder == "Textron Aviation Inc."
    assert r.previously_held_by == ("Cessna Aircraft Company",)
    item = r.raw_applicability()[0]
    assert item["make"] == "Textron Aviation Inc."
    assert item["previously_held_by"] == ["Cessna Aircraft Company"]


@pytest.mark.parametrize("title", [
    "Airworthiness Directives; Various Helicopters",
    "Airworthiness Directives; Various Aircraft Equipped With Wing Lift Struts",
    "Airworthiness Directives; Bell Textron Inc. and Various Restricted Category Helicopters",
    "Airworthiness Directives; Textron Lycoming and Teledyne Continental Motors Reciprocating Engines",
])
def test_no_unambiguous_holder_emits_no_applicability(title):
    """Declarar uno solo de varios titulares sería un subconjunto parcial: sin
    Applicability ni Make, el AD viaja `failed` y sus filas guardadas quedan."""
    fields = applicability_fields(title, "")
    assert "Applicability" not in fields
    assert fields["Make"] == ""
    assert populate_db._applicability_pairs({"make": None, "raw_data": fields}) == []


def test_holder_without_model_is_unknown_not_all_models():
    fields = applicability_fields("Airworthiness Directives; The Boeing Company Airplanes", "")
    assert fields["Applicability"] == [{"make": "The Boeing Company", "models": []}]
    assert fields["Model"] == ""
    # (make, None) -> model_status 'unknown' en populate_relations.
    assert populate_db._applicability_pairs({"raw_data": fields}) == [("The Boeing Company", None)]


def test_models_from_abstract_are_not_borrowed_from_another_holder():
    """Un appliance "instalado en Airbus Model A330" no es un AD de Airbus."""
    doc = next(d for d in REAL_DOCS if d["title"].startswith("Airworthiness Directives; Kidde"))
    r = extract_applicability(doc["title"], doc["abstract"])
    assert (r.holder, r.models) == ("Kidde Graviner", ())
    title = ("Airworthiness Directives; Goodrich Evacuation Systems Approved Under Technical "
             "Standard Order (TSO) TSO-C69b and Installed on Airbus Model A330-200 Airplanes")
    abstract = ("The FAA is adopting a new airworthiness directive (AD) for certain Goodrich "
                "evacuation systems installed on Airbus Model A330-200 airplanes.")
    r = extract_applicability(title, abstract)
    assert r.holder == "Goodrich"  # el prefijo que el abstract usa como nombre
    assert r.models == ()


def test_scalars_come_from_the_same_extraction():
    doc = _by_doc("2024-08105")
    fields = applicability_fields(doc["title"], doc["abstract"])
    assert fields["Make"] == fields["Applicability"][0]["make"]
    assert fields["Model"] == ", ".join(fields["Applicability"][0]["models"])


# ------------------------------------------------------------- scrapers FAA

def test_federal_register_record_carries_applicability(monkeypatch):
    monkeypatch.setattr(federal_register, "FETCH_FULL_TEXT", False)
    doc = _by_doc("2024-08105")
    rec = federal_register._parse_document({
        "title": doc["title"], "abstract": doc["abstract"],
        "document_number": doc["document_number"], "publication_date": doc["publication_date"],
        "type": "Rule", "docket_ids": ["Docket No. FAA-2023-2366", "AD 2024-07-11"],
    })
    assert rec["AD_Number"] == "2024-07-11"
    assert rec["Make"] == "The Boeing Company"
    assert rec["Model"] == "737-8, 737-9"
    row = {"make": rec["Make"], "model": rec["Model"], "raw_data": rec}
    assert populate_db._applicability_pairs(row) == [("The Boeing Company", "737-8, 737-9")]


def test_govinfo_uses_the_same_extraction_from_the_title():
    g = {"title": "Airworthiness Directives; Pratt & Whitney PW4074, PW4077, and PW4098 "
                  "Turbofan Engines",
         "granuleId": "E6-18368", "dateIssued": "2006-10-31"}
    rec = faa_govinfo._parse_granule(g, "FR-2006-10-31", {"ad": "AD 2006-22-05"})
    assert rec["Applicability"] == [{"make": "Pratt & Whitney",
                                     "models": ["PW4074", "PW4077", "PW4098"]}]
    assert rec["Make"] == "Pratt & Whitney"


def _doc_args(number: str) -> dict:
    d = _by_doc(number)
    return {"title": d["title"], "abstract": d["abstract"]}
