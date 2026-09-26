# Zephr - scrappingAvia (Backend)

Python backend for Zephr: scrapers for aviation Airworthiness Directives (ADs) from 10 national regulators. Populates a Supabase Postgres database consumed by the [ad-scrapper](https://github.com/MrxHuaang/Ad-Scrapper) Next.js frontend.

## What this does

Aggregates ADs from regulators with fragmented or hard-to-scrape sites and writes a unified row per AD into the `ads` table. Each row is unique by `(source, ad_number)`.

Run as a scheduled cron (GitHub Actions) or manually for backfill and testing.

## Sources

Verified row counts as of 2026-08-03 (production):

| Source key            | Authority            | Status                      | Rows   |
|-----------------------|----------------------|-----------------------------|--------|
| `easa`                | EASA (EU)            | OK                          | 18,308 |
| `federal_register`    | FAA (USA)            | OK — identity via `docket_ids` | 13,069 |
| `transport_canada`    | Transport Canada     | OK                          | 4,749  |
| `anac_brazil`         | ANAC Brazil          | OK                          | 450    |
| `uk_caa`              | UK CAA               | OK                          | 201    |
| `dgac_chile`          | DGAC Chile           | OK                          | 44     |
| `anac_argentina`      | ANAC Argentina       | OK                          | 16     |
| `gcaa_uae`            | GCAA UAE             | **Disabled from cron** — listing behind an ASP.NET AJAX postback; needs Playwright | 0 |
| `aerocivil_colombia`  | AEROCIVIL Colombia   | **Disabled from cron** — SharePoint WAF 403s every plain request | 0 |
| `casa_australia`      | CASA Australia       | **Disabled from cron** — network-level block (timeouts) | 0 |

The three disabled sources are excluded from the daily run (`DISABLED_SOURCES`
in `populate_db.py`) so they stop reporting "completed, 0 ADs" as if it were
real coverage. They remain runnable by hand (`--source gcaa_uae`) for debugging.

## Quick start

```bash
git clone https://github.com/MrxHuaang/scrappingAvia.git
cd scrappingAvia
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS
pip install -r requirements.lock
pip install -e . --no-deps
cp .env.example .env              # then fill secrets
```

Dependencies are pinned in `requirements.lock` (generated with pip-compile from
`pyproject.toml`) so the cron installs the same versions every day. To change a
dependency, edit `pyproject.toml` and regenerate the lockfile.

## Environment variables

See `.env.example` for the full annotated list. The scraping path needs:

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=eyJ...       # service role (writes)
SUPABASE_ANON_KEY=eyJ...          # anon (reads only)
GOVINFO_API_KEY=...               # free key https://api.govinfo.gov/signup/
                                  # without it FAA deep fields silently self-skip
```

Never commit `.env`.

### FAA upstreams

The `federal_register` source consolidates 2 upstreams: FR API (year-split,
1994+) and GovInfo (MODS XML, optional `--with-govinfo`). Both take holder and
models from `scrapers/common/fr_applicability.py`. ADs before 1994 are not in
either; the DRS scraper that tried drs.faa.gov was removed (Akamai blocked it,
it always returned 0).

## Usage

```bash
# Single source, dry-run (no DB writes)
python populate_db.py --source federal_register --dry-run

# Single source, live (upserts to Supabase)
python populate_db.py --source easa

# All daily sources sequentially (skips DISABLED_SOURCES)
python populate_db.py

# DESTRUCTIVE: wipe a source and repopulate (use after schema changes)
python populate_db.py --source federal_register --reset

# Data quality report (complete / partial / critical per source)
python scripts/validate_ads.py
```

## Schema (table `ads`)

```sql
id              UUID PRIMARY KEY  -- surrogate key; FKs elsewhere point here
source          TEXT              -- "federal_register", "easa", ...
source_label    TEXT              -- "FAA", "EASA", ...
ad_number       TEXT
subject         TEXT
make            TEXT
model           TEXT              -- primary model; multiples live in ad_applicability
product_type    TEXT              -- Aircraft | Engine | Propeller | Helicopter | Appliance
published_date  DATE
effective_date  DATE
status          TEXT              -- canonical enum: New | Revision | Superseded | Cancelled
priority        TEXT              -- Emergency | Normal
superseded_to_ad_id UUID          -- FK to ads.id (requires status = 'Superseded')
docket          TEXT
pdf_link        TEXT
raw_data        JSONB             -- original scraper payload
first_seen_at   TIMESTAMPTZ
last_updated_at TIMESTAMPTZ
UNIQUE (source, ad_number)        -- upsert key (on_conflict=source,ad_number)
```

Related tables populated in the post-pass (`populate_relations`):
`ad_applicability` (make/model affected, replaced transactionally per batch via
the `replace_ad_applicability` RPC with a per-AD tri-state contract — see
`docs/applicability-contract.md`) and `ad_service_bulletins`.

Audit table `scrape_runs` records every live execution (source, status,
counts, error_msg, timestamps). A run with failed writes is recorded as
`status='error'` AND exits non-zero, so the Actions job goes red.

## Project structure

```
scrapers/
  common/                    Shared HTTP (headers + backoff), atomic JSON cache, logging
  federal_register.py        FAA via federalregister.gov API (identity from docket_ids)
  faa_govinfo.py             GovInfo MODS XML (docket/amendment backfill, optional)
  transport_canada.py        TC CAWIS HTML scraping (+ PDF deep fields)
  easa.py                    EASA AD list paginated HTML (+ detail page + PDF)
  anac_brazil.py             ANAC Brazil HTML
  dgac_chile.py              DGAC Chile
  anac_argentina.py          ANAC Argentina
  uk_caa.py                  UK CAA ADs + Mandatory Permit Directives
  gcaa_uae.py                Disabled from cron; stub kept
  aerocivil_colombia.py      Disabled from cron; stub kept
  casa_australia.py          Disabled from cron; stub kept
populate_db.py               Orchestrator + upsert + relations + scrape_runs logger
enrich.py                    All derivations (status/priority/product_type/SBs/...)
enrich_ai.py                 LLM enrichment of deep fields (see docs)
utils.py                     AD identity, normalization, timeouts, MAX_RESULTS_PER_SOURCE
webapp/                      Flask app: core + blueprints (pages/auth/billing/search/export/status)
app.py                       Web entry point (Vercel builds this; assembles webapp/)
dashboard_server.py          Local monitor for enrich_ai (loopback-only + token guard)
watch_progress.py            CLI/HTML progress monitor (planner-estimate counts)
tests/                       HTTP contract suite (pytest) for the web app
scripts/                     11 maintenance utilities:
  validate_ads.py            Data quality report, emits ads_to_rescrape.csv
  build_equivalents.py       Cross-authority equivalents (staged swap via RPCs)
  fr_identity.py             FR API identity index -> outputs/fr_identity.json
  fix_faa_identity.py        FAA identity audit/repair (--audit)
  prune_faa_stale.py         Plan/execute deletion of stale FAA rows
  cleanup_faa_stale.py       One-shot 2026-07 migration to bare AD numbers (spent)
  verify_faa_pdfs.py         Sample-check each row's PDF declares its own AD
  warm_faa_mods.py           Pre-warm the GovInfo MODS cache
  link_supersession.py       Resolve supersession chains
  test_*.py                  Self-checks (no network, no DB) — run in CI
static/, templates/          Flask web assets
outputs/                     Caches and generated CSVs (gitignored; cached in Actions)
.github/workflows/           ci.yml (tests+lint+audit) and populate_db.yml (daily cron)
```

## Adding a new source

1. Create `scrapers/<source_key>.py` with a `search_ads(...)` function returning `list[dict]`.
   Use `scrapers.common.http` (headers + `get_with_backoff`) and
   `scrapers.common.cache` for any on-disk cache.
2. Each dict must include: `Source`, `AD_Number`, `Subject`, `Make`, `Model`, `Effective_Date`, `Status`, `Product_Type`, `Docket`, `PDF_Link`.
3. Register a `run_<source>()` wrapper in `populate_db.py` and add it to `RUNNERS` + `SOURCE_LABELS`.
4. Declare the applicability evidence contract for the source in `docs/applicability-contract.md`.
5. Dry-run to validate, then verify row counts via `scripts/validate_ads.py`.

## Gotchas

### Windows console encoding (cp1252)

Scrapers must use ASCII only in log messages. No checkmarks, no arrows. The
validate script reconfigures stdout to UTF-8 explicitly.

### Supabase PostgREST 1000-row cap

For full-table reads use `.range(page*1000, (page+1)*1000-1)` in a loop. See `fetch_all_ads()` in `scripts/validate_ads.py`.

### Transport Canada superseded ADs

ADs superseded by FAA/EASA equivalents have only reference links, no PDF. Around 867 such rows are expected critical in the quality report. Not a bug.

### FAA identity

The AD number comes from the FR API `docket_ids` header — NEVER from the first
"AD nnnn-nn-nn" in the rule body (that names the AD being *superseded*; mining
it mislabeled 6,075 rows in 2026-07). See CLAUDE.md for the full contract.

### FAA Federal Register pagination

The API stops sending `next_page_url` after roughly page 50 but pagination continues. Detect end-of-results via `len(documents) < per_page` and compare cumulative count to the API `count` total. Implemented in `scrapers/federal_register.py`.

### EASA pagination

EASA paginates via JS. Workaround: request `result/?page=N` directly with a persistent session. Total ADs read from the response text `Displaying X to Y out of total of Z`.

## Resilience patterns

- Shared `get_with_backoff` (scrapers/common/http.py): exponential backoff on 429/5xx; 4xx never retried.
- All caches write atomically (tmp + os.replace) — a crash can't truncate a cache and silently force a full re-fetch.
- SSL verification always on; explicit timeouts everywhere (15-60s).
- `MAX_RESULTS_PER_SOURCE` (utils.py) caps any single source at 20,000 rows.
- Orchestrator (`populate_db.py`) isolates each source with try/except; one failure does not block others — but failed WRITES force `status='error'` and a non-zero exit.

## Cron

GitHub Actions in `.github/workflows/populate_db.yml` runs daily at **06:00
UTC** (180-minute timeout, `outputs/` cache restored/saved per run, locked
dependencies). Manual runs via *workflow_dispatch* with a source choice input.
`ci.yml` runs the self-checks, the HTTP contract suite, ruff, pip-audit and a
secret scan on every push/PR.

If the cron fails, see `docs/runbook-cron.md`.

## Cleaning a source for re-scrape

```python
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()
client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = client.table('ads').delete().eq('source', 'transport_canada').execute()
print(f'Deleted: {len(result.data)}')
```

Then re-run `python populate_db.py --source transport_canada`.

Production note: upsert is idempotent, so cleaning is rarely needed. Use only if the source data structure is known to be corrupted in DB. The **catalog policy is additive**: never delete existing ADs as part of a routine run.

## License

Proprietary. All rights reserved.
