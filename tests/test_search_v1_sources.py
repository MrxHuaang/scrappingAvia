"""
Contrato v1: una consulta a la BD por autoridad, en paralelo, sin scraping en
vivo. Las fuentes corren concurrentemente, no una tras otra, y el stream
termina con un solo `done` aunque el cliente corte a mitad.
"""

import json
import time

import pytest

from webapp import search as search_mod
from webapp.search import _query_sources_concurrently, _v1_sources, _v1_stream


def _fake_search(delays, calls=None):
    def fake(source, max_results, **filters):
        if calls is not None:
            calls.append((source, max_results, filters))
        delay = delays.get(source, 0)
        if delay is None:
            raise RuntimeError("upstream down")
        time.sleep(delay)
        return [source]
    return fake


def test_all_is_the_same_seven_authorities_as_before():
    assert [k for k, _label in _v1_sources("all")] == [
        "federal_register", "easa", "anac_brazil", "transport_canada",
        "dgac_chile", "anac_argentina", "uk_caa",
    ]
    assert _v1_sources("gcaa_uae") == [("gcaa_uae", "GCAA UAE")]


def test_every_source_is_a_database_query_with_the_same_filters(monkeypatch):
    calls = []
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({}, calls))
    out = list(_query_sources_concurrently(_v1_sources("all"), 500, keyword="fuel", offset=10))
    assert sorted(k for k, *_ in out) == sorted(k for k, _ in _v1_sources("all"))
    assert {c[0] for c in calls} == {k for k, _ in _v1_sources("all")}
    assert all(c[1] == 500 and c[2] == {"keyword": "fuel", "offset": 10} for c in calls)


def test_total_time_is_the_slowest_source_not_the_sum(monkeypatch):
    sources = [(f"s{i}", f"S{i}") for i in range(5)]
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({k: 0.3 for k, _ in sources}))
    started = time.monotonic()
    out = list(_query_sources_concurrently(sources, 10))
    assert len(out) == 5
    # En serie serian 1,5 s.
    assert time.monotonic() - started < 1.0


def test_results_arrive_as_each_source_finishes(monkeypatch):
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({"slow": 0.4, "fast": 0.05}))
    keys = [k for k, *_ in _query_sources_concurrently([("slow", "Slow"), ("fast", "Fast")], 10)]
    assert keys == ["fast", "slow"]


def test_a_failing_source_reports_its_error_without_sinking_the_rest(monkeypatch):
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({"bad": None}))
    got = {k: (r, e) for k, _l, r, e in _query_sources_concurrently([("bad", "Bad"), ("ok", "Ok")], 10)}
    assert got["ok"][0] == ["ok"]
    assert got["bad"][0] == [] and got["bad"][1]


def test_no_sources_yields_nothing():
    assert list(_query_sources_concurrently([], 10)) == []


# ----------------------------------------------------- stream v1: cierre --
def _events(chunks):
    return [json.loads(c[len("data: "):]) for c in chunks]


def test_stream_emits_one_event_per_source_and_a_single_done(monkeypatch):
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({}))
    events = _events(_v1_stream([("easa", "EASA"), ("uk_caa", "UK CAA")], 10, {}))
    assert sorted(e["source"] for e in events[:-1]) == ["easa", "uk_caa"]
    assert [e for e in events if e.get("event") == "done"] == [{"event": "done", "total": 2}]
    assert events[-1] == {"event": "done", "total": 2}


def test_a_failure_mid_stream_is_error_then_one_done(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("pool gone")
        yield  # pragma: no cover

    monkeypatch.setattr(search_mod, "_query_sources_concurrently", boom)
    events = _events(_v1_stream([("easa", "EASA")], 10, {}))
    assert events == [{"event": "error", "message": "pool gone"}, {"event": "done", "total": 0}]


def test_client_disconnect_closes_the_stream_cleanly(monkeypatch):
    monkeypatch.setattr(search_mod, "search_supabase", _fake_search({}))
    gen = _v1_stream([("easa", "EASA"), ("uk_caa", "UK CAA")], 10, {})
    first = json.loads(next(gen)[len("data: "):])
    assert "source" in first
    # Lo que hace el servidor cuando el cliente corta. Con un `yield` en el
    # `finally` esto lanzaba RuntimeError("generator ignored GeneratorExit").
    gen.close()
    with pytest.raises(StopIteration):
        next(gen)
