"""
Self-check de `populate_db._dedup_rows()` (sin red, sin DB):

    python scripts/test_upsert_dedup.py

Cubre el defecto que se corrigio: last-wins puramente posicional dentro de un
mismo scrape podia dejar ganar a la fila menos completa (o sin PDF) por pura
suerte de orden — el mismo patron que rompio la identidad de FAA, un nivel mas
arriba.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from populate_db import _dedup_rows  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n    got : {got!r}\n    want: {want!r}")


def row(**kw):
    base = {"source": "transport_canada", "ad_number": "CF-2015-19", "pdf_link": None,
             "subject": None, "make": None, "model": None, "product_type": None,
             "effective_date": None, "docket": None, "unsafe_condition": None,
             "corrective_action": None, "compliance_time": None}
    base.update(kw)
    return base


# TC emite el mismo AD una vez por modelo aplicable: la fila con PDF debe
# ganar aunque llegue PRIMERO en la lista (last-wins la habria perdido).
with_pdf_first = row(model="CRJ-100", pdf_link="https://x/doc.pdf", subject="Some AD")
no_pdf_second = row(model="CRJ-200")
out = _dedup_rows([with_pdf_first, no_pdf_second])
check("una fila por AD", len(out), 1)
check("gana la fila CON pdf aunque llegue primero", out[0]["pdf_link"], "https://x/doc.pdf")

# Misma situacion pero el pdf llega ULTIMO en la lista.
out2 = _dedup_rows([no_pdf_second, with_pdf_first])
check("gana el pdf sin importar el orden", out2[0]["pdf_link"], "https://x/doc.pdf")

# Dos filas CON pdf: gana la mas completa (mas campos no vacios), no la ultima.
sparse = row(pdf_link="https://x/a.pdf", subject="A")
rich = row(pdf_link="https://x/b.pdf", subject="B", make="Bombardier", model="CRJ",
           effective_date="2015-06-01", docket="CF-2015-19")
out3 = _dedup_rows([rich, sparse])
check("entre dos con pdf, gana la mas completa", out3[0]["pdf_link"], "https://x/b.pdf")

# Distintos AD numbers no se tocan entre si.
a = row(ad_number="CF-2015-19", pdf_link="https://x/a.pdf")
b = row(ad_number="CF-2016-01", pdf_link="https://x/b.pdf")
out4 = _dedup_rows([a, b])
check("AD numbers distintos no colisionan", len(out4), 2)


if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("OK — dedup por completitud")
