from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ScanAirDJIImporter"
PROJECTS_DIR = APP_DIR / "projects"
STATE_PATH = APP_DIR / "state.json"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class StoredFile:
    name: str
    size: int
    modified_at: str
    path: Path


@dataclass(frozen=True)
class Project:
    name: str
    active: bool
    file_count: int
    files: list[StoredFile]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_name(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("", name).strip(" .")
    if not cleaned:
        raise ValueError("Use a project name with at least one letter or number.")
    return cleaned[:80]


def sanitize_filename(name: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", Path(name).name).strip(" ._")
    if not cleaned.lower().endswith(".kmz"):
        raise ValueError(f"{name} is not a KMZ file.")
    return cleaned[:120]


class ProjectStore:
    def __init__(self, root: Path = APP_DIR) -> None:
        self.root = root
        self.projects_dir = root / "projects"
        self.state_path = root / "state.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write_state({"active_project": None, "created_at": utc_now()})

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"active_project": None, "created_at": utc_now()}

    def _write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def project_path(self, name: str) -> Path:
        return self.projects_dir / sanitize_name(name)

    def project_files_path(self, name: str) -> Path:
        path = self.project_path(name)
        files_path = path / "kmz"
        files_path.mkdir(parents=True, exist_ok=True)
        self._migrate_project_files(path, files_path)
        return files_path

    def _migrate_project_files(self, project_path: Path, files_path: Path) -> None:
        if not project_path.exists():
            return
        for old_file in project_path.glob("*.kmz"):
            target = unique_target(files_path / old_file.name)
            shutil.move(str(old_file), str(target))

    def ensure_default_project(self) -> None:
        if not self.list_project_names():
            self.create_project("Default")
            self.set_active_project("Default")

    def list_project_names(self) -> list[str]:
        return sorted(path.name for path in self.projects_dir.iterdir() if path.is_dir())

    def get_active_project_name(self) -> str | None:
        active = self._read_state().get("active_project")
        if active and self.project_path(active).exists():
            return active
        names = self.list_project_names()
        return names[0] if names else None

    def set_active_project(self, name: str) -> None:
        path = self.project_path(name)
        if not path.exists():
            raise ValueError(f"Project '{name}' does not exist.")
        state = self._read_state()
        state["active_project"] = path.name
        state["updated_at"] = utc_now()
        self._write_state(state)

    def create_project(self, name: str) -> Project:
        path = self.project_path(name)
        if path.exists():
            raise ValueError(f"Project '{path.name}' already exists.")
        path.mkdir(parents=True)
        (path / "kmz").mkdir()
        meta = {"name": path.name, "created_at": utc_now()}
        (path / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if self.get_active_project_name() is None:
            self.set_active_project(path.name)
        return self.get_project(path.name)

    def delete_project(self, name: str) -> None:
        path = self.project_path(name)
        if not path.exists():
            raise ValueError(f"Project '{name}' does not exist.")
        shutil.rmtree(path)
        if self.get_active_project_name() == path.name:
            names = self.list_project_names()
            state = self._read_state()
            state["active_project"] = names[0] if names else None
            state["updated_at"] = utc_now()
            self._write_state(state)

    def get_project(self, name: str) -> Project:
        path = self.project_path(name)
        if not path.exists():
            raise ValueError(f"Project '{name}' does not exist.")
        files = []
        for file_path in sorted(self.project_files_path(path.name).glob("*.kmz")):
            stat = file_path.stat()
            files.append(
                StoredFile(
                    name=file_path.name,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    path=file_path,
                )
            )
        return Project(
            name=path.name,
            active=path.name == self.get_active_project_name(),
            file_count=len(files),
            files=files,
        )

    def list_projects(self) -> list[Project]:
        return [self.get_project(name) for name in self.list_project_names()]

    def add_files(
        self,
        source_paths: Iterable[Path],
        project_name: str | None = None,
        replace: bool = False,
        replace_existing_name: bool = True,
    ) -> list[StoredFile]:
        target_name = project_name or self.get_active_project_name()
        if not target_name:
            raise ValueError("Create or select a project before importing KMZ files.")
        project_dir = self.project_path(target_name)
        if not project_dir.exists():
            self.create_project(target_name)
            project_dir = self.project_path(target_name)
        files_dir = self.project_files_path(project_dir.name)
        if replace:
            for old_file in files_dir.glob("*.kmz"):
                old_file.unlink()
        imported = []
        for source_path in source_paths:
            safe_name = sanitize_filename(source_path.name)
            target = files_dir / safe_name
            if not replace_existing_name:
                target = unique_target(target)
            if source_path.resolve() != target.resolve():
                shutil.copy2(source_path, target)
            imported.append(self.get_file(target_name, target.name))
        return imported

    def add_file_bytes(
        self,
        filename: str,
        payload: bytes,
        project_name: str | None = None,
        replace_existing_name: bool = True,
    ) -> StoredFile:
        if not payload:
            raise ValueError(f"{filename} is empty.")
        target_name = project_name or self.get_active_project_name()
        if not target_name:
            raise ValueError("Create or select a project before importing KMZ files.")
        project_dir = self.project_path(target_name)
        if not project_dir.exists():
            self.create_project(target_name)
            project_dir = self.project_path(target_name)
        files_dir = self.project_files_path(project_dir.name)
        safe_name = sanitize_filename(filename)
        target = files_dir / safe_name
        if not replace_existing_name:
            target = unique_target(target)
        target.write_bytes(payload)
        return self.get_file(target_name, target.name)

    def delete_file(self, project_name: str, filename: str) -> None:
        path = self.project_files_path(project_name) / sanitize_filename(filename)
        if not path.exists():
            raise ValueError(f"{filename} does not exist in {project_name}.")
        path.unlink()

    def get_file(self, project_name: str, filename: str) -> StoredFile:
        path = self.project_files_path(project_name) / sanitize_filename(filename)
        stat = path.stat()
        return StoredFile(
            name=path.name,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            path=path,
        )

    def active_files(self) -> list[Path]:
        active = self.get_active_project_name()
        if not active:
            return []
        return [stored.path for stored in self.get_project(active).files]


def unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for index in range(2, 1000):
        candidate = target.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"Could not create a unique filename for {target.name}.")


def project_to_dict(project: Project) -> dict:
    return {
        "name": project.name,
        "active": project.active,
        "file_count": project.file_count,
        "files": [
            {
                "name": file.name,
                "size": file.size,
                "modified_at": file.modified_at,
            }
            for file in project.files
        ],
    }
