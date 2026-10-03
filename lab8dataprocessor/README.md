# Bambuddy ↔ InvenTree Sync

Production sidecar service that connects Bambuddy print tracking with InvenTree inventory. InvenTree is the source of truth for filament spools, stock locations, printer rates and finished goods; Bambuddy is the source of print archives and actual print usage.

The service runs in its own Docker container and uses the public APIs of both systems. Runtime secrets and SQLite state are never committed.

## Business workflow

### Filament spools

Each InvenTree filament `StockItem` is mirrored to exactly one Bambuddy spool.

- A missing batch code is assigned automatically as `SP-xxxxxx`, derived from the StockItem ID. Example: StockItem `134` becomes `SP-000134`.
- The Bambuddy note has the stable format `SP-000134 stock_item_id=134 part_id=263; IPN=FIL-PLA-0002`.
- Material, colour, manufacturer, nominal spool weight and core weight are copied from the InvenTree part metadata.
- `StockItem.purchase_price` is treated as price per gram; Bambuddy receives `cost_per_kg = purchase_price × 1000`.
- Remaining grams become Bambuddy net stock and drive the fill indicator.
- The short InvenTree Stock Location name is copied to Bambuddy `storage_location`: for example `INPUT_STOCK/SH-12` becomes `SH-12`.
- Moving the StockItem in InvenTree updates both its Bambuddy storage location and, for equipment locations, its loaded-slot assignment.

InvenTree remains authoritative. Manual changes to a managed Bambuddy spool are overwritten during reconciliation.

### Printer and AMS mapping

The default equipment root is `EQIPMENT`.

| InvenTree Stock Location | Bambuddy assignment |
| --- | --- |
| `EQIPMENT/B1/AMS-1` … `AMS-4` | B1 AMS slots 1 … 4 |
| `EQIPMENT/B2` | B2 External |
| `EQIPMENT/B3` | B3 External |
| `EQIPMENT/B4` | B4 External |

A slot conflict is not silently overwritten: the reconciliation reports a conflict. When a managed StockItem leaves an equipment location, its Bambuddy assignment is removed.

The included `bambuddy-patch/` image fixes A1 Mini external-spool auto-unlinking when firmware reports a colour but leaves `tray_type` blank.

### Build Order print workflow

New production printing starts from an InvenTree Build Order:

1. The Build Order Part must have a `.3mf` attachment.
2. The InvenTree plugin panel sends the Build Order to the sidecar with the number of physical parts on one plate.
3. The sidecar uploads the attachment to the Bambuddy library by content hash, creates one Bambuddy Batch and creates one queue item for every required plate run.
4. A Pending Build Order is issued and moves to Production only after the queue has been created successfully.
5. Queue and printer state are shown in the **Bambuddy друк** panel on the Build Order page. While a printer is active, the panel also shows live percentage, remaining time and layer count.
6. When a queue run starts, the service creates exactly one native InvenTree **Incomplete Output**. Its quantity is the configured number of physical parts per plate.
7. After the run succeeds, the incomplete StockItem receives the calculated unit Purchase Price. Filament deduction and printer-minute accounting continue to use the measured Bambuddy Archive values.
8. The operator completes each output manually in InvenTree, selects its final Stock Location, and completes the Build Order when appropriate. The service does not complete either step automatically.

One Build Order maps to one Bambuddy Batch. A durable SQLite mapping prevents duplicate batches, queue items and Build Outputs across retries and container restarts.

By default the Build Order quantity must be divisible by the number of parts per plate. The panel can explicitly allow overproduction for the final plate. Every run still reports the physical number of parts on the plate: a target of 10 with four parts per plate creates three Incomplete Outputs of four parts each, for 12 physical parts.

### Print completion

For every successful Bambuddy archive linked to a managed Build Order the service keeps its Build Output incomplete for manual acceptance. Prints started directly in Bambuddy without a managed Build Order do **not** create finished StockItems. They only:

1. Deduct the used filament grams from the StockItem loaded in the printer/AMS location when `FILAMENT_DEDUCTION_ENABLED=true`.
2. Add the actual print duration in minutes to the printer StockItem.

The old direct-print finished-stock path can be restored temporarily with `LEGACY_FINISHED_STOCK_ENABLED=true`, but it is disabled by default.

The finished-goods unit price is:

```text
unit price =
  (filament grams × filament price per gram
   + actual print minutes × printer price per minute)
  / printed item count
```

The printer rate is read from `Purchase Price` of the printer StockItem located at `EQIPMENT/B1`, `EQIPMENT/B2`, and so on. Printer Parts should use `min` as their unit. Their accumulated Stock quantity is total worked minutes; therefore their Stock Value represents earned printer time.

If one run produces four items, the whole run cost is divided by four. The calculation breakdown is stored in the finished StockItem notes.

### Duplicate protection

SQLite state in `data/sync.sqlite3` makes all irreversible actions idempotent:

- a managed Bambuddy archive creates at most one Build Output or legacy finished StockItem;
- a Build Order creates at most one Bambuddy Batch;
- every planned plate run creates at most one queue item;
- batch `bambuddy-<archive_id>` is a second duplicate guard;
- filament is deducted at most once per archive;
- printer minutes are added at most once per archive;
- deleting a Bambuddy archive never deletes InvenTree inventory.

## InvenTree label

`inventree-labels/stock_filament_bambuddy_38x21_2.html` is a 38 × 21.2 mm StockItem label with QR code, batch, material, IPN, spool weight, manufacturer and colour.

Expected parameter template names:

- `FL-SpoolWeight`
- `FL-Manufacturer`
- `Color`

## Deployment

Requirements:

- Docker with Linux containers;
- network access to Bambuddy and InvenTree APIs;
- a Bambuddy API key and InvenTree API token;
- existing InvenTree categories, locations, printed Parts and printer StockItems.

Create configuration and start:

```powershell
Copy-Item .env.example .env
notepad .env
docker compose up -d --build
docker compose logs --tail=100
```

Update:

```powershell
git pull
docker compose up -d --build
```

Never commit `.env` or `data/`.

## Configuration

```env
BAMBUDDY_BASE_URL=http://host.docker.internal:8000/api/v1
BAMBUDDY_API_KEY=replace-with-bambuddy-api-key

INVENTREE_BASE_URL=http://host.docker.internal:1337
INVENTREE_WEB_URL=
INVENTREE_TOKEN=replace-with-inventree-token
INVENTREE_PART_CATEGORY_ID=12
INVENTREE_STOCK_LOCATION_ID=110

SERVICE_API_TOKEN=change-me
WEBHOOK_SHARED_SECRET=

SYNC_SUCCESS_ONLY=true
DEFAULT_STOCK_QUANTITY=1
INVENTREE_STOCK_STATUS=10

FILAMENT_DEDUCTION_ENABLED=true
FILAMENT_EQUIPMENT_LOCATION_PATH=EQIPMENT
FILAMENT_PART_CATEGORY_ID=19
FILAMENT_DEFAULT_CORE_WEIGHT=250
FILAMENT_DEFAULT_LABEL_WEIGHT=1000
FILAMENT_CORE_WEIGHT_CATALOG_ID=

BUILD_ORDER_SYNC_ENABLED=true
LEGACY_FINISHED_STOCK_ENABLED=false
BUILD_ORDER_RECONCILE_ON_STARTUP=true
BUILD_ORDER_POLL_INTERVAL_SECONDS=10

BACKFILL_PAGE_SIZE=50
POLL_INTERVAL_SECONDS=300
SYNC_ON_STARTUP=false
HTTP_TIMEOUT_SECONDS=30
DATA_DIR=/data
```

Important settings:

- `INVENTREE_PART_CATEGORY_ID`: category of finished/printed Parts, such as `PLASTIC_PARTS`.
- `INVENTREE_STOCK_LOCATION_ID`: destination for finished StockItems.
- `FILAMENT_PART_CATEGORY_ID`: `PARTS/FILAMENT` category containing filament Parts.
- `FILAMENT_EQUIPMENT_LOCATION_PATH`: root path containing B1–B4 and B1 AMS locations.
- `BUILD_ORDER_SYNC_ENABLED`: enables Build Order to Bambuddy Batch orchestration.
- `LEGACY_FINISHED_STOCK_ENABLED`: opt-in compatibility mode which creates finished StockItems for successful prints not linked to a Build Order. Keep this `false` for the Build Order workflow.
- `BUILD_ORDER_RECONCILE_ON_STARTUP`: resumes incomplete Build Order synchronization after a container restart.
- `BUILD_ORDER_POLL_INTERVAL_SECONDS`: checks active Bambuddy runs independently of the slower inventory sync. When a run enters `printing`, the service creates its InvenTree Build Output immediately; it remains in **Incomplete Outputs** until an operator completes it manually.
- `POLL_INTERVAL_SECONDS`: automatic reconciliation interval; `0` disables polling.

## HTTP API

When `SERVICE_API_TOKEN` is configured, send it as `X-Service-Token` to protected endpoints.

```text
GET  /health
GET  /validate
GET  /sync/status
POST /sync/archive/{archive_id}
POST /sync/backfill
POST /sync/purchase-prices
POST /sync/build-orders
GET  /build-orders/{build_order_id}
POST /build-orders/{build_order_id}/queue
POST /webhooks/bambuddy
```

Examples:

```powershell
curl.exe http://localhost:8088/health
curl.exe -H "X-Service-Token: change-me" http://localhost:8088/validate
curl.exe -X POST -H "X-Service-Token: change-me" "http://localhost:8088/sync/backfill?max_archives=1"
curl.exe -X POST -H "X-Service-Token: change-me" http://localhost:8088/sync/purchase-prices
```

`/sync/purchase-prices` recalculates existing Bambuddy-managed finished StockItems without creating stock, deducting filament or adding printer time again.

`POST /build-orders/{id}/queue` accepts:

```json
{
  "units_per_run": 4,
  "plate_id": 0,
  "printer_id": 5,
  "allow_overproduction": false
}
```

The `.3mf` file and Build Order quantity are resolved from InvenTree; callers do not provide file paths or finished-goods quantities.

## Bambuddy compatibility patch

Copy these files into the Bambuddy Compose directory:

```text
bambuddy-patch/Dockerfile.inventree
bambuddy-patch/docker-compose.override.yml
bambuddy-main.py
```

Then run `docker compose up -d --build`. The override builds `bambuddy-inventree-patched:latest` and preserves normal Bambuddy data and port configuration from the base Compose file.

## Troubleshooting and backup

```powershell
docker compose logs --tail=200
curl.exe http://localhost:8088/health
```

Common causes:

- `401`: invalid service, Bambuddy or InvenTree token;
- no finished stock: archive is not completed or no matching printed Part exists;
- Build Order cannot be queued: its Part has no `.3mf` attachment, its status is not Pending/Production, or the quantity is not divisible by `units_per_run`;
- no deduction: no matching filament StockItem is loaded in the expected equipment location;
- missing cost: filament or printer `Purchase Price` is empty;
- missing storage location: the InvenTree StockItem has no Stock Location;
- duplicate prevention: use forced sync only for recovery, because it can retry previously failed side effects.

Back up `data/sync.sqlite3` together with the deployment `.env`. The repository intentionally contains neither.

## Project structure

```text
src/bambuddy_inventree_sync/   FastAPI service and sync business logic
bambuddy-patch/                derived Bambuddy image for External spool compatibility
inventree-labels/              InvenTree label templates
Dockerfile                     sync-service image
docker-compose.yml             sync-service deployment
.env.example                   safe configuration template
```
