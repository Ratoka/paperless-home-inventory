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
from typing import Any
from urllib.parse import unquote as _urlunquote, urljoin as _urljoin

import httpx
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; inventory-manager/1.0; homelab)"


def pdf_filename(device_name: str, doc_type: str) -> str:
    """Return a filesystem-safe PDF filename: <device_name>_<doc_type>.pdf"""
    def _safe(s: str) -> str:
        s = s.strip()
        s = re.sub(r'[\\/:*?"<>|]', "", s)   # strip chars illegal on Windows/FAT
        s = re.sub(r"\s+", "_", s)
        return s or "unknown"
    return f"{_safe(device_name)}_{_safe(doc_type)}.pdf"


# ── PDF search ─────────────────────────────────────────────────────────────

# Manual-aggregator sites that host third-party scans, require auth, or reliably
# return 403/paywalls when the actual PDF is fetched.  URLs from these domains
# are discarded before any download is attempted.
_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "manuals.plus",
    "manualslib.com",
    "manualzz.com",
    "manuall.com",
    "manuall.nl",
    "manua.ls",
    "manualsnet.com",
    "manualsdir.com",
    "usermanual.wiki",
    "user-manuals.com",
    "lastmanuals.com",
    "retrevo.com",
    "elektrotanya.com",
    "diagramasde.com",
    "diagramas.diagramasde.com",
    "datasheet.rs",
    "datasheet.directory",
    "manualmachine.com",
    "manualsbase.com",
    "calameo.com",
    "scribd.com",
    "slideshare.net",
    "docplayer.net",
    "yumpu.com",
    "issuu.com",
    "pdf.manualslib.com",
})

# Operator string for Google/DDG to exclude blocked domains from results
_BLOCK_OPERATORS: str = " ".join(f"-site:{d}" for d in sorted(_BLOCKED_DOMAINS))


def _is_blocked(url: str) -> bool:
    """Return True if the URL's hostname matches any blocked domain."""
    try:
        host = httpx.URL(url).host.lower()
        return any(host == d or host.endswith("." + d) for d in _BLOCKED_DOMAINS)
    except Exception:
        return False

class _LinkParser(HTMLParser):
    """Minimal HTML parser that collects href values from <a> tags."""
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val:
                    self.links.append(val)


class _ContextLinkParser(HTMLParser):
    """
    Collect all <a href> links alongside the text content of their containing block element.
    Block elements (tr, div, p, li, td, h1-h4) reset the text buffer, so each link is
    paired with only the text from its immediate surrounding context.
    """
    _BLOCK_TAGS: frozenset[str] = frozenset(
        {"div", "p", "tr", "li", "section", "article", "td", "th", "h1", "h2", "h3", "h4"}
    )

    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self._block_text: str = ""
        self._pending_links: list[str] = []

    def _flush(self):
        for link in self._pending_links:
            self.items.append({"href": link, "context": self._block_text.strip()})
        self._pending_links.clear()
        self._block_text = ""

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK_TAGS:
            self._flush()
        if tag == "a":
            for name, val in attrs:
                if name == "href" and val:
                    self._pending_links.append(val)

    def handle_endtag(self, tag):
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        self._block_text += data

    def finalize(self):
        self._flush()


def _extract_ddg_url(link: str) -> str | None:
    """
    Resolve one raw href from a DDG HTML result page to an actual URL.

    DDG wraps every result in a tracking redirect whose real URL sits in the
    `uddg` query parameter — URL-encoded.  Two forms appear in practice:
      • /l/?uddg=https%3A%2F%2F...        (relative path on duckduckgo.com)
      • https://duckduckgo.com/l/?uddg=… (absolute, same domain)
    Both must be decoded; both were previously skipped or mis-parsed.
    Plain https:// hrefs (rare in DDG HTML) are returned as-is.
    """
    m = re.search(r"[?&]uddg=(https?[^&]+)", link)
    if m:
        return _urlunquote(m.group(1))
    if link.startswith("https://") or link.startswith("http://"):
        if "duckduckgo.com" not in link:
            return link
    return None


async def _ddg_candidates(query: str) -> list[str]:
    """POST to DuckDuckGo HTML search and return resolved, unblocked result URLs in order."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": f"{query} {_BLOCK_OPERATORS}"},
                headers={"User-Agent": _UA, "Accept": "text/html"},
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.debug("DDG request failed for %r: %s", query, exc)
        return []

    parser = _LinkParser()
    parser.feed(resp.text)

    seen: set[str] = set()
    results: list[str] = []
    for link in parser.links:
        url = _extract_ddg_url(link)
        if url and url not in seen and not _is_blocked(url):
            seen.add(url)
            results.append(url)
    return results


_GOOGLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _google_candidates(query: str) -> list[str]:
    """
    GET Google search results and return unblocked URLs in order.
    Returns an empty list (not an error) if Google blocks the request.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://www.google.com/search",
                params={"q": f"{query} {_BLOCK_OPERATORS}", "num": "10", "hl": "en"},
                headers={
                    "User-Agent": _GOOGLE_UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
    except Exception as exc:
        logger.debug("Google request failed for %r: %s", query, exc)
        return []

    if resp.status_code != 200 or "detected unusual traffic" in resp.text:
        logger.debug("Google search unavailable for %r (status %s)", query, resp.status_code)
        return []

    seen: set[str] = set()
    results: list[str] = []

    # Format 1 — Google redirect links: /url?q=<encoded-url>&sa=...
    for m in re.finditer(r"/url\?q=(https?[^&\"' >]+)", resp.text):
        url = _urlunquote(m.group(1))
        if "google.com" not in url and url not in seen and not _is_blocked(url):
            seen.add(url)
            results.append(url)

    # Format 2 — direct href links that appear in some Google layouts
    parser = _LinkParser()
    parser.feed(resp.text)
    for link in parser.links:
        if (link.startswith("http") and "google.com" not in link
                and link not in seen and not _is_blocked(link)):
            seen.add(link)
            results.append(link)

    return results


async def _first_pdf(
    candidates: list[str],
    max_head: int = 8,
    exclude: set[str] | None = None,
) -> str | None:
    """
    Return the first URL in candidates that is confirmed to be a PDF.
    Checks file extension first (free), then HEAD-checks the remainder.
    Blocked domains and URLs in `exclude` are skipped.
    """
    head_queue: list[str] = []
    for url in candidates:
        if _is_blocked(url):
            logger.debug("Skipping blocked domain: %s", url)
            continue
        if exclude and url in exclude:
            logger.debug("Skipping previously rejected URL: %s", url)
            continue
        try:
            if httpx.URL(url).path.lower().endswith(".pdf"):
                return url
        except Exception:
            pass
        head_queue.append(url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
        for url in head_queue[:max_head]:
            if exclude and url in exclude:
                continue
            try:
                head = await client.head(url, headers={"User-Agent": _UA})
                ct = head.headers.get("content-type", "")
                if "pdf" in ct.lower():
                    return url
            except Exception:
                continue
    return None


async def search_pdf_url(
    search_hint: str,
    *,
    manufacturer: str = "",
    model: str = "",
    log_fn=None,
    rejected_urls: set[str] | None = None,
) -> tuple[str, str] | None:
    """
    Multi-stage PDF search — free/unlimited stages first, API providers as deep-search
    fallback. Stops at the first confirmed PDF URL.

    Returns (url, stage_label) so callers can log which method succeeded, or None.
    log_fn, if provided, is called with a short status string for each stage attempted.

    Free stages (no API key required, always run):
      1. Manufacturer direct    — DDG site:{brand}.com scoped search
      2. Archive.org CDX        — Wayback Machine PDF index for manufacturer URLs
      3. Google HTML scraping   — fragile but high-quality index
      4. DDG filetype:pdf       — stable HTML endpoint, weaker indexing
      5. DDG broad              — drops filetype filter

    API stages (configured in Settings UI, used when free stages find nothing):
      6. Brave Search API       — ~1 000 req/month free
      7. Google Custom Search   — 100 req/day free
      8. Bing Web Search API    — 1 000 req/month free tier
    """
    import config as _cfg

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    hint_pdf = f"{search_hint} filetype:pdf"

    # ── Stage 1: Manufacturer direct (site: search, zero API cost) ────────────
    if manufacturer and model:
        _log("manufacturer direct — searching…")
        candidates = await _manufacturer_direct_candidates(manufacturer, model)
        url = await _first_pdf(candidates, max_head=6, exclude=rejected_urls)
        if url:
            logger.info("PDF found via manufacturer-direct for %r: %s", search_hint, url)
            _log("manufacturer direct — ✓ found")
            return url, "manufacturer direct"
        _log("manufacturer direct — no results")
    else:
        _log("manufacturer direct — skipped (no manufacturer/model on device)")

    # ── Stage 2: Archive.org CDX ──────────────────────────────────────────────
    if manufacturer and model:
        _log("Archive.org CDX — searching…")
        candidates = await _archive_org_candidates(manufacturer, model)
        url = await _first_pdf(candidates, max_head=6, exclude=rejected_urls)
        if url:
            logger.info("PDF found via archive.org CDX for %r: %s", search_hint, url)
            _log("Archive.org CDX — ✓ found")
            return url, "Archive.org CDX"
        _log("Archive.org CDX — no results")
    else:
        _log("Archive.org CDX — skipped (no manufacturer/model on device)")

    # ── Stage 3: Google HTML scraping ────────────────────────────────────────
    _log("Google scraping — searching…")
    candidates = await _google_candidates(hint_pdf)
    url = await _first_pdf(candidates, exclude=rejected_urls)
    if url:
        logger.info("PDF found via Google scrape for %r: %s", search_hint, url)
        _log("Google scraping — ✓ found")
        return url, "Google scraping"
    _log("Google scraping — no results")

    # ── Stage 4: DDG filetype:pdf ────────────────────────────────────────────
    _log("DuckDuckGo (filetype:pdf) — searching…")
    candidates = await _ddg_candidates(hint_pdf)
    url = await _first_pdf(candidates, exclude=rejected_urls)
    if url:
        logger.info("PDF found via DDG (filetype) for %r: %s", search_hint, url)
        _log("DuckDuckGo (filetype:pdf) — ✓ found")
        return url, "DuckDuckGo (filetype:pdf)"
    _log("DuckDuckGo (filetype:pdf) — no results")

    # ── Stage 5: DDG broad ───────────────────────────────────────────────────
    _log("DuckDuckGo (broad) — searching…")
    candidates = await _ddg_candidates(search_hint)
    url = await _first_pdf(candidates, max_head=10, exclude=rejected_urls)
    if url:
        logger.info("PDF found via DDG (broad) for %r: %s", search_hint, url)
        _log("DuckDuckGo (broad) — ✓ found")
        return url, "DuckDuckGo (broad)"
    _log("DuckDuckGo (broad) — no results")

    # ── Stage 6: Brave ────────────────────────────────────────────────────────
    brave_key = _cfg.get_api_key("brave")
    if brave_key:
        if _cfg.is_over_free_limit("brave") and not _cfg.allow_paid("brave"):
            logger.info("Brave: free limit reached and paid usage disabled — skipping")
            _log("Brave Search API — skipped (monthly limit reached)")
        else:
            _log("Brave Search API — searching…")
            candidates = await _brave_candidates(hint_pdf, brave_key)
            url = await _first_pdf(candidates, exclude=rejected_urls)
            if url:
                logger.info("PDF found via Brave for %r: %s", search_hint, url)
                _log("Brave Search API — ✓ found")
                return url, "Brave Search API"
            _log("Brave Search API — no results")

    # ── Stage 7: Google Custom Search ─────────────────────────────────────────
    gkey = _cfg.get_api_key("google_cse")
    gcx  = _cfg.get_field("google_cse", "cx")
    if gkey and gcx:
        if _cfg.is_over_free_limit("google_cse") and not _cfg.allow_paid("google_cse"):
            logger.info("Google CSE: free limit reached and paid usage disabled — skipping")
            _log("Google Custom Search — skipped (daily limit reached)")
        else:
            _log("Google Custom Search — searching…")
            candidates = await _google_cse_candidates(hint_pdf, gkey, gcx)
            url = await _first_pdf(candidates, exclude=rejected_urls)
            if url:
                _cfg.record_use("google_cse")
                logger.info("PDF found via Google CSE for %r: %s", search_hint, url)
                _log("Google Custom Search — ✓ found")
                return url, "Google Custom Search"
            _log("Google Custom Search — no results")

    # ── Stage 8: Bing ─────────────────────────────────────────────────────────
    bing_key = _cfg.get_api_key("bing")
    if bing_key:
        if _cfg.is_over_free_limit("bing") and not _cfg.allow_paid("bing"):
            logger.info("Bing: free limit reached and paid usage disabled — skipping")
            _log("Bing Search API — skipped (monthly limit reached)")
        else:
            _log("Bing Search API — searching…")
            candidates = await _bing_candidates(hint_pdf, bing_key)
            url = await _first_pdf(candidates, exclude=rejected_urls)
            if url:
                _cfg.record_use("bing")
                logger.info("Bing: PDF found for %r: %s", search_hint, url)
                _log("Bing Search API — ✓ found")
                return url, "Bing Search API"
            _log("Bing Search API — no results")

    logger.warning("All search stages exhausted for %r", search_hint)
    return None


# ── API-backed search providers ────────────────────────────────────────────

def _parse_brave_rate_limit_headers(headers) -> tuple[int | None, str | None]:
    """
    Extract monthly remaining + reset date from Brave's X-RateLimit-* headers.

    Brave encodes multiple windows in a single header following the IETF draft
    RateLimit spec, e.g.:
        X-RateLimit-Policy:    1;w=1, 1000;w=2592000
        X-RateLimit-Remaining: 1, 847
        X-RateLimit-Reset:     0, 1814400

    We find the window with the largest w= value (the monthly window) and return
    its remaining count and the computed reset date.  Returns (None, None) if the
    headers are missing or unparsable.

    IMPORTANT: the positional index found from Policy is used to index into
    Remaining/Reset, so all three headers must list windows in the same order.
    """
    import datetime as _dt
    policy    = headers.get("x-ratelimit-policy", "")
    remaining = headers.get("x-ratelimit-remaining", "")
    reset_raw = headers.get("x-ratelimit-reset", "")

    # Always log raw headers at INFO so they appear in container logs for diagnosis.
    logger.info(
        "Brave rate-limit raw headers: policy=%r remaining=%r reset=%r",
        policy, remaining, reset_raw,
    )

    if not policy or not remaining:
        logger.info("Brave rate-limit: policy/remaining headers absent — local counter will be used")
        return None, None

    windows   = [w.strip() for w in policy.split(",")]
    rem_parts = [r.strip() for r in remaining.split(",")]
    rst_parts = [r.strip() for r in reset_raw.split(",")]

    # Identify the longest window that represents a billing period (≥ 1 day).
    # Per-second or per-minute burst windows return remaining=0 immediately after
    # each call, which would falsely appear as "quota exhausted".
    _ONE_DAY_SECS = 86_400
    best_idx, best_w = -1, 0
    for i, seg in enumerate(windows):
        m = re.match(r"\d+;w=(\d+)", seg)
        if m:
            w = int(m.group(1))
            if w >= _ONE_DAY_SECS and w > best_w:
                best_w, best_idx = w, i

    if best_idx < 0:
        logger.info(
            "Brave rate-limit: no billing-period window (≥1 day) in policy %r — "
            "local counter will be used",
            policy,
        )
        return None, None

    if best_idx >= len(rem_parts):
        logger.warning(
            "Brave rate-limit: window index %d out of range for remaining parts (len=%d) — "
            "header count mismatch, local counter will be used",
            best_idx, len(rem_parts),
        )
        return None, None

    try:
        rem_val = int(rem_parts[best_idx].split(";")[0])
    except ValueError:
        logger.warning(
            "Brave rate-limit: could not parse remaining value %r — local counter will be used",
            rem_parts[best_idx],
        )
        return None, None

    # Sanity-check: never trust a server-reported remaining=0 — a genuine
    # monthly exhaustion would cause API calls to fail with 4xx, making it
    # immediately obvious.  A zero here is almost always a mis-parsed burst
    # window or an API quirk.  Fall back to the local counter instead.
    if rem_val == 0:
        logger.warning(
            "Brave rate-limit: server reports remaining=0 for w=%ds window — "
            "ignoring (likely burst-window artifact); local counter will be used",
            best_w,
        )
        return None, None

    reset_date: str | None = None
    if best_idx < len(rst_parts):
        try:
            reset_secs = int(rst_parts[best_idx].split(";")[0])
            reset_date = (_dt.datetime.now() + _dt.timedelta(seconds=reset_secs)).date().isoformat()
        except ValueError:
            logger.warning(
                "Brave rate-limit: could not parse reset value %r — reset date unknown",
                rst_parts[best_idx],
            )

    logger.info(
        "Brave rate-limit: w=%ds window → remaining=%d, resets %s",
        best_w, rem_val, reset_date,
    )
    return rem_val, reset_date


async def _brave_candidates(query: str, api_key: str) -> list[str]:
    """Brave Search JSON API — returns unblocked result URLs."""
    import config as _cfg
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 10},
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.debug("Brave search failed for %r: %s", query, exc)
        return []

    # Count every API call against the local quota tracker (quota is consumed
    # regardless of whether results are found).
    _cfg.record_use("brave")

    # Prefer server-reported quota — more accurate than local counting.
    remaining, reset_date = _parse_brave_rate_limit_headers(resp.headers)
    if remaining is not None and reset_date:
        _cfg.store_rate_limit_info("brave", remaining, reset_date)
        logger.info("Brave rate limit: %d remaining, resets %s", remaining, reset_date)
    else:
        logger.info(
            "Brave rate limit headers not parseable — using local count (%d this month)",
            _cfg.current_month_usage("brave"),
        )

    results = []
    for item in body.get("web", {}).get("results", []):
        url = item.get("url", "")
        if url and not _is_blocked(url):
            results.append(url)
    return results


async def brave_sync_usage(api_key: str) -> dict:
    """
    Make a minimal Brave API call solely to read the current rate-limit headers.
    Consumes 1 API call.  Updates config.yaml and returns a status dict:
      {"ok": True,  "remaining": 847, "reset_date": "2026-06-01"}
      {"ok": False, "error": "reason"}
    """
    import config as _cfg

    # Clear stale cached data unconditionally so that even if the API call fails
    # the UI switches to the local counter rather than showing a wrong cached value.
    _cfg.clear_rate_limit_info("brave")
    logger.info("Brave usage sync: cleared stale cached data, calling API…")

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": "manual pdf", "count": 1},
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                },
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Brave usage sync: API call failed — %s", exc)
        return {"ok": False, "error": str(exc)}

    _cfg.record_use("brave")

    remaining, reset_date = _parse_brave_rate_limit_headers(resp.headers)
    if remaining is not None and reset_date:
        _cfg.store_rate_limit_info("brave", remaining, reset_date)
        logger.info("Brave usage sync: %d remaining, resets %s", remaining, reset_date)
        return {"ok": True, "remaining": remaining, "reset_date": reset_date}

    logger.info(
        "Brave usage sync: no monthly window in headers — local counter "
        "(%d this month) will be used",
        _cfg.current_month_usage("brave"),
    )
    return {"ok": True, "remaining": None, "reset_date": None}


async def _google_cse_candidates(query: str, api_key: str, cx: str) -> list[str]:
    """Google Custom Search JSON API — requires a CX (Search Engine ID)."""
    if not cx:
        return []
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"q": query, "key": api_key, "cx": cx, "num": 10},
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.debug("Google CSE failed for %r: %s", query, exc)
        return []
    results = []
    for item in body.get("items", []):
        url = item.get("link", "")
        if url and not _is_blocked(url):
            results.append(url)
    return results


async def _bing_candidates(query: str, api_key: str) -> list[str]:
    """Bing Web Search API (Azure Cognitive Services)."""
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                "https://api.bing.microsoft.com/v7.0/search",
                params={"q": query, "count": 10, "responseFilter": "Webpages"},
                headers={"Ocp-Apim-Subscription-Key": api_key},
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.debug("Bing search failed for %r: %s", query, exc)
        return []
    results = []
    for item in body.get("webPages", {}).get("value", []):
        url = item.get("url", "")
        if url and not _is_blocked(url):
            results.append(url)
    return results


async def _manufacturer_direct_candidates(manufacturer: str, model: str) -> list[str]:
    """
    Site-scoped DDG search on the manufacturer's guessed domain.
    Costs zero API calls and produces much more targeted results than a
    generic search when the manufacturer's domain can be guessed.
    e.g. manufacturer='Onkyo', model='TX-NR7100' → site:onkyo.com TX-NR7100 manual pdf
    """
    brand_slug = re.sub(r"[^a-z0-9]", "", manufacturer.lower())
    if not brand_slug or not model:
        return []
    domain = f"{brand_slug}.com"
    query = f"site:{domain} {model} manual pdf"
    return await _ddg_candidates(query)


async def _archive_org_candidates(manufacturer: str, model: str) -> list[str]:
    """
    Query the Wayback Machine CDX API for PDFs matching manufacturer+model.
    Returns original URLs that were once served by the manufacturer's domain —
    useful for finding the correct URL pattern even if the page has moved.
    """
    brand_slug = re.sub(r"[^a-z0-9]", "", manufacturer.lower())
    model_slug = re.sub(r"[^a-z0-9]", "", model.lower())
    if not brand_slug or not model_slug:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": f"*.{brand_slug}.com/*{model_slug}*",
                    "output": "json",
                    "fl": "original",
                    "filter": "mimetype:application/pdf",
                    "limit": "8",
                    "collapse": "original",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        logger.debug("Archive.org CDX failed for %s/%s: %s", manufacturer, model, exc)
        return []
    # rows[0] is the header row ["original"], skip it
    results = []
    for row in (rows[1:] if len(rows) > 1 else []):
        if row and not _is_blocked(row[0]):
            results.append(row[0])
    return results


# ── Vendor index scraper ───────────────────────────────────────────────────

async def _scrape_vendor_index(index_url: str, model: str) -> str | None:
    """
    Fetch a vendor's manual index page and return a PDF URL for the given model.

    Matching strategy (in order):
      1. Links whose surrounding block text contains the normalized model number.
      2. Links whose own URL contains the normalized model number (catches short
         redirect URLs like https://inov.li/vzm31snPDF for model VZM31-SN).

    Relative hrefs are resolved against index_url.  Each candidate is HEAD-checked
    (with redirect following) to confirm it serves a PDF.
    """
    if not model:
        return None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(index_url, headers={"User-Agent": _UA, "Accept": "text/html"})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.debug("Vendor index fetch failed for %r: %s", index_url, exc)
        return None

    model_norm = re.sub(r"[^a-z0-9]", "", model.lower())
    if not model_norm:
        return None

    parser = _ContextLinkParser()
    parser.feed(html)
    parser.finalize()

    seen: set[str] = set()
    candidates: list[str] = []

    def _abs(href: str) -> str:
        return _urljoin(index_url, href)

    # Priority 1: link in same block as the model text
    for item in parser.items:
        ctx_norm = re.sub(r"[^a-z0-9]", "", item["context"].lower())
        if model_norm in ctx_norm:
            url = _abs(item["href"])
            if url not in seen and not _is_blocked(url):
                candidates.append(url)
                seen.add(url)

    # Priority 2: model appears in the link URL itself (e.g. inov.li/vzm31snPDF)
    for item in parser.items:
        url = _abs(item["href"])
        link_norm = re.sub(r"[^a-z0-9]", "", url.lower())
        if model_norm in link_norm and url not in seen and not _is_blocked(url):
            candidates.append(url)
            seen.add(url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        for url in candidates:
            try:
                if httpx.URL(url).path.lower().endswith(".pdf"):
                    logger.debug("Vendor index PDF (extension match): %s", url)
                    return url
                head = await client.head(url, headers={"User-Agent": _UA})
                final_url = str(head.url)
                ct = head.headers.get("content-type", "")
                if "pdf" in ct.lower() or final_url.lower().endswith(".pdf"):
                    logger.debug("Vendor index PDF (HEAD check): %s → %s", url, final_url)
                    return final_url
            except Exception as exc:
                logger.debug("HEAD failed for vendor candidate %s: %s", url, exc)
                continue

    return None


# ── PDF download ───────────────────────────────────────────────────────────

def _strip_external_resources(html: str) -> str:
    """Remove script, noscript, and link tags so WeasyPrint doesn't fetch CDN resources."""
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<noscript\b[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<link\b[^>]*/?\s*>', '', html, flags=re.IGNORECASE)
    return html


def _html_to_pdf(html: str, base_url: str, dest_path: Path) -> None:
    """Render an HTML page to PDF using WeasyPrint (sync, run via to_thread)."""
    import concurrent.futures
    from weasyprint import HTML
    from weasyprint.urls import default_url_fetcher

    clean = _strip_external_resources(html)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def _fetcher(url: str):
        future = pool.submit(default_url_fetcher, url)
        try:
            return future.result(timeout=5)
        except Exception:
            return {"string": b"", "mime_type": "text/plain"}

    try:
        HTML(string=clean, base_url=base_url, url_fetcher=_fetcher).write_pdf(str(dest_path))
    finally:
        pool.shutdown(wait=False)

    size = dest_path.stat().st_size
    if size < 5_000:
        raise ValueError(f"HTML-to-PDF conversion produced a suspiciously small file ({size} bytes)")


async def download_pdf(url: str, dest_path: Path) -> dict:
    """
    Download a PDF (or convert an HTML page to PDF) and save to dest_path.
    Returns HTTP headers plus a converted_from_html flag.
    Raises httpx.HTTPError or ValueError on failure.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(
            url,
            headers={"User-Agent": _UA, "Accept": "application/pdf, text/html, */*;q=0.8"},
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")

        if "pdf" in ct.lower() or resp.content[:4] == b"%PDF":
            if len(resp.content) < 10_000:
                raise ValueError(f"Downloaded file is too small ({len(resp.content)} bytes) — likely not a real manual")
            dest_path.write_bytes(resp.content)
            converted_from_html = False
        elif "html" in ct.lower():
            await asyncio.to_thread(_html_to_pdf, resp.text, url, dest_path)
            converted_from_html = True
        else:
            raise ValueError(f"Response is not a PDF or HTML page (content-type: {ct!r})")

    return {
        "last_modified": resp.headers.get("last-modified"),
        "etag": resp.headers.get("etag"),
        "converted_from_html": converted_from_html,
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
    Returns {pdf_mod_date, pdf_version, pdf_pages, pdf_has_text}; values may be None.
    pdf_has_text is True if any page yields extractable text.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        info = reader.metadata or {}
        page_count = len(reader.pages)
        has_text = any(page.extract_text().strip() for page in reader.pages)
        return {
            "pdf_mod_date": _clean_pdf_date(
                info.get("/ModDate") or info.get("/CreationDate")
            ),
            "pdf_version": _extract_version_string(dict(info)),
            "pdf_pages": page_count,
            "pdf_has_text": has_text,
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

def _tlog(task: Any, msg: str) -> None:
    """Append a timestamped line to an AppTask if one was provided."""
    if task is None:
        return
    import tasks as _t
    _t.task_log(task, msg)


async def fetch_device_docs(
    device_id: str,
    inventory_path: Path,
    manuals_dir: Path,
    lock: threading.Lock,
    app_task: Any = None,
) -> None:
    """
    Background task: search, download, and upload all docs for a device.
    Writes status fields back to devices.yaml after each step.
    If app_task is provided, progress is logged to it.
    """
    import tasks as _t

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
        if app_task:
            _t.task_done(app_task, False, "Device not found in inventory")
        return

    from paperless_api import PaperlessClient, paperless_available

    def slugify(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text.strip())
        return re.sub(r"-+", "-", text).strip("-")

    docs = device.get("docs") or []
    errors: list[str] = []

    for doc in docs:
        doc_type = doc.get("type", "manual")
        label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
        doc_label = doc.get("label") or label_map.get(doc_type, doc_type.replace("-", " ").title())

        # ── 1. Find PDF URL ────────────────────────────────────────────────
        update_doc_fields(device_id, doc_type,
                          {"fetch_status": "searching", "fetch_error": None},
                          inventory_path, lock)

        dest_path: Path | None = None
        http_headers: dict = {}
        pdf_meta: dict = {}

        if doc.get("url"):
            source_url = doc["url"]
            _tlog(app_task, f"[{doc_label}] Using provided URL")
        else:
            source_url = None

            # Try vendor manual index page first (e.g. Inovelli help.inovelli.com page)
            vendor_index = device.get("vendor_index_url") or doc.get("vendor_index_url")
            if vendor_index and device.get("model"):
                _tlog(app_task, f"[{doc_label}] Checking vendor index…")
                source_url = await _scrape_vendor_index(vendor_index, device["model"])
                if source_url:
                    _tlog(app_task, f"[{doc_label}] Found via vendor index: {source_url[:70]}")

            if not source_url:
                if doc_type == "manual":
                    default_hint = f"{device.get('manufacturer', '')} {device.get('model', '')} user manual PDF"
                else:
                    default_hint = f"{device.get('manufacturer', '')} {device.get('model', '')} {doc_type} PDF"
                hint = doc.get("search_hint") or default_hint
                _tlog(app_task, f"[{doc_label}] Searching: {hint[:60]}")

                rejected_urls: set[str] = set()
                found_via = ""
                for _attempt in range(9):  # max retries = number of search stages
                    result = await search_pdf_url(
                        hint,
                        manufacturer=device.get("manufacturer", ""),
                        model=device.get("model", ""),
                        log_fn=lambda msg: _tlog(app_task, f"[{doc_label}]   {msg}"),
                        rejected_urls=rejected_urls or None,
                    )
                    if not result:
                        break
                    source_url, found_via = result
                    _tlog(app_task, f"[{doc_label}] Found via {found_via}: {source_url[:70]}")

                    # ── Download and validate immediately ──────────────────
                    update_doc_fields(device_id, doc_type, {"fetch_status": "downloading"},
                                      inventory_path, lock)
                    _tlog(app_task, f"[{doc_label}] Downloading…")
                    dest_path = manuals_dir / device_id / pdf_filename(
                        device.get("name", device_id), doc_type)
                    try:
                        http_headers = await download_pdf(source_url, dest_path)
                    except Exception as exc:
                        _tlog(app_task, f"[{doc_label}] Download failed: {exc} — trying next source")
                        rejected_urls.add(source_url)
                        source_url = None
                        update_doc_fields(device_id, doc_type, {"fetch_status": "searching"},
                                          inventory_path, lock)
                        continue

                    pdf_meta = extract_pdf_meta(dest_path)
                    pages = pdf_meta.get("pdf_pages", "?")
                    _tlog(app_task, f"[{doc_label}] Downloaded — {pages} page(s)")

                    if pdf_meta.get("pdf_pages") == 1 and not http_headers.get("converted_from_html"):
                        _tlog(app_task,
                              f"[{doc_label}] Rejected — 1-page PDF (cover sheet, not a real manual)")
                        _tlog(app_task, f"[{doc_label}]   Rejected URL: {source_url[:80]}")
                        _tlog(app_task, f"[{doc_label}]   Retrying search with this URL excluded…")
                        rejected_urls.add(source_url)
                        source_url = None
                        update_doc_fields(device_id, doc_type, {"fetch_status": "searching"},
                                          inventory_path, lock)
                        continue

                    # Valid PDF found — break out of retry loop
                    break
                # end retry loop

        if not source_url:
            msg = "No PDF found — provide a URL or upload the file manually."
            update_doc_fields(device_id, doc_type,
                              {"fetch_status": "not_found", "fetch_error": msg},
                              inventory_path, lock)
            if rejected_urls:
                _tlog(app_task,
                      f"[{doc_label}] Not found — {len(rejected_urls)} source(s) tried and rejected")
            else:
                _tlog(app_task, f"[{doc_label}] Not found — all search stages exhausted")
            errors.append(f"{doc_label}: not found")
            continue

        # ── 2. Download (if URL came from doc["url"] or vendor index) ───────
        if not dest_path:
            dest_path = manuals_dir / device_id / pdf_filename(
                device.get("name", device_id), doc_type)
            update_doc_fields(device_id, doc_type, {"fetch_status": "downloading"},
                              inventory_path, lock)
            _tlog(app_task, f"[{doc_label}] Downloading…")
            try:
                http_headers = await download_pdf(source_url, dest_path)
            except Exception as exc:
                _tlog(app_task, f"[{doc_label}] Download failed: {exc}")
                update_doc_fields(device_id, doc_type,
                                  {"fetch_status": "not_found", "fetch_error": str(exc)},
                                  inventory_path, lock)
                errors.append(f"{doc_label}: download failed")
                continue
            pdf_meta = extract_pdf_meta(dest_path)
            _tlog(app_task, f"[{doc_label}] Downloaded — {pdf_meta.get('pdf_pages', '?')} page(s)")

        update_doc_fields(device_id, doc_type, {"fetch_status": "uploading"}, inventory_path, lock)

        paperless_id: int | None = None
        if paperless_available():
            _tlog(app_task, f"[{doc_label}] Uploading to Paperless…")
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

                title = f"{device.get('name', device_id)} — {doc_label}"
                client = PaperlessClient()
                task_id = await client.upload_document(
                    dest_path,
                    title=title,
                    device_id=device_id,
                    manufacturer=device.get("manufacturer", ""),
                    doc_type=doc_label,
                    extra_tags=extra_tags,
                )
                _tlog(app_task, f"[{doc_label}] Waiting for OCR…")
                paperless_id = await client.resolve_task(task_id)
                if paperless_id:
                    _tlog(app_task, f"[{doc_label}] Paperless #{paperless_id} ✓")
                else:
                    _tlog(app_task, f"[{doc_label}] OCR timed out — check Paperless logs")
            except Exception as exc:
                logger.error("Paperless upload failed for %s/%s: %s", device_id, doc_type, exc)
                _tlog(app_task, f"[{doc_label}] Upload error: {exc}")

        # ── 4. Write final status ─────────────────────────────────────────
        if paperless_available() and not paperless_id:
            fetch_status = "error"
            fetch_error = "PDF downloaded but Paperless did not confirm storage — check Paperless logs"
            errors.append(f"{doc_label}: Paperless storage unconfirmed")
        else:
            fetch_status = "success"
            fetch_error = None

        final: dict = {
            "fetch_status": fetch_status,
            "fetch_error": fetch_error,
            "source_url": source_url,
            "last_modified": http_headers.get("last_modified"),
            "etag": http_headers.get("etag"),
            "pdf_mod_date": pdf_meta.get("pdf_mod_date"),
            "pdf_version": pdf_meta.get("pdf_version"),
            "pdf_pages": pdf_meta.get("pdf_pages"),
            "fetched_at": datetime.date.today().isoformat(),
        }
        if paperless_id:
            final["paperless_id"] = paperless_id
        update_doc_fields(device_id, doc_type, final, inventory_path, lock)

    logger.info("Fetch complete for device %s", device_id)
    if app_task:
        if errors:
            _t.task_done(app_task, False, f"Finished with errors: {'; '.join(errors)}")
        else:
            _t.task_done(app_task, True, f"All {len(docs)} document(s) fetched successfully")


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

    if check["status"] == "error":
        return {"result": "error", "detail": check["reason"]}

    if check["status"] == "no_version_info":
        # Source URL has no ETag/Last-Modified — try searching using the
        # Paperless OCR title, which may be more accurate than the original hint.
        search_query = doc.get("search_hint", "")
        paperless_id = doc.get("paperless_id")
        if paperless_id and paperless_available():
            try:
                pl_title = await PaperlessClient().get_document_title(paperless_id)
                if pl_title:
                    search_query = pl_title
            except Exception:
                pass

        if not search_query:
            return {"result": "no_version_info", "detail": check["reason"]}

        search_result = await search_pdf_url(
            search_query,
            manufacturer=device.get("manufacturer", ""),
            model=device.get("model", ""),
        )
        if not search_result or search_result[0] == source_url:
            return {
                "result": "no_version_info",
                "detail": f"{check['reason']} — search found no different URL",
            }
        new_url, found_via = search_result
        logger.info("Update search found URL via %s for %s/%s", found_via, device_id, doc_type)
        source_url = new_url  # fall through to download + compare below

    # ── Headers changed — download and compare PDF metadata ────────────────
    device_name = device.get("name", device_id)
    final_filename = pdf_filename(device_name, doc_type)
    dest_path = manuals_dir / device_id / f"{final_filename}.update.pdf"
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
    final_path = manuals_dir / device_id / final_filename
    dest_path.replace(final_path)

    label_map = {"manual": "Manual", "quickstart": "Quick Start", "datasheet": "Datasheet"}
    doc_label = doc.get("label") or label_map.get(doc_type, doc_type.replace("-", " ").title())
    date_str = datetime.date.today().isoformat()
    title = f"{device.get('name', device_id)} — {doc_label} (updated {date_str})"
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
                doc_type=doc_label,
                extra_tags=extra_tags,
            )
            new_paperless_id = await client.resolve_task(task_id)

            # Delete old document
            old_paperless_id = doc.get("paperless_id")
            if old_paperless_id:
                await client.delete_document(old_paperless_id)
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
