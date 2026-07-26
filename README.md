# Zephr - scrappingAvia (Backend)

Python backend for Zephr: scrapers for aviation Airworthiness Directives (ADs) from 9+ national regulators. Populates a Supabase Postgres database consumed by the [ad-scrapper](https://github.com/MrxHuaang/Ad-Scrapper) Next.js frontend.

## What this does

Aggregates ADs from regulators with fragmented or hard-to-scrape sites and writes a unified row per AD into the `ads` table. Each row keyed by `(source, ad_number)`.

Run as scheduled cron (GitHub Actions) or manually for backfill and testing.

## Sources

| Source key            | Authority            | Status        | Verified count |
|-----------------------|----------------------|---------------|----------------|
| `federal_register`    | FAA (USA)            | OK (3 sources) | ~40-50k expected |
| `transport_canada`    | Transport Canada     | OK            | 36,399         |
| `easa`                | EASA (EU)            | OK            | 18,199         |
| `anac_brazil`         | ANAC Brazil          | OK            | 450            |
| `dgac_chile`          | DGAC Chile           | Active        | TBV            |
| `anac_argentina`      | ANAC Argentina       | Active        | TBV            |
| `gcaa_uae`            | GCAA UAE             | Active        | TBV            |
| `uk_caa`              | UK CAA               | Active        | 201            |
| `aerocivil_colombia`  | AEROCIVIL Colombia   | Blocked       | 0              |
| `casa_australia`      | CASA Australia       | Blocked (403) | 0              |

## Quick start

```bash
git clone https://github.com/MrxHuaang/scrappingAvia.git
cd scrappingAvia
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS
pip install -r requirements.txt
cp .env.example .env              # then fill secrets
```

## Environment variables

`.env`:

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_KEY=eyJ...       # service role (writes)
SUPABASE_ANON_KEY=eyJ...          # anon (reads only)
GOVINFO_API_KEY=...               # free key https://api.govinfo.gov/signup/
                                  # required for full FAA coverage via GovInfo
```

Never commit `.env`.

### FAA: optional Playwright for pre-1995 ADs

The `federal_register` source consolidates 3 upstreams: DRS (Playwright,
pre-1995 historical), FR API (year-split, 1994+), and GovInfo (XML, 1995+).
DRS is best-effort — pipeline does not fail if browser missing.

```bash
pip install playwright
playwright install chromium       # ~150MB
```

## Usage

```bash
# Single source, dry-run (no DB writes)
python populate_db.py --source federal_register --dry-run

# Single source, live (upserts to Supabase)
python populate_db.py --source easa

# All sources sequentially
python populate_db.py

# DESTRUCTIVE: wipe a source and repopulate (use after schema changes)
python populate_db.py --source federal_register --reset

# Data quality report (complete / partial / critical per source)
python scripts/validate_ads.py
```

## Schema (table `ads`)

```sql
source          TEXT          -- "federal_register", "easa", ...
source_label    TEXT          -- "FAA", "EASA", ...
ad_number       TEXT
subject         TEXT
make            TEXT
model           TEXT
product_type    TEXT          -- Aircraft | Engine | Propeller | Helicopter | Appliance
effective_date  DATE
status          TEXT          -- Current | Final Rule | Superseded | Cancelled | Emergency AD | NPRM
docket          TEXT
pdf_link        TEXT
raw_data        JSONB         -- original scraper payload
last_updated_at TIMESTAMPTZ
PRIMARY KEY (source, ad_number)
```

Audit table `scrape_runs` records every live execution (source, status, counts, error_msg, timestamps). Consumed by the frontend admin dashboard at `/admin/scrapers`.

## Project structure

```
scrapers/
  federal_register.py        FAA via federalregister.gov API
  transport_canada.py        TC CAWIS HTML scraping
  easa.py                    EASA AD list paginated HTML
  anac_brazil.py             ANAC Brazil HTML
  dgac_chile.py              DGAC Chile
  anac_argentina.py          ANAC Argentina
  gcaa_uae.py                GCAA UAE
  uk_caa.py                  UK CAA ADs + Mandatory Permit Directives
  aerocivil_colombia.py      Blocked; stub kept
  casa_australia.py          Blocked; stub kept
populate_db.py               Orchestrator + upsert + scrape_runs logger
utils.py                     MAX_RESULTS_PER_SOURCE, timeout helpers
scripts/
  validate_ads.py            Data quality report, emits ads_to_rescrape.csv
app.py                       Legacy Flask API (deprecating)
static/, templates/          Legacy Flask assets
outputs/                     Generated CSVs and logs (gitignored)
.github/workflows/           GitHub Actions cron
```

## Adding a new source

1. Create `scrapers/<source_key>.py` with a `search_ads(...)` function returning `list[dict]`.
2. Each dict must include: `Source`, `AD_Number`, `Subject`, `Make`, `Model`, `Effective_Date`, `Status`, `Product_Type`, `Docket`, `PDF_Link`.
3. Register a `run_<source>()` wrapper in `populate_db.py` and add it to `RUNNERS` + `ALL_SOURCES`.
4. Dry-run to validate.
5. Verify row counts via `scripts/validate_ads.py`.

## Gotchas

### Windows console encoding (cp1252)

Scrapers must use ASCII only in `print()`. No `->`, no checkmarks, no arrows. The validate script reconfigures stdout to UTF-8 explicitly.

### Supabase PostgREST 1000-row cap

For full-table reads use `.range(page*1000, (page+1)*1000-1)` in a loop. See `fetch_all_ads()` in `scripts/validate_ads.py`.

### Transport Canada superseded ADs

ADs superseded by FAA/EASA equivalents have only reference links, no PDF. Around 867 such rows are expected critical in the quality report. Not a bug.

### FAA Federal Register pagination

The API stops sending `next_page_url` after roughly page 50 but pagination continues. Detect end-of-results via `len(documents) < per_page` and compare cumulative count to the API `count` total. Implemented in `scrapers/federal_register.py`.

### EASA pagination

EASA paginates via JS. Workaround: request `result/?page=N` directly with a persistent session. Total ADs read from the response text `Displaying X to Y out of total of Z`.

## Resilience patterns

- All `requests.get()` calls have explicit timeouts (15-60s).
- SSL verification always on.
- EASA implements exponential backoff on HTTP 429.
- `MAX_RESULTS_PER_SOURCE` (utils.py) caps any single source at 20,000 rows.
- EASA row parser counts and logs first 10 row-level parse failures rather than swallowing silently.
- Orchestrator (`populate_db.py`) isolates each source with try/except; one failure does not block others.

## Cron

GitHub Actions in `.github/workflows/scrape.yml` runs daily 04:00 UTC. See workflow file for schedule details.

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

Production note: upsert is idempotent, so cleaning is rarely needed. Use only if the source data structure is known to be corrupted in DB.

## Roadmap

See `Documentacion Proyectos/Zephr/ROADMAP.md`. Near-term goals:

- Migrate Flask `app.py` to Next.js + Supabase Edge Functions.
- Add automated daily smoke test (CI) comparing row counts against baseline.
- Investigate proxy bypass for AEROCIVIL Colombia and CASA Australia.
- Add HTML fixture-based unit tests per scraper.

## License

Proprietary. All rights reserved.
