import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request

from .bambuddy import BambuddyClient
from .build_orders import BuildOrderService
from .config import Settings, get_settings
from .database import Database
from .http_errors import BuildOrderError, ExternalApiError
from .inventree import InvenTreeClient
from .models import BambuddyWebhook, BuildOrderQueueRequest, SyncResult
from .sync import ArchiveSyncService

logger = logging.getLogger(__name__)


async def require_service_token(
    request: Request,
    x_service_token: str | None = Header(default=None),
) -> None:
    settings: Settings = request.app.state.settings
    if settings.service_api_token and x_service_token != settings.service_api_token:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Service-Token")


async def require_webhook_secret(
    request: Request,
    x_sync_secret: str | None = Header(default=None),
) -> None:
    settings: Settings = request.app.state.settings
    if settings.webhook_shared_secret and x_sync_secret != settings.webhook_shared_secret:
        raise HTTPException(status_code=401, detail="Missing or invalid X-Sync-Secret")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    database = Database(settings.database_path)
    database.init()

    bambuddy = BambuddyClient(settings)
    inventree = InvenTreeClient(settings)
    build_orders = BuildOrderService(
        settings=settings,
        database=database,
        bambuddy=bambuddy,
        inventree=inventree,
    )
    sync_service = ArchiveSyncService(
        settings=settings,
        database=database,
        bambuddy=bambuddy,
        inventree=inventree,
        build_orders=build_orders,
    )
    build_orders.archive_sync = sync_service

    app.state.settings = settings
    app.state.database = database
    app.state.bambuddy = bambuddy
    app.state.inventree = inventree
    app.state.sync_service = sync_service
    app.state.build_orders = build_orders

    stop_event = asyncio.Event()
    poll_task: asyncio.Task[Any] | None = None

    async def poll_loop() -> None:
        while not stop_event.is_set():
            try:
                await sync_service.backfill()
                await sync_service.assign_missing_filament_batches()
                await sync_service.reconcile_filament_inventory()
                await build_orders.reconcile()
            except Exception:
                logger.exception("Scheduled sync failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.poll_interval_seconds)
            except TimeoutError:
                continue

    if settings.sync_on_startup:
        async def startup_sync() -> None:
            await sync_service.backfill()
            await sync_service.reconcile_filament_inventory()
            if settings.build_order_reconcile_on_startup:
                await build_orders.reconcile()
        asyncio.create_task(startup_sync())

    # Inventory must exist before archive polling can process new filament usage.
    await sync_service.assign_missing_filament_batches()
    await sync_service.reconcile_filament_inventory()

    if settings.poll_interval_seconds > 0:
        poll_task = asyncio.create_task(poll_loop())

    try:
        yield
    finally:
        stop_event.set()
        if poll_task:
            poll_task.cancel()
        await bambuddy.close()
        await inventree.close()


app = FastAPI(
    title="Bambuddy InvenTree Sync",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    database: Database = request.app.state.database
    return {"status": "ok", "counts": database.counts()}


@app.get("/validate", dependencies=[Depends(require_service_token)])
async def validate(request: Request) -> dict[str, Any]:
    sync_service: ArchiveSyncService = request.app.state.sync_service
    try:
        return await sync_service.validate_targets()
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/webhooks/bambuddy", response_model=SyncResult, dependencies=[Depends(require_webhook_secret)])
async def bambuddy_webhook(request: Request, payload: BambuddyWebhook) -> SyncResult:
    if payload.event not in {"print_complete", "print_failed"}:
        return SyncResult(archive_id=0, status="skipped", message=f"Ignored event {payload.event}")

    archive_id = payload.data.get("archive_id") or payload.data.get("id")
    if archive_id is None:
        raise HTTPException(status_code=400, detail="Webhook payload does not contain data.archive_id")

    sync_service: ArchiveSyncService = request.app.state.sync_service
    try:
        return await sync_service.sync_archive_id(int(archive_id), webhook_data=payload.data)
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/sync/archive/{archive_id}", response_model=SyncResult, dependencies=[Depends(require_service_token)])
async def sync_archive(
    request: Request,
    archive_id: int,
    force: bool = Query(default=False),
) -> SyncResult:
    sync_service: ArchiveSyncService = request.app.state.sync_service
    try:
        return await sync_service.sync_archive_id(archive_id, force=force)
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/sync/backfill", dependencies=[Depends(require_service_token)])
async def sync_backfill(
    request: Request,
    background_tasks: BackgroundTasks,
    background: bool = Query(default=False),
    status: str | None = Query(default=None),
    max_archives: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    sync_service: ArchiveSyncService = request.app.state.sync_service

    if background:
        background_tasks.add_task(sync_service.backfill, status=status, max_archives=max_archives)
        return {"status": "accepted"}

    try:
        return await sync_service.backfill(status=status, max_archives=max_archives)
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sync/status", dependencies=[Depends(require_service_token)])
async def sync_status(request: Request) -> dict[str, int]:
    database: Database = request.app.state.database
    return database.counts()


@app.post("/sync/purchase-prices", dependencies=[Depends(require_service_token)])
async def sync_purchase_prices(request: Request) -> dict[str, int]:
    sync_service: ArchiveSyncService = request.app.state.sync_service
    return await sync_service.refresh_purchase_prices()


@app.get("/build-orders/{build_order_id}", dependencies=[Depends(require_service_token)])
async def build_order_status(request: Request, build_order_id: int) -> dict[str, Any]:
    service: BuildOrderService = request.app.state.build_orders
    try:
        return await service.status(build_order_id)
    except BuildOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/build-orders/{build_order_id}/queue", dependencies=[Depends(require_service_token)])
async def queue_build_order(
    request: Request,
    build_order_id: int,
    options: BuildOrderQueueRequest,
) -> dict[str, Any]:
    service: BuildOrderService = request.app.state.build_orders
    try:
        return await service.enqueue(build_order_id, options)
    except BuildOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExternalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/sync/build-orders", dependencies=[Depends(require_service_token)])
async def reconcile_build_orders(request: Request) -> dict[str, int]:
    service: BuildOrderService = request.app.state.build_orders
    return await service.reconcile()
