"""Las fuentes de una búsqueda corren en paralelo, no una tras otra."""

import time

from webapp.search import _run_scrapers_concurrently


def _slow(value, delay):
    def fn():
        time.sleep(delay)
        return [value]
    return fn


def test_total_time_is_the_slowest_source_not_the_sum():
    scrapers = [(f"s{i}", f"S{i}", _slow(i, 0.3)) for i in range(5)]
    started = time.monotonic()
    out = list(_run_scrapers_concurrently(scrapers, max_results=10))
    elapsed = time.monotonic() - started
    assert len(out) == 5
    # En serie serían 1,5 s.
    assert elapsed < 1.0


def test_results_arrive_as_each_source_finishes():
    scrapers = [("slow", "Slow", _slow("a", 0.4)), ("fast", "Fast", _slow("b", 0.05))]
    keys = [src_key for src_key, _label, _results, _err in _run_scrapers_concurrently(scrapers, 10)]
    assert keys == ["fast", "slow"]


def test_a_failing_source_reports_its_error_without_sinking_the_rest():
    def boom():
        raise RuntimeError("upstream down")

    scrapers = [("bad", "Bad", boom), ("ok", "Ok", _slow("x", 0))]
    got = {k: (r, e) for k, _l, r, e in _run_scrapers_concurrently(scrapers, 10)}
    assert got["ok"][0] == ["x"]
    assert got["bad"][0] == [] and got["bad"][1]


def test_no_sources_yields_nothing():
    assert list(_run_scrapers_concurrently([], 10)) == []
