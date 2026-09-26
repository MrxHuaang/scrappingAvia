"""TCDS holders and former holders as manufacturer alias candidates.

Offline: real TCDSMODEL documents (tests/fixtures/drs/tcdsmodel_documents.json,
lists trimmed) and a small groups file written here in the format of
ad-scrapper's docs/data/manufacturer-groups.csv. The script only reads that
file and writes a CSV report; the tests check both.
"""

import csv

import pytest

from drs_fakes import FAKE_CONTACT, FAKE_KEY, FakeSession, load_fixture, make_doc
from scripts import tcds_alias_candidates as tcds
from scrapers import faa_drs_api as drs
from scrapers import faa_drs_sync as sync
from scrapers.common import http

GROUPS = """root_canonical,member_canonical,kind,"aliases","note"
Rolls-Royce,Rolls-Royce,oem,"Rolls-Royce|Rolls-Royce Corporation|Rolls-Royce plc","Engines."
Rolls-Royce,Allison,former,"Allison|Allison Engine Company","Allison Engine Company was bought by Rolls-Royce in 1995."
Bell,Bell Textron Canada,division,"Bell Textron Canada Limited|Bell Helicopter Textron Canada Limited","Canadian division."
Continental Aerospace Technologies,Continental Motors,former,"Continental Motors|Teledyne Continental Motors","Continental Copters Inc. is deliberately kept out: another company."
"""


@pytest.fixture()
def groups_file(tmp_path):
    path = tmp_path / "manufacturer-groups.csv"
    path.write_text(GROUPS, encoding="utf-8")
    return path


@pytest.fixture()
def rows(groups_file):
    docs = load_fixture("tcdsmodel_documents.json")["documents"]
    return {r["name"]: r for r in tcds.report_rows(tcds.collect(docs), tcds.Groups.load(groups_file))}


def test_name_keys_drop_accents_punctuation_and_company_suffixes():
    assert tcds.name_key("Messerschmitt-Bölkow-Blohm GmbH") == "messerschmitt bolkow blohm"
    assert tcds.name_key("The Boeing Company") == "boeing"
    assert tcds.name_key("Partenavia Costruzioni Aeronautiche S.p.A.") == "partenavia costruzioni aeronautiche"
    assert tcds.name_key("Pratt & Whitney Canada Corp.") == "pratt and whitney canada"


def test_the_brand_is_the_first_distinctive_word_only():
    assert tcds.brand("allison gas turbine division of general motors") == "allison"
    assert tcds.brand("helicopter transport services") is None
    assert tcds.brand("") is None


def test_an_exact_alias_is_known_with_its_member(rows):
    row = rows["Allison Engine Company Inc."]
    assert row["status"] == "known" and row["group_roots"] == "Rolls-Royce"
    assert (row["matched_member"], row["matched_alias"]) == ("Allison", "Allison Engine Company")
    assert rows["Rolls-Royce Corporation"]["status"] == "known"
    assert rows["Bell Textron Canada Limited"]["status"] == "known"


def test_a_new_spelling_of_a_known_brand_is_flagged(rows):
    row = rows["Rolls-Royce Corporation (Indiana)"]
    assert row["status"] == "new_spelling" and row["group_roots"] == "Rolls-Royce"
    # Same key as a known alias once punctuation and "Limited" are dropped.
    bell = rows["Bell Helicopter Textron (Canada)"]
    assert bell["status"] == "known" and bell["matched_alias"] == "Bell Helicopter Textron Canada Limited"


def test_a_former_holder_shows_who_holds_the_sheets_now(rows):
    row = rows["Allison Gas Turbine Division of General Motors"]
    assert row["roles"] == "former_holder"
    assert row["successor_holders"] == "Allison Engine Company Inc.|Rolls-Royce Corporation"
    assert row["successor_roots"] == "Rolls-Royce"
    assert row["tcds_numbers"] == "TE1CH|TE6CH" and row["n_tcds"] == 2
    assert row["in_current_tcds"] == "yes"


def test_chained_former_holders_are_separate_names(rows):
    assert "Bell Helicopter Textron (Canada)" in rows
    assert "Bell Helicopter Textron Canada Limited" in rows
    assert not any("\\" in name for name in rows)


def test_a_name_the_curator_ruled_on_is_pointed_out(rows):
    row = rows["Continental Copters Inc."]
    # Same first word as Continental: only a hint, and the note says why it is not grouped.
    assert row["status"] == "new_spelling"
    assert row["mentioned_in_notes"] == "Continental Aerospace Technologies"


def test_a_name_nothing_relates_is_unknown(rows):
    row = rows["Tom Cat Helicopter, Inc."]
    assert row["status"] == "unknown" and row["group_roots"] == ""
    assert row["successor_holders"] == "Continental Copters Inc." and row["successor_roots"] == ""


def test_a_former_holder_of_a_grouped_holder_is_a_candidate_even_without_a_shared_brand(groups_file):
    base = load_fixture("tcdsmodel_documents.json")["documents"][1]
    doc = dict(base, **{"drs:tcdsmodelFormerHolders": ["Detroit Diesel Allison"]})
    row = {r["name"]: r for r in tcds.report_rows(tcds.collect([doc]), tcds.Groups.load(groups_file))}
    assert row["Detroit Diesel Allison"]["status"] == "new_spelling"
    assert row["Detroit Diesel Allison"]["group_roots"] == "Rolls-Royce"


def test_suffixes_split_at_the_comma_and_empty_sentinels_are_cleaned(groups_file):
    doc = {"drs:documentNumber": "A1XX", "drs:status": "Current", "drs:tcdsmodelTCHolder": "Acme Aircraft",
           "drs:tcdsmodelFormerHolders": ["Acme Aviation", "Inc.", "_EMPTY_", "Acme Aviation", "Inc."]}
    names = set(tcds.collect([doc]))
    assert names == {"Acme Aircraft", "Acme Aviation, Inc."}


def test_rows_are_ordered_for_review(rows):
    order = [r["status"] for r in sorted(rows.values(), key=lambda r: tcds._STATUS_ORDER[r["status"]])]
    listed = [r["status"] for r in rows.values()]
    assert listed == order and listed[0] == "new_spelling" and listed[-1] == "known"


def test_main_writes_the_report_and_leaves_the_seed_untouched(groups_file, tmp_path):
    before = groups_file.read_bytes()
    out = tmp_path / "report.csv"
    source = tmp_path / "tcds.json"
    source.write_text((tcds.ROOT / "tests" / "fixtures" / "drs" / "tcdsmodel_documents.json").read_text(
        encoding="utf-8"), encoding="utf-8")
    assert tcds.main(["--input", str(source), "--groups", str(groups_file), "--out", str(out)]) == 0
    assert groups_file.read_bytes() == before
    with open(out, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        assert tuple(reader.fieldnames) == tcds.CSV_COLUMNS
        assert len(list(reader)) == 9


def test_main_needs_the_flag_to_call_the_api(groups_file, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DRS_API_ENABLED", raising=False)
    monkeypatch.setenv("DRS_API_KEY", FAKE_KEY)
    assert tcds.main(["--groups", str(groups_file), "--out", str(tmp_path / "r.csv")]) == 1
    assert "DRS_API_ENABLED" in capsys.readouterr().err
    assert not (tmp_path / "r.csv").exists()


# --------------------------------------------------------------- the API --

def _client(session):
    return drs.DrsClient(FAKE_KEY, contact_email=FAKE_CONTACT, session=session, sleep=lambda _s: None, retry_delay=0, retries=2)


def test_fetch_pages_tcdsmodel_read_only_and_keeps_only_the_report_fields(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    base = load_fixture("tcdsmodel_documents.json")["documents"][0]
    corpus = [make_doc(dict(base, **{"drs:tcdsmodelModelComments": ["long text"]}), f"G{i:04d}",
                       "2024-05-30T19:48:53.441Z") for i in range(5)]
    session = FakeSession({"TCDSMODEL": corpus}, page_size=2)
    docs = tcds.fetch(_client(session))
    assert len(docs) == 5 and {c["method"] for c in session.calls} == {"GET"}
    assert [c["params"]["offset"] for c in session.calls] == [0, 2, 4]
    assert all(c["url"].endswith("/TCDSMODEL") for c in session.calls)
    assert set(docs[0]) <= set(tcds.FIELDS) and "drs:tcdsmodelModelComments" not in docs[0]


def test_fetch_stops_at_the_page_budget(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    base = load_fixture("tcdsmodel_documents.json")["documents"][0]
    corpus = [make_doc(base, f"G{i:04d}", "2024-05-30T19:48:53.441Z") for i in range(5)]
    with pytest.raises(drs.DrsRequestError, match="more than 2 pages"):
        tcds.fetch(_client(FakeSession({"TCDSMODEL": corpus}, page_size=2)), max_pages=2)


def test_tcdsmodel_is_readable_but_not_synced():
    assert "TCDSMODEL" in drs.READ_DOCTYPES and "TCDSMODEL" not in drs.DOCTYPES
    assert tuple(sync.DOCTYPES) == ("ADFRAWD", "ADFREAD")
    with pytest.raises(ValueError):
        _client(FakeSession()).fetch_page("NOSUCH")
