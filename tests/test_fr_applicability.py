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

import enrich
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


def test_a_part_number_is_never_part_of_the_holder():
    """E6-14636 daba el titular "Goodyear Aviation Tires, Part": el número de
    parte se consumía como modelo y arrastraba "Part". El abstract no nombra a
    Goodyear, así que sin titular inequívoco el AD viaja `failed`."""
    assert extract_applicability(**_doc_args("E6-14636")).holder is None
    assert extract_applicability(**_doc_args("98-25124")).holder == "SAFT America Inc."


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


# ------------------------------------------------------------- sección Applicability

with open(os.path.join(os.path.dirname(__file__), "fixtures", "fr_text.json"), encoding="utf-8") as _f:
    TEXT_DOCS = {d["document_number"]: d for d in json.load(_f) if d.get("document_number")}


def _text_split(number: str) -> dict:
    d = TEXT_DOCS[number]
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "fr_text", d["file"]), encoding="utf-8") as f:
        return enrich.split_ad_sections(f.read(), d["expected_ad"], d["expected_amendment"])


def _applicability_body(number: str) -> str:
    return next(s["body"] for s in _text_split(number)["sections"] if s["kind"] == "applicability")


def test_models_come_from_the_applicability_section_before_the_abstract():
    """2024-13013: el abstract resume la applicability de los ADs que reemplaza
    (A330 y A340); la sección Applicability del rule solo lista A330."""
    d = TEXT_DOCS["2024-13013"]
    from_abstract = extract_applicability(d["title"], d["abstract"])
    assert "A340-200" in from_abstract.models
    r = extract_applicability(d["title"], d["abstract"], _applicability_body("2024-13013"))
    assert r.holder == from_abstract.holder == "Airbus SAS"
    assert r.models[:3] == ("A330-201", "-202", "-203")
    assert r.models[-1] == "-941"
    assert not any("A340" in m for m in r.models)


def test_section_that_does_not_name_the_holder():
    """Sección legada: "Model 737-300, -400, and -500 series airplanes ..."."""
    title = "Airworthiness Directives; The Boeing Company Airplanes"
    r = extract_applicability(title, "", _applicability_body("98-4716"))
    assert (r.holder, r.models) == ("The Boeing Company", ("737-300", "-400", "-500 series"))
    r = extract_applicability(title, "", "(c) This AD applies to Model 737-8 airplanes.")
    assert r.models == ("737-8",)


def test_section_without_models_falls_back_to_the_abstract():
    r = extract_applicability(**_doc_args("2024-08105"),
                              applicability_text="This AD applies to the airplanes identified in Table 1 of this AD.")
    assert r.models == ("737-8", "737-9")


def test_title_models_still_win_over_the_section():
    """Un título legado que enumera modelos no cambia por la sección."""
    d = TEXT_DOCS["98-4716"]
    assert (extract_applicability(d["title"], d["abstract"], _applicability_body("98-4716")).models
            == extract_applicability(d["title"], d["abstract"]).models)


def test_federal_register_takes_models_from_the_anchored_section(monkeypatch):
    d = TEXT_DOCS["2024-13013"]
    split = _text_split("2024-13013")
    monkeypatch.setattr(federal_register, "_fetch_fulltext_fields",
                        lambda *a, **k: {"v": 2, **split})
    rec = federal_register._parse_document({
        "title": d["title"], "abstract": d["abstract"], "document_number": "2024-13013",
        "publication_date": d["publication_date"], "type": "Rule", "docket_ids": d["docket_ids"],
    })
    assert rec["Make"] == "Airbus SAS"
    assert rec["Model"].startswith("A330-201, -202") and "A340" not in rec["Model"]
    assert rec["Applicability"] == [{"make": "Airbus SAS", "models": rec["Model"].split(", ")}]


def test_an_unanchored_text_is_not_a_models_source(monkeypatch):
    d = TEXT_DOCS["2024-13013"]
    split = _text_split("2024-13013")
    monkeypatch.setattr(federal_register, "_fetch_fulltext_fields",
                        lambda *a, **k: {"v": 2, **split, "status": "unanchored"})
    rec = federal_register._parse_document({
        "title": d["title"], "abstract": d["abstract"], "document_number": "2024-13013",
        "publication_date": d["publication_date"], "type": "Rule", "docket_ids": d["docket_ids"],
    })
    assert "A340-200" in rec["Model"]  # el abstract, como antes
