import unittest
import shutil
import json
from pathlib import Path
from unittest.mock import patch

from scanair_dji_importer.credential_protection import (
    CredentialProtectionError,
    protect_secret,
    unprotect_secret,
)
from scanair_dji_importer.store import ProjectStore, sanitize_filename


class ProjectStoreTests(unittest.TestCase):
    def test_project_lifecycle_and_active_files(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-lifecycle"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            store.create_project("Roof A")
            store.set_active_project("Roof A")

            source = root / "mission.kmz"
            source.write_bytes(b"kmz-data")
            imported = store.add_files([source])

            self.assertEqual(store.get_active_project_name(), "Roof A")
            self.assertEqual(imported[0].name, "mission.kmz")
            self.assertEqual(imported[0].path.parent.name, "kmz")
            self.assertEqual([path.name for path in store.active_files()], ["mission.kmz"])

            store.delete_project("Roof A")
            self.assertIsNone(store.get_active_project_name())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_migrates_old_flat_project_files_into_kmz_folder(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-migration"
        shutil.rmtree(root, ignore_errors=True)
        project = root / "projects" / "Legacy"
        project.mkdir(parents=True)
        (project / "old.kmz").write_bytes(b"legacy")
        try:
            store = ProjectStore(root)
            files = store.get_project("Legacy").files

            self.assertEqual([file.name for file in files], ["old.kmz"])
            self.assertTrue((project / "kmz" / "old.kmz").exists())
            self.assertFalse((project / "old.kmz").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_add_files_replaces_matching_project_filename(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-replace-filename"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            store.create_project("Roof A")
            store.set_active_project("Roof A")

            source_dir = root / "incoming"
            source_dir.mkdir()
            source = source_dir / "mission.kmz"
            source.write_bytes(b"old-kmz")
            store.add_files([source])

            source.write_bytes(b"updated-kmz")
            imported = store.add_files([source])

            project = store.get_project("Roof A")
            self.assertEqual([file.name for file in project.files], ["mission.kmz"])
            self.assertEqual(imported[0].name, "mission.kmz")
            self.assertEqual((root / "projects" / "Roof A" / "kmz" / "mission.kmz").read_bytes(), b"updated-kmz")
            self.assertFalse((root / "projects" / "Roof A" / "kmz" / "mission-2.kmz").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_project_files_keep_manual_sync_order(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-file-order"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            store.create_project("Roof A")
            store.set_active_project("Roof A")

            source_dir = root / "incoming"
            source_dir.mkdir()
            first = source_dir / "first.kmz"
            second = source_dir / "second.kmz"
            third = source_dir / "third.kmz"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            third.write_bytes(b"third")
            store.add_files([first, second, third])

            self.assertTrue(store.move_file("Roof A", "third.kmz", -1))
            self.assertTrue(store.move_file("Roof A", "third.kmz", -1))

            self.assertEqual([file.name for file in store.get_project("Roof A").files], ["third.kmz", "first.kmz", "second.kmz"])
            self.assertEqual([path.name for path in store.active_files()], ["third.kmz", "first.kmz", "second.kmz"])

            third.write_bytes(b"third-updated")
            store.add_files([third])
            self.assertEqual([file.name for file in store.get_project("Roof A").files], ["third.kmz", "first.kmz", "second.kmz"])
            self.assertEqual((root / "projects" / "Roof A" / "kmz" / "third.kmz").read_bytes(), b"third-updated")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_sanitize_filename_requires_kmz(self) -> None:
        self.assertEqual(sanitize_filename("my mission.kmz"), "my mission.kmz")
        with self.assertRaisesRegex(ValueError, "KMZ"):
            sanitize_filename("mission.txt")

    def test_creator_website_defaults_to_path_domain(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-creator-settings"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            self.assertEqual(store.get_creator_settings()["website_url"], "https://path.scanair.ca")

            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            state["creator"] = {"website_url": "https://scanair.ca/"}
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")

            self.assertEqual(store.get_creator_settings()["website_url"], "https://path.scanair.ca")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_explicit_local_creator_pair_is_not_replaced_by_production(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-local-creator-settings"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            store.set_creator_settings(
                base_url="http://localhost:8000",
                website_url="http://localhost:5173",
                access_token="",
            )

            settings = store.get_creator_settings()

            self.assertEqual(settings["base_url"], "http://localhost:8000")
            self.assertEqual(settings["website_url"], "http://localhost:5173")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dpapi_round_trip(self) -> None:
        encrypted = protect_secret("sdi_test-token")

        self.assertTrue(encrypted.startswith("dpapi:v1:"))
        self.assertNotIn("sdi_test-token", encrypted)
        self.assertEqual(unprotect_secret(encrypted), "sdi_test-token")

    def test_migrates_plaintext_creator_token_to_dpapi(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-token-migration"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            state["creator"] = {
                "base_url": "https://api.scanair.ca",
                "access_token": "legacy-plaintext-token",
                "refresh_token": "legacy-refresh-token",
            }
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")

            settings = store.get_creator_settings()
            persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))["creator"]

            self.assertEqual(settings["access_token"], "legacy-plaintext-token")
            self.assertNotIn("access_token", persisted)
            self.assertNotIn("refresh_token", persisted)
            self.assertEqual(
                unprotect_secret(persisted["encrypted_access_token"]),
                "legacy-plaintext-token",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_corrupt_encrypted_token_fails_closed(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-corrupt-token"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            state["creator"] = {"encrypted_access_token": "dpapi:v1:not-base64!"}
            (root / "state.json").write_text(json.dumps(state), encoding="utf-8")

            settings = store.get_creator_settings()
            persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))["creator"]

            self.assertEqual(settings["access_token"], "")
            self.assertNotIn("encrypted_access_token", persisted)
            self.assertIn("corrupted", store.credential_error.lower())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dpapi_failure_never_persists_plaintext_token(self) -> None:
        root = Path.cwd() / ".test-tmp" / "store-dpapi-failure"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        try:
            store = ProjectStore(root)
            with patch(
                "scanair_dji_importer.store.protect_secret",
                side_effect=CredentialProtectionError("DPAPI unavailable"),
            ):
                store.set_creator_settings(
                    base_url="https://api.scanair.ca",
                    access_token="sdi_memory-only",
                )

            raw_state = (root / "state.json").read_text(encoding="utf-8")
            self.assertNotIn("sdi_memory-only", raw_state)
            self.assertEqual(store.get_creator_settings()["access_token"], "sdi_memory-only")
            self.assertIn("DPAPI unavailable", store.credential_error)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
