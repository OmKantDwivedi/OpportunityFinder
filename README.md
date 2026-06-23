# Threadfinder — Reddit Affiliate Opportunity Finder

A web-based research tool that combines your **Levanta** affiliate catalog with
**Ahrefs** SEO data to surface high-traffic Reddit threads ranking in Google's
top 10 — places where your affiliate products are genuinely relevant to the
discussion.

## What it does (pipeline)

1. **Levanta** — pulls all affiliate products (name, category, brand, price,
   commission) and keeps only those earning **≥ $10 commission per sale**
   (configurable).
2. **Keywords** — derives the generic product type from each product name
   ("PureBreeze HEPA Air Purifier H13" → "air purifier") and expands it with
   modifiers: `air purifier`, `best air purifier`, `air purifier reddit`,
   `air purifier review`, `best air purifier reddit`, `air purifier recommendation`.
3. **Ahrefs SERP overview** — checks Google's top 10 for every keyword and keeps
   only **reddit.com thread URLs**.
4. **Dedupe** — Reddit URLs are normalized and de-duplicated; keyword + URL +
   position are stored.
5. **Ahrefs batch analysis** — fetches monthly organic traffic for every thread.
6. **Matching & scoring** — relevance = token overlap between product
   type/category/brand and thread keyword/title/subreddit; the opportunity
   score (0–100) combines relevance × log-scaled traffic × SERP position ×
   commission.
7. **Dashboard** — sortable table of product / keyword / thread / position /
   traffic / score / status, with full-text search and filters (product,
   status, min traffic, min score). Status workflow: New → Approved →
   Commented / Rejected / On Hold.
8. **CSV export** — exports the currently filtered view.

## Quick start (demo mode — no API keys needed)

```bash
cd reddit-affiliate-finder
pip install -r requirements.txt
cp .env.example .env          # MOCK_MODE=auto: mock while keys are blank
cd backend
uvicorn app:app --port 8000   # run WITHOUT --reload (see Troubleshooting)
```

Open <http://localhost:8000>, click **Run pipeline**. Demo mode generates a
realistic sample catalog and deterministic SERP/traffic data so the whole flow
can be tested without spending Ahrefs credits.

> **Demo-mode Reddit links are simulated and will show "Page not found" on
> reddit.com.** They exist only to demonstrate the workflow. Real, working
> thread URLs come from real Ahrefs SERP data once your keys are configured.
> The dashboard shows a yellow banner whenever demo data is active.

## Going live

1. Edit `.env`:
   ```ini
   LEVANTA_API_KEY=your_key
   AHREFS_API_KEY=your_key
   MOCK_MODE=auto        # auto switches to live once both keys are set
   ```
2. Restart the server and run the pipeline. The dashboard's `MOCK DATA` badge
   and yellow banner disappear when live mode is active. If you force
   `MOCK_MODE=false` with a key missing, the run is blocked with a clear
   error instead of silently falling back to sample data.

## Troubleshooting

- **"It didn't pull all products that qualify"** — a normal run is bounded by
  two caps: the **page-range** setting (how deep into the catalog it scans) and
  `MAX_PRODUCTS` in `.env` (how many qualifying products to keep, highest
  commission first; the activity log says when it truncates). To pull *every*
  qualifying product of a given type regardless of those caps, use the **product
  name search** described above. Each request now also fetches 500 products per
  page (the API max) and skips out-of-stock items.
- **Huge catalog / run takes forever on "Levanta: page N..."** — Levanta's API
  has no server-side commission filter, so the client filters while paginating
  and is bounded by scale guards in `.env`: `LEVANTA_MAX_PAGES` (default 50 =
  5,000 products scanned), `MAX_PRODUCTS` (default 200, best commissions kept),
  and `MAX_SERP_KEYWORDS` (default 300 unique keywords per run — keywords are
  de-duplicated globally so identical product types are searched once). If your
  partnered catalog is very large, the most effective lever is
  `LEVANTA_BRAND_IDS=<id1>,<id2>` to scan only chosen partners server-side
  (brand IDs come from Levanta's List Brands endpoint or the dashboard URL).
  A running pipeline can be aborted any time with the **Stop** button.

- **"It behaves the same with or without API keys"** — check the `MOCK DATA`
  badge next to Run pipeline and the yellow banner. If they're visible, the
  app is in demo mode: either a key is blank or `MOCK_MODE=true` is forcing it.
  Set `MOCK_MODE=auto` (or `false`) and restart the server after editing `.env`
  (env vars are read at startup).
- **Reddit links 404** — expected in demo mode (see above).
- **Run interrupted / progress stopped** — don't use `--reload` while a
  pipeline is running: any file change restarts the server and kills the
  background run. The app now detects this, marks the run *interrupted*, and
  shows a banner telling you to re-run. The frontend also keeps polling
  through brief server restarts instead of freezing ("Reconnecting to
  server…").
- **What is it doing right now?** — open the **Activity log** panel under the
  filters: it streams every step live (each keyword's SERP check, thread
  counts, traffic batch, scoring) and keeps warnings/errors.


### API notes

- **Levanta** — the client targets the **Creator API v2**
  (`https://app.levanta.io/api/creator/v2/products`, Bearer auth, required
  `marketplace` param, cursor-based pagination), per the official docs at
  <https://api-docs.levanta.io>. Commission rates come back as percentages;
  the app converts them to **dollars per sale** (`price × totalCommission%`)
  before applying the $10 filter. If your account tier serves a different
  path, the client auto-tries known alternates (v2 preview, v1 creator,
  seller v1), logs which one worked in the activity log, and tells you what
  to put in `LEVANTA_BASE_URL`. Get your key from the Levanta dashboard under
  **Settings → API** (admin access required). Set `LEVANTA_ACCESS_ONLY=false`
  in `.env` to include products from brands you haven't partnered with yet.
- **Ahrefs** requires an API v3 key (Enterprise plan or API add-on). The client
  uses `GET /v3/serp-overview/serp-overview` and
  `POST /v3/batch-analysis/batch-analysis`. SERP-overview rows cost API units
  per keyword — `REQUEST_DELAY_SECONDS` throttles requests.

## Find products by name

The search box next to **Run pipeline** (placeholder "Find by product name")
lets you target a single product type. Type e.g. `air purifier` and the button
becomes **Search & run**: the pipeline scans the **entire** Levanta catalog
(ignoring the page-range and max-products caps) and keeps every in-stock
product whose title contains all the words you typed AND meets the min
commission. This is the way to answer "pull all air purifiers that earn at
least $10/sale" on a large catalog. Multi-word queries match titles containing
every word (e.g. `cold brew coffee`). Leave the box empty to run a normal
catalog scan using the page-range setting.

A safety cap, `NAME_SEARCH_MAX_MATCHES` in `.env` (default 1000), stops a name
search once that many matches are collected; set it to `0` for unlimited.

## Settings in the UI

Three settings can be changed directly from the dashboard (the **Pipeline
settings** panel) without editing `.env` or restarting the server:

- **Min commission ($ per sale)** — products below this are skipped.
- **Catalog pages to scan** — each page is 100 products. Enter a depth like
  `50` (pages 1–50) or a *range* like `5,10` to collect only pages 5 through
  10. Because Levanta uses cursor pagination, pages before the start are walked
  through to reach the range but their products are discarded.
- **Brand IDs** — comma-separated Levanta partner IDs for a server-side filter;
  empty means all partnered brands.

Values are stored in the app database, override the `.env` defaults, and apply
to the next run. They can't be changed while a pipeline is running.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `MIN_COMMISSION_USD` | `10` | Drop products below this commission |
| `SERP_TOP_N` | `10` | Only consider this many top results |
| `SERP_COUNTRY` | `us` | Ahrefs country code |
| `MAX_KEYWORDS_PER_PRODUCT` | `6` | How many modifier keywords per product |
| `REQUEST_DELAY_SECONDS` | `0.4` | Throttle between Ahrefs SERP calls |
| `MOCK_MODE` | `auto` | `auto` = mock while keys are blank; `true`/`false` to force |
| `DB_PATH` | `data/app.db` | SQLite location |

## Project layout

```
backend/
  app.py                 FastAPI app: REST API + serves frontend
  config.py              Env-driven settings
  database.py            SQLite schema (products, keywords, threads, opportunities)
  clients/levanta.py     Levanta product catalog client (+ mock)
  clients/ahrefs.py      Ahrefs SERP + batch-analysis client (+ mock)
  services/keywords.py   Product → keyword generation
  services/matching.py   Relevance + opportunity scoring
  services/pipeline.py   Background pipeline orchestrator with progress
frontend/
  index.html / styles.css / app.js   Dashboard (no build step)
```

## API endpoints

- `POST /api/pipeline/run` — start a run (409 if already running)
- `GET  /api/pipeline/status` — progress, step, errors
- `GET  /api/opportunities` — `search, status, product_id, min_traffic, min_score, sort, order, limit, offset`
- `PATCH /api/opportunities/{id}/status` — `{"status": "Approved"}`
- `GET  /api/products`, `GET /api/stats`
- `GET  /api/export.csv` — same filters as the list endpoint

## A note on Reddit promotion

The tool finds and scores opportunities; acting on them is manual by design.
Reddit communities and Reddit's own rules penalize undisclosed promotion —
your client should disclose affiliations, follow each subreddit's
self-promotion rules, and contribute genuinely useful answers. That also tends
to convert better and keeps accounts from being banned.
