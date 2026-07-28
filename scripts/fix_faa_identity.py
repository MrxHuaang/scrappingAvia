"""
Audit / repair of FAA (Federal Register) AD identity in Supabase.

Problem it fixes: `ads.ad_number` for source=federal_register was mined from
prose (first "AD nnnn-nn-nn" in the body / MODS XML). A rule cites the AD it
supersedes before naming itself, so many rows carry another AD's number while
linking the right document's PDF — the page showed "AD 2023-04-05" over the PDF
of AD 2024-19-03.

Ground truth: the FR API `docket_ids` array of each document, indexed by
scripts/fr_identity.py into outputs/fr_identity.json.

    python scripts/fix_faa_identity.py --audit
    python scripts/fix_faa_identity.py --apply            # repair in place
    python scripts/fix_faa_identity.py --apply --yes      # no prompt
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from scripts.fr_identity import INDEX_PATH, load_index  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("fix_faa_identity")

SOURCE = "federal_register"
PAGE = 1000

# FR document number inside a govinfo/federalregister pdf url:
#   .../FR-2023-08-07/pdf/2023-16884.pdf   (moderno)
#   .../FR-1996-05-07/pdf/96-10722.pdf     (anio de 2 digitos, pre-2000)
#   .../FR-2008-03-13/pdf/E8-5008.pdf      (prefijo E8/E9, 2008-2009)
#   .../pdf/C1-2012-14544.pdf              (prefijo C1/C2: correcciones)
_DOCNUM = r"(?:C\d-)?(?:E\d|\d{2,4})-\d{3,6}"
_PDF_DOCNUM_RE = re.compile(rf"/pdf/({_DOCNUM})\.pdf", re.I)
_FR_HTML_RE = re.compile(
    rf"federalregister\.gov/documents/\d{{4}}/\d{{2}}/\d{{2}}/({_DOCNUM})", re.I
)
_DOCNUM_SHAPE_RE = re.compile(rf"^{_DOCNUM}$", re.I)


def client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
    return create_client(url, key)


def fr_docnum(row: dict) -> str | None:
    """FR document_number for a DB row, from raw_data or the PDF url."""
    raw = row.get("raw_data") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    for k in ("FR_DocNumber", "GranuleId"):
        v = str(raw.get(k) or "").strip()
        if _DOCNUM_SHAPE_RE.match(v):
            return v
    link = str(row.get("pdf_link") or "")
    m = _PDF_DOCNUM_RE.search(link) or _FR_HTML_RE.search(link)
    if m:
        return m.group(1)
    v = str(row.get("docket") or "").strip()
    if _DOCNUM_SHAPE_RE.match(v):
        return v
    return None


def fetch_rows(sb) -> list[dict]:
    rows, page = [], 0
    cols = ("id, ad_number, subject, status, docket, pdf_link, effective_date, "
            "published_date, raw_data, superseded_to_ad_id")
    while True:
        resp = (sb.table("ads").select(cols).eq("source", SOURCE)
                .order("id").range(page * PAGE, page * PAGE + PAGE - 1).execute())
        chunk = resp.data or []
        rows.extend(chunk)
        if len(chunk) < PAGE:
            break
        page += 1
    return rows


def audit(sb, index: dict):
    rows = fetch_rows(sb)
    log.info(f"{len(rows)} rows for source={SOURCE}")

    no_docnum, not_in_index, ok, wrong, no_ad_in_index = [], [], [], [], []
    for r in rows:
        num = fr_docnum(r)
        if not num:
            no_docnum.append(r)
            continue
        ent = index.get(num)
        if ent is None:
            not_in_index.append(r)
            continue
        truth = ent.get("ad")
        if not truth:
            no_ad_in_index.append(r)
        elif (r["ad_number"] or "").upper() == truth:
            ok.append(r)
        else:
            wrong.append((r, num, truth))

    # target collisions: repaired number already used by another row
    by_number = defaultdict(list)
    for r in rows:
        by_number[(r["ad_number"] or "").upper()].append(r)
    collisions = sum(1 for r, _, t in wrong if by_number.get(t))

    print("\n=== FAA identity audit ===")
    print(f"  rows                       : {len(rows)}")
    print(f"  correct ad_number          : {len(ok)}")
    print(f"  WRONG ad_number            : {len(wrong)}")
    print(f"    of which target exists   : {collisions} (merge, not rename)")
    print(f"  FR doc has no AD number    : {len(no_ad_in_index)} (NPRM / legacy)")
    print(f"  doc not in FR index        : {len(not_in_index)}")
    print(f"  no FR doc number on row    : {len(no_docnum)} (DRS / other upstream)")

    print("\n  sample of wrong rows:")
    for r, num, truth in wrong[:15]:
        print(f"    {r['ad_number']:>14}  ->  {truth:<14} (FR {num})  {(r['subject'] or '')[:48]}")
    return rows, wrong


def _norm_status(ent: dict) -> str | None:
    t = f"{ent.get('title','')} {ent.get('action','')}".lower()
    if "correction" in t or "correcting amendment" in t:
        return "Revision"
    return "New"


def apply(sb, index: dict, yes: bool):
    rows, wrong = audit(sb, index)
    if not wrong:
        log.info("nothing to repair")
        return

    by_number: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_number[(r["ad_number"] or "").upper()].append(r)

    renames, merges = [], []
    taken: set[str] = set()
    for r, num, truth in wrong:
        holders = [h for h in by_number.get(truth, []) if h["id"] != r["id"]]
        if holders or truth in taken:
            merges.append((r, num, truth))
        else:
            renames.append((r, num, truth))
            taken.add(truth)

    print(f"\n  plan: {len(renames)} rename, {len(merges)} delete-as-duplicate")
    if not yes:
        if input("  proceed? [y/N] ").strip().lower() != "y":
            print("  aborted")
            return

    now = datetime.now(timezone.utc).isoformat()
    fixed = 0
    for r, num, truth in renames:
        ent = index[num]
        upd = {
            "ad_number": truth,
            "docket": ent.get("docket") or r.get("docket"),
            "pdf_link": ent.get("pdf_url") or r.get("pdf_link"),
            "published_date": ent.get("publication_date") or None,
            "effective_date": ent.get("effective_on") or ent.get("publication_date") or None,
            "last_updated_at": now,
            # semantic fields were extracted from the wrong document's context;
            # blank them so enrich_ai.py refills from the right PDF.
            "unsafe_condition": None,
            "corrective_action": None,
            "compliance_time": None,
        }
        try:
            sb.table("ads").update(upd).eq("id", r["id"]).execute()
            fixed += 1
        except Exception as e:
            log.warning(f"  rename failed {r['ad_number']} -> {truth}: {e}")
        if fixed % 200 == 0:
            log.info(f"  renamed {fixed}/{len(renames)}")

    deleted = 0
    for r, _num, _truth in merges:
        try:
            sb.table("ads").delete().eq("id", r["id"]).execute()
            deleted += 1
        except Exception as e:
            log.warning(f"  delete failed {r['id']}: {e}")

    log.info(f"done: {fixed} renamed, {deleted} duplicates deleted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(INDEX_PATH):
        raise SystemExit("run scripts/fr_identity.py first")
    index = load_index()
    log.info(f"FR index: {len(index)} docs, "
             f"{sum(1 for v in index.values() if v.get('ad'))} with AD number")

    sb = client()
    if args.apply:
        apply(sb, index, args.yes)
    else:
        audit(sb, index)


if __name__ == "__main__":
    main()
