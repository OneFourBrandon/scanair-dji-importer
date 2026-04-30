import json
import shutil
import unittest
import urllib.request
from pathlib import Path

from scanair_dji_importer.server import ImportServer
from scanair_dji_importer.store import ProjectStore


class ImportServerTests(unittest.TestCase):
    def test_multipart_import_uses_named_project(self) -> None:
        root = Path.cwd() / ".test-tmp" / "server-import"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        server = None
        try:
            store = ProjectStore(root)
            store.create_project("Default")
            store.set_active_project("Default")
            server = ImportServer(store, lambda _message: None, port=9876)
            server.start()

            boundary = "ScanAirBoundary"
            body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="project"\r\n\r\n'
                "Imported Project\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="files"; filename="mission.kmz"\r\n'
                "Content-Type: application/vnd.google-earth.kmz\r\n\r\n"
            ).encode("utf-8") + b"kmz-data" + f"\r\n--{boundary}--\r\n".encode("utf-8")
            request = urllib.request.Request(
                f"{server.url}/import",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            self.assertEqual(payload["project"], "Imported Project")
            self.assertTrue((root / "projects" / "Imported Project" / "kmz" / "mission.kmz").exists())
        finally:
            if server:
                server.stop()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
