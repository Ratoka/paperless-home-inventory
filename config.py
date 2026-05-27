"""
Persistent configuration and API usage tracking.

All data lives in DATA_DIR/config.yaml so it survives container restarts.
The module is a process-level singleton: call init() once at startup.
"""

import logging
import threading
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_data: dict = {}
_path: Path | None = None

# Free-tier limits per provider.  Tuple is (queries, period) where period is
# "month" or "day".  Used by is_over_free_limit() and the Settings UI.
FREE_LIMITS: dict[str, tuple[int, str]] = {
    "brave":      (1000, "month"),
    "google_cse": (100,  "day"),
    "bing":       (1000, "month"),
}


def _ryaml() -> YAML:
    ry = YAML()
    ry.preserve_quotes = True
    ry.width = 120
    return ry


def init(path: Path) -> None:
    global _path
    _path = path
    _reload()


def _reload() -> None:
    if _path and _path.exists():
        ry = _ryaml()
        with open(_path) as f:
            loaded = ry.load(f) or {}
        _data.clear()
        _data.update(loaded)
    _data.setdefault("search_providers", {})
    _data.setdefault("api_usage", {})


def _persist() -> None:
    if _path:
        ry = _ryaml()
        with open(_path, "w") as f:
            ry.dump(dict(_data), f)


# ── Provider config ──────────────────────────────────────────────────────────

def get_provider(name: str) -> dict:
    """Return a copy of the stored config dict for a provider (may be empty)."""
    return dict(_data.get("search_providers", {}).get(name, {}))


def set_provider(name: str, fields: dict) -> None:
    """Merge non-blank fields into provider config and persist."""
    with _lock:
        existing = dict(_data.get("search_providers", {}).get(name, {}))
        for k, v in fields.items():
            if v:  # never overwrite an existing key with blank
                existing[k] = v
        _data.setdefault("search_providers", {})[name] = existing
        _persist()


def remove_provider(name: str) -> None:
    with _lock:
        _data.get("search_providers", {}).pop(name, None)
        _persist()


def get_api_key(name: str) -> str:
    return _data.get("search_providers", {}).get(name, {}).get("api_key", "")


def get_field(name: str, field: str) -> str:
    return _data.get("search_providers", {}).get(name, {}).get(field, "")


def is_enabled(name: str) -> bool:
    return bool(get_api_key(name))


# ── Usage tracking (daily keys: YYYY-MM-DD) ──────────────────────────────────

def record_use(provider: str) -> None:
    """Increment today's counter for a provider. Thread-safe and persisted."""
    day = date.today().isoformat()
    with _lock:
        bucket = _data.setdefault("api_usage", {}).setdefault(provider, {})
        bucket[day] = bucket.get(day, 0) + 1
        _persist()


def current_day_usage(provider: str) -> int:
    day = date.today().isoformat()
    return _data.get("api_usage", {}).get(provider, {}).get(day, 0)


def current_month_usage(provider: str) -> int:
    month = date.today().strftime("%Y-%m")
    return sum(
        v for k, v in _data.get("api_usage", {}).get(provider, {}).items()
        if k.startswith(month)
    )


def usage_history(provider: str) -> dict[str, int]:
    """All daily usage records for a provider, sorted newest-first."""
    return dict(sorted(_data.get("api_usage", {}).get(provider, {}).items(), reverse=True))


def all_usage() -> dict[str, dict[str, int]]:
    return {k: dict(v) for k, v in _data.get("api_usage", {}).items()}


# ── Paid-usage flag ───────────────────────────────────────────────────────────

def allow_paid(provider: str) -> bool:
    """Return True if the user has opted in to paid usage beyond the free tier."""
    return bool(_data.get("search_providers", {}).get(provider, {}).get("allow_paid", False))


def set_allow_paid(provider: str, value: bool) -> None:
    with _lock:
        _data.setdefault("search_providers", {}).setdefault(provider, {})["allow_paid"] = value
        _persist()


# ── Server-reported rate-limit info (parsed from response headers) ────────────

def store_rate_limit_info(provider: str, remaining: int, reset_date: str) -> None:
    """
    Persist the rate-limit snapshot returned by the provider's API headers.
    remaining  — queries left in the current billing period
    reset_date — ISO date (YYYY-MM-DD) when the period resets
    """
    with _lock:
        _data.setdefault("search_providers", {}).setdefault(provider, {})["rate_limit"] = {
            "remaining":  remaining,
            "reset_date": reset_date,
        }
        _persist()


def get_rate_limit_info(provider: str) -> dict:
    """Return the last stored rate-limit snapshot, or {} if none recorded yet."""
    return dict(_data.get("search_providers", {}).get(provider, {}).get("rate_limit", {}))


def clear_rate_limit_info(provider: str) -> None:
    """Remove the cached server rate-limit snapshot so the local counter is used."""
    with _lock:
        _data.get("search_providers", {}).get(provider, {}).pop("rate_limit", None)
        _persist()


# ── Limit enforcement ─────────────────────────────────────────────────────────

def is_over_free_limit(provider: str) -> bool:
    """
    Return True if this provider has consumed its free-tier quota.

    Prefers server-reported remaining (from the last API response headers) when
    the reset date is still in the future.  Falls back to summing our local
    day-counters for the current month/day when no server data is available.
    """
    limit, period = FREE_LIMITS.get(provider, (0, "month"))
    if not limit:
        return False

    info = get_rate_limit_info(provider)
    reset = info.get("reset_date", "")
    if info.get("remaining") is not None and reset >= date.today().isoformat():
        over = int(info["remaining"]) <= 0
        logger.info(
            "%s limit check: server-reported remaining=%s reset=%s → over=%s",
            provider, info["remaining"], reset, over,
        )
        return over

    # Fall back to local count
    usage = current_day_usage(provider) if period == "day" else current_month_usage(provider)
    over = usage >= limit
    logger.info(
        "%s limit check: no server data (reset=%r) — local %s usage=%d limit=%d → over=%s",
        provider, reset, period, usage, limit, over,
    )
    return over
