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


# ------------------------------------------------- el enlace manda sobre la forma
# Transport Canada pone el pais de origen en la URL del documento. Es la
# autoridad diciendo de donde viene, y le gana a inferirlo del numero: la forma
# lee "2024-08-01" como de la FAA cuando el enlace dice BZ (EMBRAER).
@pytest.mark.parametrize("ad_number,link,expected", [
    ("2024-08-01", "https://wwwapps.tc.gc.ca/x?ad=2024-08-01&cn=BZ&ft=pdf", "anac_brazil"),
    ("2008-17-14", "https://wwwapps.tc.gc.ca/x?ad=2008-17-14&cn=US", "federal_register"),
    ("CF-2026-45", "https://wwwapps.tc.gc.ca/x?ad=CF-2026-45&cn=CF", "transport_canada"),
    ("2011-0021", "https://wwwapps.tc.gc.ca/x?ad=2011-0021&cn=EU", "easa"),
    # FR/GY/IL no tienen corpus nuestro: cae a la regla del numero, no se
    # inventa una autoridad que no indexamos.
    ("2011-0021", "https://wwwapps.tc.gc.ca/x?ad=2011-0021&cn=FR", "easa"),
    ("CF-2011-06", "https://wwwapps.tc.gc.ca/x?ad=CF-2011-06&cn=FR", "transport_canada"),
    # Sin enlace, la forma sigue siendo lo unico que hay.
    ("2024-08-01", None, "federal_register"),
    ("2024-08-01", "", "federal_register"),
])
def test_link_country_beats_number_shape(ad_number, link, expected):
    assert enrich.issuing_authority(ad_number, "transport_canada", link) == expected


def test_link_is_ignored_for_other_corpora():
    # Solo Transport Canada usa ese parametro; nadie mas debe leerlo.
    assert enrich.issuing_authority("2026-0177", "easa", "https://x?cn=US") == "easa"


def test_normalize_uses_the_link():
    row = normalize(
        {"AD_Number": "2024-08-01", "Subject": "x", "Make": "EMBRAER",
         "PDF_Link": "https://wwwapps.tc.gc.ca/x?ad=2024-08-01&cn=BZ&ft=pdf"},
        "transport_canada",
    )
    assert row["issuing_authority"] == "anac_brazil"
    assert row["state_of_design"] == "Brazil"
