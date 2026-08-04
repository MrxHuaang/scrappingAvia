"""The daily-scrape workflow and populate_db must agree on the source list.

A GitHub matrix cannot import Python, so `.github/workflows/populate_db.yml`
carries its own copy of the daily sources. A duplicated list is a list that
drifts: add a scraper to DAILY_SOURCES and forget the workflow, and the source
is simply never scraped — silently, because a source that never runs writes no
`scrape_runs` row and therefore produces no error to notice. That is the shape
of the outage this split was built to end (see the header of the workflow).

So the duplication is allowed and the drift is guarded here.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

import populate_db

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "populate_db.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog(workflow) -> list[dict]:
    """The JSON catalog embedded in the `plan` job's shell script.

    Extracted between the single quotes of `catalog='...'` and parsed as JSON —
    a real parse, not a pattern match over the values.
    """
    steps = workflow["jobs"]["plan"]["steps"]
    script = "\n".join(s.get("run", "") for s in steps)
    match = re.search(r"catalog='(\[.*?\])'", script, re.DOTALL)
    assert match, "the plan job no longer defines a catalog='[...]' block"
    return json.loads(match.group(1))


def test_the_catalog_covers_every_known_source(catalog):
    assert sorted(e["source"] for e in catalog) == sorted(populate_db.ALL_SOURCES)


def test_the_daily_entries_are_exactly_daily_sources(catalog):
    daily = sorted(e["source"] for e in catalog if e["daily"])
    assert daily == sorted(populate_db.DAILY_SOURCES)


def test_the_non_daily_entries_are_exactly_the_disabled_sources(catalog):
    not_daily = sorted(e["source"] for e in catalog if not e["daily"])
    assert not_daily == sorted(populate_db.DISABLED_SOURCES)


def test_the_process_budget_is_always_below_the_job_budget(catalog):
    """The one invariant that turns an overrun into a visible failure.

    A job killed by `timeout-minutes` is reported as *cancelled*, and a
    cancelled run fires no failure notification — 40 consecutive nights went
    unnoticed that way. Bounding the process below the job budget makes an
    overrun exit 124, which GitHub reports as *failure*. If these two numbers
    ever cross, the outage goes quiet again.
    """
    for entry in catalog:
        assert entry["soft_minutes"] < entry["job_minutes"], (
            f"{entry['source']}: the process budget ({entry['soft_minutes']}m) must stay "
            f"below the job budget ({entry['job_minutes']}m), or a timeout is reported "
            f"as cancelled and nothing alerts"
        )


def test_the_job_budget_fits_a_hosted_runner(catalog):
    # GitHub-hosted jobs are killed at 360 minutes regardless of the value here.
    for entry in catalog:
        assert entry["job_minutes"] <= 360, f"{entry['source']} exceeds the hosted-runner ceiling"


def test_the_dispatch_allowlist_offers_every_source(workflow):
    # `on` is parsed as the boolean True by YAML 1.1, which is why this reads it
    # by value rather than by the key "on".
    trigger = workflow[True] if True in workflow else workflow["on"]
    options = trigger["workflow_dispatch"]["inputs"]["source"]["options"]
    assert options[0] == "all", "the default 'all' must stay first in the choice list"
    assert sorted(options[1:]) == sorted(populate_db.ALL_SOURCES)


def test_a_failing_source_cannot_cancel_the_others(workflow):
    strategy = workflow["jobs"]["scrape"]["strategy"]
    assert strategy["fail-fast"] is False, (
        "fail-fast must stay off: one authority failing used to take the rest of the "
        "run with it, which is the starvation this matrix exists to prevent"
    )
