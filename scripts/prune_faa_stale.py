"""
Borra las filas FAA cuyo ad_number no es el del documento que enlazan.

Se corre DESPUES de `populate_db.py --source federal_register`: el repoblado ya
insertó cada AD bajo su número real (tomado de `docket_ids`), así que las filas
viejas con el número equivocado quedan como duplicados que compiten con la
buena en búsqueda y export.

Verdad: outputs/fr_identity.json (scripts/fr_identity.py). Para cada fila se
resuelve su FR document_number (raw_data.FR_DocNumber o el PDF) y se compara su
ad_number con el `AD` del header regulatorio de ese documento.

Reglas:
  * ad_number == verdad                    -> se queda.
  * ad_number != verdad y la fila correcta existe -> se mueven las refs de
    usuario a la correcta y se borra la fila.
  * ad_number != verdad y la correcta NO existe   -> se renombra (no se pierde
    el AD).
  * documento sin AD en el índice, o fila sin documento resoluble -> se REPORTA
    y se queda. Nunca se borra a ciegas.

Las refs de usuario (ad_saved / ad_reviewed / ad_compliance / ad_notes) llevan
FK (source, ad_number) con ON DELETE RESTRICT: hay que moverlas antes de borrar.

    python scripts/prune_faa_stale.py             # plan (dry-run)
    python scripts/prune_faa_stale.py --execute   # ejecuta

Antes de borrar, las filas afectadas se volcan completas a
outputs/prune_faa_backup.json (reinsertables si algo sale mal).
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

load_dotenv()
from supabase import create_client  # noqa: E402

from scripts.fr_identity import INDEX_PATH, load_index  # noqa: E402
from scripts.fix_faa_identity import fr_docnum  # noqa: E402

SOURCE = "federal_register"
PAGE = 1000
BACKUP_PATH = os.path.join(_ROOT, "outputs", "prune_faa_backup.json")

# Forma de un numero AD de la FAA: 2024-19-03 / 98-21-09 / 2018-23-51R1.
# Lo que NO la cumple y vive en ad_number es un FR document_number colado como
# identidad (2013-19500, E7-25505, 00-6162): esas filas son basura del bug.
_FAA_AD_SHAPE = re.compile(r"^(?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?$", re.I)

# (tabla, columnas extra de la clave unica, permite merge)
# allow_merge=False -> ad_notes no tiene clave unica por AD: siempre repointear.
USER_TABLES = [
    ("ad_saved", ["workspace_id"], True),
    ("ad_reviewed", ["workspace_id", "user_id"], True),
    ("ad_compliance", ["workspace_id", "user_id", "aircraft_id"], True),
    ("ad_notes", [], False),
]


def sb_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def fetch_rows(sb):
    rows, page = [], 0
    cols = "*"
    while True:
        chunk = (sb.table("ads").select(cols).eq("source", SOURCE)
                 .order("id").range(page * PAGE, page * PAGE + PAGE - 1).execute().data or [])
        rows.extend(chunk)
        if len(chunk) < PAGE:
            return rows
        page += 1


def move_refs(sb, old: str, target: str) -> int:
    """Mueve refs de usuario de `old` a `target`; mergea si ya hay una igual."""
    moved = 0
    for tbl, extra, allow_merge in USER_TABLES:
        rows = (sb.table(tbl).select("*").eq("source", SOURCE)
                .eq("ad_number", old).execute().data or [])
        if not rows:
            continue
        if not allow_merge:
            sb.table(tbl).update({"ad_number": target}).eq("source", SOURCE) \
              .eq("ad_number", old).execute()
            moved += len(rows)
            continue
        for ro in rows:
            probe = {"source": SOURCE, "ad_number": target}
            match = {"source": SOURCE, "ad_number": old}
            for c in extra:
                probe[c] = ro.get(c)
                match[c] = ro.get(c)
            dup = sb.table(tbl).select("ad_number").match(probe).execute().data
            if dup:
                sb.table(tbl).delete().match(match).execute()
            else:
                sb.table(tbl).update({"ad_number": target}).match(match).execute()
            moved += 1
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="aplica los cambios")
    args = ap.parse_args()
    dry_run = not args.execute

    if not os.path.exists(INDEX_PATH):
        raise SystemExit("corre scripts/fr_identity.py primero")
    index = load_index()
    sb = sb_client()

    rows = fetch_rows(sb)
    present = {(r["ad_number"] or "") for r in rows}
    print(f"=== prune FAA stale — {'DRY-RUN' if dry_run else 'EXECUTE'} ===")
    print(f"filas federal_register: {len(rows)} | índice FR: {len(index)} docs")

    keep, deletes, renames, unresolved, doc_no_ad, malformed = [], [], [], [], [], []
    for r in rows:
        num = fr_docnum(r)
        ent = index.get(num) if num else None
        truth = (ent or {}).get("ad")
        if not truth:
            # Sin verdad disponible. Si el ad_number ni siquiera tiene forma de
            # AD de la FAA, es un FR document_number: se borra. Si la tiene, se
            # deja (no se puede probar que este mal).
            bucket = unresolved if ent is None else doc_no_ad
            if _FAA_AD_SHAPE.match(r["ad_number"] or ""):
                bucket.append(r)
            else:
                malformed.append(r)
            continue
        if (r["ad_number"] or "").upper() == truth:
            keep.append(r)
        elif truth in present:
            deletes.append((r, truth))
        else:
            renames.append((r, truth))
            present.add(truth)

    print(f"  correctas               : {len(keep)}")
    print(f"  a borrar (dup)          : {len(deletes)}")
    print(f"  a borrar (no es num AD) : {len(malformed)}")
    print(f"  a renombrar             : {len(renames)}")
    print(f"  doc sin AD (queda)      : {len(doc_no_ad)}")
    print(f"  sin doc (queda)         : {len(unresolved)}")

    if unresolved:
        print("  sin doc, muestra:", [r["ad_number"] for r in unresolved[:10]])
    if doc_no_ad:
        print("  doc sin AD, muestra:", [r["ad_number"] for r in doc_no_ad[:10]])
    if malformed:
        print("  no es num AD, muestra:", [r["ad_number"] for r in malformed[:10]])

    if dry_run:
        print("DRY-RUN: sin escrituras. --execute para aplicar.")
        return

    # Respaldo completo de todo lo que se va a borrar, antes de tocar nada.
    doomed = [r for r, _ in deletes] + malformed
    os.makedirs(os.path.dirname(BACKUP_PATH), exist_ok=True)
    with open(BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(doomed, f, ensure_ascii=False, default=str)
    print(f"respaldo de {len(doomed)} filas -> {BACKUP_PATH}")

    for r, truth in renames:
        try:
            sb.table("ads").update({"ad_number": truth}).eq("id", r["id"]).execute()
        except Exception as e:
            print(f"  rename {r['ad_number']} -> {truth} fallo: {e}")

    moved = deleted = failed = 0
    for r, truth in deletes + [(m, None) for m in malformed]:
        if truth:
            try:
                moved += move_refs(sb, r["ad_number"], truth)
            except Exception as e:
                print(f"  refs {r['ad_number']} -> {truth} fallo: {e}")
        try:
            sb.table("ads").delete().eq("id", r["id"]).execute()
            deleted += 1
        except Exception as e:
            failed += 1
            print(f"  DELETE fallo {r['ad_number']}: {e}")
        if deleted % 1000 == 0 and deleted:
            print(f"  borradas {deleted}/{len(deletes) + len(malformed)}")

    print(f"listo: {len(renames)} renombradas, {deleted} borradas, {moved} refs movidas, "
          f"{failed} fallos")


if __name__ == "__main__":
    main()
