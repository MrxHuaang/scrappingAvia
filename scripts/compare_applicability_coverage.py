"""
Applicability coverage for FAA ADs: legacy (make, model) heuristic vs the
Federal Register parser in scrapers/common/fr_applicability.py.

Runs offline over a JSON dump; it never calls the FR API or Supabase. Accepted
inputs (auto-detected per item):

  * FR API documents:   {"title", "abstract", ...}
                        (a list, {"results": [...]}, or {"<year>": [...]})
  * scraper records:    {"Subject", "Abstract", ...}  (federal_register output)
  * `ads` rows:         {"subject", "raw_data": {...}}
  * test fixtures:      tests/fixtures/fr_applicability.json

For every AD it reports whether each extractor produced a holder with models,
a holder without models (model_status = unknown), or no holder at all (the AD
travels `failed` and keeps its stored rows), plus how often the two disagree.

Usage:
    python scripts/compare_applicability_coverage.py dump.json
    python scripts/compare_applicability_coverage.py dump.json --examples 15
    python scripts/compare_applicability_coverage.py dump.json --json out.json
"""

import argparse
import json
import re
import sys
from collections import Counter

from scrapers.common.fr_applicability import extract_applicability

_TITLE_PREFIX = "Airworthiness Directives; "

# ---------------------------------------------------------------------------
# Legacy baseline: frozen copy of the removed utils.extract_make_model, kept
# here ONLY as the "before" side of the comparison. It first-matched a fixed
# list of 38 makes anywhere in title + abstract and took one model token.
# Nothing in the pipeline uses it.
# ---------------------------------------------------------------------------
_LEGACY_MAKES = [
    "The Boeing Company", "Boeing", "Airbus", "Airbus S.A.S.",
    "Bombardier", "Embraer", "Cessna", "Textron Aviation",
    "Gulfstream", "Dassault", "Pilatus", "Beechcraft",
    "Piper", "Mooney", "Cirrus", "Diamond",
    "Rolls-Royce", "General Electric", "Pratt & Whitney",
    "CFM International", "Honeywell", "Safran",
    "Bell", "Sikorsky", "Leonardo", "Robinson",
    "de Havilland", "ATR", "Saab", "BAE Systems",
    "McDonnell Douglas", "Douglas", "Lockheed", "Fokker",
    "Israel Aerospace", "Mitsubishi", "Hawker", "Learjet",
]
_LEGACY_MODEL_PATTERNS = [
    r"\b(737[-\s]?\d{0,4}(?:[-/]\d+)?(?:\s*(?:MAX|NG))?)\b",
    r"\b(A\s?3[0-9]{2}(?:[-/]\d+)?(?:neo)?)\b",
    r"\b(777[-\s]?\d{0,4})\b",
    r"\b(787[-\s]?\d{0,4})\b",
    r"\b(747[-\s]?\d{0,4})\b",
    r"\b(CL[-\s]?\d{3,4})\b",
    r"\b(DHC[-\s]?\d+)\b",
]


def legacy_make_model(title: str, abstract: str) -> tuple[str, str]:
    text = f"{title or ''} {abstract or ''}"
    low = text.lower()
    make = next((k for k in _LEGACY_MAKES if k.lower() in low), "")
    m = re.search(r"Model\s+([\w\-/]+(?:\s+[\w\-/]+)?)", text)
    model = m.group(1) if m else ""
    if not model:
        for pat in _LEGACY_MODEL_PATTERNS:
            m = re.search(pat, text, re.I)
            if m:
                model = m.group(1)
                break
    return make, model


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def iter_items(data):
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict):
        if isinstance(data.get("results"), list):
            yield from data["results"]
        else:
            for value in data.values():
                if isinstance(value, list):
                    yield from value


def title_abstract(item: dict) -> tuple[str, str, str]:
    """(id, title, abstract) for any of the accepted item shapes."""
    raw = item.get("raw_data") if isinstance(item.get("raw_data"), dict) else item
    title = item.get("title") or raw.get("title") or ""
    if not title:
        subject = raw.get("Subject") or item.get("subject") or ""
        title = f"{_TITLE_PREFIX}{subject}" if subject else ""
    abstract = item.get("abstract") or raw.get("Abstract") or raw.get("abstract") or ""
    ident = (item.get("ad_number") or raw.get("AD_Number") or item.get("document_number")
             or raw.get("FR_DocNumber") or "")
    return str(ident), title, abstract


def bucket(holder: str, models: str) -> str:
    if not holder:
        return "no_holder"
    return "holder_with_models" if models else "holder_without_models"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def compare(items) -> dict:
    counts = {"legacy": Counter(), "new": Counter()}
    agreement = Counter()
    transitions = Counter()
    examples: dict[str, list] = {}
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        ident, title, abstract = title_abstract(item)
        if not title:
            continue
        total += 1
        old_make, old_model = legacy_make_model(title, abstract)
        new = extract_applicability(title, abstract)
        new_holder, new_models = new.holder or "", ", ".join(new.models)

        old_b, new_b = bucket(old_make, old_model), bucket(new_holder, new_models)
        counts["legacy"][old_b] += 1
        counts["new"][new_b] += 1
        transitions[f"{old_b} -> {new_b}"] += 1
        if new.previously_held_by:
            counts["new"]["with_previous_holder"] += 1

        same_pair = (old_make, old_model) == (new_holder, new_models)
        if same_pair:
            agreement["same_pair"] += 1
        else:
            if old_make and new_holder and old_make.casefold() not in new_holder.casefold():
                agreement["holder_differs"] += 1
            if old_model and new_models and old_model not in new.models:
                agreement["model_differs"] += 1
            if len(new.models) > 1:
                agreement["new_lists_more_models"] += 1
            examples.setdefault(f"{old_b} -> {new_b}", []).append({
                "id": ident, "title": title[len(_TITLE_PREFIX):] if title.startswith(_TITLE_PREFIX) else title,
                "legacy": [old_make, old_model], "new": [new_holder, list(new.models)],
            })
    return {"total": total, "counts": {k: dict(v) for k, v in counts.items()},
            "agreement": dict(agreement), "transitions": dict(transitions), "examples": examples}


def _pct(n: int, total: int) -> str:
    return f"{n:6d}  ({(100 * n / total if total else 0):5.1f}%)"


def print_report(report: dict, n_examples: int) -> None:
    total = report["total"]
    print(f"ADs analyzed: {total}\n")
    print(f"{'':28s}{'legacy':>18s}{'new':>18s}")
    for key, label in (("holder_with_models", "holder + models"),
                       ("holder_without_models", "holder, no model (unknown)"),
                       ("no_holder", "no holder (failed)")):
        print(f"{label:28s}{_pct(report['counts']['legacy'].get(key, 0), total):>18s}"
              f"{_pct(report['counts']['new'].get(key, 0), total):>18s}")
    prev = report["counts"]["new"].get("with_previous_holder", 0)
    print(f"{'previous TC holder kept':28s}{'-':>18s}{_pct(prev, total):>18s}")
    print("\nDisagreements (legacy vs new):")
    for key in ("same_pair", "holder_differs", "model_differs", "new_lists_more_models"):
        print(f"  {key:24s}{_pct(report['agreement'].get(key, 0), total)}")
    print("\nTransitions:")
    for key, n in sorted(report["transitions"].items(), key=lambda kv: -kv[1]):
        print(f"  {key:52s}{n:6d}")
    if n_examples:
        for key, rows in report["examples"].items():
            print(f"\n[{key}] first {min(n_examples, len(rows))} of {len(rows)}")
            for r in rows[:n_examples]:
                print(f"  {r['id']}: {r['title'][:90]}")
                print(f"      legacy={r['legacy']}  new={r['new']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("dump", help="JSON dump of FR documents, scraper records or ads rows")
    parser.add_argument("--examples", type=int, default=0, help="changed ADs to print per transition")
    parser.add_argument("--json", dest="json_out", help="also write the full report here")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with open(args.dump, encoding="utf-8") as f:
        data = json.load(f)
    report = compare(iter_items(data))
    print_report(report, args.examples)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
