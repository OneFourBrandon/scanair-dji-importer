from __future__ import annotations

from pathlib import Path
from typing import Callable


class FileDropAdapter:
    def __init__(self, on_files: Callable[[list[Path]], None]) -> None:
        self.on_files = on_files
        self.enabled = False
        self.error: str | None = None

    def register(self, root, *widgets) -> bool:
        try:
            from tkinterdnd2 import DND_FILES
        except Exception as exc:
            self.error = str(exc)
            return False

        for widget in widgets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._handle_drop)
        self.enabled = True
        return True

    def unregister_all(self) -> None:
        return

    def _handle_drop(self, event) -> None:
        files = [Path(path) for path in event.widget.tk.splitlist(event.data)]
        if files:
            self.on_files(files)


def make_tk_root_class():
    try:
        from tkinterdnd2 import TkinterDnD
    except Exception:
        import tkinter as tk

        return tk.Tk
    return TkinterDnD.Tk
