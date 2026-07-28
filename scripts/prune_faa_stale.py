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
  * documento sin AD en el índice, o fila sin documento resoluble -> segunda
    pasada: se lee el header del PDF que la fila enlaza. Si su ad_number no
    aparece en ningún header de ese PDF, la fila no es de ese documento y se
    borra (o se manda al AD correcto). Si el PDF no se puede leer, se REPORTA
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

import requests
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

load_dotenv()
from supabase import create_client  # noqa: E402

from scripts.fr_identity import INDEX_PATH, load_index  # noqa: E402
from scripts.fix_faa_identity import fr_docnum  # noqa: E402
from scripts.verify_faa_pdfs import header_ads  # noqa: E402

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
    """
    Todas las filas FAA, paginadas por keyset (id > ultimo).

    Con OFFSET, la pagina 20 de 25k filas hace que Postgres recorra y descarte
    20k filas por request y la consulta muere por statement timeout.
    """
    rows, last_id = [], "00000000-0000-0000-0000-000000000000"
    while True:
        chunk = (sb.table("ads").select("*").eq("source", SOURCE)
                 .gt("id", last_id).order("id").limit(PAGE).execute().data or [])
        rows.extend(chunk)
        if len(chunk) < PAGE:
            return rows
        last_id = chunk[-1]["id"]


def refs_by_table(sb, ad_numbers: list[str]) -> dict[str, set]:
    """
    Que ad_numbers llevan refs de usuario, por tabla.

    Se consulta una vez en lotes: las tablas de usuario son minusculas frente a
    los ~12k borrados, y preguntar por fila (4 tablas x 12k filas) son ~50k
    requests seriales.
    """
    out: dict[str, set] = {}
    for tbl, _extra, _merge in USER_TABLES:
        hit: set = set()
        for i in range(0, len(ad_numbers), 200):
            rows = (sb.table(tbl).select("ad_number").eq("source", SOURCE)
                    .in_("ad_number", ad_numbers[i:i + 200]).execute().data or [])
            hit.update(r["ad_number"] for r in rows)
        out[tbl] = hit
        print(f"  [{tbl}] refs sobre {len(hit)} de {len(ad_numbers)} filas a borrar")
    return out


def move_refs(sb, old: str, target: str, has_refs: dict[str, set]) -> int:
    """Mueve refs de usuario de `old` a `target`; mergea si ya hay una igual."""
    moved = 0
    for tbl, extra, allow_merge in USER_TABLES:
        if old not in has_refs.get(tbl, ()):
            continue
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

    # Segunda pasada sobre lo que el indice no puede juzgar (~140 filas): la
    # verdad se lee del header del PDF que la propia fila enlaza. Un AD que no
    # aparece en NINGUN header de su PDF no es de ese documento.
    unverifiable = doc_no_ad + unresolved
    if unverifiable:
        session = requests.Session()
        session.headers.update({"User-Agent": "zephr-prune/1.0"})
        still, deep_bad = [], 0
        for r in unverifiable:
            link, ad = r.get("pdf_link"), (r["ad_number"] or "").upper()
            if not link:
                still.append(r)
                continue
            try:
                heads = header_ads(session.get(link, timeout=60).content)
            except Exception:
                still.append(r)
                continue
            if not heads or ad in heads:
                still.append(r)
            else:
                # El PDF nombra otros ADs, no este. Si alguno de esos ya existe
                # como fila, esta es la copia con el numero equivocado.
                target = next((h for h in heads if h in present and h != ad), None)
                if target:
                    deletes.append((r, target))
                else:
                    malformed.append(r)
                deep_bad += 1
        print(f"  verificadas por PDF     : {len(unverifiable)} filas sin verdad en el "
              f"indice, {deep_bad} desmentidas por su propio PDF")
        doc_no_ad, unresolved = still, []

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

    has_refs = refs_by_table(sb, sorted({r["ad_number"] for r in doomed if r["ad_number"]}))

    moved = 0
    for r, truth in deletes:
        try:
            moved += move_refs(sb, r["ad_number"], truth, has_refs)
        except Exception as e:
            print(f"  refs {r['ad_number']} -> {truth} fallo: {e}")
    print(f"refs movidas: {moved}")

    # Borrado por lotes de ids. Fila por fila son ~12k requests seriales; si un
    # lote falla (una ref bloqueando el FK) se reintenta fila por fila para que
    # una mala no arrastre a las otras 49.
    deleted = failed = 0
    ids = [r["id"] for r in doomed]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            sb.table("ads").delete().in_("id", chunk).execute()
            deleted += len(chunk)
        except Exception as e:
            print(f"  lote fallo ({e}); reintento fila por fila")
            for one in chunk:
                try:
                    sb.table("ads").delete().eq("id", one).execute()
                    deleted += 1
                except Exception as e2:
                    failed += 1
                    print(f"  DELETE fallo {one}: {e2}")
        if i % 2000 == 0:
            print(f"  borradas {deleted}/{len(ids)}")

    print(f"listo: {len(renames)} renombradas, {deleted} borradas, {moved} refs movidas, "
          f"{failed} fallos")


if __name__ == "__main__":
    main()
