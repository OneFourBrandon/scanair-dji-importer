# ScanAir DJI Importer

Windows desktop companion app for ScanAir Drone Path Creator. It connects a user's ScanAir cloud account to a DJI RC 2 controller, generates selected cloud paths through the backend, and syncs the resulting DJI KMZ waypoint missions directly onto the controller.

## Why This Project Matters

This app bridges a real SaaS workflow, a cloud mission-planning backend, DJI's KMZ waypoint format, and Windows MTP device integration. It is designed around a practical field workflow: plan missions in the browser, authorize the desktop importer with the same account, choose paths, and load them onto the RC 2 without manually moving files through Explorer.

## Current Features

- Website authorization through `https://path.scanair.ca` using a one-time desktop auth session.
- Cloud project and path loading from the ScanAir Drone Path Creator backend.
- Backend-generated DJI KMZ exports, including existing account, subscription, mission limit, and export checks.
- Multi-path selection with manual sync ordering, so the user controls which cloud path replaces each dummy slot.
- DJI RC 2 detection through Windows MTP / This PC.
- Controller-specific dummy slot mapping based on discovered DJI waypoint package IDs.
- Local generated-KMZ cache for the current sync only.
- Backup and restore tools for the controller waypoint folder.
- Packaged Windows EXE build flow.

Generated KMZ files are synced to:

```text
DJI RC 2\Internal shared storage\Android\data\dji.go.v5\files\waypoint
```

## Engineering Highlights

- **Desktop to web authorization:** Implements a one-time browser approval flow so the desktop app can authenticate through the SaaS web app without asking the user to paste tokens.
- **Cloud backend integration:** Uses the same FastAPI export route as the web app, keeping billing, ownership, mission generation, and waypoint limits centralized.
- **DJI package handling:** Writes DJI's standard waypoint package layout: `waypoint\<mission-name>\<mission-name>.kmz`.
- **KMZ normalization:** Normalizes KMZ internals to DJI's expected `wpmz/template.kml` and `wpmz/waylines.wpml` paths when source packages use root-level files.
- **Windows MTP automation:** Uses Windows PowerShell's `Shell.Application` COM API to copy files into an RC 2 exposed through the Shell namespace rather than a normal drive letter.
- **Controller safety:** Reads existing DJI waypoint packages, preserves system folders such as `capability` and `map_preview`, and supports local zip backups before destructive restore workflows.
- **Test coverage:** Includes unit tests for project storage, creator backend client behavior, cloud auth defaults, file ordering, and KMZ handling.

## Run

```powershell
python -m scanair_dji_importer
```

On first run, the app shows a setup window until it detects 10 identical dummy missions on the connected RC 2. Create one waypoint mission in DJI Fly with at least two waypoints, then use Save As / duplicate until there are 10 copies of the same path.

After detection, the app remembers the slot IDs for that physical controller, so those slots can later contain replaced KMZ files with any waypoint count.

Click **Authorize With Website** to connect the importer to your ScanAir account. The app opens the Drone Path Creator website with a one-time code, and the website authorizes the desktop importer using the user's existing ScanAir login.

Then choose a cloud project, select the paths to sync, and use **Move Up** / **Move Down** to control sync order. The displayed slot number is the dummy slot that path will replace on the controller.

## Drone Path Creator backend

The importer talks to the deployed ScanAir backend instead of generating missions locally. This keeps account ownership, subscription access, mission validation, and export behavior consistent with the browser app.

The default backend URL is:

```text
https://api.scanair.ca
```

The default website authorization URL is:

```text
https://path.scanair.ca
```

For local development, set these before starting the importer:

```powershell
$env:SCANAIR_CREATOR_API_URL = "http://localhost:8000"
$env:SCANAIR_CREATOR_WEBSITE_URL = "http://localhost:5173"
python -m scanair_dji_importer
```

The app saves the backend URL, website URL, account email, and resulting session locally in its app state.

## Tests

```powershell
python -m pytest
```

Focused tests currently cover:

- Creator backend client requests and responses.
- Default website authorization domain behavior.
- Project storage lifecycle.
- Manual sync ordering.
- KMZ filename validation.
- Legacy project file migration.

## Build a Windows EXE

Install build dependencies and create a single-window executable:

```powershell
.\build_exe.ps1
```

To force a specific interpreter:

```powershell
$env:PYTHON_EXE = "C:\Python314\python.exe"
.\build_exe.ps1
```

The output is written to:

```text
dist\ScanAirDJIImporter.exe
```

## DJI controller note

The DJI RC 2 is exposed by Windows as an MTP shell device, not a normal drive letter. This app uses Windows PowerShell's `Shell.Application` COM API to navigate and copy into the controller.

Because DJI Fly appears to expose waypoint missions through history/indexed entries, this app uses 10 duplicate dummy missions as fixed slots. The controller folder and KMZ filenames are treated as DJI-generated IDs. The app discovers those IDs from matching dummy KMZ waypoint signatures and creation times, then stores them under the connected controller's Windows hardware ID. If Windows does not expose that ID, it falls back to the detected MTP controller name.

The backup manager stores zip backups locally before destructive workflows such as restore. Windows MTP delete operations may still show Explorer confirmation dialogs because the controller is exposed through the Shell namespace rather than as a normal drive.
