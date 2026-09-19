"""
enrich_details de Transport Canada: una busqueda de PDF por AD, no por fila.

TC devuelve el mismo AD una vez por cada modelo al que aplica, y la corrida
diaria consulta cuatro fabricantes: ~37.000 filas para ~4.800 ADs. El bucle
recorria las 37.000 pagando la pausa de cortesia en cada una — 42 minutos de
runner, el 87% repitiendo trabajo. Las duplicadas NO se pueden descartar: cada
una lleva su modelo y de ahi sale `ad_applicability`.
"""

import scrapers.transport_canada as tc


def _rows():
    return [
        {"AD_Number": "CF-2020-01", "Model": "DHC-8-102", "PDF_Link": "https://x?ad=CF-2020-01&ft=pdf"},
        {"AD_Number": "CF-2020-01", "Model": "DHC-8-202", "PDF_Link": "https://x?ad=CF-2020-01&ft=pdf"},
        {"AD_Number": "CF-2020-01", "Model": "DHC-8-311", "PDF_Link": "https://x?ad=CF-2020-01&ft=pdf"},
        {"AD_Number": "CF-2020-02", "Model": "CL-600", "PDF_Link": "https://x?ad=CF-2020-02&ft=pdf"},
        # Sin PDF: no hay nada que pedir, y no debe contar como fetch.
        {"AD_Number": "CF-2020-03", "Model": "BD-500", "PDF_Link": "https://x?ad=CF-2020-03&ft=html"},
    ]


def test_one_fetch_per_ad_number(monkeypatch):
    calls = []

    def fake_fetch(ad_number, pdf_link, session):
        calls.append(ad_number)
        return {"unsafe_condition": f"UC {ad_number}", "corrective_action": None,
                "compliance_time": None, "amoc": True, "service_bulletins": [],
                "supersedes": None}

    monkeypatch.setattr(tc, "_fetch_tc_pdf", fake_fetch)
    monkeypatch.setattr(tc, "FETCH_PDF", True)
    monkeypatch.setattr(tc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tc, "_save_tc_cache", lambda: None)
    monkeypatch.setattr(tc, "_load_tc_cache", lambda: {})

    rows = _rows()
    out = tc.enrich_details(rows)

    # Cinco filas, dos ADs con PDF -> dos busquedas.
    assert calls == ["CF-2020-01", "CF-2020-02"]
    # Y ninguna fila se pierde: las duplicadas llevan el modelo que alimenta
    # ad_applicability.
    assert len(out) == 5
    assert [r["Model"] for r in out] == ["DHC-8-102", "DHC-8-202", "DHC-8-311", "CL-600", "BD-500"]


def test_every_duplicate_gets_the_enriched_fields(monkeypatch):
    monkeypatch.setattr(tc, "_fetch_tc_pdf", lambda n, l, s: {
        "unsafe_condition": "cracking", "corrective_action": "inspect",
        "compliance_time": "within 600 hours", "amoc": True,
        "service_bulletins": ["SB-1"], "supersedes": "CF-2019-11"})
    monkeypatch.setattr(tc, "FETCH_PDF", True)
    monkeypatch.setattr(tc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tc, "_save_tc_cache", lambda: None)
    monkeypatch.setattr(tc, "_load_tc_cache", lambda: {})

    out = tc.enrich_details(_rows())
    for row in out[:3]:
        assert row["Unsafe_Condition"] == "cracking"
        assert row["Compliance_Time"] == "within 600 hours"
        assert row["AMOC_Available"] is True
        assert row["Supersedes"] == "CF-2019-11"
    # El que no tiene PDF se queda como estaba.
    assert "Unsafe_Condition" not in out[4]


def test_no_courtesy_sleep_on_a_cache_hit(monkeypatch):
    slept = []
    monkeypatch.setattr(tc, "FETCH_PDF", True)
    monkeypatch.setattr(tc, "_save_tc_cache", lambda: None)
    monkeypatch.setattr(tc, "_fetch_tc_pdf", lambda n, l, s: {"amoc": False})
    monkeypatch.setattr(tc.time, "sleep", lambda s: slept.append(s))
    # Ambos ADs ya en cache con el esquema nuevo.
    monkeypatch.setattr(tc, "_load_tc_cache", lambda: {
        "CF-2020-01": {"supersedes": None}, "CF-2020-02": {"supersedes": None}})

    tc.enrich_details(_rows())
    # La pausa es cortesia con el servidor de TC; un acierto de cache no lo toca.
    assert slept == []
