"""
Builder de la tabla derivada `ad_equivalents` (ADs equivalentes cross-authority,
"harmonized"). Dos capas deterministas:

  Layer A `reference`  (match_level=full): referencias explicitas a ADs de otra
    autoridad en el texto ("FAA AD 2023-18-04", "EASA AD 2024-0102", "CF-...").
    Corpus: subject + unsafe_condition + corrective_action + raw_data.Abstract
    (en DB) + PDFs cacheados en outputs/pdf_cache/ (refs cacheadas en
    outputs/equiv_ref_cache.json). Bonus: ADs EASA con numero prefijado
    US-/CF- son adopciones literales de FAA/TC.

  Layer B `shared_sb`  (match_level=partial): dos ADs de autoridades distintas
    que citan el mismo Service Bulletin (ad_service_bulletins) y comparten make.

Idempotente: delete-and-rebuild por method (tabla 100% derivada, patron de
ad_applicability). Insercion simetrica cuando el target existe en la DB.

Uso:
  python scripts/build_equivalents.py [--layer all|reference|shared_sb]
                                      [--execute] [--limit N]

Default DRY-RUN: imprime conteos + muestra, no escribe.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase import create_client  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUTS = os.path.join(ROOT, "outputs")
REF_CACHE_PATH = os.path.join(OUTPUTS, "equiv_ref_cache.json")
PAGE = 1000
SAMPLE = 20
SB_FREQ_CAP = 8          # SB citado por mas ADs = boletin generico, ruido O(n^2)
SB_MIN_LEN = 6

# Referencias foraneas: prefijo de autoridad OBLIGATORIO (EASA 2024-0102 vs FAA
# 2024-01-02 estan a un guion de distancia en texto ruidoso de PDF).
RE_FAA = re.compile(
    r"\bFAA\s+(?:Emergency\s+)?AD\s+((?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?)(?![-\d])", re.I
)
RE_EASA = re.compile(
    r"\bEASA\s+(?:Emergency\s+)?(?<!P)AD\s+(\d{4}-\d{4}(?:R\d+)?)(?![-\d])", re.I
)
# CF- es suficientemente distintivo para ir bare
RE_TC = re.compile(r"\b(CF-\d{2,4}-\d{2,3}(?:R\d+)?)(?![-\d])", re.I)

FOREIGN_PATTERNS = {
    "federal_register": RE_FAA,
    "easa": RE_EASA,
    "transport_canada": RE_TC,
}

# EASA legado: numero con prefijo nacional = adopcion literal del AD extranjero
ADOPTION_PREFIX = {"US": "federal_register", "CF": "transport_canada"}


def get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env")
    return create_client(url, key)


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError:
        pass


def raw_dict(row: dict) -> dict:
    """raw_data puede venir como dict (jsonb) o string JSON segun el cliente."""
    raw = row.get("raw_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def norm_make(m: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", (m or "").lower())
    s = re.sub(r"\b(the|company|inc|corp|corporation|sa|s a|gmbh|ltd)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def same_make(a: str, b: str) -> bool:
    na, nb = norm_make(a), norm_make(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def pdf_text(pdf_link: str) -> str:
    if not pdf_link:
        return ""
    path = os.path.join(OUTPUTS, "pdf_cache", hashlib.sha1(pdf_link.encode()).hexdigest() + ".pdf")
    if not os.path.exists(path):
        return ""
    try:
        from pypdf import PdfReader
        with open(path, "rb") as f:
            reader = PdfReader(io.BytesIO(f.read()))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:8])
    except Exception:
        return ""


def fetch_all_ads(sb, limit: int | None):
    offset = 0
    fetched = 0
    while True:
        resp = (
            sb.table("ads")
            .select("id, source, ad_number, make, subject, unsafe_condition, corrective_action, pdf_link, raw_data, superseded_to_ad_id")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        rows = resp.data or []
        for r in rows:
            yield r
            fetched += 1
            if limit and fetched >= limit:
                return
        if len(rows) < PAGE:
            return
        offset += len(rows)


def strip_rev(num: str) -> str:
    return re.sub(r"R\d+$", "", num)


# ---------------------------------------------------------------------------
# Layer A — referencias explicitas
# ---------------------------------------------------------------------------
def build_reference_rows(sb, limit, stats, samples):
    ref_cache = load_json(REF_CACHE_PATH)
    index: dict[tuple[str, str], dict] = {}   # (source, ad_number) -> row
    chain_partner: dict[str, set[str]] = defaultdict(set)  # id -> ids en su cadena
    proposals = []  # (row, foreign_source, foreign_number)

    rows = list(fetch_all_ads(sb, limit))
    for r in rows:
        index[(r["source"], r["ad_number"])] = r
        if r.get("superseded_to_ad_id"):
            chain_partner[r["id"]].add(r["superseded_to_ad_id"])
            chain_partner[r["superseded_to_ad_id"]].add(r["id"])

    for r in rows:
        stats["scanned"] += 1
        src = r["source"]

        # Bonus: adopcion literal EASA (US-2002-08-03 == FAA 2002-08-03;
        # CF-2011-06 == TC CF-2011-06 — TC conserva el prefijo CF- en su numero).
        if src == "easa":
            m = re.match(r"^([A-Z]{2})-(.+)$", r["ad_number"])
            if m and m.group(1) in ADOPTION_PREFIX:
                target_num = r["ad_number"] if m.group(1) == "CF" else m.group(2)
                proposals.append((r, ADOPTION_PREFIX[m.group(1)], target_num, "EASA national-adoption number"))

        raw = raw_dict(r)
        corpus = " ".join(
            filter(None, [r.get("subject"), r.get("unsafe_condition"),
                          r.get("corrective_action"), raw.get("Abstract")])
        )

        # PDF cacheado: refs extraidas una vez y cacheadas por sha1 del link
        link = r.get("pdf_link") or ""
        if link:
            sha = hashlib.sha1(link.encode()).hexdigest()
            if sha in ref_cache:
                pdf_refs = ref_cache[sha]
            else:
                text = pdf_text(link)
                pdf_refs = {}
                if text:
                    for fsrc, pat in FOREIGN_PATTERNS.items():
                        found = list(dict.fromkeys(m.upper() if fsrc == "transport_canada" else m
                                                   for m in pat.findall(text)))
                        if found:
                            pdf_refs[fsrc] = found[:10]
                ref_cache[sha] = pdf_refs
                stats["pdf_parsed"] += 1
        else:
            pdf_refs = {}

        for fsrc, pat in FOREIGN_PATTERNS.items():
            if fsrc == src:
                continue  # referencias a la propia autoridad = supersession, no equivalencia
            nums = list(dict.fromkeys(
                (m.upper() if fsrc == "transport_canada" else m) for m in pat.findall(corpus)
            ))
            for extra in pdf_refs.get(fsrc, []):
                if extra not in nums:
                    nums.append(extra)
            for num in nums:
                if num == r["ad_number"]:
                    continue
                proposals.append((r, fsrc, num, None))

    save_json(REF_CACHE_PATH, ref_cache)

    out = []
    seen = set()
    for item in proposals:
        r, fsrc, num, note = item
        target = index.get((fsrc, num)) or index.get((fsrc, strip_rev(num)))
        tgt_id = target["id"] if target else None
        # excluir pares que ya estan unidos por cadena de supersession
        if tgt_id and tgt_id in chain_partner.get(r["id"], set()):
            stats["chain_skipped"] += 1
            continue
        key = (r["id"], fsrc, num)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ad_id": r["id"],
            "equivalent_ad_id": tgt_id,
            "equivalent_source": fsrc,
            "equivalent_ad_number": num,
            "match_level": "full",
            "method": "reference",
            "note": note or f"Referenced in {r['source']} {r['ad_number']}",
        })
        if len(samples) < SAMPLE:
            samples.append(f"  REF {r['source']} {r['ad_number']} -> {fsrc} {num}{' (resolved)' if tgt_id else ''}")
        # espejo cuando el target existe
        if target:
            mkey = (target["id"], r["source"], r["ad_number"])
            if mkey not in seen:
                seen.add(mkey)
                out.append({
                    "ad_id": target["id"],
                    "equivalent_ad_id": r["id"],
                    "equivalent_source": r["source"],
                    "equivalent_ad_number": r["ad_number"],
                    "match_level": "full",
                    "method": "reference",
                    "note": f"Cited by {r['source']} {r['ad_number']}",
                })
    stats["reference_rows"] = len(out)
    return out


# ---------------------------------------------------------------------------
# Layer B — service bulletins compartidos
# ---------------------------------------------------------------------------
def norm_sb(sb_number: str) -> str:
    s = (sb_number or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    return re.sub(r"(?:REV|R)[A-Z0-9]{0,2}$", "", s)  # strip sufijo de revision


def build_shared_sb_rows(sb, limit, stats, samples, reference_pairs: set):
    # ads minimos
    ads: dict[str, dict] = {}
    for r in fetch_all_ads(sb, limit):
        ads[r["id"]] = {"source": r["source"], "ad_number": r["ad_number"], "make": r.get("make") or ""}

    # ad_service_bulletins paginado
    by_sb: dict[str, list[str]] = defaultdict(list)
    offset = 0
    while True:
        resp = (
            sb.table("ad_service_bulletins")
            .select("ad_id, sb_number")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        rows = resp.data or []
        for row in rows:
            n = norm_sb(row.get("sb_number") or "")
            if len(n) < SB_MIN_LEN or not re.search(r"\d", n):
                continue  # token generico / ruido
            if row["ad_id"] in ads:
                by_sb[n].append(row["ad_id"])
        if len(rows) < PAGE:
            break
        offset += len(rows)

    out = []
    seen = set()
    for sb_num, ad_ids in by_sb.items():
        uniq = list(dict.fromkeys(ad_ids))
        if len(uniq) < 2 or len(uniq) > SB_FREQ_CAP:
            if len(uniq) > SB_FREQ_CAP:
                stats["sb_capped"] += 1
            continue
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                ra, rb = ads[a], ads[b]
                if ra["source"] == rb["source"]:
                    continue
                if not same_make(ra["make"], rb["make"]):
                    stats["sb_make_mismatch"] += 1
                    continue
                for x, y in ((a, b), (b, a)):
                    rx, ry = ads[x], ads[y]
                    key = (x, ry["source"], ry["ad_number"])
                    if key in seen or key in reference_pairs:
                        continue
                    seen.add(key)
                    out.append({
                        "ad_id": x,
                        "equivalent_ad_id": y,
                        "equivalent_source": ry["source"],
                        "equivalent_ad_number": ry["ad_number"],
                        "match_level": "partial",
                        "method": "shared_sb",
                        "note": f"Both reference SB {sb_num}",
                    })
                if len(samples) < SAMPLE:
                    samples.append(
                        f"  SB  {ra['source']} {ra['ad_number']} <-> {rb['source']} {rb['ad_number']} (SB {sb_num})"
                    )
    stats["shared_sb_rows"] = len(out)
    return out


STAGE_CHUNK = 2000
# Cota inferior del swap: un lote con menos del 70% de las filas vivas del metodo
# se rechaza (algo se rompio aguas arriba). Con la tabla vacia no aplica.
MIN_RATIO = 0.7


def canonical_checksum(rows: list[dict]) -> str:
    """
    Canonicalizacion COMPARTIDA con public.ad_equivalents_checksum_rows (SQL).
    Filas ordenadas por la clave natural; cada fila son los 5 campos en orden
    fijo con prefijo de longitud en bytes (`<n>:<valor>`) unidos por '|'; filas
    unidas por '\\n'; UTF-8; SHA-256 hex. El prefijo de longitud es lo que hace
    inofensivo un '|' o un salto de linea dentro de una nota.
    Vector de prueba fijo en scripts/test_equivalents_checksum.py.
    """
    def field(v: str) -> str:
        return f"{len(v.encode('utf-8'))}:{v}"

    lines = []
    for r in rows:
        lines.append((
            (str(r["ad_id"]), str(r["equivalent_source"]), str(r["equivalent_ad_number"])),
            "|".join([
                field(str(r["ad_id"])),
                field(str(r["equivalent_source"])),
                field(str(r["equivalent_ad_number"])),
                field(str(r.get("match_level") or "candidate")),
                field(str(r.get("note") or "")),
            ]),
        ))
    # El ORDER BY de Postgres sobre el sort_key (campos unidos por chr(31)) y el
    # sort de Python sobre la tupla coinciden porque chr(31) es menor que
    # cualquier caracter imprimible: la comparacion nunca cruza el separador.
    lines.sort(key=lambda t: "\x1f".join(t[0]))
    payload = "\n".join(line for _, line in lines)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def replace_method_rows(sb, method: str, rows: list[dict]):
    """
    Swap por staging con generacion y fencing (INT-4). El delete+insert vive
    dentro de commit_ad_equivalents, en una sola transaccion validada por conteo,
    cota inferior y checksum: un fallo aguas arriba ya no puede dejar la capa del
    metodo vacia para todos los clientes.
    """
    run_id = sb.rpc("open_ad_equivalents_run", {"p_method": method}).execute().data

    staged = 0
    for i in range(0, len(rows), STAGE_CHUNK):
        chunk = [
            {
                "ad_id": r["ad_id"],
                "equivalent_ad_id": r.get("equivalent_ad_id"),
                "equivalent_source": r["equivalent_source"],
                "equivalent_ad_number": r["equivalent_ad_number"],
                "match_level": r.get("match_level") or "candidate",
                "note": r.get("note"),
            }
            for r in rows[i : i + STAGE_CHUNK]
        ]
        staged = sb.rpc("stage_ad_equivalents", {
            "p_run_id": run_id, "p_rows": chunk,
        }).execute().data

    committed = sb.rpc("commit_ad_equivalents", {
        "p_run_id": run_id,
        "p_method": method,
        "p_expected_count": staged,
        "p_min_ratio": MIN_RATIO,
        "p_checksum": canonical_checksum(rows),
    }).execute().data
    return committed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--layer", default="all", choices=["all", "reference", "shared_sb"])
    ap.add_argument("--execute", action="store_true", help="Escribir a la DB (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="Max filas de ads a escanear")
    args = ap.parse_args()

    sb = get_client()
    stats = defaultdict(int)
    samples: list[str] = []
    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[build_equivalents] mode={mode} layer={args.layer}")

    ref_rows: list[dict] = []
    if args.layer in ("all", "reference"):
        ref_rows = build_reference_rows(sb, args.limit, stats, samples)
        if args.execute:
            replace_method_rows(sb, "reference", ref_rows)
            print(f"  reference: {len(ref_rows)} filas escritas")

    if args.layer in ("all", "shared_sb"):
        reference_pairs = {(r["ad_id"], r["equivalent_source"], r["equivalent_ad_number"]) for r in ref_rows}
        sb_rows = build_shared_sb_rows(sb, args.limit, stats, samples, reference_pairs)
        if args.execute:
            replace_method_rows(sb, "shared_sb", sb_rows)
            print(f"  shared_sb: {len(sb_rows)} filas escritas")

    print("\n=== stats ===")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")
    if samples:
        print(f"\n=== muestra (max {SAMPLE}) ===")
        for s in samples:
            print(s)
    if not args.execute:
        print("\nDry-run: nada escrito. Repetir con --execute para aplicar.")


if __name__ == "__main__":
    main()
