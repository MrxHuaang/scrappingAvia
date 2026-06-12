# Zephr Ingestion Runbook

Last updated: 2026-06-12

## Purpose

This repo owns authority ingestion for Zephr. The job of ingestion is to fetch regulator data, normalize it into a stable AD catalog shape, persist it idempotently, and report source health.

## Current Production Flow

1. A scheduled or manual job runs `python populate_db.py`.
2. `populate_db.py` chooses one source or all registered sources.
3. Source-specific functions in `scrapers/` return raw AD dictionaries.
4. `normalize()` maps raw dictionaries to the Supabase `ads` table.
5. `upsert_ads()` deduplicates by `(source, ad_number)` and upserts batches.
6. `save_scrape_run()` writes source status and row counts to `scrape_runs`.
7. The Next.js admin page reads `scrape_runs` for scraper health.

## Source Contract

Every scraper module should expose:

```python
def search_ads(...) -> list[dict]:
    ...
```

Expected raw fields:

- `AD_Number`: required. Stable authority AD identity when available.
- `Subject`
- `Make`
- `Model`
- `Product_Type`
- `Effective_Date`
- `Status`
- `Docket`
- `PDF_Link`

Rules:

- Do not use source document ids as AD numbers unless the authority itself treats them as AD identity.
- Return an empty list instead of raising for expected "no results" cases.
- Raise or log a clear error for parser/source failures that should appear in job health.
- Keep network timeouts explicit.
- Keep stdout ASCII-safe for Windows shells.
- Preserve source-specific extra fields in the raw row. They are stored in `raw_data`.

## Common Commands

```bash
python populate_db.py --source federal_register --dry-run
python populate_db.py --source federal_register --since 2026-01-01
python populate_db.py --source federal_register --reset
python populate_db.py
python scripts/validate_ads.py
python scripts/check_scraper_contracts.py
```

## Required Environment

```text
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
SUPABASE_ANON_KEY=
GOVINFO_API_KEY=
```

`GOVINFO_API_KEY` is required for full FAA identity enrichment. If it is missing, FAA can fall back to Federal Register data, but AD identity quality is lower.

## Scheduling

Current workflow file:

```text
.github/workflows/populate_db.yml
```

Recommended cadence:

- FAA incremental: daily with `--source federal_register --since <recent date>`.
- Full FAA backfill: manual only, with `--reset`, after parser/identity changes.
- EASA and Transport Canada: daily or every 2 days.
- Smaller sources: weekly unless authority update frequency justifies more.
- Blocked sources: do not hammer. Track as blocked with clear status.

## Backfill Rules

- Prefer idempotent upsert over destructive reset.
- Use `--reset` only when existing source rows are known to be incorrect.
- Record why a reset was run in the deployment notes or issue tracker.
- After a full backfill, run `python scripts/validate_ads.py`.

## Operational Health

Minimum source health fields:

- source
- status
- rows found
- rows inserted
- rows updated
- started_at
- finished_at
- error message

Next fields to add:

- trigger: schedule, manual, backfill
- attempt number
- duration_ms
- parser warning count
- source HTTP status summary
- normalized content hash count
- baseline row count delta

Alert conditions:

- no successful run inside expected cadence
- repeated parser failure
- row count drops by more than an expected threshold
- source returns authentication, 403, captcha, or layout-change symptoms
- `GOVINFO_API_KEY` missing on FAA jobs

## Future Job Architecture

Near term:

- Keep GitHub Actions for scheduled runs.
- Keep source jobs sequential until source-specific runtime and rate behavior is better measured.

Medium term:

- Add `ingestion_jobs` in Postgres.
- A scheduler creates jobs.
- Python workers lease and process jobs.
- Workers write structured logs and final status.

Long term:

- Use a managed queue or Postgres-backed queue.
- Use Supabase Cron only as a lightweight trigger for job creation or Edge Function invocation, not as the long-running scraping runtime.

