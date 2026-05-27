#!/usr/bin/env python3
"""
Guided setup for the inventory-manager service account in Paperless-NGX.

TrueNAS iX-managed app containers do not allow docker exec, so account creation
must be done through the Paperless Django admin web UI. This script:
  1. Prompts for Paperless URL and verifies it is reachable
  2. Prints step-by-step instructions with direct admin URLs
  3. Lists the exact permissions to assign
  4. Prompts you to paste in the generated API token
  5. Verifies the token works with the required API endpoints
  6. Prints the PAPERLESS_TOKEN line for your Dockge .env

Run from your local machine (requires network access to Paperless):
  python3 paperless/scripts/inventory_manager/deploy/setup-paperless-account.py
"""

import getpass
import json
import sys
import urllib.error
import urllib.request

DEFAULT_PAPERLESS_URL = "http://truenas.local:30070"
SERVICE_USERNAME = "inventory-manager"

REQUIRED_PERMISSIONS = [
    ("Documents",       ["view_document", "add_document", "delete_document"]),
    ("Tags",            ["view_tag", "add_tag"]),
    ("Correspondents",  ["view_correspondent", "add_correspondent"]),
    ("Document types",  ["view_documenttype", "add_documenttype"]),
    ("Task status",     ["view_paperlesstask"]),
]


# ── Helpers ────────────────────────────────────────────────────────────────

def prompt(message: str, default: str) -> str:
    value = input(f"{message} [{default}]: ").strip()
    return value if value else default


def http_get(url: str, token: str | None = None) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 4xx/5xx — server responded, so it is reachable
        return e.code, {}
    except urllib.error.URLError:
        # DNS failure, connection refused, timeout — truly unreachable
        return 0, {}


def http_post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def divider(char: str = "─", width: int = 60) -> None:
    print(char * width)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("Paperless-NGX — inventory-manager service account setup")
    divider("=")
    print()

    # ── 1. Paperless URL ────────────────────────────────────────────────────
    paperless_url = prompt("Paperless URL", DEFAULT_PAPERLESS_URL).rstrip("/")
    print()

    # ── 2. Verify the instance is reachable ─────────────────────────────────
    print("Checking Paperless is reachable...")
    status, _ = http_get(f"{paperless_url}/api/")
    if status == 0:
        print(f"Error: could not connect to Paperless at {paperless_url}.")
        print("  - If you used a .local hostname, try the IP address instead.")
        print("    WSL2 and some Linux environments do not resolve mDNS names.")
        print(f"  - Verify TrueNAS is reachable from this machine.")
        sys.exit(1)
    print(f"Paperless is reachable at {paperless_url}")
    print()

    # ── 3. Verify admin credentials (optional — confirms login works) ────────
    print("Enter your Paperless admin credentials to verify access.")
    print("(Credentials are used only for this check and are never stored.)")
    admin_user = input("Admin username: ").strip()
    admin_pass = getpass.getpass("Admin password: ")
    print()

    print("Verifying admin credentials...")
    status, body = http_post(
        f"{paperless_url}/api/token/",
        {"username": admin_user, "password": admin_pass},
    )
    if status != 200:
        print(f"Error: authentication failed (HTTP {status}).")
        print("Check your credentials and try again.")
        sys.exit(1)
    print("Admin credentials verified.")
    print()

    # ── 4. Print step-by-step Django admin instructions ─────────────────────
    divider()
    print("STEP 1 — Open the Paperless Django admin in your browser:")
    print()
    print(f"  {paperless_url}/admin/")
    print()
    print("Log in with your admin credentials, then follow these steps:")
    print()
    divider()
    print()
    print("STEP 2 — Create the service account user:")
    print()
    print(f"  {paperless_url}/admin/auth/user/add/")
    print()
    print("  Username:   inventory-manager")
    print("  Password:   click 'Usable password' → select 'Unusable password'")
    print("              (the account cannot log in with a password — token only)")
    print("  Staff:      unchecked")
    print("  Superuser:  unchecked")
    print()
    print("  Click SAVE — this opens the user edit page.")
    print()
    divider()
    print()
    print("STEP 3 — Assign permissions on the user edit page:")
    print()
    print("  Scroll to the 'User permissions' section.")
    print("  Search for and add each of the following permissions:")
    print()
    for group, codenames in REQUIRED_PERMISSIONS:
        print(f"  {group}:")
        for c in codenames:
            print(f"    + {c}")
        print()
    print("  Click SAVE.")
    print()
    divider()
    print()
    print("STEP 4 — Generate an API token:")
    print()
    print(f"  {paperless_url}/admin/authtoken/tokenproxy/add/")
    print()
    print("  User: select 'inventory-manager' from the dropdown")
    print("  Click SAVE — the token key is shown on the next page.")
    print()
    divider()
    print()
    input("Press Enter once you have completed the steps above and have the token ready...")
    print()

    # ── 5. Collect and verify the token ─────────────────────────────────────
    token = getpass.getpass("Paste the API token here (input hidden): ").strip()
    if not token:
        print("Error: no token entered.")
        sys.exit(1)
    print()

    print("Verifying token permissions...")
    errors = []

    # view_document
    s, _ = http_get(f"{paperless_url}/api/documents/?page_size=1", token)
    if s != 200:
        errors.append(f"view_document — GET /api/documents/ returned HTTP {s}")

    # view_tag
    s, _ = http_get(f"{paperless_url}/api/tags/?page_size=1", token)
    if s != 200:
        errors.append(f"view_tag — GET /api/tags/ returned HTTP {s}")

    # view_correspondent
    s, _ = http_get(f"{paperless_url}/api/correspondents/?page_size=1", token)
    if s != 200:
        errors.append(f"view_correspondent — GET /api/correspondents/ returned HTTP {s}")

    # view_documenttype
    s, _ = http_get(f"{paperless_url}/api/document_types/?page_size=1", token)
    if s != 200:
        errors.append(f"view_documenttype — GET /api/document_types/ returned HTTP {s}")

    if errors:
        print()
        print("Warning: some permission checks failed:")
        for e in errors:
            print(f"  ✗  {e}")
        print()
        print("The token was saved but the account may be missing permissions.")
        print("Re-check the assigned permissions in Django admin and re-run this script.")
    else:
        print("All read permissions verified.")
    print()

    # ── 6. Output ────────────────────────────────────────────────────────────
    divider()
    print("Add this to your Dockge stack env (inventory-manager → Env):")
    print()
    print(f"  PAPERLESS_TOKEN={token}")
    print()
    divider()
    print()
    print("To revoke access at any time, delete the token in Paperless admin:")
    print(f"  {paperless_url}/admin/authtoken/tokenproxy/")
    print()


if __name__ == "__main__":
    main()
