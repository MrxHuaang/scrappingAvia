"""
FR identity index — authoritative AD number per Federal Register document.

The FR API exposes the regulatory header of every AD as structured
`docket_ids`: ["Docket No. FAA-2023-0927", "Project Identifier ...",
"Amendment 39-22461", "AD 2023-12-03"]. That is the identity of the AD, and it
needs no full-text regex, no GovInfo MODS XML and no API key.

Reading identity from prose ("the first `AD nnnn-nn-nn` in the body") is what
put wrong AD numbers on FR rows: the body cites the AD it supersedes before it
names itself, so the row ended up carrying another AD's number while showing
the right document's PDF.

Usage:
    python scripts/fr_identity.py            # build/refresh outputs/fr_identity.json
    python scripts/fr_identity.py --year 2024
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from utils import ad_identity_from_docket_ids as identity_from_docket_ids  # noqa: E402

INDEX_PATH = os.path.join(_ROOT, "outputs", "fr_identity.json")
API = "https://www.federalregister.gov/api/v1/documents.json"
PER_PAGE = 1000
START_YEAR = 1994

FIELDS = [
    "document_number", "docket_ids", "title", "abstract", "action", "type",
    "publication_date", "effective_on", "pdf_url", "html_url",
    "cfr_references", "agencies", "citation", "correction_of", "corrections",
]

HEADERS = {"User-Agent": "AD-Scraper/1.0 (identity index)", "Accept": "application/json"}

def _get(params, retries=4):
    delay = 2
    for attempt in range(retries):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=90)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"  [fr_identity] {e} (attempt {attempt + 1})")
            time.sleep(delay)
            delay *= 2
    return None


def fetch_year(year: int, doc_types=("RULE", "PRORULE")) -> list[dict]:
    docs, page = [], 1
    while page <= 20:
        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": list(doc_types),
            "conditions[publication_date][gte]": f"{year}-01-01",
            "conditions[publication_date][lte]": f"{year}-12-31",
            "per_page": PER_PAGE,
            "page": page,
            "order": "oldest",
            "fields[]": FIELDS,
        }
        data = _get(params)
        if not data:
            break
        batch = data.get("results") or []
        docs.extend(batch)
        if len(batch) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)
    return docs


def build(years) -> dict:
    index = {}
    for yr in years:
        docs = fetch_year(yr)
        with_ad = 0
        for d in docs:
            num = (d.get("document_number") or "").strip()
            if not num:
                continue
            ident = identity_from_docket_ids(d.get("docket_ids"))
            if ident["ad"]:
                with_ad += 1
            index[num] = {
                **ident,
                "title": d.get("title") or "",
                "abstract": d.get("abstract") or "",
                "action": d.get("action") or "",
                "type": d.get("type") or "",
                "publication_date": d.get("publication_date") or "",
                "effective_on": d.get("effective_on") or "",
                "pdf_url": d.get("pdf_url") or "",
                "html_url": d.get("html_url") or "",
                "citation": d.get("citation") or "",
                "correction_of": (d.get("correction_of") or {}).get("document_number")
                if isinstance(d.get("correction_of"), dict) else d.get("correction_of"),
                "docket_ids": d.get("docket_ids") or [],
            }
        print(f"[fr_identity] {yr}: {len(docs)} docs, {with_ad} with AD number")
        time.sleep(0.2)
    return index


def load_index() -> dict:
    with open(INDEX_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None, help="single year (debug)")
    ap.add_argument("--from-year", type=int, default=START_YEAR)
    ap.add_argument("--to-year", type=int, default=datetime.now().year)
    ap.add_argument("--merge", action="store_true", help="merge into existing index")
    args = ap.parse_args()

    years = [args.year] if args.year else range(args.from_year, args.to_year + 1)
    index = build(years)

    if args.merge and os.path.exists(INDEX_PATH):
        prev = load_index()
        prev.update(index)
        index = prev

    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    total = len(index)
    with_ad = sum(1 for v in index.values() if v.get("ad"))
    rules = sum(1 for v in index.values() if (v.get("type") or "").upper() == "RULE")
    print(f"[fr_identity] {total} docs indexed ({rules} RULE), {with_ad} with AD number")
    print(f"[fr_identity] -> {INDEX_PATH}")


if __name__ == "__main__":
    main()
