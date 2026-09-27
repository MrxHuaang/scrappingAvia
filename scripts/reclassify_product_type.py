"""
Reclasifica el product_type de los ADs FAA guardados con el clasificador
compartido (enrich.classify_product_type + enrich.product_subtype).

Las filas ya guardadas llevan el tipo que les dio la regla anterior (y la
migración 20260923180000_fr_product_type_from_title.sql); la corrida nocturna
solo reescribe lo que vuelve a bajar. Este script aplica la regla vigente a
TODO lo guardado, sin bajar nada: lee subject + raw_data.Abstract de `ads`.
La regla vive solo en enrich.py — aquí no hay otra copia (ni en SQL).

Qué escribe (--apply):

  1. `ads.product_type`, en lotes agrupados por tipo nuevo. El trigger
     `ads_propagate_product_type` (migración 20260926120000 de ad-scrapper)
     lleva el tipo a las filas de `ad_applicability` con
     product_type_source='derived'; NO toca product_subtype ni las filas
     'explicit' (las que escribió populate_db con el contrato v2).
  2. `ad_applicability`, por la misma RPC que usa populate_db
     (`replace_ad_applicability`, vía populate_db._send_applicability_batches):
     para cada AD cuyas filas no tienen ya el tipo/subtipo nuevo, se reenvían
     TODAS sus filas tal cual están guardadas (make, model, series, seriales,
     confidence, model_status) cambiando solo product_type y product_subtype.
     La clave natural no cambia, así que la RPC actualiza en sitio: no
     inserta ni poda filas. Las filas quedan 'explicit', igual que las que
     escribe populate_db. Un AD sin filas no se toca.

Uso:
    python scripts/reclassify_product_type.py                 # dry-run: CSV + conteos
    python scripts/reclassify_product_type.py --csv plan.csv  # el CSV a un archivo
    python scripts/reclassify_product_type.py --apply         # escribe

El CSV lista cada AD que cambia (tipo en `ads` o tipo/subtipo en sus filas de
applicability); los conteos van a stderr.
"""

import argparse
import collections
import csv
import sys

import enrich
import populate_db

SOURCE = "federal_register"
PAGE = 1000
ID_CHUNK = 200
ADS_UPDATE_CHUNK = 500
_ROW_FIELDS = ("make", "model", "series", "serial_range_start", "serial_range_end",
               "confidence", "model_status")
CSV_COLUMNS = ["ad_number", "product_type_before", "product_type_after", "product_subtype_after",
               "applicability_rows", "rows_to_rewrite"]


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
def fetch_faa_ads(supabase) -> list[dict]:
    """id, ad_number, subject, product_type y el abstract de cada AD FAA."""
    out: list[dict] = []
    page = 0
    while True:
        resp = (supabase.table("ads")
                .select("id,ad_number,subject,product_type,abstract:raw_data->>Abstract")
                .eq("source", SOURCE)
                .order("id")
                .range(page * PAGE, (page + 1) * PAGE - 1)
                .execute())
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        page += 1


def fetch_applicability(supabase, ad_ids: list[str]) -> dict[str, list[dict]]:
    """Filas de ad_applicability por ad_id (todas: la RPC reemplaza el AD entero)."""
    by_ad: dict[str, list[dict]] = collections.defaultdict(list)
    cols = ",".join(("ad_id",) + _ROW_FIELDS + ("product_type", "product_subtype", "product_type_source"))
    for i in range(0, len(ad_ids), ID_CHUNK):
        chunk = ad_ids[i:i + ID_CHUNK]
        page = 0
        while True:
            resp = (supabase.table("ad_applicability").select(cols)
                    .in_("ad_id", chunk).order("id")
                    .range(page * PAGE, (page + 1) * PAGE - 1).execute())
            rows = resp.data or []
            for r in rows:
                by_ad[r["ad_id"]].append(r)
            if len(rows) < PAGE:
                break
            page += 1
    return by_ad


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def classify(ad: dict) -> tuple[str | None, str | None]:
    """(ads.product_type canónico, subtipo) con la regla de populate_db."""
    subject = ad.get("subject") or ""
    abstract = ad.get("abstract") or ""
    # Todo AD FAA es CFR 14 parte 39: el último respaldo de la regla.
    pt = enrich.normalize_product_type(enrich.classify_product_type(subject, abstract, [{"part": "39"}]))
    code = enrich.PRODUCT_TYPE_CODE.get(pt or "")
    return pt, (enrich.product_subtype(code, subject, abstract) if code else None)


def _row_needs_rpc(row: dict, code: str, subtype: str | None, type_changes: bool) -> bool:
    """Una fila 'derived' cuyo único cambio es el tipo la arregla el trigger
    `ads_propagate_product_type` al escribir ads.product_type, y sigue
    'derived' (enganchada a correcciones futuras). Todo lo demás —subtipo
    distinto, o una fila 'explicit' con otro tipo— pasa por la RPC."""
    if row.get("product_subtype") != subtype:
        return True
    if row.get("product_type") == code:
        return False
    return not (row.get("product_type_source") == "derived" and type_changes)


def plan(ads: list[dict], applicability: dict[str, list[dict]]) -> list[dict]:
    """Un elemento por AD que cambia algo."""
    changes: list[dict] = []
    for ad in ads:
        pt, subtype = classify(ad)
        code = enrich.PRODUCT_TYPE_CODE.get(pt or "")
        rows = applicability.get(ad["id"], [])
        type_changes = pt is not None and pt != ad.get("product_type")
        stale = [r for r in rows if code and _row_needs_rpc(r, code, subtype, type_changes)]
        if not type_changes and not stale:
            continue
        changes.append({
            "id": ad["id"], "ad_number": ad["ad_number"],
            "before": ad.get("product_type"), "after": pt if pt is not None else ad.get("product_type"),
            "type_changes": type_changes, "code": code, "subtype": subtype,
            "rows": rows, "stale": len(stale),
        })
    return changes


def write_csv(changes: list[dict], fh) -> None:
    w = csv.writer(fh)
    w.writerow(CSV_COLUMNS)
    for c in changes:
        w.writerow([c["ad_number"], c["before"] or "", c["after"] or "", c["subtype"] or "",
                    len(c["rows"]), c["stale"]])


def summary(ads: list[dict], changes: list[dict]) -> list[str]:
    transitions = collections.Counter((c["before"], c["after"]) for c in changes if c["type_changes"])
    lines = [f"ADs FAA leidos: {len(ads)}",
             f"ADs con ads.product_type nuevo: {sum(transitions.values())}"]
    lines += [f"  {b or '(vacio)'} -> {a}: {n}" for (b, a), n in transitions.most_common()]
    rewrite = [c for c in changes if c["stale"]]
    lines.append(f"ADs con filas de applicability a reescribir: {len(rewrite)} "
                 f"({sum(c['stale'] for c in rewrite)} filas con tipo/subtipo distinto, "
                 f"{sum(len(c['rows']) for c in rewrite)} filas reenviadas)")
    return lines


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------
def _row_payload(row: dict, code: str, subtype: str | None) -> dict:
    out = {k: row[k] for k in _ROW_FIELDS if row.get(k) is not None}
    out["product_type"] = code
    if subtype:
        out["product_subtype"] = subtype
    return out


def apply(supabase, changes: list[dict]) -> dict[str, int]:
    # 1) ads.product_type, agrupado por tipo nuevo.
    by_type: dict[str, list[str]] = collections.defaultdict(list)
    for c in changes:
        if c["type_changes"]:
            by_type[c["after"]].append(c["id"])
    updated = 0
    for pt, ids in by_type.items():
        for i in range(0, len(ids), ADS_UPDATE_CHUNK):
            chunk = ids[i:i + ADS_UPDATE_CHUNK]
            supabase.table("ads").update({"product_type": pt}).in_("id", chunk).execute()
            updated += len(chunk)

    # 2) ad_applicability por la RPC de populate_db: el AD entero, misma clave.
    payload = [
        {"ad_id": c["id"], "status": "success_with_rows",
         "rows": [_row_payload(r, c["code"], c["subtype"]) for r in c["rows"]]}
        for c in changes if c["stale"] and c["code"]
    ]
    counts = populate_db._send_applicability_batches(supabase, payload) if payload else {}
    return {"ads_updated": updated, "applicability_ads_sent": len(payload), **counts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe (por defecto: dry-run)")
    ap.add_argument("--csv", help="archivo para el CSV del plan (por defecto: stdout)")
    args = ap.parse_args(argv)

    supabase = populate_db.get_supabase_client()
    ads = fetch_faa_ads(supabase)
    changes = plan(ads, fetch_applicability(supabase, [a["id"] for a in ads]))

    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            write_csv(changes, fh)
    else:
        write_csv(changes, sys.stdout)
    for line in summary(ads, changes):
        print(line, file=sys.stderr)

    if not args.apply:
        print("dry-run: nada escrito (--apply para escribir)", file=sys.stderr)
        return 0
    result = apply(supabase, changes)
    print(f"escrito: {result}", file=sys.stderr)
    return 1 if populate_db._write_errors else 0


if __name__ == "__main__":
    sys.exit(main())
