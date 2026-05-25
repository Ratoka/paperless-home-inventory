"""
Document fetch, download, and update-check pipeline.

Flow for a new device:
  search_pdf_url → download_pdf → extract_pdf_meta → (upload via paperless_api)

Flow for update check:
  head_check → if changed: download_pdf → extract_pdf_meta → compare metadata
"""

import asyncio
import datetime
import logging
import re
import threading
from html.parser import HTMLParser
from pathlib import Path

import httpx
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; inventory-manager/1.0; homelab)"


# ── PDF search ─────────────────────────────────────────────────────────────

class _LinkParser(HTMLParser):
    """Minimal HTML parser that collects href values."""
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val:
                    self.links.append(val)


async def search_pdf_url(search_hint: str) -> str | None:
    """
    Search DuckDuckGo HTML for a PDF URL matching search_hint.
    Returns the first direct .pdf link found, or None.
    """
    query = f"{search_hint} filetype:pdf"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": _UA, "Accept": "text/html"},
            )
            resp.raise_for_status()
        parser = _LinkParser()
        parser.feed(resp.text)
        for link in parser.links:
            # Skip DDG-internal links and tracking redirects
            if "duckduckgo.com" in link:
                continue
            if link.lower().endswith(".pdf"):
                return link
            # Some links hide the extension — try the uddg= param
            m = re.search(r"uddg=(https?[^&]+)", link)
            if m:
                decoded = httpx.URL(m.group(1)).path
                if decoded.lower().endswith(".pdf"):
                    return m.group(1)
        return None
    except Exception as exc:
        logger.warning("PDF search failed for %r: %s", search_hint, exc)
        return None


# ── PDF download ───────────────────────────────────────────────────────────

async def download_pdf(url: str, dest_path: Path) -> dict:
    """
    Download a PDF to dest_path.
    Returns a dict of useful HTTP response headers (last_modified, etag).
    Raises httpx.HTTPError or ValueError on failure.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(url, headers={"User-Agent": _UA})
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "pdf" not in ct.lower() and not resp.content[:4] == b"%PDF":
            raise ValueError(f"Response is not a PDF (content-type: {ct!r})")
        dest_path.write_bytes(resp.content)
    return {
        "last_modified": resp.headers.get("last-modified"),
        "etag": resp.headers.get("etag"),
    }


# ── PDF metadata ───────────────────────────────────────────────────────────

def _clean_pdf_date(raw: str | None) -> str | None:
    """Normalise PDF date string (D:YYYYMMDDHHmmSS...) to YYYY-MM-DD."""
    if not raw:
        return None
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Already looks like a date?
    m2 = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m2.group(1) if m2 else None


def _extract_version_string(info: dict) -> str | None:
    """Look for a version string in PDF metadata keywords, subject, or title."""
    for field in ("/Keywords", "/Subject", "/Title"):
        val = str(info.get(field) or "")
        m = re.search(
            r"(v\d+[\.\d]*|rev\.?\s*[a-z0-9]+|\d+(?:st|nd|rd|th)\s+edition|"
            r"version\s+[\d\.]+|release\s+[\d\.]+)",
            val, re.I,
        )
        if m:
            return m.group(0).strip()
    return None


def extract_pdf_meta(path: Path) -> dict:
    """
    Extract version-relevant fields from a PDF using pypdf.
    Returns {pdf_mod_date, pdf_version}; both may be None if unavailable.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        info = reader.metadata or {}
        return {
            "pdf_mod_date": _clean_pdf_date(
                info.get("/ModDate") or info.get("/CreationDate")
            ),
            "pdf_version": _extract_version_string(dict(info)),
        }
    except Exception as exc:
        logger.debug("PDF metadata extraction failed for %s: %s", path, exc)
        return {"pdf_mod_date": None, "pdf_version": None}


# ── Update check ───────────────────────────────────────────────────────────

async def head_check(source_url: str, stored_last_modified: str | None, stored_etag: str | None) -> dict:
    """
    Send a HEAD request and compare against stored HTTP headers.

    Returns a dict:
      status: "unchanged" | "changed" | "no_version_info" | "error"
      reason: human-readable explanation
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.head(source_url, headers={"User-Agent": _UA})
            resp.raise_for_status()

        new_etag = resp.headers.get("etag")
        new_last_mod = resp.headers.get("last-modified")

        # ETag is the most reliable signal
        if stored_etag and new_etag:
            if stored_etag == new_etag:
                return {"status": "unchanged", "reason": "ETag matches"}
            return {"status": "changed", "reason": f"ETag changed: {stored_etag} → {new_etag}"}

        # Fall back to Last-Modified
        if stored_last_modified and new_last_mod:
            if stored_last_modified == new_last_mod:
                return {"status": "unchanged", "reason": "Last-Modified matches"}
            return {"status": "changed", "reason": f"Last-Modified changed: {stored_last_modified} → {new_last_mod}"}

        # Server returned no usable headers
        return {
            "status": "no_version_info",
            "reason": "Server does not provide ETag or Last-Modified headers",
        }

    except Exception as exc:
        return {"status": "error", "reason": str(exc)}


# ── YAML helpers (shared with app.py via lock) ─────────────────────────────

def _ryaml() -> YAML:
    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 120
    return ry


def update_doc_fields(
    device_id: str,
    doc_type: str,
    fields: dict,
    inventory_path: Path,
    lock: threading.Lock,
) -> None:
    """Atomically update fields on a single doc entry in devices.yaml."""
    with lock:
        ry = _ryaml()
        with open(inventory_path) as f:
            data = ry.load(f)
        for device in data.get("devices") or []:
            if device.get("id") == device_id:
                for doc in device.get("docs") or []:
                    if doc.get("type") == doc_type:
                        doc.update(fields)
                        break
                break
        with open(inventory_path, "w") as f:
            ry.dump(data, f)


# ── Main fetch pipeline ────────────────────────────────────────────────────

async def fetch_device_docs(
    device_id: str,
    inventory_path: Path,
    manuals_dir: Path,
    lock: threading.Lock,
) -> None:
    """
    Background task: search, download, and upload all docs for a device.
    Writes status fields back to devices.yaml after each step.
    """
    # Read current device state under lock
    with lock:
        ry = _ryaml()
        with open(inventory_path) as f:
            data = ry.load(f)
        device = next(
            (dict(d) for d in (data.get("devices") or []) if d.get("id") == device_id),
            None,
        )
    if not device:
        return

    # Import here to avoid circular dependency at module level
    from paperless_api import PaperlessClient, paperless_available

    def slugify(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text.strip())
        return re.sub(r"-+", "-", text).strip("-")

    for doc in device.get("docs") or []:
        doc_type = doc.get("type", "manual")

        # ── 1. Find PDF URL ────────────────────────────────────────────────
        update_doc_fields(device_id, doc_type,
                          {"fetch_status": "searching", "fetch_error": None},
                          inventory_path, lock)

        source_url = doc.get("url") or await search_pdf_url(
            doc.get("search_hint")
            or f"{device.get('manufacturer', '')} {device.get('model', '')} {doc_type} manual PDF"
        )

        if not source_url:
            update_doc_fields(device_id, doc_type,
                              {"fetch_status": "not_found",
                               "fetch_error": "No PDF found — provide a URL or upload the file."},
                              inventory_path, lock)
            continue

        # ── 2. Download ────────────────────────────────────────────────────
        update_doc_fields(device_id, doc_type, {"fetch_status": "downloading"}, inventory_path, lock)

        dest_path = manuals_dir / device_id / f"{doc_type}.pdf"
        try:
            http_headers = await download_pdf(source_url, dest_path)
        except Exception as exc:
            update_doc_fields(device_id, doc_type,
                              {"fetch_status": "error", "fetch_error": str(exc)},
                              inventory_path, lock)
            continue

        # ── 3. Extract PDF metadata ────────────────────────────────────────
        pdf_meta = extract_pdf_meta(dest_path)

        # ── 4. Upload to Paperless ─────────────────────────────────────────
        update_doc_fields(device_id, doc_type, {"fetch_status": "uploading"}, inventory_path, lock)

        paperless_id: int | None = None
        if paperless_available():
            try:
                cat = device.get("category", {})
                if not isinstance(cat, dict):
                    cat = {}
                extra_tags = []
                if cat.get("primary"):
                    extra_tags.append(f"cat1:{slugify(cat['primary'])}")
                if cat.get("secondary"):
                    extra_tags.append(f"cat2:{slugify(cat['secondary'])}")
                if cat.get("tertiary"):
                    extra_tags.append(f"cat3:{slugify(cat['tertiary'])}")

                label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
                title = f"{device.get('name', device_id)} — {label_map.get(doc_type, doc_type.title())}"

                client = PaperlessClient()
                task_id = await client.upload_document(
                    dest_path,
                    title=title,
                    device_id=device_id,
                    manufacturer=device.get("manufacturer", ""),
                    doc_type=label_map.get(doc_type, doc_type.title()),
                    extra_tags=extra_tags,
                )
                paperless_id = await client.resolve_task(task_id)
            except Exception as exc:
                logger.error("Paperless upload failed for %s/%s: %s", device_id, doc_type, exc)

        # ── 5. Write final status ──────────────────────────────────────────
        final: dict = {
            "fetch_status": "success",
            "fetch_error": None,
            "source_url": source_url,
            "last_modified": http_headers.get("last_modified"),
            "etag": http_headers.get("etag"),
            "pdf_mod_date": pdf_meta.get("pdf_mod_date"),
            "pdf_version": pdf_meta.get("pdf_version"),
            "fetched_at": datetime.date.today().isoformat(),
        }
        if paperless_id:
            final["paperless_id"] = paperless_id
        update_doc_fields(device_id, doc_type, final, inventory_path, lock)

    logger.info("Fetch complete for device %s", device_id)


# ── Update pipeline ────────────────────────────────────────────────────────

async def check_and_apply_update(
    device_id: str,
    doc_type: str,
    note: str,
    inventory_path: Path,
    manuals_dir: Path,
    lock: threading.Lock,
) -> dict:
    """
    Check for an update and, if found, download + replace in Paperless.

    Returns a summary dict for display:
      {result: "unchanged"|"updated"|"no_version_info"|"metadata_match"|"error", detail: str}
    """
    from paperless_api import PaperlessClient, paperless_available

    def slugify(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text.strip())
        return re.sub(r"-+", "-", text).strip("-")

    # Read current doc state
    with lock:
        ry = _ryaml()
        with open(inventory_path) as f:
            data = ry.load(f)
        device = next(
            (dict(d) for d in (data.get("devices") or []) if d.get("id") == device_id),
            None,
        )
    if not device:
        return {"result": "error", "detail": "Device not found"}

    doc = next((dict(d) for d in (device.get("docs") or []) if d.get("type") == doc_type), None)
    if not doc:
        return {"result": "error", "detail": "Doc type not found"}

    source_url = doc.get("source_url") or doc.get("url")
    if not source_url:
        return {"result": "error", "detail": "No source URL stored — fetch the document first"}

    # ── HEAD check ─────────────────────────────────────────────────────────
    check = await head_check(source_url, doc.get("last_modified"), doc.get("etag"))

    if check["status"] == "unchanged":
        return {"result": "unchanged", "detail": check["reason"]}

    if check["status"] == "no_version_info":
        return {"result": "no_version_info", "detail": check["reason"]}

    if check["status"] == "error":
        return {"result": "error", "detail": check["reason"]}

    # ── Headers changed — download and compare PDF metadata ────────────────
    dest_path = manuals_dir / device_id / f"{doc_type}-update.pdf"
    try:
        new_http = await download_pdf(source_url, dest_path)
    except Exception as exc:
        return {"result": "error", "detail": f"Download failed: {exc}"}

    new_meta = extract_pdf_meta(dest_path)

    old_mod = doc.get("pdf_mod_date")
    old_ver = doc.get("pdf_version")
    new_mod = new_meta.get("pdf_mod_date")
    new_ver = new_meta.get("pdf_version")

    # If both have dates/versions and they match → treat as republish, skip
    if (old_mod and new_mod and old_mod == new_mod) and (old_ver == new_ver):
        dest_path.unlink(missing_ok=True)
        return {
            "result": "metadata_match",
            "detail": (
                f"File changed on server but PDF metadata is identical "
                f"(version: {old_ver or '—'}, date: {old_mod}). "
                f"Likely a republish — no update applied."
            ),
        }

    # ── Real update — move new file, upload, delete old ────────────────────
    final_path = manuals_dir / device_id / f"{doc_type}.pdf"
    dest_path.replace(final_path)

    label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
    date_str = datetime.date.today().isoformat()
    title = f"{device.get('name', device_id)} — {label_map.get(doc_type, doc_type.title())} (updated {date_str})"
    if note.strip():
        title += f" [{note.strip()}]"

    new_paperless_id: int | None = None
    if paperless_available():
        try:
            cat = device.get("category", {})
            if not isinstance(cat, dict):
                cat = {}
            extra_tags = []
            if cat.get("primary"):
                extra_tags.append(f"cat1:{slugify(cat['primary'])}")
            if cat.get("secondary"):
                extra_tags.append(f"cat2:{slugify(cat['secondary'])}")
            if cat.get("tertiary"):
                extra_tags.append(f"cat3:{slugify(cat['tertiary'])}")

            client = PaperlessClient()
            task_id = await client.upload_document(
                final_path,
                title=title,
                device_id=device_id,
                manufacturer=device.get("manufacturer", ""),
                doc_type=label_map.get(doc_type, doc_type.title()),
                extra_tags=extra_tags,
            )
            new_paperless_id = await client.resolve_task(task_id)

            # Delete old document
            old_paperless_id = doc.get("paperless_id")
            if old_paperless_id:
                async with client._client() as c:
                    await c.delete(f"/api/documents/{old_paperless_id}/")
        except Exception as exc:
            logger.error("Paperless update failed for %s/%s: %s", device_id, doc_type, exc)

    # ── Write updated metadata ──────────────────────────────────────────────
    final_fields: dict = {
        "fetch_status": "success",
        "fetch_error": None,
        "source_url": source_url,
        "last_modified": new_http.get("last_modified"),
        "etag": new_http.get("etag"),
        "pdf_mod_date": new_mod,
        "pdf_version": new_ver,
        "fetched_at": date_str,
    }
    if new_paperless_id:
        final_fields["paperless_id"] = new_paperless_id
    update_doc_fields(device_id, doc_type, final_fields, inventory_path, lock)

    ver_str = f"{old_ver or '—'} → {new_ver or '—'}"
    date_str2 = f"{old_mod or '—'} → {new_mod or '—'}"
    return {
        "result": "updated",
        "detail": f"Version: {ver_str}  |  Date: {date_str2}",
    }
