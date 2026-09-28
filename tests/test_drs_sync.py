"""DRS incremental sync: first scan, incremental overlap, failures and payloads.

The API is `drs_fakes.FakeSession` (strict, second-resolution date filter and
750-row ASC pages, as measured); the database is `drs_fakes.FakeStore`, which
records every RPC call so the payloads can be asserted exactly.
"""

from datetime import datetime, timezone

import pytest

import populate_db
from drs_fakes import FAKE_CONTACT, FAKE_KEY, FakeSession, FakeStore, load_fixture, make_doc
from scrapers import faa_drs_api as drs
from scrapers import faa_drs_sync as sync
from scrapers.common import http

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
BULK = "2024-05-30T19:48:53.441Z"   # the real 14,193-document tie

RECORD_KEYS = {
    "document_guid", "document_number", "ad_number", "status", "doc_last_modified",
    "publication_date", "effective_date", "document_url", "main_download_url",
    "makes", "models", "regulatory_text", "payload",
}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)


def _client(session):
    return drs.DrsClient(FAKE_KEY, contact_email=FAKE_CONTACT, session=session, sleep=lambda _s: None, retry_delay=0, retries=2)


def _base():
    return load_fixture("adfrawd_documents.json")["documents"][0]


def _docs(n, lm=BULK, start=0, prefix="GUID"):
    return [make_doc(_base(), f"{prefix}{i:028d}", lm, f"2024-{i // 100:02d}-{i % 100:02d}")
            for i in range(start, start + n)]


def _sent(store):
    return [d for call in store.upserts for d in call["p_documents"]]


# ------------------------------------------------------------- to_record --

def test_record_shape_is_exactly_the_contract():
    doc = dict(_base())
    doc["drs:adfrawdSupplementaryInfo"] = "Background ... (the FR preamble, ~15 KB in real documents)"
    rec = sync.to_record(doc, "ADFRAWD")
    assert set(rec) == RECORD_KEYS
    assert rec == {
        "document_guid": "FR-ADFRAWD-2026-19171-0000000000",
        "document_number": "2026-18-52",
        "ad_number": "2026-18-52",
        "status": "Current",
        "doc_last_modified": "2026-09-18T14:28:27.520Z",
        "publication_date": "2026-09-18",
        "effective_date": "2026-09-21",
        "document_url": "https://drs.faa.gov/browse/excelExternalWindow/FR-ADFRAWD-2026-19171-0000000000.0001",
        "main_download_url": "https://drs.faa.gov/api/drs/data-pull/download/0b66db95-d226-4961-a864-52562de01840",
        "makes": ["Rolls-Royce Deutschland Ltd & Co KG"],
        "models": doc["drs:adfrawdModel"],
        "regulatory_text": doc["drs:adfrawdRegulatoryText"],
        "payload": rec["payload"],
    }
    payload = rec["payload"]
    assert not any(k.endswith("SupplementaryInfo") for k in payload), "SupplementaryInfo is never stored"
    assert "drs:adfrawdRegulatoryText" not in payload, "the text travels in its column, not twice"
    assert payload["drs:adfrawdSummary"] == doc["drs:adfrawdSummary"]
    assert payload["documentGuid"] == rec["document_guid"]


def test_ad_number_is_normalized_and_the_raw_number_kept():
    doc = make_doc(_base(), "G1", BULK, "AD 2026–01–51 R1")
    rec = sync.to_record(doc, "ADFRAWD")
    assert rec["ad_number"] == "2026-01-51 R1"
    assert rec["document_number"] == "AD 2026–01–51 R1"


def test_arrays_drop_sentinels_blanks_and_repeats_without_pairing():
    doc = make_doc(_base(), "G1", BULK, **{
        "drs:adfrawdMake": ["The Boeing Company", " ", "The Boeing Company", "_EMPTY_", "Rockwell Collins", "Inc."],
        "drs:adfrawdModel": ["737-800", "_EMPTY_"],
    })
    rec = sync.to_record(doc, "ADFRAWD")
    assert rec["makes"] == ["The Boeing Company", "Rockwell Collins", "Inc."]
    assert rec["models"] == ["737-800"]


def test_old_documents_with_null_dates_and_hex_guid():
    old = load_fixture("adfrawd_documents.json")["documents"][-1]
    rec = sync.to_record({**old, "drs:effectiveDate": None, "drs:adfrawdCitationPublishDate": None}, "ADFRAWD")
    assert rec["document_guid"] == "32367EAE9D7A95B4862581E6004E4163"
    assert rec["publication_date"] is None and rec["effective_date"] is None


def test_adfread_uses_its_own_field_names_and_has_no_text():
    ead = load_fixture("adfread_documents.json")["documents"][0]
    rec = sync.to_record(ead, "ADFREAD")
    assert set(rec) == RECORD_KEYS
    assert rec["ad_number"] == "2026-18-51"
    assert rec["makes"] == ["Rolls-Royce Deutschland Ltd & Co KG"]
    assert rec["models"][0] == "RB211 Trent 768-60"
    assert rec["regulatory_text"] is None
    assert rec["publication_date"] is None


@pytest.mark.parametrize("change", [
    {"documentGuid": ""},
    {"drs:documentNumber": None},
    {"drs:status": "Draft"},
    {"docLastModifiedDate": "2026-09-18"},
    {"documentURL": "https://example.com/x"},
    {"drs:adfrawdRegulatoryText": "x" * (sync.REGULATORY_TEXT_MAX + 1)},
    {"drs:adfrawdSummary": "x" * (sync.PAYLOAD_MAX_BYTES + 1)},
])
def test_documents_the_rpc_would_reject_are_invalid(change):
    with pytest.raises(sync.InvalidDocument):
        sync.to_record({**_base(), **change}, "ADFRAWD")


_URL = "https://drs.faa.gov/browse/excelExternalWindow/X.0001"


@pytest.mark.parametrize("change", [
    # document_guid: ^[A-Za-z0-9-]{1,64}$
    {"documentGuid": "G" * 65},
    {"documentGuid": "FR ADFRAWD 2026"},
    {"documentGuid": "FR_ADFRAWD"},
    {"documentGuid": "ÄBC"},
    {"documentGuid": None},
    # document_number / ad_number: non-blank, at most 64 characters
    {"drs:documentNumber": "   "},
    {"drs:documentNumber": "2026-18-52 " + "R" * 60},
    {"drs:documentNumber": "AD " + "9" * 65},
    # URLs: DRS prefix, no whitespace, at most 2048 characters
    {"documentURL": "http://drs.faa.gov/browse/x"},
    {"documentURL": "https://drs.faa.gov/browse/a b"},
    {"documentURL": "https://drs.faa.gov/browse/a\tb"},
    {"documentURL": "https://drs.faa.gov/" + "x" * 2029},
    {"mainDocumentDownloadURL": "https://example.com/download/1"},
    {"mainDocumentDownloadURL": "https://drs.faa.gov/api/drs/data-pull/download/a\nb"},
    {"mainDocumentDownloadURL": "https://drs.faa.gov/" + "x" * 2029},
    {"mainDocumentDownloadURL": 12},
    # makes / models: at most 2000 items of at most 2048 characters
    {"drs:adfrawdMake": [f"Maker {i}" for i in range(2001)]},
    {"drs:adfrawdMake": ["M" * 2049]},
    {"drs:adfrawdModel": [f"M-{i}" for i in range(2001)]},
    {"drs:adfrawdModel": ["737-800", "X" * 2049]},
    # doc_last_modified: starts with YYYY-MM-DDTHH:MM
    {"docLastModifiedDate": "2026-09-18T14Z"},
    {"docLastModifiedDate": "18/09/2026 14:28"},
    {"docLastModifiedDate": None},
])
def test_every_database_limit_is_checked_before_sending(change):
    with pytest.raises(sync.InvalidDocument):
        sync.to_record({**_base(), **change}, "ADFRAWD")


@pytest.mark.parametrize("change", [
    {"documentGuid": "G" * 64},
    {"documentGuid": "FR-ADFRAWD-R1-2025-04334-0000000"},
    {"drs:documentNumber": "9" * 64},
    {"documentURL": "https://drs.faa.gov/" + "x" * 2028},
    {"mainDocumentDownloadURL": None},
    {"drs:adfrawdMake": [f"Maker {i}" for i in range(2000)]},
    {"drs:adfrawdModel": ["X" * 2048]},
    {"docLastModifiedDate": "2026-09-18T14:28:27.520-05:00"},
])
def test_values_at_the_limit_are_accepted(change):
    rec = sync.to_record({**_base(), **change}, "ADFRAWD")
    assert len(rec["document_guid"]) <= sync.KEY_MAX
    assert rec["main_download_url"] is None or rec["main_download_url"].startswith(sync.URL_PREFIX)


@pytest.mark.parametrize("value", ["2026-9-18", "20260918", "2026-W38-5", "2026-02-30", "18/09/2026", 20260918])
def test_a_date_that_is_not_yyyy_mm_dd_travels_as_null(value):
    rec = sync.to_record({**_base(), "drs:effectiveDate": value, "drs:adfrawdCitationPublishDate": value},
                         "ADFRAWD")
    assert rec["effective_date"] is None and rec["publication_date"] is None
    assert rec["payload"]["drs:effectiveDate"] == value, "the raw value is kept in the payload"


def test_an_oversized_document_is_skipped_without_failing_its_batch():
    docs = _docs(3)
    docs[1] = {**docs[1], "drs:adfrawdModel": ["X" * 2049]}
    store = FakeStore()
    report = sync.sync(_client(FakeSession({"ADFRAWD": docs})), store, ["ADFRAWD"], now=NOW)
    assert len(store.upserts) == 1 and len(_sent(store)) == 2
    assert store.advances, "the cursor still moves"
    assert len(report.errors) == 1 and "2048" in report.errors[0]


def test_pending_status_is_valid():
    assert sync.to_record({**_base(), "drs:status": "Pending"}, "ADFRAWD")["status"] == "Pending"


# ------------------------------------------------------------ first scan --

def test_first_scan_pages_by_offset_without_a_date_filter_then_advances_once():
    docs = _docs(1600)
    session = FakeSession({"ADFRAWD": docs})
    store = FakeStore()
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)

    assert res.error is None and res.mode == "initial"
    assert [c["params"]["offset"] for c in session.calls] == [0, 750, 1500]
    assert all("docLastModifiedDate" not in c["params"] for c in session.calls)
    assert all(c["params"]["docLastModifiedDateSortOrder"] == "ASC" for c in session.calls)
    assert all(len(u["p_documents"]) <= sync.UPSERT_BATCH for u in store.upserts)
    assert sorted(d["document_guid"] for d in _sent(store)) == sorted(d["documentGuid"] for d in docs)
    assert store.advances == [{"p_doctype": "ADFRAWD", "p_last_modified": BULK, "p_initial_scan_done": True}]


def test_a_tie_block_spanning_pages_is_read_whole():
    """1,700 documents share one second, then a few later ones. A date cursor
    could never split the block; the offset scan reads every one of them."""
    later = [make_doc(_base(), f"LATE{i}", f"2026-01-21T18:01:36.95{i}Z", f"2026-02-0{i}") for i in range(3)]
    docs = _docs(1700) + later
    session = FakeSession({"ADFRAWD": docs})
    store = FakeStore()
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)
    assert res.pages == 3 and res.documents == 1703
    assert len({d["document_guid"] for d in _sent(store)}) == 1703
    assert store.advances[-1]["p_last_modified"] == "2026-01-21T18:01:36.952Z"


def test_the_cursor_is_capped_at_the_run_start():
    docs = _docs(2) + [make_doc(_base(), "FUTURE", "2026-09-26T12:30:00.000Z", "2026-19-01")]
    store = FakeStore()
    sync.sync_doctype(_client(FakeSession({"ADFRAWD": docs})), store, "ADFRAWD", now=NOW)
    assert store.advances[-1]["p_last_modified"] == "2026-09-26T12:00:00.000Z"


# ----------------------------------------------------------- incremental --

def test_incremental_asks_cursor_minus_one_hour_and_overlap_is_idempotent():
    cursor = "2026-09-18T14:28:27.520Z"
    overlap = [make_doc(_base(), "SEEN", "2026-09-18T14:00:00.000Z", "2026-18-40")]
    fresh = [make_doc(_base(), "NEW", "2026-09-19T08:00:00.123Z", "2026-18-60")]
    old = _docs(5)
    session = FakeSession({"ADFRAWD": old + overlap + fresh})
    store = FakeStore({"ADFRAWD": {"last_modified": "2026-09-18T14:28:27.52+00:00", "initial_scan_done": True}})
    store.rows["SEEN"] = sync.to_record(overlap[0], "ADFRAWD")

    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)

    assert res.mode == "incremental"
    assert session.calls[0]["params"]["docLastModifiedDate"] == "2026-09-18T13:28:27.520Z"
    assert {d["document_guid"] for d in _sent(store)} == {"SEEN", "NEW"}
    assert (res.inserted, res.unchanged) == (1, 1)
    assert store.advances == [{"p_doctype": "ADFRAWD", "p_last_modified": "2026-09-19T08:00:00.123Z",
                               "p_initial_scan_done": True}]
    assert cursor < store.advances[0]["p_last_modified"]


def test_incremental_with_a_big_same_second_rewrite_pages_to_the_end_before_advancing():
    rewrite = _docs(1600, lm="2026-10-01T03:00:00.000Z")
    session = FakeSession({"ADFRAWD": _docs(3) + rewrite})
    store = FakeStore({"ADFRAWD": {"last_modified": "2026-09-30T00:00:00.000Z", "initial_scan_done": True}})
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert res.pages == 3 and res.documents == 1600
    assert len(store.advances) == 1
    assert store.advances[0]["p_last_modified"] == "2026-10-01T03:00:00.000Z"


def test_no_changes_means_no_advance_and_no_link():
    session = FakeSession({"ADFRAWD": _docs(3)})
    store = FakeStore({"ADFRAWD": {"last_modified": "2026-09-30T00:00:00.000Z", "initial_scan_done": True}})
    report = sync.sync(_client(session), store, ["ADFRAWD"], now=NOW)
    assert report.errors == [] and store.advances == [] and store.links == []


def test_a_state_without_initial_scan_done_scans_again_from_offset_zero():
    session = FakeSession({"ADFRAWD": _docs(3)})
    store = FakeStore({"ADFRAWD": {"last_modified": "2026-09-30T00:00:00.000Z", "initial_scan_done": False}})
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)
    assert res.mode == "initial" and "docLastModifiedDate" not in session.calls[0]["params"]


# -------------------------------------------------------------- failures --

def test_a_failed_page_stops_the_run_and_never_advances_the_cursor():
    # Page 2 answers 503 on both attempts (retries=2): the run stops there.
    session = FakeSession({"ADFRAWD": _docs(1600)}, script={2: 503, 3: 503})
    store = FakeStore()
    report = sync.sync(_client(session), store, ["ADFRAWD"], now=NOW)
    res = report.results[0]
    assert res.error and "cursor not advanced" in res.error
    assert store.advances == []
    assert res.documents == 750, "page 1 is kept: the upsert is idempotent"
    assert len(session.calls) == 3, "nothing is requested after the failed page"
    # What was uploaded still gets linked.
    linked = [n for call in store.links for n in call["p_ad_numbers"]]
    assert sorted(linked) == sorted({d["ad_number"] for d in _sent(store)})
    assert report.errors


def test_a_failed_upsert_stops_the_run_without_advancing():
    session = FakeSession({"ADFRAWD": _docs(1600)})
    store = FakeStore(fail_upsert_on=4)       # the first chunk of page 2
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)
    assert res.error and store.advances == []
    assert len(session.calls) == 2


def test_after_a_failed_first_scan_the_next_run_restarts_the_scan():
    corpus = {"ADFRAWD": _docs(1600)}
    store = FakeStore()
    sync.sync_doctype(_client(FakeSession(corpus, script={2: 503, 3: 503})), store, "ADFRAWD", now=NOW)
    assert "ADFRAWD" not in store.state

    session = FakeSession(corpus)
    res = sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)
    assert res.mode == "initial" and res.error is None
    assert session.calls[0]["params"] == {"offset": 0, "docLastModifiedDateSortOrder": "ASC"}
    assert store.state["ADFRAWD"] == {"last_modified": BULK, "initial_scan_done": True}


def test_a_failed_incremental_keeps_the_old_cursor():
    store = FakeStore({"ADFRAWD": {"last_modified": "2026-09-30T00:00:00.000Z", "initial_scan_done": True}})
    session = FakeSession({"ADFRAWD": _docs(800, lm="2026-10-01T03:00:00.000Z")}, script={2: 500, 3: 500})
    sync.sync_doctype(_client(session), store, "ADFRAWD", now=NOW)
    assert store.advances == []
    assert store.state["ADFRAWD"]["last_modified"] == "2026-09-30T00:00:00.000Z"


def test_an_unreadable_cursor_makes_no_request():
    session = FakeSession({"ADFRAWD": _docs(3)})
    res = sync.sync_doctype(_client(session), FakeStore(fail_read=True), "ADFRAWD", now=NOW)
    assert res.error and session.calls == []


def test_a_rejected_key_stops_every_doctype():
    session = FakeSession({"ADFRAWD": _docs(3), "ADFREAD": _docs(3)})
    client = drs.DrsClient("revoked-key", contact_email=FAKE_CONTACT, session=session, sleep=lambda _s: None, retry_delay=0)
    report = sync.sync(client, FakeStore(), now=NOW)
    assert len(report.results) == 1 and report.results[0].auth_failed
    assert len(session.calls) == 1


def test_a_failed_link_is_an_error_but_the_cursor_already_moved():
    store = FakeStore(fail_link=True)
    report = sync.sync(_client(FakeSession({"ADFRAWD": _docs(3)})), store, ["ADFRAWD"], now=NOW)
    assert store.advances and report.link_error
    assert any("link_ads_to_drs" in e for e in report.errors)


# ------------------------------------------------------------ dedupe/skip --

def test_duplicate_guids_across_pages_are_sent_once():
    docs = _docs(760)
    docs.insert(755, docs[3])                  # offset shift: a row seen on page 1 reappears on page 2
    store = FakeStore()
    res = sync.sync_doctype(_client(FakeSession({"ADFRAWD": docs})), store, "ADFRAWD", now=NOW)
    guids = [d["document_guid"] for d in _sent(store)]
    assert len(guids) == len(set(guids)) == 760
    assert res.duplicates == 1


def test_an_invalid_document_is_skipped_reported_and_does_not_block_the_cursor():
    docs = _docs(3)
    docs[1] = {**docs[1], "drs:status": "Draft"}
    store = FakeStore()
    report = sync.sync(_client(FakeSession({"ADFRAWD": docs})), store, ["ADFRAWD"], now=NOW)
    assert len(_sent(store)) == 2
    assert store.advances
    assert len(report.errors) == 1 and "Draft" in report.errors[0]


# ------------------------------------------------------------------ link --

def test_link_gets_the_touched_ad_numbers_of_both_doctypes_in_batches(monkeypatch):
    monkeypatch.setattr(sync, "LINK_BATCH", 2)
    ead = load_fixture("adfread_documents.json")["documents"]
    session = FakeSession({"ADFRAWD": _docs(3), "ADFREAD": ead})
    store = FakeStore()
    report = sync.sync(_client(session), store, now=NOW)
    sent = [n for call in store.links for n in call["p_ad_numbers"]]
    assert sent == sorted({"2024-00-00", "2024-00-01", "2024-00-02", "2026-18-51", "2026-18-52"})
    assert [len(c["p_ad_numbers"]) for c in store.links] == [2, 2, 1]
    assert report.linked == 5
    assert [a["p_doctype"] for a in store.advances] == ["ADFRAWD", "ADFREAD"]


# ------------------------------------------------------ Supabase adapter --

class _Exec:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _Query:
    def __init__(self, sb, table):
        self.sb, self.table, self.filters = sb, table, []

    def select(self, cols):
        self.cols = cols
        return self

    def eq(self, col, value):
        self.filters.append((col, value))
        return self

    def execute(self):
        self.sb.reads.append((self.table, self.cols, self.filters))
        return _Exec(self.sb.state_rows)


class FakeSupabase:
    def __init__(self, state_rows=None, rpc_data=None):
        self.state_rows = state_rows or []
        self.rpc_data = rpc_data or {}
        self.rpcs, self.reads = [], []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return _Exec(self.rpc_data.get(name))


def test_the_supabase_store_sends_the_three_rpcs_exactly():
    sb = FakeSupabase(
        state_rows=[{"last_modified": "2026-09-18T14:28:27.52+00:00", "initial_scan_done": True}],
        rpc_data={"upsert_drs_documents": {"inserted": 1, "updated": 0, "unchanged": 0}, "link_ads_to_drs": 1},
    )
    store = sync.SupabaseDrsStore(sb)
    fresh = make_doc(_base(), "NEW", "2026-09-19T08:00:00.123Z", "2026-18-60")
    report = sync.sync(_client(FakeSession({"ADFRAWD": [fresh]})), store, ["ADFRAWD"], now=NOW)

    assert report.errors == []
    assert sb.reads == [("drs_sync_state", "last_modified, initial_scan_done", [("doctype", "ADFRAWD")])]
    names = [n for n, _ in sb.rpcs]
    assert names == ["upsert_drs_documents", "advance_drs_cursor", "link_ads_to_drs"]
    upsert = sb.rpcs[0][1]
    assert set(upsert) == {"p_doctype", "p_documents"} and upsert["p_doctype"] == "ADFRAWD"
    assert [set(d) for d in upsert["p_documents"]] == [RECORD_KEYS]
    assert sb.rpcs[1][1] == {"p_doctype": "ADFRAWD", "p_last_modified": "2026-09-19T08:00:00.123Z",
                             "p_initial_scan_done": True}
    assert sb.rpcs[2][1] == {"p_ad_numbers": ["2026-18-60"]}
    assert report.results[0].inserted == 1 and report.linked == 1


# ------------------------------------------------------------ populate_db --

def test_populate_db_does_nothing_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: pytest.fail("no DB when disabled"))
    assert populate_db.run_drs_sync(["ADFREAD"], env={"DRS_API_KEY": FAKE_KEY}) == 0


def test_populate_db_fails_when_enabled_without_a_key(monkeypatch):
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: pytest.fail("no DB without a key"))
    assert populate_db.run_drs_sync(["ADFREAD"], env={"DRS_API_ENABLED": "true"}) == 1


def test_populate_db_exit_code_follows_the_sync(monkeypatch):
    monkeypatch.setattr(populate_db, "_write_errors", [])
    ok = populate_db.run_drs_sync(["ADFRAWD"], client=_client(FakeSession({"ADFRAWD": _docs(2)})),
                                  store=FakeStore())
    assert ok == 0
    failing = populate_db.run_drs_sync(
        ["ADFRAWD"], client=_client(FakeSession({"ADFRAWD": _docs(2)}, script={1: 500, 2: 500})),
        store=FakeStore())
    assert failing == 1
    assert any("[DRS]" in e for e in populate_db._write_errors)


def test_dry_run_writes_nothing():
    store = sync.DryRunStore()
    report = sync.sync(_client(FakeSession({"ADFREAD": _docs(2)})), store, ["ADFREAD"], now=NOW)
    assert report.errors == [] and report.results[0].documents == 2


@pytest.mark.parametrize("argv", [
    ["--drs", "--source", "easa"], ["--drs", "--reset"], ["--drs", "--since", "2026-01-01"],
    ["--drs-doctype", "ADFREAD"],
])
def test_drs_flag_rejects_scraper_only_options(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["populate_db.py", *argv])
    with pytest.raises(SystemExit) as exc:
        populate_db.main()
    assert exc.value.code == 2
