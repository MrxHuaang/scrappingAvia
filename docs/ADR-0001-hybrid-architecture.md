# ADR 0001: Hybrid Zephr Architecture

Date: 2026-06-12

## Status

Accepted

## Context

Zephr currently spans two repositories:

- `scrappingAvia`: Python scrapers, a `populate_db.py` Supabase ingestion orchestrator, and a legacy Flask API.
- `ad-scrapper`: Next.js 16 SaaS frontend, Supabase Auth/RLS, billing, search UI, fleet, compliance, exports, and admin scraper health.

The main scaling pressure is not generic HTTP API structure. It is source ingestion reliability, AD identity correctness, data freshness, source health, tenant compliance workflows, and auditability.

## Decision

Use a staged hybrid architecture:

1. Keep Python as the ingestion runtime.
2. Modularize the Python backend around source connectors, normalization, persistence, and job logging.
3. Keep Supabase Postgres as the system of record.
4. Keep Next.js as the product app and backend-for-frontend.
5. Treat Flask as legacy. Retire product endpoints from Flask as equivalent Next route handlers or server-only data access layers are created.
6. Defer NestJS until Zephr has a large public API, partner integrations, webhook surface, or a larger backend team that benefits from Nest modules and guards.

## Consequences

Positive:

- Avoids a high-risk rewrite while preserving the scraper knowledge already encoded in Python.
- Lets the team harden the real bottlenecks: ingestion jobs, versioning, source health, alerting, and compliance audit trails.
- Keeps product iteration fast in Next.js and Supabase.
- Leaves a clean path to NestJS later if API complexity justifies it.

Negative:

- Requires clear boundaries so Python, Flask, Next.js, and Supabase do not keep overlapping.
- Requires discipline around service-role usage, RLS, and server-only data access.
- Requires incremental refactoring of `populate_db.py` instead of a single clean rewrite.

## Migration Plan

Stage 1:

- Add scraper contract checks.
- Add runbooks and architecture docs.
- Keep GitHub Actions ingestion, but ensure required secrets include `GOVINFO_API_KEY`.
- Backfill FAA with real AD numbers.

Stage 2:

- Extract source registry, normalization, persistence, and job logging modules from `populate_db.py`.
- Add parser fixtures and non-network tests per scraper.
- Expand `scrape_runs` or add `ingestion_jobs`.

Stage 3:

- Move Flask search/export dependencies into Next route handlers or a dedicated API boundary.
- Add organization-level tenancy and evidence storage.
- Add AD versioning and supersedence relationships.

Stage 4:

- Re-evaluate NestJS for public API and partner integrations only after product/API complexity proves the need.

