"""
Paperless Device Inventory Manager
Run: uvicorn app:app --reload --port 7070

Environment variables:
  DATA_DIR         Persistent data directory (default: ../../inventory).
                   Contains devices.yaml and a manuals/ staging subdirectory.
  PAPERLESS_URL    Base URL of the Paperless-NGX instance.
  PAPERLESS_TOKEN  Paperless API token. Enables upload and scoped deletion.
"""

import asyncio
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

import config as _config_mod
import tasks as _tasks_mod

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from ruamel.yaml import YAML

# ── Paths ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
_default_data = BASE_DIR.parent.parent / "inventory"
DATA_DIR = Path(os.getenv("DATA_DIR", str(_default_data)))
INVENTORY_PATH = DATA_DIR / "devices.yaml"
MANUALS_DIR = DATA_DIR / "manuals"

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
logger = logging.getLogger(__name__)

# ── Category taxonomy ──────────────────────────────────────────────────────

_DEFAULT_CATEGORY_TREE: dict[str, dict] = {
    "electronics": {
        "label": "Electronics", "icon": "📺",
        "secondaries": ["Entertainment", "Audio", "Computing", "Networking", "Photography"],
        "automotive": False, "electronic": True,
    },
    "appliances": {
        "label": "Appliances", "icon": "🍽",
        "secondaries": ["Kitchen", "Laundry", "Climate"],
        "automotive": False, "electronic": True,
    },
    "smart-home": {
        "label": "Smart Home", "icon": "🏠",
        "secondaries": ["Lighting", "Security", "Hubs & Controllers", "Sensors"],
        "automotive": False, "electronic": True,
    },
    "automotive": {
        "label": "Automotive", "icon": "🚗",
        "secondaries": [],
        "automotive": True, "electronic": False,
        "tertiaries": [
            "Owners Manual", "Service Manual",
            "Engine & Drivetrain", "Suspension & Brakes",
            "Exhaust", "Electrical", "Interior",
            "Body & Exterior", "Performance",
        ],
    },
    "tools": {
        "label": "Tools", "icon": "🔧",
        "secondaries": ["Power Tools", "Outdoor Power"],
        "automotive": False, "electronic": False,
    },
    "fitness": {
        "label": "Fitness", "icon": "💪",
        "secondaries": ["Cardio", "Strength"],
        "automotive": False, "electronic": False,
    },
    "outdoor": {
        "label": "Outdoor", "icon": "🌿",
        "secondaries": ["Lawn & Garden", "Recreation"],
        "automotive": False, "electronic": False,
    },
}

# Mutable category tree — populated from disk at startup (falls back to defaults)
CATEGORY_TREE: dict[str, dict] = {}


def _categories_path() -> Path:
    return DATA_DIR / "categories.yaml"


def _load_categories() -> None:
    p = _categories_path()
    CATEGORY_TREE.clear()
    if p.exists():
        ry = _ryaml()
        with open(p) as f:
            loaded = ry.load(f) or {}
        CATEGORY_TREE.update({k: dict(v) for k, v in loaded.items()})
    else:
        CATEGORY_TREE.update(_DEFAULT_CATEGORY_TREE)


def _save_categories() -> None:
    ry = _ryaml()
    with open(_categories_path(), "w") as f:
        ry.dump(dict(CATEGORY_TREE), f)

PROTOCOLS = ["ethernet", "matter", "wifi", "zigbee", "zwave"]
INTEGRATIONS = ["home_connect", "hue", "vivint", "zigbee2mqtt", "zwave_js"]
STATUSES = ["active", "retired", "stored"]

# ── State ──────────────────────────────────────────────────────────────────

_yaml_lock = threading.Lock()
# Tracks devices that have a fetch background task currently running.
_fetching: set[str] = set()

# ── App ────────────────────────────────────────────────────────────────────

_INPROGRESS_STATUSES: frozenset[str] = frozenset({"searching", "downloading", "uploading"})


def _reset_stuck_statuses() -> None:
    """
    On startup, any doc whose fetch_status is still an in-progress value from a
    prior run is reset to 'error' so the UI shows an actionable badge instead of
    a spinner that never resolves.
    """
    if not INVENTORY_PATH.exists():
        return
    try:
        with _yaml_lock:
            ry = _ryaml()
            with open(INVENTORY_PATH) as f:
                data = ry.load(f)
            changed = False
            for device in (data.get("devices") or []):
                for doc in (device.get("docs") or []):
                    if doc.get("fetch_status") in _INPROGRESS_STATUSES:
                        doc["fetch_status"] = "error"
                        doc["fetch_error"] = (
                            "Interrupted — server restarted during fetch. Click ↺ to retry."
                        )
                        changed = True
            if changed:
                with open(INVENTORY_PATH, "w") as f:
                    ry.dump(data, f)
                logger.info("Reset stuck in-progress doc statuses at startup")
    except Exception:
        logger.exception("Failed to reset stuck doc statuses at startup")


_PROVIDER_META: list[dict] = [
    {
        "id": "brave",
        "name": "Brave Search",
        "description": "High-quality JSON API with strong coverage of vendor support sites.",
        "free_limit": 1000,
        "period": "month",
        "has_paid_option": True,
        "fields": [{"name": "api_key", "label": "API Key", "type": "password"}],
        "signup_url": "https://api.search.brave.com/",
        "usage_url": "https://api-dashboard.search.brave.com/app/dashboard",
        "notes": (
            "Free tier: ~1,000 queries/month ($5 monthly credit at $5/1,000 requests). "
            "A credit card is required to create an account but is not charged within the "
            "free credit allowance. Remaining quota is read from Brave's response headers "
            "and updated automatically after each search."
        ),
    },
    {
        "id": "google_cse",
        "name": "Google Custom Search",
        "description": "Best index quality. Configure your CSE to search the entire web.",
        "free_limit": 100,
        "period": "day",
        "has_paid_option": True,
        "fields": [
            {"name": "api_key", "label": "API Key", "type": "password"},
            {"name": "cx",      "label": "Search Engine ID (CX)", "type": "text"},
        ],
        "signup_url": "https://programmablesearchengine.google.com/",
        "usage_url": "https://console.cloud.google.com/apis/api/customsearch.googleapis.com/quotas",
        "notes": "Free tier: 100 queries/day. Create a CSE at programmablesearchengine.google.com and set it to search the entire web.",
    },
    {
        "id": "bing",
        "name": "Bing Web Search",
        "description": "Microsoft search via Azure Cognitive Services.",
        "free_limit": 1000,
        "period": "month",
        "has_paid_option": True,
        "fields": [{"name": "api_key", "label": "API Key", "type": "password"}],
        "signup_url": "https://azure.microsoft.com/en-us/products/ai-services/bing-search",
        "usage_url": "https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBlade",
        "notes": "Free tier: 1,000 queries/month via Azure. Requires an Azure account.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    MANUALS_DIR.mkdir(parents=True, exist_ok=True)
    _config_mod.init(DATA_DIR / "config.yaml")
    _load_categories()
    _reset_stuck_statuses()
    yield


app = FastAPI(title="Paperless Device Inventory", lifespan=lifespan)

# ── YAML helpers ───────────────────────────────────────────────────────────

def _ryaml() -> YAML:
    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 120
    return ry


def load_data() -> dict:
    with _yaml_lock:
        ry = _ryaml()
        with open(INVENTORY_PATH) as f:
            return ry.load(f)


def save_data(data: dict) -> None:
    with _yaml_lock:
        ry = _ryaml()
        with open(INVENTORY_PATH, "w") as f:
            ry.dump(data, f)


def load_devices() -> list:
    data = load_data()
    return [dict(d) for d in (data.get("devices") or [])]


def _primary(device: dict) -> str:
    cat = device.get("category", {})
    if isinstance(cat, dict):
        return cat.get("primary", "other")
    return str(cat) or "other"


def devices_by_category(devices: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for d in devices:
        grouped.setdefault(_primary(d), []).append(d)
    return grouped


# ── Validation ─────────────────────────────────────────────────────────────

async def search_product(manufacturer: str, model: str) -> dict:
    query = f"{manufacturer} {model}"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json",
                        "no_redirect": "1", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "paperless-inventory/1.0 (homelab)"},
            )
        try:
            d = resp.json()
        except Exception:
            d = {}
        abstract = (d.get("AbstractText") or "").strip()
        abstract_url = d.get("AbstractURL") or ""
        related = [
            t.get("Text", "")
            for t in (d.get("RelatedTopics") or [])[:4]
            if isinstance(t, dict) and t.get("Text")
        ]
        if abstract:
            return {"found": True, "confidence": "high",
                    "summary": abstract, "url": abstract_url, "related": related}
        if related:
            return {"found": True, "confidence": "medium",
                    "summary": related[0], "url": abstract_url, "related": related[1:]}
        ddg_url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}"
        return {"found": False, "confidence": "low",
                "summary": "Not in DDG knowledge base (normal for niche/newer products) — verify via web search.",
                "url": ddg_url, "related": []}
    except Exception as exc:
        return {"found": False, "confidence": "error",
                "summary": f"Search error: {exc}", "url": "", "related": []}


# ── Slug helper ────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text.strip())
    return re.sub(r"-+", "-", text).strip("-")


# ── Device builder ─────────────────────────────────────────────────────────

def _doc_type_slug(label: str, existing: list[str]) -> str:
    """Slugify a label into a unique doc type key."""
    base = slugify(label) or "doc"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _parse_extra_docs(form_data) -> list[dict]:
    """Extract extra_label_N / extra_url_N pairs from raw form data."""
    import re as _re
    indices = sorted(
        int(m.group(1))
        for key in form_data.keys()
        if (m := _re.match(r"extra_label_(\d+)$", key))
    )
    result = []
    for idx in indices:
        label = str(form_data.get(f"extra_label_{idx}") or "").strip()
        url   = str(form_data.get(f"extra_url_{idx}")   or "").strip()
        if label:
            result.append({"label": label, "url": url})
    return result


def build_device(
    device_id: str,
    name: str,
    manufacturer: str,
    model: str,
    cat_primary: str,
    cat_secondary: str,
    cat_tertiary: str,
    protocols: List[str],
    integration: str,
    location: str,
    status: str,
    manual_hint: str,
    manual_url: str,
    extra_docs: list | None = None,
) -> dict:
    cat: dict = {"primary": cat_primary.strip()}
    if cat_secondary.strip():
        cat["secondary"] = cat_secondary.strip()
    if cat_tertiary.strip():
        cat["tertiary"] = cat_tertiary.strip()

    docs = []
    if manual_hint or manual_url:
        entry: dict = {"type": "manual", "fetch_status": "pending"}
        if manual_hint:
            entry["search_hint"] = manual_hint
        if manual_url:
            entry["url"] = manual_url
        docs.append(entry)

    for extra in (extra_docs or []):
        label = extra.get("label", "").strip()
        if not label:
            continue
        doc_type = _doc_type_slug(label, [d["type"] for d in docs])
        entry = {"type": doc_type, "label": label, "fetch_status": "pending"}
        if extra.get("url"):
            entry["url"] = extra["url"].strip()
        docs.append(entry)

    device: dict = {"id": device_id, "name": name.strip(), "manufacturer": manufacturer.strip()}
    if model.strip():
        device["model"] = model.strip()
    device["category"] = cat
    device["protocols"] = [p for p in protocols if p]
    if integration:
        device["integration"] = integration
    if location.strip():
        device["location"] = location.strip()
    device["status"] = status
    if docs:
        device["docs"] = docs
    return device


# ── Template context helpers ───────────────────────────────────────────────

def _cat_label(device: dict) -> str:
    cat = device.get("category", {})
    if not isinstance(cat, dict):
        return str(cat)
    parts = [cat.get("secondary") or "", cat.get("tertiary") or ""]
    return " / ".join(p for p in parts if p) or cat.get("primary", "")


def _tmpl_ctx(request: Request) -> dict:
    from ha_client import ha_available as _ha_available
    from fetch import update_doc_fields
    devices = load_devices()
    for d in devices:
        is_fetching = d.get("id") in _fetching
        # Eagerly fix any stuck in-progress statuses when building full-page context
        if not is_fetching:
            stuck = [
                doc for doc in (d.get("docs") or [])
                if doc.get("fetch_status") in _INPROGRESS_STATUSES
            ]
            for doc in stuck:
                update_doc_fields(d["id"], doc["type"], {
                    "fetch_status": "error",
                    "fetch_error": "Interrupted — server restarted during fetch. Click ↺ to retry.",
                }, INVENTORY_PATH, _yaml_lock)
                doc["fetch_status"] = "error"
                doc["fetch_error"] = "Interrupted — server restarted during fetch. Click ↺ to retry."
        d["_fetching"] = is_fetching
        d["_cat_label"] = _cat_label(d)
    return {
        "request": request,
        "by_category": devices_by_category(devices),
        "category_tree": CATEGORY_TREE,
        "total": len(devices),
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
        "ha_available": _ha_available(),
    }


# ── HA ignore list helpers ─────────────────────────────────────────────────

def _load_ha_ignored() -> list[dict]:
    data = load_data()
    return list(data.get("ha_ignored") or [])


def _save_ha_ignored(ignored: list[dict]) -> None:
    with _yaml_lock:
        ry = _ryaml()
        with open(INVENTORY_PATH) as f:
            data = ry.load(f)
        data["ha_ignored"] = ignored
        with open(INVENTORY_PATH, "w") as f:
            ry.dump(data, f)


def _form_ctx(request: Request, device: dict, edit_id: str | None) -> dict:
    cat = device.get("category", {})
    if isinstance(cat, str):
        cat = {}
    extra_docs = [
        {"label": d.get("label") or d.get("type", "").replace("-", " ").title(),
         "url": d.get("url", "")}
        for d in (device.get("docs") or [])
        if d.get("type") != "manual"
    ]
    return {
        "request": request,
        "device": device,
        "edit_id": edit_id,
        "category_tree": CATEGORY_TREE,
        "cat_primary": cat.get("primary", ""),
        "cat_secondary": cat.get("secondary", ""),
        "cat_tertiary": cat.get("tertiary", ""),
        "protocols": PROTOCOLS,
        "integrations": INTEGRATIONS,
        "statuses": STATUSES,
        "extra_docs": extra_docs,
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _tmpl_ctx(request))


@app.get("/devices/new", response_class=HTMLResponse)
async def new_form(request: Request):
    return templates.TemplateResponse(request, "_form.html", _form_ctx(request, {}, None))


@app.get("/devices/{device_id}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, device_id: str):
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")
    for doc in device.get("docs") or []:
        t = doc.get("type")
        device[f"{t}_hint"] = doc.get("search_hint", "")
        device[f"{t}_url"] = doc.get("url", "")
    return templates.TemplateResponse(request, "_form.html", _form_ctx(request, device, device_id))


@app.post("/validate", response_class=HTMLResponse)
async def validate(
    request: Request,
    manufacturer: str = Form(""),
    model: str = Form(""),
):
    if not manufacturer.strip() or not model.strip():
        return HTMLResponse("")
    result = await search_product(manufacturer.strip(), model.strip())
    return templates.TemplateResponse(request, "_validation.html", {"request": request, "result": result})


@app.post("/devices", response_class=HTMLResponse)
async def create_device(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    manufacturer: str = Form(...),
    model: str = Form(""),
    cat_primary: str = Form(...),
    cat_secondary: str = Form(""),
    cat_tertiary: str = Form(""),
    location: str = Form(""),
    status: str = Form("active"),
    integration: str = Form(""),
    custom_id: str = Form(""),
    protocols: List[str] = Form(default=[]),
    manual_hint: str = Form(""),
    manual_url: str = Form(""),
):
    form_data = await request.form()
    extra_docs = _parse_extra_docs(form_data)

    device_id = custom_id.strip() or slugify(f"{manufacturer}-{model or name}")
    data = load_data()
    existing = [d.get("id") for d in (data.get("devices") or [])]
    if device_id in existing:
        raise HTTPException(400, f"ID '{device_id}' already exists.")

    new_device = build_device(
        device_id, name, manufacturer, model,
        cat_primary, cat_secondary, cat_tertiary,
        protocols, integration, location, status,
        manual_hint, manual_url, extra_docs,
    )
    if data.get("devices") is None:
        data["devices"] = []
    data["devices"].append(new_device)
    save_data(data)

    # Trigger background fetch if there are docs to fetch
    if new_device.get("docs"):
        _fetching.add(device_id)
        background_tasks.add_task(_run_fetch, device_id)

    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


@app.post("/devices/{device_id}/update", response_class=HTMLResponse)
async def update_device(
    request: Request,
    device_id: str,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    manufacturer: str = Form(...),
    model: str = Form(""),
    cat_primary: str = Form(...),
    cat_secondary: str = Form(""),
    cat_tertiary: str = Form(""),
    location: str = Form(""),
    status: str = Form("active"),
    integration: str = Form(""),
    protocols: List[str] = Form(default=[]),
    manual_hint: str = Form(""),
    manual_url: str = Form(""),
):
    form_data = await request.form()
    extra_docs = _parse_extra_docs(form_data)

    data = load_data()
    devices = data.get("devices") or []
    idx = next((i for i, d in enumerate(devices) if d.get("id") == device_id), None)
    if idx is None:
        raise HTTPException(404, "Device not found")

    # Preserve existing doc status fields when editing
    existing_docs = {d.get("type"): dict(d) for d in (devices[idx].get("docs") or [])}
    updated = build_device(
        device_id, name, manufacturer, model,
        cat_primary, cat_secondary, cat_tertiary,
        protocols, integration, location, status,
        manual_hint, manual_url, extra_docs,
    )
    for doc in updated.get("docs") or []:
        prev = existing_docs.get(doc["type"], {})
        for field in ("fetch_status", "fetch_error", "source_url", "last_modified",
                      "etag", "pdf_mod_date", "pdf_version", "fetched_at", "paperless_id"):
            if field in prev:
                doc[field] = prev[field]

    devices[idx] = updated
    save_data(data)

    # Trigger fetch for any docs that have never been fetched and aren't already running
    if device_id not in _fetching and any(
        doc.get("fetch_status") == "pending"
        for doc in (updated.get("docs") or [])
    ):
        _fetching.add(device_id)
        background_tasks.add_task(_run_fetch, device_id)

    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


@app.post("/devices/{device_id}/retire", response_class=HTMLResponse)
async def retire_device(request: Request, device_id: str):
    data = load_data()
    for d in (data.get("devices") or []):
        if d.get("id") == device_id:
            d["status"] = "retired"
            break
    save_data(data)
    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


@app.delete("/devices/{device_id}", response_class=HTMLResponse)
async def delete_device(request: Request, device_id: str):
    from paperless_api import PaperlessClient, paperless_available
    if paperless_available():
        try:
            deleted = await PaperlessClient().delete_device_documents(device_id)
            if deleted:
                logger.info("Deleted %d Paperless doc(s) for device %s", len(deleted), device_id)
        except Exception:
            logger.exception("Paperless deletion failed for %s — continuing", device_id)

    data = load_data()
    devices = data.get("devices") or []
    data["devices"] = [d for d in devices if d.get("id") != device_id]
    save_data(data)
    _fetching.discard(device_id)
    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


# ── Duplicate check ────────────────────────────────────────────────────────

@app.get("/devices/check-duplicate", response_class=HTMLResponse)
async def check_duplicate(name: str = "", model: str = ""):
    """Return an inline HTML warning if name/model resemble an existing device."""
    name  = name.strip()
    model = model.strip()
    if not name and not model:
        return HTMLResponse("")

    devices  = load_devices()
    name_l   = name.lower()
    model_l  = model.lower()
    # Meaningful words (>2 chars) for name comparison
    q_words  = {w for w in name_l.split() if len(w) > 2}

    matches: list[dict] = []
    for d in devices:
        d_name  = (d.get("name")  or "").lower()
        d_model = (d.get("model") or "").lower()
        reasons: list[str] = []

        if q_words and d_name:
            d_words = {w for w in d_name.split() if len(w) > 2}
            overlap = q_words & d_words
            if len(overlap) >= 2 or any(len(w) >= 5 for w in overlap):
                reasons.append("similar name")

        if model_l and d_model and model_l == d_model:
            reasons.append("same model number")

        if reasons:
            matches.append({
                "name":         d.get("name", ""),
                "manufacturer": d.get("manufacturer", ""),
                "model":        d.get("model", ""),
                "reason":       " + ".join(reasons),
            })

    if not matches:
        return HTMLResponse("")

    items = "".join(
        f'<li><strong>{m["name"]}</strong>'
        f'{" (" + m["manufacturer"] + ")" if m["manufacturer"] else ""}'
        f'{" · <code>" + m["model"] + "</code>" if m["model"] else ""}'
        f' <span class="text-muted small">— {m["reason"]}</span></li>'
        for m in matches[:5]
    )
    return HTMLResponse(f"""
<div class="alert alert-warning py-2 px-3 mb-0" id="dup-alert" role="alert">
  <div class="fw-semibold small mb-1">⚠ Possible duplicate</div>
  <ul class="mb-2 small ps-3">{items}</ul>
  <button type="button" class="btn btn-sm btn-warning"
          onclick="this.closest('#dup-alert').remove()">
    Add Anyway
  </button>
</div>""")


# ── Inventory analysis helpers ─────────────────────────────────────────────

def _discover_secondaries() -> dict[str, dict[str, int]]:
    """Returns {primary: {secondary_value: device_count}} from inventory."""
    result: dict[str, dict[str, int]] = {}
    for d in load_devices():
        cat = d.get("category", {})
        if not isinstance(cat, dict):
            continue
        p, s = cat.get("primary", ""), cat.get("secondary", "")
        if p and s:
            result.setdefault(p, {})
            result[p][s] = result[p].get(s, 0) + 1
    return result


def _discover_tertiaries() -> dict[str, dict[str, int]]:
    """Returns {primary: {tertiary_value: device_count}} from inventory."""
    result: dict[str, dict[str, int]] = {}
    for d in load_devices():
        cat = d.get("category", {})
        if not isinstance(cat, dict):
            continue
        p, t = cat.get("primary", ""), cat.get("tertiary", "")
        if p and t:
            result.setdefault(p, {})
            result[p][t] = result[p].get(t, 0) + 1
    return result


# ── Fetch routes ────────────────────────────────────────────────────────────

async def _run_fetch(device_id: str) -> None:
    from fetch import fetch_device_docs
    # Resolve device name for a meaningful task label
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    device_name = device.get("name", device_id) if device else device_id
    app_task = _tasks_mod.create_task("fetch", f"{device_name} — fetch")
    try:
        await fetch_device_docs(device_id, INVENTORY_PATH, MANUALS_DIR, _yaml_lock, app_task)
    except Exception:
        logger.exception("Fetch pipeline error for device %s", device_id)
        _tasks_mod.task_done(app_task, False, "Unexpected error in fetch pipeline")
    finally:
        _fetching.discard(device_id)


@app.post("/devices/{device_id}/fetch", response_class=HTMLResponse)
async def trigger_fetch(request: Request, device_id: str, background_tasks: BackgroundTasks):
    """Manually re-trigger the fetch pipeline for a device."""
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")

    # Reset doc statuses to pending
    data = load_data()
    for d in (data.get("devices") or []):
        if d.get("id") == device_id:
            for doc in (d.get("docs") or []):
                doc["fetch_status"] = "pending"
                doc.pop("fetch_error", None)
            break
    save_data(data)

    _fetching.add(device_id)
    background_tasks.add_task(_run_fetch, device_id)
    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


@app.get("/devices/{device_id}/docs/{doc_type}/pdf")
async def serve_pdf(device_id: str, doc_type: str):
    """Serve the locally-stored PDF for a device document."""
    from fetch import pdf_filename
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")
    path = MANUALS_DIR / device_id / pdf_filename(device.get("name", device_id), doc_type)
    if not path.exists():
        raise HTTPException(404, "PDF not stored locally")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/devices/{device_id}/doc-status", response_class=HTMLResponse)
async def doc_status(request: Request, device_id: str):
    """Polled by the UI to update doc status badges while a fetch is running."""
    from fetch import update_doc_fields
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        return HTMLResponse("")
    is_fetching = device_id in _fetching
    # Detect docs stuck in an in-progress state with no active background task
    # (happens when the server was restarted mid-fetch). Reset them immediately.
    if not is_fetching:
        stuck = [
            doc for doc in (device.get("docs") or [])
            if doc.get("fetch_status") in _INPROGRESS_STATUSES
        ]
        if stuck:
            for doc in stuck:
                update_doc_fields(device_id, doc["type"], {
                    "fetch_status": "error",
                    "fetch_error": "Interrupted — server restarted during fetch. Click ↺ to retry.",
                }, INVENTORY_PATH, _yaml_lock)
            devices = load_devices()
            device = next((d for d in devices if d.get("id") == device_id), None)
            if not device:
                return HTMLResponse("")
    device["_fetching"] = is_fetching
    device["_cat_label"] = _cat_label(device)
    return templates.TemplateResponse(request, "_doc_status.html", {
        "request": request,
        "device": device,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
    })


# ── Paperless sync route ────────────────────────────────────────────────────

_DOC_TYPE_TO_LABEL: dict[str, str] = {
    "manual": "Manual",
    "quickstart": "Quick Start",
    "datasheet": "Datasheet",
}


def _expected_label(doc: dict) -> str:
    """Return the Paperless title suffix we'd expect for this inventory doc."""
    return doc.get("label") or _DOC_TYPE_TO_LABEL.get(
        doc.get("type", ""), doc.get("type", "").replace("-", " ").title()
    )


@app.post("/devices/{device_id}/sync-paperless", response_class=HTMLResponse)
async def sync_from_paperless(request: Request, device_id: str):
    """
    Query Paperless for all documents tagged to this device and reconcile them
    back into the inventory — sets fetch_status=success and populates paperless_id
    for any doc whose Paperless title matches.
    """
    from paperless_api import PaperlessClient, paperless_available
    from fetch import update_doc_fields

    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")

    if not paperless_available():
        sync_result = {"matched": 0, "total": 0, "note": "Paperless not configured"}
    else:
        try:
            pl_docs = await PaperlessClient().list_device_documents(device_id)
        except Exception as exc:
            sync_result = {"matched": 0, "total": 0, "note": f"Paperless error: {exc}"}
            pl_docs = []

        if pl_docs is not None:
            matched = 0
            for pl_doc in pl_docs:
                title = (pl_doc.get("title") or "").strip()
                if " — " not in title:
                    continue
                # Strip trailing update annotations like "(updated 2024-01-01 [note])"
                suffix = re.sub(r"\s*\(.*\)\s*$", "", title.split(" — ", 1)[1]).strip()

                for inv_doc in (device.get("docs") or []):
                    expected = _expected_label(inv_doc)
                    if suffix.lower() == expected.lower():
                        update_doc_fields(device_id, inv_doc["type"], {
                            "fetch_status": "success",
                            "fetch_error": None,
                            "paperless_id": pl_doc["id"],
                        }, INVENTORY_PATH, _yaml_lock)
                        matched += 1
                        break

            sync_result = {"matched": matched, "total": len(pl_docs)}

    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        return HTMLResponse("")
    device["_fetching"] = device_id in _fetching
    device["_cat_label"] = _cat_label(device)
    return templates.TemplateResponse(request, "_doc_status.html", {
        "request": request,
        "device": device,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
        "sync_result": sync_result,
    })


# ── Update check routes ─────────────────────────────────────────────────────

@app.post("/devices/{device_id}/check-update", response_class=HTMLResponse)
async def check_update(
    request: Request,
    device_id: str,
    doc_type: str = Form("manual"),
    note: str = Form(""),
):
    from fetch import check_and_apply_update
    result = await check_and_apply_update(
        device_id, doc_type, note, INVENTORY_PATH, MANUALS_DIR, _yaml_lock
    )
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        return HTMLResponse("")
    device["_fetching"] = False
    device["_cat_label"] = _cat_label(device)
    return templates.TemplateResponse(request, "_doc_status.html", {
        "request": request,
        "device": device,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
        "update_result": result,
    })


# ── Fallback: provide URL or upload file ───────────────────────────────────

async def _paperless_upload_and_resolve(
    device: dict,
    device_id: str,
    doc_type: str,
    dest_path: Path,
    base_fields: dict,
    app_task: "_tasks_mod.AppTask",
) -> None:
    """Upload dest_path to Paperless, wait for OCR, persist result. Runs in background."""
    import datetime
    from paperless_api import PaperlessClient, paperless_available
    from fetch import update_doc_fields

    if not paperless_available():
        update_doc_fields(device_id, doc_type, {
            **base_fields,
            "fetch_status": "success",
            "fetch_error": None,
            "fetched_at": datetime.date.today().isoformat(),
        }, INVENTORY_PATH, _yaml_lock)
        _tasks_mod.task_done(app_task, True, "Saved locally (no Paperless configured)")
        return

    try:
        label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
        cat = device.get("category", {}) if isinstance(device.get("category"), dict) else {}
        extra_tags = [
            f"{lvl}:{slugify(cat[key])}"
            for lvl, key in [("cat1", "primary"), ("cat2", "secondary"), ("cat3", "tertiary")]
            if cat.get(key)
        ]
        doc_label = label_map.get(doc_type, doc_type.title())
        _tasks_mod.task_log(app_task, "Uploading to Paperless…")
        client = PaperlessClient()
        task_id = await client.upload_document(
            dest_path,
            title=f"{device.get('name', device_id)} — {doc_label}",
            device_id=device_id,
            manufacturer=device.get("manufacturer", ""),
            doc_type=doc_label,
            extra_tags=extra_tags,
        )
        _tasks_mod.task_log(app_task, "Waiting for OCR (up to 10 min)…")
        paperless_id = await client.resolve_task(task_id, timeout=600)
        if paperless_id:
            update_doc_fields(device_id, doc_type, {
                **base_fields,
                "fetch_status": "success",
                "fetch_error": None,
                "paperless_id": paperless_id,
                "fetched_at": datetime.date.today().isoformat(),
            }, INVENTORY_PATH, _yaml_lock)
            _tasks_mod.task_done(app_task, True, f"Done — Paperless #{paperless_id}")
        else:
            msg = "Paperless OCR did not complete — check Paperless logs"
            update_doc_fields(device_id, doc_type, {
                **base_fields,
                "fetch_status": "error",
                "fetch_error": msg,
                "fetched_at": datetime.date.today().isoformat(),
            }, INVENTORY_PATH, _yaml_lock)
            _tasks_mod.task_done(app_task, False, msg)
    except Exception as exc:
        logger.exception("Paperless upload failed for %s/%s", device_id, doc_type)
        msg = str(exc)
        update_doc_fields(device_id, doc_type, {
            **base_fields,
            "fetch_status": "error",
            "fetch_error": msg,
            "fetched_at": datetime.date.today().isoformat(),
        }, INVENTORY_PATH, _yaml_lock)
        _tasks_mod.task_done(app_task, False, f"Error: {msg}")
    finally:
        _fetching.discard(device_id)


async def _run_provide_file_bg(
    device_id: str,
    doc_type: str,
    dest_path: Path,
    file_content: bytes,
    source_url: str | None,
    device: dict,
    app_task: "_tasks_mod.AppTask",
) -> None:
    """Background: validate + write file + upload to Paperless."""
    import datetime
    from fetch import extract_pdf_meta, update_doc_fields
    try:
        if file_content[:4] != b"%PDF":
            update_doc_fields(device_id, doc_type, {
                "fetch_status": "error",
                "fetch_error": "Uploaded file is not a valid PDF.",
                "fetched_at": datetime.date.today().isoformat(),
            }, INVENTORY_PATH, _yaml_lock)
            _tasks_mod.task_done(app_task, False, "Not a valid PDF")
            return
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(file_content)
        _tasks_mod.task_log(app_task, f"Saved {len(file_content)//1024} KB")
        pdf_meta = extract_pdf_meta(dest_path)
        base_fields: dict = {
            "pdf_mod_date": pdf_meta.get("pdf_mod_date"),
            "pdf_version": pdf_meta.get("pdf_version"),
            "pdf_pages": pdf_meta.get("pdf_pages"),
        }
        if source_url:
            base_fields["source_url"] = source_url
        _tasks_mod.task_log(app_task, f"{pdf_meta.get('pdf_pages', '?')} pages")
        await _paperless_upload_and_resolve(device, device_id, doc_type, dest_path, base_fields, app_task)
    except Exception as exc:
        logger.exception("File provide failed for %s/%s", device_id, doc_type)
        _tasks_mod.task_done(app_task, False, f"Error: {exc}")
        _fetching.discard(device_id)


async def _run_provide_url_bg(
    device_id: str,
    doc_type: str,
    dest_path: Path,
    source_url: str,
    device: dict,
    app_task: "_tasks_mod.AppTask",
) -> None:
    """Background: download URL + validate + upload to Paperless."""
    import datetime
    from fetch import download_pdf, extract_pdf_meta, update_doc_fields
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _tasks_mod.task_log(app_task, f"Downloading {source_url[:60]}…")
        try:
            http_headers = await download_pdf(source_url, dest_path)
        except Exception as exc:
            msg = f"Download failed: {exc}"
            update_doc_fields(device_id, doc_type, {
                "fetch_status": "error",
                "fetch_error": msg,
                "fetched_at": datetime.date.today().isoformat(),
            }, INVENTORY_PATH, _yaml_lock)
            _tasks_mod.task_done(app_task, False, msg)
            return
        converted_from_html = http_headers.get("converted_from_html", False)
        pdf_meta = extract_pdf_meta(dest_path)
        pages = pdf_meta.get("pdf_pages", 0)
        if pages == 1 and not converted_from_html:
            msg = "Downloaded PDF is only 1 page — likely a placeholder. Provide the direct PDF link."
            update_doc_fields(device_id, doc_type, {
                "fetch_status": "error",
                "fetch_error": msg,
                "fetched_at": datetime.date.today().isoformat(),
            }, INVENTORY_PATH, _yaml_lock)
            _tasks_mod.task_done(app_task, False, msg)
            return
        _tasks_mod.task_log(app_task, f"Downloaded — {pages} pages")
        base_fields: dict = {
            "source_url": source_url,
            "pdf_mod_date": pdf_meta.get("pdf_mod_date"),
            "pdf_version": pdf_meta.get("pdf_version"),
            "pdf_pages": pages,
            "last_modified": http_headers.get("last_modified"),
            "etag": http_headers.get("etag"),
        }
        await _paperless_upload_and_resolve(device, device_id, doc_type, dest_path, base_fields, app_task)
    except Exception as exc:
        logger.exception("URL provide failed for %s/%s", device_id, doc_type)
        _tasks_mod.task_done(app_task, False, f"Error: {exc}")
        _fetching.discard(device_id)


@app.post("/devices/{device_id}/provide-url", response_class=HTMLResponse)
async def provide_url(
    request: Request,
    background_tasks: BackgroundTasks,
    device_id: str,
    doc_type: str = Form("manual"),
    url: str = Form(""),
    file: UploadFile = File(None),
):
    """Accept a URL or file upload, queue as a background task, return immediately."""
    from fetch import update_doc_fields

    data = load_data()
    device = next((dict(d) for d in (data.get("devices") or []) if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")

    from fetch import pdf_filename
    dest_path = MANUALS_DIR / device_id / pdf_filename(device.get("name", device_id), doc_type)
    label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
    doc_label = label_map.get(doc_type, doc_type.title())
    task_label = f"{device.get('name', device_id)} — {doc_label}"

    if file and file.filename:
        file_content = await file.read()
        if file_content[:4] != b"%PDF":
            raise HTTPException(400, "Uploaded file does not appear to be a PDF.")
        app_task = _tasks_mod.create_task("upload", task_label)
        update_doc_fields(device_id, doc_type, {"fetch_status": "uploading", "fetch_error": None},
                          INVENTORY_PATH, _yaml_lock)
        _fetching.add(device_id)
        background_tasks.add_task(
            _run_provide_file_bg, device_id, doc_type, dest_path,
            file_content, url.strip() or None, device, app_task,
        )
    elif url.strip():
        app_task = _tasks_mod.create_task("upload", task_label)
        update_doc_fields(device_id, doc_type, {"fetch_status": "uploading", "fetch_error": None},
                          INVENTORY_PATH, _yaml_lock)
        _fetching.add(device_id)
        background_tasks.add_task(
            _run_provide_url_bg, device_id, doc_type, dest_path,
            url.strip(), device, app_task,
        )
    else:
        raise HTTPException(400, "Provide a URL or upload a file.")

    # Return immediately — modal closes, uploading spinner shows in device list
    devices = load_devices()
    device_updated = next((d for d in devices if d.get("id") == device_id), None)
    if not device_updated:
        return HTMLResponse("")
    device_updated["_fetching"] = True
    device_updated["_cat_label"] = _cat_label(device_updated)
    return templates.TemplateResponse(request, "_doc_status.html", {
        "request": request,
        "device": device_updated,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
    })


# ── Home Assistant import routes ───────────────────────────────────────────

@app.get("/tools/ha-import", response_class=HTMLResponse)
async def ha_import_page(request: Request):
    return templates.TemplateResponse(request, "_ha_import.html", {"request": request})


@app.post("/tools/ha-fetch", response_class=HTMLResponse)
async def ha_fetch(request: Request):
    """Fetch physical devices from HA, filter against inventory + ignore list."""
    from ha_client import fetch_ha_devices, deduplicate_by_model

    try:
        raw_devices = await fetch_ha_devices()
    except Exception as exc:
        return HTMLResponse(
            f'<div id="ha-import-content" class="alert alert-danger">'
            f"Failed to connect to Home Assistant: {exc}</div>"
        )

    deduped = deduplicate_by_model(raw_devices)

    # Load existing inventory models for filtering
    inventory = load_devices()
    inv_models: set[tuple[str, str]] = set()
    for d in inventory:
        mfr   = (d.get("manufacturer") or "").strip().lower()
        model = (d.get("model")        or "").strip().lower()
        if model:
            inv_models.add((mfr, model))

    # Load ignore list
    ignored = _load_ha_ignored()
    ignored_ha_ids: set[str]              = {i["ha_id"] for i in ignored if i.get("ha_id")}
    ignored_models: set[tuple[str, str]]  = set()
    for i in ignored:
        mfr   = (i.get("manufacturer") or "").strip().lower()
        model = (i.get("model")        or "").strip().lower()
        if model:
            ignored_models.add((mfr, model))

    importable: list[dict] = []
    skipped = 0
    for d in deduped:
        mfr   = (d.get("manufacturer") or "").strip().lower()
        model = (d.get("model")        or "").strip().lower()
        model_key = (mfr, model)

        if d["id"] in ignored_ha_ids:
            skipped += 1
            continue
        if model and model_key in ignored_models:
            skipped += 1
            continue
        if model and model_key in inv_models:
            skipped += 1
            continue

        importable.append({
            "ha_id":        d["id"],
            "name":         d.get("name", ""),
            "manufacturer": d.get("manufacturer", ""),
            "model":        d.get("model", ""),
            "area":         d.get("area", ""),
            "domain":       d.get("domain", ""),
        })

    return templates.TemplateResponse(request, "_ha_device_list.html", {
        "request":      request,
        "ha_devices":   importable,
        "skipped_count": skipped,
    })


@app.post("/tools/ha-import/execute", response_class=HTMLResponse)
async def ha_import_execute(
    request: Request,
    background_tasks: BackgroundTasks,
    ha_ids: List[str] = Form(default=[]),
):
    """Import selected HA devices into the inventory."""
    from ha_client import fetch_ha_devices, domain_to_protocols

    if not ha_ids:
        raise HTTPException(400, "No devices selected.")

    try:
        all_ha = await fetch_ha_devices()
    except Exception as exc:
        raise HTTPException(502, f"Could not re-fetch HA devices: {exc}")

    ha_map = {d["id"]: d for d in all_ha}

    data = load_data()
    if not isinstance(data.get("devices"), list):
        data["devices"] = []

    existing_ids: set[str] = {d.get("id", "") for d in data["devices"]}
    imported_ids: list[str] = []

    for ha_id in ha_ids:
        ha_dev = ha_map.get(ha_id)
        if not ha_dev:
            continue

        mfr   = (ha_dev.get("manufacturer") or "").strip()
        model = (ha_dev.get("model")        or "").strip()
        raw_name = model or (ha_dev.get("name") or "").strip() or mfr or ha_id
        name  = raw_name.title()

        base_id = slugify(f"{mfr}-{model}" if (mfr and model) else f"{mfr}-{name}" if mfr else name)
        device_id = base_id
        n = 2
        while device_id in existing_ids:
            device_id = f"{base_id}-{n}"
            n += 1
        existing_ids.add(device_id)

        protocols = domain_to_protocols(ha_dev.get("domain", ""))
        new_device: dict = {
            "id":           device_id,
            "name":         name,
            "manufacturer": mfr,
            "status":       "active",
            "category":     {"primary": "smart-home"},
            "protocols":    protocols,
        }
        if model:
            new_device["model"] = model
        if ha_dev.get("area"):
            new_device["location"] = ha_dev["area"]

        hint_parts = [p for p in [mfr, model, "user manual PDF"] if p]
        new_device["docs"] = [{"type": "manual", "fetch_status": "pending", "search_hint": " ".join(hint_parts)}]

        data["devices"].append(new_device)
        imported_ids.append(device_id)

    save_data(data)
    logger.info("HA import: added %d device(s)", len(imported_ids))

    # Kick off a background fetch for each newly imported device
    for device_id in imported_ids:
        _fetching.add(device_id)
        background_tasks.add_task(_run_fetch, device_id)

    return templates.TemplateResponse(request, "_device_list.html", _tmpl_ctx(request))


@app.post("/tools/ha-ignore", response_class=HTMLResponse)
async def ha_ignore_add(
    ha_id:        str = Form(...),
    name:         str = Form(""),
    manufacturer: str = Form(""),
    model:        str = Form(""),
    area:         str = Form(""),
):
    """Add a device to the HA ignore list."""
    import datetime
    ignored = _load_ha_ignored()
    # Avoid duplicates in the list
    if not any(i.get("ha_id") == ha_id for i in ignored):
        ignored.append({
            "ha_id":        ha_id,
            "name":         name,
            "manufacturer": manufacturer,
            "model":        model,
            "ignored_at":   datetime.date.today().isoformat(),
        })
        _save_ha_ignored(ignored)
    return HTMLResponse("")  # hx-swap="delete" removes the row client-side


@app.get("/tools/ha-ignore-list", response_class=HTMLResponse)
async def ha_ignore_list(request: Request):
    ignored = _load_ha_ignored()
    return templates.TemplateResponse(request, "_ha_ignore_list.html", {
        "request":         request,
        "ignored_devices": ignored,
    })


@app.delete("/tools/ha-ignore/{ha_id:path}", response_class=HTMLResponse)
async def ha_ignore_remove(ha_id: str):
    """Remove a device from the HA ignore list."""
    ignored = _load_ha_ignored()
    _save_ha_ignored([i for i in ignored if i.get("ha_id") != ha_id])
    return HTMLResponse("")  # hx-swap="delete" removes the row client-side


# ── View routes (main-view HTMX navigation) ────────────────────────────────

@app.get("/views/inventory", response_class=HTMLResponse)
async def views_inventory(request: Request):
    return templates.TemplateResponse(request, "_view_inventory.html", _tmpl_ctx(request))


@app.get("/views/tasks", response_class=HTMLResponse)
async def views_tasks(request: Request):
    task_list = _tasks_mod.list_tasks()
    return templates.TemplateResponse(request, "_tasks.html", {
        "request": request,
        "tasks": task_list,
        "any_running": _tasks_mod.any_running(),
    })


@app.post("/views/tasks/clear", response_class=HTMLResponse)
async def views_tasks_clear(request: Request):
    _tasks_mod.clear_completed()
    task_list = _tasks_mod.list_tasks()
    return templates.TemplateResponse(request, "_tasks.html", {
        "request": request,
        "tasks": task_list,
        "any_running": _tasks_mod.any_running(),
    })


@app.get("/views/categories", response_class=HTMLResponse)
async def views_categories(request: Request):
    sec = _discover_secondaries()
    ter = _discover_tertiaries()
    return templates.TemplateResponse(request, "_category_manager.html", {
        "request": request,
        "category_tree": CATEGORY_TREE,
        "secondaries": sec,
        "tertiaries": ter,
    })


# ── Category CRUD routes ────────────────────────────────────────────────────

def _cat_manager_ctx(request: Request) -> dict:
    return {
        "request": request,
        "category_tree": CATEGORY_TREE,
        "secondaries": _discover_secondaries(),
        "tertiaries": _discover_tertiaries(),
    }


@app.post("/tools/categories", response_class=HTMLResponse)
async def category_create(
    request: Request,
    key: str = Form(...),
    label: str = Form(...),
    icon: str = Form("📦"),
    electronic: str = Form(""),
):
    key = slugify(key.strip()) or slugify(label)
    if not key or key in CATEGORY_TREE:
        raise HTTPException(400, f"Key '{key}' is empty or already exists.")
    CATEGORY_TREE[key] = {
        "label": label.strip(),
        "icon": icon.strip() or "📦",
        "secondaries": [],
        "automotive": False,
        "electronic": bool(electronic),
    }
    _save_categories()
    return templates.TemplateResponse(request, "_category_manager.html", _cat_manager_ctx(request))


@app.delete("/tools/categories/{key}", response_class=HTMLResponse)
async def category_delete(request: Request, key: str):
    CATEGORY_TREE.pop(key, None)
    _save_categories()
    return templates.TemplateResponse(request, "_category_manager.html", _cat_manager_ctx(request))


@app.post("/tools/categories/{key}/edit", response_class=HTMLResponse)
async def category_edit(
    request: Request,
    key: str,
    label: str = Form(...),
    icon: str = Form(""),
    electronic: str = Form(""),
):
    if key not in CATEGORY_TREE:
        raise HTTPException(404, "Category not found")
    CATEGORY_TREE[key]["label"] = label.strip()
    CATEGORY_TREE[key]["icon"] = icon.strip() or CATEGORY_TREE[key].get("icon", "📦")
    CATEGORY_TREE[key]["electronic"] = bool(electronic)
    _save_categories()
    return templates.TemplateResponse(request, "_category_manager.html", _cat_manager_ctx(request))


@app.get("/tools/categories/{key}/secondaries", response_class=HTMLResponse)
async def category_secondaries(request: Request, key: str):
    info = CATEGORY_TREE.get(key)
    if not info:
        raise HTTPException(404, "Category not found")
    all_sec = _discover_secondaries()
    all_ter = _discover_tertiaries()
    return templates.TemplateResponse(request, "_secondary_panel.html", {
        "request": request,
        "primary_key": key,
        "info": info,
        "secondaries": all_sec.get(key, {}),
        "tertiaries": all_ter.get(key, {}),
        "suggested": info.get("secondaries", []),
        "suggested_tertiaries": info.get("tertiaries", []),
    })


@app.post("/tools/categories/{key}/secondaries/rename", response_class=HTMLResponse)
async def category_secondary_rename(
    request: Request,
    background_tasks: BackgroundTasks,
    key: str,
    old_val: str = Form(...),
    new_val: str = Form(...),
    field: str = Form("secondary"),   # "secondary" or "tertiary"
):
    """Rename a secondary/tertiary value across all devices in a category, retag Paperless."""
    old_val = old_val.strip()
    new_val = new_val.strip()
    if not new_val or old_val == new_val:
        # No-op — just re-render the panel
        info = CATEGORY_TREE.get(key, {})
        return templates.TemplateResponse(request, "_secondary_panel.html", {
            "request": request,
            "primary_key": key,
            "info": info,
            "secondaries": _discover_secondaries().get(key, {}),
            "tertiaries": _discover_tertiaries().get(key, {}),
            "suggested": info.get("secondaries", []),
            "suggested_tertiaries": info.get("tertiaries", []),
        })

    # ── Update inventory YAML ──────────────────────────────────────────────
    updated = 0
    with _yaml_lock:
        ry = _ryaml()
        with open(INVENTORY_PATH) as f:
            data = ry.load(f)
        for d in (data.get("devices") or []):
            cat = d.get("category")
            if not isinstance(cat, dict):
                continue
            if cat.get("primary") != key:
                continue
            if field == "secondary" and cat.get("secondary", "").strip() == old_val:
                cat["secondary"] = new_val
                updated += 1
            elif field == "tertiary" and cat.get("tertiary", "").strip() == old_val:
                cat["tertiary"] = new_val
                updated += 1
        with open(INVENTORY_PATH, "w") as f:
            ry.dump(data, f)

    # ── Retag Paperless in background ──────────────────────────────────────
    tag_level = "cat2" if field == "secondary" else "cat3"
    old_tag = f"{tag_level}:{slugify(old_val)}"
    new_tag = f"{tag_level}:{slugify(new_val)}"

    if old_tag != new_tag:
        app_task = _tasks_mod.create_task(
            "retag",
            f"Retag {tag_level}: {old_val!r} → {new_val!r} ({updated} device(s))",
        )

        async def _do_retag(old: str, new: str, task: "_tasks_mod.AppTask") -> None:
            from paperless_api import PaperlessClient, paperless_available
            if not paperless_available():
                _tasks_mod.task_done(task, True, "No Paperless configured — inventory updated only")
                return
            try:
                _tasks_mod.task_log(task, f"Retagging {old!r} → {new!r}…")
                count = await PaperlessClient().retag_category(old, new)
                _tasks_mod.task_done(task, True, f"Retagged {count} document(s) in Paperless")
            except Exception as exc:
                _tasks_mod.task_done(task, False, f"Paperless retag failed: {exc}")

        background_tasks.add_task(_do_retag, old_tag, new_tag, app_task)

    info = CATEGORY_TREE.get(key, {})
    return templates.TemplateResponse(request, "_secondary_panel.html", {
        "request": request,
        "primary_key": key,
        "info": info,
        "secondaries": _discover_secondaries().get(key, {}),
        "tertiaries": _discover_tertiaries().get(key, {}),
        "suggested": info.get("secondaries", []),
        "suggested_tertiaries": info.get("tertiaries", []),
        "rename_msg": f"Updated {updated} device(s): {old_val!r} → {new_val!r}",
    })


# ── Manufacturer bulk-rename routes ─────────────────────────────────────────

def _load_manufacturers() -> list[dict]:
    """Return [{name, count}] sorted alphabetically from devices.yaml."""
    with _yaml_lock:
        ry = _ryaml()
        with open(INVENTORY_PATH) as f:
            data = ry.load(f) or {}
    counter: dict[str, int] = {}
    for d in (data.get("devices") or []):
        mfr = (d.get("manufacturer") or "").strip()
        if mfr:
            counter[mfr] = counter.get(mfr, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda x: x[0].lower())
    ]


@app.get("/views/manufacturers", response_class=HTMLResponse)
async def views_manufacturers(request: Request):
    return templates.TemplateResponse(request, "_manufacturers.html", {
        "request": request,
        "manufacturers": _load_manufacturers(),
        "rename_results": [],
    })


@app.post("/tools/manufacturers/bulk-rename", response_class=HTMLResponse)
async def manufacturers_bulk_rename(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Apply multiple manufacturer renames at once; retag Paperless mfr: tags in background."""
    form = await request.form()

    # Collect (old, new) pairs where names actually changed
    renames: dict[str, str] = {}
    i = 0
    while f"old_{i}" in form:
        old = str(form[f"old_{i}"]).strip()
        new = str(form[f"new_{i}"]).strip()
        if old and new and old != new:
            renames[old] = new
        i += 1

    rename_results: list[dict] = []

    if renames:
        with _yaml_lock:
            ry = _ryaml()
            with open(INVENTORY_PATH) as f:
                data = ry.load(f) or {}
            counts: dict[str, int] = {old: 0 for old in renames}
            for d in (data.get("devices") or []):
                mfr = (d.get("manufacturer") or "").strip()
                if mfr in renames:
                    d["manufacturer"] = renames[mfr]
                    counts[mfr] += 1
            with open(INVENTORY_PATH, "w") as f:
                ry.dump(data, f)

        # Queue Paperless mfr: tag retag for each rename
        for old_name, new_name in renames.items():
            old_tag = f"mfr:{slugify(old_name)}"
            new_tag = f"mfr:{slugify(new_name)}"
            n = counts[old_name]
            rename_results.append({"old": old_name, "new": new_name, "count": n, "old_tag": old_tag, "new_tag": new_tag})

            if old_tag != new_tag and n > 0:
                app_task = _tasks_mod.create_task(
                    "retag",
                    f"Retag mfr: {old_name!r} → {new_name!r} ({n} device(s))",
                )

                async def _do_retag(o: str, n_: str, task: "_tasks_mod.AppTask") -> None:
                    from paperless_api import PaperlessClient, paperless_available
                    if not paperless_available():
                        _tasks_mod.task_done(task, True, "No Paperless configured — inventory updated only")
                        return
                    try:
                        _tasks_mod.task_log(task, f"Retagging {o!r} → {n_!r}…")
                        count = await PaperlessClient().retag_category(o, n_)
                        _tasks_mod.task_done(task, True, f"Retagged {count} document(s) in Paperless")
                    except Exception as exc:
                        _tasks_mod.task_done(task, False, f"Paperless retag failed: {exc}")

                background_tasks.add_task(_do_retag, old_tag, new_tag, app_task)

    return templates.TemplateResponse(request, "_manufacturers.html", {
        "request": request,
        "manufacturers": _load_manufacturers(),
        "rename_results": rename_results,
    })


# ── Settings routes ─────────────────────────────────────────────────────────

def _settings_ctx(request: Request) -> dict:
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    month = today.strftime("%Y-%m")

    # Compute the 1st of next month as a default rollover date
    if today.month == 12:
        default_rollover = f"{today.year + 1}-01-01"
    else:
        default_rollover = f"{today.year}-{today.month + 1:02d}-01"

    providers = []
    for meta in _PROVIDER_META:
        pid   = meta["id"]
        cfg   = _config_mod.get_provider(pid)
        limit = meta["free_limit"]
        is_daily = meta["period"] == "day"

        # Local usage counters
        local_usage = _config_mod.current_day_usage(pid) if is_daily else _config_mod.current_month_usage(pid)

        # Server-reported rate-limit data (updated from Brave response headers)
        rl = _config_mod.get_rate_limit_info(pid)
        server_remaining = rl.get("remaining")
        server_reset     = rl.get("reset_date")

        # Decide what to display as the primary usage figure
        if server_remaining is not None and server_reset and server_reset >= today.isoformat():
            display_used = limit - server_remaining
            display_remaining = server_remaining
            source = "server"
        else:
            display_used = local_usage
            display_remaining = max(0, limit - local_usage)
            source = "local"

        pct = min(100, round(display_used / limit * 100)) if limit else 0
        at_limit = _config_mod.is_over_free_limit(pid)
        paid_ok  = _config_mod.allow_paid(pid)

        # Reset / rollover date to display
        rollover = server_reset if (server_reset and server_reset >= today.isoformat()) else default_rollover
        try:
            days_until = (_date.fromisoformat(rollover) - today).days
        except Exception:
            days_until = None

        providers.append({
            **meta,
            "cfg": cfg,
            "configured": bool(cfg.get("api_key")),
            "local_usage": local_usage,
            "display_used": display_used,
            "display_remaining": display_remaining,
            "usage_pct": pct,
            "usage_class": "bg-danger" if pct >= 100 else "bg-warning" if pct >= 80 else "bg-success",
            "at_limit": at_limit,
            "allow_paid": paid_ok,
            "source": source,
            "rollover": rollover,
            "days_until_reset": days_until,
            "period_label": "Today" if is_daily else "This month",
            "history": _config_mod.usage_history(pid),
        })
    return {
        "request": request,
        "providers": providers,
        "current_month": month,
        "today": today.isoformat(),
    }


@app.get("/views/settings", response_class=HTMLResponse)
async def views_settings(request: Request):
    return templates.TemplateResponse(request, "_settings.html", _settings_ctx(request))


@app.post("/tools/settings/search/{provider_id}", response_class=HTMLResponse)
async def settings_save_provider(request: Request, provider_id: str):
    """Save API key (and optional extra fields) for a search provider."""
    if not any(p["id"] == provider_id for p in _PROVIDER_META):
        raise HTTPException(404, "Unknown provider")
    form = await request.form()
    # Handle allow_paid separately — checkboxes are absent when unchecked,
    # so we can't rely on set_provider's "skip blank" merge logic for booleans.
    allow_paid = "allow_paid" in form
    _config_mod.set_allow_paid(provider_id, allow_paid)
    # Key fields: merge non-blank values only (preserves existing key if left blank)
    key_fields = {k: str(v).strip() for k, v in form.items() if k != "allow_paid"}
    _config_mod.set_provider(provider_id, key_fields)
    return templates.TemplateResponse(request, "_settings.html", _settings_ctx(request))


@app.delete("/tools/settings/search/{provider_id}", response_class=HTMLResponse)
async def settings_remove_provider(request: Request, provider_id: str):
    """Remove API key for a search provider."""
    _config_mod.remove_provider(provider_id)
    return templates.TemplateResponse(request, "_settings.html", _settings_ctx(request))


@app.post("/tools/settings/search/{provider_id}/sync-usage", response_class=HTMLResponse)
async def settings_sync_usage(request: Request, provider_id: str):
    """
    Probe the provider API to get current quota and update the cached value.
    Currently only Brave supports this (quota is in response headers).
    For other providers, clears any stale cached data so the local counter is used.
    """
    logger.info("sync-usage requested for provider: %s", provider_id)
    if not any(p["id"] == provider_id for p in _PROVIDER_META):
        raise HTTPException(404, "Unknown provider")

    if provider_id == "brave":
        api_key = _config_mod.get_api_key("brave")
        if api_key:
            from fetch import brave_sync_usage
            await brave_sync_usage(api_key)
        else:
            logger.warning("sync-usage: Brave selected but no API key configured")
    else:
        _config_mod.clear_rate_limit_info(provider_id)

    return templates.TemplateResponse(request, "_settings.html", _settings_ctx(request))
