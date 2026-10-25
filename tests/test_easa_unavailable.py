"""EASA caido no es "0 ADs": la corrida tiene que fallar.

Desde ~2026-10-21 ad.easa.europa.eu contesta HTTP 200 con su pagina de
mantenimiento en cada URL. El recorrido leia "pagina 1 sin resultados", paraba
y el job salia en verde con 0 ADs. El fixture es la respuesta real del
2026-10-25 a /search/advanced/result (identica en ?page=2 y en /json/).
"""

import os

import pytest

import populate_db
from scrapers import easa

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "easa", "maintenance_page_2026-10-25.html")


def _maintenance() -> str:
    with open(_FIXTURE, encoding="utf-8") as f:
        return f.read()


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.fixture()
def listing(monkeypatch):
    """Sirve `pages[i]` a la i-esima pagina pedida y anota las URLs."""
    pages: list[str] = []
    asked: list[str] = []

    def fake_get(url, session=None, params=None, headers=None, timeout=None, tag=None):
        asked.append(url)
        return _Resp(pages[len(asked) - 1])

    monkeypatch.setattr(easa, "get_with_backoff", fake_get)
    monkeypatch.setattr(easa.requests.Session, "get", lambda self, url, **k: _Resp(pages[0]))
    monkeypatch.setattr(easa.time, "sleep", lambda _s: None)
    return pages, asked


def test_the_saved_maintenance_page_is_recognised():
    from bs4 import BeautifulSoup
    assert easa._is_maintenance_page(BeautifulSoup(_maintenance(), "lxml"))
    assert not easa._is_maintenance_page(BeautifulSoup(
        "<html><title>EASA - Safety Publications</title><table class='ad-list'></table></html>", "lxml"))


def test_the_maintenance_page_fails_the_walk_instead_of_ending_it(listing):
    pages, asked = listing
    pages.append(_maintenance())
    with pytest.raises(easa.EasaUnavailable, match="mantenimiento"):
        easa.search_ads(max_pages=910)
    assert len(asked) == 1


def test_the_maintenance_page_mid_walk_also_fails(listing, monkeypatch):
    """Un recorrido que se corta a la mitad tampoco es el final del listado."""
    pages, _ = listing
    monkeypatch.setattr(easa, "_PAGE_SIZE", 1)
    row = ("<p>Displaying records 1 to 1 out of a total of 5 publications.</p>"
           "<table class='ad-list'><tr><td>x</td></tr></table>")
    monkeypatch.setattr(easa, "_parse_results_table",
                        lambda soup: [{"AD_Number": "2026-0001"}] if soup.find("table") else [])
    pages.extend([row, _maintenance()])
    with pytest.raises(easa.EasaUnavailable):
        easa.search_ads(max_pages=910)


def test_an_empty_first_page_of_the_unfiltered_listing_fails(listing):
    """Markup cambiado (sin tabla ni total) con el mismo efecto: 0 filas."""
    pages, _ = listing
    pages.append("<html><title>EASA</title><body><div>nothing here</div></body></html>")
    with pytest.raises(easa.EasaUnavailable, match="pagina 1"):
        easa.search_ads(max_pages=910)


def test_an_empty_filtered_search_is_still_an_empty_result(listing):
    pages, _ = listing
    pages.append("<html><title>EASA</title><body><div>no results</div></body></html>")
    assert easa.search_ads(keyword="no-such-model", max_pages=5) == []


def test_the_main_page_fallback_does_not_hide_it_either(listing):
    pages, _ = listing
    pages.append(_maintenance())
    with pytest.raises(easa.EasaUnavailable):
        easa.search_main_page(max_pages=10)


def test_inside_actions_a_failed_source_gets_an_error_annotation(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    populate_db._actions_error("easa scrape failed", "EasaUnavailable: pagina de mantenimiento,\n100% caida")
    out = capsys.readouterr().out
    assert out == "::error title=easa scrape failed::EasaUnavailable: pagina de mantenimiento, 100%25 caida\n"


def test_outside_actions_there_is_no_annotation(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    populate_db._actions_error("easa scrape failed", "x")
    assert capsys.readouterr().out == ""
