import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scanair_dji_importer.creator_client import CreatorClient, filename_from_headers


class CreatorClientTests(unittest.TestCase):
    def test_lists_projects_and_downloads_path_kmz(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = CreatorClient(f"http://127.0.0.1:{server.server_port}", "token")
            projects = client.list_projects()

            self.assertEqual(projects[0].name, "Roof A")
            self.assertEqual(projects[0].paths[0].name, "North Grid")
            self.assertTrue(projects[0].paths[0].has_mission_area)

            package = client.download_path_kmz("project-1", "path-1")
            self.assertEqual(package.filename, "Roof-A-North-Grid.kmz")
            self.assertEqual(package.payload, b"kmz-data")
        finally:
            server.shutdown()
            server.server_close()

    def test_filename_from_headers_allows_unquoted_filename(self) -> None:
        from email.message import Message

        headers = Message()
        headers["Content-Disposition"] = "attachment; filename=scanair.kmz"
        self.assertEqual(filename_from_headers(headers), "scanair.kmz")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.assert_auth()
        if self.path != "/projects":
            self.send_error(404)
            return
        payload = [
            {
                "project_id": "project-1",
                "name": "Roof A",
                "updated_at": "2026-05-29T12:00:00Z",
                "payload": {
                    "paths": [
                        {
                            "id": "path-1",
                            "name": "North Grid",
                            "polygon": {"type": "Polygon", "coordinates": []},
                            "settings": {"scanType": "grid"},
                            "updatedAt": 123,
                        }
                    ]
                },
            }
        ]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_POST(self) -> None:
        self.assert_auth()
        if self.path != "/projects/project-1/paths/path-1/exports/kmz?auto_record_video=true&record_grid_passes=false&ignore_waypoint_limit=false":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.google-earth.kmz")
        self.send_header("Content-Disposition", 'attachment; filename="Roof-A-North-Grid.kmz"')
        self.end_headers()
        self.wfile.write(b"kmz-data")

    def assert_auth(self) -> None:
        if self.headers.get("Authorization") != "Bearer token":
            self.send_error(401)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    unittest.main()
