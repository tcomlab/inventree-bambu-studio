from __future__ import annotations

import asyncio
import hashlib
from pathlib import PurePosixPath
from typing import Any, TYPE_CHECKING

from .database import Database
from .http_errors import BuildOrderError, ExternalApiError
from .models import Archive, BuildOrderQueueRequest

if TYPE_CHECKING:
    from .bambuddy import BambuddyClient
    from .config import Settings
    from .inventree import InvenTreeClient
    from .sync import ArchiveSyncService


class BuildOrderService:
    """Coordinate InvenTree Build Orders with Bambuddy batches and queue items."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        bambuddy: BambuddyClient,
        inventree: InvenTreeClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.bambuddy = bambuddy
        self.inventree = inventree
        self.archive_sync: ArchiveSyncService | None = None
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        build_order_id: int,
        options: BuildOrderQueueRequest,
    ) -> dict[str, Any]:
        if not self.settings.build_order_sync_enabled:
            raise BuildOrderError("Build Order synchronization is disabled")

        async with self._lock:
            build = await self.inventree.get_build_order(build_order_id)
            status = int(build.get("status") or 0)
            if status not in {10, 20}:
                raise BuildOrderError(
                    f"Build Order must be Pending or Production, current status is {build.get('status_text') or status}"
                )

            total_quantity = self._whole_quantity(build.get("quantity"), "Build Order quantity")
            completed_quantity = self._whole_quantity(build.get("completed") or 0, "completed quantity")
            remaining_quantity = total_quantity - completed_quantity
            if remaining_quantity <= 0:
                raise BuildOrderError("Build Order does not have any remaining quantity")

            runs, remainder = divmod(remaining_quantity, options.units_per_run)
            if remainder:
                if not options.allow_overproduction:
                    raise BuildOrderError(
                        f"Remaining quantity {remaining_quantity} is not divisible by "
                        f"{options.units_per_run} units per run"
                    )
                runs += 1

            existing = self.database.get_build_order_record(build_order_id)
            if existing:
                self._validate_existing_plan(existing, options, remaining_quantity, runs)
                await self._ensure_queue_items(existing, options)
                return await self.status(build_order_id)

            part_id = int(build.get("part") or 0)
            if part_id <= 0:
                raise BuildOrderError("Build Order has no printable Part")

            attachment = await self._latest_3mf_attachment(part_id)
            library_file_id = await self._ensure_library_file(attachment)
            reference = str(build.get("reference") or f"BO-{build_order_id}").strip()
            part_name = str(build.get("part_name") or (build.get("part_detail") or {}).get("name") or part_id)
            marker = f"inventree_build_id={build_order_id}"
            batch_payload: dict[str, Any] = {
                "name": f"[{reference}] {part_name}"[:200],
                "library_file_id": library_file_id,
                "notes": f"{marker}; reference={reference}; units_per_run={options.units_per_run}",
                "plates": [
                    {
                        "plate_id": options.plate_id,
                        "plate_name": options.plate_name,
                        "quantity_target": runs,
                        "sort_order": 0,
                    }
                ],
            }
            if build.get("target_date"):
                batch_payload["due_date"] = f"{build['target_date']}T23:59:59"

            batch = await self._find_batch_by_marker(marker)
            if not batch:
                batch = await self.bambuddy.create_print_batch(batch_payload)
            batch_id = int(batch.get("id") or 0)
            if batch_id <= 0:
                raise ExternalApiError(f"Bambuddy did not return a batch ID: {batch!r}")

            self.database.upsert_build_order_record(
                build_order_id=build_order_id,
                build_reference=reference,
                part_id=part_id,
                bambuddy_batch_id=batch_id,
                bambuddy_library_file_id=library_file_id,
                inventree_attachment_id=int(attachment.get("pk") or attachment.get("id")),
                units_per_run=options.units_per_run,
                plate_id=options.plate_id,
                planned_quantity=remaining_quantity,
                planned_runs=runs,
                sync_status="queued",
            )
            record = self.database.get_build_order_record(build_order_id)
            if not record:
                raise RuntimeError("Build Order mapping was not saved")
            await self._ensure_queue_items(record, options)

            if status == 10:
                await self.inventree.issue_build_order(build_order_id)

            return await self.status(build_order_id)

    async def status(self, build_order_id: int) -> dict[str, Any]:
        build = await self.inventree.get_build_order(build_order_id)
        record = self.database.get_build_order_record(build_order_id)
        printers = await self.bambuddy.list_printers()
        printer_choices = [
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or item["id"]),
                "model": item.get("model"),
            }
            for item in printers
            if item.get("id") is not None
        ]

        response: dict[str, Any] = {
            "configured": bool(record),
            "build": self._build_summary(build),
            "printers": printer_choices,
            "batch": None,
            "queue_items": [],
            "progress": {
                "planned_pieces": 0,
                "completed_pieces": int(build.get("completed") or 0),
                "pending_runs": 0,
                "printing_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "percent": 0.0,
            },
        }
        if not record:
            attachments = await self.inventree.list_part_attachments(int(build.get("part") or 0))
            response["has_3mf"] = any(self._attachment_filename(item).lower().endswith(".3mf") for item in attachments)
            return response

        batch = await self.bambuddy.get_print_batch(int(record["bambuddy_batch_id"]))
        all_queue_items = await self.bambuddy.list_queue_items()
        batch_items = [
            item for item in all_queue_items
            if int(item.get("batch_id") or 0) == int(record["bambuddy_batch_id"])
        ]
        local_by_id = {
            int(item["queue_item_id"]): item
            for item in self.database.list_build_queue_items(build_order_id)
        }
        printer_statuses = await self._printer_statuses(batch_items)

        rendered_items: list[dict[str, Any]] = []
        status_counts = {key: 0 for key in ("pending", "printing", "completed", "failed", "cancelled", "skipped")}
        completed_pieces = 0
        for item in sorted(batch_items, key=lambda value: int(value.get("id") or 0)):
            queue_item_id = int(item.get("id") or 0)
            local = local_by_id.get(queue_item_id)
            if not local:
                continue
            queue_status = str(item.get("status") or "pending")
            status_counts[queue_status] = status_counts.get(queue_status, 0) + 1
            archive_id = self._optional_int(item.get("archive_id"))
            self.database.upsert_build_queue_item(
                queue_item_id=queue_item_id,
                build_order_id=build_order_id,
                bambuddy_batch_id=int(record["bambuddy_batch_id"]),
                run_number=int(local["run_number"]),
                planned_quantity=int(local["planned_quantity"]),
                queue_status=queue_status,
                archive_id=archive_id,
                build_output_stock_item_id=self._optional_int(local.get("build_output_stock_item_id")),
                error=item.get("error_message"),
            )
            if queue_status == "completed":
                completed_pieces += int(local["planned_quantity"])

            live = printer_statuses.get(int(item.get("printer_id") or 0), {})
            if queue_status != "printing":
                live = {}
            rendered_items.append({
                "id": queue_item_id,
                "run_number": int(local["run_number"]),
                "pieces": int(local["planned_quantity"]),
                "status": queue_status,
                "printer_id": item.get("printer_id"),
                "printer_name": item.get("printer_name"),
                "archive_id": archive_id,
                "error": item.get("error_message") or item.get("waiting_reason"),
                "progress": live.get("progress"),
                "remaining_time": live.get("remaining_time"),
                "layer_num": live.get("layer_num"),
                "total_layers": live.get("total_layers"),
                "output_stock_item_id": local.get("build_output_stock_item_id"),
            })

        build_total = self._whole_quantity(build.get("quantity"), "Build Order quantity")
        planned_pieces = build_total
        baseline_completed = max(0, build_total - int(record["planned_quantity"]))
        completed_pieces += baseline_completed
        response.update({
            "configured": True,
            "mapping": {
                "build_order_id": build_order_id,
                "batch_id": int(record["bambuddy_batch_id"]),
                "library_file_id": int(record["bambuddy_library_file_id"]),
                "units_per_run": int(record["units_per_run"]),
                "plate_id": record.get("plate_id"),
                "planned_runs": int(record["planned_runs"]),
                "sync_status": record.get("sync_status"),
                "error": record.get("error"),
            },
            "batch": batch,
            "queue_items": rendered_items,
            "progress": {
                "planned_pieces": planned_pieces,
                "completed_pieces": completed_pieces,
                "pending_runs": status_counts.get("pending", 0),
                "printing_runs": status_counts.get("printing", 0),
                "completed_runs": status_counts.get("completed", 0),
                "failed_runs": status_counts.get("failed", 0),
                "cancelled_runs": status_counts.get("cancelled", 0),
                "percent": round(min(100.0, completed_pieces * 100.0 / max(1, planned_pieces)), 1),
            },
        })
        return response

    async def reconcile(self) -> dict[str, int]:
        counts = {
            "seen": 0,
            "incomplete_outputs_created": 0,
            "outputs_synced": 0,
            "failed": 0,
            "completed": 0,
        }
        if not self.settings.build_order_sync_enabled:
            return counts

        async with self._lock:
            for record in self.database.list_active_build_order_records():
                counts["seen"] += 1
                build_order_id = int(record["build_order_id"])
                try:
                    state = await self.status(build_order_id)
                    build = await self.inventree.get_build_order(build_order_id)
                    counts["incomplete_outputs_created"] += await self._ensure_printing_outputs(
                        build_order_id,
                        build,
                    )

                    for item in self.database.list_build_queue_items(build_order_id):
                        if (
                            item.get("queue_status") == "completed"
                            and item.get("archive_id")
                            and item.get("output_status") not in {"printed_incomplete", "completion_submitted"}
                            and self.archive_sync is not None
                        ):
                            await self.archive_sync.sync_archive_id(int(item["archive_id"]))
                            counts["outputs_synced"] += 1

                    if int(build.get("status") or 0) == 30:
                        self.database.update_build_order_status(build_order_id, "cancelled")
                    elif int(build.get("status") or 0) == 40:
                        self.database.update_build_order_status(build_order_id, "complete")
                        counts["completed"] += 1
                except Exception as exc:
                    self.database.update_build_order_status(build_order_id, "failed", str(exc))
                    counts["failed"] += 1
        return counts

    async def find_queue_item_for_archive(self, archive_id: int) -> dict[str, Any] | None:
        local = self.database.get_build_queue_item_by_archive(archive_id)
        if local:
            return local

        for item in await self.bambuddy.list_queue_items():
            if self._optional_int(item.get("archive_id")) != archive_id:
                continue
            batch_id = self._optional_int(item.get("batch_id"))
            if batch_id is None:
                return None
            record = next(
                (
                    value for value in self.database.list_active_build_order_records()
                    if int(value["bambuddy_batch_id"]) == batch_id
                ),
                None,
            )
            if not record:
                return None
            known = {
                int(value["queue_item_id"]): value
                for value in self.database.list_build_queue_items(int(record["build_order_id"]))
            }.get(int(item["id"]))
            if not known:
                return None
            self.database.upsert_build_queue_item(
                queue_item_id=int(item["id"]),
                build_order_id=int(record["build_order_id"]),
                bambuddy_batch_id=batch_id,
                run_number=int(known["run_number"]),
                planned_quantity=int(known["planned_quantity"]),
                queue_status=str(item.get("status") or "completed"),
                archive_id=archive_id,
                build_output_stock_item_id=self._optional_int(known.get("build_output_stock_item_id")),
                error=item.get("error_message"),
            )
            return self.database.get_build_queue_item_by_archive(archive_id)
        return None

    async def complete_archive_output(
        self,
        archive: Archive,
        *,
        purchase_price: float | None,
        notes: str,
    ) -> dict[str, Any] | None:
        item = await self.find_queue_item_for_archive(archive.id)
        if not item:
            return None

        build_order_id = int(item["build_order_id"])
        build = await self.inventree.get_build_order(build_order_id)
        stock_item_id, output_status = await self._ensure_build_output(build, item)

        if purchase_price is not None:
            await self.inventree.update_stock_purchase_price(stock_item_id, purchase_price)

        # A successful print remains an Incomplete Output. The operator completes
        # it manually in InvenTree and chooses the final Stock Location there.
        # Mark the archive as processed locally so the reconcile loop does not
        # submit the same finished print again.
        if output_status not in {"printed_incomplete", "completion_submitted"}:
            self.database.set_build_output_stock_item(
                int(item["queue_item_id"]), stock_item_id, "printed_incomplete"
            )

        return {
            "build_order_id": build_order_id,
            "part_id": int(build.get("part") or 0),
            "part_key": str(build.get("reference") or f"BO-{build_order_id}"),
            "stock_item_id": stock_item_id,
        }

    async def _ensure_printing_outputs(
        self,
        build_order_id: int,
        build: dict[str, Any],
    ) -> int:
        """Create one incomplete InvenTree output as soon as each run starts."""
        created = 0
        for item in self.database.list_build_queue_items(build_order_id):
            if item.get("queue_status") != "printing":
                continue
            if self._optional_int(item.get("build_output_stock_item_id")) is not None:
                continue
            await self._ensure_build_output(build, item)
            created += 1
        return created

    async def _ensure_build_output(
        self,
        build: dict[str, Any],
        item: dict[str, Any],
    ) -> tuple[int, str]:
        stock_item_id = self._optional_int(item.get("build_output_stock_item_id"))
        output_status = str(item.get("output_status") or "")
        if stock_item_id is not None:
            return stock_item_id, output_status

        build_order_id = int(item["build_order_id"])
        batch_code = self._output_batch_code(build, item)
        existing = await self.inventree.find_stock_by_batch(
            part_id=int(build.get("part") or 0),
            batch=batch_code,
        )
        if existing:
            stock_item_id = int(existing.get("pk") or existing.get("id"))
        else:
            output = await self.inventree.create_build_output(
                build_order_id=build_order_id,
                quantity=int(item["planned_quantity"]),
                batch_code=batch_code,
                location_id=int(build.get("destination") or self.settings.inventree_stock_location_id),
            )
            stock_item_id = int(output.get("pk") or output.get("id"))

        self.database.set_build_output_stock_item(
            int(item["queue_item_id"]),
            stock_item_id,
            "incomplete",
        )
        return stock_item_id, "incomplete"

    @staticmethod
    def _output_batch_code(build: dict[str, Any], item: dict[str, Any]) -> str:
        reference = str(build.get("reference") or f"BO-{item['build_order_id']}").strip()
        suffix = f"-R{int(item['run_number']):03d}-Q{int(item['queue_item_id'])}"
        return f"{reference[:max(1, 100 - len(suffix))]}{suffix}"

    async def _ensure_queue_items(
        self,
        record: dict[str, Any],
        options: BuildOrderQueueRequest,
    ) -> None:
        existing = self.database.list_build_queue_items(int(record["build_order_id"]))
        existing_runs = {int(item["run_number"]) for item in existing}
        for run_number in range(1, int(record["planned_runs"]) + 1):
            if run_number in existing_runs:
                continue
            payload: dict[str, Any] = {
                "library_file_id": int(record["bambuddy_library_file_id"]),
                "batch_id": int(record["bambuddy_batch_id"]),
                "plate_id": record.get("plate_id"),
                "quantity": 1,
                "manual_start": options.manual_start,
                "require_previous_success": run_number > 1,
            }
            if options.printer_id is not None:
                payload["printer_id"] = options.printer_id
            if options.target_model:
                payload["target_model"] = options.target_model
            queue_item = await self.bambuddy.add_queue_item(payload)
            queue_item_id = int(queue_item.get("id") or 0)
            if queue_item_id <= 0:
                raise ExternalApiError(f"Bambuddy did not return a queue item ID: {queue_item!r}")
            self.database.upsert_build_queue_item(
                queue_item_id=queue_item_id,
                build_order_id=int(record["build_order_id"]),
                bambuddy_batch_id=int(record["bambuddy_batch_id"]),
                run_number=run_number,
                planned_quantity=int(record["units_per_run"]),
                queue_status=str(queue_item.get("status") or "pending"),
                archive_id=self._optional_int(queue_item.get("archive_id")),
                error=queue_item.get("error_message"),
            )

    async def _latest_3mf_attachment(self, part_id: int) -> dict[str, Any]:
        attachments = await self.inventree.list_part_attachments(part_id)
        for attachment in attachments:
            if self._attachment_filename(attachment).lower().endswith(".3mf"):
                return attachment
        raise BuildOrderError("The Build Order Part has no .3mf attachment")

    async def _ensure_library_file(self, attachment: dict[str, Any]) -> int:
        attachment_id = int(attachment.get("pk") or attachment.get("id"))
        filename = self._attachment_filename(attachment)
        content, content_type = await self.inventree.download_attachment(attachment)
        content_hash = hashlib.sha256(content).hexdigest()
        mapped = self.database.get_library_file_map(attachment_id)
        if mapped and mapped.get("content_hash") == content_hash:
            library_id = int(mapped["bambuddy_library_file_id"])
            files = await self.bambuddy.list_library_files()
            if any(int(item.get("id") or 0) == library_id for item in files):
                return library_id

        uploaded = await self.bambuddy.upload_library_file(
            filename=filename,
            content=content,
            content_type=content_type,
        )
        library_file_id = int(uploaded.get("duplicate_of") or uploaded.get("id") or 0)
        if library_file_id <= 0:
            raise ExternalApiError(f"Bambuddy did not return a library file ID: {uploaded!r}")
        self.database.upsert_library_file_map(
            attachment_id=attachment_id,
            library_file_id=library_file_id,
            filename=filename,
            content_hash=content_hash,
        )
        return library_file_id

    async def _find_batch_by_marker(self, marker: str) -> dict[str, Any] | None:
        for batch in await self.bambuddy.list_print_batches():
            if marker in str(batch.get("notes") or ""):
                return batch
        return None

    async def _printer_statuses(self, queue_items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        printer_ids = {
            int(item["printer_id"])
            for item in queue_items
            if item.get("status") == "printing" and item.get("printer_id") is not None
        }
        if not printer_ids:
            return {}
        values = await asyncio.gather(
            *(self.bambuddy.get_printer_status(printer_id) for printer_id in printer_ids),
            return_exceptions=True,
        )
        return {
            printer_id: value
            for printer_id, value in zip(printer_ids, values)
            if isinstance(value, dict)
        }

    @staticmethod
    def _validate_existing_plan(
        record: dict[str, Any],
        options: BuildOrderQueueRequest,
        planned_quantity: int,
        planned_runs: int,
    ) -> None:
        expected = (
            int(record["units_per_run"]),
            record.get("plate_id"),
            int(record["planned_quantity"]),
            int(record["planned_runs"]),
        )
        requested = (options.units_per_run, options.plate_id, planned_quantity, planned_runs)
        if expected != requested:
            raise BuildOrderError(
                "This Build Order already has a Bambuddy plan with different quantity or plate settings"
            )

    @staticmethod
    def _whole_quantity(value: Any, label: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise BuildOrderError(f"{label} is invalid") from exc
        if number < 0 or not number.is_integer():
            raise BuildOrderError(f"{label} must be a whole number of parts")
        return int(number)

    @staticmethod
    def _attachment_filename(attachment: dict[str, Any]) -> str:
        filename = str(attachment.get("filename") or "").strip()
        if filename:
            return PurePosixPath(filename).name
        url = str(attachment.get("attachment") or "")
        return PurePosixPath(url.split("?", 1)[0]).name

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_summary(build: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(build.get("pk") or build.get("id") or 0),
            "reference": build.get("reference"),
            "title": build.get("title"),
            "part": build.get("part"),
            "part_name": build.get("part_name"),
            "quantity": build.get("quantity"),
            "completed": build.get("completed"),
            "status": build.get("status"),
            "status_text": build.get("status_text"),
            "target_date": build.get("target_date"),
            "destination": build.get("destination"),
        }
