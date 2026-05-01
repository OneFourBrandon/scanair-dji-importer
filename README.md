# ScanAir DJI Importer

Windows desktop helper for managing ScanAir DJI KMZ waypoint files by project and syncing one active project to a connected DJI RC 2 controller.

## What it does

- Stores KMZ files in local projects.
- Imports KMZ files by button or by dragging them into the app.
- Keeps each project's imported files under the app's storage folder at `projects\<project>\kmz\`.
- Tracks one active project that the ScanAir drone path creator can import into.
- Exposes a local HTTP endpoint for the website to send KMZ files later.
- Detects a DJI RC 2 controller through Windows MTP / This PC.
- Syncs the active project's KMZ files to:

```text
DJI RC 2\Internal shared storage\Android\data\dji.go.v5\files\waypoint
```

- Writes DJI's standard waypoint package layout: `waypoint\<mission-name>\<mission-name>.kmz`.
- Normalizes KMZ internals to DJI's expected `wpmz/template.kml` and `wpmz/waylines.wpml` paths when the input KMZ has those files at the root.
- Reads existing DJI waypoint packages from that same folder layout and leaves system folders such as `capability` and `map_preview` intact.
- Can back up the controller waypoint folder into local zip files and restore one of those backups later.
- Requires 10 identical DJI Fly dummy missions before the app unlocks.
- The dummy missions should be made by creating one path with at least two waypoints, then using Save As / duplicate until there are 10 matching copies.
- The app sorts matching dummy missions by KMZ creation time, oldest first, and remembers the controller-generated folder/KMZ IDs as slots 1-10 for the connected controller identity.
- The Dummy Slot Manager can give saved slots local names and reorder the saved sequence if DJI Fly's history order needs correction.
- Sync overwrites only the remembered dummy history slot KMZ files needed for the active project files.
- Unused slots are left unchanged to avoid unnecessary MTP delete prompts.
- Synced KMZ files are copied byte-for-byte and only renamed to the DJI slot filename, matching the manual replacement workflow. ShotSnap files and map preview thumbnails are left unchanged.
- Preserves calibration paths whose filenames match common calibration naming patterns.

## Run

```powershell
python -m scanair_dji_importer
```

The app starts a local HTTP server on `http://127.0.0.1:8765`.

On first run, the app shows a setup window until it detects 10 identical dummy missions on the connected RC 2. Create one waypoint mission in DJI Fly with at least two waypoints, then use Save As / duplicate until there are 10 copies of the same path. After detection, the app remembers the slot IDs for that physical controller, so those slots can later contain replaced KMZ files with any waypoint count.

Drag one or more `.kmz` files onto the drop area or stored-file list to import them into the active project.

Drag-and-drop uses `tkinterdnd2`. Install it into the Python interpreter used by PyCharm if the drop area says drag-and-drop is unavailable:

```powershell
C:\Python314\python.exe -m pip install tkinterdnd2
```

## Website handoff contract

You can send KMZ files to this app by using:

```http
POST http://127.0.0.1:8765/import
Content-Type: multipart/form-data
```

Form fields:

- `files`: one or more `.kmz` files.
- `project`: optional project name. If omitted, the current active project is used.
- `replace`: optional boolean-ish value (`true`, `1`, `yes`) to replace the project's stored files before import.

Useful endpoints:

- `GET /health`
- `GET /projects`
- `POST /active-project` with JSON body `{ "project": "Project Name" }`
- `POST /import`

## DJI controller note

The DJI RC 2 is exposed by Windows as an MTP shell device, not a normal drive letter. This app uses Windows PowerShell's `Shell.Application` COM API to navigate and copy into the controller.

Because DJI Fly appears to expose waypoint missions through history/indexed entries, this app uses 10 duplicate dummy missions as fixed slots. The controller folder and KMZ filenames are treated as DJI-generated IDs; the app discovers those IDs from the matching dummy KMZ waypoint signatures and creation times, then stores them under the connected controller's Windows hardware ID. If Windows does not expose that ID, it falls back to the detected MTP controller name.

The backup manager stores zip backups locally before destructive workflows such as restore. Windows MTP delete operations may still show Explorer confirmation dialogs because the controller is exposed through the Shell namespace rather than as a normal drive.
