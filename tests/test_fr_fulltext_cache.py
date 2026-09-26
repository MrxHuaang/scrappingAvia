"""Cache v2 del full text de GovInfo (scrapers/federal_register.py).

Sin red: la Session de requests se sustituye por una falsa que sirve granulos
reales de `fixtures/fr_text/` y cuenta los GET.
"""

import hashlib
import json
import os
import threading

import pytest
import requests

from scrapers import federal_register as fr
from scrapers.common.cache import load_json_cache

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _html(name: str) -> str:
    with open(os.path.join(_DIR, "fr_text", name), encoding="utf-8") as f:
        return f.read()


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code, self.text, self.ok = status, text, status < 400


class _Session:
    """Responde por document_number con (status, texto) o una excepción."""

    def __init__(self, responses: dict):
        self.responses, self.calls, self.headers = responses, [], {}
        self._lock = threading.Lock()

    def get(self, url, timeout=None):
        doc = url.split("/granules/")[1].split("/")[0]
        with self._lock:
            self.calls.append(doc)
        answer = self.responses[doc]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(*answer)


@pytest.fixture()
def govinfo(monkeypatch, tmp_path):
    """Cache vacío en tmp, clave presente y una Session falsa por prueba."""
    monkeypatch.setattr(fr, "_FULLTEXT_CACHE_PATH", str(tmp_path / "fr_fulltext_cache.json"))
    monkeypatch.setattr(fr, "_fulltext_cache", {})
    monkeypatch.setattr(fr, "_ft_attempted", set())
    monkeypatch.setattr(fr, "_ft_local", threading.local())
    monkeypatch.setenv("GOVINFO_API_KEY", "clave-de-prueba")
    session = _Session({})
    monkeypatch.setattr(requests, "Session", lambda: session)
    return session


def _new_run():
    """Una corrida nueva: mismo cache en disco, sin intentos registrados."""
    fr._ft_attempted.clear()


def test_extracted_entry_keeps_sections_status_and_sha(govinfo):
    html = _html("2024-08105.htm")
    govinfo.responses["2024-08105"] = (200, html)
    entry = fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03", amendment="39-22708")
    assert entry["v"] == 2
    assert entry["status"] == "extracted"
    assert entry["sha"] == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert [s["kind"] for s in entry["sections"]][:3] == ["effective_date", "affected_ads", "applicability"]
    assert entry["anchor"].startswith("2024-06-03 The Boeing Company")
    # Campos del cuerpo que ya existían (link_supersession lee `supersedes`).
    assert entry["amoc"] is True and entry["supersedes"] == []
    _new_run()
    fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03", amendment="39-22708")
    assert govinfo.calls == ["2024-08105"]  # definitivo: no se vuelve a pedir


def test_unanchored_is_final(govinfo):
    govinfo.responses["E7-1708"] = (200, _html("E7-1708.htm"))
    entry = fr._fetch_fulltext_fields("E7-1708", "2007-02-05", ad="2007-03-13", amendment="39-14924")
    assert (entry["status"], entry["sections"]) == ("unanchored", [])
    _new_run()
    fr._fetch_fulltext_fields("E7-1708", "2007-02-05", ad="2007-03-13", amendment="39-14924")
    assert govinfo.calls == ["E7-1708"]


@pytest.mark.parametrize("answer,status", [
    ((500, "Internal Server Error"), "error"),
    (requests.ConnectionError("reset"), "error"),
    ((200, "<html>Please request access</html>"), "empty"),
])
def test_error_and_empty_are_retried_next_run_but_once_per_run(govinfo, answer, status):
    govinfo.responses["2024-08105"] = answer
    assert fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03")["status"] == status
    fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03")
    assert govinfo.calls == ["2024-08105"]  # misma corrida: un solo GET
    _new_run()
    govinfo.responses["2024-08105"] = (200, _html("2024-08105.htm"))
    assert fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03")["status"] == "extracted"
    assert govinfo.calls == ["2024-08105", "2024-08105"]


V1_ENTRY = {"compliance_time": "within 30 days", "amoc": True, "ad": "2019-01-02",
            "docket": "FAA-2023-1400", "supersedes": ["2019-01-02"], "ad_header": None}


def test_v1_entry_is_read_as_is_without_a_key(govinfo, monkeypatch):
    monkeypatch.delenv("GOVINFO_API_KEY")
    fr._fulltext_cache["2024-08105"] = dict(V1_ENTRY)
    assert fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03") == V1_ENTRY
    assert govinfo.calls == []
    assert fr._fulltext_cache["2024-08105"] == V1_ENTRY


def test_v1_entry_is_migrated_by_one_fetch(govinfo):
    fr._fulltext_cache["2024-08105"] = dict(V1_ENTRY)
    govinfo.responses["2024-08105"] = (200, _html("2024-08105.htm"))
    entry = fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03", amendment="39-22708")
    assert (entry["v"], entry["status"]) == (2, "extracted")
    assert "ad" not in entry  # el 'ad' v1 (primer AD del cuerpo) no sobrevive


def test_failed_migration_keeps_the_v1_fields(govinfo):
    fr._fulltext_cache["2024-08105"] = dict(V1_ENTRY)
    govinfo.responses["2024-08105"] = (503, "busy")
    entry = fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03")
    assert entry["status"] == "error"
    assert (entry["compliance_time"], entry["supersedes"]) == ("within 30 days", ["2019-01-02"])


def test_without_a_key_nothing_is_fetched_nor_cached(govinfo, monkeypatch):
    monkeypatch.delenv("GOVINFO_API_KEY")
    assert fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03") == {}
    assert govinfo.calls == [] and fr._fulltext_cache == {}


def test_cache_round_trips_through_disk(govinfo):
    govinfo.responses["2024-08105"] = (200, _html("2024-08105.htm"))
    entry = fr._fetch_fulltext_fields("2024-08105", "2024-04-17", ad="2024-06-03", amendment="39-22708")
    fr._save_fulltext_cache()
    on_disk = load_json_cache(fr._FULLTEXT_CACHE_PATH)
    assert on_disk["2024-08105"] == json.loads(json.dumps(entry))


def test_prefetch_is_concurrent_and_skips_final_entries(govinfo):
    govinfo.responses.update({
        "2024-08105": (200, _html("2024-08105.htm")),
        "2024-00993": (200, _html("2024-00993.htm")),
        "E7-1708": (500, "down"),
    })
    fr._fulltext_cache["E8-2623"] = {"v": 2, "status": "unanchored", "anchor": None, "sections": []}
    docs = [
        {"document_number": "2024-08105", "publication_date": "2024-04-17",
         "docket_ids": ["Amendment 39-22708", "AD 2024-06-03"]},
        {"document_number": "2024-00993", "publication_date": "2024-01-18",
         "docket_ids": ["Amendment 39-22663", "AD 2024-02-51"]},
        {"document_number": "E7-1708", "publication_date": "2007-02-05",
         "docket_ids": ["Amendment 39-14924", "AD 2007-03-13"]},
        {"document_number": "E8-2623", "publication_date": "2008-02-14", "docket_ids": []},
    ]
    fr._prefetch_fulltext(docs)
    assert sorted(govinfo.calls) == ["2024-00993", "2024-08105", "E7-1708"]
    assert {d: fr._fulltext_cache[d]["status"] for d in ("2024-08105", "2024-00993", "E7-1708")} == {
        "2024-08105": "extracted", "2024-00993": "extracted", "E7-1708": "error"}
    fr._prefetch_fulltext(docs)  # misma corrida: nada pendiente
    assert len(govinfo.calls) == 3


def test_header_ad_backs_up_docket_ids_without_the_ad(govinfo):
    """docket_ids sin AD: el respaldo anclado al amendment del preámbulo da la
    identidad y también el AD al que se anclan las secciones."""
    govinfo.responses["2024-08105"] = (200, _html("2024-08105.htm"))
    rec = fr._parse_document({
        "title": "Airworthiness Directives; The Boeing Company Airplanes", "abstract": "",
        "document_number": "2024-08105", "publication_date": "2024-04-17", "type": "Rule",
        "docket_ids": ["Docket No. FAA-2023-1400", "Amendment 39-22708"],
    })
    assert rec["AD_Number"] == "2024-06-03"
    assert fr._fulltext_cache["2024-08105"]["status"] == "extracted"
