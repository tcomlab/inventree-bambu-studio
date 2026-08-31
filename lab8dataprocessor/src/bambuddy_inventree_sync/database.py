import json
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS sync_records (
                    archive_id INTEGER PRIMARY KEY,
                    part_key TEXT,
                    part_id INTEGER,
                    stock_item_id INTEGER,
                    archive_status TEXT,
                    sync_status TEXT NOT NULL,
                    error TEXT,
                    raw_archive TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS part_map (
                    part_key TEXT PRIMARY KEY,
                    inventree_part_id INTEGER NOT NULL,
                    display_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS filament_deduction_records (
                    archive_id INTEGER PRIMARY KEY,
                    printer_name TEXT,
                    source_location_id INTEGER,
                    stock_item_id INTEGER,
                    quantity REAL,
                    sync_status TEXT NOT NULL,
                    error TEXT,
                    raw_archive TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS filament_spool_map (
                    inventree_part_id INTEGER PRIMARY KEY,
                    bambuddy_spool_id INTEGER NOT NULL UNIQUE,
                    ipn TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS filament_stock_spool_map (
                    inventree_stock_item_id INTEGER PRIMARY KEY,
                    inventree_part_id INTEGER NOT NULL,
                    bambuddy_spool_id INTEGER NOT NULL UNIQUE,
                    batch TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS printer_time_records (
                    archive_id INTEGER PRIMARY KEY,
                    printer_name TEXT NOT NULL,
                    stock_item_id INTEGER,
                    minutes REAL NOT NULL,
                    sync_status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                """
            )

    def get_printer_time_record(self, archive_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM printer_time_records WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_printer_time_record(
        self,
        *,
        archive_id: int,
        printer_name: str,
        minutes: float,
        sync_status: str,
        stock_item_id: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO printer_time_records (
                    archive_id, printer_name, stock_item_id, minutes, sync_status, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    printer_name = excluded.printer_name,
                    stock_item_id = excluded.stock_item_id,
                    minutes = excluded.minutes,
                    sync_status = excluded.sync_status,
                    error = excluded.error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (archive_id, printer_name, stock_item_id, minutes, sync_status, error),
            )

    def get_spool_map(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT inventree_part_id, bambuddy_spool_id FROM filament_spool_map").fetchall()
        return {int(row["inventree_part_id"]): int(row["bambuddy_spool_id"]) for row in rows}

    def upsert_spool_map(self, *, part_id: int, spool_id: int, ipn: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO filament_spool_map (inventree_part_id, bambuddy_spool_id, ipn)
                VALUES (?, ?, ?) ON CONFLICT(inventree_part_id) DO UPDATE SET
                bambuddy_spool_id=excluded.bambuddy_spool_id, ipn=excluded.ipn,
                updated_at=CURRENT_TIMESTAMP""", (part_id, spool_id, ipn),
            )

    def get_stock_spool_map(self) -> dict[int, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT inventree_stock_item_id, bambuddy_spool_id FROM filament_stock_spool_map").fetchall()
        return {int(row["inventree_stock_item_id"]): int(row["bambuddy_spool_id"]) for row in rows}

    def upsert_stock_spool_map(self, *, stock_item_id: int, part_id: int, spool_id: int, batch: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO filament_stock_spool_map
                (inventree_stock_item_id, inventree_part_id, bambuddy_spool_id, batch)
                VALUES (?, ?, ?, ?) ON CONFLICT(inventree_stock_item_id) DO UPDATE SET
                inventree_part_id=excluded.inventree_part_id,
                bambuddy_spool_id=excluded.bambuddy_spool_id,
                batch=excluded.batch, updated_at=CURRENT_TIMESTAMP""",
                (stock_item_id, part_id, spool_id, batch),
            )

    def get_record(self, archive_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_records WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_record(
        self,
        *,
        archive_id: int,
        sync_status: str,
        part_key: str | None = None,
        part_id: int | None = None,
        stock_item_id: int | None = None,
        archive_status: str | None = None,
        error: str | None = None,
        raw_archive: dict[str, Any] | None = None,
    ) -> None:
        raw_archive_json = json.dumps(raw_archive, ensure_ascii=False) if raw_archive is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_records (
                    archive_id, part_key, part_id, stock_item_id, archive_status,
                    sync_status, error, raw_archive
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    part_key = excluded.part_key,
                    part_id = excluded.part_id,
                    stock_item_id = excluded.stock_item_id,
                    archive_status = excluded.archive_status,
                    sync_status = excluded.sync_status,
                    error = excluded.error,
                    raw_archive = COALESCE(excluded.raw_archive, sync_records.raw_archive),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    archive_id,
                    part_key,
                    part_id,
                    stock_item_id,
                    archive_status,
                    sync_status,
                    error,
                    raw_archive_json,
                ),
            )

    def get_part_id(self, part_key: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT inventree_part_id FROM part_map WHERE part_key = ?",
                (part_key,),
            ).fetchone()
        return int(row["inventree_part_id"]) if row else None

    def upsert_part(self, *, part_key: str, inventree_part_id: int, display_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO part_map (part_key, inventree_part_id, display_name)
                VALUES (?, ?, ?)
                ON CONFLICT(part_key) DO UPDATE SET
                    inventree_part_id = excluded.inventree_part_id,
                    display_name = excluded.display_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (part_key, inventree_part_id, display_name),
            )

    def get_filament_deduction_record(self, archive_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM filament_deduction_records WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        return dict(row) if row else None

    def synced_stock_records(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT archive_id, part_id, stock_item_id
                FROM sync_records
                WHERE sync_status = 'synced' AND stock_item_id IS NOT NULL
                ORDER BY archive_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_filament_deduction_record(
        self,
        *,
        archive_id: int,
        sync_status: str,
        printer_name: str | None = None,
        source_location_id: int | None = None,
        stock_item_id: int | None = None,
        quantity: float | None = None,
        error: str | None = None,
        raw_archive: dict[str, Any] | None = None,
    ) -> None:
        raw_archive_json = json.dumps(raw_archive, ensure_ascii=False) if raw_archive is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO filament_deduction_records (
                    archive_id, printer_name, source_location_id, stock_item_id,
                    quantity, sync_status, error, raw_archive
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    printer_name = excluded.printer_name,
                    source_location_id = excluded.source_location_id,
                    stock_item_id = excluded.stock_item_id,
                    quantity = excluded.quantity,
                    sync_status = excluded.sync_status,
                    error = excluded.error,
                    raw_archive = COALESCE(excluded.raw_archive, filament_deduction_records.raw_archive),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    archive_id,
                    printer_name,
                    source_location_id,
                    stock_item_id,
                    quantity,
                    sync_status,
                    error,
                    raw_archive_json,
                ),
            )

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sync_status, COUNT(*) AS count
                FROM sync_records
                GROUP BY sync_status
                """
            ).fetchall()
            parts = conn.execute("SELECT COUNT(*) AS count FROM part_map").fetchone()
            filament_rows = conn.execute(
                """
                SELECT sync_status, COUNT(*) AS count
                FROM filament_deduction_records
                GROUP BY sync_status
                """
            ).fetchall()
            printer_time_rows = conn.execute(
                "SELECT sync_status, COUNT(*) AS count FROM printer_time_records GROUP BY sync_status"
            ).fetchall()
        result = {row["sync_status"]: int(row["count"]) for row in rows}
        result["known_parts"] = int(parts["count"]) if parts else 0
        for row in filament_rows:
            result[f"filament_deduction_{row['sync_status']}"] = int(row["count"])
        for row in printer_time_rows:
            result[f"printer_time_{row['sync_status']}"] = int(row["count"])
        return result

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn
