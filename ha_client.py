"""
Home Assistant API client — fetches physical devices via the template engine.

Environment variables:
  HA_URL    Base URL of the HA instance (e.g. https://10.0.0.5:8123)
  HA_TOKEN  Long-lived access token

SSL verification is skipped by default (common in homelab HA installs with
self-signed certs). Set HA_VERIFY_SSL=true to enable.
"""

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Jinja2 template evaluated by HA's template engine.
# Collects all physical devices (entry_type is None), one entry per device ID.
# Also extracts the integration domain from the first device identifier so the
# caller can map it to a protocol (zha → zigbee, zwave_js → zwave, etc.).
_DEVICE_TEMPLATE = """\
{%- set ns = namespace(seen=[], result=[]) -%}
{%- for state in states -%}
{%- set did = device_id(state.entity_id) -%}
{%- if did is not none and did not in ns.seen and device_attr(did, 'entry_type') is none -%}
{%- set ns.seen = ns.seen + [did] -%}
{%- set dom = namespace(val='') -%}
{%- for ident in device_attr(did, 'identifiers') -%}{%- set dom.val = ident[0] -%}{%- endfor -%}
{%- set ns.result = ns.result + [{'id': did, 'name': (device_attr(did, 'name') or ''), 'manufacturer': (device_attr(did, 'manufacturer') or ''), 'model': (device_attr(did, 'model') or ''), 'area': (area_name(did) or ''), 'domain': dom.val}] -%}
{%- endif -%}
{%- endfor -%}
{{ ns.result | to_json }}\
"""

# Maps HA integration domain → inventory protocol tags
_DOMAIN_PROTOCOLS: dict[str, list[str]] = {
    "zha":                ["zigbee"],
    "zwave_js":           ["zwave"],
    "ozw":                ["zwave"],
    "matter":             ["matter"],
    "hue":                ["zigbee"],
    "deconz":             ["zigbee"],
    "esphome":            ["wifi"],
    "tuya":               ["wifi"],
    "shelly":             ["wifi"],
    "wled":               ["wifi"],
    "tasmota":            ["wifi"],
    "homekit_controller": ["wifi"],
    "unifi":              ["ethernet"],
}


def ha_available() -> bool:
    return bool(os.getenv("HA_URL") and os.getenv("HA_TOKEN"))


def _ha_client() -> httpx.AsyncClient:
    verify = os.getenv("HA_VERIFY_SSL", "false").lower() not in ("0", "false", "no")
    return httpx.AsyncClient(verify=verify, timeout=30.0)


async def fetch_ha_devices() -> list[dict]:
    """
    Fetch all physical HA devices via the template API.
    Returns a list of dicts: {id, name, manufacturer, model, area}.
    Raises httpx.HTTPError or ValueError on failure.
    """
    url   = os.environ["HA_URL"].rstrip("/")
    token = os.environ["HA_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with _ha_client() as client:
        resp = await client.post(
            f"{url}/api/template",
            headers=headers,
            json={"template": _DEVICE_TEMPLATE},
        )
        resp.raise_for_status()

    text = resp.text.strip()
    try:
        devices = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse HA device list: %s | response: %r", exc, text[:400])
        raise ValueError(f"Unexpected response from HA template API: {text[:200]!r}") from exc

    if not isinstance(devices, list):
        raise ValueError(f"Expected a list from HA template, got: {type(devices).__name__}")

    return devices


def domain_to_protocols(domain: str) -> list[str]:
    """Return inventory protocol tags inferred from an HA integration domain."""
    return list(_DOMAIN_PROTOCOLS.get((domain or "").lower(), []))


def deduplicate_by_model(devices: list[dict]) -> list[dict]:
    """
    Keep one representative per unique (manufacturer, model) pair.
    Devices with no model are kept as-is (cannot deduplicate them by model).
    """
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for d in devices:
        mfr   = (d.get("manufacturer") or "").strip().lower()
        model = (d.get("model") or "").strip().lower()
        if model:
            key = (mfr, model)
            if key in seen:
                continue
            seen.add(key)
        result.append(d)
    return result
