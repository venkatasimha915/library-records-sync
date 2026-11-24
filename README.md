# Library Records Sync

[![CI](https://github.com/venkatasimha915/library-records-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/venkatasimha915/library-records-sync/actions/workflows/ci.yml)

A small integration service of the kind a library IT team runs: it **harvests catalog and repository
records over OAI-PMH (XML)**, keeps a local **PostgreSQL** copy in sync, and serves the records through a
**JSON REST API** and an **accessible search page**.

Library systems (institutional repositories, digital collections, discovery layers) commonly share metadata
through OAI-PMH. This project is the "glue" in between: pull the XML, clean it up, handle the things that go
wrong (timeouts, bad records, deleted items, paging bugs), and give other applications simple JSON to work with.

![Search page after a sync](docs/screenshot-search.png)

## What it does

- **Harvests OAI-PMH `ListRecords`** in Dublin Core (`oai_dc`), following `resumptionToken` paging to the end.
- **Syncs safely:** inserts new records, updates changed ones, marks deleted ones, and skips unchanged ones.
  Running the same harvest twice writes nothing (idempotent).
- **Handles failures:**
  - retries timeouts, `429` and `5xx` with exponential backoff (1s, 2s, 4s…); does not retry `4xx`
  - saves each page in its own transaction, so a failure on page 3 keeps pages 1–2 and marks the run `partial`
  - stops if a source keeps returning the same `resumptionToken` (an infinite-loop bug some sources have)
  - logs bad records (e.g. no title) to a `sync_errors` table with the reason, instead of failing the whole run
  - only one sync runs at a time (`409 Conflict` for a second request)
- **Parses XML safely** with `defusedxml`, which blocks XML bomb and external-entity attacks.
- **REST API** (FastAPI) with search, filters, paging, run history, and per-run error lists. Interactive docs are at `/docs`.
- **Accessible search page:** labelled form fields, skip link, live status announcements for screen readers,
  visible focus, dark mode. It is checked with **axe-core against WCAG 2.1 AA in CI** (0 violations).
- **Runs anywhere:** a CLI for cron, Docker image, and `docker compose` with PostgreSQL.

## Quick start

### Run locally (SQLite, sample data, no setup)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open <http://localhost:8000> and click **Run sync now**. By default it reads the sample OAI-PMH responses in
`app/sample_data/` (made-up records), so it works offline.

### Run with PostgreSQL in Docker

```bash
docker compose up --build
```

### Point it at a real OAI-PMH source

```bash
export OAI_BASE_URL="https://your-repository.example.edu/oai/request"
export OAI_SET="col_123"                # optional: harvest one collection/set
python -m app.cli sync                   # exit code 0 = success, 1 = partial/failed (good for cron alerts)
```

Any OAI-PMH 2.0 endpoint that supports `oai_dc` should work (DSpace, Fedora/Islandora, EPrints and similar
platforms expose one). The automated tests use recorded sample responses, not a live server.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./records.db` | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host/db` |
| `OAI_BASE_URL` | `mock` | OAI-PMH endpoint; `mock` uses `app/sample_data/` |
| `OAI_METADATA_PREFIX` | `oai_dc` | Metadata format to request |
| `OAI_SET` | – | Optional set (collection) to harvest |
| `OAI_MAX_RETRIES` | `3` | Attempts per page before giving up |
| `OAI_RETRY_BACKOFF_SECONDS` | `1.0` | First wait between retries; doubles each time |
| `OAI_TIMEOUT_SECONDS` | `30` | HTTP timeout per request |

Scheduled hourly with cron:

```cron
0 * * * *  cd /opt/library-records-sync && .venv/bin/python -m app.cli sync >> /var/log/records-sync.log 2>&1
```

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/records?q=&type=&status=&page=&page_size=` | Search title / creator / subject; filter by type; `status` = `active` (default), `deleted`, `all`; max 100 per page |
| `GET` | `/api/records/{id}` | One record (`404` if not found) |
| `POST` | `/api/sync` | Run a harvest now and return the run summary (`409` if one is already running) |
| `GET` | `/api/sync-runs?take=20` | Recent runs with counts: inserted, updated, unchanged, deleted, failed |
| `GET` | `/api/sync-runs/{id}/errors` | Records rejected in a run, with the reason |
| `GET` | `/health` | Liveness check |

Example:

```bash
curl -X POST localhost:8000/api/sync
# {"id":1,"trigger":"api","status":"success","pages":2,"inserted":5,"updated":0,"unchanged":1,"deleted":0,"failed":1,...}

curl "localhost:8000/api/records?q=railroad"
# {"total":1,"page":1,"page_size":20,"items":[{"title":"Oral Histories of Champaign County Railroad Workers",
#   "creators":["Johnson, Patricia"],"subjects":["Oral history","Railroads"],"resource_type":"Sound",...}]}
```

## How the sync works

```
 OAI-PMH source ──XML──▶ HttpOAISource ──▶ parse_page ──▶ normalize ──▶ sync rules ──▶ PostgreSQL
                         (retries,        (defusedxml,    (clean text,   (insert /       records
                          backoff)         tokens,         dedupe,        update /        sync_runs
                                           OAI errors)     validate,      delete /        sync_errors
                                                           hash)          reject)              │
                                                                                               ▼
                                                                FastAPI JSON API + search page
```

| Source says | We do |
|---|---|
| New identifier | Insert |
| Known identifier, same content | Nothing (counted as unchanged) |
| Known identifier, different content | Update; reactivate if it had been deleted |
| `<header status="deleted">` | Mark our copy `deleted` (row kept for history) |
| Missing identifier, metadata or title | Save to `sync_errors` with the reason; continue with the rest |
| `noRecordsMatch` | Treat as an empty harvest, not an error |
| Other OAI error / bad XML / source down | Stop; keep pages already saved; run = `partial` (or `failed` if nothing was saved) |

**Change detection** uses a SHA-256 hash of the normalized fields. The source's datestamp is left out on purpose,
because some repositories bump it without changing the record. Without that, every run would "update" everything.

## Project layout

```
app/
  oai.py          OAI-PMH client (HTTP with retries, or sample files), safe XML parsing
  normalize.py    Dublin Core -> clean fields, validation, change hash
  sync.py         the harvest loop and sync rules, run lock
  models.py       records, sync_runs, sync_errors (SQLAlchemy)
  api.py          FastAPI endpoints
  cli.py          `python -m app.cli sync` for cron
  static/         accessible search page (plain HTML/CSS/JS)
  sample_data/    two pages of sample OAI-PMH XML used by the demo and tests
tests/            37 tests: parsing, retries, normalization, sync rules, API
scripts/          accessibility check (axe-core + Playwright)
```

## Testing and CI

```bash
pytest -q          # 37 tests on in-memory SQLite
ruff check .

# same tests against PostgreSQL
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/records_test pytest -q
```

GitHub Actions runs on every push and pull request:

1. **Lint + tests** on SQLite
2. **Tests on PostgreSQL 16** (service container)
3. **Docker build + smoke test** (start the container, run a sync, query the API)
4. **Accessibility:** axe-core, WCAG 2.1 A/AA, light and dark mode

The tests cover the cases that tend to break integrations: retry/backoff timing, no retries on `4xx`,
`resumptionToken` requests not repeating other arguments (an OAI-PMH spec rule), XML entity attacks,
idempotent re-runs, delete → reactivate, duplicates within one page, failure mid-harvest, token loops,
and concurrent sync requests.

## What I'd add for production

- Incremental harvests using `from=<last successful datestamp>` instead of a full harvest each time
- Authentication on `POST /api/sync` (e.g. campus SSO / Shibboleth in front, or an API key)
- Run the sync in a background worker (or a scheduled job) instead of inside the HTTP request
- A distributed lock (e.g. a PostgreSQL advisory lock) if more than one instance runs
- Database migrations with Alembic instead of `create_all`
- Alerts when a run is `partial`/`failed` or `failed` counts spike (CloudWatch / Azure Monitor)
- Support for other metadata formats (MARCXML, MODS)

## AI assistance

This project was built with help from an AI coding assistant (Claude). All of it is covered by the automated
tests and CI checks described above.
