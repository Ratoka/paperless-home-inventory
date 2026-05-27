# Inventory Manager

A self-hosted web application for tracking home devices and managing their manuals in [Paperless-NGX](https://docs.paperless-ngx.com/). Built with FastAPI, served as a Docker container.

---

## Features

- **Device inventory** — track devices by name, manufacturer, model, category, protocols, location, and status (active / retired / stored)
- **Tree view** — collapsible category groups with persistent expand/collapse state
- **Real-time search** — filters rows across all categories instantly
- **Duplicate detection** — warns when a new device matches an existing name or model number
- **Multi-stage document search** — 8-stage PDF search pipeline (API providers → manufacturer direct → archive.org → scraping fallbacks)
- **Blocked aggregator domains** — automatically skips manual-aggregator sites (ManualsLib, Manuals.plus, Scribd, etc.) at every search stage
- **Vendor index scraping** — point a device at a manufacturer's manual index page and the app scrapes it for the correct PDF link
- **Document pipeline** — downloads PDFs (or converts HTML pages via WeasyPrint) and uploads to Paperless-NGX
- **Manual update check** — HEAD-checks stored source URLs for changes and re-uploads when content genuinely changes
- **Provide URL / upload** — manual fallback when auto-search fails; accepts a direct PDF link, a web page URL, or a file upload
- **Paperless sync** — reconcile existing Paperless documents back to inventory with a single click
- **Background task queue** — all fetch and upload work runs in the background with a live task log view
- **Navigation sidebar** — hamburger menu with Devices, Tasks, Categories, Settings, and HA import views
- **Category manager** — create, edit, and delete primary categories; rename secondary/tertiary values across all devices with automatic Paperless retag
- **Settings UI** — manage search API keys and track per-provider usage from within the app
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

All inventory state lives in `devices.yaml` on a persistent volume. Categories and search API config each have their own YAML files in the same volume. No database.

---

## Container Configuration

### Image

```
inventory-manager:1.1
```

The image tag is intentionally versioned. Incrementing it in `compose.yaml` forces a rebuild (e.g. after a `requirements.txt` change). Code-only changes (`.py` or templates) only need a restart because source is volume-mounted.

### Ports

| Container | Host | Protocol |
|-----------|------|----------|
| 7070      | 7070 | TCP      |

### Volumes

| Container path | Purpose |
|----------------|---------|
| `/app`         | Application source (Python files, templates). Volume-mounted so code updates only need a restart. |
| `/data`        | Persistent data: `devices.yaml`, `categories.yaml`, `config.yaml`, and `manuals/` PDF staging. |

### Environment Variables

| Variable          | Required | Default | Description |
|-------------------|----------|---------|-------------|
| `DATA_DIR`        | No       | `/data` | Path inside the container where persistent data files live. |
| `PAPERLESS_URL`   | Yes      | —       | Base URL of your Paperless-NGX instance, e.g. `http://10.250.0.240:30070`. |
| `PAPERLESS_TOKEN` | Yes      | —       | API token for the Paperless service account. See [Paperless setup](#paperless-ngx-setup). |
| `HA_URL`          | No       | —       | Base URL of your Home Assistant instance, e.g. `https://10.0.0.5:8123`. Enables the HA import tool. |
| `HA_TOKEN`        | No       | —       | Home Assistant long-lived access token. Required if `HA_URL` is set. |
| `HA_VERIFY_SSL`   | No       | `false` | Set to `true` to enforce SSL verification for HA connections. |

> **Search API keys are not environment variables.** They are stored in `$DATA_DIR/config.yaml` via the in-app Settings UI so they persist across restarts without needing a container rebuild.

### compose.yaml

```yaml
services:
  inventory-manager:
    build: .
    image: inventory-manager:1.1
    container_name: inventory-manager
    volumes:
      - /mnt/zfs-acd-01/apps/inventory-manager:/app
      - /mnt/zfs-acd-01/media/docs/inventory:/data
    ports:
      - "7070:7070"
    environment:
      - DATA_DIR=/data
      - PAPERLESS_URL=http://10.250.0.240:30070
      - PAPERLESS_TOKEN=${PAPERLESS_TOKEN}
      - HA_URL=${HA_URL:-}
      - HA_TOKEN=${HA_TOKEN:-}
    restart: unless-stopped
```

### `.env` (Dockge stack environment)

```env
PAPERLESS_TOKEN=your-paperless-api-token-here
HA_URL=https://10.0.0.5:8123
HA_TOKEN=your-ha-long-lived-token-here
```

---

## Deployment

### First Deploy

1. **Run sync.sh** from your local machine to copy source to TrueNAS and seed `devices.yaml`:

   ```bash
   ./paperless/scripts/inventory_manager/deploy/sync.sh
   ```

2. **Create the Dockge stack** (run once as root on TrueNAS):

   ```bash
   STACK=/mnt/.ix-apps/app_mounts/dockge/stacks/inventory-manager
   mkdir -p $STACK
   cp /mnt/zfs-acd-01/apps/inventory-manager/stack/* $STACK/
   ```

3. In **Dockge**, open the `inventory-manager` stack, set `PAPERLESS_TOKEN` in the env, and click **Deploy**.

### Code Update (Python / template changes only)

```bash
./deploy/sync.sh   # rsync source to TrueNAS
# Dockge → inventory-manager → Restart
```

### Dependency Update (`requirements.txt` changed)

```bash
./deploy/sync.sh
# Bump image tag in compose.yaml (e.g. 1.1 → 1.2)
# Dockge → inventory-manager → Rebuild → Restart
```

---

## Paperless-NGX Setup

The app uses a dedicated **service account** in Paperless-NGX with limited permissions.

### Guided setup script

```bash
python3 paperless/scripts/inventory_manager/deploy/setup-paperless-account.py
```

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
| `mfr:<slug>`              | Manufacturer slug tag, e.g. `mfr:onkyo`. Makes manufacturer searchable even if correspondent creation fails. |
| `cat1:<primary>`          | Primary category slug, e.g. `cat1:smart-home`. |
| `cat2:<secondary>`        | Secondary category slug (if set). |
| `cat3:<tertiary>`         | Tertiary category slug (if set). |

The document correspondent is set to the manufacturer name. The document type is set to the document label (e.g. `Manual`, `Quick Start`, `Datasheet`).

---

## Navigation

The hamburger button (☰) opens a left-side navigation panel with the following views, each loaded into the main content area without a full page reload:

| Nav item     | Path                | Description |
|--------------|---------------------|-------------|
| 📦 Devices   | `/views/inventory`  | Main device inventory (default view) |
| ✅ Tasks      | `/views/tasks`      | Live task log for all background operations |
| 🏷 Categories | `/views/categories` | Category taxonomy manager |
| ⚙ Settings   | `/views/settings`   | Search API keys and usage tracking |
| 📡 Import Devices | drawer         | Home Assistant device import (HA only) |
| 🚫 Ignore List    | drawer         | HA device ignore list (HA only) |

---

## Background Task Queue

All fetch, upload, and Paperless retag operations run as background tasks. The **Tasks** view shows:

- Task type (fetch / upload / retag), label, and elapsed time
- Status (running / success / error) with a spinner while running
- Last 8 log lines per task with timestamps
- **Clear Completed** button to remove finished tasks

> Tasks are **in-memory only** and are lost on container restart. The YAML inventory file is the source of truth for document status.

### Stuck-status recovery

If the container restarts while a fetch or upload is in progress, any document left in a `searching`, `downloading`, or `uploading` state is automatically reset to `error` with the message *"Interrupted — server restarted during fetch. Click ↺ to retry."* This happens at startup and also on the first poll of the doc-status endpoint for any affected device.

---

## Document Pipeline

### Automatic fetch

When a device is added (or ↺ Fetch is clicked), the pipeline runs for each configured doc type:

1. **Vendor index** (if `vendor_index_url` is set on the device) — scrapes the manufacturer's manual index page and matches by model number. See [Vendor index scraping](#vendor-index-scraping).
2. **Search** — 8-stage PDF search. See [Search pipeline](#search-pipeline).
3. **Download** — saves the PDF directly, or converts HTML pages to PDF via WeasyPrint (CDN resources stripped).
4. **Validate** — rejects single-page PDFs not converted from HTML (likely placeholders).
5. **Upload** — uploads to Paperless-NGX, polls task API until OCR completes (up to 90 s).

### PDF filenames

Local PDFs are stored as `$DATA_DIR/manuals/<device-id>/<DeviceName>_<doctype>.pdf`. Illegal filesystem characters are stripped; spaces become underscores.

### Manual fallback

When auto-search fails (badge shows `?` or `!`), clicking the badge opens **Provide Document**. Accepts:
- A direct PDF URL
- A web page URL (converted to PDF)
- A file upload (`.pdf`)

The upload is queued immediately — the modal closes and a toast notification points to the Tasks view.

### Update check

The **↻** button on a successfully-fetched document:
1. Sends a HEAD request to the stored source URL.
2. Compares ETag / Last-Modified headers.
3. If changed: downloads and compares PDF metadata (mod date, version string).
4. If metadata is identical: treats it as a server-side republish, skips the update.
5. If genuinely new: uploads to Paperless and deletes the old document.

### Paperless sync

The **🔍 Check Paperless** button appears on any device with an unresolved document. It queries Paperless for all documents tagged `device:<id>`, matches them to inventory docs by title suffix (e.g. `"Onkyo TX-NR7100 — Manual"` → type `manual`), and writes back `fetch_status: success` + `paperless_id`. Use this when a document is visible in Paperless but the inventory still shows an error.

---

## Search Pipeline

Stages are tried in order; the first confirmed PDF URL is returned and subsequent stages are skipped. Free stages run first to preserve API quota; the configured API providers are used only when the free stages find nothing.

| Stage | Provider | Cost | Notes |
|-------|----------|------|-------|
| 1 | **Manufacturer direct** | Free | DDG `site:{brand}.com` scoped search |
| 2 | **Archive.org CDX** | Free | Wayback Machine PDF index for manufacturer URLs |
| 3 | **Google HTML scraping** | Free | Fragile (captcha/rate-limit silent failures) |
| 4 | **DDG filetype:pdf** | Free | Stable but weaker PDF index |
| 5 | **DDG broad** | Free | Drops `filetype:pdf` filter |
| 6 | **Brave Search API** | ~1,000 req/month free¹ | JSON API, configured in Settings |
| 7 | **Google Custom Search** | 100 req/day free | Best index quality, configured in Settings |
| 8 | **Bing Web Search** | 1,000 req/month free | Azure-backed, configured in Settings |

API stages are only active when a key is configured in **⚙ Settings**. Stages 1–5 always run regardless.

> ¹ Brave's free tier is $5/month in account credits at $5/1,000 queries. A credit card is required to create an account.

### Rate-limit enforcement

Each API provider tracks its free-tier quota and enforces a hard stop when the limit is reached:

| Provider | Limit | Window | Source |
|----------|-------|--------|--------|
| Brave | ~1,000 req | Monthly | Server-confirmed via `X-RateLimit-*` response headers; falls back to local daily counter sum |
| Google CSE | 100 req | Daily | Local counter |
| Bing | 1,000 req | Monthly | Local counter |

When a provider reaches its limit:
- **Allow paid usage unchecked** — that provider is skipped for the rest of the period. Searches fall through to the next stage automatically.
- **Allow paid usage checked** — the provider continues to be called; charges beyond the free tier apply.

The **⚙ Settings** UI shows for each provider: current usage vs. limit on a colour-coded progress bar, whether the number is server-confirmed or a local estimate, the reset date with a day countdown, and a status badge (Active / ⏸ Paused / 💳 Paid mode). A red card border and alert banner appear when a provider is paused due to a reached limit.

**Brave quota tracking:** After every Brave API call the app parses the `X-RateLimit-Policy`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` response headers to get the server-confirmed remaining quota and reset date for the monthly billing window. This data is persisted to `config.yaml` and shown in Settings as "✓ From Brave API headers". If no API call has been made yet in the current period the app falls back to summing local daily counters.

> API stages (6–8) are skipped entirely when their free limit is reached and **Allow paid usage** is unchecked. The search falls through to the next configured provider, then stops. Free stages (1–5) are unaffected by this.

### Blocked domains

The following manual-aggregator sites are blocked at every stage — URLs from these domains are discarded before any download is attempted:

`manuals.plus`, `manualslib.com`, `manualzz.com`, `usermanual.wiki`, `scribd.com`, `calameo.com`, `issuu.com`, `docplayer.net`, and others. `-site:` operators are also appended to every search query to prevent aggregators appearing in results.

---

## Search API Setup Guides

API keys are entered in **⚙ Settings** inside the app. They are saved to `$DATA_DIR/config.yaml` and never need to be in environment variables or the container config.

### Brave Search API

Brave uses a **freemium credit model**: $5 in free credits are applied to your account each month, which covers approximately 1,000 queries at the standard rate of $5/1,000 requests. **A credit card is required** to create an account (used for identity verification only — you will not be charged within the monthly credit allowance).

1. Go to [api.search.brave.com](https://api.search.brave.com/) and create an account.
2. Add a payment method (required for account activation; not charged within free credit).
3. Create an API key under **API Keys**.
4. In the app: **⚙ Settings → Brave Search → API Key** → paste key → **Save**.

> **Free limit:** ~1,000 queries/month ($5 credit ÷ $5 per 1,000). The Settings usage meter reflects this, updated from Brave's own rate-limit response headers after each search call.

To allow spending beyond the free credit: check **Allow usage beyond free tier** in Settings. Leave it unchecked to hard-stop at the monthly limit and fall through to free search stages instead.

### Google Custom Search API

Highest result quality, but limited to 100 free queries per day. Good as the primary provider if you rarely re-search.

1. Go to [programmablesearchengine.google.com](https://programmablesearchengine.google.com/) and click **Add**.
2. Under **What to search**, choose **Search the entire web**.
3. Copy the **Search engine ID** (CX) — it looks like `a1b2c3d4e5f6g7h8i`.
4. Go to [console.cloud.google.com](https://console.cloud.google.com/), create a project, enable the **Custom Search JSON API**, and create an **API key** under Credentials.
5. In the app: **⚙ Settings → Google Custom Search** → paste both the **API Key** and **Search Engine ID (CX)** → **Save**.

> The daily 100-query limit resets at midnight Pacific. The Settings UI shows today's usage on a progress bar. Check **Allow usage beyond free tier** to continue past 100 queries/day (paid); leave it unchecked to fall through to Bing or free stages instead.

### Bing Web Search API

Available through Azure Cognitive Services with a free tier.

1. Create an [Azure account](https://azure.microsoft.com/) if you don't have one (free tier available).
2. In the Azure portal, search for **Bing Search v7**, create a resource, and select the **F1** (free) pricing tier.
3. Under **Keys and Endpoint**, copy **Key 1**.
4. In the app: **⚙ Settings → Bing Web Search → API Key** → paste key → **Save**.

---

## Vendor Index Scraping

For manufacturers that publish a central manual index page (e.g. Inovelli's help site), you can skip the search engine entirely by pointing the device at that page.

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

The scraper:
1. Fetches the index page and parses all links with their surrounding block context.
2. **Priority 1** — links in the same table row / block as text matching the normalized model number (e.g. `VZM31-SN` → `vzm31sn`).
3. **Priority 2** — links whose own URL contains the normalized model (catches short redirect URLs like `https://inov.li/vzm31snPDF`).
4. HEAD-checks each candidate to confirm a PDF is served (follows redirects).

`vendor_index_url` can also be set at the individual doc level (`doc.vendor_index_url`) for doc-type-specific index pages.

---

## Category Manager

Open **🏷 Categories** from the nav to manage the category taxonomy.

### Primary categories

- **Add** — key (slug), display label, icon emoji, electronic flag.
- **Edit** — label, icon, and electronic flag in-place.
- **Delete** — removes the category. Devices using it keep their existing value.

### Secondary / tertiary values

Click a primary category to load its secondary panel:

- Shows all secondary values actually in use across inventory, with device counts.
- **Rename** — updates every matching device in `devices.yaml` synchronously, then queues a Paperless retag (`cat2:old-slug` → `cat2:new-slug`) as a background task visible in the Tasks view.
- Suggested values from the category definition are shown as reference chips.
- Tertiary values (used by Automotive and similar categories) are listed separately.

---

## Home Assistant Integration

When `HA_URL` and `HA_TOKEN` are set, HA-specific nav items appear.

### How device fetching works

1. Posts a Jinja2 template to the HA `/api/template` endpoint, collecting physical devices (`entry_type is none`).
2. Deduplicates by `(manufacturer, model)` — 12 identical Hue bulbs appear as one import candidate.
3. Filters out devices already in inventory (matched by manufacturer + model) and devices on the ignore list.

### Ignore list

Ignoring stores both the HA device ID and the `(manufacturer, model)` pair. Future imports silently skip both the exact HA ID and any device with the same model.

### HA token

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
    # Optional: skip search engines and scrape vendor index page instead
    vendor_index_url: https://www.onkyo.com/manual/…
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
    allow_paid: false          # true = continue past free tier (charges apply)
    rate_limit:                # updated from X-RateLimit-* headers after each call
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

Usage counters are keyed by ISO date (`YYYY-MM-DD`). Monthly totals are computed by summing all days in the current month. The Settings UI shows a progress bar and collapsible day-by-day history per provider.

The `rate_limit` block under `brave` is written after each Brave API call from the `X-RateLimit-Remaining` and `X-RateLimit-Reset` response headers. When the `reset_date` is still in the future, this server-confirmed value takes precedence over the local counter sum in the limit-enforcement logic.

---

## Recommended Enhancements

The following features are not yet implemented but are straightforward additions that would complement the current architecture.

### 1. Search Result Caching

**What it does:** Caches the last successful `(manufacturer, model, doc_type) → PDF URL` mapping for 24 hours. Re-fetching a device after a transient error reuses the cached URL instead of burning API calls.

**Quick setup guide:**
1. Create `$DATA_DIR/search_cache.yaml` (app creates it automatically on first write).
2. Add a `cache_search_result(manufacturer, model, doc_type, url)` function to `config.py` that writes `{date: today, url: url}` under a compound key.
3. In `fetch_device_docs`, before calling `search_pdf_url`, check the cache. If a valid (< 24 h) URL exists, use it directly and log `[{doc_label}] Using cached URL`.
4. On download failure, invalidate the cache entry and retry with full search.

### 2. Bulk Retry for Unfetched Devices

**What it does:** A single button queues a background fetch for every device that has at least one doc in `not_found` or `error` state, processing them sequentially to avoid hammering APIs.

**Quick setup guide:**
1. Add a route `POST /devices/retry-all-failed` that reads all devices, filters to those with unresolved docs, and queues one `_run_fetch` background task per device.
2. Add a semaphore in `_run_fetch` (e.g. `asyncio.Semaphore(2)`) so at most 2 fetches run concurrently.
3. Add a **Retry all failed** button in `_device_list.html` near the top of the page, only shown when at least one device has an unresolved doc.
4. The Tasks view already shows all in-flight operations — no new UI needed.

### 3. Per-Search Attribution Logging

**What it does:** Records which search stage found each document (`found_by: brave`, `found_by: manufacturer-direct`, etc.) in `devices.yaml`. Over time this reveals which providers are actually useful for your device mix and informs which API tiers are worth paying for.

**Quick setup guide:**
1. Add `found_by: str | None = None` to the fields written in the final status block in `fetch_device_docs`.
2. Return the stage name from `search_pdf_url` alongside the URL (change return type to `tuple[str, str] | None`).
3. In the Settings UI, add a **Provider performance** section that reads all devices and counts `found_by` values — shows a small bar chart of which provider found the most manuals.

### 4. Vendor Index URL Library

**What it does:** A shared `$DATA_DIR/vendor_indexes.yaml` that maps manufacturer names to their manual index pages. When a device is fetched and its manufacturer is in the library, `vendor_index_url` is applied automatically without needing it on every device record.

**Quick setup guide:**
1. Create `$DATA_DIR/vendor_indexes.yaml`:
   ```yaml
   Inovelli: https://help.inovelli.com/en/articles/8426779-device-manuals
   Onkyo: https://www.onkyo.com/manual/
   ```
2. In `fetch_device_docs`, after reading the device, check `vendor_indexes.yaml` for the device's manufacturer; merge with any device-level `vendor_index_url` (device-level takes priority).
3. Add a **Vendor Index Library** section to the Category Manager or Settings view with a simple add/edit/delete table.

---

## Local Development

```bash
cd paperless/scripts/inventory_manager

pip install -r requirements.txt

DATA_DIR=../../inventory \
PAPERLESS_URL=http://localhost:8000 \
PAPERLESS_TOKEN=your-token \
uvicorn app:app --reload --port 7070
```

Open [http://localhost:7070](http://localhost:7070).

WeasyPrint system dependencies (Debian/Ubuntu):

```bash
apt-get install libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libglib2.0-0 fonts-liberation
```
