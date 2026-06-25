"""
Zephr - scripts/validate_ads.py
Consulta la tabla 'ads' en Supabase y genera un reporte de calidad de datos.

Categorías:
  ✅ Completos  → tienen product_type, status, pdf_link, effective_date
  ⚠️  Parciales  → faltan algunos campos PERO pdf_link existe
  ❌ Críticos   → pdf_link es NULL  O  ad_number duplicado en la fuente

Output:
  - Reporte en consola con colores ANSI
  - CSV en outputs/ads_to_rescrape.csv con los ADs que necesitan re-scraping

Uso:
    python scripts/validate_ads.py
"""

import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Forzar UTF-8 en Windows para evitar UnicodeEncodeError con colores ANSI
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from supabase import create_client

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).parent.parent / ".env")

OUTPUT_DIR = Path(__file__).parent.parent / "outputs"
OUTPUT_CSV  = OUTPUT_DIR / "ads_to_rescrape.csv"

# Campos que deben estar presentes para considerar un AD "completo"
REQUIRED_FIELDS = ["product_type", "status", "pdf_link", "effective_date"]

# Colores ANSI
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_ANON_KEY", "")).strip()
    if not url or not key:
        sys.exit("ERROR: Define SUPABASE_URL y SUPABASE_SERVICE_KEY en .env")
    return create_client(url, key)


def classify(ad: dict) -> tuple[str, list[str]]:
    """
    Devuelve (categoría, lista_de_campos_faltantes).
    Categorías: 'complete' | 'partial' | 'critical'
    """
    missing = [f for f in REQUIRED_FIELDS if not ad.get(f)]

    if not ad.get("pdf_link"):          # pdf_link ausente → siempre crítico
        return "critical", missing
    if not missing:                     # todos los campos presentes
        return "complete", []
    return "partial", missing           # pdf_link existe pero faltan otros


def fetch_all_ads(client) -> list[dict]:
    """Descarga todos los registros de la tabla 'ads' en páginas de 1000."""
    all_rows, page, page_size = [], 0, 1000
    while True:
        resp = (
            client.table("ads")
            .select(
                "source_label, ad_number, product_type, status, pdf_link, effective_date,"
                "unsafe_condition, corrective_action, compliance_time"
            )
            .range(page * page_size, (page + 1) * page_size - 1)
            .execute()
        )
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return all_rows


AI_FIELDS = ["unsafe_condition", "corrective_action", "compliance_time"]


def print_ai_coverage(rows: list[dict]) -> None:
    """Imprime tabla de cobertura de campos semanticos por fuente."""
    print(f"\n{BOLD}--- Cobertura AI enrichment (unsafe / corrective / compliance) ---{RESET}\n")

    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_source[r["source_label"] or "Unknown"].append(r)

    col_w = max(len(s) for s in by_source) + 2
    header = f"{'Source':<{col_w}} {'Total':>7}  {'Unsafe':>8}  {'Corrective':>11}  {'Compliance':>11}  {'Any':>7}"
    print(header)
    print("-" * len(header))

    total_all = 0
    total_any = 0
    for source in sorted(by_source):
        source_rows = by_source[source]
        n = len(source_rows)
        unsafe = sum(1 for r in source_rows if r.get("unsafe_condition"))
        corrective = sum(1 for r in source_rows if r.get("corrective_action"))
        compliance = sum(1 for r in source_rows if r.get("compliance_time"))
        any_filled = sum(1 for r in source_rows if any(r.get(f) for f in AI_FIELDS))
        pct = any_filled / n * 100 if n else 0
        color = GREEN if pct >= 80 else (YELLOW if pct >= 30 else RED)
        print(
            f"{source:<{col_w}} {n:>7}  {unsafe:>8}  {corrective:>11}  {compliance:>11}  "
            f"{color}{any_filled:>5} {pct:>4.0f}%{RESET}"
        )
        total_all += n
        total_any += any_filled

    print("-" * len(header))
    pct_total = total_any / total_all * 100 if total_all else 0
    color = GREEN if pct_total >= 80 else (YELLOW if pct_total >= 30 else RED)
    print(
        f"{'TOTAL':<{col_w}} {total_all:>7}  {'':>8}  {'':>11}  {'':>11}  "
        f"{color}{total_any:>5} {pct_total:>4.0f}%{RESET}"
    )
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"\n{BOLD}=== Zephr — Reporte de calidad de datos ==={RESET}\n")

    rows = fetch_all_ads(get_client())
    print(f"Total ADs en BD: {BOLD}{len(rows)}{RESET}\n")

    # --- Detectar duplicados por (source_label, ad_number) ---
    seen_keys: Counter = Counter()
    for r in rows:
        seen_keys[(r["source_label"], r["ad_number"])] += 1
    duplicate_keys = {k for k, v in seen_keys.items() if v > 1}

    # --- Clasificar por source ---
    # stats[source] = {"complete": 0, "partial": 0, "critical": 0}
    stats: dict[str, Counter] = defaultdict(Counter)
    to_rescrape: list[dict] = []

    for ad in rows:
        source = ad["source_label"] or "Unknown"
        is_dup = (source, ad["ad_number"]) in duplicate_keys

        if is_dup:
            category, missing = "critical", ["duplicate_ad_number"]
        else:
            category, missing = classify(ad)

        stats[source][category] += 1

        if category in ("partial", "critical"):
            to_rescrape.append({
                "source":         source,
                "ad_number":      ad["ad_number"],
                "missing_fields": ", ".join(missing) if missing else "duplicate",
            })

    # --- Imprimir reporte por source ---
    col_w = max(len(s) for s in stats) + 2
    print(f"{'Source':<{col_w}} {'[OK] Completos':>15}  {'[!!] Parciales':>15}  {'[X] Criticos':>13}")
    print("-" * (col_w + 50))

    total = Counter()
    for source in sorted(stats):
        c = stats[source]
        total.update(c)
        print(
            f"{source:<{col_w}} "
            f"{GREEN}{c['complete']:>15}{RESET}  "
            f"{YELLOW}{c['partial']:>15}{RESET}  "
            f"{RED}{c['critical']:>13}{RESET}"
        )

    print("-" * (col_w + 50))
    print(
        f"{'TOTAL':<{col_w}} "
        f"{GREEN}{total['complete']:>15}{RESET}  "
        f"{YELLOW}{total['partial']:>15}{RESET}  "
        f"{RED}{total['critical']:>13}{RESET}"
    )

    pct_ok = total["complete"] / len(rows) * 100 if rows else 0
    print(f"\nCalidad global: {BOLD}{pct_ok:.1f}%{RESET} completos\n")

    print_ai_coverage(rows)

    # --- Guardar CSV ---
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "ad_number", "missing_fields"])
        writer.writeheader()
        writer.writerows(to_rescrape)

    print(f"CSV guardado: {BOLD}{OUTPUT_CSV}{RESET}  ({len(to_rescrape)} ADs para re-scraping)\n")


if __name__ == "__main__":
    main()
