"""
Self-check del checksum canonico de ad_equivalents (INT-4), sin red ni DB.

El mismo vector de prueba se evalua contra `public.ad_equivalents_checksum_rows`
en SQL; ambos lados DEBEN dar el mismo hash. El vector incluye a proposito:
  - separadores dentro del texto ('|' y salto de linea en la nota),
  - un caracter no ASCII (multi-byte: el prefijo es de BYTES, no de caracteres),
  - filas desordenadas respecto de la clave natural,
  - una nota vacia y una nota None.

Uso:  python scripts/test_equivalents_checksum.py
      (para el lado SQL, ver el bloque de abajo)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_equivalents import canonical_checksum  # noqa: E402

VECTOR = [
    {
        "ad_id": "3f4d1c2b-0000-4000-8000-000000000002",
        "equivalent_source": "easa",
        "equivalent_ad_number": "2024-0102",
        "match_level": "full",
        "note": "pipe | inside\nand a newline",
    },
    {
        "ad_id": "3f4d1c2b-0000-4000-8000-000000000001",
        "equivalent_source": "federal_register",
        "equivalent_ad_number": "2023-18-04",
        "match_level": "partial",
        "note": "acentuación ñ",
    },
    {
        "ad_id": "3f4d1c2b-0000-4000-8000-000000000001",
        "equivalent_source": "easa",
        "equivalent_ad_number": "2024-0102",
        "match_level": "candidate",
        "note": "",
    },
    {
        "ad_id": "3f4d1c2b-0000-4000-8000-000000000003",
        "equivalent_source": "transport_canada",
        "equivalent_ad_number": "CF-2021-11",
        "match_level": "full",
        "note": None,
    },
]

EXPECTED_SQL = """
select public.ad_equivalents_checksum_rows($vec$[
  {"ad_id":"3f4d1c2b-0000-4000-8000-000000000002","equivalent_source":"easa","equivalent_ad_number":"2024-0102","match_level":"full","note":"pipe | inside\\nand a newline"},
  {"ad_id":"3f4d1c2b-0000-4000-8000-000000000001","equivalent_source":"federal_register","equivalent_ad_number":"2023-18-04","match_level":"partial","note":"acentuación ñ"},
  {"ad_id":"3f4d1c2b-0000-4000-8000-000000000001","equivalent_source":"easa","equivalent_ad_number":"2024-0102","match_level":"candidate","note":""},
  {"ad_id":"3f4d1c2b-0000-4000-8000-000000000003","equivalent_source":"transport_canada","equivalent_ad_number":"CF-2021-11","match_level":"full","note":""}
]$vec$::jsonb);
"""


def main() -> int:
    got = canonical_checksum(VECTOR)
    print(f"python checksum: {got}")
    print("SQL equivalente para contrastar:")
    print(EXPECTED_SQL)

    # Propiedades que el contrato exige, verificables sin la DB.
    reordered = list(reversed(VECTOR))
    assert canonical_checksum(reordered) == got, "el orden de entrada no debe alterar el hash"

    tampered = [dict(r) for r in VECTOR]
    tampered[0]["note"] = "pipe | inside\nand a newlineX"
    assert canonical_checksum(tampered) != got, "un cambio en la nota debe cambiar el hash"

    # Ambiguedad de separadores: dos filas cuyo texto concatenado coincidiria sin
    # prefijo de longitud deben dar hashes distintos.
    a = [{"ad_id": "3f4d1c2b-0000-4000-8000-000000000001", "equivalent_source": "ab",
          "equivalent_ad_number": "c", "match_level": "full", "note": ""}]
    b = [{"ad_id": "3f4d1c2b-0000-4000-8000-000000000001", "equivalent_source": "a",
          "equivalent_ad_number": "bc", "match_level": "full", "note": ""}]
    assert canonical_checksum(a) != canonical_checksum(b), "el prefijo de longitud debe desambiguar"

    print("OK: orden irrelevante, alteracion detectada, separadores desambiguados")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
