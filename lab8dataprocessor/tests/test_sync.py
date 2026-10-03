import gc
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

try:
    import httpx  # noqa: F401
    import pydantic_settings  # noqa: F401
except ModuleNotFoundError:
    # The unit uses fake API clients; keep it runnable without the optional
    # runtime HTTP/configuration dependencies installed.
    for module_name, class_name in (
        ("bambuddy_inventree_sync.bambuddy", "BambuddyClient"),
        ("bambuddy_inventree_sync.config", "Settings"),
        ("bambuddy_inventree_sync.inventree", "InvenTreeClient"),
    ):
        module = ModuleType(module_name)
        setattr(module, class_name, object)
        sys.modules[module_name] = module

from bambuddy_inventree_sync.database import Database
from bambuddy_inventree_sync.models import Archive
from bambuddy_inventree_sync.sync import ArchiveSyncService


class FakeInvenTree:
    def __init__(self) -> None:
        self.created_stock_items = []
        self.added_stock = []

    async def create_stock_item(self, **kwargs):
        self.created_stock_items.append(kwargs)
        return {"pk": 900}

    async def find_stock_location_by_path(self, path):
        return {"pk": 10, "pathstring": path}

    async def list_stock_items_at_location(self, location_id):
        return [{
            "pk": 700,
            "purchase_price": "2",
            "part_detail": {"IPN": "B1", "name": "BAMBULAB X1C", "units": "min"},
        }]

    async def add_stock_quantity(self, **kwargs):
        self.added_stock.append(kwargs)
        return {"task": 1}


class FakeBuildOrders:
    async def complete_archive_output(self, archive, *, purchase_price, notes):
        return None


class ArchiveSyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "sync.sqlite3")
        self.database.init()
        self.inventree = FakeInvenTree()
        self.settings = SimpleNamespace(
            sync_success_only=True,
            legacy_finished_stock_enabled=False,
            filament_deduction_enabled=True,
            default_stock_quantity=1,
            filament_equipment_location_path="EQIPMENT",
        )
        self.service = ArchiveSyncService(
            settings=self.settings,
            database=self.database,
            bambuddy=object(),
            inventree=self.inventree,
            build_orders=FakeBuildOrders(),
        )
        self.service.purchase_price_for_archive = AsyncMock(return_value=(4.2, "price"))
        self.service._deduct_filament_for_archive = AsyncMock(return_value="filament deducted")
        self.service.track_printer_time = AsyncMock(return_value="printer time added")

    async def asyncTearDown(self):
        gc.collect()
        self.tempdir.cleanup()

    async def test_direct_print_does_not_create_finished_stock_item(self):
        result = await self.service._sync_archive(Archive(id=501, status="completed"))

        self.assertEqual(result.status, "synced")
        self.assertIsNone(result.stock_item_id)
        self.assertEqual(self.inventree.created_stock_items, [])
        self.service._deduct_filament_for_archive.assert_awaited_once()
        self.service.track_printer_time.assert_awaited_once()
        record = self.database.get_record(501)
        self.assertEqual(record["sync_status"], "synced")
        self.assertIsNone(record["stock_item_id"])

    async def test_printer_minutes_are_rounded_for_inventree_quantity(self):
        self.service.track_printer_time = ArchiveSyncService.track_printer_time.__get__(self.service)

        await self.service.track_printer_time(
            Archive(id=502, status="completed", printer_name="B1", duration=1),
        )

        self.assertEqual(self.inventree.added_stock[0]["quantity"], 0.0167)


if __name__ == "__main__":
    unittest.main()
