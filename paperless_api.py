"""
Paperless-NGX API client — scoped to device inventory documents.

Every document written by this client receives the SYSTEM_TAG marker.
Deletion queries require both SYSTEM_TAG and the per-device tag, so
documents not created by this system can never be deleted here.
"""

import os
import logging
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

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._url,
            headers=self._headers,
            timeout=30.0,
        )

    async def _get_tag_id(self, client: httpx.AsyncClient, name: str) -> int | None:
        """Return the Paperless ID for a tag, or None if it does not exist."""
        page = 1
        while True:
            resp = await client.get("/api/tags/", params={"page": page, "page_size": 100})
            resp.raise_for_status()
            body = resp.json()
            for tag in body.get("results", []):
                if tag["name"] == name:
                    return tag["id"]
            if not body.get("next"):
                return None
            page += 1

    async def _resolve_tag_id(self, client: httpx.AsyncClient, name: str) -> int:
        """Return the Paperless ID for a tag, creating it if it does not exist."""
        tag_id = await self._get_tag_id(client, name)
        if tag_id is not None:
            return tag_id
        resp = await client.post("/api/tags/", json={"name": name})
        resp.raise_for_status()
        return resp.json()["id"]

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
        """
        Upload a PDF to Paperless and return the async task ID.

        All uploaded documents receive SYSTEM_TAG and device:<device_id>.
        This enables scoped deletion — see delete_device_documents.
        """
        async with self._client() as client:
            tag_names = [SYSTEM_TAG, f"device:{device_id}"] + (extra_tags or [])
            tag_ids = [await self._resolve_tag_id(client, t) for t in tag_names]
            with pdf_path.open("rb") as fh:
                resp = await client.post(
                    "/api/documents/post_document/",
                    data={
                        "title": title,
                        "correspondent": manufacturer,
                        "document_type": doc_type,
                        "tags": tag_ids,
                    },
                    files={"document": (pdf_path.name, fh, "application/pdf")},
                )
            resp.raise_for_status()
            return resp.text.strip('"')

    async def delete_device_documents(self, device_id: str) -> list[int]:
        """
        Delete Paperless documents for a device that carry SYSTEM_TAG.

        Only documents tagged with BOTH device:<device_id> AND SYSTEM_TAG are
        deleted. Documents in Paperless that were not created by this system
        (i.e. missing SYSTEM_TAG) are never touched, even if they carry the
        device tag.

        Returns the list of deleted document IDs.
        """
        async with self._client() as client:
            sys_id = await self._get_tag_id(client, SYSTEM_TAG)
            dev_id = await self._get_tag_id(client, f"device:{device_id}")

            if sys_id is None or dev_id is None:
                return []

            deleted: list[int] = []
            page = 1
            while True:
                resp = await client.get(
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
                    del_resp = await client.delete(f"/api/documents/{doc['id']}/")
                    del_resp.raise_for_status()
                    deleted.append(doc["id"])
                if not body.get("next"):
                    break
                page += 1

            return deleted

    async def resolve_task(self, task_id: str, timeout: int = 90) -> int | None:
        """
        Poll the Paperless task endpoint until the upload task completes.
        Returns the resulting document ID, or None on timeout/failure.
        """
        import asyncio, time
        deadline = time.monotonic() + timeout
        async with self._client() as client:
            while time.monotonic() < deadline:
                resp = await client.get("/api/tasks/", params={"task_id": task_id})
                resp.raise_for_status()
                tasks = resp.json()
                if tasks:
                    task = tasks[0]
                    if task.get("status") == "SUCCESS":
                        return task.get("related_document")
                    if task.get("status") in ("FAILURE", "REVOKED"):
                        return None
                await asyncio.sleep(3)
        return None

    async def list_device_documents(self, device_id: str) -> list[dict]:
        """List documents owned by this system for a given device."""
        async with self._client() as client:
            sys_id = await self._get_tag_id(client, SYSTEM_TAG)
            dev_id = await self._get_tag_id(client, f"device:{device_id}")

            if sys_id is None or dev_id is None:
                return []

            results: list[dict] = []
            page = 1
            while True:
                resp = await client.get(
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
