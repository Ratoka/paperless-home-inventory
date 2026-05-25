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

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
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

CATEGORY_TREE: dict[str, dict] = {
    "electronics": {
        "label": "Electronics", "icon": "📺",
        "secondaries": ["Entertainment", "Audio", "Computing", "Networking", "Photography"],
        "automotive": False,
    },
    "appliances": {
        "label": "Appliances", "icon": "🍽",
        "secondaries": ["Kitchen", "Laundry", "Climate"],
        "automotive": False,
    },
    "smart-home": {
        "label": "Smart Home", "icon": "🏠",
        "secondaries": ["Lighting", "Security", "Hubs & Controllers", "Sensors"],
        "automotive": False,
    },
    "automotive": {
        "label": "Automotive", "icon": "🚗",
        "secondaries": [],
        "automotive": True,
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
        "automotive": False,
    },
    "fitness": {
        "label": "Fitness", "icon": "💪",
        "secondaries": ["Cardio", "Strength"],
        "automotive": False,
    },
    "outdoor": {
        "label": "Outdoor", "icon": "🌿",
        "secondaries": ["Lawn & Garden", "Recreation"],
        "automotive": False,
    },
}

PROTOCOLS = ["ethernet", "matter", "wifi", "zigbee", "zwave"]
INTEGRATIONS = ["home_connect", "hue", "vivint", "zigbee2mqtt", "zwave_js"]
STATUSES = ["active", "retired", "stored"]

# ── State ──────────────────────────────────────────────────────────────────

_yaml_lock = threading.Lock()
# Tracks devices that have a fetch background task currently running.
_fetching: set[str] = set()

# ── App ────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    MANUALS_DIR.mkdir(parents=True, exist_ok=True)
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
        d = resp.json()
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
                "summary": "Not found in knowledge base — verify manually.",
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
    quickstart_hint: str,
    quickstart_url: str,
    datasheet_hint: str,
    datasheet_url: str,
) -> dict:
    cat: dict = {"primary": cat_primary.strip()}
    if cat_secondary.strip():
        cat["secondary"] = cat_secondary.strip()
    if cat_tertiary.strip():
        cat["tertiary"] = cat_tertiary.strip()

    docs = []
    for doc_type, hint, url in [
        ("manual", manual_hint, manual_url),
        ("quickstart", quickstart_hint, quickstart_url),
        ("datasheet", datasheet_hint, datasheet_url),
    ]:
        if hint or url:
            entry: dict = {"type": doc_type, "fetch_status": "pending"}
            if hint:
                entry["search_hint"] = hint
            if url:
                entry["url"] = url
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
    devices = load_devices()
    for d in devices:
        d["_fetching"] = d.get("id") in _fetching
        d["_cat_label"] = _cat_label(d)
    return {
        "request": request,
        "by_category": devices_by_category(devices),
        "category_tree": CATEGORY_TREE,
        "total": len(devices),
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
    }


def _form_ctx(request: Request, device: dict, edit_id: str | None) -> dict:
    cat = device.get("category", {})
    if isinstance(cat, str):
        cat = {}
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
    }


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", _tmpl_ctx(request))


@app.get("/devices/new", response_class=HTMLResponse)
async def new_form(request: Request):
    return templates.TemplateResponse("_form.html", _form_ctx(request, {}, None))


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
    return templates.TemplateResponse("_form.html", _form_ctx(request, device, device_id))


@app.post("/validate", response_class=HTMLResponse)
async def validate(
    request: Request,
    manufacturer: str = Form(""),
    model: str = Form(""),
):
    if not manufacturer.strip() or not model.strip():
        return HTMLResponse("")
    result = await search_product(manufacturer.strip(), model.strip())
    return templates.TemplateResponse("_validation.html", {"request": request, "result": result})


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
    quickstart_hint: str = Form(""),
    quickstart_url: str = Form(""),
    datasheet_hint: str = Form(""),
    datasheet_url: str = Form(""),
):
    device_id = custom_id.strip() or slugify(f"{manufacturer}-{model or name}")
    data = load_data()
    existing = [d.get("id") for d in (data.get("devices") or [])]
    if device_id in existing:
        raise HTTPException(400, f"ID '{device_id}' already exists.")

    new_device = build_device(
        device_id, name, manufacturer, model,
        cat_primary, cat_secondary, cat_tertiary,
        protocols, integration, location, status,
        manual_hint, manual_url, quickstart_hint, quickstart_url,
        datasheet_hint, datasheet_url,
    )
    if data.get("devices") is None:
        data["devices"] = []
    data["devices"].append(new_device)
    save_data(data)

    # Trigger background fetch if there are docs to fetch
    if new_device.get("docs"):
        _fetching.add(device_id)
        background_tasks.add_task(_run_fetch, device_id)

    return templates.TemplateResponse("_device_list.html", _tmpl_ctx(request))


@app.post("/devices/{device_id}/update", response_class=HTMLResponse)
async def update_device(
    request: Request,
    device_id: str,
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
    quickstart_hint: str = Form(""),
    quickstart_url: str = Form(""),
    datasheet_hint: str = Form(""),
    datasheet_url: str = Form(""),
):
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
        manual_hint, manual_url, quickstart_hint, quickstart_url,
        datasheet_hint, datasheet_url,
    )
    for doc in updated.get("docs") or []:
        prev = existing_docs.get(doc["type"], {})
        for field in ("fetch_status", "fetch_error", "source_url", "last_modified",
                      "etag", "pdf_mod_date", "pdf_version", "fetched_at", "paperless_id"):
            if field in prev:
                doc[field] = prev[field]

    devices[idx] = updated
    save_data(data)
    return templates.TemplateResponse("_device_list.html", _tmpl_ctx(request))


@app.post("/devices/{device_id}/retire", response_class=HTMLResponse)
async def retire_device(request: Request, device_id: str):
    data = load_data()
    for d in (data.get("devices") or []):
        if d.get("id") == device_id:
            d["status"] = "retired"
            break
    save_data(data)
    return templates.TemplateResponse("_device_list.html", _tmpl_ctx(request))


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
    return templates.TemplateResponse("_device_list.html", _tmpl_ctx(request))


# ── Fetch routes ────────────────────────────────────────────────────────────

async def _run_fetch(device_id: str) -> None:
    from fetch import fetch_device_docs
    try:
        await fetch_device_docs(device_id, INVENTORY_PATH, MANUALS_DIR, _yaml_lock)
    except Exception:
        logger.exception("Fetch pipeline error for device %s", device_id)
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
    return templates.TemplateResponse("_device_list.html", _tmpl_ctx(request))


@app.get("/devices/{device_id}/doc-status", response_class=HTMLResponse)
async def doc_status(request: Request, device_id: str):
    """Polled by the UI to update doc status badges while a fetch is running."""
    devices = load_devices()
    device = next((d for d in devices if d.get("id") == device_id), None)
    if not device:
        return HTMLResponse("")
    device["_fetching"] = device_id in _fetching
    device["_cat_label"] = _cat_label(device)
    return templates.TemplateResponse("_doc_status.html", {
        "request": request,
        "device": device,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
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
    return templates.TemplateResponse("_doc_status.html", {
        "request": request,
        "device": device,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
        "update_result": result,
    })


# ── Fallback: provide URL or upload file ───────────────────────────────────

@app.post("/devices/{device_id}/provide-url", response_class=HTMLResponse)
async def provide_url(
    request: Request,
    device_id: str,
    background_tasks: BackgroundTasks,
    doc_type: str = Form("manual"),
    url: str = Form(""),
    file: UploadFile = File(None),
):
    """Allow the user to supply a URL or file when auto-search failed."""
    from paperless_api import PaperlessClient, paperless_available
    from fetch import extract_pdf_meta, update_doc_fields

    data = load_data()
    device = next((dict(d) for d in (data.get("devices") or []) if d.get("id") == device_id), None)
    if not device:
        raise HTTPException(404, "Device not found")

    dest_path = MANUALS_DIR / device_id / f"{doc_type}.pdf"
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if file and file.filename:
        content = await file.read()
        if not content[:4] == b"%PDF":
            raise HTTPException(400, "Uploaded file does not appear to be a PDF.")
        dest_path.write_bytes(content)
        source_url = url.strip() or None
    elif url.strip():
        from fetch import download_pdf
        try:
            http_headers = await download_pdf(url.strip(), dest_path)
        except Exception as exc:
            raise HTTPException(400, f"Could not download from URL: {exc}")
        source_url = url.strip()
    else:
        raise HTTPException(400, "Provide a URL or upload a file.")

    pdf_meta = extract_pdf_meta(dest_path)
    fields: dict = {
        "fetch_status": "uploading",
        "fetch_error": None,
        "pdf_mod_date": pdf_meta.get("pdf_mod_date"),
        "pdf_version": pdf_meta.get("pdf_version"),
        "fetched_at": __import__("datetime").date.today().isoformat(),
    }
    if source_url:
        fields["source_url"] = source_url
    update_doc_fields(device_id, doc_type, fields, INVENTORY_PATH, _yaml_lock)

    paperless_id: int | None = None
    if paperless_available():
        try:
            label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
            cat = device.get("category", {})
            if not isinstance(cat, dict):
                cat = {}
            extra_tags = []
            for level, key in [("cat1", "primary"), ("cat2", "secondary"), ("cat3", "tertiary")]:
                if cat.get(key):
                    extra_tags.append(f"{level}:{slugify(cat[key])}")
            client = PaperlessClient()
            task_id = await client.upload_document(
                dest_path,
                title=f"{device.get('name', device_id)} — {label_map.get(doc_type, doc_type.title())}",
                device_id=device_id,
                manufacturer=device.get("manufacturer", ""),
                doc_type=label_map.get(doc_type, doc_type.title()),
                extra_tags=extra_tags,
            )
            paperless_id = await client.resolve_task(task_id)
        except Exception as exc:
            logger.error("Paperless upload failed for %s/%s: %s", device_id, doc_type, exc)

    final: dict = {"fetch_status": "success" if paperless_id else "uploading"}
    if paperless_id:
        final["paperless_id"] = paperless_id
    update_doc_fields(device_id, doc_type, final, INVENTORY_PATH, _yaml_lock)

    devices = load_devices()
    device_updated = next((d for d in devices if d.get("id") == device_id), None)
    if not device_updated:
        return HTMLResponse("")
    device_updated["_fetching"] = False
    device_updated["_cat_label"] = _cat_label(device_updated)
    return templates.TemplateResponse("_doc_status.html", {
        "request": request,
        "device": device_updated,
        "paperless_url": os.getenv("PAPERLESS_URL", "").rstrip("/"),
    })
