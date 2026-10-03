import tempfile
import unittest
import gc
from pathlib import Path
from types import SimpleNamespace

from bambuddy_inventree_sync.build_orders import BuildOrderService
from bambuddy_inventree_sync.database import Database
from bambuddy_inventree_sync.http_errors import BuildOrderError
from bambuddy_inventree_sync.models import Archive, BuildOrderQueueRequest


class FakeInvenTree:
    def __init__(self) -> None:
        self.build = {
            "pk": 42,
            "reference": "BO-0042",
            "title": "Test print",
            "part": 7,
            "part_name": "PRINTED_PART",
            "quantity": 4,
            "completed": 0,
            "status": 10,
            "status_text": "Pending",
            "destination": 12,
        }
        self.issued = False
        self.created_outputs: list[dict] = []
        self.completed_outputs: list[dict] = []
        self.updated_prices: list[tuple[int, float]] = []

    async def get_build_order(self, build_order_id):
        assert build_order_id == 42
        return dict(self.build)

    async def issue_build_order(self, build_order_id):
        self.issued = True
        self.build["status"] = 20
        self.build["status_text"] = "Production"

    async def list_part_attachments(self, part_id):
        assert part_id == 7
        return [{"pk": 5, "filename": "PRINTED_PART.3mf", "attachment": "http://example/file.3mf"}]

    async def download_attachment(self, attachment):
        return b"3mf test payload", "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"

    async def create_build_output(self, **kwargs):
        self.created_outputs.append(kwargs)
        return {"pk": 800}

    async def find_stock_by_batch(self, *, part_id, batch):
        return None

    async def update_stock_purchase_price(self, stock_item_id, purchase_price):
        self.updated_prices.append((stock_item_id, purchase_price))

    async def complete_build_output(self, **kwargs):
        self.completed_outputs.append(kwargs)
        return {"task": 1}

    async def finish_build_order(self, build_order_id):
        self.build["status"] = 40


class FakeBambuddy:
    def __init__(self) -> None:
        self.files: list[dict] = []
        self.batches: list[dict] = []
        self.queue: list[dict] = []

    async def list_printers(self):
        return [{"id": 1, "name": "B1", "model": "X1C"}]

    async def list_library_files(self):
        return list(self.files)

    async def upload_library_file(self, **kwargs):
        item = {"id": 3, "filename": kwargs["filename"]}
        self.files.append(item)
        return item

    async def list_print_batches(self):
        return list(self.batches)

    async def create_print_batch(self, payload):
        item = {"id": 9, "status": "active", **payload}
        self.batches.append(item)
        return item

    async def get_print_batch(self, batch_id):
        return next(item for item in self.batches if item["id"] == batch_id)

    async def add_queue_item(self, payload):
        item = {
            "id": 100 + len(self.queue),
            "status": "pending",
            "archive_id": None,
            "printer_name": "B1" if payload.get("printer_id") else None,
            **payload,
        }
        self.queue.append(item)
        return item

    async def list_queue_items(self):
        return list(self.queue)

    async def get_printer_status(self, printer_id):
        return {"progress": 50, "remaining_time": 10}


class BuildOrderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "sync.sqlite3")
        self.database.init()
        self.inventree = FakeInvenTree()
        self.bambuddy = FakeBambuddy()
        settings = SimpleNamespace(
            build_order_sync_enabled=True,
            build_order_auto_complete=True,
            inventree_stock_location_id=99,
            inventree_stock_status=10,
        )
        self.service = BuildOrderService(
            settings=settings,
            database=self.database,
            bambuddy=self.bambuddy,
            inventree=self.inventree,
        )

    async def asyncTearDown(self):
        gc.collect()
        self.tempdir.cleanup()

    async def test_enqueue_creates_one_queue_item_per_plate_run(self):
        result = await self.service.enqueue(
            42,
            BuildOrderQueueRequest(units_per_run=2, printer_id=1),
        )

        self.assertTrue(self.inventree.issued)
        self.assertEqual(result["mapping"]["planned_runs"], 2)
        self.assertEqual(len(self.bambuddy.queue), 2)
        self.assertEqual([item["quantity"] for item in self.bambuddy.queue], [1, 1])
        self.assertFalse(self.bambuddy.queue[0]["require_previous_success"])
        self.assertTrue(self.bambuddy.queue[1]["require_previous_success"])

        duplicate = await self.service.enqueue(
            42,
            BuildOrderQueueRequest(units_per_run=2, printer_id=1),
        )
        self.assertEqual(duplicate["mapping"]["batch_id"], 9)
        self.assertEqual(len(self.bambuddy.queue), 2)

    async def test_non_divisible_quantity_requires_explicit_overproduction(self):
        self.inventree.build["quantity"] = 3
        with self.assertRaises(BuildOrderError):
            await self.service.enqueue(42, BuildOrderQueueRequest(units_per_run=2))

    async def test_completed_queue_item_becomes_build_output_once(self):
        await self.service.enqueue(42, BuildOrderQueueRequest(units_per_run=2, printer_id=1))
        self.bambuddy.queue[0]["status"] = "completed"
        self.bambuddy.queue[0]["archive_id"] = 501
        await self.service.status(42)

        result = await self.service.complete_archive_output(
            Archive(id=501, status="completed"),
            purchase_price=12.5,
            notes="Bambuddy archive 501",
        )
        self.assertEqual(result["stock_item_id"], 800)
        self.assertEqual(self.inventree.created_outputs[0]["quantity"], 2)
        self.assertEqual(self.inventree.updated_prices, [(800, 12.5)])
        self.assertEqual(len(self.inventree.completed_outputs), 1)

        await self.service.complete_archive_output(
            Archive(id=501, status="completed"),
            purchase_price=12.5,
            notes="Bambuddy archive 501",
        )
        self.assertEqual(len(self.inventree.created_outputs), 1)
        self.assertEqual(len(self.inventree.completed_outputs), 1)

    async def test_printing_queue_item_appears_as_incomplete_output(self):
        await self.service.enqueue(42, BuildOrderQueueRequest(units_per_run=2, printer_id=1))
        self.bambuddy.queue[0]["status"] = "printing"

        first = await self.service.reconcile()
        second = await self.service.reconcile()

        self.assertEqual(first["incomplete_outputs_created"], 1)
        self.assertEqual(second["incomplete_outputs_created"], 0)
        self.assertEqual(len(self.inventree.created_outputs), 1)
        self.assertEqual(self.inventree.created_outputs[0]["quantity"], 2)
        queue_item = self.database.list_build_queue_items(42)[0]
        self.assertEqual(queue_item["build_output_stock_item_id"], 800)
        self.assertEqual(queue_item["output_status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
