"""Static contract check for registered scrapers.

This script does not hit regulator websites. It verifies that every source
registered in the ingestion orchestrator maps to a scraper module with a
callable `search_ads` function.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from populate_db import ALL_SOURCES, RUNNERS  # noqa: E402


def main() -> int:
    failures: list[str] = []

    for source in ALL_SOURCES:
        if source not in RUNNERS:
            failures.append(f"{source}: missing RUNNERS entry")

        module_name = f"scrapers.{source}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{source}: cannot import {module_name}: {exc}")
            continue

        search_ads = getattr(module, "search_ads", None)
        if not callable(search_ads):
            failures.append(f"{source}: missing callable search_ads")

    if failures:
        print("Scraper contract check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Scraper contract check passed for {len(ALL_SOURCES)} sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

