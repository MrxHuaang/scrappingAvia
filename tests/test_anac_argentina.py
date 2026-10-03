"""ANAC Argentina: dos listados, numero ADV canonico y fallo visible."""

import pytest

from scrapers import anac_argentina


class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


def _page(*links):
    return "<html><body>" + "".join(f'<a href="{h}">{t}</a>' for t, h in links) + "</body></html>"


MAIN = _page(
    ("DA - 2024-05-01 R1 - LAVIA S.A. PA25 / PA25-235", "/sites/default/files/da_2024.pdf"),
    ("ADV 340/DAG - Talleres Aeronauticos", "/sites/default/files/adv_340.pdf"),
    ("ADV 339/DAG Talleres de aeronaves I.C.A BRASOV", "https://back.argentina.gob.ar/files/adv339pdf"),
)
ARCHIVE = _page(
    ("ADV 340/DAG - Talleres Aeronauticos", "/sites/default/files/adv_340.pdf"),
    ("ADV 336-DAG    Talleres Aeronauticos", "/sites/default/files/adv_336.pdf"),
    ("ADV 336-DAG    Talleres Aeronauticos", "/sites/default/files/adv_336.pdf"),
    ("Compartir en Facebook", "https://facebook.com/x"),
)


def _serve(monkeypatch, pages):
    def fake(method, url, **kwargs):
        return pages[url]
    monkeypatch.setattr(anac_argentina, "request_with_backoff", fake)


@pytest.mark.parametrize("text, expected", [
    ("ADV 335/DAG    Talleres", "ADV 335/DAG"),
    ("ADV 335-DAG - Talleres", "ADV 335/DAG"),
    ("ADV 309-DAG R2    Talleres", "ADV 309/DAG R2"),
    ("ADV 128 R1/DAG Talleres", "ADV 128/DAG R1"),
    ("ADV 299/DAGR1  - Aeronaves TECNAM", "ADV 299/DAG R1"),
    ("ADV-293-DAG - Posible presencia", "ADV 293/DAG"),
    ("ADV 310/DAG R02 Aeronaves TECNAM", "ADV 310/DAG R2"),
    ("ADV 292/DAG-R2 - Presencia de fisuras", "ADV 292/DAG R2"),
    ("ADV 239/DAG r2- Rotura del Fleje", "ADV 239/DAG R2"),
    ("ADV 046/DAG R1 - TAR, propietarios", "ADV 46/DAG R1"),
    ("ADV 001/DOA - Dos directivas", "ADV 1/DOA"),
    ("ADV 315/AG - Instalacion no aprobada", "ADV 315/AG"),
    ("61/DAG R1-  Instalaciones peligrosas", "ADV 61/DAG R1"),
    ("ADV 300 - Posible colapso", "ADV 300/DAG"),
    ("ADV 318/DAG  - Aeronaves AERO BOERO - Rotura", "ADV 318/DAG"),
    ("1. Roturas y fisuras de las DPA", ""),
    ("DA 2017-08-01 R2 - Fisuras", ""),
])
def test_adv_number_is_canonical(text, expected):
    assert anac_argentina._extract_adv_number(text)[0] == expected


def test_reads_both_listings_and_dedupes(monkeypatch):
    _serve(monkeypatch, {anac_argentina.ANAC_AR_URL: _Resp(MAIN),
                         anac_argentina.ANAC_AR_ADV_URL: _Resp(ARCHIVE)})
    ads = {a["AD_Number"]: a for a in anac_argentina.search_ads()}
    assert sorted(ads) == ["ADV 336/DAG", "ADV 339/DAG", "ADV 340/DAG", "DA - 2024-05-01 R1"]
    assert ads["ADV 336/DAG"]["Subject"] == "Talleres Aeronauticos"
    assert ads["ADV 340/DAG"]["PDF_Link"].startswith("https://www.argentina.gob.ar/")
    # El link sin ".pdf" no es un PDF, pero el titulo nombra la ADV.
    assert ads["ADV 339/DAG"]["PDF_Link"] == ""


@pytest.mark.parametrize("archive", [None, _Resp(status_code=404), _Resp(_page())])
def test_unreadable_listing_raises(monkeypatch, archive):
    _serve(monkeypatch, {anac_argentina.ANAC_AR_URL: _Resp(MAIN),
                         anac_argentina.ANAC_AR_ADV_URL: archive})
    with pytest.raises(anac_argentina.AnacArgentinaError):
        anac_argentina.search_ads()
