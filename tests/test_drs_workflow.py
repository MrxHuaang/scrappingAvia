"""The DRS job in the daily workflow stays off until someone turns it on.

The sync must not run in production before the repository variable is set
on purpose, it must get its key from the secret store (never a literal), and
its process budget must stay below the job budget for the same reason as the
scrape jobs: a timeout reported as cancelled alerts nobody.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "populate_db.yml"


@pytest.fixture(scope="module")
def job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["drs"]


def _sync_step(job):
    return next(s for s in job["steps"] if "--drs" in (s.get("run") or ""))


def test_the_job_is_gated_by_the_repository_variable(job):
    # always(): runs after the scrapes whatever their outcome (see below).
    assert job["if"].replace(" ", "") == "always()&&vars.DRS_API_ENABLED=='true'"


def test_the_key_and_flag_come_from_secrets_and_vars(job):
    env = _sync_step(job)["env"]
    assert env["DRS_API_KEY"] == "${{ secrets.DRS_API_KEY }}"
    assert env["DRS_API_ENABLED"] == "${{ vars.DRS_API_ENABLED }}"


def test_the_contact_email_is_a_repository_variable_not_a_secret(job):
    env = _sync_step(job)["env"]
    assert env["DRS_CONTACT_EMAIL"] == "${{ vars.DRS_CONTACT_EMAIL }}"


def test_the_process_budget_is_below_the_job_budget(job):
    step = _sync_step(job)
    soft = int(step["env"]["SOFT_MINUTES"])
    assert soft < job["timeout-minutes"]
    assert re.search(r"timeout .*\$\{SOFT_MINUTES\}m.* python populate_db\.py --drs", step["run"])


def test_the_job_waits_for_the_scrapes_but_not_for_their_success(job):
    """link_ads_to_drs rewrites the ads rows federal_register upserts: running
    both at once hit the 8 s statement timeout. So the sync waits for the
    scrape matrix, and always() keeps a failing scraper from skipping it."""
    assert job["needs"] == "scrape"
    assert job["if"].replace(" ", "").startswith("always()&&")
