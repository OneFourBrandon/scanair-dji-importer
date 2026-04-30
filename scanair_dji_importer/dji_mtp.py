from __future__ import annotations

import json
import os
import hashlib
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from xml.sax.saxutils import escape


DEVICE_NAME = "DJI RC 2"
WPML_NAMESPACE = "http://www.uav.com/wpmz/1.0.2"
MTP_MODIFIED_TIME_COMPENSATION_SECONDS = 12 * 60 * 60
WAYPOINT_PATH = [
    "Internal shared storage",
    "Android",
    "data",
    "dji.go.v5",
    "files",
    "waypoint",
]
CALIBRATION_TOKENS = ("calibration", "calibrate", "calib", "reference")
SYSTEM_WAYPOINT_FOLDERS = ("capability", "map_preview")
REQUIRED_DUMMY_NAMES = tuple(str(index) for index in range(1, 11))
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ScanAirDJIImporter"
CONTROLLER_MAPPINGS_PATH = APP_DIR / "controller-slot-mappings.json"
SYNC_STAGING_DIR = APP_DIR / "sync-staging"
INSPECT_STAGING_DIR = APP_DIR / "inspect-staging"
BACKUPS_DIR = APP_DIR / "backups"
BACKUP_STAGING_DIR = APP_DIR / "backup-staging"
RESTORE_STAGING_DIR = APP_DIR / "restore-staging"
PLACEHOLDER_JPG = bytes.fromhex(
    "ffd8ffe000104a46494600010101006000600000ffdb0043000302020302020303030304030304050805050404050a070706080c0a0c0c0b0a0b0b0d0e12100d0e110e0b0b1016101113141515150c0f171816141812141514ffdb00430103040405040509050509140d0b0d141414141414141414141414141414141414141414141414141414141414141414141414141414141414141414141414141414ffc00011080001000103012200021101031101ffc4001400010000000000000000000000000000000000000008ffc4001410010000000000000000000000000000000000000000ffda000c03010002110311003f00b2c001ffd9"
)


class DjiControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceFile:
    name: str
    is_calibration: bool
    package_name: str | None = None
    has_image_folder: bool = False
    mission_name: str | None = None
    package_name_matches_kmz: bool = False
    create_time_ms: int | None = None
    waypoint_signature: str | None = None


@dataclass(frozen=True)
class SyncResult:
    deleted: list[str]
    copied: list[str]
    preserved: list[str]
    skipped: list[str]


@dataclass(frozen=True)
class RestoreResult:
    deleted: list[str]
    restored: list[str]


@dataclass(frozen=True)
class DummySlot:
    slot_name: str
    package_name: str
    kmz_name: str
    has_image_folder: bool
    local_name: str | None = None


@dataclass(frozen=True)
class ControllerIdentity:
    key: str
    label: str
    raw_id: str
    source: str


@dataclass(frozen=True)
class DummySlotVerification:
    ok: bool
    slots: list[DummySlot]
    missing: list[str]
    duplicates: list[str]
    detected: list[str]
    controller_key: str = "Default"
    controller_label: str = DEVICE_NAME
    source: str = "detected"

    @property
    def message(self) -> str:
        if self.ok:
            if self.source == "saved":
                return f"Using saved dummy slot mapping for {self.controller_label}."
            return f"Detected and saved 10 duplicate dummy missions for {self.controller_label}."
        parts = [
            "Create one dummy waypoint mission on the DJI RC 2 with at least 2 waypoints.",
            "Then use DJI Fly's Save As / duplicate workflow until there are 10 identical copies of that same path.",
            "The app will sort those 10 copies by KMZ creation time, oldest first, and remember their controller-generated IDs as slots 1-10 for this controller.",
        ]
        if self.missing:
            if self.source == "saved":
                parts.append("Missing saved slot IDs: " + ", ".join(self.missing))
            else:
                parts.append("Need 10 matching dummy copies before slots can be assigned.")
        if self.duplicates:
            parts.append("Duplicate/ambiguous slots: " + ", ".join(self.duplicates))
        if self.detected:
            parts.append("Detected identical group sizes: " + ", ".join(self.detected))
        return "\n".join(parts)


def is_calibration_filename(filename: str | None) -> bool:
    lowered = Path(filename or "").stem.lower()
    return any(token in lowered for token in CALIBRATION_TOKENS)


def package_name_for_kmz(file_path: Path) -> str:
    metadata_name = extract_kmz_mission_name(file_path)
    return sanitize_package_name(metadata_name or file_path.stem)


def sanitize_package_name(name: str) -> str:
    safe = "".join(character if character.isalnum() or character in "._- " else "_" for character in name).strip(" ._")
    if not safe:
        raise DjiControllerError("Could not determine a safe mission package name from the KMZ.")
    return safe[:120]


def extract_kmz_mission_name(file_path: Path) -> str | None:
    try:
        with ZipFile(file_path) as archive:
            for candidate in ("wpmz/template.kml", "template.kml", "wpmz/waylines.wpml", "waylines.wpml"):
                if candidate not in archive.namelist():
                    continue
                name = extract_xml_mission_name(archive.read(candidate))
                if name:
                    return name
    except Exception:
        return None
    return None


def extract_kmz_create_time_ms(file_path: Path) -> int | None:
    try:
        with ZipFile(file_path) as archive:
            for candidate in ("wpmz/template.kml", "template.kml", "wpmz/waylines.wpml", "waylines.wpml"):
                if candidate not in archive.namelist():
                    continue
                create_time = extract_xml_create_time_ms(archive.read(candidate))
                if create_time is not None:
                    return create_time
    except Exception:
        return None
    return None


def extract_xml_create_time_ms(payload: bytes) -> int | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    for element in root.iter():
        if local_name(element.tag) == "createTime" and element.text:
            try:
                return int(float(element.text.strip()))
            except ValueError:
                return None
    return None


def extract_kmz_waypoint_signature(file_path: Path) -> str | None:
    try:
        with ZipFile(file_path) as archive:
            for candidate in ("wpmz/waylines.wpml", "waylines.wpml", "wpmz/template.kml", "template.kml"):
                if candidate not in archive.namelist():
                    continue
                signature = extract_xml_waypoint_signature(archive.read(candidate))
                if signature:
                    return signature
    except Exception:
        return None
    return None


def extract_xml_waypoint_signature(payload: bytes) -> str | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    waypoints = []
    for placemark in root.iter():
        if local_name(placemark.tag) != "Placemark":
            continue
        coordinates = None
        index = None
        height = None
        for child in placemark.iter():
            child_name = local_name(child.tag)
            if child_name == "coordinates" and child.text:
                coordinates = normalize_space(child.text)
            elif child_name == "index" and child.text:
                index = normalize_space(child.text)
            elif child_name in {"executeHeight", "height", "ellipsoidHeight"} and child.text and height is None:
                height = normalize_space(child.text)
        if coordinates:
            waypoints.append((int(index) if index and index.isdigit() else len(waypoints), coordinates, height or ""))
    if len(waypoints) < 2:
        return None
    normalized = json.dumps(sorted(waypoints), separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_space(value: str) -> str:
    return " ".join(value.split())


def extract_xml_mission_name(payload: bytes) -> str | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None

    document = first_child_by_local_name(root, "Document")
    if document is not None:
        direct_name = direct_child_text(document, "name")
        if direct_name and not direct_name.lower().startswith("waypoint "):
            return direct_name
        for child in list(document):
            if local_name(child.tag) != "Folder":
                continue
            folder_name = direct_child_text(child, "name")
            if folder_name and not folder_name.lower().startswith("waypoint "):
                return folder_name
    return None


def first_child_by_local_name(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if local_name(child.tag) == name:
            return child
    return None


def direct_child_text(element: ET.Element, name: str) -> str | None:
    for child in list(element):
        if local_name(child.tag) == name and child.text:
            value = child.text.strip()
            return value or None
    return None


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def verify_controller() -> str:
    payload = _run_ps(_ps_common() + r"""
$folder = Get-WaypointFolder
Write-Json ([pscustomobject]@{
  device = $script:Device.Name
  path = "This PC\" + $script:Device.Name + "\" + ($script:WaypointParts -join "\")
})
""")
    data = json.loads(payload)
    return data["path"]


def get_controller_identity() -> ControllerIdentity:
    payload = _run_ps(_ps_common() + r"""
$folder = Get-WaypointFolder
$rawId = $null
$identitySource = "mtp-name"
try {
  $pnp = Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.PNPClass -eq "WPD" -and ($_.Name -eq $script:Device.Name -or $_.Name -like "*DJI*") } |
    Select-Object -First 1
  if ($null -ne $pnp -and -not [string]::IsNullOrWhiteSpace($pnp.DeviceID)) {
    $rawId = $pnp.DeviceID
    $identitySource = "windows-hwid"
  }
} catch {
  $rawId = $null
}
if ([string]::IsNullOrWhiteSpace($rawId)) {
  $rawId = "MTP:" + $script:Device.Name
}
Write-Json ([pscustomobject]@{
  name = $script:Device.Name
  raw_id = $rawId
  source = $identitySource
})
""")
    data = json.loads(payload)
    raw_id = str(data.get("raw_id") or f"MTP:{DEVICE_NAME}")
    source = str(data.get("source") or "mtp-name")
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
    if source == "windows-hwid":
        label = f"{data.get('name') or DEVICE_NAME} ({raw_id})"
    else:
        label = f"{data.get('name') or DEVICE_NAME} ({source} fallback)"
    return ControllerIdentity(
        key=f"controller-{digest}",
        label=label,
        raw_id=raw_id,
        source=source,
    )


def list_device_files() -> list[DeviceFile]:
    return inspect_device_packages()


def verify_dummy_slots(controller_key: str | None = None) -> DummySlotVerification:
    identity = get_controller_identity() if controller_key is None else None
    mapping_key = controller_key or identity.key
    controller_label = identity.label if identity else mapping_key
    files = inspect_device_packages()
    saved = load_controller_mapping(mapping_key)
    if saved:
        by_package = {file.package_name: file for file in files if file.package_name}
        slots = []
        missing = []
        for item in sorted(saved, key=lambda row: int(row["slot_name"])):
            current = by_package.get(item["package_name"])
            if current is None or current.name != item["kmz_name"]:
                missing.append(item["slot_name"])
                continue
            slots.append(
                DummySlot(
                    slot_name=item["slot_name"],
                    package_name=item["package_name"],
                    kmz_name=item["kmz_name"],
                    has_image_folder=current.has_image_folder,
                    local_name=item.get("local_name"),
                )
            )
        if not missing and len(slots) == len(REQUIRED_DUMMY_NAMES):
            return DummySlotVerification(
                ok=True,
                slots=slots,
                missing=[],
                duplicates=[],
                detected=[],
                controller_key=mapping_key,
                controller_label=controller_label,
                source="saved",
            )

    groups: dict[str, list[DeviceFile]] = {}
    for file in files:
        if file.package_name and file.name and file.waypoint_signature and not file.is_calibration:
            groups.setdefault(file.waypoint_signature, []).append(file)
    candidates = [group for group in groups.values() if len(group) >= len(REQUIRED_DUMMY_NAMES)]
    detected_sizes = sorted((str(len(group)) for group in groups.values()), reverse=True)
    if not candidates:
        return DummySlotVerification(
            ok=False,
            slots=[],
            missing=list(REQUIRED_DUMMY_NAMES),
            duplicates=[],
            detected=detected_sizes,
            controller_key=mapping_key,
            controller_label=controller_label,
            source="detected",
        )

    selected_group = sorted(
        max(candidates, key=lambda group: len(group)),
        key=lambda file: (file.create_time_ms if file.create_time_ms is not None else 9999999999999, file.package_name or ""),
    )[: len(REQUIRED_DUMMY_NAMES)]
    slots = [
        DummySlot(
            slot_name=str(index),
            package_name=file.package_name or "",
            kmz_name=file.name,
            has_image_folder=file.has_image_folder,
            local_name=None,
        )
        for index, file in enumerate(selected_group, start=1)
    ]
    save_controller_mapping(mapping_key, slots, identity)
    return DummySlotVerification(
        ok=True,
        slots=slots,
        missing=[],
        duplicates=[],
        detected=[str(len(selected_group))],
        controller_key=mapping_key,
        controller_label=controller_label,
        source="detected",
    )


def read_controller_mappings() -> dict:
    try:
        return json.loads(CONTROLLER_MAPPINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_controller_mappings(data: dict) -> None:
    CONTROLLER_MAPPINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROLLER_MAPPINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_controller_mapping(controller_key: str) -> list[dict]:
    data = read_controller_mappings()
    slots = data.get(controller_key, {}).get("slots", [])
    return slots if isinstance(slots, list) else []


def get_controller_slot_mapping() -> tuple[ControllerIdentity, list[dict]]:
    identity = get_controller_identity()
    return identity, load_controller_mapping(identity.key)


def update_controller_slot_mapping(slots: list[dict]) -> ControllerIdentity:
    identity = get_controller_identity()
    if len(slots) != len(REQUIRED_DUMMY_NAMES):
        raise DjiControllerError("A controller mapping must contain exactly 10 dummy slots.")
    normalized_slots = []
    for index, slot in enumerate(slots, start=1):
        package_name = str(slot.get("package_name") or "").strip()
        kmz_name = str(slot.get("kmz_name") or "").strip()
        if not package_name or not kmz_name:
            raise DjiControllerError("Each dummy slot must have a package name and KMZ filename.")
        normalized_slots.append(
            {
                "slot_name": str(index),
                "package_name": package_name,
                "kmz_name": kmz_name,
                "local_name": str(slot.get("local_name") or "").strip(),
            }
        )
    data = read_controller_mappings()
    existing = data.get(identity.key, {})
    data[identity.key] = {
        **existing,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "controller_label": identity.label,
        "controller_raw_id": identity.raw_id,
        "controller_source": identity.source,
        "slots": normalized_slots,
    }
    write_controller_mappings(data)
    return identity


def save_controller_mapping(controller_key: str, slots: list[DummySlot], identity: ControllerIdentity | None = None) -> None:
    data = read_controller_mappings()
    data[controller_key] = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "controller_label": identity.label if identity else controller_key,
        "controller_raw_id": identity.raw_id if identity else controller_key,
        "controller_source": identity.source if identity else "manual",
        "slots": [
            {
                "slot_name": slot.slot_name,
                "package_name": slot.package_name,
                "kmz_name": slot.kmz_name,
                "local_name": slot.local_name or "",
            }
            for slot in sorted(slots, key=lambda item: int(item.slot_name))
        ],
    }
    write_controller_mappings(data)


def inspect_device_packages() -> list[DeviceFile]:
    if INSPECT_STAGING_DIR.exists():
        shutil.rmtree(INSPECT_STAGING_DIR)
    INSPECT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    payload = _run_ps(
        _ps_common()
        + f"""
$inspectRoot = ConvertFrom-Json @'
{json.dumps(str(INSPECT_STAGING_DIR))}
'@
New-Item -ItemType Directory -Force -Path $inspectRoot | Out-Null
$folder = Get-WaypointFolder
$items = @()
$index = 0
foreach ($item in @($folder.Items())) {{
  if ($item.IsFolder) {{
    if (Test-SystemWaypointFolder $item.Name) {{
      continue
    }}
    $package = $item.GetFolder
    $expectedKmz = $item.Name + ".kmz"
    $kmz = Find-Child $package $expectedKmz
    if ($null -eq $kmz) {{
      foreach ($child in @($package.Items())) {{
        if (-not $child.IsFolder -and $child.Name.ToLower().EndsWith(".kmz")) {{
          $kmz = $child
          break
        }}
      }}
    }}
    $localKmz = $null
    if ($null -ne $kmz) {{
      $localDir = Join-Path $inspectRoot ("item" + $index)
      New-Item -ItemType Directory -Force -Path $localDir | Out-Null
      $localFolder = (New-Object -ComObject Shell.Application).Namespace($localDir)
      $localFolder.CopyHere($kmz, 16)
      $localKmz = Wait-ForLocalChild $localDir $kmz.Name
    }}
    $items += [pscustomobject]@{{
      name = if ($null -ne $kmz) {{ $kmz.Name }} else {{ $null }}
      package_name = $item.Name
      has_image_folder = ($null -ne (Find-Child $package "image"))
      local_kmz_path = $localKmz
      loose = $false
    }}
    $index += 1
    continue
  }}
  if ($item.Name.ToLower().EndsWith(".kmz")) {{
    $localDir = Join-Path $inspectRoot ("item" + $index)
    New-Item -ItemType Directory -Force -Path $localDir | Out-Null
    $localFolder = (New-Object -ComObject Shell.Application).Namespace($localDir)
    $localFolder.CopyHere($item, 16)
    $localKmz = Wait-ForLocalChild $localDir $item.Name
    $items += [pscustomobject]@{{
      name = $item.Name
      package_name = $null
      has_image_folder = $false
      local_kmz_path = $localKmz
      loose = $true
    }}
    $index += 1
  }}
}}
Write-Json $items
"""
    )
    try:
        raw_items = json.loads(payload or "[]")
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        files = []
        for item in raw_items:
            local_path = item.get("local_kmz_path")
            mission_name = extract_kmz_mission_name(Path(local_path)) if local_path else None
            package_name = item.get("package_name")
            kmz_name = item.get("name") or ""
            expected_from_file = Path(kmz_name).stem if kmz_name else None
            try:
                expected_from_mission = sanitize_package_name(mission_name) if mission_name else None
            except DjiControllerError:
                expected_from_mission = None
            package_matches = bool(
                package_name
                and (
                    package_name == expected_from_file
                    or (expected_from_mission is not None and package_name == expected_from_mission)
                )
            )
            files.append(
                DeviceFile(
                    name=kmz_name,
                    package_name=package_name,
                    has_image_folder=bool(item.get("has_image_folder")),
                    mission_name=mission_name,
                    create_time_ms=extract_kmz_create_time_ms(Path(local_path)) if local_path else None,
                    waypoint_signature=extract_kmz_waypoint_signature(Path(local_path)) if local_path else None,
                    package_name_matches_kmz=package_matches,
                    is_calibration=(
                        is_calibration_filename(package_name)
                        or is_calibration_filename(kmz_name)
                        or is_calibration_filename(mission_name)
                    ),
                )
            )
        return files
    finally:
        shutil.rmtree(INSPECT_STAGING_DIR, ignore_errors=True)


def sync_files(files: list[Path], controller_key: str | None = None) -> SyncResult:
    for file_path in files:
        if not file_path.exists():
            raise DjiControllerError(f"Missing local file: {file_path}")
        if file_path.suffix.lower() != ".kmz":
            raise DjiControllerError(f"Only KMZ files can be synced: {file_path.name}")
    if len(files) > len(REQUIRED_DUMMY_NAMES):
        raise DjiControllerError("The RC2 slot workflow supports up to 10 active KMZ files.")

    verification = verify_dummy_slots(controller_key)
    if not verification.ok:
        raise DjiControllerError(verification.message)

    active_slots = sorted(verification.slots, key=lambda item: int(item.slot_name))[: len(files)]
    staging_root = build_slot_sync_files(files, active_slots)
    slot_payloads = json.dumps(
        [
            {
                "slot_name": slot.slot_name,
                "package_name": slot.package_name,
                "kmz_name": slot.kmz_name,
                "kmz_path": str((staging_root / slot.slot_name / slot.kmz_name).resolve()),
            }
            for slot in active_slots
        ]
    )
    script = _ps_common() + f"""
$slotPayloads = ConvertFrom-Json @'
{slot_payloads}
'@
$folder = Get-WaypointFolder
$copied = @()
$skipped = @()
$copyFlags = 4 + 16 + 512 + 1024

function Remove-ExistingChild($folderObject, $name) {{
  $existing = Find-Child $folderObject $name
  if ($null -eq $existing) {{
    return
  }}
  $existing.InvokeVerb("delete")
  if (-not (Wait-ForMissingChild $folderObject $name)) {{
    throw "Windows did not delete existing controller file before replacement: $name"
  }}
}}

foreach ($slot in @($slotPayloads)) {{
  $package = Find-Child $folder $slot.package_name
  if ($null -eq $package -or -not $package.IsFolder) {{
    throw "Dummy mission folder disappeared from controller: $($slot.package_name)"
  }}
  $packageFolder = $package.GetFolder
  $image = Find-Child $packageFolder "image"
  if ($null -eq $image -or -not $image.IsFolder) {{
    throw "Dummy mission $($slot.slot_name) is missing its image folder. Recreate that dummy mission in DJI Fly."
  }}
  Remove-ExistingChild $packageFolder $slot.kmz_name
  $packageFolder.CopyHere($slot.kmz_path, $copyFlags)
  Wait-ForChild $packageFolder $slot.kmz_name | Out-Null
  $copied += $slot.slot_name
}}
Write-Json ([pscustomobject]@{{ deleted = @(); copied = $copied; preserved = @(); skipped = $skipped }})
"""
    try:
        payload = _run_ps(script)
        data = json.loads(payload)
        return SyncResult(
            deleted=json_list(data.get("deleted")),
            copied=json_list(data.get("copied")),
            preserved=json_list(data.get("preserved")),
            skipped=json_list(data.get("skipped")),
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def build_sync_packages(files: list[Path], staging_dir: Path = SYNC_STAGING_DIR) -> Path:
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    package_root = staging_dir / "packages"
    preview_root = staging_dir / "map_preview"
    package_root.mkdir(parents=True, exist_ok=True)
    preview_root.mkdir(parents=True, exist_ok=True)
    for index, file_path in enumerate(files):
        package_name = package_name_for_kmz(file_path)
        package_dir = package_root / package_name
        image_dir = package_dir / "image"
        preview_dir = preview_root / package_name
        image_dir.mkdir(parents=True, exist_ok=True)
        preview_dir.mkdir(parents=True, exist_ok=True)
        normalized_payload = normalize_kmz_payload(file_path, timestamp_ms=current_time_ms())
        kmz_path = package_dir / f"{package_name}.kmz"
        shotsnap_path = image_dir / "ShotSnap.json"
        preview_path = preview_dir / f"{package_name}.jpg"
        kmz_path.write_bytes(normalized_payload)
        shotsnap_path.write_text('{"WAY_POINT":{},"POI_POINT":{}}\n', encoding="utf-8")
        write_numbered_preview_jpg(preview_path, str(index + 1))
        apply_mtp_modified_time(kmz_path, shotsnap_path, preview_path)
    return staging_dir


def build_slot_sync_files(files: list[Path], slots: list[DummySlot], staging_dir: Path = SYNC_STAGING_DIR) -> Path:
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    for index, slot in enumerate(sorted(slots, key=lambda item: int(item.slot_name))):
        slot_dir = staging_dir / slot.slot_name
        slot_dir.mkdir(parents=True, exist_ok=True)
        if index >= len(files):
            break
        kmz_path = slot_dir / slot.kmz_name
        shutil.copyfile(files[index], kmz_path)
        apply_mtp_modified_time(kmz_path)
    return staging_dir


def normalize_kmz_payload(file_path: Path, mission_name: str | None = None, timestamp_ms: int | None = None) -> bytes:
    with ZipFile(file_path) as source:
        with BytesIO() as buffer:
            with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as target:
                seen: set[str] = set()
                for item in source.infolist():
                    if item.is_dir():
                        continue
                    target_name = normalized_kmz_entry_name(item.filename)
                    if target_name in seen:
                        continue
                    seen.add(target_name)
                    payload = source.read(item.filename)
                    if target_name in {"wpmz/template.kml", "wpmz/waylines.wpml"}:
                        payload = update_kmz_xml_metadata(payload, mission_name, timestamp_ms)
                    target.writestr(zip_info_for(target_name, timestamp_ms), payload)
                if "wpmz/template.kml" not in seen or "wpmz/waylines.wpml" not in seen:
                    raise DjiControllerError(
                        f"{file_path.name} must contain template.kml and waylines.wpml, either at the KMZ root or under wpmz/."
                    )
            return buffer.getvalue()


def normalized_kmz_entry_name(name: str) -> str:
    clean_name = name.replace("\\", "/").lstrip("/")
    if clean_name in {"template.kml", "waylines.wpml"}:
        return f"wpmz/{clean_name}"
    return clean_name


def update_kmz_xml_metadata(payload: bytes, mission_name: str | None = None, timestamp_ms: int | None = None) -> bytes:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return payload
    if timestamp_ms is not None:
        set_kmz_time_fields(root, timestamp_ms)
    if mission_name is None:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    document = first_child_by_local_name(root, "Document")
    if document is None:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    name_child = None
    for child in list(document):
        if local_name(child.tag) == "name":
            name_child = child
            break
    if name_child is None:
        namespace = document.tag.split("}", 1)[0][1:] if document.tag.startswith("{") else ""
        tag = f"{{{namespace}}}name" if namespace else "name"
        name_child = ET.Element(tag)
        document.insert(0, name_child)
    name_child.text = mission_name
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def set_kmz_time_fields(root: ET.Element, timestamp_ms: int) -> None:
    for field_name in ("createTime", "updateTime"):
        found = False
        for element in root.iter():
            if local_name(element.tag) == field_name:
                element.text = str(timestamp_ms)
                found = True
        if not found:
            document = first_child_by_local_name(root, "Document")
            if document is not None:
                child = ET.Element(namespaced_tag(WPML_NAMESPACE, field_name))
                child.text = str(timestamp_ms)
                document.insert(1, child)


def namespace_for_element(element: ET.Element) -> str:
    return element.tag.split("}", 1)[0][1:] if element.tag.startswith("{") else ""


def namespaced_tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}" if namespace else name


def current_time_ms() -> int:
    return int(time.time() * 1000)


def zip_info_for(name: str, timestamp_ms: int | None = None) -> ZipInfo:
    timestamp = timestamp_ms / 1000 if timestamp_ms is not None else time.time()
    date_time = datetime.fromtimestamp(timestamp).timetuple()[:6]
    if date_time[0] < 1980:
        date_time = (1980, 1, 1, 0, 0, 0)
    info = ZipInfo(name, date_time=date_time)
    info.compress_type = ZIP_DEFLATED
    return info


def apply_mtp_modified_time(*paths: Path) -> None:
    timestamp = time.time() - MTP_MODIFIED_TIME_COMPENSATION_SECONDS
    for path in paths:
        os.utime(path, (timestamp, timestamp))


def write_numbered_preview_jpg(path: Path, label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"path": str(path), "label": label})
        _run_ps(
            f"""
$payload = ConvertFrom-Json @'
{payload}
'@
Add-Type -AssemblyName System.Drawing
$width = 1920
$height = 1080
$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(242, 246, 250))
$borderPen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(36, 75, 106)), 36
$graphics.DrawRectangle($borderPen, 18, 18, ($width - 36), ($height - 36))
$fontSize = if ($payload.label.Length -gt 1) {{ 560 }} else {{ 700 }}
$font = New-Object System.Drawing.Font "Segoe UI", $fontSize, ([System.Drawing.FontStyle]::Bold), ([System.Drawing.GraphicsUnit]::Pixel)
$brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(16, 44, 67))
$format = New-Object System.Drawing.StringFormat
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$rect = New-Object System.Drawing.RectangleF 0, 0, $width, $height
$graphics.DrawString($payload.label, $font, $brush, $rect, $format)
$bitmap.Save($payload.path, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$format.Dispose()
$brush.Dispose()
$font.Dispose()
$borderPen.Dispose()
$graphics.Dispose()
$bitmap.Dispose()
"""
        )
    except Exception:
        path.write_bytes(PLACEHOLDER_JPG)


def build_dummy_kmz_payload(mission_name: str, timestamp_ms: int | None = None) -> bytes:
    timestamp = timestamp_ms if timestamp_ms is not None else current_time_ms()
    template = build_dummy_template_kml(mission_name, timestamp)
    waylines = build_dummy_waylines_wpml(mission_name, timestamp)
    with BytesIO() as buffer:
        with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
            archive.writestr(zip_info_for("wpmz/template.kml", timestamp), template)
            archive.writestr(zip_info_for("wpmz/waylines.wpml", timestamp), waylines)
        return buffer.getvalue()


def build_dummy_template_kml(mission_name: str, timestamp_ms: int) -> str:
    name = escape(mission_name)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.uav.com/wpmz/1.0.2">
  <Document>
    <name>{name}</name>
    <wpml:createTime>{timestamp_ms}</wpml:createTime>
    <wpml:updateTime>{timestamp_ms}</wpml:updateTime>
    <wpml:author>ScanAir</wpml:author>
    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>noAction</wpml:finishAction>
      <wpml:exitOnRCLost>executeLostAction</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>1</wpml:globalTransitionalSpeed>
    </wpml:missionConfig>
    <Folder>
      <wpml:templateId>0</wpml:templateId>
      <wpml:autoFlightSpeed>1</wpml:autoFlightSpeed>
      <Placemark><name>Waypoint 1</name><Point><coordinates>0,0,20</coordinates></Point><wpml:index>0</wpml:index></Placemark>
      <Placemark><name>Waypoint 2</name><Point><coordinates>0.00001,0,20</coordinates></Point><wpml:index>1</wpml:index></Placemark>
    </Folder>
  </Document>
</kml>
'''


def build_dummy_waylines_wpml(mission_name: str, timestamp_ms: int) -> str:
    name = escape(mission_name)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.uav.com/wpmz/1.0.2">
  <Document>
    <name>{name}</name>
    <wpml:createTime>{timestamp_ms}</wpml:createTime>
    <wpml:updateTime>{timestamp_ms}</wpml:updateTime>
    <wpml:missionConfig>
      <wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
      <wpml:finishAction>noAction</wpml:finishAction>
      <wpml:exitOnRCLost>executeLostAction</wpml:exitOnRCLost>
      <wpml:executeRCLostAction>goBack</wpml:executeRCLostAction>
      <wpml:globalTransitionalSpeed>1</wpml:globalTransitionalSpeed>
    </wpml:missionConfig>
    <Folder>
      <wpml:templateId>0</wpml:templateId>
      <wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>
      <wpml:waylineId>0</wpml:waylineId>
      <wpml:distance>1</wpml:distance>
      <wpml:duration>1</wpml:duration>
      <wpml:autoFlightSpeed>1</wpml:autoFlightSpeed>
      <Placemark><name>Waypoint 1</name><Point><coordinates>0,0,20</coordinates></Point><wpml:index>0</wpml:index><wpml:executeHeight>20</wpml:executeHeight><wpml:waypointSpeed>1</wpml:waypointSpeed></Placemark>
      <Placemark><name>Waypoint 2</name><Point><coordinates>0.00001,0,20</coordinates></Point><wpml:index>1</wpml:index><wpml:executeHeight>20</wpml:executeHeight><wpml:waypointSpeed>1</wpml:waypointSpeed></Placemark>
    </Folder>
  </Document>
</kml>
'''


def create_waypoint_backup() -> Path:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    if BACKUP_STAGING_DIR.exists():
        shutil.rmtree(BACKUP_STAGING_DIR)
    waypoint_staging = BACKUP_STAGING_DIR / "waypoint"
    waypoint_staging.mkdir(parents=True, exist_ok=True)
    backup_name = f"dji-waypoint-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    backup_path = BACKUPS_DIR / backup_name
    _run_ps(
        _ps_common()
        + f"""
$backupTarget = ConvertFrom-Json @'
{json.dumps(str(waypoint_staging))}
'@
New-Item -ItemType Directory -Force -Path $backupTarget | Out-Null
$targetFolder = (New-Object -ComObject Shell.Application).Namespace($backupTarget)
$folder = Get-WaypointFolder
$copied = @()
foreach ($item in @($folder.Items())) {{
  $targetFolder.CopyHere($item, 16)
  $copied += $item.Name
  Wait-ForLocalChild $backupTarget $item.Name | Out-Null
}}
Write-Json ([pscustomobject]@{{ copied = $copied }})
"""
    )
    try:
        with ZipFile(backup_path, mode="w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(BACKUP_STAGING_DIR.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(BACKUP_STAGING_DIR))
        return backup_path
    finally:
        shutil.rmtree(BACKUP_STAGING_DIR, ignore_errors=True)


def list_backup_files() -> list[Path]:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(BACKUPS_DIR.glob("*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)


def restore_waypoint_backup(backup_path: Path) -> RestoreResult:
    if not backup_path.exists():
        raise DjiControllerError(f"Backup does not exist: {backup_path}")
    if RESTORE_STAGING_DIR.exists():
        shutil.rmtree(RESTORE_STAGING_DIR)
    RESTORE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    with ZipFile(backup_path) as archive:
        archive.extractall(RESTORE_STAGING_DIR)
    source = RESTORE_STAGING_DIR / "waypoint"
    if not source.exists():
        source = RESTORE_STAGING_DIR
    payload = _run_ps(
        _ps_common()
        + f"""
$restoreSource = ConvertFrom-Json @'
{json.dumps(str(source))}
'@
if (-not (Test-Path -LiteralPath $restoreSource -PathType Container)) {{
  throw "Backup restore source folder was not found: $restoreSource"
}}
$shell = New-Object -ComObject Shell.Application
$sourceFolder = $shell.Namespace($restoreSource)
$folder = Get-WaypointFolder
$deleted = @()
foreach ($item in @($folder.Items())) {{
  $deleted += $item.Name
  $item.InvokeVerb("delete")
  if (-not (Wait-ForMissingChild $folder $item.Name)) {{
    throw "Windows did not delete waypoint item during restore: $($item.Name)"
  }}
}}
$restored = @()
foreach ($item in @($sourceFolder.Items())) {{
  $folder.CopyHere($item.Path, 16)
  $restored += $item.Name
  Start-Sleep -Milliseconds 1000
}}
Write-Json ([pscustomobject]@{{ deleted = $deleted; restored = $restored }})
"""
    )
    try:
        data = json.loads(payload)
        return RestoreResult(deleted=json_list(data.get("deleted")), restored=json_list(data.get("restored")))
    finally:
        shutil.rmtree(RESTORE_STAGING_DIR, ignore_errors=True)


def json_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _run_ps(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "PowerShell MTP command failed.").strip()
        raise DjiControllerError(message)
    return completed.stdout.strip()


def _ps_common() -> str:
    parts_json = json.dumps(WAYPOINT_PATH)
    tokens_json = json.dumps(CALIBRATION_TOKENS)
    system_folders_json = json.dumps(SYSTEM_WAYPOINT_FOLDERS)
    return f"""
$ErrorActionPreference = "Stop"
$script:DeviceName = "{DEVICE_NAME}"
$script:WaypointParts = ConvertFrom-Json @'
{parts_json}
'@
$script:CalibrationTokens = ConvertFrom-Json @'
{tokens_json}
'@
$script:SystemWaypointFolders = ConvertFrom-Json @'
{system_folders_json}
'@

function Write-Json($value) {{
  $value | ConvertTo-Json -Depth 8 -Compress
}}

function Test-CalibrationName($name) {{
  $stem = [System.IO.Path]::GetFileNameWithoutExtension($name).ToLower()
  foreach ($token in $script:CalibrationTokens) {{
    if ($stem.Contains($token)) {{ return $true }}
  }}
  return $false
}}

function Test-SystemWaypointFolder($name) {{
  foreach ($folderName in $script:SystemWaypointFolders) {{
    if ($name -eq $folderName) {{ return $true }}
  }}
  return $false
}}

function Find-Child($folder, $name) {{
  foreach ($item in @($folder.Items())) {{
    if ($item.Name -eq $name) {{ return $item }}
  }}
  return $null
}}

function Wait-ForLocalChild($folderPath, $name) {{
  $target = Join-Path $folderPath $name
  for ($i = 0; $i -lt 60; $i++) {{
    if (Test-Path -LiteralPath $target) {{ return $target }}
    Start-Sleep -Milliseconds 500
  }}
  return $null
}}

function Get-WaypointPackageNames($waypointFolder) {{
  $names = @()
  foreach ($item in @($waypointFolder.Items())) {{
    if ($item.IsFolder -and -not (Test-SystemWaypointFolder $item.Name)) {{
      $names += $item.Name
    }}
  }}
  return $names
}}

function Wait-ForWaypointPackage($waypointFolder, $packageName) {{
  for ($i = 0; $i -lt 30; $i++) {{
    $item = Find-Child $waypointFolder $packageName
    if ($null -ne $item -and $item.IsFolder) {{
      return $true
    }}
    Start-Sleep -Milliseconds 500
  }}
  return $false
}}

function Wait-ForMissingChild($folder, $name) {{
  for ($i = 0; $i -lt 120; $i++) {{
    if ($null -eq (Find-Child $folder $name)) {{
      return $true
    }}
    Start-Sleep -Milliseconds 500
  }}
  return $false
}}

function Wait-ForChild($folder, $name) {{
  for ($i = 0; $i -lt 120; $i++) {{
    $item = Find-Child $folder $name
    if ($null -ne $item) {{
      return $item
    }}
    Start-Sleep -Milliseconds 500
  }}
  throw "Windows did not finish copying controller item: $name"
}}

function Get-WaypointFolder {{
  $shell = New-Object -ComObject Shell.Application
  $computer = $shell.Namespace(17)
  if ($null -eq $computer) {{
    throw "Windows shell namespace 'This PC' is unavailable."
  }}
  $script:Device = Find-Child $computer $script:DeviceName
  if ($null -eq $script:Device) {{
    throw "DJI RC 2 controller was not found. Connect the controller by USB, unlock it, and choose file transfer if prompted."
  }}
  $folder = $script:Device.GetFolder
  foreach ($part in $script:WaypointParts) {{
    $child = Find-Child $folder $part
    if ($null -eq $child) {{
      throw "Could not find controller folder: $part. Expected path: This PC\\$($script:DeviceName)\\$($script:WaypointParts -join '\\')"
    }}
    $folder = $child.GetFolder
  }}
  return $folder
}}
"""
