from __future__ import annotations

import json
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .store import ProjectStore, project_to_dict


class ImportServer:
    def __init__(self, store: ProjectStore, on_change: Callable[[str], None], host: str = "127.0.0.1", port: int = 8765):
        self.store = store
        self.on_change = on_change
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        handler = self._make_handler()
        last_error: OSError | None = None
        for port in range(self.port, self.port + 10):
            try:
                self._server = ThreadingHTTPServer((self.host, port), handler)
                self.port = port
                break
            except OSError as exc:
                last_error = exc
        if self._server is None:
            raise RuntimeError(f"Could not start local import server: {last_error}")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def _make_handler(self):
        store = self.store
        on_change = self.on_change

        class Handler(BaseHTTPRequestHandler):
            server_version = "ScanAirDJIImporter/0.1"

            def log_message(self, format: str, *args) -> None:
                return

            def end_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                super().end_headers()

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._json(200, {"ok": True, "active_project": store.get_active_project_name()})
                    return
                if self.path == "/projects":
                    self._json(200, {"projects": [project_to_dict(project) for project in store.list_projects()]})
                    return
                self._json(404, {"error": "Not found"})

            def do_POST(self) -> None:
                try:
                    if self.path == "/active-project":
                        data = self._read_json()
                        store.set_active_project(str(data.get("project", "")))
                        on_change("Active project changed by website.")
                        self._json(200, {"ok": True, "active_project": store.get_active_project_name()})
                        return
                    if self.path.startswith("/import"):
                        result = self._handle_import()
                        on_change(f"Imported {len(result['files'])} file(s) from website.")
                        self._json(200, result)
                        return
                    self._json(404, {"error": "Not found"})
                except Exception as exc:
                    self._json(400, {"error": str(exc)})

            def _handle_import(self) -> dict:
                content_type = self.headers.get("Content-Type", "")
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                project = first(query.get("project")) or store.get_active_project_name()
                replace = truthy(first(query.get("replace")))

                if "multipart/form-data" not in content_type:
                    filename = first(query.get("filename")) or "scanair-mission.kmz"
                    stored = store.add_file_bytes(filename, body, project_name=project)
                    return {"ok": True, "project": project, "files": [{"name": stored.name, "size": stored.size}]}

                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
                )
                parts = list(message.iter_parts())
                for part in parts:
                    if part.get_filename():
                        continue
                    field_name = part.get_param("name", header="content-disposition")
                    if field_name == "project":
                        project = part.get_content().strip()
                    elif field_name == "replace":
                        replace = truthy(part.get_content().strip())
                if not project:
                    raise ValueError("Create or select a project before importing KMZ files.")
                if replace:
                    try:
                        for stored_file in store.get_project(project).files:
                            store.delete_file(project, stored_file.name)
                    except ValueError:
                        pass
                files = []
                for part in parts:
                    filename = part.get_filename()
                    if not filename:
                        continue
                    payload = part.get_payload(decode=True) or b""
                    stored = store.add_file_bytes(filename, payload, project_name=project)
                    files.append({"name": stored.name, "size": stored.size})
                if not files:
                    raise ValueError("No KMZ files were included in the import request.")
                return {"ok": True, "project": project, "files": files}

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length).decode("utf-8")
                return json.loads(payload or "{}")

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def truthy(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}
