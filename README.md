# InvenTree Bambu Studio

An InvenTree plugin and Windows helper that opens Part `.step` and `.stp`
attachments directly in Bambu Studio without placing files in the browser's
Downloads folder.

The plugin adds a **3D Друк** primary action to InvenTree Part pages. The action
passes the attachment URL and its original filename to a local Windows protocol
handler. The helper downloads the model to its private application-data folder,
preserves the filename, and opens it through the Windows STEP file association.

## Requirements

- InvenTree 1.4.0 (the currently tested version)
- An InvenTree deployment with plugins enabled
- Windows with Bambu Studio installed
- `.step` / `.stp` associated with Bambu Studio
- HTTPS on the public InvenTree URL

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

To target another InvenTree image version, set `INVENTREE_VERSION` before the
build. Compatibility outside 1.4.0 has not yet been verified.

## Windows helper installation

Open PowerShell in the cloned repository and install the helper for your public
InvenTree hostname:

```powershell
powershell.exe -ExecutionPolicy Bypass -File `
  .\windows-helper\Install-InvenTreeBambuHelper.ps1 `
  -AllowedHosts stock.example.com
```

Administrator rights are not required. The installer registers the
`inventree-bambu-open://` protocol for the current Windows user. Repeat this
step on every workstation that should use the **3D Друк** button.

On the first click, the browser may ask permission to open the InvenTree Bambu
Studio protocol. Approve it and optionally allow it permanently for your
InvenTree site.

## File location and names

Models are stored under:

```text
%LOCALAPPDATA%\InvenTreeBambuOpen\Models
```

Each attachment gets a private subdirectory, but its basename is preserved
exactly. For example, `W-MOUNT-BALKA.step` remains `W-MOUNT-BALKA.step` when it
is opened in Bambu Studio. Nothing is written to the Downloads folder.

The helper log is available at:

```text
%LOCALAPPDATA%\InvenTreeBambuOpen\helper.log
```

## Security

The helper accepts only HTTPS attachment URLs, only `.step` and `.stp` files,
only hosts explicitly supplied during installation, and does not follow HTTP
redirects. Run the installer again to replace the allowed-host list.

## Project structure

- `inventree_bambu_open/` — InvenTree plugin and UI action
- `windows-helper/` — Windows protocol handler and installer
- `Dockerfile.inventree` — plugin-enabled InvenTree image
- `docker-compose.plugin.yml` — safe Compose overlay without deployment secrets
