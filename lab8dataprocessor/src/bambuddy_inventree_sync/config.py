from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bambuddy_base_url: str
    bambuddy_api_key: str

    inventree_base_url: str
    inventree_web_url: str | None = None
    inventree_token: str
    inventree_part_category_id: int
    inventree_stock_location_id: int

    service_api_token: str | None = None
    webhook_shared_secret: str | None = None

    sync_success_only: bool = True
    sync_part_images: bool = True
    overwrite_part_images: bool = False
    sync_archive_external_link: bool = True
    overwrite_archive_external_link: bool = False
    default_stock_quantity: Annotated[float, Field(gt=0)] = 1
    inventree_stock_status: int = 10
    part_ipn_prefix: str = "BMB"
    part_key_fields: str = "filename,name"
    filament_deduction_enabled: bool = False
    filament_equipment_location_path: str = "EQIPMENT"
    filament_part_category_id: int = 19
    filament_default_core_weight: int = 250
    filament_default_label_weight: int = 1000
    filament_core_weight_catalog_id: int | None = None
    build_order_sync_enabled: bool = True
    build_order_auto_complete: bool = True
    build_order_reconcile_on_startup: bool = True
    backfill_page_size: Annotated[int, Field(ge=1, le=250)] = 50
    poll_interval_seconds: Annotated[int, Field(ge=0)] = 0
    sync_on_startup: bool = False
    http_timeout_seconds: Annotated[int, Field(ge=1)] = 30
    data_dir: Path = Path("/data")

    @field_validator("bambuddy_base_url", "inventree_base_url", "inventree_web_url")
    @classmethod
    def strip_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.rstrip("/")

    @field_validator("part_ipn_prefix")
    @classmethod
    def clean_prefix(cls, value: str) -> str:
        cleaned = "".join(char for char in value.upper() if char.isalnum() or char in ("-", "_"))
        return cleaned or "BMB"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "sync.sqlite3"

    @property
    def part_key_field_names(self) -> list[str]:
        fields = [field.strip() for field in self.part_key_fields.split(",") if field.strip()]
        return fields or ["filename", "name"]

    @property
    def inventree_browser_url(self) -> str:
        base_url = (self.inventree_web_url or self.inventree_base_url).rstrip("/")
        if base_url.endswith("/api"):
            return base_url[:-4]
        return base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
