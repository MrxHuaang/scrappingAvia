"""Identidad del AD desde `docket_ids` (utils.ad_identity_from_docket_ids).

Los casos con prefijo son los de la FR API tal cual: en los 13570 documentos
RULE de 1994-2026, "AD-<número>" aparece en 3 (00-27166, 05-17333,
2025-03987) y "AD No. <número>" en 1 (2020-20439). Antes quedaban sin AD y el
documento se descartaba.
"""

import json
import os

import pytest

import enrich
from scrapers import federal_register
from utils import ad_identity_from_docket_ids

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.parametrize("docket_ids,ad", [
    (["Docket No. 98-ANE-61-AD", "Amendment 39-11941", "AD-2000-21-09"], "2000-21-09"),
    (["Docket No. FAA-2005-21599", "Directorate Identifier 2005-NM-036-AD", "Amendment 39-14246",
      "AD-2005-18-06"], "2005-18-06"),
    (["Docket No. FAA-2019-0560", "Product Identifier 2018-CE-056-AD", "Amendment 39-21255",
      "AD No. 2020-19-12"], "2020-19-12"),
    (["Docket No. FAA-2024-2547", "Project Identifier AD-2024-00334-E", "Amendment 39-22987",
      "AD-2025-05-15"], "2025-05-15"),
])
def test_prefixed_ad_numbers_are_recovered(docket_ids, ad):
    assert ad_identity_from_docket_ids(docket_ids)["ad"] == ad


@pytest.mark.parametrize("docket_ids", [
    # Un Project Identifier "AD-..." no es un número de AD.
    ["Docket No. FAA-2024-2547", "Project Identifier AD-2024-00334-E", "Amendment 39-22987"],
    ["Docket No. FAA-2005-21599", "Directorate Identifier 2005-NM-036-AD", "Amendment 39-14246"],
    # Números mal formados en la fuente: no se reconstruyen.
    ["Docket No. 99-CE-51-AD", "Amendment 39-11548", "AD 2000-0230"],
    ["Docket No. 2000-CE-70-AD", "Amendment 39-12152", "AD 200106-05"],
    # Una corrección de tres ADs no tiene un AD propio.
    ["Amendments 39-15325, 39-15326, 39-15327", "ADs 2008-01-02, 2004-07-22 R1, and 90-25-05 R1"],
])
def test_entries_that_are_not_one_ad_number_stay_empty(docket_ids):
    assert ad_identity_from_docket_ids(docket_ids)["ad"] is None


def test_prefix_does_not_change_the_other_fields():
    assert ad_identity_from_docket_ids(
        ["Docket No. FAA-2024-2547", "Project Identifier AD-2024-00334-E", "Amendment 39-22987", "AD-2025-05-15"]
    ) == {"ad": "2025-05-15", "docket": "FAA-2024-2547", "amendment": "39-22987", "project": "AD-2024-00334-E"}


def _fixture_05_17333() -> tuple[dict, str]:
    with open(os.path.join(_DIR, "fr_text.json"), encoding="utf-8") as f:
        fx = next(d for d in json.load(f) if d.get("document_number") == "05-17333")
    with open(os.path.join(_DIR, "fr_text", fx["file"]), encoding="utf-8") as f:
        return fx, f.read()


def test_05_17333_is_no_longer_discarded(monkeypatch):
    monkeypatch.setattr(federal_register, "FETCH_FULL_TEXT", False)
    fx, _ = _fixture_05_17333()
    rec = federal_register._parse_document({
        "title": fx["title"], "abstract": fx["abstract"], "document_number": "05-17333",
        "publication_date": fx["publication_date"], "type": "Rule", "docket_ids": fx["docket_ids"],
    })
    assert rec is not None
    assert (rec["AD_Number"], rec["Amendment"], rec["Docket"]) == ("2005-18-06", "39-14246", "FAA-2005-21599")


def test_05_17333_text_anchors_with_the_identity_from_docket_ids():
    fx, html = _fixture_05_17333()
    ident = ad_identity_from_docket_ids(fx["docket_ids"])
    r = enrich.split_ad_sections(html, ident["ad"], ident["amendment"])
    assert r["status"] == "extracted"
    assert r["anchor"].startswith("2005-18-06 Bombardier, Inc. (Formerly Canadair): Amendment 39-14246.")
