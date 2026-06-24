# ScanAir DJI Importer

Windows desktop companion app for ScanAir Drone Path Creator. It connects a user's ScanAir cloud account to a DJI RC 2 controller, generates KMZ waypoint missions from saved cloud paths, and syncs them directly onto the controller.

## Why This Project Matters

This app bridges a real SaaS workflow, a cloud mission-planning backend, DJI's KMZ waypoint format, and Windows MTP device integration. It is designed around a practical field workflow: plan missions in the browser, authorize the desktop importer with the same account, choose paths, and load them onto the RC 2 without manually moving files through Explorer.

## Current Features

- Website authorization through `https://path.scanair.ca` using a one-time desktop auth session.
- Cloud project and path loading from ScanAir's backend using the authorized user's account token.
- On-demand DJI KMZ generation through the Creator API, using the same backend mission validation and KMZ packaging path as normal Creator exports.
- Multi-path selection with manual sync ordering, so the user controls which cloud path replaces each dummy slot.
- DJI RC 2 detection through Windows MTP / This PC.
- Controller-specific dummy slot mapping based on discovered DJI waypoint package IDs.
- Local generated-KMZ cache for the current sync only.
- Developer mode menu toggle for one-off direct KMZ drag-and-drop syncs.
- Packaged Windows EXE build flow.

Generated KMZ files are synced to:

```text
DJI RC 2\Internal shared storage\Android\data\dji.go.v5\files\waypoint
```

## Engineering Highlights

- **Desktop to web authorization:** Implements a one-time browser approval flow so the desktop app can authenticate through the SaaS web app without asking the user to paste tokens.
- **Cloud backend integration:** Reads per-user cloud projects through the Creator API, then asks the backend to generate current KMZ files for selected paths at sync time. The importer does not read KMZ files directly from Supabase.
- **DJI package handling:** Writes DJI's standard waypoint package layout: `waypoint\<mission-name>\<mission-name>.kmz`.
- **KMZ normalization:** Normalizes KMZ internals to DJI's expected `wpmz/template.kml` and `wpmz/waylines.wpml` paths when source packages use root-level files.
- **Windows MTP automation:** Uses Windows PowerShell's `Shell.Application` COM API to copy files into an RC 2 exposed through the Shell namespace rather than a normal drive letter.
- **Controller safety:** Reads existing DJI waypoint packages and preserves system folders such as `capability` and `map_preview`.
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

The importer asks the Creator backend to generate KMZ files during sync. The importer authenticates through the website, lists the user's saved cloud projects through the backend API, downloads fresh KMZ files for the selected paths, caches them locally for the current sync, and writes them to the DJI RC 2.

Current Creator API flow:

```text
POST /desktop-auth/sessions
GET  /desktop-auth/sessions/{code}
GET  /projects
GET  /projects/{project_id}/paths
POST /projects/{project_id}/paths/{path_id}/exports/kmz
```

The Creator backend should keep ownership, billing/subscription checks, mission validation, waypoint limits, and KMZ packaging on those authenticated backend routes.

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

Developer mode is available from the top menu. When enabled, the importer searches for a local Drone Path Creator instance and updates its Creator URLs when it finds one. It probes common development ports:

```text
Backend:  http://localhost:8000/health, http://127.0.0.1:8000/health
Fallback: http://localhost:8080/health, http://127.0.0.1:8080/health
Website:  http://localhost:5173, http://127.0.0.1:5173
Fallback: http://localhost:5174, http://127.0.0.1:5174, http://localhost:4173, http://127.0.0.1:4173
```

You can also use **Developer > Find Local Creator** to rerun detection without toggling Dev Mode. Dev Mode still allows dropping exactly one local `.kmz` file onto the app to sync it directly to the controller without using ScanAir cloud projects or backend generation. Drag-and-drop support requires `tkinterdnd2`; the EXE spec bundles its data files.

## Tests

```powershell
python -m unittest discover -s tests
```

If `pytest` is installed, this also works because the tests are standard `unittest` tests:

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

The Windows build uses PyInstaller and the project spec file:

```text
ScanAirDJIImporter.spec
```

The spec includes the app logo as the Windows EXE icon, bundles the `tkinterdnd2` data files needed for drag-and-drop, and disables UPX compression to reduce antivirus false-positive risk.

From PowerShell in this repo, run:

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

The build script first checks whether `PyInstaller` and `tkinterdnd2` are importable. If either is missing, it runs:

```powershell
python -m pip install -e ".[build]"
```

It also redirects temporary build files into `.build-tmp` so Windows temp-folder permission issues do not break packaging.

For GitHub Releases, expect unsigned PyInstaller executables to have some SmartScreen/antivirus false-positive risk. The best mitigation is to code-sign the EXE, publish checksums, keep UPX compression disabled, and build release artifacts from a clean CI environment.

## DJI controller note

The DJI RC 2 is exposed by Windows as an MTP shell device, not a normal drive letter. This app uses Windows PowerShell's `Shell.Application` COM API to navigate and copy into the controller.

Because DJI Fly appears to expose waypoint missions through history/indexed entries, this app uses 10 duplicate dummy missions as fixed slots. The controller folder and KMZ filenames are treated as DJI-generated IDs. The app discovers those IDs from matching dummy KMZ waypoint signatures and creation times, then stores them under the connected controller's Windows hardware ID. If Windows does not expose that ID, it falls back to the detected MTP controller name.

Windows MTP delete operations may still show Explorer confirmation dialogs because the controller is exposed through the Shell namespace rather than as a normal drive.
