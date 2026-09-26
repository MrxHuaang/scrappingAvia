"""Regulatory text from DRS as sections, for FAA ADs without GovInfo text.

Fixture: the untrimmed `drs:adfrawdRegulatoryText` of real ADFRAWD documents
(tests/fixtures/drs/regulatory_text.json) in every form DRS uses: real line
breaks, literal "\\n"/"\\t" escapes with "((Page N))" marks, and the one-line
text of 2021 onwards; headers with "Amendment", "Amdt." or no amendment at
all, and a letter AD whose DRS number carries an "L" the text does not repeat.
"""

import hashlib

import pytest

import enrich
import populate_db
from drs_fakes import FakeStore, load_fixture
from scrapers import faa_drs_ads as pre
from utils import normalize_ad_number


def _text(number):
    return next(d for d in load_fixture("regulatory_text.json")["documents"] if d["document_number"] == number)


def _split(number, expected=None):
    return enrich.split_ad_sections(_text(number)["regulatory_text"],
                                    normalize_ad_number(expected or number), plain=True)


def _kinds(result):
    return [s["kind"] for s in result["sections"]]


def _complete(result):
    kinds = set(_kinds(result))
    return "applicability" in kinds and "unsafe_condition" in kinds and bool(
        kinds & {"compliance", "required_actions"})


# ------------------------------------------------------------ the parser --

def test_a_1941_ad_without_amendment_is_anchored_on_its_number():
    r = _split("41-47-01")
    assert r["status"] == "extracted"
    assert r["anchor"] == "41-47-01 DOUGLAS: (Was Service Note 3 of AD-618-3 and Service Note 3 of AD-669-3.)"
    assert r["sections"][0]["kind"] == "applicability"
    assert r["sections"][0]["body"].startswith("Applies to All DC3 Series Aircraft.")
    assert not _complete(r)   # the AD states no unsafe condition: partial, decided by the database


def test_a_1965_ad_with_amdt_and_phrase_labels_is_complete():
    r = _split("65-06-04")
    assert r["anchor"].startswith("65-06-04 EUREKA WILLIAMS: Amdt. 39-44")
    assert _kinds(r) == ["applicability", "compliance", "unsafe_condition", "required_actions", "required_actions"]
    assert r["sections"][1]["body"].startswith("Compliance required within 30 days")
    assert [s["letter"] for s in r["sections"]][3:] == ["a", "b"]


def test_literal_escapes_become_line_breaks_and_labels_are_read():
    r = _split("85-14-02 R2")
    assert r["status"] == "extracted" and _complete(r)
    assert "\\n" not in " ".join(s["body"] for s in r["sections"])
    assert r["sections"][0]["body"].startswith("All GTEC Models GTCP331-200A")


def test_applicability_inside_the_header_paragraph_is_split_off():
    r = _split("82-16-03")
    assert r["anchor"] == "82-16-03 FOKKER B.V.: Amendment 39-4425."
    assert r["sections"][0]["kind"] == "applicability"
    assert r["sections"][0]["body"].startswith("Applies to Model F28 Series 1000 and 4000 airplanes")


def test_a_letter_ad_is_anchored_without_its_drs_suffix():
    r = _split("86-16-11 L")
    assert r["status"] == "extracted" and _complete(r)
    assert r["anchor"].startswith("86-16-11 BELL HELICOPTER TEXTRON, INC.:")


def test_the_2000s_layout_with_plain_headings_reads_as_in_govinfo():
    r = _split("2010-17-14")
    assert _complete(r)
    assert [s["heading"] for s in r["sections"]][:6] == [
        "Effective Date", "Affected ADs", "Applicability", "Subject", "Unsafe Condition", "Compliance"]


def test_page_marks_in_parentheses_are_dropped():
    r = _split("2016-25-02")
    assert _complete(r)
    assert not any("((Page" in s["body"] for s in r["sections"])


def test_a_one_line_text_gets_one_section_per_lettered_paragraph():
    r = _split("2026-18-10")
    assert r["anchor"] == ("2026-18-10 Bell Textron Canada Limited: Amendment 39-23465; Docket No. "
                           "FAA-2026-4658; Project Identifier MCAI-2026-00014-R.")
    assert [(s["letter"], s["heading"]) for s in r["sections"]] == [
        ("a", "Effective Date"), ("b", "Affected ADs"), ("c", "Applicability"), ("d", "Subject"),
        ("e", "Unsafe Condition"), ("f", "Compliance"), ("g", "Required Actions"),
        ("h", "Exceptions to Transport Canada AD CF-2026-01"),
        ("i", "Alternative Methods of Compliance (AMOCs)"), ("j", "Additional Information"),
        ("k", "Material Incorporated by Reference"),
    ]
    assert r["sections"][0]["body"] == "This airworthiness directive (AD) is effective October 22, 2026."
    assert r["sections"][1]["body"] == "None."
    assert _complete(r)


def test_a_text_that_never_names_its_ad_is_unanchored():
    # The 2026-19-01 header is "MD Helicopters, LLC: Docket No. ...", with no AD number.
    assert _split("2026-19-01") == {"status": "unanchored", "anchor": None, "sections": []}


def test_another_ads_number_does_not_anchor():
    assert _split("65-06-04", expected="65-06-05")["status"] == "unanchored"
    assert _split("2026-18-10", expected="2026-18-01")["status"] == "unanchored"


def test_an_empty_text_is_empty():
    assert _split("44-04-03")["status"] == "empty"
    assert enrich.split_ad_sections("   ", "44-04-03", plain=True)["status"] == "empty"


def test_the_plain_normalizer():
    raw = "85-01-01  ACME:  Amendment 39-1. \\n\\n\\tApplicability:  All. ((Page 12)) \\n"
    assert enrich.plain_regulatory_text(raw) == "85-01-01  ACME:  Amendment 39-1.\n\n\tApplicability:  All.\n"


def test_the_plain_mode_is_opt_in():
    """GovInfo input keeps its own mode: without plain=True the one-line
    DRS text is not restructured and does not anchor."""
    assert enrich.split_ad_sections(_text("2026-18-10")["regulatory_text"], "2026-18-10")["status"] == "unanchored"


# ------------------------------------------------------------- payload --

def _record(number, **extra):
    d = _text(number)
    rec = {"document_guid": d["document_guid"], "document_url": d["document_url"],
           "ad_number": normalize_ad_number(number), "document_number": number, "status": "Current",
           "doc_last_modified": "2024-05-30T19:48:53.441Z", "regulatory_text": d["regulatory_text"],
           "publication_date": None, "effective_date": None, "main_download_url": None,
           "makes": [], "models": [], "payload": {}}
    rec.update(extra)
    return rec


def test_the_attempt_carries_the_drs_provenance():
    rec = _record("65-06-04")
    attempt, sections, problem = populate_db._drs_text_payload(rec)
    assert problem is None
    assert attempt == {
        "status": "extracted", "provider": "drs", "document_id": rec["document_guid"],
        "source_url": rec["document_url"],
        "sha256": hashlib.sha256(rec["regulatory_text"].encode("utf-8")).hexdigest(),
        "anchor": _split("65-06-04")["anchor"], "detail": None,
    }
    assert [s["position"] for s in sections] == [1, 2, 3, 4, 5]
    assert set(sections[0]) == {"position", "letter", "heading", "kind", "body"}


def test_a_failed_attempt_has_no_sections_and_says_why():
    attempt, sections, _ = populate_db._drs_text_payload(_record("2026-19-01"))
    assert attempt["status"] == "unanchored" and sections == [] and attempt["anchor"] is None
    assert attempt["detail"] == "no regulatory header in the DRS text declares AD 2026-19-01"
    attempt, sections, _ = populate_db._drs_text_payload(_record("44-04-03"))
    assert attempt["status"] == "empty" and attempt["sha256"] is None and sections == []


def test_oversized_sections_travel_as_an_error(monkeypatch):
    monkeypatch.setattr(populate_db, "TEXT_BODY_MAX", 10)
    attempt, sections, problem = populate_db._drs_text_payload(_record("65-06-04"))
    assert attempt["status"] == "error" and sections == [] and "field limits" in problem


def test_drs_is_provider_the_rpc_accepts():
    # ad-scrapper 20260927100000_ad_text_documents.sql: provider in ('govinfo', 'drs').
    assert populate_db.TEXT_PROVIDER_DRS == "drs" and populate_db.TEXT_PROVIDER == "govinfo"


@pytest.mark.parametrize("kept, sha, send", [
    (None, "s1", True),                                                  # never attempted
    ({"text_sha256": None, "text_provider": None}, "s1", True),          # attempts, nothing kept
    ({"text_sha256": "g", "text_provider": "govinfo"}, "s1", False),     # GovInfo text wins
    ({"text_sha256": "s1", "text_provider": "drs"}, "s1", False),        # same DRS text
    ({"text_sha256": "s0", "text_provider": "drs"}, "s1", True),         # DRS text changed
])
def test_when_a_drs_attempt_is_sent(kept, sha, send):
    assert populate_db._drs_text_needs_send(kept, sha) is send


# ---------------------------------------------------------- the writer --

class _TextDb:
    """ads ids, ad_text_documents (with provider) and the RPC rule."""

    def __init__(self, ids, documents=None):
        self.ids, self.documents = ids, {k: dict(v) for k, v in (documents or {}).items()}
        self.calls, self.sections = [], {}

    def table(self, name):
        return _TextQuery(self, name)

    def rpc(self, name, params):
        assert name == "replace_ad_text_sections"
        self.calls.append(params)
        db = self

        class _Call:
            def execute(self_inner):
                return type("R", (), {"data": db._replace(params)})()
        return _Call()

    def _replace(self, params):
        ad_id, attempt, sections = params["p_ad_id"], params["p_attempt"], params["p_sections"]
        kinds = {s["kind"] for s in sections}
        complete = "applicability" in kinds and "unsafe_condition" in kinds and bool(
            kinds & {"compliance", "required_actions"})
        status = attempt["status"] if attempt["status"] != "extracted" else ("ok" if complete else "partial")
        doc = self.documents.setdefault(ad_id, {"text_sha256": None, "text_provider": None})
        if status == "ok" or (status == "partial" and not doc["text_sha256"]):
            self.sections[ad_id] = sections
            doc.update(text_sha256=attempt["sha256"], text_provider=attempt["provider"])
        return status


class _TextQuery:
    def __init__(self, db, table):
        self.db, self.table, self.values = db, table, []

    def select(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def in_(self, _col, values):
        self.values = list(values)
        return self

    def execute(self):
        if self.table == "ads":
            data = [{"id": self.db.ids[n], "ad_number": n} for n in self.values if n in self.db.ids]
        else:
            data = [{"ad_id": a, **self.db.documents[a]} for a in self.values if a in self.db.documents]
        return type("R", (), {"data": data})()


def _store(*records):
    store = FakeStore()
    store.upsert("ADFRAWD", list(records))
    return store


@pytest.fixture()
def clean(monkeypatch):
    monkeypatch.setattr(populate_db, "_write_errors", [])


def test_drs_text_goes_only_where_govinfo_has_none(clean):
    records = [_record(n) for n in ("41-47-01", "65-06-04", "2010-17-14", "2026-18-10", "86-16-11 L")]
    db = _TextDb(
        ids={"41-47-01": "id-41", "65-06-04": "id-65", "2010-17-14": "id-2010", "2026-18-10": "id-2026"},
        documents={"id-2010": {"text_sha256": "govinfo-sha", "text_provider": "govinfo"},
                   "id-2026": {"text_sha256": None, "text_provider": None}},
    )
    counts = populate_db.populate_drs_text_sections(db, _store(*records), [r["ad_number"] for r in records])
    sent = {c["p_ad_id"]: c["p_attempt"]["status"] for c in db.calls}
    assert sent == {"id-41": "extracted", "id-65": "extracted", "id-2026": "extracted"}
    assert db.documents["id-2010"] == {"text_sha256": "govinfo-sha", "text_provider": "govinfo"}
    assert db.documents["id-65"]["text_provider"] == "drs"
    assert counts["ok"] == 2 and counts["partial"] == 1        # 41-47-01 has no unsafe condition
    assert counts["text_already_kept"] == 1 and counts["not_in_ads"] == 1
    assert counts["parsed_extracted"] == 5
    assert populate_db._write_errors == []


def test_the_same_drs_text_is_not_sent_twice(clean):
    rec = _record("65-06-04")
    db = _TextDb(ids={"65-06-04": "id-65"})
    populate_db.populate_drs_text_sections(db, _store(rec), ["65-06-04"])
    populate_db.populate_drs_text_sections(db, _store(rec), ["65-06-04"])
    assert len(db.calls) == 1
    changed = _record("65-06-04", regulatory_text=rec["regulatory_text"] + " ")
    populate_db.populate_drs_text_sections(db, _store(changed), ["65-06-04"])
    assert len(db.calls) == 2


def test_the_text_comes_from_the_representative_document(clean):
    current = _record("65-06-04")
    historical = _record("65-06-04", document_guid="AAAA", status="Historical",
                         doc_last_modified="2025-08-07T14:49:20.811Z", regulatory_text="garbage")
    db = _TextDb(ids={"65-06-04": "id-65"})
    populate_db.populate_drs_text_sections(db, _store(historical, current), ["65-06-04"])
    assert db.calls[0]["p_attempt"]["document_id"] == current["document_guid"]
    assert pre.pick_document([historical, current]) is current


def test_an_unanchored_attempt_is_recorded_without_sections(clean):
    db = _TextDb(ids={"2026-19-01": "id-x"})
    counts = populate_db.populate_drs_text_sections(db, _store(_record("2026-19-01")), ["2026-19-01"])
    assert db.calls[0]["p_attempt"]["status"] == "unanchored" and db.calls[0]["p_sections"] == []
    assert counts["unanchored"] == 1 and "id-x" not in db.sections


def test_a_dry_run_parses_without_a_database(clean):
    records = [_record(n) for n in ("65-06-04", "2026-19-01", "44-04-03")]
    counts = populate_db.populate_drs_text_sections(None, _store(*records), [r["ad_number"] for r in records],
                                                    dry_run=True)
    assert counts == {"parsed_extracted": 1, "parsed_unanchored": 1, "parsed_empty": 1}


def test_the_sync_sends_text_after_creating_the_pre_1994_rows(clean, monkeypatch):
    from drs_fakes import FAKE_CONTACT, FAKE_KEY, FakeSession
    from scrapers import faa_drs_api as drs
    from scrapers.common import http

    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    events = []
    monkeypatch.setattr(populate_db, "backfill_pre_federal_register",
                        lambda *a, **k: events.append("backfill") or {})
    monkeypatch.setattr(populate_db, "populate_drs_text_sections",
                        lambda *a, **k: events.append("text") or {})
    store = FakeStore()
    real_link = store.link
    monkeypatch.setattr(store, "link", lambda nums: events.append("link") or real_link(nums))
    docs = load_fixture("adfrawd_pre1994.json")["documents"]
    client = drs.DrsClient(FAKE_KEY, contact_email=FAKE_CONTACT, session=FakeSession({"ADFRAWD": docs}), sleep=lambda _s: None,
                           retry_delay=0, retries=2)
    assert populate_db.run_drs_sync(["ADFRAWD"], client=client, store=store, supabase=_TextDb({})) == 0
    assert events == ["backfill", "text", "link"]
