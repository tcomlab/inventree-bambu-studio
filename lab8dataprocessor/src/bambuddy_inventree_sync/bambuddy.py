from typing import Any

import httpx

from .config import Settings
from .http_errors import ExternalApiError
from .models import Archive


class DownloadedFile:
    def __init__(self, *, content: bytes, content_type: str, filename: str) -> None:
        self.content = content
        self.content_type = content_type
        self.filename = filename


class BambuddyClient:
    def __init__(self, settings: Settings) -> None:
        base_url = settings.bambuddy_base_url.rstrip("/")
        if not base_url.endswith("/api/v1"):
            base_url = f"{base_url}/api/v1"

        self.base_url = base_url
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": settings.bambuddy_api_key, "Accept": "application/json"},
            timeout=settings.http_timeout_seconds,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_archive(self, archive_id: int) -> Archive:
        data = await self._request("GET", f"/archives/{archive_id}")
        return Archive.model_validate(data)

    async def get_system_info(self) -> dict[str, Any]:
        return await self._request("GET", "/system/info")

    async def get_archive_thumbnail(self, archive_id: int) -> DownloadedFile | None:
        response = await self.client.get(f"/archives/{archive_id}/thumbnail")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            body = response.text[:1000]
            raise ExternalApiError(f"Bambuddy GET /archives/{archive_id}/thumbnail failed: HTTP {response.status_code}: {body}")

        content_type = response.headers.get("content-type", "image/png").split(";")[0]
        extension = "jpg" if content_type == "image/jpeg" else "png"
        return DownloadedFile(
            content=response.content,
            content_type=content_type,
            filename=f"bambuddy-archive-{archive_id}-thumbnail.{extension}",
        )

    async def update_archive_external_url(self, archive_id: int, external_url: str) -> Archive:
        data = await self._request("PATCH", f"/archives/{archive_id}", json={"external_url": external_url})
        return Archive.model_validate(data)

    async def list_printers(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/printers/")
        return data if isinstance(data, list) else []

    async def list_spools(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/inventory/spools")
        if isinstance(data, list):
            return data
        return data.get("spools") or data.get("results") or data.get("items") or []

    async def create_spool(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/inventory/spools", json=payload)
        return data if isinstance(data, dict) else {}

    async def update_spool(self, spool_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("PATCH", f"/inventory/spools/{spool_id}", json=payload)
        return data if isinstance(data, dict) else {}

    async def delete_spool(self, spool_id: int) -> None:
        await self._request("DELETE", f"/inventory/spools/{spool_id}")

    async def list_assignments(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/inventory/assignments")
        return data if isinstance(data, list) else []

    async def assign_spool(self, *, spool_id: int, printer_id: int, ams_id: int, tray_id: int) -> None:
        await self._request("POST", "/inventory/assignments", json={
            "spool_id": spool_id, "printer_id": printer_id, "ams_id": ams_id, "tray_id": tray_id,
        })

    async def unassign_spool(self, *, printer_id: int, ams_id: int, tray_id: int) -> None:
        await self._request("DELETE", f"/inventory/assignments/{printer_id}/{ams_id}/{tray_id}")

    async def list_archives(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int | None, list[Archive]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status

        data = await self._request("GET", "/archives/", params=params)
        if isinstance(data, list):
            return None, [Archive.model_validate(item) for item in data]

        archives = data.get("archives") or data.get("results") or []
        total = data.get("total") or data.get("count")
        return total, [Archive.model_validate(item) for item in archives]

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            body = response.text[:1000]
            raise ExternalApiError(f"Bambuddy {method} {path} failed: HTTP {response.status_code}: {body}")

        if response.content:
            return response.json()
        return None
