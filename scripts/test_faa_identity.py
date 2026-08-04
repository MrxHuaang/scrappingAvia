"""
Self-check de la identidad FAA (sin red, sin DB):

    python scripts/test_faa_identity.py

Cubre las dos reglas que se rompieron en produccion:
  1. El numero AD sale de `docket_ids`, no del primer "AD nnnn-nn-nn" del cuerpo
     (que suele ser el AD superseded).
  2. Cuando varios documentos FR comparten numero AD (rule + correcciones), la
     fila canonica es el rule, no la correccion.
"""

import sys


from utils import ad_identity_from_docket_ids  # noqa: E402
from scrapers.federal_register import _collapse_by_ad_number  # noqa: E402

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}\n    got : {got!r}\n    want: {want!r}")


# --- 1. identidad desde docket_ids ----------------------------------------
check(
    "AD 2023-12-03 (correction de Airbus A350)",
    ad_identity_from_docket_ids([
        "Docket No. FAA-2023-0927", "Project Identifier MCAI-2023-00013-T",
        "Amendment 39-22461", "AD 2023-12-03",
    ]),
    {"ad": "2023-12-03", "docket": "FAA-2023-0927",
     "amendment": "39-22461", "project": "MCAI-2023-00013-T"},
)
# El caso que disparo el bug: la fila mostraba 2023-04-05 (un AD citado en el
# cuerpo) sobre el PDF de este documento, cuyo AD real es 2024-19-03.
check(
    "AD 2024-19-03 (FR 2024-23540)",
    ad_identity_from_docket_ids([
        "Docket No. FAA-2024-1694", "Project Identifier MCAI-2024-00016-T",
        "Amendment 39-22845", "AD 2024-19-03",
    ])["ad"],
    "2024-19-03",
)
# Formas legadas 1995-1999.
check("AD pegado al amendment",
      ad_identity_from_docket_ids(["Docket No. 94-ANE-38", "Amendment 39-9206, AD 95-09-02"])["ad"],
      "95-09-02")
check("AD con espacio interno",
      ad_identity_from_docket_ids(["Docket No. 91-CE-12-AD", "Amendment 39-9118", "AD 95-01- 07"])["ad"],
      "95-01-07")
check("AD con sufijo de revision",
      ad_identity_from_docket_ids(["Amendment 39-9705, AD 96-08-01 R1"])["ad"],
      "96-08-01R1")
check("AD con sufijo -AD legado",
      ad_identity_from_docket_ids(["Amendment 39-10728", "AD 98-18-12-AD"])["ad"],
      "98-18-12")
check("docket legado sin AD",
      ad_identity_from_docket_ids(["Docket No. 98-NM-22-AD", "Amendment 39-10410"]),
      {"ad": None, "docket": "98-NM-22-AD", "amendment": "39-10410", "project": None})
check("NPRM (solo docket)",
      ad_identity_from_docket_ids(["Docket No. FAA-2025-0001"])["ad"], None)
check("sin docket_ids", ad_identity_from_docket_ids([])["ad"], None)
# El docket NO debe leerse como numero AD.
check("docket FAA no es AD",
      ad_identity_from_docket_ids(["Docket No. FAA-2023-0927"])["ad"], None)


# --- 2. colapso rule vs correccion ----------------------------------------
rule = {"AD_Number": "2023-12-03", "Publication_Date": "2023-07-03",
        "Status": "Final Rule", "Is_Correction": False,
        "FR_DocNumber": "2023-14004", "PDF_Link": "…/2023-14004.pdf"}
correction = {"AD_Number": "2023-12-03", "Publication_Date": "2023-08-07",
              "Status": "Correction", "Is_Correction": True,
              "FR_DocNumber": "2023-16884", "PDF_Link": "…/2023-16884.pdf"}

for order, label in (([rule, correction], "rule primero"),
                     ([correction, rule], "correccion primero")):
    out = _collapse_by_ad_number([dict(r) for r in order])
    check(f"colapso ({label}): una fila", len(out), 1)
    check(f"colapso ({label}): gana el rule", out[0]["FR_DocNumber"], "2023-14004")
    check(f"colapso ({label}): correccion listada",
          [d["doc"] for d in out[0].get("Related_FR_Docs") or []], ["2023-16884"])

# Dos rules con el mismo AD -> gana el mas temprano (el que establece el AD).
early = {"AD_Number": "2003-07-11", "Publication_Date": "2003-04-11",
         "Status": "Final Rule", "Is_Correction": False, "FR_DocNumber": "03-8327"}
late = {"AD_Number": "2003-07-11", "Publication_Date": "2003-05-09",
        "Status": "Final Rule", "Is_Correction": False, "FR_DocNumber": "03-11537"}
out = _collapse_by_ad_number([dict(late), dict(early)])
check("colapso: gana la publicacion mas temprana", out[0]["FR_DocNumber"], "03-8327")


if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("OK — identidad FAA")
