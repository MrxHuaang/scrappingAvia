"""DGAC Chile: lectura del backend JSON, fechas en texto libre y fallo visible."""

import pytest

from scrapers import dgac_chile


class _Resp:
    def __init__(self, status_code=200, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _doc(nombre, estado="vigente", fecha="30/ABR/61", aplicacion="PIPER", url="http://servicios.dgac.gob.cl/dgac_backend/api/v1/documento/download/abc"):
    return {
        "estado": estado,
        "activo": True,
        "valores": {
            "nombre": nombre,
            "aplicacion": aplicacion,
            "fecha_vigencia": fecha,
            "documento": {"nombre": "x.pdf", "url": url, "externo": False},
        },
    }


def _serve(monkeypatch, resp):
    monkeypatch.setattr(dgac_chile, "request_with_backoff", lambda *a, **k: resp)


@pytest.mark.parametrize("text, expected", [
    ("30/ABR/61", "1961-04-30"),
    ("28/FEB/02", "2002-02-28"),
    ("Resolución 08/2/3/358 del 02.sep.2015", "2015-09-02"),
    ("12 de julio 2013", "2013-07-12"),
    ("16-Jun-2010", "2010-06-16"),
    ("01-08-1982 Rev 1 del 13 mayo 2011 Derogada", "1982-08-01"),
    ("Edición 1 del 12 dic 1986 Derogada por Resol. N° 98/2/3/322 del 28 oct 2014", "1986-12-12"),
    ("Res Nº 02497\n30 oct 2007 Cancelada\nRes N° 08/2/3/167", "2007-10-30"),
    ("sin fecha 1996", ""),
    ("", ""),
])
def test_parse_date(text, expected):
    assert dgac_chile._parse_date(text) == expected


def test_documents_become_ads(monkeypatch):
    _serve(monkeypatch, _Resp(payload=[
        _doc("DA 01-69"),
        _doc("DA 82-01", estado="derogada", fecha="02 ago 1982 Cancelada", aplicacion="CESSNA"),
        _doc("Otra norma"),
        {**_doc("DA 99-99"), "activo": False},
    ]))
    ads = dgac_chile.search_ads()
    assert [a["AD_Number"] for a in ads] == ["DA 01-69", "DA 82-01"]
    first, cancelled = ads
    assert first["Status"] == "Current"
    assert first["Make"] == "Piper"
    assert first["Effective_Date"] == "1961-04-30"
    assert first["PDF_Link"].startswith("https://servicios.dgac.gob.cl/")
    assert cancelled["Status"] == "Cancelled"


@pytest.mark.parametrize("resp", [
    None,
    _Resp(status_code=404),
    _Resp(json_error=True),
    _Resp(payload={"error": "x"}),
    _Resp(payload=[]),
])
def test_unreadable_listing_raises(monkeypatch, resp):
    # Antes un 404 devolvia [] y la corrida quedaba `completed` con 0 ADs.
    _serve(monkeypatch, resp)
    with pytest.raises(dgac_chile.DgacChileError):
        dgac_chile.search_ads()
