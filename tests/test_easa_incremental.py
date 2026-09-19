"""
Recorrido incremental de EASA.

El completo son 910 paginas a ~1,25 s: 19 minutos de runner diarios para
encontrar las ~5 directivas que EASA publica al dia. La lista viene de la mas
nueva a la mas vieja, asi que se puede parar cuando deja de aparecer algo
desconocido — con margen, y con un completo semanal que lo contrasta.
"""

import os

import populate_db
import scrapers.easa as easa


def _page(*numbers):
    return [{"AD_Number": n, "Subject": "x"} for n in numbers]


def test_walk_stops_after_quiet_pages(monkeypatch):
    pages = [
        _page("2026-0200", "2026-0199"),   # nuevas
        _page("2026-0150", "2026-0149"),   # conocidas  -> quiet 1
        _page("2026-0148"),                # conocidas  -> quiet 2
        _page("2026-0147"),                # conocidas  -> quiet 3
        _page("2026-0146"),                # conocidas  -> quiet 4
        _page("2026-0145"),                # conocidas  -> quiet 5, para aqui
        _page("2026-0144"),                # no deberia pedirse
    ]
    asked = []

    def fake_search(max_pages=910, stop_when=None, **kw):
        out = []
        for i, rows in enumerate(pages, 1):
            asked.append(i)
            out.extend(rows)
            if stop_when is not None and stop_when(rows):
                break
        return out

    known = {"2026-0150", "2026-0149", "2026-0148", "2026-0147", "2026-0146", "2026-0145", "2026-0144"}
    monkeypatch.setattr(populate_db, "easa_search", fake_search)
    monkeypatch.setattr(populate_db, "easa_enrich_details", lambda r: r)
    monkeypatch.setattr(populate_db, "_easa_known_ad_numbers", lambda _c: known)
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: object())
    monkeypatch.delenv("EASA_FULL", raising=False)

    out = populate_db.run_easa()

    assert asked == [1, 2, 3, 4, 5, 6], "para tras EASA_QUIET_PAGES paginas sin novedad"
    # Nada de lo leido se pierde por parar.
    assert [r["AD_Number"] for r in out][:2] == ["2026-0200", "2026-0199"]


def test_a_new_ad_resets_the_margin(monkeypatch):
    pages = [
        _page("2026-0200"),   # nueva
        _page("2026-0150"),   # conocida -> quiet 1
        _page("2026-0149"),   # conocida -> quiet 2
        _page("2026-0198"),   # NUEVA otra vez -> quiet vuelve a 0
        _page("2026-0148"),   # quiet 1
        _page("2026-0147"),   # quiet 2
        _page("2026-0146"),   # quiet 3
        _page("2026-0145"),   # quiet 4
        _page("2026-0144"),   # quiet 5 -> para
        _page("2026-0143"),
    ]
    asked = []

    def fake_search(max_pages=910, stop_when=None, **kw):
        out = []
        for i, rows in enumerate(pages, 1):
            asked.append(i)
            out.extend(rows)
            if stop_when is not None and stop_when(rows):
                break
        return out

    known = {f"2026-01{n}" for n in range(40, 51)}
    monkeypatch.setattr(populate_db, "easa_search", fake_search)
    monkeypatch.setattr(populate_db, "easa_enrich_details", lambda r: r)
    monkeypatch.setattr(populate_db, "_easa_known_ad_numbers", lambda _c: known)
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: object())
    monkeypatch.delenv("EASA_FULL", raising=False)

    populate_db.run_easa()
    assert asked == list(range(1, 10)), "una novedad reinicia el margen"


def test_easa_full_ignores_the_known_set(monkeypatch):
    seen = {}

    def fake_search(max_pages=910, stop_when=None, **kw):
        seen["stop_when"] = stop_when
        seen["max_pages"] = max_pages
        return _page("2026-0200")

    monkeypatch.setattr(populate_db, "easa_search", fake_search)
    monkeypatch.setattr(populate_db, "easa_enrich_details", lambda r: r)
    monkeypatch.setenv("EASA_FULL", "1")

    populate_db.run_easa()
    assert seen["stop_when"] is None, "el completo no para antes de tiempo"
    assert seen["max_pages"] == 910


def test_no_database_means_a_full_walk(monkeypatch):
    seen = {}

    def fake_search(max_pages=910, stop_when=None, **kw):
        seen["stop_when"] = stop_when
        return _page("2026-0200")

    def boom():
        raise RuntimeError("sin credenciales")

    monkeypatch.setattr(populate_db, "easa_search", fake_search)
    monkeypatch.setattr(populate_db, "easa_enrich_details", lambda r: r)
    monkeypatch.setattr(populate_db, "get_supabase_client", boom)
    monkeypatch.delenv("EASA_FULL", raising=False)

    populate_db.run_easa()
    # No saber que tenemos no es razon para asumir que lo tenemos todo.
    assert seen["stop_when"] is None


def test_scraper_consults_the_callback_after_accumulating(monkeypatch):
    # El contrato del lado del scraper: lo leido se conserva aunque se pare.
    calls = []
    monkeypatch.setattr(easa, "_parse_total_results", lambda soup: 1000)
    monkeypatch.setattr(easa, "_parse_results_table", lambda soup: _page("A", "B"))
    monkeypatch.setattr(easa.time, "sleep", lambda *_: None)

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self): pass

    monkeypatch.setattr(easa, "get_with_backoff", lambda *a, **k: FakeResp())
    monkeypatch.setattr(easa, "BeautifulSoup", lambda *a, **k: object())

    def stop_after_two(rows):
        calls.append(rows)
        return len(calls) >= 2

    out = easa.search_ads(max_pages=50, stop_when=stop_after_two)
    assert len(calls) == 2
    assert len(out) == 4, "las dos paginas leidas se devuelven completas"


def test_os_env_is_what_the_workflow_sets():
    # El workflow pone EASA_FULL=1 el domingo; aqui solo se fija el nombre.
    assert "EASA_FULL" not in os.environ or os.environ["EASA_FULL"] in ("", "1")
