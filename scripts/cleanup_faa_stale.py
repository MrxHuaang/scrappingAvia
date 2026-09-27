"""
In-place migration of `ads` (source=federal_register) to canonical BARE AD
numbers, removing the stale FR-document-number duplicates. No scraping — uses
only data already present:

  * "AD "-prefixed rows (e.g. "AD 2026-13-08")  -> correct AD, wrong format.
  * stale doc-number rows (e.g. "2026-13540")   -> FR document number; the
    GovInfo MODS cache maps each to its real AD.

End state: every real AD exists exactly once as a bare number (e.g. "2026-13-08");
no doc-number rows; no "AD " prefix. User refs (ad_saved / ad_reviewed /
ad_compliance) follow via ON UPDATE CASCADE, or are merged when a duplicate
already exists at the target.

Strategy per target bare number:
  - keeper = the row already equal to target if present, else we RENAME one
    member to target (UPDATE -> ON UPDATE CASCADE moves its refs).
  - every other member: re-point its user refs to the keeper (merge on
    conflict), then DELETE it.
Rows whose real AD can't be resolved (doc-number not in cache) are LEFT as-is
and reported — never deleted (don't lose an AD).

DRY_RUN=1 (default) = read-only plan. DRY_RUN=0 = execute.
"""

import json
import os
import re
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()
from supabase import create_client  # noqa: E402

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
SOURCE = "federal_register"
# (table, extra PK columns beyond source+ad_number, allow_merge)
# allow_merge=True: a dup at the target replaces the moved ref (delete the mover).
# allow_merge=False: the table has no meaningful unique key (ad_notes: many notes
# per AD, PK=id), so always repoint — never delete, or we'd lose annotations.
# All four carry the (source, ad_number) FK with on delete restrict, so refs on a
# to-be-deleted duplicate row must be moved first.
USER_TABLES = [
    ("ad_saved", ["workspace_id"], True),
    ("ad_reviewed", ["workspace_id", "user_id"], True),
    ("ad_compliance", ["workspace_id", "user_id", "aircraft_id"], True),
    ("ad_notes", [], False),
]

_BARE_RE = re.compile(r"^\d{2,4}-\d{2}-\d{2,4}[A-Z]?$", re.I)
_PREFIX_RE = re.compile(r"^A\.?\s*D\.?\s+", re.I)


def bare(s: str) -> str:
    return _PREFIX_RE.sub("", (s or "").strip()).strip()


def is_bare(s: str) -> bool:
    return bool(_BARE_RE.match(s or ""))


def load_docmap() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "outputs", "govinfo_mods_cache.json")
    cache = json.load(open(path, encoding="utf-8"))
    out = {}
    for gid, e in cache.get("granules", {}).items():
        ad = (e or {}).get("ad")
        if ad:
            out[gid] = bare(ad)
    return out


def target_of(adn: str, docmap: dict):
    """Canonical bare target for a row's ad_number, or None if unresolved."""
    if is_bare(adn):
        return adn
    if _PREFIX_RE.match(adn or ""):
        b = bare(adn)
        return b if is_bare(b) else None
    t = docmap.get(adn)
    return t if (t and is_bare(t)) else None


def _client():
    """
    Este script re-escribe y borra filas de `ads` (y repuntea/borra referencias
    de usuario en ad_saved/ad_reviewed/ad_compliance/ad_notes), asi que
    necesita la service key: la BD revoca los privilegios de escritura de
    anon/authenticated sobre esas tablas, y degradar a la anon key aqui ya no
    es un respaldo funcional (fallaria en runtime incluso en DRY_RUN=0).
    """
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env (este script escribe en `ads`; la anon key ya no basta)")
    return create_client(url, key)


def main():
    sb = _client()
    docmap = load_docmap()
    print(f"=== FAA canonical migration — {'DRY-RUN' if DRY_RUN else 'EXECUTE'} ===")
    print(f"MODS docnum->AD entries: {len(docmap)}")

    # load all fed_register ad_numbers ------------------------------------
    present = set()
    off = 0
    while True:
        r = sb.table("ads").select("ad_number").eq("source", SOURCE).range(off, off + 999).execute().data
        present.update(x["ad_number"] for x in r)
        if len(r) < 1000:
            break
        off += 1000
    print(f"federal_register rows: {len(present)}")

    grp = defaultdict(list)      # target bare -> [member ad_numbers]
    unresolved = []
    for adn in present:
        t = target_of(adn, docmap)
        (grp[t].append(adn) if t else unresolved.append(adn))
    print(f"distinct target ADs: {len(grp)} | unresolved (kept as-is): {len(unresolved)}")
    if unresolved:
        print("  unresolved sample:", sorted(unresolved)[:10])

    renames = []   # (old -> target)  UPDATE ads
    deletes = []   # ad_numbers to DELETE
    ref_moves = [] # (old -> target) user-ref repoint (merge)
    for target, members in grp.items():
        keeper_exists = target in present
        rest = [m for m in members if m != target]
        if not keeper_exists:
            # rename one member (prefer an "AD "-prefixed one) to the bare target
            rest_sorted = sorted(rest, key=lambda m: 0 if _PREFIX_RE.match(m) else 1)
            r0 = rest_sorted[0]
            renames.append((r0, target))
            rest = [m for m in rest_sorted[1:]]
        for m in rest:
            ref_moves.append((m, target))
            deletes.append(m)

    print(f"planned: {len(renames)} renames (cascade), {len(deletes)} deletes, "
          f"{len(ref_moves)} ref-repoint checks")
    # ref summary (only rows that actually carry user refs matter)
    print("sample renames:", renames[:3])
    print("sample deletes:", deletes[:3])

    if DRY_RUN:
        print("DRY-RUN: no writes. Set DRY_RUN=0 to execute.")
        return

    # execute -------------------------------------------------------------
    # 0) detach collection memberships sitting on ANY affected old ad_number.
    #    ad_saved_collections FKs ad_saved WITHOUT on-update-cascade, so both the
    #    ads rename cascade (ads -> ad_saved -> child) and the direct ad_saved
    #    repoint violate it. Detach globally now; reattach mapped at the end.
    target_by_old = {old: tgt for old, tgt in renames}
    target_by_old.update({old: tgt for old, tgt in ref_moves})
    affected_olds = sorted(target_by_old)
    detached: list[dict] = []
    for i in range(0, len(affected_olds), 200):
        chunk = affected_olds[i:i + 200]
        rows = (sb.table("ad_saved_collections").select("*")
                .eq("source", SOURCE).in_("ad_number", chunk).execute().data)
        detached.extend(rows)
    if detached:
        nums = sorted({r["ad_number"] for r in detached})
        for i in range(0, len(nums), 100):
            sb.table("ad_saved_collections").delete() \
              .eq("source", SOURCE).in_("ad_number", nums[i:i + 100]).execute()
        print(f"detached {len(detached)} collection memberships on {len(nums)} ADs")

    # 1) re-point user refs off soon-to-be-deleted rows, onto the keeper.
    #    keeper row is guaranteed to exist AFTER renames, so do renames first.
    done = 0
    for old, target in renames:
        try:
            sb.table("ads").update({"ad_number": target}).match(
                {"source": SOURCE, "ad_number": old}).execute()
        except Exception as e:
            # target already exists (race/dup) -> treat old as a delete instead
            print(f"  rename {old!r}->{target!r} failed ({e}); will delete")
            deletes.append(old)
            ref_moves.append((old, target))
        done += 1
        if done % 1000 == 0:
            print(f"  renamed {done}/{len(renames)}")
    print(f"renames done: {done}")

    # 2) merge/repoint user refs sitting on to-be-deleted rows.
    #    Bulk-prefetch which old ad_numbers actually carry refs (user tables are
    #    tiny vs the 13k-row move list) — iterating per-move × per-table costs
    #    ~53k serial requests and times out; this costs a few hundred.
    olds_with_refs: dict[str, set] = {}
    all_olds = sorted({old for old, _ in ref_moves})
    for tbl, _extra, _am in USER_TABLES:
        hit: set = set()
        for i in range(0, len(all_olds), 200):
            chunk = all_olds[i:i + 200]
            rows = (sb.table(tbl).select("ad_number").eq("source", SOURCE)
                    .in_("ad_number", chunk).execute().data)
            hit.update(r["ad_number"] for r in rows)
        olds_with_refs[tbl] = hit
        print(f"  [{tbl}] refs on {len(hit)} of {len(all_olds)} move candidates")

    moved = 0
    for old, target in ref_moves:
        for tbl, extra, allow_merge in USER_TABLES:
            if old not in olds_with_refs.get(tbl, ()):
                continue
            rows = sb.table(tbl).select("*").eq("source", SOURCE).eq("ad_number", old).execute().data
            if not rows:
                continue
            if not allow_merge:
                # No unique key (e.g. ad_notes) — repoint every row, never delete.
                sb.table(tbl).update({"ad_number": target}).eq("source", SOURCE).eq("ad_number", old).execute()
                print(f"  [{tbl}] REPOINT-ALL {old!r}->{target!r} ({len(rows)})")
                moved += len(rows)
                continue
            for ro in rows:
                probe = {"source": SOURCE, "ad_number": target}
                for c in extra:
                    probe[c] = ro.get(c)
                dup = sb.table(tbl).select("ad_number").match(probe).execute().data
                match = {"source": SOURCE, "ad_number": old}
                for c in extra:
                    match[c] = ro.get(c)

                # ad_saved has a child table (ad_saved_collections) whose FK has
                # NO on-update-cascade: detach memberships first, reattach after.
                memberships = []
                if tbl == "ad_saved":
                    memberships = (sb.table("ad_saved_collections").select("*")
                                   .eq("source", SOURCE).eq("ad_number", old)
                                   .eq("workspace_id", ro.get("workspace_id")).execute().data)
                    if memberships:
                        sb.table("ad_saved_collections").delete() \
                          .eq("source", SOURCE).eq("ad_number", old) \
                          .eq("workspace_id", ro.get("workspace_id")).execute()

                if dup:
                    sb.table(tbl).delete().match(match).execute()
                    print(f"  [{tbl}] MERGE {old!r}->{target!r} (deleted dup)")
                else:
                    sb.table(tbl).update({"ad_number": target}).match(match).execute()
                    print(f"  [{tbl}] REPOINT {old!r}->{target!r}")

                # Reattach collection memberships under the target ad_number
                # (upsert: the target may already sit in the same collection).
                for mb in memberships:
                    mb = dict(mb)
                    mb["ad_number"] = target
                    try:
                        sb.table("ad_saved_collections").upsert(
                            mb, on_conflict="collection_id,ad_number,source").execute()
                    except Exception as e:
                        print(f"    membership reattach failed ({e}) — skipped")
                moved += 1
    print(f"ref moves applied: {moved}")

    # 3) bulk-delete the stale rows (now unreferenced). Chunk of 50 keeps each
    #    statement (with its FK cascades) under the DB statement timeout; on a
    #    chunk failure fall back to per-row so one bad row can't block 49.
    deleted = 0
    failed = 0
    dedup = sorted(set(deletes))
    for i in range(0, len(dedup), 50):
        chunk = dedup[i:i + 50]
        try:
            sb.table("ads").delete().eq("source", SOURCE).in_("ad_number", chunk).execute()
            deleted += len(chunk)
        except Exception as e:
            print(f"  chunk delete failed ({e}); retrying per-row")
            for adn in chunk:
                try:
                    sb.table("ads").delete().eq("source", SOURCE).eq("ad_number", adn).execute()
                    deleted += 1
                except Exception as e2:
                    failed += 1
                    print(f"    DELETE FAILED {adn!r}: {e2}")
        if deleted % 2000 < 50:
            print(f"  deleted {deleted}/{len(dedup)}")
    if failed:
        print(f"  WARNING: {failed} rows could not be deleted (kept)")

    # 4) reattach the detached collection memberships under the target numbers.
    reattached = re_failed = 0
    for mb in detached:
        mb = dict(mb)
        mb["ad_number"] = target_by_old.get(mb["ad_number"], mb["ad_number"])
        try:
            sb.table("ad_saved_collections").upsert(
                mb, on_conflict="collection_id,ad_number,source").execute()
            reattached += 1
        except Exception as e:
            re_failed += 1
            print(f"  membership reattach failed {mb.get('ad_number')!r}: {e}")
    if detached:
        print(f"memberships reattached: {reattached}, failed: {re_failed}")
    print(f"done. renamed {len(renames)}, deleted {deleted}, refs moved {moved}, "
          f"unresolved-kept {len(unresolved)}")


if __name__ == "__main__":
    main()
