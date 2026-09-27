"""
Contrato v2 de busqueda (webapp.search_v2), por piezas: la cabecera de fuentes
efectivas, los parametros -> p_filters y la pagina que sale de UNA llamada a
`search_ads_v2`. La BD es un doble: aqui se fija lo que Flask manda y como
lee la respuesta, no el SQL.
"""

import pytest
from postgrest.exceptions import APIError
from werkzeug.datastructures import MultiDict

from webapp import search_v2
from webapp.search_v2 import (
    FilterError,
    Params,
    UpstreamError,
    build_filters,
    fetch_page,
    parse_effective_sources,
    requested_version,
)

_MFR = "3f2b8c1e-0a4d-4c6e-9b7a-1d2e3f4a5b6c"


class FakeSupabase:
    """Doble de `get_supabase_admin()`: registra cada RPC y devuelve `data`."""

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        if self.error:
            raise self.error
        return type("Resp", (), {"data": self.data})()


def _args(**kw):
    return Params.from_args(MultiDict(kw))


# ---------------------------------------------------------------- version --
@pytest.mark.parametrize("raw, version", [
    (None, 1), ("", 1), ("1", 1), (1, 1), ("2", 2), (2, 2), ("3", None), ("x", None),
])
def test_requested_version(raw, version):
    assert requested_version(raw) == version


# ------------------------------------------------------ effective sources --
def test_star_means_no_source_filter():
    assert parse_effective_sources("*") is None


def test_list_is_sorted_and_deduped():
    assert parse_effective_sources("federal_register, easa,easa") == ["easa", "federal_register"]


@pytest.mark.parametrize("raw", [None, "", "  ", "narnia", "easa,narnia", "easa,,uk_caa", "all", "*,easa"])
def test_missing_empty_or_unknown_header_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_effective_sources(raw)


# ----------------------------------------------------------------- params --
def test_filters_carry_every_v2_parameter():
    params = Params.from_args(MultiDict([
        ("mfr", _MFR), ("make", "Foo Aero, Ltd."), ("models", "A320"), ("models", "A321"),
        ("min_confidence", "Medium"), ("statuses", "new"), ("statuses", "Revision"),
        ("has_pdf", "true"), ("product_type", "engine"), ("product_subtype", "turbine"),
        ("date_from", "2024-01-01"), ("date_to", "2024-12-31"), ("keyword", " fuel "),
        ("limit", "50"), ("offset", "200"),
        # El query no decide las fuentes: las pone la cabecera del proxy.
        ("sources", "uk_caa"),
    ]))
    filters, limit, offset = build_filters(params, ["easa"])
    assert (limit, offset) == (50, 200)
    assert filters == {
        "v": 2,
        "sources": ["easa"],
        "manufacturer_ids": [_MFR],
        "make_texts": ["Foo Aero, Ltd."],     # la coma es parte del nombre
        "models": ["A320", "A321"],
        "min_confidence": "medium",
        "product_types": ["engine"],
        "product_subtypes": ["turbine"],
        "statuses": ["New", "Revision"],
        "has_pdf": True,
        "date_from": "2024-01-01",
        "date_to": "2024-12-31",
        "term": "fuel",
    }


def test_defaults_are_nulls_and_empty_lists():
    filters, limit, offset = build_filters(_args(keyword="2023-12-03"), None)
    assert (limit, offset) == (100, 0)
    assert filters["sources"] is None
    assert filters["manufacturer_ids"] == [] and filters["statuses"] == []
    assert filters["has_pdf"] is None and filters["min_confidence"] is None
    assert filters["date_from"] is None and filters["term"] == "2023-12-03"


def test_json_body_reads_like_the_query_string():
    body = {"v": 2, "mfr": [_MFR], "models": "A320", "has_pdf": False, "limit": 500}
    filters, limit, _ = build_filters(Params.from_json(body), None)
    assert filters["models"] == ["A320"]
    assert filters["has_pdf"] is False
    assert limit == 100                        # recortado al tope


@pytest.mark.parametrize("kw", [
    {"mfr": "not-a-uuid", "models": "A320"},
    {"mfr": _MFR, "statuses": "Pending"},
    {"mfr": _MFR, "models": "A320", "min_confidence": "certain"},
    {"mfr": _MFR, "has_pdf": "maybe"},
    {"mfr": _MFR, "date_from": "01/02/2024"},
    {"mfr": _MFR, "models": "A320", "limit": "ten"},
    {"mfr": _MFR, "models": "A320", "offset": "-1"},
    {"mfr": _MFR, "models": "A320", "offset": "100001"},
    {"mfr": _MFR},                              # gate: fabricante solo
    {"models": "A320"},                         # gate: sin fabricante
])
def test_invalid_or_insufficient_filters(kw):
    with pytest.raises(FilterError):
        build_filters(_args(**kw), None)


# ------------------------------------------------------------------- page --
_V2_ROW = {
    "source": "easa", "source_label": "EASA", "issuing_authority": "easa",
    "ad_number": "2024-0001", "subject": "Fuel pump", "make": "AIRBUS S.A.S.",
    "model": "A320-214", "product_type": "Aircraft", "effective_date": "2024-01-15",
    "status": "New", "docket": None, "pdf_link": "https://example.test/a.pdf",
    "superseded_by": None, "match_confidence": "high",
    "applicability_unknown": False, "has_text": False,
}


def test_one_rpc_per_page_and_rows_through_the_adapter(monkeypatch):
    sb = FakeSupabase({"v": 2, "total": 3, "source_counts": {"easa": 3},
                       "limit": 2, "offset": 0, "results": [_V2_ROW, _V2_ROW]})
    monkeypatch.setattr(search_v2, "get_supabase_admin", lambda: sb)
    page = fetch_page({"v": 2}, 2, 0)
    assert sb.calls == [("search_ads_v2", {"p_filters": {"v": 2}, "p_limit": 2, "p_offset": 0})]
    assert page["total"] == 3 and page["source_counts"] == {"easa": 3}
    assert page["has_more"] is True
    assert page["results"][0]["AD_Number"] == "2024-0001"
    assert page["results"][0]["Match_Confidence"] == "high"


def test_offset_past_the_total_keeps_the_total(monkeypatch):
    sb = FakeSupabase({"v": 2, "total": 3, "source_counts": {"easa": 3}, "results": []})
    monkeypatch.setattr(search_v2, "get_supabase_admin", lambda: sb)
    page = fetch_page({"v": 2}, 100, 6000)
    assert page == {"v": 2, "total": 3, "source_counts": {"easa": 3}, "limit": 100,
                    "offset": 6000, "has_more": False, "results": []}


def test_invalid_parameter_from_the_database_is_a_filter_error(monkeypatch):
    err = APIError({"code": "22023", "message": "unknown product_subtype: foo"})
    monkeypatch.setattr(search_v2, "get_supabase_admin", lambda: FakeSupabase(error=err))
    with pytest.raises(FilterError, match="unknown product_subtype"):
        fetch_page({"v": 2}, 100, 0)


@pytest.mark.parametrize("sb", [
    None,                                                    # sin Supabase configurado
    FakeSupabase(error=APIError({"code": "57014", "message": "canceling statement"})),
    FakeSupabase(error=RuntimeError("connection reset")),
    FakeSupabase(data=[{"unexpected": "list"}]),
])
def test_database_failures_are_upstream_errors(monkeypatch, sb):
    monkeypatch.setattr(search_v2, "get_supabase_admin", lambda: sb)
    with pytest.raises(UpstreamError) as exc:
        fetch_page({"v": 2}, 100, 0)
    # El detalle interno no llega al cliente.
    assert "canceling" not in str(exc.value) and "reset" not in str(exc.value)
