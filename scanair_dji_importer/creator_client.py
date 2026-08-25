from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile


DEFAULT_CREATOR_API_URL = "https://api.scanair.ca"


class CreatorApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CreatorPath:
    project_id: str
    path_id: str
    name: str
    updated_at: str
    has_mission_area: bool
    has_saved_mission: bool
    export_part_count: int = 1


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
    token_type: str
    session_expires_in: int


class CreatorClient:
    def __init__(
        self,
        base_url: str = DEFAULT_CREATOR_API_URL,
        access_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/") or DEFAULT_CREATOR_API_URL
        self.access_token = access_token.strip()

    def list_projects(self) -> list[CreatorProject]:
        project_rows = self._json("GET", "/projects")
        if not isinstance(project_rows, list):
            raise CreatorApiError("Creator backend returned an unexpected project list.")

        projects: list[CreatorProject] = []
        for row in project_rows:
            if not isinstance(row, dict):
                continue
            project_id = str(row.get("project_id") or "")
            if not project_id:
                continue
            path_rows = self._json("GET", f"/projects/{quote(project_id, safe='')}/paths")
            if not isinstance(path_rows, list):
                raise CreatorApiError("Creator backend returned an unexpected path list.")
            paths = [self._path_from_row(project_id, path) for path in path_rows if isinstance(path, dict)]
            projects.append(
                CreatorProject(
                    project_id=project_id,
                    name=str(row.get("name") or project_id),
                    updated_at=str(row.get("updated_at") or ""),
                    paths=paths,
                )
            )
        return projects

    def download_path_kmz(
        self,
        project_id: str,
        path_id: str,
    ) -> CreatorDownload:
        return self.download_path_kmz_files(project_id, path_id)[0]

    def download_path_kmz_files(
        self,
        project_id: str,
        path_id: str,
    ) -> list[CreatorDownload]:
        query = urlencode(
            {
                "auto_record_video": "true",
                "record_grid_passes": "false",
                "take_photo_each_waypoint": "false",
                "split_waypoint_files": "true",
            }
        )
        path = (
            f"/projects/{quote(project_id, safe='')}/paths/{quote(path_id, safe='')}"
            f"/exports/kmz?{query}"
        )
        request = Request(f"{self.base_url}{path}", headers=self._auth_headers(), method="POST")
        try:
            with urlopen(request, timeout=120) as response:
                payload = response.read()
                filename = _download_filename(response.headers.get("Content-Disposition", ""))
                content_type = response.headers.get("Content-Type", "")
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(
                detail or f"Creator backend returned HTTP {exc.code}.",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc}") from exc
        if _is_zip_response(filename, content_type):
            return _downloads_from_zip(payload)
        filename = filename or "scanair-mission.kmz"
        if not filename.lower().endswith(".kmz"):
            filename = f"{filename}.kmz"
        return [CreatorDownload(filename=filename, payload=payload)]

    def _path_from_row(self, project_id: str, row: dict) -> CreatorPath:
        return CreatorPath(
            project_id=project_id,
            path_id=str(row.get("path_id") or ""),
            name=str(row.get("name") or row.get("path_id") or "Path"),
            updated_at=str(row.get("updated_at") or ""),
            has_mission_area=bool(row.get("has_mission_area")),
            has_saved_mission=bool(row.get("has_saved_mission")),
            export_part_count=_positive_int(row.get("export_part_count"), 1),
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_token:
            raise CreatorApiError("Authorize with the ScanAir website before loading cloud paths.")
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _json(self, method: str, path: str) -> object:
        request = Request(f"{self.base_url}{path}", headers=self._auth_headers(), method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(
                detail or f"Creator backend returned HTTP {exc.code}.",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CreatorApiError("Creator backend returned malformed JSON.") from exc

    def revoke_session(self) -> None:
        request = Request(
            f"{self.base_url}/desktop-auth/session",
            headers=self._auth_headers(),
            method="DELETE",
        )
        try:
            with urlopen(request, timeout=30):
                return
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(
                detail or f"Creator backend returned HTTP {exc.code}.",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc}") from exc


class WebsiteAuthClient:
    def __init__(self, base_url: str = DEFAULT_CREATOR_API_URL) -> None:
        self.base_url = base_url.rstrip("/") or DEFAULT_CREATOR_API_URL

    def start(self) -> WebsiteAuthStart:
        data = self._json("POST", "/desktop-auth/sessions")
        try:
            return WebsiteAuthStart(code=str(data.get("code") or ""), expires_in=int(data.get("expires_in") or 0))
        except (TypeError, ValueError) as exc:
            raise CreatorApiError("Creator backend returned an invalid authorization session.") from exc

    def poll(self, code: str) -> WebsiteAuthPoll:
        data = self._json("GET", f"/desktop-auth/sessions/{quote(code, safe='')}")
        try:
            return WebsiteAuthPoll(
                status=str(data.get("status") or "pending"),
                access_token=str(data.get("access_token") or ""),
                email=str(data.get("email") or ""),
                expires_in=int(data.get("expires_in") or 0),
                token_type=str(data.get("token_type") or "legacy"),
                session_expires_in=int(data.get("session_expires_in") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise CreatorApiError("Creator backend returned an invalid authorization response.") from exc

    def _json(self, method: str, path: str) -> dict:
        request = Request(f"{self.base_url}{path}", headers={"Accept": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = _error_detail(exc)
            raise CreatorApiError(
                detail or f"Website auth failed with HTTP {exc.code}.",
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise CreatorApiError(f"Could not reach Creator backend at {self.base_url}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CreatorApiError("Creator backend returned malformed JSON.") from exc
        if not isinstance(body, dict):
            raise CreatorApiError("Creator backend returned an unexpected auth response.")
        return body


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


def _download_filename(disposition: str) -> str:
    for part in disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip().strip('"')
    return ""


def _is_zip_response(filename: str, content_type: str) -> bool:
    return filename.lower().endswith(".zip") or "zip" in content_type.lower()


def _downloads_from_zip(payload: bytes) -> list[CreatorDownload]:
    try:
        with ZipFile(BytesIO(payload)) as archive:
            downloads = [
                CreatorDownload(filename=name.split("/")[-1], payload=archive.read(name))
                for name in archive.namelist()
                if not name.endswith("/") and name.lower().endswith(".kmz")
            ]
    except BadZipFile as exc:
        raise CreatorApiError("Creator backend returned a split export ZIP that could not be read.") from exc
    if not downloads:
        raise CreatorApiError("Creator backend returned a split export ZIP without KMZ files.")
    return downloads


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, parsed)
