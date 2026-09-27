"""
Recalcula `ads.effective_date` de los ADs FAA guardados con la regla de
scrapers/federal_register.rule_effective_date: la fecha que declara la
sección Effective Date del texto anclado del AD, si no el `effective_on` de
la FR API, si no NULL. Nunca la fecha de publicación.

Las filas guardadas llevan la regla anterior, que caía a la fecha de
publicación cuando la FR API no traía `effective_on`. La corrida nocturna
solo reescribe lo que vuelve a bajar; este script aplica la regla a todo lo
guardado sin bajar nada: lee `ads` (FAA) y el cache full-text local
(outputs/fr_fulltext_cache.json).

`effective_on` de una fila guardada: `raw_data.Effective_On` si existe (lo
escribe el scraper desde este cambio). Las filas anteriores solo tienen
`raw_data.Effective_Date` = `effective_on` o, si faltaba, la fecha de
publicación; cuando es distinta de `raw_data.Publication_Date` era
`effective_on`, y cuando es igual no se distingue del respaldo y se trata
como ausente (la columna `source` del CSV lo marca `publication_date`).

Uso:
    python scripts/backfill_effective_date.py                 # dry-run: CSV + conteos
    python scripts/backfill_effective_date.py --csv plan.csv  # el CSV a un archivo
    python scripts/backfill_effective_date.py --apply         # escribe

El CSV lista cada AD que cambia con su valor antes y después: es también el
archivo para restaurar. `--apply` escribe con el mismo cliente y el mismo
.env que populate_db, en lotes agrupados por fecha nueva.
"""

import argparse
import collections
import csv
import sys

import populate_db
from scrapers import federal_register as fr

SOURCE = "federal_register"
PAGE = 1000
UPDATE_CHUNK = 500
CSV_COLUMNS = ["id", "ad_number", "fr_doc", "effective_date_before", "effective_date_after", "source"]


def fetch_faa_ads(supabase) -> list[dict]:
    """id, ad_number, effective_date y lo que raw_data sabe de la fecha."""
    out: list[dict] = []
    page = 0
    cols = ("id,ad_number,effective_date,fr_doc:raw_data->>FR_DocNumber,"
            "raw_effective:raw_data->>Effective_Date,raw_effective_on:raw_data->>Effective_On,"
            "raw_published:raw_data->>Publication_Date")
    while True:
        resp = (supabase.table("ads").select(cols)
                .eq("source", SOURCE)
                .order("id")
                .range(page * PAGE, (page + 1) * PAGE - 1)
                .execute())
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        page += 1


def stored_effective_on(ad: dict) -> tuple[str | None, bool]:
    """(`effective_on` de la FR API según raw_data, era el respaldo de la
    fecha de publicación)."""
    if ad.get("raw_effective_on") is not None:
        return (ad["raw_effective_on"] or None), False
    raw = (ad.get("raw_effective") or "").strip()
    if raw and raw != (ad.get("raw_published") or "").strip():
        return raw, False
    return None, bool(raw)


def plan(ads: list[dict], cache: dict) -> list[dict]:
    """Un elemento por AD cuya fecha efectiva cambia."""
    changes = []
    for ad in ads:
        effective_on, was_fallback = stored_effective_on(ad)
        entry = cache.get(ad.get("fr_doc") or "") or {}
        after, source = fr.rule_effective_date(entry, effective_on)
        after = populate_db.parse_date(after) if after else None
        if source == "none" and was_fallback:
            source = "publication_date"
        if after == ad.get("effective_date"):
            continue
        changes.append({"id": ad["id"], "ad_number": ad["ad_number"], "fr_doc": ad.get("fr_doc") or "",
                        "before": ad.get("effective_date"), "after": after, "source": source})
    return changes


def write_csv(changes: list[dict], fh) -> None:
    w = csv.writer(fh)
    w.writerow(CSV_COLUMNS)
    for c in changes:
        w.writerow([c["id"], c["ad_number"], c["fr_doc"], c["before"] or "", c["after"] or "", c["source"]])


def summary(ads: list[dict], changes: list[dict]) -> list[str]:
    by_source = collections.Counter(c["source"] for c in changes)
    lines = [f"ADs FAA leidos: {len(ads)}",
             f"ADs con effective_date nuevo: {len(changes)}",
             f"  a NULL: {sum(1 for c in changes if c['after'] is None)}",
             f"  desde NULL: {sum(1 for c in changes if c['before'] is None)}"]
    lines += [f"  fuente {s}: {n}" for s, n in by_source.most_common()]
    return lines


def apply(supabase, changes: list[dict]) -> int:
    """UPDATE ads.effective_date agrupado por fecha nueva. Un lote fallido es
    un error de escritura (salida != 0); los demás siguen."""
    by_date: dict[str | None, list[str]] = collections.defaultdict(list)
    for c in changes:
        by_date[c["after"]].append(c["id"])
    updated = 0
    for value, ids in by_date.items():
        for i in range(0, len(ids), UPDATE_CHUNK):
            chunk = ids[i:i + UPDATE_CHUNK]
            try:
                supabase.table("ads").update({"effective_date": value}).in_("id", chunk).execute()
                updated += len(chunk)
            except Exception as e:
                populate_db._record_write_error(f"  effective_date: update fallo ({value}): {e}")
    return updated


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe (por defecto: dry-run)")
    ap.add_argument("--csv", help="archivo para el CSV del plan (por defecto: stdout)")
    args = ap.parse_args(argv)

    supabase = populate_db.get_supabase_client()
    ads = fetch_faa_ads(supabase)
    changes = plan(ads, fr._load_fulltext_cache())

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
    updated = apply(supabase, changes)
    print(f"escrito: {updated} ADs", file=sys.stderr)
    return 1 if populate_db._write_errors else 0


if __name__ == "__main__":
    sys.exit(main())
