import shutil
import unittest
from pathlib import Path
from zipfile import ZipFile

from scanair_dji_importer.dji_mtp import (
    ControllerIdentity,
    DeviceFile,
    DummySlot,
    DummySlotVerification,
    build_slot_sync_files,
    build_sync_packages,
    clear_controller_mapping,
    extract_kmz_mission_name,
    extract_kmz_create_time_ms,
    extract_kmz_waypoint_signature,
    is_calibration_filename,
    normalize_kmz_payload,
    package_name_for_kmz,
    save_controller_mapping,
    update_controller_slot_mapping,
    verify_dummy_slots,
)


def write_kmz(path: Path, document_name: str | None = None, create_time: int = 1000, coordinate_offset: float = 0) -> None:
    name_xml = f"<name>{document_name}</name>" if document_name else ""
    waypoint_xml = (
        f"<Placemark><wpml:index>0</wpml:index><Point><coordinates>{coordinate_offset},0,20</coordinates></Point></Placemark>"
        f"<Placemark><wpml:index>1</wpml:index><Point><coordinates>{coordinate_offset + 0.00001},0,20</coordinates></Point></Placemark>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "template.kml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.uav.com/wpmz/1.0.2"><Document>'
            f"{name_xml}"
            f"<wpml:createTime>{create_time}</wpml:createTime>"
            f"<Folder>{waypoint_xml}</Folder>"
            "</Document></kml>",
        )
        archive.writestr(
            "waylines.wpml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.uav.com/wpmz/1.0.2"><Document>'
            f"{name_xml}"
            f"<wpml:createTime>{create_time}</wpml:createTime>"
            f"<Folder>{waypoint_xml}</Folder>"
            "</Document></kml>",
        )


class DjiMtpTests(unittest.TestCase):
    def test_build_sync_packages_matches_controller_folder_standard(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-packages"
        shutil.rmtree(root, ignore_errors=True)
        source_dir = root / "source"
        staging_dir = root / "staging"
        source_dir.mkdir(parents=True)
        source = source_dir / "roof-scan.kmz"
        write_kmz(source)
        try:
            build_sync_packages([source], staging_dir)

            package_dir = staging_dir / "packages" / "roof-scan"
            preview_dir = staging_dir / "map_preview" / "roof-scan"
            self.assertTrue(package_dir.is_dir())
            self.assertTrue((package_dir / "image" / "ShotSnap.json").exists())
            self.assertTrue((preview_dir / "roof-scan.jpg").exists())
            with ZipFile(package_dir / "roof-scan.kmz") as archive:
                self.assertEqual(sorted(archive.namelist()), ["wpmz/template.kml", "wpmz/waylines.wpml"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_package_and_calibration_names(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-name"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        source = root / "mission.kmz"
        try:
            write_kmz(source, document_name="Roof Mission")
            self.assertEqual(package_name_for_kmz(source), "Roof Mission")
            self.assertEqual(extract_kmz_mission_name(source), "Roof Mission")
            self.assertEqual(extract_kmz_create_time_ms(source), 1000)
            self.assertIsNotNone(extract_kmz_waypoint_signature(source))
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertTrue(is_calibration_filename("controller-calibration.kmz"))
        self.assertTrue(is_calibration_filename("calib-reference"))

    def test_normalize_kmz_payload_moves_root_files_under_wpmz(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-normalize"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        source = root / "mission.kmz"
        try:
            write_kmz(source)
            normalized = root / "normalized.kmz"
            normalized.write_bytes(normalize_kmz_payload(source))
            with ZipFile(normalized) as archive:
                self.assertEqual(sorted(archive.namelist()), ["wpmz/template.kml", "wpmz/waylines.wpml"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_normalize_kmz_payload_updates_create_time_when_requested(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-timestamp"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        source = root / "mission.kmz"
        normalized = root / "normalized.kmz"
        try:
            write_kmz(source, create_time=1000)
            normalized.write_bytes(normalize_kmz_payload(source, mission_name="1", timestamp_ms=1234567890))
            self.assertEqual(extract_kmz_mission_name(normalized), "1")
            self.assertEqual(extract_kmz_create_time_ms(normalized), 1234567890)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_slot_sync_files_keep_controller_ids_and_slot_names(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-slots"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        source = root / "customer-mission.kmz"
        second_source = root / "second-mission.kmz"
        staging = root / "staging"
        slots = [
            DummySlot(slot_name="1", package_name="ABC-ID", kmz_name="ABC-ID.kmz", has_image_folder=True),
            DummySlot(slot_name="2", package_name="DEF-ID", kmz_name="DEF-ID.kmz", has_image_folder=True),
        ]
        try:
            write_kmz(source, document_name="Customer Mission")
            write_kmz(second_source, document_name="Second Mission")
            build_slot_sync_files([source, second_source], slots, staging)

            first_kmz = staging / "1" / "ABC-ID.kmz"
            second_kmz = staging / "2" / "DEF-ID.kmz"
            self.assertTrue(first_kmz.exists())
            self.assertTrue(second_kmz.exists())
            self.assertEqual(extract_kmz_mission_name(first_kmz), "Customer Mission")
            self.assertEqual(extract_kmz_mission_name(second_kmz), "Second Mission")
            self.assertGreater(first_kmz.stat().st_mtime, second_kmz.stat().st_mtime)
            self.assertFalse((staging / "1" / "ShotSnap.json").exists())
            self.assertFalse((staging / "2" / "ShotSnap.json").exists())
            self.assertFalse((staging / "1" / "ABC-ID.jpg").exists())
            self.assertFalse((staging / "2" / "DEF-ID.jpg").exists())
            self.assertTrue((staging / "1" / "map_preview" / "ABC-ID" / "ABC-ID.jpg").exists())
            self.assertTrue((staging / "2" / "map_preview" / "DEF-ID" / "DEF-ID.jpg").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_slot_sync_files_leave_unused_slots_unbuilt(self) -> None:
        root = Path.cwd() / ".test-tmp" / "dji-active-slots-only"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        source = root / "customer-mission.kmz"
        staging = root / "staging"
        slots = [
            DummySlot(slot_name="1", package_name="ABC-ID", kmz_name="ABC-ID.kmz", has_image_folder=True),
            DummySlot(slot_name="2", package_name="DEF-ID", kmz_name="DEF-ID.kmz", has_image_folder=True),
        ]
        try:
            write_kmz(source, document_name="Customer Mission")
            build_slot_sync_files([source], slots, staging)

            self.assertTrue((staging / "1" / "ABC-ID.kmz").exists())
            self.assertTrue((staging / "1" / "map_preview" / "ABC-ID" / "ABC-ID.jpg").exists())
            self.assertFalse((staging / "2" / "DEF-ID.kmz").exists())
            self.assertFalse((staging / "2" / "map_preview" / "DEF-ID" / "DEF-ID.jpg").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_verify_dummy_slots_detects_identical_group_oldest_first_and_saves_mapping(self) -> None:
        import scanair_dji_importer.dji_mtp as dji_mtp

        original_inspect = dji_mtp.inspect_device_packages
        original_path = dji_mtp.CONTROLLER_MAPPINGS_PATH
        root = Path.cwd() / ".test-tmp" / "dji-verify"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        dji_mtp.CONTROLLER_MAPPINGS_PATH = root / "mappings.json"
        signature = "same"
        dji_mtp.inspect_device_packages = lambda: [
            DeviceFile(
                name=f"id-{index}.kmz",
                package_name=f"id-{index}",
                has_image_folder=True,
                is_calibration=False,
                create_time_ms=1000 + index,
                modified_at=f"2026-06-24T12:00:{index:02d}",
                waypoint_signature=signature,
            )
            for index in range(10)
        ]
        try:
            verification = verify_dummy_slots("Controller A")
            self.assertTrue(verification.ok)
            self.assertEqual(verification.slots[0].package_name, "id-0")
            self.assertEqual(verification.slots[-1].package_name, "id-9")
            saved_mapping = dji_mtp.load_controller_mapping("Controller A")
            self.assertEqual(saved_mapping[0]["create_time_ms"], 1000)
            self.assertEqual(saved_mapping[0]["modified_at"], "2026-06-24T12:00:00")

            dji_mtp.inspect_device_packages = lambda: [
                DeviceFile(
                    name=f"id-{index}.kmz",
                    package_name=f"id-{index}",
                    has_image_folder=True,
                    is_calibration=False,
                    create_time_ms=1,
                    modified_at=f"2026-06-25T12:00:{index:02d}",
                    waypoint_signature=f"changed-{index}",
                )
                for index in range(10)
            ]
            saved = verify_dummy_slots("Controller A")
            self.assertTrue(saved.ok)
            self.assertEqual(saved.source, "saved")
            self.assertEqual(saved.slots[0].create_time_ms, 1)
            self.assertEqual(saved.slots[0].modified_at, "2026-06-25T12:00:00")
        finally:
            dji_mtp.inspect_device_packages = original_inspect
            dji_mtp.CONTROLLER_MAPPINGS_PATH = original_path
            shutil.rmtree(root, ignore_errors=True)

    def test_verify_dummy_slots_requires_reset_when_saved_slot_is_missing(self) -> None:
        import scanair_dji_importer.dji_mtp as dji_mtp

        original_inspect = dji_mtp.inspect_device_packages
        original_path = dji_mtp.CONTROLLER_MAPPINGS_PATH
        root = Path.cwd() / ".test-tmp" / "dji-missing-slot"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        dji_mtp.CONTROLLER_MAPPINGS_PATH = root / "mappings.json"
        slots = [
            DummySlot(
                slot_name=str(index),
                package_name=f"id-{index}",
                kmz_name=f"id-{index}.kmz",
                has_image_folder=True,
            )
            for index in range(1, 11)
        ]
        save_controller_mapping("Controller A", slots)
        dji_mtp.inspect_device_packages = lambda: [
            DeviceFile(
                name=f"id-{index}.kmz",
                package_name=f"id-{index}",
                has_image_folder=True,
                is_calibration=False,
                create_time_ms=1000 + index,
                modified_at=f"2026-06-24T12:00:{index:02d}",
                waypoint_signature=f"changed-{index}",
            )
            for index in range(1, 10)
        ]
        try:
            verification = verify_dummy_slots("Controller A")
            self.assertFalse(verification.ok)
            self.assertEqual(verification.source, "saved")
            self.assertTrue(verification.requires_mapping_reset)
            self.assertEqual(verification.missing, ["10"])

            self.assertTrue(clear_controller_mapping("Controller A"))
            self.assertEqual(dji_mtp.load_controller_mapping("Controller A"), [])
            self.assertFalse(clear_controller_mapping("Controller A"))
        finally:
            dji_mtp.inspect_device_packages = original_inspect
            dji_mtp.CONTROLLER_MAPPINGS_PATH = original_path
            shutil.rmtree(root, ignore_errors=True)

    def test_verify_dummy_slots_recovers_saved_mapping_when_windows_hwid_is_unavailable(self) -> None:
        import scanair_dji_importer.dji_mtp as dji_mtp

        original_identity = dji_mtp.get_controller_identity
        original_inspect = dji_mtp.inspect_device_packages
        original_path = dji_mtp.CONTROLLER_MAPPINGS_PATH
        root = Path.cwd() / ".test-tmp" / "dji-fallback-identity"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        dji_mtp.CONTROLLER_MAPPINGS_PATH = root / "mappings.json"
        slots = [
            DummySlot(
                slot_name=str(index),
                package_name=f"id-{index}",
                kmz_name=f"id-{index}.kmz",
                has_image_folder=True,
            )
            for index in range(1, 11)
        ]
        hardware_identity = ControllerIdentity(
            key="controller-hardware-key",
            label="DJI RC 2 (hardware ID)",
            raw_id="USB\\DJI-HARDWARE-ID",
            source="windows-hwid",
        )
        save_controller_mapping(hardware_identity.key, slots, hardware_identity)
        dji_mtp.get_controller_identity = lambda: ControllerIdentity(
            key="controller-mtp-fallback",
            label="DJI RC 2 (mtp-name fallback)",
            raw_id="MTP:DJI RC 2",
            source="mtp-name",
        )
        dji_mtp.inspect_device_packages = lambda: [
            DeviceFile(
                name=f"id-{index}.kmz",
                package_name=f"id-{index}",
                has_image_folder=True,
                is_calibration=False,
                waypoint_signature=f"modified-{index}",
            )
            for index in range(1, 11)
        ]
        try:
            verification = verify_dummy_slots()

            self.assertTrue(verification.ok)
            self.assertEqual(verification.source, "saved")
            self.assertEqual(verification.controller_key, hardware_identity.key)
            self.assertEqual([slot.package_name for slot in verification.slots], [f"id-{index}" for index in range(1, 11)])
        finally:
            dji_mtp.get_controller_identity = original_identity
            dji_mtp.inspect_device_packages = original_inspect
            dji_mtp.CONTROLLER_MAPPINGS_PATH = original_path
            shutil.rmtree(root, ignore_errors=True)

    def test_update_controller_slot_mapping_renumbers_reordered_slots_and_keeps_local_names(self) -> None:
        import scanair_dji_importer.dji_mtp as dji_mtp

        original_identity = dji_mtp.get_controller_identity
        original_path = dji_mtp.CONTROLLER_MAPPINGS_PATH
        root = Path.cwd() / ".test-tmp" / "dji-slot-manager"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        dji_mtp.CONTROLLER_MAPPINGS_PATH = root / "mappings.json"
        dji_mtp.get_controller_identity = lambda: ControllerIdentity(
            key="controller-test",
            label="Test Controller",
            raw_id="USB\\TEST",
            source="windows-hwid",
        )
        slots = [
            {"slot_name": str(index), "package_name": f"id-{index}", "kmz_name": f"id-{index}.kmz", "local_name": f"Slot {index}"}
            for index in range(1, 11)
        ]
        try:
            update_controller_slot_mapping([slots[1], slots[0], *slots[2:]])
            saved = dji_mtp.load_controller_mapping("controller-test")
            self.assertEqual(saved[0]["slot_name"], "1")
            self.assertEqual(saved[0]["package_name"], "id-2")
            self.assertEqual(saved[0]["local_name"], "Slot 2")
            self.assertEqual(saved[1]["slot_name"], "2")
            self.assertEqual(saved[1]["package_name"], "id-1")
        finally:
            dji_mtp.get_controller_identity = original_identity
            dji_mtp.CONTROLLER_MAPPINGS_PATH = original_path
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
