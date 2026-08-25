import json
import threading
import unittest
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlparse
from zipfile import ZipFile

from scanair_dji_importer.creator_client import (
    CreatorApiError,
    CreatorClient,
    WebsiteAuthClient,
)


class CreatorClientTests(unittest.TestCase):
    def test_lists_projects_and_downloads_path_kmz(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CreatorClient(
                f"http://127.0.0.1:{server.server_port}",
                "token",
            )
            projects = client.list_projects()

            self.assertEqual(projects[0].name, "Roof A")
            self.assertEqual(projects[0].paths[0].name, "North Grid")
            self.assertTrue(projects[0].paths[0].has_saved_mission)
            self.assertEqual(projects[0].paths[0].export_part_count, 1)
            self.assertEqual(projects[0].paths[1].export_part_count, 2)

            package = client.download_path_kmz("project-1", "path-1")
            self.assertEqual(package.filename, "Roof-A-North-Grid.kmz")
            self.assertEqual(package.payload, b"kmz-data")
        finally:
            server.shutdown()
            server.server_close()

    def test_auth_error_keeps_status_code(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CreatorClient(
                f"http://127.0.0.1:{server.server_port}",
                "expired-token",
            )
            with self.assertRaises(CreatorApiError) as raised:
                client.list_projects()

            self.assertEqual(raised.exception.status_code, 401)
        finally:
            server.shutdown()
            server.server_close()

    def test_download_path_kmz_files_unpacks_split_zip(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CreatorClient(
                f"http://127.0.0.1:{server.server_port}",
                "token",
            )

            packages = client.download_path_kmz_files("project-1", "path-2")

            self.assertEqual([package.filename for package in packages], ["part-01.kmz", "part-02.kmz"])
            self.assertEqual([package.payload for package in packages], [b"kmz-one", b"kmz-two"])
        finally:
            server.shutdown()
            server.server_close()

    def test_revoke_desktop_session(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CreatorClient(f"http://127.0.0.1:{server.server_port}", "token")

            client.revoke_session()

            self.assertTrue(server.session_revoked)
        finally:
            server.shutdown()
            server.server_close()

    def test_malformed_json_becomes_creator_api_error(self) -> None:
        response = _FakeResponse(b"{not-json")
        with patch("scanair_dji_importer.creator_client.urlopen", return_value=response):
            with self.assertRaisesRegex(CreatorApiError, "malformed JSON"):
                CreatorClient("https://api.example", "token").list_projects()

    def test_timeout_becomes_creator_api_error(self) -> None:
        with patch(
            "scanair_dji_importer.creator_client.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaisesRegex(CreatorApiError, "Could not reach Creator backend"):
                WebsiteAuthClient("https://api.example").start()

    def test_invalid_auth_numbers_become_creator_api_error(self) -> None:
        response = _FakeResponse(b'{"code":"abc","expires_in":"not-a-number"}')
        with patch("scanair_dji_importer.creator_client.urlopen", return_value=response):
            with self.assertRaisesRegex(CreatorApiError, "invalid authorization session"):
                WebsiteAuthClient("https://api.example").start()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not self.assert_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/projects":
            payload = [
                {
                    "project_id": "project-1",
                    "name": "Roof A",
                    "updated_at": "2026-05-29T12:00:00Z",
                }
            ]
        elif parsed.path == "/projects/project-1/paths":
            payload = [
                {
                    "project_id": "project-1",
                    "path_id": "path-1",
                    "name": "North Grid",
                    "updated_at": "2026-05-29T12:00:00Z",
                    "has_mission_area": True,
                    "has_saved_mission": True,
                    "export_part_count": 1,
                },
                {
                    "project_id": "project-1",
                    "path_id": "path-2",
                    "name": "Long Grid",
                    "updated_at": "2026-05-29T12:05:00Z",
                    "has_mission_area": True,
                    "has_saved_mission": True,
                    "export_part_count": 2,
                }
            ]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_POST(self) -> None:
        if not self.assert_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/projects/project-1/paths/path-1/exports/kmz":
            if parsed.path != "/projects/project-1/paths/path-2/exports/kmz":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="Roof-A-Split.zip"')
            self.end_headers()
            self.wfile.write(_split_zip_payload())
            return
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.google-earth.kmz")
            self.send_header("Content-Disposition", 'attachment; filename="Roof-A-North-Grid.kmz"')
            self.end_headers()
            self.wfile.write(b"kmz-data")

    def do_DELETE(self) -> None:
        if not self.assert_auth():
            return
        if urlparse(self.path).path != "/desktop-auth/session":
            self.send_error(404)
            return
        self.server.session_revoked = True
        self.send_response(204)
        self.end_headers()

    def assert_auth(self) -> bool:
        if self.headers.get("Authorization") != "Bearer token":
            self.send_error(401)
            return False
        return True

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _split_zip_payload() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, mode="w") as archive:
        archive.writestr("part-01.kmz", b"kmz-one")
        archive.writestr("part-02.kmz", b"kmz-two")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


if __name__ == "__main__":
    unittest.main()
