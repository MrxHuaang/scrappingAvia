"""
Clase de documento EASA (scrapers/common/easa_document_class.py).

Los links son los reales del listado de ad.easa.europa.eu: la cola del link
nombra la clase de la publicacion, aunque el archivo sea el ZIP de otra.
"""

import pytest

import populate_db
from scrapers.common.easa_document_class import (
    DOCUMENT_CLASSES,
    class_from_icon,
    class_from_link,
    document_class,
)

_BLOB = "https://ad.easa.europa.eu/blob"


@pytest.mark.parametrize("href, expected", [
    (f"{_BLOB}/EASA_SIB_2007_12.pdf/SIB_2007-12_1", "SIB"),
    (f"{_BLOB}/EASA_AD_2024_0109_CN.pdf/AD_2024-0109-CN_1", "AD"),
    (f"{_BLOB}/EASA_PAD_26_092.pdf/PAD_26-092_1", "PAD"),
    (f"{_BLOB}/2001-08-52.pdf/EAD_US-2001-08-52_1", "EAD"),
    (f"{_BLOB}/EASA_ADR_2013_0305.pdf/ADR_2013-0305_1", "ADR"),
    (f"{_BLOB}/SD-2019-01.pdf/SD_SD-2019-01_1", "SD"),
    # El ZIP del PAD de un AD final: manda la cola, no el nombre del archivo.
    (f"{_BLOB}/EASA_PAD_26_031.zip/AD_2026-0052R2_2", "AD"),
])
def test_the_link_tail_names_the_class(href, expected):
    assert class_from_link(href) == expected


@pytest.mark.parametrize("href", [
    None, "", "https://ad.easa.europa.eu/ad/2024-0195.pdf",
    f"{_BLOB}/X.pdf/PSD_2019-01_1",      # clase fuera del contrato
    f"{_BLOB}/x.pdf/ad_2024-0001_1",     # minusculas: no es la forma de EASA
])
def test_a_tail_that_is_not_a_known_class_is_unknown(href):
    assert class_from_link(href) is None


def test_icon_alt_is_read_case_and_space_insensitively():
    assert class_from_icon(" sib ") == "SIB"
    assert class_from_icon("PAD") == "PAD"
    assert class_from_icon("send comment") is None
    assert class_from_icon(None) is None


def test_the_link_wins_over_the_icon():
    assert document_class([f"{_BLOB}/a.pdf/SIB_2026-10_1"], icon_alt="PAD") == "SIB"


def test_the_first_link_that_declares_a_class_wins():
    links = [None, "https://example.test/doc.pdf", f"{_BLOB}/a.pdf/EAD_2008-0200-E_1"]
    assert document_class(links, icon_alt="SIB") == "EAD"


def test_the_icon_is_the_fallback_when_no_link_declares_a_class():
    assert document_class([], icon_alt="SIB") == "SIB"
    assert document_class(["https://example.test/doc.pdf"], icon_alt="PAD") == "PAD"


def test_no_signal_is_unknown_not_a_guess():
    assert document_class() is None
    assert document_class([""], icon_alt="") is None


def test_the_contract_set_is_exactly_the_six_classes():
    assert DOCUMENT_CLASSES == {"AD", "EAD", "PAD", "SIB", "SD", "ADR"}


# ----------------------------------------------------- escritura en ads


def _easa_row(**extra):
    return {"AD_Number": "2026-10", "Subject": "x", "Make": "ALLSTAR PZL GLIDER",
            "Status": "Current", "PDF_Link": f"{_BLOB}/a.pdf/SIB_2026-10_1", **extra}


def test_an_easa_row_writes_its_known_class():
    assert populate_db.normalize(_easa_row(Document_Class="SIB"), "easa")["document_class"] == "SIB"


def test_an_unknown_class_does_not_mention_the_column():
    assert "document_class" not in populate_db.normalize(_easa_row(Document_Class=None), "easa")
    assert "document_class" not in populate_db.normalize(_easa_row(Document_Class="PSD"), "easa")
    assert "document_class" not in populate_db.normalize(_easa_row(), "easa")


def test_other_sources_never_write_the_column():
    row = {"AD_Number": "2023-12-03", "Subject": "x", "Document_Class": "AD"}
    assert "document_class" not in populate_db.normalize(row, "federal_register")


class _FakeAds:
    def __init__(self):
        self.upserts = []

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def upsert(self, payload, on_conflict=None):
        self.upserts.append(payload)
        return self

    def execute(self):
        return type("Resp", (), {"data": []})()


def test_upsert_ads_sends_the_class_to_the_ads_table():
    sb = _FakeAds()
    rows = [populate_db.normalize(_easa_row(Document_Class="SIB"), "easa")]
    populate_db.upsert_ads(sb, rows)
    [payload] = sb.upserts
    assert payload[0]["document_class"] == "SIB"
    assert payload[0]["raw_data"]["Document_Class"] == "SIB"
