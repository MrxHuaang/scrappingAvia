"""Texto de un rule FAA en secciones (enrich.split_ad_sections).

Los granulos de `fixtures/fr_text/` son documentos REALES de GovInfo
(`govinfo.gov/content/pkg/FR-<fecha>/html/<doc>.htm`, el mismo HTML que
devuelve `api.govinfo.gov/.../granules/<doc>/htm`), recortados: se conserva
la cabecera hasta el SUMMARY (con el corchete de identidad) y el texto
regulatorio desde "PART 39--AIRWORTHINESS DIRECTIVES". `fixtures/fr_text.json`
dice de dónde sale cada uno y qué caso cubre. El único compuesto (dos ADs en
un documento) une dos granulos reales y lo declara en su nota.
"""

import json
import os

import pytest

import enrich

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_DIR, "fr_text.json"), encoding="utf-8") as _f:
    FIXTURES = {f["file"]: f for f in json.load(_f)}


def _html(name: str) -> str:
    with open(os.path.join(_DIR, "fr_text", name), encoding="utf-8") as f:
        return f.read()


def _split(doc: str, ad: str | None = None, amendment: str | None = None) -> dict:
    fx = FIXTURES[f"{doc}.htm"]
    return enrich.split_ad_sections(_html(fx["file"]), ad or fx["expected_ad"],
                                    amendment or fx["expected_amendment"])


def _kinds(result: dict) -> list[tuple[str | None, str]]:
    return [(s["letter"], s["kind"]) for s in result["sections"]]


def _by_kind(result: dict, kind: str) -> list[dict]:
    return [s for s in result["sections"] if s["kind"] == kind]


MODERN = [
    ("a", "effective_date"), ("b", "affected_ads"), ("c", "applicability"), ("d", "subject"),
    ("e", "unsafe_condition"), ("f", "compliance"), ("g", "required_actions"), ("h", "other"),
    ("i", "amoc"), ("j", "related_information"), ("k", "material"),
]


# ------------------------------------------------------------- por formato

def test_2024_rule_sections_by_letter_and_kind():
    r = _split("2024-08105")
    assert r["status"] == "extracted"
    assert r["anchor"].startswith("2024-06-03 The Boeing Company: Amendment 39-22708;")
    assert _kinds(r) == MODERN
    assert [s["position"] for s in r["sections"]] == list(range(1, 12))
    applicability = _by_kind(r, "applicability")[0]
    assert applicability["heading"] == "Applicability"
    assert applicability["body"].startswith("This AD applies to The Boeing Company Model 737-8 and 737-9")


def test_amoc_is_found_by_its_heading_not_by_its_letter():
    """El AMOC va en (i) en 2024-08105 y en (g) "Other FAA AD Provisions" en
    2015-06235: ninguno en (h)."""
    assert [s["letter"] for s in _by_kind(_split("2024-08105"), "amoc")] == ["i"]
    mcai = _split("2015-06235")
    assert [(s["letter"], s["heading"]) for s in _by_kind(mcai, "amoc")] == [("g", "Other FAA AD Provisions")]


def test_2015_mcai_reason_and_actions_and_compliance():
    r = _split("2015-06235")
    assert _kinds(r) == [
        ("a", "effective_date"), ("b", "affected_ads"), ("c", "applicability"), ("d", "subject"),
        ("e", "unsafe_condition"), ("f", "required_actions"), ("g", "amoc"),
        ("h", "related_information"), ("i", "material"),
    ]
    assert _by_kind(r, "unsafe_condition")[0]["heading"] == "Reason"
    assert _by_kind(r, "required_actions")[0]["heading"] == "Actions and Compliance"


def test_emergency_ad():
    r = _split("2024-00993")
    assert r["status"] == "extracted"
    assert _kinds(r) == MODERN
    effective = _by_kind(r, "effective_date")[0]["body"]
    assert "Emergency Airworthiness Directive (AD) 2024-02-51" in effective


def test_multi_model_applicability_is_one_section():
    r = _split("2015-23117")
    body = _by_kind(r, "applicability")[0]["body"]
    assert body.startswith("This AD applies to all The Boeing Company Model 737-600, -700, -700C, -800, -900")


def test_2005_plain_headings_group_their_lettered_paragraphs():
    r = _split("05-17333")
    assert r["status"] == "extracted"
    assert _kinds(r)[:5] == [("a", "effective_date"), ("b", "affected_ads"), ("c", "applicability"),
                             ("d", "unsafe_condition"), ("e", "compliance")]
    assert _by_kind(r, "amoc") and _by_kind(r, "material")
    unsafe = _by_kind(r, "unsafe_condition")[0]
    assert (unsafe["heading"], unsafe["body"][:11]) == ("Unsafe Condition", "(d) This AD")


def test_1998_legacy_labels_and_lettered_paragraphs():
    r = _split("98-4716")
    assert r["status"] == "extracted"
    assert _kinds(r) == [
        (None, "applicability"), (None, "compliance"), (None, "unsafe_condition"),
        ("a", "required_actions"), ("b", "required_actions"), ("c", "required_actions"),
        ("d", "required_actions"), ("e", "amoc"), ("f", "other"), ("g", "effective_date"),
    ]
    assert _by_kind(r, "applicability")[0]["body"].startswith("Model 737-300, -400, and -500 series airplanes")
    assert _by_kind(r, "unsafe_condition")[0]["body"].startswith("To prevent reduced structural integrity")
    # Notas y subpárrafos van con su sección, no como secciones propias.
    assert "Note 1:" in _by_kind(r, "applicability")[0]["body"]
    assert "(1) Perform a general visual inspection" in r["sections"][3]["body"]


def test_legacy_unsafe_condition_inside_the_compliance_paragraph():
    r = _split("01-917")
    compliance, unsafe = r["sections"][1], r["sections"][2]
    assert compliance["kind"] == "compliance"
    assert compliance["body"] == "Compliance with this AD is required as indicated, unless already done."
    assert unsafe["kind"] == "unsafe_condition"
    assert unsafe["body"].startswith("To prevent defective oil filter differential pressure switches")


def test_page_markers_do_not_cut_a_section():
    body = _by_kind(_split("2024-08105"), "amoc")[0]["body"]
    assert "[[Page" not in body


# ------------------------------------------------------------- anclaje

def test_header_with_a_misprinted_ad_number_anchors_through_the_preamble():
    """05-2578: el encabezado dice "2005-NM-03-11"; el corchete del preámbulo
    empareja su amendment (39-13967) con AD 2005-03-11."""
    r = _split("05-2578")
    assert r["status"] == "extracted"
    assert r["anchor"].startswith("2005-NM-03-11 Boeing: Amendment 39-13967.")


def test_header_with_a_different_amendment_is_unanchored():
    """E7-1708: el encabezado declara "Amendment 39-149824" y docket_ids dice
    39-14924. Sin amendment esperado el mismo encabezado sí ancla."""
    assert _split("E7-1708") == {"status": "unanchored", "anchor": None, "sections": []}
    fx = FIXTURES["E7-1708.htm"]
    assert enrich.split_ad_sections(_html(fx["file"]), fx["expected_ad"], None)["status"] == "extracted"


@pytest.mark.parametrize("ad,amendment", [
    ("2008-01-02", "39-15325"), ("2004-07-22R1", "39-15326"), ("90-25-05R1", "39-15327"),
])
def test_correction_that_cites_the_ad_without_its_text_is_unanchored(ad, amendment):
    """E8-2623 corrige tres ADs: cita su corchete ("Amendment 39-15325; AD
    2008-01-02") y reproduce sus encabezados entre elisiones "* * *". Ninguna
    de las dos cosas es el texto del AD."""
    assert _split("E8-2623", ad, amendment)["status"] == "unanchored"


def test_an_ad_only_cited_in_the_body_is_unanchored():
    """2024-13013 reemplaza a AD 2014-15-09 y lo nombra en "Affected ADs"."""
    assert _split("2024-13013", "2014-15-09", None)["status"] == "unanchored"


_COMPOSED = "composed-2015-12-04-then-2006-15-08.htm"


def test_first_block_is_another_ad_citing_the_expected_one():
    """El primer bloque (AD 2015-12-04) cita a AD 2006-15-08 en "Affected ADs";
    el ancla es el encabezado del segundo, que lo declara."""
    r = enrich.split_ad_sections(_html(_COMPOSED), "2006-15-08", "39-14688")
    assert r["status"] == "extracted"
    assert r["anchor"].startswith("2006-15-08 Honeywell International Inc. (formerly AlliedSignal")
    assert _by_kind(r, "effective_date")[0]["body"] == "(a) This airworthiness directive (AD) becomes effective August 24, 2006."


def test_two_ads_in_one_document_keep_only_the_expected_block():
    r = enrich.split_ad_sections(_html(_COMPOSED), "2015-12-04", "39-18177")
    assert r["status"] == "extracted"
    assert r["anchor"].startswith("2015-12-04 Honeywell International Inc.: Amendment 39-18177;")
    text = " ".join(s["body"] for s in r["sections"])
    assert "This AD replaces AD 2006-15-08" in text
    assert "August 24, 2006" not in text  # nada del bloque siguiente
    assert r["sections"][-1]["kind"] == "material"


def test_rule_without_unsafe_condition_is_still_extracted():
    """Una rescisión no tiene condición insegura: el parser entrega lo que hay
    y la base decide que el texto no está completo."""
    r = _split("2017-25253")
    assert r["status"] == "extracted"
    assert not _by_kind(r, "unsafe_condition")
    assert _kinds(r) == [("a", "effective_date"), ("b", "affected_ads"), ("c", "applicability"),
                         ("d", "related_information"), ("e", "material")]


# ------------------------------------------------------------- estados

@pytest.mark.parametrize("html", ["", "   ", "<html><body>Please request access to continue</body></html>"])
def test_empty_or_request_access_is_empty(html):
    assert enrich.split_ad_sections(html, "2024-06-03", "39-22708")["status"] == "empty"


def test_anchored_block_without_sections_is_error():
    html = "<pre>\n2024-06-03 The Boeing Company: Amendment 39-22708; Docket No. FAA-2023-1400.\n\n    Issued on March 15, 2024.\n</pre>"
    assert enrich.split_ad_sections(html, "2024-06-03", "39-22708")["status"] == "error"


def test_no_expected_ad_is_unanchored():
    assert enrich.split_ad_sections(_html("2024-08105.htm"), None, "39-22708")["status"] == "unanchored"


def test_every_section_has_a_non_empty_body_and_a_known_kind():
    kinds = set(enrich.SECTION_KINDS) | {"other"}
    for name, fx in FIXTURES.items():
        if not fx.get("expected_ad"):
            continue
        for s in enrich.split_ad_sections(_html(name), fx["expected_ad"], fx["expected_amendment"])["sections"]:
            assert s["body"].strip() and s["heading"] and s["kind"] in kinds, (name, s["position"])
