from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Archive(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str | None = None
    filename: str | None = None
    print_name: str | None = None
    content_hash: str | None = None
    printer_id: int | None = None
    printer_name: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration: int | float | None = None
    print_time_seconds: int | float | None = None
    actual_time_seconds: int | float | None = None
    status: str | None = None
    filament_used: float | None = None
    filament_used_grams: float | None = None
    filament_type: str | None = None
    filament_color: str | None = None
    quantity: float | None = None
    object_count: int | None = None
    cost: float | None = None
    external_url: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class BambuddyWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str
    timestamp: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    archive_id: int
    status: str
    message: str
    part_id: int | None = None
    stock_item_id: int | None = None
    part_key: str | None = None
