# Inventory Manager

A self-hosted web application for tracking home devices and managing their manuals in [Paperless-NGX](https://docs.paperless-ngx.com/). Built with FastAPI, served as a Docker container.

---

## Features

- **Device inventory** — track devices by name, manufacturer, model, category, protocols, location, and status (active / retired / stored)
- **Tree view** — collapsible category groups with persistent expand/collapse state and per-category "Fetch all" button
- **Real-time search** — filters rows across all categories instantly
- **Duplicate detection** — warns when a new device matches an existing name or model number
- **Multi-stage document search** — 8-stage PDF search pipeline: free stages first (manufacturer direct, archive.org, Google scraping, DuckDuckGo), then configured API providers as deeper-search fallback
- **Blocked aggregator domains** — automatically skips manual-aggregator sites (ManualsLib, Manuals.plus, Scribd, etc.) at every search stage
- **Vendor index scraping** — point a device at a manufacturer's manual index page and the app scrapes it for the correct PDF link
- **1-page placeholder retry** — automatically rejects single-page cover-sheet PDFs and retries remaining search stages with the bad URL excluded
- **Document pipeline** — downloads PDFs (or converts HTML pages via WeasyPrint) and uploads to Paperless-NGX
- **Manual update check** — HEAD-checks stored source URLs for changes and re-uploads when content genuinely changes
- **Provide URL / upload** — manual fallback when auto-search fails; accepts a direct PDF link, a web page URL, or a file upload
- **Paperless sync** — reconcile existing Paperless documents back to inventory with a single click
- **Background task queue** — all fetch and upload work runs in the background with a live task log showing per-stage search status
- **Navigation sidebar** — hamburger menu with Devices, Tasks, Categories, Manufacturers, Settings, and HA import views
- **Category manager** — create, edit, and delete primary categories; rename secondary/tertiary values across all devices with automatic Paperless retag
- **Manufacturer bulk rename** — rename a manufacturer name across all devices at once, with automatic Paperless `mfr:` retag
- **Settings UI** — manage search API keys, track per-provider usage, sync Brave quota from API headers, and link directly to each provider's usage portal
- **Home Assistant import** — reads physical devices from HA, deduplicates by model, imports selected devices
- **Ignore list** — suppress unwanted HA devices permanently by device ID or model
- **Persistent config** — API keys, categories, and usage counters all survive container restarts

---

## Architecture

```
inventory-manager (FastAPI + uvicorn, port 7070)
│
├── app.py            — routes, form handling, HTMX responses, background task dispatch
├── fetch.py          — 8-stage PDF search, download, WeasyPrint HTML→PDF, update checks
├── paperless_api.py  — Paperless-NGX API client (tags, correspondents, document upload/delete)
├── ha_client.py      — Home Assistant device fetch via template API
├── tasks.py          — in-memory background task store (type, status, timestamped log)
├── config.py         — persistent config + API usage tracking (DATA_DIR/config.yaml)
└── templates/        — Jinja2 + HTMX + Bootstrap 5 UI
```

All inventory state lives in `devices.yaml` on a persistent data volume. No database.

---

## Container Configuration

### Image

```
inventory-manager:1.1
```

The image tag is intentionally versioned. Incrementing it in `compose.yaml` forces a rebuild (required after `requirements.txt` changes). Code-only changes (`.py` files or templates) only need a container restart because the source directory is volume-mounted.

### Ports

| Container | Host | Protocol |
|-----------|------|----------|
| 7070      | 7070 | TCP      |

### Volumes

| Container path | Purpose |
|----------------|---------|
| `/app`         | Application source (Python files, templates). Volume-mount your local clone here so code updates only need a restart. |
| `/data`        | Persistent data: `devices.yaml`, `categories.yaml`, `config.yaml`, and `manuals/` PDF staging. |

### Environment Variables

| Variable          | Required | Default | Description |
|-------------------|----------|---------|-------------|
| `DATA_DIR`        | No       | `/data` | Path inside the container where persistent data files live. |
| `PAPERLESS_URL`   | Yes      | —       | Base URL of your Paperless-NGX instance, e.g. `http://192.168.1.10:8000`. |
| `PAPERLESS_TOKEN` | Yes      | —       | API token for the Paperless service account. See [Paperless setup](#paperless-ngx-setup). |
| `HA_URL`          | No       | —       | Base URL of your Home Assistant instance, e.g. `http://192.168.1.20:8123`. Enables the HA import tool. |
| `HA_TOKEN`        | No       | —       | Home Assistant long-lived access token. Required if `HA_URL` is set. |
| `HA_VERIFY_SSL`   | No       | `false` | Set to `true` to enforce SSL verification for HA connections. |

> **Search API keys are not environment variables.** They are stored in `$DATA_DIR/config.yaml` via the in-app Settings UI so they persist across restarts without needing a container rebuild.

---

## Deployment

### File layout

```
/your/app/path/           ← clone or copy repo contents here → mounted to /app
  app.py
  fetch.py
  templates/
  ...

/your/data/path/          ← create this directory → mounted to /data
  devices.yaml            ← created automatically on first run (or seed from your own file)
  config.yaml             ← created automatically
  manuals/                ← downloaded PDFs stored here
```

### compose.yaml

Copy `deploy/compose.yaml` to your stack directory and update the volume paths:

```yaml
services:
  inventory-manager:
    build: .
    image: inventory-manager:1.1
    container_name: inventory-manager
    volumes:
      - /your/app/path:/app
      - /your/data/path:/data
    ports:
      - "7070:7070"
    environment:
      - DATA_DIR=/data
      - PAPERLESS_URL=${PAPERLESS_URL}
      - PAPERLESS_TOKEN=${PAPERLESS_TOKEN}
      # Optional: enable HA import tool
      - HA_URL=${HA_URL:-}
      - HA_TOKEN=${HA_TOKEN:-}
    restart: unless-stopped
```

### .env file

Create a `.env` file alongside `compose.yaml` (use `deploy/.env.example` as a starting point):

```env
PAPERLESS_URL=http://your-paperless-host:8000
PAPERLESS_TOKEN=your-paperless-api-token-here
# HA_URL=http://your-homeassistant-host:8123
# HA_TOKEN=your-ha-long-lived-access-token
```

### First deploy

1. Clone or copy the repo to the machine running Docker, e.g.:

   ```bash
   git clone https://github.com/Ratoka/paperless-home-inventory.git /your/app/path
   ```

2. Create the data directory and your `.env` file:

   ```bash
   mkdir -p /your/data/path
   cp /your/app/path/deploy/.env.example /your/stack/path/.env
   # edit .env with your actual values
   ```

3. Build and start the container:

   ```bash
   docker compose up -d
   ```

   Or use a GUI like [Dockge](https://dockge.kuma.pet/) — place `compose.yaml` and `.env` in a stack directory and click **Deploy**.

4. Open [http://your-host:7070](http://your-host:7070).

5. Set up the Paperless service account — see [Paperless-NGX Setup](#paperless-ngx-setup).

### Code updates (Python / template changes)

Because the source directory is volume-mounted, you only need to pull new changes and restart:

```bash
git -C /your/app/path pull
docker restart inventory-manager
```

### Dependency updates (`requirements.txt` changed)

```bash
git -C /your/app/path pull
# Bump image tag in compose.yaml (e.g. 1.1 → 1.2)
docker compose up -d --build
```

---

## Paperless-NGX Setup

The app uses a dedicated **service account** in Paperless-NGX with limited permissions.

### Guided setup script

```bash
python3 deploy/setup-paperless-account.py
```

Run this from inside the app directory. It prompts for your Paperless URL and an admin token, then creates the service account and prints the API token to paste into your `.env`.

### Required permissions

| Resource        | Permissions                                  |
|-----------------|----------------------------------------------|
| Documents       | view_document, add_document, delete_document |
| Tags            | view_tag, add_tag                            |
| Correspondents  | view_correspondent, add_correspondent        |
| Document types  | view_documenttype, add_documenttype          |
| Task status     | view_paperlesstask                           |

### Tags created automatically

| Tag pattern               | Purpose |
|---------------------------|---------|
| `source:device-inventory` | Safety guard — only documents with this tag can be deleted through this app. |
| `device:<device-id>`      | Per-device tag, e.g. `device:philips-hue-bridge`. |
| `mfr:<slug>`              | Manufacturer slug tag, e.g. `mfr:onkyo`. |
| `cat1:<primary>`          | Primary category slug, e.g. `cat1:smart-home`. |
| `cat2:<secondary>`        | Secondary category slug (if set). |
| `cat3:<tertiary>`         | Tertiary category slug (if set). |

The document correspondent is set to the manufacturer name. The document type is set to the document label (e.g. `Manual`, `Quick Start`, `Datasheet`).

---

## Navigation

The hamburger button (☰) opens a left-side navigation panel:

| Nav item          | Description |
|-------------------|-------------|
| 📦 Devices        | Main device inventory (default view) |
| ✅ Tasks           | Live task log for all background operations |
| 🏷 Categories      | Category taxonomy manager |
| 🏭 Manufacturers   | Bulk rename manufacturers across all devices |
| ⚙ Settings        | Search API keys and usage tracking |
| 📡 Import Devices  | Home Assistant device import (HA only) |
| 🚫 Ignore List     | HA device ignore list (HA only) |

---

## Background Task Queue

All fetch, upload, and Paperless retag operations run as background tasks. The **Tasks** view shows:

- Task type (fetch / upload / retag), label, and elapsed time
- Status (running / success / error) with a spinner while running
- Per-stage search log with ✓ found / no results / skipped status for each stage
- **Clear Completed** button to remove finished tasks

> Tasks are **in-memory only** and are lost on container restart. The YAML inventory file is the source of truth for document status.

### Stuck-status recovery

If the container restarts while a fetch or upload is in progress, any document left in a `searching`, `downloading`, or `uploading` state is automatically reset to `error` with the message *"Interrupted — server restarted during fetch. Click ↺ to retry."*

---

## Document Pipeline

### Automatic fetch

When a device is added (or ↺ Fetch is clicked), the pipeline runs for each configured doc type:

1. **Vendor index** (if `vendor_index_url` is set) — scrapes the manufacturer's manual index page and matches by model number.
2. **Search** — 8-stage PDF search. See [Search pipeline](#search-pipeline).
3. **Download** — saves the PDF directly, or converts HTML pages to PDF via WeasyPrint (CDN resources stripped).
4. **Validate** — rejects single-page PDFs (likely cover sheets or landing pages). If rejected, the URL is excluded and the search resumes from the next stage automatically.
5. **Upload** — uploads to Paperless-NGX, polls task API until OCR completes (up to 90 s).

### Category fetch-all

Each category header shows a **↓ Fetch all (N)** button when one or more devices in that category have unfetched or failed documents. Clicking it queues background fetch tasks for every eligible device in the category at once.

### PDF filenames

Local PDFs are stored as `$DATA_DIR/manuals/<device-id>/<DeviceName>_<doctype>.pdf`. Illegal filesystem characters are stripped; spaces become underscores.

### Manual fallback

When auto-search fails (badge shows `?` or `!`), clicking the badge opens **Provide Document**. Accepts:
- A direct PDF URL
- A web page URL (converted to PDF)
- A file upload (`.pdf`)

### Update check

The **↻** button on a successfully-fetched document:
1. Sends a HEAD request to the stored source URL.
2. Compares ETag / Last-Modified headers.
3. If changed: downloads and compares PDF metadata (mod date, version string).
4. If metadata is identical: treats it as a server-side republish, skips the update.
5. If genuinely new: uploads to Paperless and deletes the old document.

### Paperless sync

The **🔍 Check Paperless** button appears on any device with an unresolved document. It queries Paperless for all documents tagged `device:<id>`, matches them to inventory docs by title suffix, and writes back `fetch_status: success` + `paperless_id`. Use this when a document is visible in Paperless but the inventory still shows an error.

---

## Search Pipeline

Stages are tried in order; the first confirmed PDF URL is returned and subsequent stages are skipped. Free stages run first to preserve API quota.

| Stage | Provider | Cost | Notes |
|-------|----------|------|-------|
| 1 | **Manufacturer direct** | Free | DDG `site:{brand}.com` scoped search. Requires manufacturer + model on the device. |
| 2 | **Archive.org CDX** | Free | Wayback Machine PDF index for manufacturer URLs. Requires manufacturer + model. |
| 3 | **Google HTML scraping** | Free | High-quality index; silently fails when rate-limited. |
| 4 | **DDG filetype:pdf** | Free | Stable HTML endpoint. |
| 5 | **DDG broad** | Free | Drops `filetype:pdf` filter. |
| 6 | **Brave Search API** | ~1,000 req/month free¹ | JSON API, configured in Settings. |
| 7 | **Google Custom Search** | 100 req/day free | Best index quality, configured in Settings. |
| 8 | **Bing Web Search** | 1,000 req/month free | Azure-backed, configured in Settings. |

API stages (6–8) are only active when a key is configured in **⚙ Settings**. Stages 1–5 always run regardless.

> ¹ Brave's free tier is $5/month in account credits at $5/1,000 queries. A credit card is required to create an account.

### Rate-limit enforcement

| Provider | Limit | Window | Tracking |
|----------|-------|--------|----------|
| Brave | ~1,000 req | Monthly | Server-confirmed via `X-RateLimit-*` response headers; falls back to local daily counter |
| Google CSE | 100 req | Daily | Local counter |
| Bing | 1,000 req | Monthly | Local counter |

When a provider reaches its limit:
- **Allow paid usage unchecked** — that provider is skipped for the rest of the period and searches fall through to the next stage.
- **Allow paid usage checked** — the provider continues to be called; charges beyond the free tier apply.

The **⚙ Settings** UI shows for each provider: current usage vs. limit on a colour-coded progress bar, the reset date with a day countdown, a **↻ Sync usage** button to probe the Brave API live, and a link to the provider's usage portal.

### Blocked domains

The following manual-aggregator sites are blocked at every stage — URLs from these domains are discarded before any download is attempted, and `-site:` operators are appended to every search query:

`manuals.plus`, `manualslib.com`, `manualzz.com`, `usermanual.wiki`, `scribd.com`, `calameo.com`, `issuu.com`, `docplayer.net`, and others.

---

## Search API Setup Guides

API keys are entered in **⚙ Settings** inside the app. They are saved to `$DATA_DIR/config.yaml` and never need to be in environment variables.

### Brave Search API

1. Go to [api.search.brave.com](https://api.search.brave.com/) and create an account (credit card required for activation; not charged within monthly credits).
2. Create an API key under **API Keys**.
3. In the app: **⚙ Settings → Brave Search → API Key** → paste key → **Save**.

> Free limit: ~1,000 queries/month. The Settings usage meter is updated from Brave's rate-limit response headers after each search call.

### Google Custom Search API

1. Go to [programmablesearchengine.google.com](https://programmablesearchengine.google.com/), click **Add**, and choose **Search the entire web**.
2. Copy the **Search engine ID (CX)**.
3. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the **Custom Search JSON API**, and create an API key under Credentials.
4. In the app: **⚙ Settings → Google Custom Search** → paste **API Key** and **CX** → **Save**.

> Free limit: 100 queries/day, resets at midnight Pacific.

### Bing Web Search API

1. Create an [Azure account](https://azure.microsoft.com/) if you don't have one.
2. Search for **Bing Search v7**, create a resource on the **F1 (free)** pricing tier.
3. Under **Keys and Endpoint**, copy **Key 1**.
4. In the app: **⚙ Settings → Bing Web Search → API Key** → paste key → **Save**.

> Free limit: 1,000 queries/month.

---

## Vendor Index Scraping

For manufacturers that publish a central manual index page, you can skip the search engine entirely by pointing the device at that page.

Add `vendor_index_url` to the device entry in `devices.yaml`:

```yaml
- id: inovelli-vzm31sn
  name: Inovelli VZM31-SN
  manufacturer: Inovelli
  model: VZM31-SN
  vendor_index_url: https://help.inovelli.com/en/articles/8426779-device-manuals
  docs:
    - type: manual
      fetch_status: pending
```

The scraper fetches the index page, parses links with their surrounding block context, and matches by normalized model number. `vendor_index_url` can also be set at the individual doc level for doc-type-specific index pages.

---

## Category Manager

Open **🏷 Categories** from the nav to manage the category taxonomy.

- **Primary categories** — add (key, label, icon), edit, or delete. Devices keep their existing value if a category is deleted.
- **Secondary / tertiary values** — click a primary category to see all values in use with device counts. Renaming a value updates every matching device in `devices.yaml` and queues a Paperless retag background task.

---

## Home Assistant Integration

When `HA_URL` and `HA_TOKEN` are set, HA-specific nav items appear.

1. Posts a Jinja2 template to the HA `/api/template` endpoint, collecting physical devices (`entry_type is none`).
2. Deduplicates by `(manufacturer, model)` — 12 identical bulbs appear as one import candidate.
3. Filters out devices already in inventory and devices on the ignore list.

Create a long-lived access token in Home Assistant under **Profile → Security → Long-Lived Access Tokens**.

---

## Data Storage

### File layout

| File | Contents |
|------|----------|
| `$DATA_DIR/devices.yaml` | Device inventory and all document status fields |
| `$DATA_DIR/categories.yaml` | Custom category taxonomy (created on first category edit) |
| `$DATA_DIR/config.yaml` | Search API keys and per-day usage counters |
| `$DATA_DIR/manuals/<device-id>/` | Downloaded PDFs, named `<DeviceName>_<doctype>.pdf` |

### devices.yaml structure

```yaml
devices:
  - id: onkyo-tx-nr7100
    name: Onkyo TX-NR7100
    manufacturer: Onkyo
    model: TX-NR7100
    category:
      primary: electronics
      secondary: Audio
    protocols: [ethernet]
    location: Living Room
    status: active
    docs:
      - type: manual
        search_hint: "Onkyo TX-NR7100 user manual PDF"
        fetch_status: success   # pending | searching | downloading | uploading | success | error | not_found
        source_url: https://…
        paperless_id: 42
        pdf_pages: 191
        pdf_mod_date: "2023-04-01"
        pdf_version: "v2.1"
        last_modified: "Sat, 01 Apr 2023 00:00:00 GMT"
        etag: '"abc123"'
        fetched_at: "2026-05-20"

ha_ignored:
  - ha_id: abc123def456
    name: Hue Motion Sensor
    manufacturer: Philips
    model: SML001
    ignored_at: "2026-05-20"
```

### config.yaml structure

```yaml
search_providers:
  brave:
    api_key: "BSAxxxxxxxxx"
    allow_paid: false
    rate_limit:               # updated from X-RateLimit-* headers after each call
      remaining: 847
      reset_date: "2026-06-01"
  google_cse:
    api_key: "AIzaxxxxxxxxx"
    cx: "a1b2c3d4e5f6"
    allow_paid: false
  bing:
    api_key: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    allow_paid: false

api_usage:
  brave:
    "2026-05-27": 14
    "2026-05-26": 8
  google_cse:
    "2026-05-27": 3
```

---

## Local Development

```bash
git clone https://github.com/Ratoka/paperless-home-inventory.git
cd paperless-home-inventory

pip install -r requirements.txt

DATA_DIR=/path/to/data \
PAPERLESS_URL=http://your-paperless-host:8000 \
PAPERLESS_TOKEN=your-token \
uvicorn app:app --reload --port 7070
```

Open [http://localhost:7070](http://localhost:7070).

WeasyPrint system dependencies (Debian/Ubuntu):

```bash
apt-get install libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libglib2.0-0 fonts-liberation
```

---

## Recommended Enhancements

### Search Result Caching

Cache the last successful `(manufacturer, model, doc_type) → PDF URL` mapping for 24 hours. Re-fetching a device after a transient error reuses the cached URL instead of burning API calls.

1. Add `cache_search_result(manufacturer, model, doc_type, url)` to `config.py`, writing to `$DATA_DIR/search_cache.yaml`.
2. In `fetch_device_docs`, check the cache before calling `search_pdf_url`. On download failure, invalidate the cache entry and retry with full search.

### Per-Search Attribution Stats

Record which search stage found each document (`found_by: brave`, `found_by: manufacturer-direct`, etc.) in `devices.yaml`. The task log already shows this per-run; persisting it to the YAML enables a **Provider performance** section in Settings showing which providers are actually finding documents for your device mix.

### Vendor Index URL Library

A shared `$DATA_DIR/vendor_indexes.yaml` mapping manufacturer names to their manual index pages. When a device is fetched and its manufacturer is in the library, `vendor_index_url` is applied automatically without needing it on every device record.
