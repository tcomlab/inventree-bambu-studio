# InvenTree Bambu Studio

An InvenTree plugin and Windows helper that opens Part `.3mf`, `.step` and
`.stp` attachments directly in Bambu Studio, and saves the active Bambu Studio
project back to the same Part.

The repository also contains the production
[`Bambuddy ↔ InvenTree Sync`](lab8dataprocessor/README.md) sidecar. It mirrors
filament StockItems and locations into Bambuddy, assigns loaded spools from
InvenTree equipment locations, deducts material after printing, creates finished
stock with calculated unit prices, and records printer working minutes.

The plugin adds a **3D Друк** primary action to InvenTree Part pages. The action
passes the attachment URL and its original filename to a local Windows protocol
handler. A `.3mf` attachment is preferred; if none exists, the newest STEP/STP
attachment is opened. The helper keeps the original filename and never uses the
browser's Downloads folder.

On Build Order pages the same plugin adds a **Bambuddy друк** panel. It can send
the Part's latest `.3mf` attachment to the Bambuddy queue and shows the Batch,
individual plate runs, printer, live progress, remaining time, completed parts
and failures. The plugin proxies these requests through InvenTree, so the sync
service token is never exposed to the browser.

## Requirements

- InvenTree 1.5.5 (the currently tested version)
- An InvenTree deployment with plugins enabled
- Windows with Bambu Studio installed
- `.3mf`, `.step` and `.stp` associated with Bambu Studio
- HTTPS on the public InvenTree URL
- An InvenTree API token belonging to a user with Part change permission

## Server installation with Docker Compose

Clone this repository as `bambu-open-plugin` next to your existing InvenTree
Compose file:

```bash
git clone https://github.com/tcomlab/inventree-bambu-studio.git bambu-open-plugin
```

The included Compose overlay replaces the `server` and `worker` images with a
local image containing the plugin. From your InvenTree Compose directory, run:

```bash
docker compose \
  -f docker-compose.yml \
  -f bambu-open-plugin/docker-compose.plugin.yml \
  build server worker

docker compose \
  -f docker-compose.yml \
  -f bambu-open-plugin/docker-compose.plugin.yml \
  up -d --no-build server worker
```

Collect the plugin's frontend asset, then restart the static web container. The
container names below match the standard names used in the example deployment;
adjust them if yours differ:

```bash
docker exec -w /home/inventree/src/backend/InvenTree \
  inventree-server python manage.py collectplugins

docker compose restart web
```

Open **Settings → Plugins** in InvenTree and enable **Bambu Studio**. Both the
server and worker must use the plugin-enabled image.

Configure these plugin settings:

- `Bambuddy sync service URL`: an internal URL reachable from the InvenTree server, for example `http://192.168.1.5:8088`;
- `Bambuddy sync service token`: the same value as `SERVICE_API_TOKEN` in the sidecar `.env`;
- `Bambuddy browser URL`: optional user-facing Bambuddy URL.

To target another InvenTree image version, set `INVENTREE_VERSION` before the build.

## Printing a Build Order

1. Attach the sliced `.3mf` project to the printable Part.
2. Create a Build Order for that Part and open its **Bambuddy друк** panel.
3. Enter how many physical parts are placed on one plate, optionally select a plate and printer, and select **Передати в Bambuddy**.
4. Track each run in the same panel. The panel refreshes automatically every five seconds.
5. On successful completion the sidecar creates a native Build Output and places the completed stock in the Build Order destination, or the configured finished-goods location when no destination is selected.

Repeated clicks and service restarts do not duplicate the Bambuddy Batch, queue items or Build Output.

## Windows helper installation

Open PowerShell in the cloned repository and install the helper for your public
InvenTree hostname:

```powershell
powershell.exe -ExecutionPolicy Bypass -File `
  .\windows-helper\Install-InvenTreeBambuHelper.ps1 `
  -AllowedHosts stock.example.com
```

Administrator rights are not required. The installer registers the
`inventree-bambu-open://` protocol and starts a tray application for the current
Windows user. It prompts for an InvenTree API token and encrypts that token with
Windows DPAPI, so it can only be decrypted by the same Windows user. Repeat the
installation on every workstation that should use the **3D Друк** button.

On the first click, the browser may ask permission to open the InvenTree Bambu
Studio protocol. Approve it and optionally allow it permanently for your
InvenTree site.

## Saving a Bambu Studio project to InvenTree

1. Open a model using **3D Друк** in InvenTree.
2. If a STEP/STP model was opened, save the Bambu Studio project as `.3mf`.
   Bambu Studio normally proposes the same model directory and basename.
3. Right-click the InvenTree Bambu tray icon.
4. Select **Save 3MF to InvenTree**.

If the expected project file does not exist, the helper asks you to select a
`.3mf` file. The first save creates a new attachment on the original Part;
later saves update that same attachment. The next **3D Друк** click opens the
`.3mf` project instead of the STEP source.

## File location and names

Models are stored under:

```text
%LOCALAPPDATA%\InvenTreeBambuOpen\Models
```

Each Part gets a private subdirectory, but attachment basenames are preserved.
For example, `W-MOUNT-BALKA.step` is opened under that exact name and its Bambu
Studio project is stored as `W-MOUNT-BALKA.3mf`. Nothing is written to the
Downloads folder.

The helper log is available at:

```text
%LOCALAPPDATA%\InvenTreeBambuOpen\helper.log
```

## Security

The helper accepts only HTTPS attachment URLs, only `.3mf`, `.step` and `.stp`
files, only hosts explicitly supplied during installation, and does not follow
HTTP redirects. The upload token is encrypted using Windows DPAPI. Run the
installer again to replace the allowed-host list or API token.

## Project structure

- `inventree_bambu_open/` — InvenTree plugin and UI action
- `windows-helper/` — Windows protocol handler, tray application and installer
- `Dockerfile.inventree` — plugin-enabled InvenTree image
- `docker-compose.plugin.yml` — safe Compose overlay without deployment secrets
- `lab8dataprocessor/` — Bambuddy ↔ InvenTree inventory synchronization service
