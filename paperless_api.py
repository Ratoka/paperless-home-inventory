"""
Paperless-NGX API client — scoped to device inventory documents.

Every document written by this client receives the SYSTEM_TAG marker.
Deletion queries require both SYSTEM_TAG and the per-device tag, so
documents not created by this system can never be deleted here.
"""

import asyncio
import os
import logging
import time
from pathlib import Path

import httpx

SYSTEM_TAG = "source:device-inventory"

logger = logging.getLogger(__name__)


def paperless_available() -> bool:
    return bool(os.getenv("PAPERLESS_URL") and os.getenv("PAPERLESS_TOKEN"))


class PaperlessClient:
    def __init__(self):
        self._url = os.environ["PAPERLESS_URL"].rstrip("/")
        self._headers = {"Authorization": f"Token {os.environ['PAPERLESS_TOKEN']}"}

    def _client(self, timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(
            base_url=self._url,
            headers=self._headers,
            timeout=timeout,
        )

    # ── Sync internals ─────────────────────────────────────────────────────

    def _get_tag_id(self, client: httpx.Client, name: str) -> int | None:
        page = 1
        while True:
            resp = client.get("/api/tags/", params={"page": page, "page_size": 100})
            resp.raise_for_status()
            body = resp.json()
            for tag in body.get("results", []):
                if tag["name"] == name:
                    return tag["id"]
            if not body.get("next"):
                return None
            page += 1

    def _resolve_tag_id(self, client: httpx.Client, name: str) -> int:
        tag_id = self._get_tag_id(client, name)
        if tag_id is not None:
            return tag_id
        resp = client.post("/api/tags/", json={"name": name, "owner": None})
        resp.raise_for_status()
        return resp.json()["id"]

    def _resolve_correspondent_id(self, client: httpx.Client, name: str) -> int | None:
        if not name:
            return None
        resp = client.get("/api/correspondents/", params={"name": name, "page_size": 50})
        resp.raise_for_status()
        for item in resp.json().get("results", []):
            if item["name"].lower() == name.lower():
                return item["id"]
        # Try creating; fall back silently on 400 (already exists under different casing
        # or Paperless version doesn't support a field we sent).
        for payload in [{"name": name, "owner": None}, {"name": name}]:
            try:
                r = client.post("/api/correspondents/", json=payload)
                if r.status_code == 201:
                    return r.json()["id"]
                if r.status_code == 400:
                    # Might be a uniqueness conflict — re-search before giving up
                    r2 = client.get("/api/correspondents/", params={"name": name, "page_size": 50})
                    r2.raise_for_status()
                    for item in r2.json().get("results", []):
                        if item["name"].lower() == name.lower():
                            return item["id"]
                    if payload.get("owner") is None:
                        continue  # try without owner field
                    logger.warning("Could not create correspondent %r: %s", name, r.text)
                    return None
                r.raise_for_status()
            except httpx.HTTPStatusError:
                pass
        return None

    def _resolve_document_type_id(self, client: httpx.Client, name: str) -> int | None:
        if not name:
            return None
        resp = client.get("/api/document_types/", params={"name": name, "page_size": 50})
        resp.raise_for_status()
        for item in resp.json().get("results", []):
            if item["name"].lower() == name.lower():
                return item["id"]
        for payload in [{"name": name, "owner": None}, {"name": name}]:
            try:
                r = client.post("/api/document_types/", json=payload)
                if r.status_code == 201:
                    return r.json()["id"]
                if r.status_code == 400:
                    r2 = client.get("/api/document_types/", params={"name": name, "page_size": 50})
                    r2.raise_for_status()
                    for item in r2.json().get("results", []):
                        if item["name"].lower() == name.lower():
                            return item["id"]
                    if payload.get("owner") is None:
                        continue
                    logger.warning("Could not create document_type %r: %s", name, r.text)
                    return None
                r.raise_for_status()
            except httpx.HTTPStatusError:
                pass
        return None

    def _do_upload(
        self,
        pdf_path: Path,
        *,
        title: str,
        device_id: str,
        manufacturer: str,
        doc_type: str,
        extra_tags: list[str] | None = None,
    ) -> str:
        with self._client(timeout=120.0) as client:
            # Always-present tags: system marker, per-device tag, category tags,
            # plus manufacturer as a tag so it's searchable even if correspondent fails.
            tag_names = [SYSTEM_TAG, f"device:{device_id}"] + (extra_tags or [])
            if manufacturer:
                import re as _re
                mfr_slug = _re.sub(r"[^a-z0-9-]", "-",
                                   manufacturer.strip().lower()).strip("-")
                mfr_slug = _re.sub(r"-+", "-", mfr_slug)
                if mfr_slug:
                    tag_names.append(f"mfr:{mfr_slug}")
            tag_ids = [self._resolve_tag_id(client, t) for t in tag_names]

            # Correspondent and document type are best-effort: failures here must
            # not prevent the document from being stored.
            correspondent_id: int | None = None
            try:
                correspondent_id = self._resolve_correspondent_id(client, manufacturer)
            except Exception as exc:
                logger.warning("Skipping correspondent for %r: %s", manufacturer, exc)

            document_type_id: int | None = None
            try:
                document_type_id = self._resolve_document_type_id(client, doc_type)
            except Exception as exc:
                logger.warning("Skipping document_type for %r: %s", doc_type, exc)

            form: dict = {
                "title": title,
                "tags": [str(tid) for tid in tag_ids],
            }
            if correspondent_id is not None:
                form["correspondent"] = str(correspondent_id)
            if document_type_id is not None:
                form["document_type"] = str(document_type_id)

            with pdf_path.open("rb") as fh:
                resp = client.post(
                    "/api/documents/post_document/",
                    data=form,
                    files={"document": (pdf_path.name, fh, "application/pdf")},
                )
            resp.raise_for_status()
            return resp.text.strip('"')

    def _do_delete_device_documents(self, device_id: str) -> list[int]:
        with self._client() as client:
            sys_id = self._get_tag_id(client, SYSTEM_TAG)
            dev_id = self._get_tag_id(client, f"device:{device_id}")

            if sys_id is None or dev_id is None:
                return []

            deleted: list[int] = []
            page = 1
            while True:
                resp = client.get(
                    "/api/documents/",
                    params={
                        "tags__id__all": f"{sys_id},{dev_id}",
                        "page": page,
                        "page_size": 100,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                for doc in body.get("results", []):
                    del_resp = client.delete(f"/api/documents/{doc['id']}/")
                    del_resp.raise_for_status()
                    deleted.append(doc["id"])
                if not body.get("next"):
                    break
                page += 1

            return deleted

    def _do_get_document_title(self, doc_id: int) -> str | None:
        with self._client() as client:
            resp = client.get(f"/api/documents/{doc_id}/")
            if resp.status_code == 200:
                return resp.json().get("title")
            return None

    def _do_resolve_task(self, task_id: str, timeout: int = 90) -> int | None:
        deadline = time.monotonic() + timeout
        with self._client() as client:
            while time.monotonic() < deadline:
                resp = client.get("/api/tasks/", params={"task_id": task_id})
                resp.raise_for_status()
                tasks = resp.json()
                if tasks:
                    task = tasks[0]
                    if task.get("status") == "SUCCESS":
                        return task.get("related_document")
                    if task.get("status") in ("FAILURE", "REVOKED"):
                        return None
                time.sleep(3)
        return None

    def _do_list_device_documents(self, device_id: str) -> list[dict]:
        with self._client() as client:
            sys_id = self._get_tag_id(client, SYSTEM_TAG)
            dev_id = self._get_tag_id(client, f"device:{device_id}")

            if sys_id is None or dev_id is None:
                return []

            results: list[dict] = []
            page = 1
            while True:
                resp = client.get(
                    "/api/documents/",
                    params={
                        "tags__id__all": f"{sys_id},{dev_id}",
                        "page": page,
                        "page_size": 100,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                results.extend(body.get("results", []))
                if not body.get("next"):
                    break
                page += 1

            return results

    def _do_delete_document(self, doc_id: int) -> None:
        with self._client() as client:
            resp = client.delete(f"/api/documents/{doc_id}/")
            resp.raise_for_status()

    def _do_retag_category(self, old_tag: str, new_tag: str) -> int:
        """Replace old_tag with new_tag on all matching documents. Returns doc count."""
        with self._client() as client:
            old_id = self._get_tag_id(client, old_tag)
            if old_id is None:
                return 0
            new_id = self._resolve_tag_id(client, new_tag)
            count = 0
            page = 1
            while True:
                resp = client.get("/api/documents/", params={
                    "tags__id__all": str(old_id),
                    "page": page,
                    "page_size": 100,
                })
                resp.raise_for_status()
                body = resp.json()
                for doc in body["results"]:
                    curr = list(doc["tags"])
                    new_list = [t for t in curr if t != old_id]
                    if new_id not in new_list:
                        new_list.append(new_id)
                    client.patch(f"/api/documents/{doc['id']}/", json={"tags": new_list}).raise_for_status()
                    count += 1
                if not body.get("next"):
                    break
                page += 1
            return count

    # ── Async public interface ──────────────────────────────────────────────

    async def upload_document(
        self,
        pdf_path: Path,
        *,
        title: str,
        device_id: str,
        manufacturer: str,
        doc_type: str,
        extra_tags: list[str] | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._do_upload,
            pdf_path,
            title=title,
            device_id=device_id,
            manufacturer=manufacturer,
            doc_type=doc_type,
            extra_tags=extra_tags,
        )

    async def delete_device_documents(self, device_id: str) -> list[int]:
        return await asyncio.to_thread(self._do_delete_device_documents, device_id)

    async def get_document_title(self, doc_id: int) -> str | None:
        return await asyncio.to_thread(self._do_get_document_title, doc_id)

    async def resolve_task(self, task_id: str, timeout: int = 90) -> int | None:
        return await asyncio.to_thread(self._do_resolve_task, task_id, timeout)

    async def list_device_documents(self, device_id: str) -> list[dict]:
        return await asyncio.to_thread(self._do_list_device_documents, device_id)

    async def delete_document(self, doc_id: int) -> None:
        await asyncio.to_thread(self._do_delete_document, doc_id)

    async def retag_category(self, old_tag: str, new_tag: str) -> int:
        return await asyncio.to_thread(self._do_retag_category, old_tag, new_tag)
