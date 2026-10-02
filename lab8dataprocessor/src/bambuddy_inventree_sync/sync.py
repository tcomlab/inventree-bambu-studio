import asyncio
import logging
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, TYPE_CHECKING

from .bambuddy import BambuddyClient
from .config import Settings
from .database import Database
from .http_errors import ExternalApiError
from .inventree import InvenTreeClient
from .models import Archive, SyncResult

if TYPE_CHECKING:
    from .build_orders import BuildOrderService

logger = logging.getLogger(__name__)


class ArchiveSyncService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        bambuddy: BambuddyClient,
        inventree: InvenTreeClient,
        build_orders: "BuildOrderService | None" = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.bambuddy = bambuddy
        self.inventree = inventree
        self.build_orders = build_orders
        self._lock = asyncio.Lock()
        self._printer_name_cache: dict[int, str] | None = None

    async def validate_targets(self) -> dict[str, Any]:
        category, location, bambuddy_info = await asyncio.gather(
            self.inventree.get_part_category(),
            self.inventree.get_stock_location(),
            self.bambuddy.get_system_info(),
        )
        return {"part_category": category, "stock_location": location, "bambuddy": bambuddy_info}

    async def reconcile_filament_inventory(self) -> dict[str, int]:
        """Mirror every InvenTree filament StockItem to one Bambuddy spool."""
        parts = await self.inventree.list_filament_parts()
        spools = await self.bambuddy.list_spools()
        spool_by_id = {int(s["id"]): s for s in spools if s.get("id") is not None}
        mapped = self.database.get_stock_spool_map()
        printers = {str(p.get("name") or "").strip().upper(): int(p["id"]) for p in await self.bambuddy.list_printers() if p.get("id") is not None}
        desired_slots: dict[tuple[int, int, int], int] = {}
        counts = {"created": 0, "updated": 0, "assigned": 0, "conflicts": 0, "legacy_deleted": 0}
        managed_spool_ids: set[int] = set(mapped.values())

        for part in parts:
            part_id = int(part.get("pk") or part.get("id"))
            stock_items = await self.inventree.list_stock_items_for_part(part_id)
            fields = self.filament_fields_for_part(part)
            nominal_weight = self.nominal_filament_weight(part)
            for item in stock_items:
                stock_item_id = int(item.get("pk") or item.get("id"))
                batch = str(item.get("batch") or f"SP-{stock_item_id:06d}").strip()
                quantity = max(0.0, self.stock_quantity(item))
                location_detail = item.get("location_detail") or {}
                path = str(location_detail.get("pathstring") or "")
                storage_location = str(location_detail.get("name") or "").strip()
                if item.get("location") is not None and (not path or not storage_location):
                    location_detail = await self.inventree.get_stock_location_by_id(int(item["location"]))
                    path = str(location_detail.get("pathstring") or path)
                    storage_location = str(location_detail.get("name") or "").strip()
                if not storage_location and path:
                    storage_location = path.strip("/").rsplit("/", 1)[-1]
                try:
                    price_per_gram = float(item.get("purchase_price"))
                except (TypeError, ValueError):
                    price_per_gram = 0.0
                spool = spool_by_id.get(mapped.get(stock_item_id, -1))
                label_weight = max(nominal_weight, int(round(quantity)))
                weight_used = max(0.0, label_weight - quantity)
                payload = {
                    **fields,
                    "category": None,
                    "label_weight": label_weight,
                    "weight_used": weight_used,
                    "core_weight": self.settings.filament_default_core_weight,
                    "core_weight_catalog_id": self.settings.filament_core_weight_catalog_id,
                    "cost_per_kg": price_per_gram * 1000 if price_per_gram > 0 else None,
                    "slicer_filament": batch,
                    "slicer_filament_name": f"{batch} · {part.get('name') or part.get('IPN') or ''}",
                    "note": (
                        f"{batch} stock_item_id={stock_item_id} "
                        f"part_id={part_id}; IPN={part.get('IPN') or ''}"
                        + ("; EMPTY" if quantity <= 0 else "")
                    ),
                }
                if storage_location:
                    payload["storage_location"] = storage_location
                if spool:
                    await self.bambuddy.update_spool(int(spool["id"]), payload)
                    spool_id = int(spool["id"])
                    counts["updated"] += 1
                else:
                    created = await self.bambuddy.create_spool(payload)
                    spool_id = int(created["id"])
                    self.database.upsert_stock_spool_map(
                        stock_item_id=stock_item_id, part_id=part_id, spool_id=spool_id, batch=batch,
                    )
                    counts["created"] += 1
                managed_spool_ids.add(spool_id)

                slot = self.loaded_slot_for_path(path, printers)
                if slot and self.stock_quantity(item) > 0:
                    if slot in desired_slots and desired_slots[slot] != spool_id:
                        counts["conflicts"] += 1
                    else:
                        desired_slots[slot] = spool_id

        current = {(int(a["printer_id"]), int(a["ams_id"]), int(a["tray_id"])): int(a["spool_id"]) for a in await self.bambuddy.list_assignments()}
        for slot, spool_id in desired_slots.items():
            if current.get(slot) == spool_id:
                continue
            if slot in current:
                await self.bambuddy.unassign_spool(printer_id=slot[0], ams_id=slot[1], tray_id=slot[2])
            await self.bambuddy.assign_spool(spool_id=spool_id, printer_id=slot[0], ams_id=slot[1], tray_id=slot[2])
            counts["assigned"] += 1
        for slot in current.keys() - desired_slots.keys():
            if current[slot] in managed_spool_ids:
                await self.bambuddy.unassign_spool(printer_id=slot[0], ams_id=slot[1], tray_id=slot[2])

        # Remove obsolete aggregate Part-level spools only after StockItem spools exist.
        for spool in spools:
            note = str(spool.get("note") or "")
            if "Managed by InvenTree; part_id=" in note and "stock_item_id=" not in note:
                await self.bambuddy.delete_spool(int(spool["id"]))
                counts["legacy_deleted"] += 1
        return counts

    async def assign_missing_filament_batches(self) -> dict[str, int]:
        """Give every new filament StockItem a stable SP code derived from its ID."""
        parts = await self.inventree.list_filament_parts()
        stock_items: list[dict[str, Any]] = []
        for part in parts:
            part_id = int(part.get("pk") or part.get("id"))
            stock_items.extend(await self.inventree.list_stock_items_for_part(part_id))

        assigned = 0
        for item in sorted(stock_items, key=lambda value: int(value.get("pk") or value.get("id"))):
            if str(item.get("batch") or "").strip():
                continue
            stock_item_id = int(item.get("pk") or item.get("id"))
            batch = f"SP-{stock_item_id:06d}"
            await self.inventree.update_stock_batch(stock_item_id, batch)
            logger.info("Assigned filament batch %s to InvenTree stock item %s", batch, stock_item_id)
            assigned += 1
        return {"seen": len(stock_items), "assigned": assigned}

    @staticmethod
    def loaded_slot_for_path(path: str, printers: dict[str, int]) -> tuple[int, int, int] | None:
        match = re.fullmatch(r"EQIPMENT/(B[1-4])/AMS[-_ ]*([1-4])", path.strip(), re.IGNORECASE)
        if match and match.group(1).upper() in printers:
            return printers[match.group(1).upper()], 0, int(match.group(2)) - 1
        match = re.fullmatch(r"EQIPMENT/(B[2-4])", path.strip(), re.IGNORECASE)
        if match and match.group(1).upper() in printers:
            # Bambuddy represents a printer's external spool holder as the
            # synthetic AMS id 255. AMS id 0 is a real AMS unit and therefore
            # does not match the External slot rendered by the printer UI.
            return printers[match.group(1).upper()], 255, 0
        return None

    @staticmethod
    def filament_fields_for_part(part: dict[str, Any]) -> dict[str, Any]:
        name = str(part.get("name") or "").strip()
        description = str(part.get("description") or "")
        material, _, color = name.partition("_")
        brand_match = re.search(r"(?:brand|manufacturer|бренд/постачальник)\s*:\s*([^;.]+)", description, re.IGNORECASE)
        normalized_color = re.sub(r"_REF$", "", color.upper())
        rgba_by_color = {
            "BLACK": "111111FF", "WHITE": "F5F5F5FF", "BLUE": "1565C0FF",
            "GREEN": "2E7D32FF", "RED": "C62828FF", "GRAY": "808080FF",
            "GREY": "808080FF", "ORANGE": "F57C00FF", "YELLOW": "FDD835FF",
            "KHAKI": "9A8F45FF", "BRONZE": "A97142FF", "NATURAL": "E8DCC4FF",
            "TRANSPARENT": "E8F4F880", "WOOD": "9B6A3CFF",
            "SILK_TRICOLOR_NN": "D4AF37FF",
        }
        rgba = rgba_by_color.get(normalized_color)
        return {
            "material": material or "UNKNOWN",
            "color_name": color or None,
            "rgba": rgba,
            "brand": brand_match.group(1).strip() if brand_match else None,
        }

    def nominal_filament_weight(self, part: dict[str, Any]) -> int:
        description = str(part.get("description") or "")
        match = re.search(
            r"(?:вага|weight)\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*(кг|kg|г|g)\b",
            description,
            re.IGNORECASE,
        )
        if match:
            value = float(match.group(1).replace(",", "."))
            if match.group(2).lower() in {"кг", "kg"}:
                value *= 1000
            if value > 0:
                return max(1, int(round(value)))
        return self.settings.filament_default_label_weight

    async def sync_archive_id(
        self,
        archive_id: int,
        *,
        force: bool = False,
        webhook_data: dict[str, Any] | None = None,
    ) -> SyncResult:
        async with self._lock:
            existing = self.database.get_record(archive_id)
            if existing and existing["sync_status"] == "synced" and not force:
                return SyncResult(
                        archive_id=archive_id,
                        status="already_synced",
                        message="Archive was already synced",
                        part_id=existing["part_id"],
                        stock_item_id=existing["stock_item_id"],
                        part_key=existing["part_key"],
                    )

            try:
                archive = await self._load_archive(archive_id, webhook_data)
                return await self._sync_archive(archive, force=force, existing_record=existing)
            except Exception as exc:
                self.database.upsert_record(
                    archive_id=archive_id,
                    sync_status="failed",
                    error=str(exc),
                    raw_archive=webhook_data,
                )
                raise

    async def backfill(self, *, status: str | None = None, max_archives: int | None = None) -> dict[str, int]:
        status_filter = status
        target_status = status.lower() if status else None

        counts = {"seen": 0, "synced": 0, "already_synced": 0, "skipped": 0, "failed": 0}
        offset = 0
        limit = self.settings.backfill_page_size

        while True:
            total, archives = await self.bambuddy.list_archives(status=status_filter, limit=limit, offset=offset)
            if not archives:
                break

            for archive in archives:
                if target_status and (archive.status or "").lower() != target_status:
                    continue

                if max_archives is not None and self._backfill_limit_reached(counts, max_archives):
                    return counts

                counts["seen"] += 1
                try:
                    result = await self.sync_archive_id(archive.id, webhook_data=archive.model_dump())
                    counts[result.status] = counts.get(result.status, 0) + 1
                except Exception:
                    logger.exception("Failed to sync Bambuddy archive %s during backfill", archive.id)
                    counts["failed"] += 1

                if max_archives is not None and self._backfill_limit_reached(counts, max_archives):
                    return counts

            offset += len(archives)
            if total is not None and offset >= total:
                break

        return counts

    async def refresh_purchase_prices(self) -> dict[str, int]:
        """Recalculate existing synced finished-goods StockItem unit prices."""
        counts = {"seen": 0, "updated": 0, "skipped": 0, "failed": 0}
        records: list[dict[str, Any]] = []
        # Build the work list from StockItems that still exist in the configured
        # finished-goods category. The local history also contains IDs of old
        # StockItems that users have already deleted during inventory cleanup.
        for item in await self.inventree.list_stock_items_for_category(self.settings.inventree_part_category_id):
            match = re.fullmatch(r"bambuddy-(\d+)", str(item.get("batch") or "").strip(), re.IGNORECASE)
            if not match:
                continue
            records.append({
                "archive_id": int(match.group(1)),
                "stock_item_id": int(item.get("pk") or item.get("id")),
            })

        for record in records:
            counts["seen"] += 1
            try:
                archive = await self.bambuddy.get_archive(int(record["archive_id"]))
                purchase_price, _ = await self.purchase_price_for_archive(archive)
                if purchase_price is None:
                    counts["skipped"] += 1
                    continue
                await self.inventree.update_stock_purchase_price(
                    int(record["stock_item_id"]),
                    purchase_price,
                )
                counts["updated"] += 1
            except Exception:
                logger.exception(
                    "Failed to refresh Purchase Price for archive %s / StockItem %s",
                    record["archive_id"],
                    record["stock_item_id"],
                )
                counts["failed"] += 1
        return counts

    async def _load_archive(self, archive_id: int, webhook_data: dict[str, Any] | None) -> Archive:
        try:
            return await self.bambuddy.get_archive(archive_id)
        except ExternalApiError:
            if webhook_data:
                data = dict(webhook_data)
                data.setdefault("id", archive_id)
                return Archive.model_validate(data)
            raise

    async def _sync_archive(
        self,
        archive: Archive,
        *,
        force: bool = False,
        existing_record: dict[str, Any] | None = None,
    ) -> SyncResult:
        if self.settings.sync_success_only and not self.is_successful_archive(archive):
            self.database.upsert_record(
                archive_id=archive.id,
                sync_status="skipped",
                archive_status=archive.status,
                raw_archive=archive.model_dump(),
            )
            return SyncResult(archive_id=archive.id, status="skipped", message=f"Archive status is {archive.status}")

        purchase_price, price_note = await self.purchase_price_for_archive(archive)
        deduction = await self._deduct_filament_for_archive(archive, force=force, existing_record=existing_record)

        if self.build_orders is not None:
            build_output = await self.build_orders.complete_archive_output(
                archive,
                purchase_price=purchase_price,
                notes=self.stock_notes_for_archive(archive, price_note=price_note),
            )
            if build_output:
                printer_time = await self.track_printer_time(archive)
                self.database.upsert_record(
                    archive_id=archive.id,
                    sync_status="synced",
                    part_key=str(build_output["part_key"]),
                    part_id=int(build_output["part_id"]),
                    stock_item_id=int(build_output["stock_item_id"]),
                    archive_status=archive.status,
                    raw_archive=archive.model_dump(),
                )
                return SyncResult(
                    archive_id=archive.id,
                    status="synced",
                    message=self._sync_message(
                        self._sync_message("Created InvenTree Build Output", deduction),
                        printer_time,
                    ),
                    part_id=int(build_output["part_id"]),
                    stock_item_id=int(build_output["stock_item_id"]),
                    part_key=str(build_output["part_key"]),
                )

        part_references = self.part_references_for_archive(archive)
        part = None
        part_reference = part_references[0]
        for candidate in part_references:
            part = await self.inventree.find_part_by_reference(candidate)
            if part:
                part_reference = candidate
                break

        if not part:
            references = ", ".join(f"'{candidate}'" for candidate in part_references)
            self.database.upsert_record(
                archive_id=archive.id,
                sync_status="skipped",
                part_key=part_reference,
                archive_status=archive.status,
                error=f"InvenTree part was not found for {references}",
                raw_archive=archive.model_dump(),
            )
            return SyncResult(
                archive_id=archive.id,
                status="skipped",
                message=self._sync_message(f"InvenTree part was not found for {references}", deduction),
                part_key=part_reference,
            )

        part_id = int(part.get("pk") or part.get("id"))
        batch = self.inventree.batch_for_archive(archive.id)
        existing_stock = await self.inventree.find_stock_by_batch(part_id=part_id, batch=batch)

        if existing_stock:
            stock_item_id = int(existing_stock.get("pk") or existing_stock.get("id"))
            printer_time = await self.track_printer_time(archive)
            self.database.upsert_record(
                archive_id=archive.id,
                sync_status="synced",
                part_key=part_reference,
                part_id=part_id,
                stock_item_id=stock_item_id,
                archive_status=archive.status,
                raw_archive=archive.model_dump(),
            )
            return SyncResult(
                archive_id=archive.id,
                status="already_synced",
                message=self._sync_message(
                    self._sync_message("Stock item already exists in InvenTree", deduction),
                    printer_time,
                ),
                part_id=part_id,
                stock_item_id=stock_item_id,
                part_key=part_reference,
            )

        stock_quantity = self.printed_quantity_for_archive(archive)
        stock = await self.inventree.create_stock_item(
            part_id=part_id,
            archive=archive,
            quantity=stock_quantity,
            notes=self.stock_notes_for_archive(archive, price_note=price_note),
            purchase_price=purchase_price,
        )
        stock_item_id = int(stock.get("pk") or stock.get("id"))
        printer_time = await self.track_printer_time(archive)

        self.database.upsert_record(
            archive_id=archive.id,
            sync_status="synced",
            part_key=part_reference,
            part_id=part_id,
            stock_item_id=stock_item_id,
            archive_status=archive.status,
            raw_archive=archive.model_dump(),
        )
        logger.info(
            "Synced Bambuddy archive %s to InvenTree part %s stock item %s",
            archive.id,
            part_id,
            stock_item_id,
        )
        return SyncResult(
            archive_id=archive.id,
            status="synced",
            message=self._sync_message(
                self._sync_message("Created stock item in InvenTree", deduction),
                printer_time,
            ),
            part_id=part_id,
            stock_item_id=stock_item_id,
            part_key=part_reference,
        )

    async def track_printer_time(self, archive: Archive) -> str:
        """Add this completed print's elapsed minutes to its printer StockItem once."""
        existing = self.database.get_printer_time_record(archive.id)
        if existing and existing.get("sync_status") == "synced":
            return f"printer time previously added: {self.format_number(float(existing['minutes']))} min"

        duration_seconds = self.duration_seconds_for_archive(archive)
        printer_name = await self.printer_name_for_archive(archive)
        if duration_seconds is None or duration_seconds <= 0 or not printer_name:
            return "printer time skipped: duration or printer is unavailable"

        minutes = duration_seconds / 60.0
        try:
            printer_item = await self.printer_stock_item(printer_name)
            if not printer_item:
                raise ExternalApiError(f"Printer StockItem for {printer_name} was not found")
            stock_item_id = int(printer_item.get("pk") or printer_item.get("id"))
            await self.inventree.add_stock_quantity(
                stock_item_id=stock_item_id,
                quantity=minutes,
                notes=f"Bambuddy archive {archive.id}; printer {printer_name}; print time {self.format_number(minutes)} min",
            )
            self.database.upsert_printer_time_record(
                archive_id=archive.id,
                printer_name=printer_name,
                stock_item_id=stock_item_id,
                minutes=minutes,
                sync_status="synced",
            )
            return f"added {self.format_number(minutes)} min to printer {printer_name}"
        except Exception as exc:
            self.database.upsert_printer_time_record(
                archive_id=archive.id,
                printer_name=printer_name,
                minutes=minutes,
                sync_status="failed",
                error=str(exc),
            )
            logger.exception("Failed to add printer time for archive %s", archive.id)
            return f"printer time failed: {exc}"

    async def purchase_price_for_archive(self, archive: Archive) -> tuple[float | None, str]:
        """Calculate the unit cost of a printed item in InvenTree currency."""
        quantity = self.filament_grams_for_archive(archive)
        duration_seconds = self.duration_seconds_for_archive(archive)
        printed_quantity = self.printed_quantity_for_archive(archive)
        printer_name = await self.printer_name_for_archive(archive)

        filament_cost = 0.0
        filament_priced = False
        # Archive.cost is the historical Bambuddy filament cost for this exact
        # run. Prefer it during backfill so today's loaded spool cannot change
        # the price of an older print.
        if archive.cost is not None:
            try:
                filament_cost = max(0.0, float(archive.cost))
                filament_priced = filament_cost > 0
            except (TypeError, ValueError):
                pass

        if not filament_priced and quantity and quantity > 0 and printer_name:
            try:
                _, stock_items = await self.filament_source_for_archive(archive, printer_name=printer_name)
                remaining = quantity
                for item in stock_items:
                    if remaining <= 0:
                        break
                    take = min(remaining, self.stock_quantity(item))
                    price_per_gram = self.stock_purchase_price(item)
                    if price_per_gram is not None:
                        filament_cost += take * price_per_gram
                        filament_priced = True
                    remaining -= take
            except ExternalApiError:
                logger.exception("Could not calculate filament cost for archive %s", archive.id)

        printer_rate = None
        if printer_name:
            printer_rate = await self.printer_price_per_minute(printer_name)
        print_minutes = max(0.0, duration_seconds / 60.0) if duration_seconds is not None else 0.0
        printer_cost = print_minutes * printer_rate if printer_rate is not None else 0.0

        if not filament_priced and printer_rate is None:
            return None, "Purchase price was not calculated: filament and printer prices are unavailable."

        total_cost = filament_cost + printer_cost
        unit_cost = round(total_cost / max(1, printed_quantity), 4)
        note = (
            f"Purchase price: ({self.format_number(quantity or 0)} g filament = "
            f"{self.format_number(filament_cost)}) + ({self.format_number(print_minutes)} min × "
            f"{self.format_number(printer_rate or 0)}/min = {self.format_number(printer_cost)}); "
            f"total {self.format_number(total_cost)} / {printed_quantity} item(s) = "
            f"{self.format_number(unit_cost)} per item."
        )
        return unit_cost, note

    async def printer_price_per_minute(self, printer_name: str) -> float | None:
        printer_item = await self.printer_stock_item(printer_name)
        return self.stock_purchase_price(printer_item) if printer_item else None

    async def printer_stock_item(self, printer_name: str) -> dict[str, Any] | None:
        root = self.settings.filament_equipment_location_path.strip().strip("/")
        location = await self.inventree.find_stock_location_by_path(f"{root}/{printer_name.strip().upper()}")
        if not location:
            return None
        location_id = int(location.get("pk") or location.get("id"))
        printer_key = printer_name.strip().upper()
        candidates: list[dict[str, Any]] = []
        for item in await self.inventree.list_stock_items_at_location(location_id):
            part = item.get("part_detail") or {}
            references = {
                str(part.get("IPN") or "").strip().upper(),
                str(part.get("name") or "").strip().upper(),
            }
            if printer_key in references:
                candidates.insert(0, item)
            elif str(part.get("units") or "").strip().lower() != "g":
                candidates.append(item)
        for item in candidates:
            price = self.stock_purchase_price(item)
            if price is not None:
                return item
        return None

    @staticmethod
    def stock_purchase_price(stock_item: dict[str, Any]) -> float | None:
        try:
            value = float(stock_item.get("purchase_price"))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @staticmethod
    def duration_seconds_for_archive(archive: Archive) -> float | None:
        for value in (archive.actual_time_seconds, archive.print_time_seconds, archive.duration):
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds >= 0:
                return seconds
        return None

    async def _deduct_filament_for_archive(
        self,
        archive: Archive,
        *,
        force: bool = False,
        existing_record: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.settings.filament_deduction_enabled:
            return None

        existing = self.database.get_filament_deduction_record(archive.id)
        if existing and existing.get("sync_status") == "synced":
            return "filament already deducted"
        if existing and existing.get("sync_status") == "failed" and not force:
            return "filament deduction requires explicit force after previous failure"
        if existing and existing.get("sync_status") != "failed" and not force:
            return f"filament deduction previously {existing.get('sync_status')}"

        if not existing and existing_record and not force and not self.should_auto_deduct_filament(existing_record):
            return "filament deduction skipped for pre-existing completed archive"

        quantity = self.filament_grams_for_archive(archive)
        if quantity is None or quantity <= 0:
            self.database.upsert_filament_deduction_record(
                archive_id=archive.id,
                sync_status="skipped",
                error="Archive does not contain positive filament usage",
                raw_archive=archive.model_dump(),
            )
            return "filament deduction skipped: no positive usage"

        try:
            printer_name = await self.printer_name_for_archive(archive)
            if not printer_name:
                raise ExternalApiError(f"Printer was not found for archive printer_id={archive.printer_id}")

            source_location, stock_items = await self.filament_source_for_archive(archive, printer_name=printer_name)
            stock_item_id = int(stock_items[0].get("pk") or stock_items[0].get("id"))
            source_location_id = int(source_location.get("pk") or source_location.get("id"))
            available = sum(self.stock_quantity(item) for item in stock_items)
            if quantity > available:
                raise ExternalApiError(
                    f"Filament stock item {stock_item_id} in {source_location.get('pathstring')} has {available:g} g, "
                    f"but archive {archive.id} needs {quantity:g} g"
                )

            remaining = quantity
            for stock_item in stock_items:
                if remaining <= 0:
                    break
                item_id = int(stock_item.get("pk") or stock_item.get("id"))
                take = min(remaining, self.stock_quantity(stock_item))
                await self.inventree.remove_stock_quantity(
                    stock_item_id=item_id,
                    quantity=take,
                    notes=(
                    f"Bambuddy archive {archive.id}; printer {printer_name}; "
                    f"material {archive.filament_type or 'unknown'}; color {archive.filament_color or 'unknown'}"
                    ),
                )
                remaining -= take
            self.database.upsert_filament_deduction_record(
                archive_id=archive.id,
                sync_status="synced",
                printer_name=printer_name,
                source_location_id=source_location_id,
                stock_item_id=stock_item_id,
                quantity=quantity,
                raw_archive=archive.model_dump(),
            )
            logger.info(
                "Deducted %s g of filament for Bambuddy archive %s from stock item %s",
                self.format_number(quantity),
                archive.id,
                stock_item_id,
            )
            return f"deducted {self.format_number(quantity)} g filament from {source_location.get('pathstring')}"
        except Exception as exc:
            self.database.upsert_filament_deduction_record(
                archive_id=archive.id,
                sync_status="failed",
                quantity=quantity,
                error=str(exc),
                raw_archive=archive.model_dump(),
            )
            logger.exception("Failed to deduct filament for Bambuddy archive %s", archive.id)
            return f"filament deduction failed: {exc}"

    async def printer_name_for_archive(self, archive: Archive) -> str | None:
        if archive.printer_name and archive.printer_name.strip():
            return archive.printer_name.strip()
        if archive.printer_id is None:
            return None

        if self._printer_name_cache is None:
            printers = await self.bambuddy.list_printers()
            self._printer_name_cache = {
                int(printer["id"]): str(printer.get("name") or "").strip()
                for printer in printers
                if printer.get("id") is not None and str(printer.get("name") or "").strip()
            }
        return self._printer_name_cache.get(archive.printer_id)

    async def filament_source_for_archive(
        self,
        archive: Archive,
        *,
        printer_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        printer_key = printer_name.strip().upper()
        root = self.settings.filament_equipment_location_path.strip().strip("/")
        printer_path = f"{root}/{printer_key}"
        printer_location = await self.inventree.find_stock_location_by_path(printer_path)
        if not printer_location:
            raise ExternalApiError(f"InvenTree stock location '{printer_path}' was not found")

        printer_location_id = int(printer_location.get("pk") or printer_location.get("id"))
        child_locations = await self.inventree.child_stock_locations(printer_location_id)
        ams_locations = self.ams_locations(child_locations)
        candidate_locations = self.candidate_filament_locations(archive, printer_location, ams_locations)

        candidates: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for location in candidate_locations:
            location_id = int(location.get("pk") or location.get("id"))
            for stock_item in await self.inventree.list_stock_items_at_location(location_id):
                if not self.is_filament_stock_item(stock_item):
                    continue
                score = self.filament_match_score(archive, stock_item)
                if score >= 0:
                    candidates.append((score, location, stock_item))

        if not candidates:
            paths = ", ".join(str(location.get("pathstring") or location.get("name")) for location in candidate_locations)
            raise ExternalApiError(f"No filament stock item found for archive {archive.id} in {paths}")

        candidates.sort(key=lambda item: (item[0], self.stock_quantity(item[2])), reverse=True)
        _, location, stock_item = candidates[0]
        part_id = stock_item.get("part") or (stock_item.get("part_detail") or {}).get("pk")
        same_part = [item for _, candidate_location, item in candidates if candidate_location == location and (item.get("part") or (item.get("part_detail") or {}).get("pk")) == part_id]
        same_part.sort(key=self.stock_quantity)
        return location, same_part

    def candidate_filament_locations(
        self,
        archive: Archive,
        printer_location: dict[str, Any],
        ams_locations: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not ams_locations:
            return [printer_location]

        ams_slot = self.ams_slot_for_archive(archive)
        if ams_slot and ams_slot in ams_locations:
            return [ams_locations[ams_slot]]

        return [ams_locations[key] for key in sorted(ams_locations)]

    @staticmethod
    def ams_locations(locations: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for location in locations:
            name = str(location.get("name") or "").strip().upper()
            match = re.fullmatch(r"AMS[-_\s]*(\d+)", name)
            if match:
                result[int(match.group(1))] = location
        return result

    @staticmethod
    def ams_slot_for_archive(archive: Archive) -> int | None:
        raw = archive.model_dump()
        for key, value in raw.items():
            normalized_key = "".join(char for char in key.lower() if char.isalnum())
            if normalized_key not in {
                "ams",
                "amsid",
                "amsindex",
                "amsslot",
                "tray",
                "trayid",
                "slot",
                "slotid",
                "filamenttray",
                "filamenttrayid",
                "filamentslot",
            }:
                continue

            slot = ArchiveSyncService._parse_slot_number(value)
            if slot is None:
                continue
            if normalized_key in {"tray", "trayid", "slot", "slotid", "filamenttray", "filamenttrayid", "filamentslot"}:
                if 0 <= slot <= 3:
                    return slot + 1
            if 1 <= slot <= 4:
                return slot
        return None

    @staticmethod
    def _parse_slot_number(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        match = re.search(r"(\d+)", str(value))
        return int(match.group(1)) if match else None

    @staticmethod
    def is_filament_stock_item(stock_item: dict[str, Any]) -> bool:
        part = stock_item.get("part_detail") or {}
        units = str(part.get("units") or "").strip().lower()
        if units and units != "g":
            return False
        return ArchiveSyncService.stock_quantity(stock_item) > 0

    @staticmethod
    def filament_match_score(archive: Archive, stock_item: dict[str, Any]) -> int:
        part = stock_item.get("part_detail") or {}
        haystack = " ".join(
            str(value or "")
            for value in (
                part.get("IPN"),
                part.get("name"),
                part.get("full_name"),
                part.get("description"),
            )
        ).upper()
        score = 0

        material = str(archive.filament_type or "").strip().upper()
        if material:
            if material not in haystack:
                return -1
            score += 10

        color = ArchiveSyncService.color_name_for_archive(archive)
        if color:
            if color not in haystack:
                return -1
            score += 10

        return score

    @staticmethod
    def color_name_for_archive(archive: Archive) -> str | None:
        color = str(archive.filament_color or "").strip()
        if not color:
            return None

        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            red = int(color[1:3], 16)
            green = int(color[3:5], 16)
            blue = int(color[5:7], 16)
            if max(red, green, blue) < 50:
                return "BLACK"
            if min(red, green, blue) > 220:
                return "WHITE"
            if red > green * 1.5 and red > blue * 1.5:
                return "RED"
            if green > red * 1.5 and green > blue * 1.5:
                return "GREEN"
            if blue > red * 1.5 and blue > green * 1.5:
                return "BLUE"
            return None

        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", color).strip("_").upper()
        return normalized or None

    @staticmethod
    def filament_grams_for_archive(archive: Archive) -> float | None:
        raw = archive.model_dump()
        for key in ("filament_used_grams", "total_filament_actual_grams", "filament_used"):
            value = raw.get(key)
            if value is None:
                continue
            try:
                grams = float(value)
            except (TypeError, ValueError):
                continue
            if grams > 0:
                return grams
        return None

    @staticmethod
    def stock_quantity(stock_item: dict[str, Any]) -> float:
        try:
            return float(stock_item.get("quantity") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def format_number(value: float) -> str:
        return f"{value:.3f}".rstrip("0").rstrip(".")

    @staticmethod
    def _sync_message(message: str, deduction: str | None) -> str:
        return f"{message}; {deduction}" if deduction else message

    @staticmethod
    def should_auto_deduct_filament(existing_record: dict[str, Any]) -> bool:
        status = str(existing_record.get("archive_status") or "").lower()
        if status in {"printing", "running", "queued", "pending"}:
            return True
        return False

    def printed_quantity_for_archive(self, archive: Archive) -> int:
        raw = archive.model_dump()
        normalized_values = {
            "".join(char for char in key.lower() if char.isalnum()): value
            for key, value in raw.items()
        }
        for key in (
            "itemsprinted",
            "printeditems",
            "items",
            "itemcount",
            "objectcount",
            "quantity",
        ):
            value = normalized_values.get(key)
            if value is None:
                continue
            try:
                quantity = int(float(value))
            except (TypeError, ValueError):
                continue
            if quantity > 0:
                return quantity
        return max(1, int(round(self.settings.default_stock_quantity)))

    @classmethod
    def part_references_for_archive(cls, archive: Archive) -> list[str]:
        references: list[str] = []

        if archive.name and archive.name.strip():
            references.append(archive.name.strip())

        if archive.print_name and archive.print_name.strip():
            references.append(archive.print_name.strip())

        if archive.filename and archive.filename.strip():
            filename = archive.filename.strip()
            if "\\" in filename:
                references.append(PureWindowsPath(filename).stem or filename)
            else:
                references.append(PurePosixPath(filename).stem or filename)
            references.append(filename)

        for reference in list(references):
            references.extend(cls._part_reference_variants(reference))
        references.append(f"Bambuddy archive {archive.id}")

        unique: list[str] = []
        seen: set[str] = set()
        for reference in references:
            normalized = reference.strip()
            key = normalized.lower()
            if normalized and key not in seen:
                seen.add(key)
                unique.append(normalized)
        return unique

    @classmethod
    def _part_reference_variants(cls, reference: str) -> list[str]:
        variants = [cls._strip_print_extensions(reference)]
        for part in re.split(r"\s+\+\s+", reference):
            part = part.strip()
            if part and part != reference:
                variants.append(part)
                variants.append(cls._strip_print_extensions(part))
        return variants

    @staticmethod
    def _strip_print_extensions(reference: str) -> str:
        path = PureWindowsPath(reference) if "\\" in reference else PurePosixPath(reference)
        known_suffixes = {".3mf", ".stl", ".step", ".stp", ".obj", ".gcode"}
        value = path.stem if path.suffix.lower() in known_suffixes else str(path)

        while True:
            next_path = PureWindowsPath(value) if "\\" in value else PurePosixPath(value)
            if next_path.suffix.lower() not in known_suffixes:
                return value
            value = next_path.stem

    def stock_notes_for_archive(self, archive: Archive, *, price_note: str | None = None) -> str:
        duration = archive.actual_time_seconds or archive.print_time_seconds or archive.duration
        filament_used = archive.filament_used_grams or archive.filament_used
        printed_quantity = self.printed_quantity_for_archive(archive)
        rows = [
            "Imported from Bambuddy.",
            f"Archive ID: {archive.id}",
            f"Print name: {archive.print_name}" if archive.print_name else "",
            f"Filename: {archive.filename}" if archive.filename else "",
            f"Printer: {archive.printer_name}" if archive.printer_name else "",
            f"Started at: {archive.started_at}" if archive.started_at else "",
            f"Completed at: {archive.completed_at}" if archive.completed_at else "",
            f"Created at: {archive.created_at}" if archive.created_at else "",
            f"Duration: {duration} seconds" if duration is not None else "",
            f"Items printed: {printed_quantity}",
            f"Quantity: {archive.quantity}" if archive.quantity is not None else "",
            f"Object count: {archive.object_count}" if archive.object_count is not None else "",
            f"Filament used: {filament_used} g" if filament_used is not None else "",
            f"Filament type: {archive.filament_type}" if archive.filament_type else "",
            f"Filament color: {archive.filament_color}" if archive.filament_color else "",
            f"Cost: {archive.cost}" if archive.cost is not None else "",
            price_note or "",
            f"Bambuddy status: {archive.status}" if archive.status else "",
        ]
        return "\n".join(row for row in rows if row)

    @staticmethod
    def is_successful_archive(archive: Archive) -> bool:
        status = (archive.status or "").lower()
        if status in {"success", "completed", "complete", "done"}:
            return True
        return bool(archive.completed_at and status not in {"failed", "stopped", "printing", "running"})

    @staticmethod
    def _backfill_limit_reached(counts: dict[str, int], max_archives: int) -> bool:
        return counts["synced"] + counts["already_synced"] + counts["failed"] >= max_archives
