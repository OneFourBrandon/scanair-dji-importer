from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_CREATOR_API_URL = "https://api.scanair.ca"


class CreatorApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatorPath:
    project_id: str
    path_id: str
    name: str
    updated_at: str
    has_mission_area: bool
    has_saved_mission: bool


@dataclass(frozen=True)
class CreatorProject:
    project_id: str
    name: str
    updated_at: str
    paths: list[CreatorPath]


@dataclass(frozen=True)
class CreatorDownload:
    filename: str
    payload: bytes


@dataclass(frozen=True)
class WebsiteAuthStart:
    code: str
    expires_in: int


@dataclass(frozen=True)
class WebsiteAuthPoll:
    status: str
    access_token: str
    email: str
    expires_in: int


class CreatorClient:
    def __init__(self, base_url: str = DEFAULT_CREATOR_API_URL, access_token: str = "") -> None:
        self.base_url = base_url.rstrip("/") or DEFAULT_CREATOR_API_URL
        self.access_token = access_token.strip()

    def list_projects(self) -> list[CreatorProject]:
        rows = self._json("GET", "/projects")
        if not isinstance(rows, list):
            raise CreatorApiError("Creator backend returned an unexpected project list.")
        return [self._project_from_row(row) for row in rows if isinstance(row, dict)]

    def download_path_kmz(
        self,
        project_id: str,
        path_id: str,
        *,
        auto_record_video: bool = True,
        record_grid_passes: bool = False,
        ignore_waypoint_limit: bool = False,
    ) -> CreatorDownload:
        query = urlencode(
            {
                "auto_record_video": str(auto_record_video).lower(),
                "record_grid_passes": str(record_grid_passes).lower(),
                "ignore_waypoint_limit": str(ignore_waypoint_limit).lower(),
            }
        )
        path = f"/projects/{quote(project_id, safe='')}/paths/{quote(path_id, safe='')}/exports/kmz?{query}"
        payload, headers = self._bytes("POST", path)
        filename = filename_from_headers(headers) or "scanair-mission.kmz"
        if not filename.lower().endswith(".kmz"):
            filename = f"{filename}.kmz"
        return CreatorDownload(filename=filename, payload=payload)

    def _project_from_row(self, row: dict) -> CreatorProject:
        project_id = str(row.get("project_id") or row.get("projectId") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        paths = []
        for path in payload.get("paths") or []:
            if not isinstance(path, dict):
                continue
            path_id = str(path.get("id") or "")
            if not project_id or not path_id:
                continue
            settings = path.get("settings") if isinstance(path.get("settings"), dict) else {}
            mission_area = path.get("orbitArea") if settings.get("scanType") == "orbit" else path.get("polygon")
            mission = path.get("mission") if isinstance(path.get("mission"), dict) else {}
            paths.append(
                CreatorPath(
                    project_id=project_id,
                    path_id=path_id,
                    name=str(path.get("name") or path_id),
                    updated_at=str(path.get("updatedAt") or ""),
                    has_mission_area=mission_area is not None,
                    has_saved_mission=bool(mission.get("waypoints")),
                )
            )
        return CreatorProject(
            project_id=project_id,
            name=str(row.get("name") or payload.get("name") or project_id),
            updated_at=str(row.get("updated_at") or row.get("updatedAt") or ""),
            paths=paths,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _json(self, method: str, path: str) -> object:
        payload, _headers = self._request(method, path, expect_json=True)
        return json.loads(payload.decode("utf-8"))

    def _bytes(self, method: str, path: str) -> tuple[bytes, Message]:
        return self._request(method, path, expect_json=False)

    def _request(self, method: str, path: str, *, expect_json: bool) -> tuple[bytes, Message]:
        headers = self._headers()
        if expect_json:
            headers["Accept"] = "application/json"
        request = Request(f"{self.base_url}{path}", headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return response.read(), response.headers
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(detail or f"Creator backend returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc


class WebsiteAuthClient:
    def __init__(self, base_url: str = DEFAULT_CREATOR_API_URL) -> None:
        self.base_url = base_url.rstrip("/") or DEFAULT_CREATOR_API_URL

    def start(self) -> WebsiteAuthStart:
        data = self._json("POST", "/desktop-auth/sessions")
        return WebsiteAuthStart(code=str(data.get("code") or ""), expires_in=int(data.get("expires_in") or 0))

    def poll(self, code: str) -> WebsiteAuthPoll:
        data = self._json("GET", f"/desktop-auth/sessions/{quote(code, safe='')}")
        return WebsiteAuthPoll(
            status=str(data.get("status") or "pending"),
            access_token=str(data.get("access_token") or ""),
            email=str(data.get("email") or ""),
            expires_in=int(data.get("expires_in") or 0),
        )

    def _json(self, method: str, path: str) -> dict:
        request = Request(f"{self.base_url}{path}", headers={"Accept": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(detail or f"Website auth failed with HTTP {exc.code}.") from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc
        if not isinstance(body, dict):
            raise CreatorApiError("Creator backend returned an unexpected auth response.")
        return body


def filename_from_headers(headers: Message) -> str | None:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _error_detail(exc: HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if not body:
        return f"Creator backend returned HTTP {exc.code}."
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body
    detail = data.get("detail") if isinstance(data, dict) else None
    return str(detail) if detail else body
