"""
Autoridad emisora vs base donde leimos el AD (enrich.issuing_authority).

Cada autoridad republica las directivas de los estados de diseno que reconoce,
asi que `source` dice DONDE lo encontramos y no QUIEN lo emitio. Medido en
produccion: el 27% del corpus de EASA y mas de la mitad del de Transport Canada
fueron emitidos por otro.

Esta tabla de casos es la MISMA que la de tests/issuingAuthority.test.ts en el
repo del front y la del backfill en la migracion 20260919200000. Si una cambia
sin las otras, el badge de la UI y el dato guardado dejan de coincidir.
"""

import pytest

import enrich
from populate_db import normalize


@pytest.mark.parametrize("ad_number,source,expected", [
    # EASA guarda el origen en el prefijo.
    ("CF-2026-45", "easa", "transport_canada"),
    ("US-2026-17-08", "easa", "federal_register"),
    ("2026-0177", "easa", "easa"),
    # Legado nacional: son de EASA, que las administra. No son extranjeras.
    ("F-1998-173", "easa", "easa"),
    ("G-2001-0012", "easa", "easa"),
    ("D-1999-044", "easa", "easa"),
    # Canada: solo las CF- son propias.
    ("CF-2026-47", "transport_canada", "transport_canada"),
    ("2026-18-07", "transport_canada", "federal_register"),
    ("98-21-09", "transport_canada", "federal_register"),
    ("2026-0176", "transport_canada", "easa"),
    # El numero se lee DENTRO de su corpus: 2020-01-02 es de la FAA en la base
    # canadiense y brasileno en la de ANAC. Ninguna forma los separa sola.
    ("2020-01-02R4", "anac_brazil", "anac_brazil"),
    ("2020-01-02", "transport_canada", "federal_register"),
    # Formas que no prueban nada: se queda con el corpus.
    ("DA 84-01", "dgac_chile", "dgac_chile"),
    ("ADV 337-DAG", "anac_argentina", "anac_argentina"),
    ("CAP747-055", "uk_caa", "uk_caa"),
    ("", "easa", "easa"),
    ("2026-18-10", "federal_register", "federal_register"),
])
def test_issuing_authority(ad_number, source, expected):
    assert enrich.issuing_authority(ad_number, source) == expected


def test_normalize_stores_the_issuer_and_its_country():
    # El caso reportado: este AD abria como "Authority: EASA" sobre un PDF con
    # la bandera de Canada, y su estado de diseno decia "European Union".
    row = normalize({"AD_Number": "CF-2026-45", "Subject": "Wings"}, "easa")
    assert row["source"] == "easa"
    assert row["issuing_authority"] == "transport_canada"
    assert row["state_of_design"] == "Canada"


def test_normalize_leaves_an_own_ad_alone():
    row = normalize({"AD_Number": "2026-0177", "Subject": "x"}, "easa")
    assert row["issuing_authority"] == "easa"
    assert row["state_of_design"] == "European Union"
