"""
Backfill de supersession para las filas existentes de `ads` SIN re-scrapear.

`ads.superseded_to_ad_id` esta NULL en toda la tabla porque los scrapers nunca
emitieron Supersedes/Superseded_By (ver scrapers/*.py, ya corregidos para
corridas futuras). Este script deriva los links desde datos ya disponibles:

  * easa              -> raw_data + outputs/easa_detail_cache.json (miles de
                         supersessions ya parseadas que nunca llegaron a la DB)
                         + regla implicita de revisiones (2024-0195R1 -> 2024-0195).
  * federal_register  -> regex sobre raw_data.Abstract ("The FAA is superseding
                         AD X" / "replaces AD X") + outputs/fr_fulltext_cache.json.
  * transport_canada  -> regex sobre subject "(Superseded by CF-...)" +
                         outputs/pdf_cache/{sha1(pdf_link)}.pdf via pypdf.

Resolucion SAME-SOURCE only: la cadena regulatoria vive dentro de una autoridad;
las relaciones cross-authority son ad_equivalents (scripts/build_equivalents.py).

Uso:
  python scripts/link_supersession.py [--source all|easa|federal_register|transport_canada]
                                      [--execute] [--limit N] [--llm] [--stats-only]

Por defecto DRY-RUN: imprime stats + muestra de links propuestos, no escribe.
Cada update setea superseded_to_ad_id + status='Superseded' juntos (CHECK de
coherencia de la DB) y solo sobre filas que sigan sin link (no overwrite).
"""

import argparse
import hashlib
import io
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

from supabase import create_client  # noqa: E402
from utils import normalize_ad_number  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUTS = os.path.join(ROOT, "outputs")
PAGE = 1000
UPDATE_SAMPLE = 20

SOURCES = ["easa", "federal_register", "transport_canada"]

# Formato de numero por autoridad (validacion de targets, incluye respuesta LLM)
AD_FORMAT = {
    "federal_register": re.compile(r"^(?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?$", re.I),
    "easa": re.compile(r"^(?:[A-Z]{1,2}-)?\d{4}-\d{3,4}(?:R\d+)?$", re.I),
    "transport_canada": re.compile(r"^CF-\d{2,4}-\d{2,3}(?:R\d+)?$", re.I),
}

# FAA: "The FAA is superseding AD 2018-23-51" / "This AD replaces AD 2020-06-14"
_FAA_SUP_RE = re.compile(
    r"(?:supersed(?:es|ing)|replaces)\s+(?:Emergency\s+)?"
    r"(?:Airworthiness\s+Directive\s*\(?AD\)?|AD)\s*"
    r"((?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?)(?![-\d])",
    re.I,
)
# TC subject: "(Superseded by CF-2019-33)"
_TC_SUBJ_RE = re.compile(r"superseded\s+by\s+(CF-\d{2,4}-\d{2,3}(?:R\d+)?)", re.I)
# TC PDF: "This directive supersedes CF-2015-19"
_TC_PDF_RE = re.compile(
    r"(?:this\s+(?:directive|AD)\s+)?supersedes\s+(?:AD\s+|Airworthiness\s+Directive\s+)?"
    r"(CF-\d{2,4}-\d{2,3}(?:R\d+)?)(?![-\d])",
    re.I,
)


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


def pdf_cache_text(pdf_link: str) -> str:
    """Texto del PDF cacheado por enrich_ai (outputs/pdf_cache/{sha1}.pdf), si existe."""
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


# ---------------------------------------------------------------------------
# Extraccion de señales por fuente.
# Devuelve (superseded_by: str|None, supersedes: list[str]) en numeros crudos.
# ---------------------------------------------------------------------------
def extract_easa(row: dict, detail_cache: dict) -> tuple[str | None, list[str]]:
    raw = raw_dict(row)
    by = (raw.get("Superseded_By") or "").strip() or None
    sup = [t.strip() for t in (raw.get("Supersedes") or "").split(",") if t.strip()]

    if not by and not sup:
        d = detail_cache.get(row["ad_number"]) or {}
        by = (d.get("superseded_by") or "").strip() or None
        sup = [t for t in (d.get("supersedes_all") or []) if t]
        if not sup and d.get("supersedes"):
            sup = [d["supersedes"]]

    # Regla implicita de revisiones: NNNN-NNNNRn supersede la revision previa.
    if not sup:
        m = re.match(r"^(.+?)R(\d+)$", row["ad_number"])
        if m:
            base, n = m.group(1), int(m.group(2))
            sup = [f"{base}R{n - 1}" if n > 1 else base]
    return by, sup


def extract_faa(row: dict, ft_cache: dict) -> tuple[str | None, list[str]]:
    raw = raw_dict(row)
    abstract = raw.get("Abstract") or ""
    sup = [normalize_ad_number(n) for n in dict.fromkeys(_FAA_SUP_RE.findall(abstract))]
    if not sup:
        doc = raw.get("FR_DocNumber") or ""
        entry = ft_cache.get(doc) or {}
        sup = [normalize_ad_number(n) for n in (entry.get("supersedes") or [])]
    return None, sup


def extract_tc(row: dict) -> tuple[str | None, list[str]]:
    m = _TC_SUBJ_RE.search(row.get("subject") or "")
    if m:
        return m.group(1).upper(), []
    text = pdf_cache_text(row.get("pdf_link") or "")
    if text:
        m = _TC_PDF_RE.search(text)
        if m:
            return None, [m.group(1).upper()]
    return None, []


# ---------------------------------------------------------------------------
# Fallback LLM (Ollama, mismo endpoint/modelo que enrich_ai)
# ---------------------------------------------------------------------------
LLM_SYSTEM = (
    "You read aviation Airworthiness Directive text. Return ONLY a JSON object:\n"
    '{"superseded_by": "<AD number>" | null, "supersedes": "<AD number>" | null}\n'
    "Use the exact AD number as written, from the SAME issuing authority as the "
    "document (FAA: NNNN-NN-NN, EASA: NNNN-NNNN, Transport Canada: CF-NNNN-NN). "
    "null if the text does not state a supersession. Never guess."
)


_llm_session = None


def llm_extract(row: dict, source: str, text: str) -> tuple[str | None, list[str]]:
    from enrich_ai import _post_chat, _parse_json_loose  # lazy: solo con --llm
    import requests

    global _llm_session
    if _llm_session is None:
        _llm_session = requests.Session()

    endpoint = os.environ.get("LLM_ENDPOINT", "http://localhost:11434/v1")
    model = os.environ.get("LLM_MODEL", "qwen-ad")
    api_key = os.environ.get("LLM_API_KEY", "")
    user = (
        f"Authority: {source}\nAD: {row['ad_number']}\n"
        f"--- TEXT ---\n{text[:8000]}"
    )
    try:
        content = _post_chat(endpoint, model, api_key, LLM_SYSTEM, user, _llm_session)
        obj = _parse_json_loose(content) or {}
    except Exception:
        return None, []
    fmt = AD_FORMAT[source]
    by = obj.get("superseded_by")
    sup = obj.get("supersedes")
    by = by.strip() if isinstance(by, str) and fmt.match(by.strip()) else None
    sup_list = [sup.strip()] if isinstance(sup, str) and fmt.match(sup.strip()) else []
    return by, sup_list


# ---------------------------------------------------------------------------
def fetch_rows(sb, source: str, limit: int | None):
    """Filas del source sin link, paginadas."""
    offset = 0
    fetched = 0
    while True:
        q = (
            sb.table("ads")
            .select("id, ad_number, status, subject, pdf_link, raw_data, superseded_to_ad_id")
            .eq("source", source)
            .is_("superseded_to_ad_id", "null")
            .order("ad_number")
            .range(offset, offset + PAGE - 1)
        )
        rows = q.execute().data or []
        for r in rows:
            yield r
            fetched += 1
            if limit and fetched >= limit:
                return
        if len(rows) < PAGE:
            return
        offset += len(rows)


def fetch_ids(sb, source: str, numbers: list[str]) -> dict[str, str]:
    ids: dict[str, str] = {}
    uniq = list({n for n in numbers if n})
    for i in range(0, len(uniq), 300):
        chunk = uniq[i : i + 300]
        try:
            resp = (
                sb.table("ads")
                .select("id, ad_number, superseded_to_ad_id")
                .eq("source", source)
                .in_("ad_number", chunk)
                .execute()
            )
            for r in resp.data or []:
                ids[r["ad_number"]] = r["id"]
        except Exception as e:
            print(f"  [warn] fetch_ids fallo: {e}")
    return ids


def strip_rev(num: str) -> str:
    return re.sub(r"R\d+$", "", num)


def run_source(sb, source: str, args, stats: dict, samples: list):
    detail_cache = load_json(os.path.join(OUTPUTS, "easa_detail_cache.json")) if source == "easa" else {}
    ft_cache = load_json(os.path.join(OUTPUTS, "fr_fulltext_cache.json")) if source == "federal_register" else {}

    # (row_id, row_num, direction, target_num)
    # direction "self": row queda superseded_by target (pointer en row)
    # direction "older": target (mas viejo) queda superseded_by row (pointer en target)
    pending: list[tuple[str, str, str, str]] = []
    llm_candidates: list[dict] = []

    for row in fetch_rows(sb, source, args.limit):
        stats["scanned"] += 1
        if source == "easa":
            by, sup = extract_easa(row, detail_cache)
        elif source == "federal_register":
            by, sup = extract_faa(row, ft_cache)
        else:
            by, sup = extract_tc(row)

        if not by and not sup and args.llm:
            status = (row.get("status") or "").lower()
            subject = (row.get("subject") or "").lower()
            if "supersed" in status or "supersed" in subject:
                llm_candidates.append(row)
                continue

        fmt = AD_FORMAT[source]
        if by:
            by = normalize_ad_number(by)
            if fmt.match(by):
                pending.append((row["id"], row["ad_number"], "self", by))
            else:
                stats["bad_format"] += 1
        for t in sup:
            t = normalize_ad_number(t)
            if t and t != row["ad_number"]:
                if fmt.match(t):
                    pending.append((row["id"], row["ad_number"], "older", t))
                else:
                    stats["bad_format"] += 1

    # LLM fallback (pocas filas; secuencial)
    for row in llm_candidates:
        raw = raw_dict(row)
        text = raw.get("Abstract") or pdf_cache_text(row.get("pdf_link") or "")
        if len(text) < 200:
            stats["llm_no_text"] += 1
            continue
        by, sup = llm_extract(row, source, text)
        stats["llm_called"] += 1
        if by:
            pending.append((row["id"], row["ad_number"], "self", normalize_ad_number(by)))
        for t in sup:
            pending.append((row["id"], row["ad_number"], "older", normalize_ad_number(t)))

    stats["signal_found"] += len(pending)
    if not pending:
        return

    # Resolver targets -> ids (same source), fallback sin sufijo R
    all_nums = [t for _, _, _, t in pending] + [n for _, n, _, _ in pending]
    id_map = fetch_ids(sb, source, all_nums + [strip_rev(n) for n in all_nums])

    # updates: row_a (viejo) -> apunta a row_b (nuevo). Dedup por row_a, primero gana.
    updates: dict[str, tuple[str, str, str]] = {}  # old_id -> (new_id, old_num, new_num)
    for row_id, row_num, direction, target_num in pending:
        tgt_id = id_map.get(target_num) or id_map.get(strip_rev(target_num))
        if not tgt_id:
            stats["unresolved"] += 1
            if sum(1 for s in samples if "UNRESOLVED" in s) < 5 and len(samples) < UPDATE_SAMPLE:
                samples.append(f"  UNRESOLVED {source} {row_num} -> {target_num}")
            continue
        if direction == "self":
            old_id, new_id, old_num, new_num = row_id, tgt_id, row_num, target_num
        else:
            old_id, new_id, old_num, new_num = tgt_id, row_id, target_num, row_num
        if old_id == new_id:
            stats["self_skipped"] += 1
            continue
        # guard de 2-ciclo: si el "nuevo" ya apunta al "viejo", no cerrar el ciclo
        prev = updates.get(new_id)
        if prev and prev[0] == old_id:
            stats["cycles_skipped"] += 1
            continue
        updates.setdefault(old_id, (new_id, old_num, new_num))

    stats["resolved"] += len(updates)
    for old_id, (new_id, old_num, new_num) in updates.items():
        if len(samples) < UPDATE_SAMPLE:
            samples.append(f"  LINK {source} {old_num} superseded by {new_num}")
        if not args.execute:
            continue
        try:
            sb.table("ads").update(
                {"superseded_to_ad_id": new_id, "status": "Superseded"}
            ).eq("id", old_id).is_("superseded_to_ad_id", "null").execute()
            stats["updated"] += 1
        except Exception as e:
            stats["update_failed"] += 1
            print(f"  [warn] update fallo {old_num}: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="all", choices=["all"] + SOURCES)
    ap.add_argument("--execute", action="store_true", help="Escribir a la DB (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="Max filas a escanear por source")
    ap.add_argument("--llm", action="store_true", help="Fallback Ollama para filas sin match regex")
    ap.add_argument("--stats-only", action="store_true", help="Solo contadores, sin muestra")
    args = ap.parse_args()

    sb = get_client()
    sources = SOURCES if args.source == "all" else [args.source]
    stats = {k: 0 for k in (
        "scanned", "signal_found", "resolved", "unresolved", "bad_format", "updated",
        "update_failed", "self_skipped", "cycles_skipped", "llm_called", "llm_no_text",
    )}
    samples: list[str] = []

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[link_supersession] mode={mode} sources={sources} llm={args.llm}")
    for source in sources:
        run_source(sb, source, args, stats, samples)

    print("\n=== stats ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if samples and not args.stats_only:
        print(f"\n=== muestra (max {UPDATE_SAMPLE}) ===")
        for s in samples:
            print(s)
    if not args.execute:
        print("\nDry-run: nada escrito. Repetir con --execute para aplicar.")


if __name__ == "__main__":
    main()
