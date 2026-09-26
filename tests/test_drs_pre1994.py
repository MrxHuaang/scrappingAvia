"""FAA ADs older than the Federal Register, from DRS into `ads`.

Fixture: real ADFRAWD documents (tests/fixtures/drs/adfrawd_pre1994.json),
trimmed. They cover the cases the backfill has to get right: one make with its
models, several makes (with repeats), no make, no publication or effective
date, Historical with a successor, and two ADs that are NOT pre-1994 but have
several documents (98-02-05 with two Current records, 2017-17-03 with a
Current and a newer Historical one).
"""

from datetime import datetime, timezone

import pytest

import populate_db
from drs_fakes import FAKE_KEY, FakeSession, FakeStore, load_fixture, make_doc
from scrapers import faa_drs_ads as pre
from scrapers import faa_drs_api as drs
from scrapers import faa_drs_sync as sync
from scrapers.common import http

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
PRE_1994 = ["41-47-01", "60-03-01", "65-06-04", "82-16-03", "84-18-03 R1", "85-14-02 R2", "91-20-02"]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)


def _docs():
    return load_fixture("adfrawd_pre1994.json")["documents"]


def _records(docs=None):
    return [sync.to_record(d, "ADFRAWD") for d in (docs if docs is not None else _docs())]


def _row(ad_number, records=None):
    groups = pre.group_by_ad(records if records is not None else _records())
    return pre.scraper_row(groups[ad_number])


def _normalized(ad_number):
    return populate_db.normalize(_row(ad_number), "federal_register")


# ------------------------------------------------------ which ADs qualify --

@pytest.mark.parametrize("number, year", [
    ("41-47-01", 1941), ("93-14-19R1", 1993), ("T91-11-51", 1991), ("85-14-02 R2", 1985),
    ("99-99-99 PL", 1999), ("05-01-01", 2005), ("2017-17-03", 2017), ("CF-2026-01", None), ("", None),
])
def test_the_year_comes_from_the_ad_number(number, year):
    assert pre.ad_year(number) == year


def test_exactly_the_pre_1994_ads_of_the_fixture_become_rows():
    rows = pre.pre_federal_register_rows(_records())
    assert [r["AD_Number"] for r in rows] == PRE_1994


def _one(number, publication_date):
    doc = make_doc(_docs()[0], f"G{number.replace(' ', '')}", "2024-05-30T19:48:53.441Z", number,
                   **{"drs:adfrawdCitationPublishDate": publication_date})
    return sync.to_record(doc, "ADFRAWD")


@pytest.mark.parametrize("number, published, qualifies", [
    ("85-05-03", None, True),             # no date: the number decides
    ("65-06-04", "1965-03-06", True),
    ("74-08-09 R3", "2012-02-22", False),  # a revision published in the FR era
    ("93-24-51", "1994-01-05", False),     # numbered in 1993, published in the FR API range
    ("94-03-09", "1993-02-17", False),     # the number already says 1994
    ("2001-01-01", None, False),
])
def test_an_ad_qualifies_by_number_and_publication_date(number, published, qualifies):
    assert pre.predates_federal_register([_one(number, published)]) is qualifies


def test_one_document_published_in_the_fr_era_excludes_the_whole_ad():
    old, new = _one("80-01-01", None), _one("80-01-01", "1995-06-01")
    new["document_guid"] = "OTHER"
    assert pre.predates_federal_register([old]) is True
    assert pre.predates_federal_register([old, new]) is False


# -------------------------------------------------- the representative doc --

def test_two_current_records_pick_the_same_one_as_link_ads_to_drs():
    docs = pre.group_by_ad(_records())["98-02-05"]
    assert [d["status"] for d in docs] == ["Current", "Current"]
    assert docs[0]["doc_last_modified"] == docs[1]["doc_last_modified"]
    # Same status and timestamp: the smaller guid, as in link_ads_to_drs.
    assert pre.pick_document(docs)["document_guid"] == min(d["document_guid"] for d in docs)
    assert pre.pick_document(list(reversed(docs)))["document_guid"] == min(d["document_guid"] for d in docs)


def test_current_wins_over_a_newer_historical_record():
    docs = pre.group_by_ad(_records())["2017-17-03"]
    historical = next(d for d in docs if d["status"] == "Historical")
    assert historical["doc_last_modified"] > next(d for d in docs if d["status"] == "Current")["doc_last_modified"]
    assert pre.pick_document(docs)["status"] == "Current"


def test_between_two_current_records_the_latest_modified_wins():
    a, b = _one("80-01-01", None), _one("80-01-01", None)
    b["document_guid"], b["doc_last_modified"] = "AAAA", "2024-05-30T19:48:53.442Z"
    a["doc_last_modified"] = "2025-01-01T00:00:00.000+00:00"   # as read back from the database
    assert pre.pick_document([b, a]) is a


# ------------------------------------------------------------ applicability --

def test_one_make_keeps_all_its_models_in_one_row():
    row = _row("91-20-02")
    doc = next(d for d in _docs() if d["drs:documentNumber"] == "91-20-02")
    assert row["Applicability"] == [{"make": "General Electric Company", "models": doc["drs:adfrawdModel"]}]
    assert row["Make"] == "General Electric Company"


def test_several_makes_are_one_row_each_without_models():
    row = _row("84-18-03 R1")
    assert row["Applicability"] == [
        {"make": "California Department of Forestry", "models": []},
        {"make": "International Helicopters, Inc.", "models": []},
        {"make": "Richards Heavylift Helo, Inc.", "models": []},
        {"make": "Rotorcraft Development Corporation", "models": []},
        {"make": "San Joaquin Helicopters", "models": []},
        {"make": "Tamarack Helicopters, Inc.", "models": []},
    ]
    assert row["Make"] == "California Department of Forestry"
    # The models still reach ads.model (the search field), just not paired.
    assert row["Model"] == "UH-1F, UH-1B, UH-1A, UH-1E, UH-1L"


def test_no_make_means_no_applicability_rows():
    row = _row("82-16-03")
    assert "Applicability" not in row and row["Make"] == ""


def test_makes_split_at_the_comma_are_glued_back_and_placeholders_dropped():
    assert pre.clean_makes(["Rockwell Collins", "Inc.", "N/A", "rockwell collins, inc."]) == ["Rockwell Collins, Inc."]
    assert pre.clean_makes(["Inc."]) == ["Inc."]   # nothing to glue it to
    assert pre.clean_makes(None) == []


def test_a_long_model_list_is_split_across_rows_of_the_same_make():
    models = [f"MODEL-{i:04d}-LONG-DESIGNATION" for i in range(300)]
    holders = pre.applicability(["Lycoming Engines"], models)
    assert len(holders) > 1 and {h["make"] for h in holders} == {"Lycoming Engines"}
    assert [m for h in holders for m in h["models"]] == models
    assert all(len(", ".join(h["models"])) <= pre.MODEL_TEXT_MAX for h in holders)


def test_the_model_limit_is_the_rpc_limit():
    assert pre.MODEL_TEXT_MAX == populate_db.APPLICABILITY_MODEL_MAX


# ------------------------------------------------------------ normalization --

def test_the_row_is_a_federal_register_row_with_the_drs_link():
    row = _normalized("41-47-01")
    doc = next(d for d in _docs() if d["drs:documentNumber"] == "41-47-01")
    assert row["source"] == "federal_register" and row["source_label"] == "FAA"
    assert row["issuing_authority"] == "federal_register"
    assert row["ad_number"] == "41-47-01"
    assert row["subject"] == "DOUGLAS Model DC3 Series Aircraft"
    assert row["pdf_link"] == doc["documentURL"]
    assert row["docket"] is None                       # DRS says "Unknown"
    assert row["published_date"] is None and row["effective_date"] is None
    assert row["status"] == "New" and row["product_type"] == "Aircraft"
    assert row["raw_data"]["Origin"] == pre.ORIGIN
    assert row["raw_data"]["DRS_Document_Guid"] == doc["documentGuid"]


def test_dates_travel_when_drs_has_them():
    row = _normalized("65-06-04")
    assert row["published_date"] == "1965-03-06" and row["effective_date"] == "1965-03-06"
    assert row["product_type"] == "Appliance"
    assert _normalized("60-03-01")["effective_date"] is None


def test_a_historical_ad_with_a_successor_is_superseded():
    row = _normalized("91-20-02")
    assert row["status"] == "Superseded"
    assert row["raw_data"]["Superseded_By"] == "2014-21-01"
    assert row["raw_data"]["DRS_Status"] == "Historical"
    assert row["docket"] == "91-ANE-04" and row["product_type"] == "Engine"


def test_a_historical_ad_without_a_successor_has_no_canonical_status():
    doc = dict(next(d for d in _docs() if d["drs:documentNumber"] == "60-03-01"), **{"drs:status": "Historical"})
    row = populate_db.normalize(pre.scraper_row(_records([doc])), "federal_register")
    assert row["status"] is None


def test_rotorcraft_is_a_helicopter_and_title_escapes_are_flattened():
    assert _normalized("84-18-03 R1")["product_type"] == "Helicopter"
    subject = _normalized("85-14-02 R2")["subject"]
    assert subject.startswith("GARRETT AUXILIARY POWER DIVISION (FORMERLY GARRETT TURBINE ENGINE COMPANY")
    assert "\\n" not in subject and "Airworthiness Directives" not in subject


def test_applicability_payload_follows_the_contract(monkeypatch):
    """One make -> listed rows; several makes -> unknown rows; no make -> failed."""
    sent = []

    class _Rpc:
        def __init__(self, batch):
            self.batch = batch

        def execute(self):
            sent.extend(self.batch)
            return type("R", (), {"data": [{"ad_id": e["ad_id"], "status": e["status"]} for e in self.batch]})()

    class _Table:
        def __getattr__(self, _name):
            return lambda *a, **k: self

        def execute(self):
            return type("R", (), {"data": []})()

    class _Sb:
        def table(self, _name):
            return _Table()

        def rpc(self, name, params):
            assert name == "replace_ad_applicability"
            return _Rpc(params["p_ads"])

    rows = [_normalized(n) for n in ("91-20-02", "84-18-03 R1", "82-16-03")]
    ids = {r["ad_number"]: f"id-{r['ad_number']}" for r in rows}
    monkeypatch.setattr(populate_db, "_fetch_ad_ids", lambda _sb, _src, nums: {n: ids[n] for n in nums if n in ids})
    populate_db.populate_relations(_Sb(), "federal_register", rows)
    by_ad = {e["ad_id"]: e for e in sent}
    single = by_ad["id-91-20-02"]
    assert single["status"] == "success_with_rows" and len(single["rows"]) == 1
    assert single["rows"][0]["model_status"] == "listed" and single["rows"][0]["confidence"] == "high"
    multi = by_ad["id-84-18-03 R1"]
    assert multi["status"] == "success_with_rows" and len(multi["rows"]) == 6
    assert {r["model_status"] for r in multi["rows"]} == {"unknown"}
    assert all(r["model"] is None for r in multi["rows"])
    assert by_ad["id-82-16-03"] == {"ad_id": "id-82-16-03", "status": "failed", "rows": []}


# --------------------------------------------------------------- the writer --

class _AdsRead:
    """`ads` rows the ownership guard sees: ad_number -> raw_data.Origin."""

    def __init__(self, existing, fail=False):
        self.existing, self.fail, self.queries = existing, fail, []

    def table(self, name):
        assert name == "ads"
        return _AdsQuery(self)


class _AdsQuery:
    def __init__(self, sb):
        self.sb, self.filters = sb, {}

    def select(self, cols):
        self.filters["select"] = cols
        return self

    def eq(self, col, value):
        self.filters[col] = value
        return self

    def in_(self, col, values):
        self.filters[col] = list(values)
        return self

    def execute(self):
        self.sb.queries.append(self.filters)
        if self.sb.fail:
            raise RuntimeError("ads unreachable")
        rows = [{"ad_number": n, "origin": o} for n, o in self.sb.existing.items()
                if n in self.filters["ad_number"]]
        return type("R", (), {"data": rows})()


@pytest.fixture()
def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(populate_db, "_write_errors", [])
    monkeypatch.setattr(populate_db, "upsert_ads",
                        lambda _sb, rows: calls.append(("upsert_ads", [r["ad_number"] for r in rows])) or (len(rows), 0))
    monkeypatch.setattr(populate_db, "populate_relations",
                        lambda _sb, source, rows: calls.append(("relations", source, [r["ad_number"] for r in rows])))
    return calls


def _stored():
    store = FakeStore()
    store.upsert("ADFRAWD", _records())
    return store


def test_the_backfill_writes_only_pre_1994_ads_through_the_faa_path(writes):
    sb = _AdsRead({})
    counts = populate_db.backfill_pre_federal_register(sb, _stored(), [r["ad_number"] for r in _records()])
    assert writes == [("upsert_ads", PRE_1994), ("relations", "federal_register", PRE_1994)]
    assert counts == {"candidates": 7, "rows": 7, "skipped_existing": 0, "new": 7, "updated": 0}
    assert sb.queries[0]["select"] == "ad_number, origin:raw_data->>Origin"
    assert sb.queries[0]["source"] == "federal_register"


def test_a_row_the_federal_register_scraper_created_is_never_overwritten(writes):
    sb = _AdsRead({"41-47-01": None, "60-03-01": pre.ORIGIN})
    counts = populate_db.backfill_pre_federal_register(sb, _stored(), PRE_1994)
    assert "41-47-01" not in writes[0][1] and "60-03-01" in writes[0][1]
    assert counts["skipped_existing"] == 1 and counts["rows"] == 7


def test_without_the_ownership_read_nothing_is_written(writes):
    counts = populate_db.backfill_pre_federal_register(_AdsRead({}, fail=True), _stored(), PRE_1994)
    assert writes == [] and counts["new"] == 0
    assert any("no se pudo leer ads" in e for e in populate_db._write_errors)


def test_only_candidate_numbers_are_read_from_drs_documents(writes):
    store = _stored()
    populate_db.backfill_pre_federal_register(_AdsRead({}), store, ["2017-17-03", "98-02-05", "41-47-01"])
    assert store.document_reads == [{"doctype": "ADFRAWD", "ad_numbers": ["41-47-01"]}]


def test_nothing_to_do_reads_nothing(writes):
    store = _stored()
    counts = populate_db.backfill_pre_federal_register(None, store, ["2017-17-03"])
    assert store.document_reads == [] and writes == [] and counts["candidates"] == 0


def test_a_dry_run_counts_without_a_database(writes):
    counts = populate_db.backfill_pre_federal_register(None, _stored(), PRE_1994, dry_run=True)
    assert writes == [] and counts["rows"] == 7


# ------------------------------------------------------------- in the sync --

def _client(session):
    return drs.DrsClient(FAKE_KEY, session=session, sleep=lambda _s: None, retry_delay=0, retries=2)


def test_the_sync_creates_the_rows_after_upserting_and_before_linking(writes, monkeypatch):
    events = []
    store = FakeStore()
    real_upsert, real_link = store.upsert, store.link
    monkeypatch.setattr(store, "upsert", lambda *a: events.append("upsert_drs_documents") or real_upsert(*a))
    monkeypatch.setattr(store, "link", lambda nums: events.append("link_ads_to_drs") or real_link(nums))
    monkeypatch.setattr(populate_db, "upsert_ads",
                        lambda _sb, rows: events.append("upsert_ads") or (len(rows), 0))
    code = populate_db.run_drs_sync(["ADFRAWD"], client=_client(FakeSession({"ADFRAWD": _docs()})),
                                    store=store, supabase=_AdsRead({}))
    assert code == 0
    assert events == ["upsert_drs_documents", "upsert_ads", "link_ads_to_drs"]
    assert set(PRE_1994) <= set(store.links[0]["p_ad_numbers"])


def test_a_backfill_failure_turns_the_run_red_but_still_links(writes, monkeypatch):
    def boom(_sb, _rows):
        raise RuntimeError("ads upsert failed")

    monkeypatch.setattr(populate_db, "upsert_ads", boom)
    store = FakeStore()
    code = populate_db.run_drs_sync(["ADFRAWD"], client=_client(FakeSession({"ADFRAWD": _docs()})),
                                    store=store, supabase=_AdsRead({}))
    assert code == 1 and store.links
    assert any("ads upsert failed" in e for e in populate_db._write_errors)


def test_a_dry_run_sync_builds_the_rows_in_memory(writes):
    code = populate_db.run_drs_sync(["ADFRAWD"], dry_run=True,
                                    client=_client(FakeSession({"ADFRAWD": _docs()})))
    assert code == 0 and writes == []


def test_the_dry_run_store_answers_documents_from_what_it_would_upload():
    store = sync.DryRunStore()
    sync.sync(_client(FakeSession({"ADFRAWD": _docs()})), store, ["ADFRAWD"], now=NOW)
    got = store.documents("ADFRAWD", ["98-02-05", "41-47-01"])
    assert sorted(d["ad_number"] for d in got) == ["41-47-01", "98-02-05", "98-02-05"]
    assert store.documents("ADFREAD", ["41-47-01"]) == []


def test_the_supabase_store_reads_drs_documents_in_batches():
    reads = []

    class _Q:
        def __init__(self, table):
            self.q = {"table": table}

        def select(self, cols):
            self.q["select"] = cols
            return self

        def eq(self, col, value):
            self.q[col] = value
            return self

        def in_(self, col, values):
            self.q[col] = list(values)
            return self

        def execute(self):
            reads.append(self.q)
            return type("R", (), {"data": [{"ad_number": n} for n in self.q["ad_number"]]})()

    class _Sb:
        def table(self, name):
            return _Q(name)

    numbers = [f"80-{i // 100:02d}-{i % 100:02d}" for i in range(260)]
    got = sync.SupabaseDrsStore(_Sb()).documents("ADFRAWD", numbers + numbers[:5])
    assert len(got) == 260
    assert [len(r["ad_number"]) for r in reads] == [sync.READ_BATCH, 260 - sync.READ_BATCH]
    assert reads[0]["table"] == "drs_documents" and reads[0]["doctype"] == "ADFRAWD"
    assert reads[0]["select"] == sync.DOCUMENT_COLUMNS
    assert {c.strip() for c in sync.DOCUMENT_COLUMNS.split(",")} == {
        "document_guid", "document_number", "ad_number", "status", "doc_last_modified", "publication_date",
        "effective_date", "document_url", "main_download_url", "makes", "models", "regulatory_text", "payload"}
