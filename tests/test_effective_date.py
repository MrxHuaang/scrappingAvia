"""Fecha efectiva FAA desde el rule y scripts/backfill_effective_date.py.

Sin red ni base: granulos reales de `fixtures/fr_text/` y un cliente falso.
La fecha efectiva es la que declara la sección Effective Date del texto
anclado, si no el `effective_on` de la FR API, si no NULL: nunca la fecha de
publicación.
"""

import io
import json
import os

import pytest

import enrich
import populate_db
from scrapers import federal_register as fr
from scripts import backfill_effective_date as bf

_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
with open(os.path.join(_DIR, "fr_text.json"), encoding="utf-8") as _f:
    FIXTURES = {e["file"]: e for e in json.load(_f)}


def _split(file: str) -> dict:
    e = FIXTURES[file]
    with open(os.path.join(_DIR, "fr_text", file), encoding="utf-8") as f:
        return enrich.split_ad_sections(f.read(), e.get("expected_ad"), e.get("expected_amendment"))


@pytest.mark.parametrize("file,expected", [
    ("2024-08105.htm", "2024-05-22"),   # "is effective May 22, 2024."
    ("2024-00993.htm", "2024-01-18"),   # emergencia: la frase con fecha, no "was effective for ..."
    ("2015-06235.htm", "2015-03-30"),   # "becomes effective on March 30, 2015."
    ("05-17333.htm", "2005-10-06"),
    ("98-4716.htm", "1998-03-02"),      # "... to all persons except ... telegraphic AD"
    ("01-917.htm", "2001-01-31"),
    ("2017-25253.htm", "2018-01-02"),   # "This rescission is effective ..."
])
def test_effective_date_from_the_rule_section(file, expected):
    split = _split(file)
    assert enrich.effective_date_from_sections(split["sections"]) == expected


def test_no_effective_date_section_or_no_date_gives_none():
    assert enrich.effective_date_from_sections(None) is None
    assert enrich.effective_date_from_sections(
        [{"kind": "effective_date", "body": "The emergency AD was effective on the date it was provided."}]) is None
    assert enrich.effective_date_from_sections(
        [{"kind": "other", "body": "This AD is effective May 22, 2024."}]) is None
    assert enrich.effective_date_from_sections(
        [{"kind": "effective_date", "body": "This AD is effective February 30, 2024."}]) is None


@pytest.mark.parametrize("entry,effective_on,expected", [
    ({"status": "extracted", "sections": [{"kind": "effective_date", "body": "This AD is effective May 22, 2024."}]},
     "2024-05-21", ("2024-05-22", "section")),
    ({"status": "unanchored", "sections": []}, "2024-05-21", ("2024-05-21", "effective_on")),
    ({"status": "unanchored", "sections": [{"kind": "effective_date", "body": "effective May 22, 2024"}]},
     "", (None, "none")),
    ({}, None, (None, "none")),
])
def test_rule_effective_date_order(entry, effective_on, expected):
    assert fr.rule_effective_date(entry, effective_on) == expected


def _doc(file: str, effective_on: str = "") -> dict:
    e = FIXTURES[file]
    return {"title": e["title"], "abstract": e.get("abstract") or "", "document_number": e["document_number"],
            "publication_date": e["publication_date"], "type": "Rule", "docket_ids": e["docket_ids"],
            "effective_on": effective_on}


@pytest.fixture()
def no_fetch(monkeypatch):
    monkeypatch.setattr(fr, "_ft_attempted", set())
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)

    def _with(cache):
        monkeypatch.setattr(fr, "_fulltext_cache", cache)
    return _with


def test_the_fr_record_never_falls_back_to_the_publication_date(no_fetch):
    no_fetch({})
    rec = fr._parse_document(_doc("2024-08105.htm"))
    assert rec["Publication_Date"] == "2024-04-17"
    assert rec["Effective_Date"] is None and rec["Effective_On"] == ""
    assert populate_db.normalize(rec, "federal_register")["effective_date"] is None


def test_the_fr_record_takes_the_rule_date_then_effective_on(no_fetch):
    split = _split("2024-08105.htm")
    no_fetch({"2024-08105": {"v": 2, **split}})
    assert fr._parse_document(_doc("2024-08105.htm", "2024-05-21"))["Effective_Date"] == "2024-05-22"
    no_fetch({})
    rec = fr._parse_document(_doc("2024-08105.htm", "2024-05-21"))
    assert (rec["Effective_Date"], rec["Effective_On"]) == ("2024-05-21", "2024-05-21")


# ---------------------------------------------------------------------------
# scripts/backfill_effective_date.py contra un cliente falso: nunca una base
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db):
        self.db, self.filters, self.rng, self.values = db, [], None, None

    def select(self, cols):
        self.db.selects.append(cols)
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def update(self, values):
        self.values = values
        return self

    def execute(self):
        rows = [r for r in self.db.ads if all(f(r) for f in self.filters)]
        if self.values is not None:
            if self.db.fail_updates:
                raise RuntimeError("update failed")
            self.db.updates.append((dict(self.values), sorted(r["id"] for r in rows)))
            return _Resp([])
        if self.rng:
            rows = rows[self.rng[0]:self.rng[1] + 1]
        return _Resp([dict(r) for r in rows])


class FakeSupabase:
    def __init__(self, ads):
        self.ads, self.updates, self.selects, self.fail_updates = ads, [], [], False

    def table(self, name):
        assert name == "ads"
        return _Query(self)


def _ad(i, fr_doc, effective_date, raw_effective, raw_published, raw_effective_on=None):
    return {"id": f"ad-{i}", "ad_number": f"AD-{i}", "source": "federal_register",
            "effective_date": effective_date, "fr_doc": fr_doc, "raw_effective": raw_effective,
            "raw_effective_on": raw_effective_on, "raw_published": raw_published}


@pytest.fixture()
def fake(monkeypatch):
    ads = [
        # Guardado con la fecha de publicación; el rule dice otra.
        _ad(1, "2024-08105", "2024-04-17", "2024-04-17", "2024-04-17"),
        # Ya correcto.
        _ad(2, "2015-06235", "2015-03-30", "2015-03-30", "2015-03-19"),
        # Sin texto anclado; raw distinto de la publicación: era effective_on.
        _ad(3, "E7-1708", "2007-03-19", "2007-03-19", "2007-02-12"),
        # Sin texto anclado y raw igual a la publicación: era el respaldo -> NULL.
        _ad(4, "E8-2623", "2008-02-14", "2008-02-14", "2008-02-14"),
        # Fila nueva con Effective_On vacío y sin sección: NULL (ya lo es).
        _ad(5, "missing", None, None, "2025-01-02", raw_effective_on=""),
        # Fila nueva con Effective_On que difiere de lo guardado.
        _ad(6, "missing", "2025-01-02", None, "2025-01-02", raw_effective_on="2025-02-06"),
    ]
    split = _split("2024-08105.htm")
    cache = {"2024-08105": {"v": 2, **split},
             "2015-06235": {"v": 2, **_split("2015-06235.htm")},
             "E7-1708": {"v": 2, "status": "unanchored", "sections": []},
             "E8-2623": {"v": 2, "status": "unanchored", "sections": []}}
    sb = FakeSupabase(ads)
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(populate_db, "_write_errors", [])
    monkeypatch.setattr(fr, "_fulltext_cache", cache)
    return sb


def test_plan_lists_only_what_changes_and_why(fake):
    changes = {c["ad_number"]: c for c in bf.plan(bf.fetch_faa_ads(fake), fr._fulltext_cache)}
    assert {k: (c["before"], c["after"], c["source"]) for k, c in changes.items()} == {
        "AD-1": ("2024-04-17", "2024-05-22", "section"),
        "AD-4": ("2008-02-14", None, "publication_date"),
        "AD-6": ("2025-01-02", "2025-02-06", "effective_on"),
    }


def test_dry_run_is_the_default_and_writes_nothing(fake, capsys):
    assert bf.main([]) == 0
    out, err = capsys.readouterr()
    assert fake.updates == []
    assert out.splitlines()[0] == ",".join(bf.CSV_COLUMNS)
    assert "ad-1,AD-1,2024-08105,2024-04-17,2024-05-22,section" in out
    assert "ad-4,AD-4,E8-2623,2008-02-14,,publication_date" in out
    assert "dry-run" in err and "ADs con effective_date nuevo: 3" in err
    assert all("raw_data->>" in s for s in fake.selects)


def test_apply_updates_by_new_date_and_only_the_changes(fake, capsys):
    assert bf.main(["--apply"]) == 0
    assert sorted(fake.updates, key=lambda u: str(u[0]["effective_date"])) == [
        ({"effective_date": "2024-05-22"}, ["ad-1"]),
        ({"effective_date": "2025-02-06"}, ["ad-6"]),
        ({"effective_date": None}, ["ad-4"]),
    ]


def test_a_failed_update_is_a_write_error_and_exits_non_zero(fake, capsys):
    fake.fail_updates = True
    assert bf.main(["--apply"]) == 1
    assert len(populate_db._write_errors) == 3


def test_csv_to_a_file_is_the_restore_file(fake, tmp_path):
    path = tmp_path / "plan.csv"
    assert bf.main(["--csv", str(path)]) == 0
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == ",".join(bf.CSV_COLUMNS) and len(rows) == 4
    buf = io.StringIO()
    bf.write_csv(bf.plan(bf.fetch_faa_ads(fake), fr._fulltext_cache), buf)
    assert buf.getvalue().splitlines() == rows
