"""compliance_time y AMOC de un AD FAA desde sus secciones clasificadas.

Con granulos reales de `fixtures/fr_text/`: el valor sale de las secciones
`compliance`/`required_actions` (plazo) y `amoc`/`required_actions` (AMOC)
del bloque anclado al AD, nunca del documento FR entero, cuyo preámbulo
discute plazos y AMOCs de otros ADs. La tabla fija la medición sobre los
fixtures (docs/fr-text.md).
"""

import json
import os

import pytest

import enrich
from scrapers import federal_register as fr

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_DIR, "fr_text.json"), encoding="utf-8") as _f:
    FIXTURES = {e["file"]: e for e in json.load(_f)}


def _split(file: str) -> dict:
    e = FIXTURES[file]
    with open(os.path.join(_DIR, "fr_text", file), encoding="utf-8") as f:
        return enrich.split_ad_sections(f.read(), e.get("expected_ad"), e.get("expected_amendment"))


# archivo -> (compliance_time, amoc) con la regla de las secciones.
EXPECTED = {
    "2024-08105.htm": (None, True),   # plazos en el boletín de servicio, no en el AD
    "2024-00993.htm": (None, True),   # "further flight is prohibited until ...": sin frase
    "2015-23117.htm": ("Within 30 days after the effective date of this AD", True),
    "2015-06235.htm": ("Within 30 days", True),
    "05-17333.htm": ("Within 14 days", True),
    "05-2578.htm": ("within 1,800 flight cycles", True),
    "98-4716.htm": ("Within 5 flight cycles", True),
    "01-917.htm": ("Within 50 flight hours after the effective date of this AD", True),
    "2017-25253.htm": (None, False),  # rescisión: ni plazo ni AMOC
    "2024-13013.htm": ("Within 48 months", True),
}


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_compliance_time_and_amoc_from_the_sections(file):
    split = _split(file)
    assert split["status"] == "extracted"
    got = (enrich.compliance_time_from_sections(split["sections"]),
           enrich.amoc_from_sections(split["sections"]))
    assert got == EXPECTED[file]


def test_the_boilerplate_compliance_sentence_is_not_a_compliance_time():
    sections = [{"kind": "compliance",
                 "body": "Comply with this AD within the compliance times specified, unless already done."}]
    assert enrich.compliance_time_from_sections(sections) is None


def test_compliance_time_follows_document_order_over_both_kinds():
    sections = [
        {"kind": "unsafe_condition", "body": "Within 10 days of the report, cracks were found."},
        {"kind": "compliance", "body": "Required as indicated, unless accomplished previously."},
        {"kind": "required_actions", "body": "Within 600 flight hours time-in-service, inspect."},
        {"kind": "required_actions", "body": "Before further flight, replace."},
    ]
    assert enrich.compliance_time_from_sections(sections) == "Within 600 flight hours time-in-service"


def test_amoc_named_inside_the_required_actions_counts():
    """Legado: "(e) An alternative method of compliance ..." cae en un párrafo
    de acciones requeridas cuando el tipo no lo separa."""
    sections = [{"kind": "required_actions",
                 "body": "(c) An alternative method of compliance may be used if approved."}]
    assert enrich.amoc_from_sections(sections) is True
    assert enrich.amoc_from_sections([{"kind": "other", "body": "alternative method of compliance"}]) is False
    assert enrich.amoc_from_sections(None) is False


def _parse(file: str, entry: dict, monkeypatch) -> dict:
    e = FIXTURES[file]
    monkeypatch.setattr(fr, "_fulltext_cache", {e["document_number"]: entry})
    monkeypatch.setattr(fr, "_ft_attempted", set())
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    return fr._parse_document({
        "title": e["title"], "abstract": e.get("abstract") or "", "document_number": e["document_number"],
        "publication_date": e["publication_date"], "type": "Rule", "docket_ids": e["docket_ids"],
    })


def test_the_fr_record_takes_both_fields_from_the_anchored_sections(monkeypatch):
    split = _split("2015-23117.htm")
    rec = _parse("2015-23117.htm", {"v": 2, **split}, monkeypatch)
    assert rec["Compliance_Time"] == "Within 30 days after the effective date of this AD"
    assert rec["AMOC_Available"] is True


@pytest.mark.parametrize("entry", [
    # Sin texto anclado: el preámbulo de otro AD no aporta nada.
    {"v": 2, "status": "unanchored", "anchor": None, "sections": []},
    {"v": 2, "status": "error", "anchor": None, "sections": []},
    # Una entrada v1 guardaba valores del documento entero: ya no se leen.
    {"compliance_time": "within 30 days", "amoc": True},
])
def test_without_anchored_text_nothing_comes_from_the_body(entry, monkeypatch):
    rec = _parse("E7-1708.htm", entry, monkeypatch)
    assert "Compliance_Time" not in rec
    assert rec["AMOC_Available"] is False
