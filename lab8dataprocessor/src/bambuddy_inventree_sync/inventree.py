from typing import Any

import httpx

from .config import Settings
from .http_errors import ExternalApiError
from .models import Archive


class InvenTreeClient:
    def __init__(self, settings: Settings) -> None:
        base_url = settings.inventree_base_url.rstrip("/")
        if not base_url.endswith("/api"):
            base_url = f"{base_url}/api"

        self.settings = settings
        self.base_url = base_url
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Token {settings.inventree_token}",
                "Accept": "application/json",
            },
            timeout=settings.http_timeout_seconds,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_part_category(self) -> dict[str, Any]:
        return await self._request("GET", f"/part/category/{self.settings.inventree_part_category_id}/")

    async def get_stock_location(self) -> dict[str, Any]:
        return await self._request("GET", f"/stock/location/{self.settings.inventree_stock_location_id}/")

    async def get_stock_location_by_id(self, location_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/stock/location/{location_id}/")

    async def find_part_by_reference(self, reference: str) -> dict[str, Any] | None:
        normalized_reference = reference.strip().lower()
        if not normalized_reference:
            return None

        for params in (
            {
                "search": normalized_reference,
                "category": self.settings.inventree_part_category_id,
                "limit": 100,
            },
            {"search": normalized_reference, "limit": 100},
        ):
            data = await self._request("GET", "/part/", params=params)
            for item in self._items(data):
                candidates = (
                    item.get("IPN"),
                    item.get("name"),
                    item.get("full_name"),
                )
                if any(str(value or "").strip().lower() == normalized_reference for value in candidates):
                    return item
        return None

    async def find_stock_by_batch(self, *, part_id: int, batch: str) -> dict[str, Any] | None:
        data = await self._request("GET", "/stock/", params={"part": part_id, "batch": batch, "limit": 100})
        for item in self._items(data):
            if item.get("part") == part_id and item.get("batch") == batch:
                return item
        return None

    async def list_filament_parts(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/part/", params={
            "category": self.settings.filament_part_category_id, "limit": 250,
        })
        return self._items(data)

    async def list_stock_items_for_part(self, part_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", "/stock/", params={"part": part_id, "limit": 250})
        return [item for item in self._items(data) if item.get("part") == part_id]

    async def list_stock_items_for_category(self, category_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", "/stock/", params={"category": category_id, "limit": 250})
        return self._items(data)

    async def update_stock_batch(self, stock_item_id: int, batch: str) -> dict[str, Any]:
        data = await self._request("PATCH", f"/stock/{stock_item_id}/", json={"batch": batch})
        return data if isinstance(data, dict) else {}

    async def update_stock_purchase_price(self, stock_item_id: int, purchase_price: float) -> dict[str, Any]:
        data = await self._request(
            "PATCH",
            f"/stock/{stock_item_id}/",
            json={"purchase_price": purchase_price},
        )
        return data if isinstance(data, dict) else {}

    async def find_stock_location_by_path(self, path: str) -> dict[str, Any] | None:
        normalized_path = path.strip().strip("/").lower()
        if not normalized_path:
            return None

        leaf_name = normalized_path.rsplit("/", 1)[-1]
        for item in await self.list_stock_locations(search=leaf_name):
            if str(item.get("pathstring") or "").strip("/").lower() == normalized_path:
                return item
        return None

    async def child_stock_locations(self, parent_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", "/stock/location/", params={"parent": parent_id, "limit": 100})
        items = [item for item in self._items(data) if item.get("parent") == parent_id]
        if items:
            return items

        parent = await self.get_stock_location_by_id(parent_id)
        path = str(parent.get("pathstring") or parent.get("name") or "")
        data = await self._request("GET", "/stock/location/", params={"search": path.rsplit("/", 1)[-1], "limit": 100})
        return [item for item in self._items(data) if item.get("parent") == parent_id]

    async def list_stock_locations(self, *, search: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"limit": limit}
        if search:
            query["search"] = search
        items: list[dict[str, Any]] = []
        offset = 0

        while True:
            query["offset"] = offset
            data = await self._request("GET", "/stock/location/", params=query)
            page_items = self._items(data)
            items.extend(page_items)

            if isinstance(data, dict):
                total = data.get("count")
                if total is not None and len(items) >= int(total):
                    break

            if len(page_items) < limit:
                break
            offset += len(page_items)

        return items

    async def list_stock_items_at_location(self, location_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"location": location_id, "limit": limit}
        items: list[dict[str, Any]] = []
        offset = 0

        while True:
            query["offset"] = offset
            data = await self._request("GET", "/stock/", params=query)
            page_items = [
                item
                for item in self._items(data)
                if item.get("location") == location_id and bool(item.get("in_stock", True))
            ]
            items.extend(page_items)

            if isinstance(data, dict):
                total = data.get("count")
                if total is not None and len(items) >= int(total):
                    break

            if len(self._items(data)) < limit:
                break
            offset += len(self._items(data))

        return items

    async def create_stock_item(
        self,
        *,
        part_id: int,
        archive: Archive,
        quantity: int,
        notes: str,
        purchase_price: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "part": part_id,
            "location": self.settings.inventree_stock_location_id,
            "quantity": quantity,
            "status": self.settings.inventree_stock_status,
            "batch": self.batch_for_archive(archive.id),
            "notes": notes,
        }
        if purchase_price is not None:
            payload["purchase_price"] = purchase_price
        data = await self._request("POST", "/stock/", json=payload)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        raise ExternalApiError(f"InvenTree POST /stock/ returned an unexpected response: {data!r}")

    async def remove_stock_quantity(self, *, stock_item_id: int, quantity: float, notes: str) -> dict[str, Any]:
        payload = {
            "items": [{"pk": stock_item_id, "quantity": quantity}],
            "notes": notes,
        }
        data = await self._request("POST", "/stock/remove/", json=payload)
        return data if isinstance(data, dict) else {}

    async def add_stock_quantity(self, *, stock_item_id: int, quantity: float, notes: str) -> dict[str, Any]:
        payload = {
            "items": [{"pk": stock_item_id, "quantity": quantity}],
            "notes": notes,
        }
        data = await self._request("POST", "/stock/add/", json=payload)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def batch_for_archive(archive_id: int) -> str:
        return f"bambuddy-{archive_id}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            body = response.text[:1000]
            raise ExternalApiError(f"InvenTree {method} {path} failed: HTTP {response.status_code}: {body}")

        if response.content:
            return response.json()
        return None

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return results
        return []
