"""
Normalizacion y tipo del termino de busqueda (utils.search_term_normalize /
search_term_kind).

La BD decide con `ad_search_term_normalize` / `ad_search_term_kind` si un
termino se busca como numero de AD, como docket o como texto, y el gate de
Flask tiene que clasificarlo igual. tests/fixtures/search_terms.json es el
mismo archivo en los dos repos: si un lado cambia una regex, su test rompe.
"""

import json
from pathlib import Path

import pytest

from utils import (
    SEARCH_FILTER_HINT,
    search_term_kind,
    search_term_normalize,
    validate_search_filters_v2,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "search_terms.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _CASES, ids=[c["input"] for c in _CASES])
def test_shared_fixture(case):
    assert search_term_normalize(case["input"]) == case["norm"]
    assert search_term_kind(case["input"]) == case["kind"]


def test_fixture_covers_every_kind():
    assert {c["kind"] for c in _CASES} == {"ad_number", "docket", "text"}


@pytest.mark.parametrize("term, norm", [
    ("  AD   2023-12-03 ", "2023-12-03"),
    ("A.D. 95-09-02", "95-09-02"),
    ("Ad hoc repair", "Ad hoc repair"),     # "AD" sin numero detras: es texto
    ("Docket fuel", "Docket fuel"),
    ("", ""),
    (None, ""),
])
def test_prefixes_are_only_dropped_before_an_identifier(term, norm):
    assert search_term_normalize(term) == norm


@pytest.mark.parametrize("term, kind", [
    ("2018-23-51R1", "ad_number"),
    ("98-18-12-AD", "ad_number"),
    ("MCAI-2023-00013-T", "docket"),
    ("A320", "text"),
    ("", "text"),
])
def test_kind_edges(term, kind):
    assert search_term_kind(term) == kind


# --------------------------------------------------------------- gate v2 --
@pytest.mark.parametrize("kwargs", [
    {"keyword": "2023-12-03"},
    {"keyword": "Docket No. FAA-2023-0927"},     # docket con prefijo: pasa solo
    {"keyword": "2024-0195"},                   # EASA: identificador por is_ad_identifier
    {"manufacturer_ids": ["m1"], "models": ["A320"]},
    {"make_texts": ["Foo Aero"], "keyword": "fuel"},
    {"manufacturer_ids": ["m1"], "product_types": ["engine"]},
    {"manufacturer_ids": ["m1"], "product_subtypes": ["turbine"]},
    {"manufacturer_ids": ["m1"], "statuses": ["New"]},
    {"manufacturer_ids": ["m1"], "has_pdf": False},
    {"manufacturer_ids": ["m1"], "date_from": "2026-01-01"},
])
def test_v2_gate_accepts(kwargs):
    assert validate_search_filters_v2(**kwargs) is None


@pytest.mark.parametrize("kwargs", [
    {},
    {"keyword": "fuel"},
    {"manufacturer_ids": ["m1"]},                # fabricante solo
    {"make_texts": ["  "], "models": ["A320"]},  # make en blanco no cuenta
    {"models": ["A320"], "statuses": ["New"]},   # sin fabricante
])
def test_v2_gate_rejects(kwargs):
    assert validate_search_filters_v2(**kwargs) == SEARCH_FILTER_HINT
