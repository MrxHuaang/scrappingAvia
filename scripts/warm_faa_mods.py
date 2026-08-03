"""
Warms outputs/govinfo_mods_cache.json for the FAA rows whose `ad_number` is
still a Federal Register document number (e.g. 2017-03020, 97-2853) instead of
the real AD number (2014-23-06). ~13k of ~25k FAA rows are in this state,
which is why the displayed PDF (built from the doc number) does not match the
AD number stored in the DB.

This script does NOT touch the DB. It only resolves each doc-number granule's
real AD via the GovInfo MODS XML and caches it, so that the existing
scripts/cleanup_faa_stale.py (whose docmap reads this cache) can then re-key
the rows with proper FK cascade handling.

Usage:
  python scripts/warm_faa_mods.py [--limit N]

Requires GOVINFO_API_KEY in .env. Resumable (incremental cache saves); safe to
re-run — cached granules are skipped.
"""

import argparse
import os
import re

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client  # noqa: E402
import scrapers.faa_govinfo as g  # noqa: E402

# Doc-number = year + a single 4-6 digit group (no second hyphen group).
_DOCNUM_RE = re.compile(r"^\d{2,4}-\d{4,6}$")
_PKG_RE = re.compile(r"/pkg/([^/]+)/(?:pdf|html)/([^/.]+)", re.I)


def get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env")
    return create_client(url, key)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="Max doc-number rows a procesar")
    args = ap.parse_args()

    if not g._get_key():
        print("GOVINFO_API_KEY no configurada en .env — abortando.")
        return

    sb = get_client()

    # Cargar todas las filas FAA (ad_number, pdf_link) paginadas
    rows = []
    off = 0
    while True:
        b = (
            sb.table("ads")
            .select("ad_number, pdf_link")
            .eq("source", "federal_register")
            .range(off, off + 999)
            .execute()
            .data
        )
        if not b:
            break
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000

    docs = [r for r in rows if _DOCNUM_RE.match(r["ad_number"] or "")]
    if args.limit:
        docs = docs[: args.limit]
    print(f"FAA rows: {len(rows)} | doc-number rows: {len(docs)}")

    cache = g._load_cache()
    stats = {"hits": 0, "fetches": 0}
    resolved = unresolved = no_parse = 0

    for i, r in enumerate(docs, 1):
        m = _PKG_RE.search(r["pdf_link"] or "")
        if not m:
            no_parse += 1
            continue
        pkg, gran = m.group(1), m.group(2)
        entry = g._fetch_mods(pkg, gran, cache, stats)
        if entry.get("ad"):
            resolved += 1
        else:
            unresolved += 1
        if i % 500 == 0:
            print(f"  {i}/{len(docs)} | resolved={resolved} unresolved={unresolved} "
                  f"cache_hits={stats['hits']} fetches={stats['fetches']}")

    g._save_cache(cache)
    print(
        f"done. resolved={resolved} unresolved={unresolved} no_parse={no_parse} "
        f"| cache hits={stats['hits']} fetches={stats['fetches']}"
    )
    print("Next: python scripts/cleanup_faa_stale.py   (dry-run), then DRY_RUN=0 to execute.")


if __name__ == "__main__":
    main()
