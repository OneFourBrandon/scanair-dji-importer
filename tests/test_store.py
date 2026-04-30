import unittest
import shutil
from pathlib import Path

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

    def test_sanitize_filename_requires_kmz(self) -> None:
        self.assertEqual(sanitize_filename("my mission.kmz"), "my mission.kmz")
        with self.assertRaisesRegex(ValueError, "KMZ"):
            sanitize_filename("mission.txt")


if __name__ == "__main__":
    unittest.main()
