import unittest
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scanair_dji_importer.app import first_reachable_url, format_timestamp, is_creator_auth_error
from scanair_dji_importer.creator_client import CreatorApiError


class AppFormattingTests(unittest.TestCase):
    def test_format_timestamp_handles_epoch_milliseconds(self) -> None:
        formatted = format_timestamp("1717000000000")

        self.assertRegex(formatted, r"^2024-05-29 \d{2}:\d{2} [AP]M$")

    def test_format_timestamp_handles_iso_strings(self) -> None:
        formatted = format_timestamp("2026-05-29T12:00:00Z")

        self.assertRegex(formatted, r"^2026-05-29 \d{2}:\d{2} [AP]M$")

    def test_first_reachable_url_finds_local_http_server(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            found = first_reachable_url(
                ("http://127.0.0.1:1", f"http://127.0.0.1:{server.server_port}"),
                "/health",
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(found, f"http://127.0.0.1:{server.server_port}")

    def test_creator_auth_error_detects_expired_status(self) -> None:
        self.assertTrue(is_creator_auth_error(CreatorApiError("expired", status_code=401)))
        self.assertFalse(is_creator_auth_error(CreatorApiError("temporary backend issue", status_code=502)))


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    unittest.main()
