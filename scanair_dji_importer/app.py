from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .creator_client import CreatorApiError, CreatorClient, CreatorPath, CreatorProject, WebsiteAuthClient
from .dji_mtp import (
    clear_controller_mapping,
    DjiControllerError,
    get_controller_slot_mapping,
    list_device_files,
    sync_files,
    update_controller_slot_mapping,
    verify_dummy_slots,
    verify_controller,
)
from .store import ProjectStore

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = ""
    TkRoot = tk.Tk
    DND_AVAILABLE = False
else:
    TkRoot = TkinterDnD.Tk
    DND_AVAILABLE = True

APP_ROOT = Path(__file__).resolve().parent
DRONEPATH_LOGO_PATH = APP_ROOT / "assets" / "dronepath-logo.png"
DRONEPATH_ICON_PATH = APP_ROOT / "assets" / "dronepath-logo.ico"
WINDOWS_APP_ID = "ScanAir.DJIImporter"
LOCAL_CREATOR_BACKEND_CANDIDATES = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
)
LOCAL_CREATOR_WEBSITE_CANDIDATES = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)


@dataclass(frozen=True)
class CreatorPathRow:
    path: CreatorPath
    part_index: int = 1
    part_count: int = 1


class ScanAirImporterApp(TkRoot):
    def __init__(self) -> None:
        set_windows_app_id()
        super().__init__()
        self.title("ScanAir DJI Importer")
        self.geometry("1040x660")
        self.minsize(900, 560)

        self.store = ProjectStore()
        self.messages: queue.Queue[str] = queue.Queue()
        creator_settings = self.store.get_creator_settings()

        self.project_var = tk.StringVar(value="No cloud project selected")
        initial_status = (
            f"Saved authorization could not be unlocked. Re-authorize with ScanAir. {self.store.credential_error}"
            if self.store.credential_error
            else "Authorize with ScanAir to load cloud paths."
        )
        self.status_var = tk.StringVar(value=initial_status)
        self.creator_login_var = tk.StringVar(
            value=f"Authorized as {creator_settings['email']}" if creator_settings["access_token"] else "Not authorized"
        )
        self.device_var = tk.StringVar(value="Controller not checked")
        self.slot_status_var = tk.StringVar(value="Dummy missions are not verified.")
        self.controller_identity_var = tk.StringVar(value="Controller identity not checked")
        self.dummy_verified = False
        self.controller_connected = False
        self.verification_in_progress = False
        self.connection_check_in_progress = False
        self.connection_check_after_id: str | None = None
        self.controller_operation_active = False
        self.sync_in_progress = False
        self.dummy_popup: tk.Toplevel | None = None
        self.dummy_reset_popup_open = False
        self.slot_manager_window: tk.Toplevel | None = None
        self.creator_window: tk.Toplevel | None = None
        self.creator_projects: list[CreatorProject] = []
        self.creator_path_rows: dict[str, CreatorPathRow] = {}
        self.creator_base_url_var: tk.StringVar | None = None
        self.creator_website_url_var: tk.StringVar | None = None
        self.creator_email_var: tk.StringVar | None = None
        self.creator_status_var: tk.StringVar | None = None
        self.creator_project_list: tk.Listbox | None = None
        self.creator_path_tree: ttk.Treeview | None = None
        self.toast_var = tk.StringVar(value="Checking controller...")
        self.status_bar: tk.Frame | None = None
        self.status_indicator: tk.Frame | None = None
        self.status_bar_label: tk.Label | None = None
        self.auth_expired_popup_open = False
        self.logo_image: tk.PhotoImage | None = None
        self.logo_header_image: tk.PhotoImage | None = None
        self.operational_widgets: list[tk.Widget] = []
        self.dev_mode_var = tk.BooleanVar(value=False)
        self.dev_drop_registered = False

        self._load_branding()
        self._build_menu()
        self._build_ui()
        self._register_dev_drop_targets()
        self.set_operational_enabled(False)
        self.show_controller_toast("Checking controller...", kind="checking")
        if creator_settings["access_token"]:
            self.refresh_creator_projects_async()
        self.after(500, self.verify_dummy_slots_async)
        self.schedule_connection_check()
        self.after(200, self._drain_messages)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_branding(self) -> None:
        if DRONEPATH_ICON_PATH.exists():
            try:
                self.iconbitmap(default=str(DRONEPATH_ICON_PATH))
            except tk.TclError:
                pass
        if not DRONEPATH_LOGO_PATH.exists():
            return
        try:
            self.logo_image = tk.PhotoImage(file=str(DRONEPATH_LOGO_PATH))
            self.logo_header_image = self.logo_image.subsample(12, 12)
            self.iconphoto(True, self.logo_image)
        except tk.TclError:
            self.logo_image = None
            self.logo_header_image = None

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)
        dev_menu = tk.Menu(menu_bar, tearoff=False)
        dev_menu.add_checkbutton(label="Dev Mode", variable=self.dev_mode_var, command=self.on_dev_mode_changed)
        dev_menu.add_command(label="Find Local Creator", command=self.detect_local_creator_async)
        menu_bar.add_cascade(label="Developer", menu=dev_menu)
        self.config(menu=menu_bar)

    def _build_ui(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        root = ttk.Frame(shell, padding=16)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="ScanAir DJI Importer", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        if self.logo_header_image is not None:
            ttk.Label(header, image=self.logo_header_image).grid(row=0, column=1, rowspan=2, sticky="ne")

        project_panel = ttk.LabelFrame(root, text="ScanAir Cloud", padding=12)
        project_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        project_panel.rowconfigure(0, weight=1)

        self.project_list = tk.Listbox(project_panel, width=30, exportselection=False)
        self.project_list.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        self.creator_project_list = self.project_list
        self.project_list.bind("<<ListboxSelect>>", lambda _event: self.on_cloud_project_selected())
        self.set_active_button = ttk.Button(project_panel, text="Refresh Projects", command=self.refresh_creator_projects_async)
        self.set_active_button.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.delete_project_button = ttk.Button(project_panel, text="Sign Out", command=self.sign_out_creator)
        self.delete_project_button.grid(row=2, column=0, sticky="ew")

        main_frame = ttk.Frame(root)
        main_frame.grid(row=1, column=1, sticky="nsew")
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)

        active_bar = ttk.LabelFrame(main_frame, text="ScanAir Account", padding=12)
        active_bar.grid(row=0, column=0, sticky="ew")
        active_bar.columnconfigure(1, weight=1)
        ttk.Label(active_bar, textvariable=self.creator_login_var, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        self.import_button = ttk.Button(active_bar, text="Authorize With Website", command=self.authorize_creator_with_website)
        self.import_button.grid(row=0, column=2, padx=(8, 0))
        self.creator_button = ttk.Button(active_bar, text="Refresh Projects", command=self.refresh_creator_projects_async)
        self.creator_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Label(active_bar, textvariable=self.project_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.drop_label = ttk.Label(
            active_bar,
            text="Save projects in ScanAir Creator, refresh here, then generate and sync KMZ files to the DJI RC 2.",
            anchor="center",
            relief=tk.RIDGE,
            padding=(12, 10),
        )
        self.drop_label.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        files_panel = ttk.LabelFrame(main_frame, text="ScanAir Cloud Paths (Sync Order)", padding=12)
        files_panel.grid(row=1, column=0, sticky="nsew", pady=12)
        files_panel.rowconfigure(0, weight=1)
        files_panel.columnconfigure(0, weight=1)
        columns = ("slot", "name", "status", "modified")
        self.files_tree = ttk.Treeview(files_panel, columns=columns, show="headings", selectmode="extended", height=10)
        self.creator_path_tree = self.files_tree
        self.files_tree.heading("slot", text="Slot")
        self.files_tree.heading("name", text="Path")
        self.files_tree.heading("status", text="Status")
        self.files_tree.heading("modified", text="Updated")
        self.files_tree.column("slot", width=60, anchor="center", stretch=False)
        self.files_tree.column("name", width=320, anchor="w")
        self.files_tree.column("status", width=140, anchor="w")
        self.files_tree.column("modified", width=220, anchor="w")
        self.files_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(files_panel, orient=tk.VERTICAL, command=self.files_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.files_tree.configure(yscrollcommand=scrollbar.set)
        file_buttons = ttk.Frame(files_panel)
        file_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.remove_files_button = ttk.Button(file_buttons, text="Sync Selected Paths", command=self.load_selected_creator_paths_to_controller)
        self.remove_files_button.pack(side=tk.LEFT)
        self.move_file_up_button = ttk.Button(file_buttons, text="Move Up", command=lambda: self.move_selected_file(-1))
        self.move_file_up_button.pack(side=tk.LEFT, padx=(8, 0))
        self.move_file_down_button = ttk.Button(file_buttons, text="Move Down", command=lambda: self.move_selected_file(1))
        self.move_file_down_button.pack(side=tk.LEFT, padx=(8, 0))
        self.open_project_button = ttk.Button(file_buttons, text="Refresh Paths", command=self.refresh_creator_projects_async)
        self.open_project_button.pack(side=tk.LEFT, padx=(8, 0))

        device_panel = ttk.LabelFrame(main_frame, text="DJI RC 2 Sync", padding=12)
        device_panel.grid(row=2, column=0, sticky="ew")
        device_panel.columnconfigure(0, weight=1)
        ttk.Label(device_panel, textvariable=self.device_var).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(device_panel, textvariable=self.controller_identity_var).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))
        self.check_controller_button = ttk.Button(device_panel, text="Check Controller", command=self.verify_dummy_slots_async)
        self.check_controller_button.grid(row=2, column=0, sticky="w")
        self.show_device_files_button = ttk.Button(device_panel, text="Show Device Files", command=self.show_device_files)
        self.show_device_files_button.grid(row=2, column=1, padx=8)
        self.manage_slots_button = ttk.Button(device_panel, text="Manage Dummy Slots", command=self.manage_dummy_slots)
        self.manage_slots_button.grid(row=2, column=2, padx=(0, 8))
        self.sync_button = ttk.Button(device_panel, text="Sync Selected Paths", command=self.sync_active_project)
        self.sync_button.grid(row=2, column=3)
        self.sync_progress = ttk.Progressbar(device_panel, mode="indeterminate")
        self.sync_progress.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.sync_progress.grid_remove()

        self.operational_widgets = [
            self.remove_files_button,
            self.show_device_files_button,
            self.manage_slots_button,
            self.sync_button,
        ]
        self._build_controller_status_bar(shell)

    def _build_controller_status_bar(self, shell: ttk.Frame) -> None:
        self.status_bar = tk.Frame(shell, bg="#f3f4f6", bd=0, highlightthickness=1, highlightbackground="#d1d5db")
        self.status_bar.grid(row=1, column=0, sticky="ew")
        self.status_bar.columnconfigure(1, weight=1)
        self.status_indicator = tk.Frame(self.status_bar, width=10, height=10, bg="#4b5563", bd=0, highlightthickness=0)
        self.status_indicator.grid(row=0, column=0, padx=(14, 8), pady=8)
        self.status_indicator.grid_propagate(False)
        self.status_bar_label = tk.Label(
            self.status_bar,
            textvariable=self.toast_var,
            bg="#f3f4f6",
            fg="#111827",
            padx=0,
            pady=5,
            justify=tk.LEFT,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.status_bar_label.grid(row=0, column=1, sticky="ew")

    def _register_dev_drop_targets(self) -> None:
        if self.dev_drop_registered or not DND_AVAILABLE:
            return
        for widget in (self, self.drop_label, self.files_tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self.on_dev_file_drop)
        self.dev_drop_registered = True

    def on_dev_mode_changed(self) -> None:
        if self.dev_mode_var.get():
            if DND_AVAILABLE:
                self.drop_label.configure(text="Dev mode: searching for local Creator. Drop one KMZ file here to sync directly to slot 1.")
                self.set_status("Dev mode enabled. Searching for a local ScanAir Creator instance...")
            else:
                self.drop_label.configure(text="Dev mode needs tkinterdnd2. Install dependencies or use the EXE build.")
                self.set_status("Dev mode enabled, but drag-and-drop support is not available.")
            self.detect_local_creator_async()
        else:
            self.drop_label.configure(text="Save projects in ScanAir Creator, refresh here, then generate and sync KMZ files to the DJI RC 2.")
            self.set_status("Dev mode disabled.")

    def detect_local_creator_async(self) -> None:
        self.set_status("Searching localhost for ScanAir Creator...")

        def runner() -> None:
            backend_url = first_reachable_url(LOCAL_CREATOR_BACKEND_CANDIDATES, "/health")
            website_url = first_reachable_url(LOCAL_CREATOR_WEBSITE_CANDIDATES, "/")
            self.after(0, partial(self.apply_local_creator_detection, backend_url, website_url))

        threading.Thread(target=runner, daemon=True).start()

    def apply_local_creator_detection(self, backend_url: str, website_url: str) -> None:
        if not backend_url and not website_url:
            self.set_status("No local ScanAir Creator instance found. Start backend on :8000 and frontend on :5173.")
            if self.dev_mode_var.get() and DND_AVAILABLE:
                self.drop_label.configure(text="Dev mode: no local Creator found. Drop one KMZ file here to sync directly to slot 1.")
            return
        settings = self.store.get_creator_settings()
        base_url = backend_url or settings["base_url"]
        creator_site = website_url or settings["website_url"]
        self.store.set_creator_settings(
            base_url=base_url,
            website_url=creator_site,
            access_token=settings["access_token"],
            email=settings["email"],
            refresh_token=settings["refresh_token"],
        )
        if self.creator_base_url_var is not None:
            self.creator_base_url_var.set(base_url)
        if self.creator_website_url_var is not None:
            self.creator_website_url_var.set(creator_site)
        if backend_url and website_url:
            message = f"Dev mode using local Creator: backend {backend_url}, website {website_url}."
        elif backend_url:
            message = f"Dev mode found local Creator backend at {backend_url}. Website URL left as {creator_site}."
        else:
            message = f"Dev mode found local Creator website at {website_url}. Backend URL left as {base_url}."
        self.set_status(message)
        if self.dev_mode_var.get() and DND_AVAILABLE:
            self.drop_label.configure(text=f"Dev mode: {message} Drop one KMZ file here to sync directly to slot 1.")

    def on_dev_file_drop(self, event) -> None:
        if not self.dev_mode_var.get():
            return
        paths = [Path(path) for path in self.tk.splitlist(event.data)]
        self.after(0, partial(self.sync_dev_dropped_file, paths))

    def sync_dev_dropped_file(self, paths: list[Path]) -> None:
        files = [path for path in paths if path.is_file()]
        if len(files) != 1 or files[0].suffix.lower() != ".kmz":
            messagebox.showwarning("Dev Mode Sync", "Drop exactly one KMZ file.", parent=self)
            return
        self.start_sync_progress()
        self.run_background("Dev mode syncing dropped KMZ to DJI RC 2...", lambda: self._sync_files([files[0]]))

    def set_operational_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in self.operational_widgets:
            widget["state"] = state

    def show_dummy_setup_popup(self) -> None:
        self.show_controller_toast("DJI RC 2 setup needed. Connect the controller, unlock it, and verify dummy slots.", kind="error")

    def show_controller_toast(self, message: str, *, kind: str) -> None:
        colors = {
            "ok": "#047857",
            "checking": "#4b5563",
            "error": "#b45309",
            "offline": "#991b1b",
        }
        color = colors.get(kind, "#4b5563")
        self.toast_var.set(message)
        if self.status_indicator is not None:
            self.status_indicator.configure(bg=color)

    def verify_dummy_slots_async(self) -> None:
        if self.verification_in_progress:
            return
        self.verification_in_progress = True
        self.set_status("Checking DJI RC 2 dummy missions...")

        def runner() -> None:
            try:
                path = verify_controller()
                verification = verify_dummy_slots()
            except DjiControllerError as exc:
                self.apply_dummy_verification_async(False, str(exc), "Controller identity not checked", "", False)
                return
            message = f"{path}\n\n{verification.message}"
            self.apply_dummy_verification_async(
                verification.ok,
                message,
                verification.controller_label,
                verification.controller_key,
                verification.requires_mapping_reset,
            )

        threading.Thread(target=runner, daemon=True).start()

    def apply_dummy_verification_async(
        self,
        ok: bool,
        message: str,
        controller_label: str,
        controller_key: str,
        requires_mapping_reset: bool,
    ) -> None:
        def apply() -> None:
            self.apply_dummy_verification(ok, message, controller_label, controller_key, requires_mapping_reset)

        self.after(0, apply)

    def apply_dummy_verification(
        self,
        ok: bool,
        message: str,
        controller_label: str,
        controller_key: str,
        requires_mapping_reset: bool,
    ) -> None:
        self.verification_in_progress = False
        self.controller_connected = ok
        self.dummy_verified = ok
        display_message = friendly_waiting_message(message) if not ok and is_controller_disconnect_message(message) else message
        self.slot_status_var.set(display_message)
        self.controller_identity_var.set(f"Controller identity: {controller_label}")
        self.set_operational_enabled(ok)
        if ok:
            self.device_var.set("Dummy slots verified for connected controller")
            self.set_status("Ready. Dummy slot IDs verified.")
            self.show_controller_toast("DJI RC 2 connected. Dummy slots verified.", kind="ok")
        else:
            self.device_var.set("Dummy slots not verified")
            if "controller was not found" in message.lower():
                self.set_status("DJI RC 2 disconnected. Plug it back in, unlock it, and choose file transfer.")
                self.show_controller_toast("DJI RC 2 disconnected.", kind="offline")
            else:
                self.set_status("Setup required: create/duplicate 10 identical dummy missions on the RC 2.")
                self.show_controller_toast("DJI RC 2 connected, but dummy slots need setup.", kind="error")
                if requires_mapping_reset and controller_key:
                    self.show_dummy_slot_reset_prompt(controller_key)
        self.schedule_connection_check()

    def show_dummy_slot_reset_prompt(self, controller_key: str) -> None:
        if self.dummy_reset_popup_open:
            return
        self.dummy_reset_popup_open = True
        cleared = clear_controller_mapping(controller_key)
        reset_message = (
            "One or more saved dummy slot files for this controller were deleted.\n\n"
            "I cleared this controller's saved dummy slot memory in the importer.\n\n"
            "On the DJI RC 2, delete any remaining old dummy missions for this setup. Then create one dummy waypoint mission and use Save As / duplicate until there are 10 identical copies.\n\n"
            "After that, click Check Controller so the importer can save the new slot IDs."
        )
        if not cleared:
            reset_message = (
                "One or more saved dummy slot files for this controller were deleted.\n\n"
                "The importer did not find an existing saved mapping to clear, so the next Check Controller will look for a fresh set of 10 identical dummy missions.\n\n"
                "On the DJI RC 2, delete any remaining old dummy missions for this setup. Then create one dummy waypoint mission and use Save As / duplicate until there are 10 identical copies."
            )
        try:
            messagebox.showwarning("Dummy Slot Setup Reset", reset_message, parent=self)
        finally:
            self.dummy_reset_popup_open = False

    def schedule_connection_check(self, delay_ms: int = 5000) -> None:
        if self.connection_check_after_id is not None:
            self.after_cancel(self.connection_check_after_id)
        self.connection_check_after_id = self.after(delay_ms, self.check_controller_connection_async)

    def check_controller_connection_async(self) -> None:
        self.connection_check_after_id = None
        if self.connection_check_in_progress or self.verification_in_progress or self.controller_operation_active:
            self.schedule_connection_check()
            return
        self.connection_check_in_progress = True

        def runner() -> None:
            try:
                path = verify_controller()
            except DjiControllerError as exc:
                self.apply_controller_connection_async(False, str(exc))
                return
            self.apply_controller_connection_async(True, path)

        threading.Thread(target=runner, daemon=True).start()

    def apply_controller_connection_async(self, connected: bool, message: str) -> None:
        def apply() -> None:
            self.apply_controller_connection(connected, message)

        self.after(0, apply)

    def apply_controller_connection(self, connected: bool, message: str) -> None:
        self.connection_check_in_progress = False
        if connected:
            if not self.controller_connected:
                self.controller_connected = True
                self.device_var.set("DJI RC 2 reconnected")
                self.set_status("DJI RC 2 reconnected. Verifying dummy slots...")
                self.show_controller_toast("DJI RC 2 reconnected. Verifying dummy slots...", kind="checking")
                self.verify_dummy_slots_async()
                return
            self.schedule_connection_check()
            return

        if self.controller_connected or self.dummy_verified:
            self.mark_controller_unavailable(message)
        self.schedule_connection_check()

    def mark_controller_unavailable(self, message: str) -> None:
        self.controller_connected = False
        self.dummy_verified = False
        self.device_var.set("DJI RC 2 disconnected")
        self.controller_identity_var.set("Controller identity not checked")
        self.slot_status_var.set(friendly_waiting_message(message))
        self.set_operational_enabled(False)
        self.set_status("DJI RC 2 disconnected. Plug it back in, unlock it, and choose file transfer.")
        self.show_controller_toast("DJI RC 2 disconnected.", kind="offline")

    def bring_main_window_to_front(self) -> None:
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(800, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def require_dummy_slots(self) -> bool:
        if self.dummy_verified:
            return True
        self.show_controller_toast("Connect and verify the DJI RC 2 before syncing.", kind="error")
        self.set_status("Connect and verify the DJI RC 2 before using the importer.")
        return False

    def require_creator_auth(self) -> bool:
        if self.store.get_creator_settings()["access_token"]:
            return True
        self.set_creator_status("Authorize with the ScanAir website before using the importer.")
        messagebox.showwarning(
            "ScanAir Authorization",
            "Authorize with the ScanAir website before loading or syncing paths.",
            parent=self.creator_window or self,
        )
        return False

    def refresh_projects(self) -> None:
        self.refresh_creator_projects_async()

    def refresh_files(self) -> None:
        self.refresh_creator_projects_async()

    def select_project_from_list(self) -> None:
        project = self.selected_creator_project()
        if project:
            self.project_var.set(project.name)

    def set_selected_active(self) -> None:
        self.refresh_creator_projects_async()

    def create_project(self) -> None:
        self.authorize_creator_with_website()

    def delete_project(self) -> None:
        self.sign_out_creator()

    def import_files(self) -> None:
        self.authorize_creator_with_website()

    def remove_selected_files(self) -> None:
        self.load_selected_creator_paths_to_controller()

    def move_selected_file(self, delta: int) -> None:
        selected = self.files_tree.selection()
        if not selected:
            return
        item = selected[0]
        siblings = list(self.files_tree.get_children(""))
        index = siblings.index(item)
        new_index = index + delta
        if new_index < 0 or new_index >= len(siblings):
            return
        self.files_tree.move(item, "", new_index)
        self._renumber_path_rows()
        self.files_tree.selection_set(item)
        self.files_tree.focus(item)
        self.files_tree.see(item)
        direction = "up" if delta < 0 else "down"
        self.set_status(f"Moved selected cloud path {direction}. It will sync to the displayed slot number.")

    def _renumber_path_rows(self) -> None:
        if not self.creator_path_tree:
            return
        for index, item in enumerate(self.creator_path_tree.get_children(""), start=1):
            values = list(self.creator_path_tree.item(item, "values"))
            if values:
                values[0] = index
                self.creator_path_tree.item(item, values=values)

    def open_active_project_folder(self) -> None:
        self.refresh_creator_projects_async()

    def creator_client_from_window(self) -> CreatorClient:
        settings = self.store.get_creator_settings()
        base_url = self.creator_base_url_var.get() if self.creator_base_url_var else settings["base_url"]
        access_token = settings["access_token"]
        website_url = self.creator_website_url_var.get() if self.creator_website_url_var else settings["website_url"]
        self.store.set_creator_settings(
            base_url=base_url,
            website_url=website_url,
            access_token=access_token,
            email=settings["email"],
            refresh_token=settings["refresh_token"],
        )
        return CreatorClient(base_url, access_token)

    def authorize_creator_with_website(self) -> None:
        settings = self.store.get_creator_settings()
        base_url = self.creator_base_url_var.get() if self.creator_base_url_var else settings["base_url"]
        website_url = self.creator_website_url_var.get() if self.creator_website_url_var else settings["website_url"]
        self.store.set_creator_settings(
            base_url=base_url,
            website_url=website_url,
            access_token="",
        )
        self.set_creator_status("Starting website authorization...")

        def runner() -> None:
            try:
                auth_client = WebsiteAuthClient(base_url)
                session = auth_client.start()
                if not session.code:
                    raise CreatorApiError("Creator backend did not return an authorization code.")
                auth_url = f"{website_url.rstrip('/')}/?desktop_auth_code={session.code}"
                webbrowser.open(auth_url)
                self.after(0, lambda: self.set_creator_status("Authorize the importer in the browser window that just opened."))
                deadline = time.monotonic() + max(session.expires_in, 1)
                while time.monotonic() < deadline:
                    time.sleep(2)
                    polled = auth_client.poll(session.code)
                    if polled.status == "authorized" and polled.access_token:
                        self.store.set_creator_settings(
                            base_url=base_url,
                            website_url=website_url,
                            access_token=polled.access_token,
                            email=polled.email,
                        )
                        self.after(0, partial(self.apply_creator_authorized, polled.email))
                        return
                raise CreatorApiError("Website authorization expired. Try again.")
            except CreatorApiError as exc:
                error_message = str(exc)
                self.after(0, partial(self.set_creator_status, error_message))
            except Exception as exc:
                self.after(0, partial(self.set_creator_status, f"Website authorization failed: {exc}"))

        threading.Thread(target=runner, daemon=True).start()

    def apply_creator_authorized(self, email: str) -> None:
        label = f"Authorized as {email}" if email else "Authorized"
        if self.creator_login_var is not None:
            self.creator_login_var.set(label)
        self.set_creator_status("Website authorization complete. Loading cloud projects...")
        self.refresh_creator_projects_async()

    def sign_out_creator(self) -> None:
        settings = self.store.get_creator_settings()
        token = settings["access_token"]
        base_url = settings["base_url"]
        self.store.clear_creator_session()
        if self.creator_login_var is not None:
            self.creator_login_var.set("Not authorized")
        if self.creator_project_list is not None:
            self.creator_project_list.delete(0, tk.END)
        if self.creator_path_tree is not None:
            self.creator_path_tree.delete(*self.creator_path_tree.get_children())
        self.creator_projects = []
        self.creator_path_rows = {}
        self.project_var.set("No cloud project selected")
        self.set_creator_status("Signed out of Creator.")
        if token.startswith("sdi_"):
            def revoke_runner() -> None:
                try:
                    CreatorClient(base_url, token).revoke_session()
                except Exception as exc:
                    self.after(
                        0,
                        partial(
                            self.set_creator_status,
                            f"Signed out locally. The server session could not be revoked: {exc}",
                        ),
                    )

            threading.Thread(target=revoke_runner, daemon=True).start()

    def refresh_creator_projects_async(self) -> None:
        if not self.store.get_creator_settings()["access_token"]:
            self.set_creator_status("Authorize with the ScanAir website before loading projects.")
            return
        self.set_creator_status("Loading cloud projects...")
        client = self.creator_client_from_window()
        selected_project = self.selected_creator_project()
        selected_project_id = selected_project.project_id if selected_project else ""

        def runner() -> None:
            try:
                loaded_projects = client.list_projects()
            except CreatorApiError as exc:
                error_message = str(exc)
                if is_creator_auth_error(exc):
                    self.after(0, partial(self.show_auth_expired_popup, error_message))
                else:
                    self.after(0, partial(self.set_creator_status, error_message))
                return
            except Exception as exc:
                self.after(0, partial(self.set_creator_status, f"Could not load cloud projects: {exc}"))
                return
            self.after(0, partial(self.apply_creator_projects, loaded_projects, selected_project_id))

        threading.Thread(target=runner, daemon=True).start()

    def apply_creator_projects(self, projects: list[CreatorProject], selected_project_id: str = "") -> None:
        self.creator_projects = projects
        if not self.creator_project_list:
            return
        self.creator_project_list.delete(0, tk.END)
        for project in projects:
            self.creator_project_list.insert(tk.END, f"{project.name} ({len(project.paths)} path{'s' if len(project.paths) != 1 else ''})")
        if projects:
            selected_index = next(
                (index for index, project in enumerate(projects) if project.project_id == selected_project_id),
                0,
            )
            self.creator_project_list.selection_clear(0, tk.END)
            self.creator_project_list.selection_set(selected_index)
            self.creator_project_list.activate(selected_index)
            self.creator_project_list.see(selected_index)
            self.project_var.set(projects[selected_index].name)
        else:
            self.project_var.set("No cloud project selected")
        self.refresh_creator_path_list()
        self.set_creator_status(f"Loaded {len(projects)} cloud project(s).")

    def selected_creator_project(self) -> CreatorProject | None:
        if not self.creator_project_list:
            return None
        selection = self.creator_project_list.curselection()
        if not selection:
            return None
        index = selection[0]
        return self.creator_projects[index] if index < len(self.creator_projects) else None

    def on_cloud_project_selected(self) -> None:
        project = self.selected_creator_project()
        self.project_var.set(project.name if project else "No cloud project selected")
        self.refresh_creator_path_list()

    def refresh_creator_path_list(self) -> None:
        if not self.creator_path_tree:
            return
        self.creator_path_tree.delete(*self.creator_path_tree.get_children())
        self.creator_path_rows = {}
        project = self.selected_creator_project()
        if not project:
            return
        self.project_var.set(project.name)
        for index, path in enumerate(project.paths, start=1):
            if path.has_mission_area:
                status_text = "Ready"
            else:
                status_text = "No mission area"
            part_count = max(1, path.export_part_count)
            for part_index in range(1, part_count + 1):
                row_id = f"{path.path_id}::part-{part_index}"
                row = CreatorPathRow(path=path, part_index=part_index, part_count=part_count)
                display_name = path.name if part_count == 1 else f"{path.name} (part {part_index}/{part_count})"
                display_status = status_text if part_count == 1 else f"{status_text} · split KMZ"
                self.creator_path_rows[row_id] = row
                self.creator_path_tree.insert(
                    "",
                    tk.END,
                    iid=row_id,
                    values=(
                        len(self.creator_path_rows),
                        display_name,
                        display_status,
                        format_timestamp(path.updated_at),
                    ),
                )

    def selected_creator_path_rows(self) -> list[CreatorPathRow]:
        if not self.creator_path_tree:
            return []
        selected = set(self.creator_path_tree.selection())
        if not selected:
            return [
                row
                for item in self.creator_path_tree.get_children("")
                if (row := self.creator_path_rows.get(item)) is not None
            ]
        selected_path_keys = {
            (row.path.project_id, row.path.path_id)
            for item, row in self.creator_path_rows.items()
            if item in selected
        }
        return [
            row
            for item in self.creator_path_tree.get_children("")
            if (row := self.creator_path_rows.get(item)) is not None
            and (row.path.project_id, row.path.path_id) in selected_path_keys
        ]

    def load_selected_creator_paths_to_controller(self) -> None:
        if not self.require_creator_auth():
            return
        if not self.require_dummy_slots():
            return
        rows = self.selected_creator_path_rows()
        if not rows:
            messagebox.showwarning("Creator Paths", "No cloud paths are available to load.", parent=self.creator_window or self)
            return
        prompt = (
            f"Import and sync {len(rows)} ScanAir KMZ file(s) to the DJI RC 2?\n\n"
            "The Creator backend will prepare the latest KMZ for each selected path."
        )
        if not messagebox.askyesno("Load Creator Paths", prompt, parent=self.creator_window or self):
            return
        client = self.creator_client_from_window()
        self.start_sync_progress()
        self.run_background("Importing ScanAir KMZ files and syncing to DJI RC 2...", partial(self._download_creator_path_rows, rows, client, sync_after=True))

    def _download_creator_path_rows(self, rows: list[CreatorPathRow], client: CreatorClient, *, sync_after: bool) -> None:
        self.store.clear_creator_cache()
        downloaded = []
        packages_by_path: dict[tuple[str, str], list] = {}
        rows_by_path: dict[tuple[str, str], list[CreatorPathRow]] = {}
        for row in rows:
            rows_by_path.setdefault((row.path.project_id, row.path.path_id), []).append(row)
        for path_key, path_rows in rows_by_path.items():
            path = path_rows[0].path
            if not path.has_mission_area:
                raise ValueError(f"{path.name} has no mission area to import.")
            if path_key not in packages_by_path:
                packages_by_path[path_key] = client.download_path_kmz_files(path.project_id, path.path_id)
            packages = packages_by_path[path_key]
            rows_to_write = path_rows
            if len(path_rows) == 1 and path_rows[0].part_count == 1 and len(packages) > 1:
                rows_to_write = [
                    CreatorPathRow(path=path, part_index=part_index, part_count=len(packages))
                    for part_index in range(1, len(packages) + 1)
                ]
            for row in rows_to_write:
                if row.part_index > len(packages):
                    raise ValueError(
                        f"{path.name} expected part {row.part_index} of {row.part_count}, "
                        f"but Creator returned {len(packages)} KMZ file(s)."
                    )
                package = packages[row.part_index - 1]
                downloaded.append(self.store.write_creator_cache_file(package.filename, package.payload))
        if sync_after:
            result = sync_files(downloaded)
            skipped = f"; skipped {len(result.skipped)} existing folder(s)" if result.skipped else ""
            message = f"Imported {len(downloaded)} KMZ file(s). Sync complete. Updated {len(result.copied)} dummy slot(s){skipped}."
        else:
            message = f"Imported {len(downloaded)} KMZ file(s) into the importer cache."
        self.enqueue_status(message)
        self.show_info_async("Creator Paths", message)

    def set_creator_status(self, message: str) -> None:
        if self.creator_status_var is not None:
            self.creator_status_var.set(message)
        self.set_status(message)

    def show_auth_expired_popup(self, message: str) -> None:
        if self.auth_expired_popup_open:
            self.set_creator_status("ScanAir session expired. Re-authenticate to continue.")
            return
        self.auth_expired_popup_open = True
        self.store.clear_creator_session()
        self.creator_login_var.set("Not authorized")
        self.set_creator_status("ScanAir session expired. Re-authenticate to continue.")
        self.bring_main_window_to_front()
        prompt = (
            "Your ScanAir session has expired or is no longer valid.\n\n"
            "Re-authenticate with the ScanAir website now?"
        )
        if message:
            prompt += f"\n\nDetails: {message}"
        try:
            should_reauth = messagebox.askyesno(
                "ScanAir Session Expired",
                prompt,
                parent=self.creator_window or self,
            )
        finally:
            self.auth_expired_popup_open = False
        if should_reauth:
            self.authorize_creator_with_website()

    def check_controller(self) -> None:
        self.verify_dummy_slots_async()

    def auto_check_controller(self) -> None:
        def runner() -> None:
            try:
                path = verify_controller()
            except DjiControllerError as exc:
                message = str(exc)
                self.mark_controller_unavailable_async(message)
                self.enqueue_status(message)
                return
            self.after(0, lambda: self.device_var.set(f"Found: {path}"))
            self.enqueue_status("DJI RC 2 controller found.")

        threading.Thread(target=runner, daemon=True).start()

    def _check_controller(self) -> None:
        path = verify_controller()
        self.after(0, lambda: self.device_var.set(f"Found: {path}"))
        self.enqueue_status("DJI RC 2 controller found.")

    def show_device_files(self) -> None:
        self.run_background("Reading controller waypoint folder...", self._show_device_files)

    def _show_device_files(self) -> None:
        files = list_device_files()
        if not files:
            text = "No KMZ files found in the controller waypoint folder."
        else:
            rows = []
            for file in files:
                package = f"{file.package_name}\\" if file.package_name else ""
                image_status = "" if file.has_image_folder else " (missing image folder)"
                calibration_status = " (calibration, preserved)" if file.is_calibration else ""
                mission = f" [{file.mission_name}]" if file.mission_name else ""
                match = "" if file.package_name_matches_kmz else " (name mismatch)"
                rows.append(f"{package}{file.name}{mission}{image_status}{match}{calibration_status}")
            text = "\n".join(rows)
        self.after(0, lambda: messagebox.showinfo("Controller KMZ Files", text, parent=self))
        self.enqueue_status(f"Read {len(files)} controller KMZ file(s).")

    def manage_dummy_slots(self) -> None:
        if not self.require_dummy_slots():
            return
        self.run_background("Loading saved dummy slots...", self._open_dummy_slot_manager)

    def _open_dummy_slot_manager(self) -> None:
        identity, slots = get_controller_slot_mapping()
        if len(slots) != 10:
            raise DjiControllerError("No complete dummy slot mapping is stored for this controller yet.")
        self.show_dummy_slot_manager_async(slots, identity.label)
        self.enqueue_status("Loaded saved dummy slots.")

    def show_dummy_slot_manager_async(self, slots: list[dict], controller_label: str) -> None:
        def show_manager() -> None:
            self.show_dummy_slot_manager(slots, controller_label)

        self.after(0, show_manager)

    def show_dummy_slot_manager(self, slots: list[dict], controller_label: str) -> None:
        if self.slot_manager_window and self.slot_manager_window.winfo_exists():
            self.slot_manager_window.destroy()
        window = tk.Toplevel(self)
        window.title("Dummy Slot Manager")
        window.geometry("1100x460")
        window.minsize(980, 380)
        self.slot_manager_window = window

        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=controller_label, wraplength=1040).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        columns = ("slot", "local_name", "package", "kmz", "created", "updated")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("slot", text="Sync #")
        tree.heading("local_name", text="Local Name")
        tree.heading("package", text="Controller Folder ID")
        tree.heading("kmz", text="KMZ ID")
        tree.heading("created", text="Created")
        tree.heading("updated", text="Updated")
        tree.column("slot", width=70, anchor="center", stretch=False)
        tree.column("local_name", width=180, anchor="w")
        tree.column("package", width=210, anchor="w")
        tree.column("kmz", width=210, anchor="w")
        tree.column("created", width=170, anchor="w")
        tree.column("updated", width=170, anchor="w")
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)

        slot_rows = [dict(slot) for slot in slots]

        def refresh_tree(select_index: int | None = None) -> None:
            tree.delete(*tree.get_children())
            for index, slot in enumerate(slot_rows, start=1):
                item_id = str(index - 1)
                tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(
                        index,
                        slot.get("local_name") or "",
                        slot.get("package_name") or "",
                        slot.get("kmz_name") or "",
                        format_timestamp(str(slot.get("create_time_ms") or "")),
                        format_timestamp(str(slot.get("modified_at") or "")),
                    ),
                )
            if select_index is not None and 0 <= select_index < len(slot_rows):
                tree.selection_set(str(select_index))
                tree.focus(str(select_index))

        def selected_index() -> int | None:
            selection = tree.selection()
            return int(selection[0]) if selection else None

        def rename_selected() -> None:
            index = selected_index()
            if index is None:
                return
            current = slot_rows[index].get("local_name") or ""
            name = simpledialog.askstring("Local Slot Name", "Name for this dummy slot:", initialvalue=current, parent=window)
            if name is None:
                return
            slot_rows[index]["local_name"] = name.strip()
            refresh_tree(index)

        def move_selected(delta: int) -> None:
            index = selected_index()
            if index is None:
                return
            new_index = index + delta
            if new_index < 0 or new_index >= len(slot_rows):
                return
            slot_rows[index], slot_rows[new_index] = slot_rows[new_index], slot_rows[index]
            refresh_tree(new_index)

        def save_slots() -> None:
            try:
                update_controller_slot_mapping(slot_rows)
            except DjiControllerError as exc:
                messagebox.showerror("Dummy Slot Manager", str(exc), parent=window)
                return
            self.set_status("Saved dummy slot order and local names.")
            window.destroy()
            self.verify_dummy_slots_async()

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Rename", command=rename_selected).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Move Up", command=lambda: move_selected(-1)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Move Down", command=lambda: move_selected(1)).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(buttons, text="Save", command=save_slots).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        tree.bind("<Double-1>", lambda _event: rename_selected())
        refresh_tree(0)

    def sync_active_project(self) -> None:
        self.load_selected_creator_paths_to_controller()

    def _sync_files(self, files: list[Path]) -> None:
        result = sync_files(files)
        skipped = f"; skipped {len(result.skipped)} existing folder(s)" if result.skipped else ""
        message = f"Sync complete. Updated {len(result.copied)} dummy slot(s){skipped}."
        self.enqueue_status(message)
        self.show_info_async("Sync Complete", message)

    def start_sync_progress(self) -> None:
        self.sync_in_progress = True
        self.sync_button.configure(state=tk.DISABLED)
        self.sync_progress.grid()
        self.sync_progress.start(12)

    def stop_sync_progress(self) -> None:
        if not self.sync_in_progress:
            return
        self.sync_in_progress = False
        self.sync_progress.stop()
        self.sync_progress.grid_remove()
        if self.dummy_verified:
            self.sync_button.configure(state=tk.NORMAL)

    def run_background(self, started_message: str, target) -> None:
        self.set_status(started_message)
        self.controller_operation_active = True

        def runner() -> None:
            try:
                target()
            except CreatorApiError as exc:
                message = str(exc)
                if is_creator_auth_error(exc):
                    self.after(0, partial(self.show_auth_expired_popup, message))
                    self.enqueue_status("ScanAir session expired. Re-authenticate to continue.")
                else:
                    self.show_error_async("Creator Error", message)
                    self.enqueue_status("Creator operation failed.")
            except DjiControllerError as exc:
                message = str(exc)
                if is_controller_disconnect_message(message):
                    self.mark_controller_unavailable_async(message)
                    self.enqueue_status("DJI RC 2 disconnected. Operation stopped safely.")
                else:
                    self.show_error_async("DJI Controller Error", message)
                    self.enqueue_status("Controller operation failed.")
            except Exception as exc:
                self.show_error_async("Error", str(exc))
                self.enqueue_status("Operation failed.")
            finally:
                self.after(0, self.finish_controller_operation)

        threading.Thread(target=runner, daemon=True).start()

    def finish_controller_operation(self) -> None:
        self.controller_operation_active = False
        self.stop_sync_progress()
        self.schedule_connection_check()

    def mark_controller_unavailable_async(self, message: str) -> None:
        def mark_unavailable() -> None:
            self.mark_controller_unavailable(message)

        self.after(0, mark_unavailable)

    def show_error_async(self, title: str, message: str) -> None:
        def show_error() -> None:
            messagebox.showerror(title, message, parent=self)

        self.after(0, show_error)

    def show_info_async(self, title: str, message: str) -> None:
        def show_info() -> None:
            messagebox.showinfo(title, message, parent=self)

        self.after(0, show_info)

    def enqueue_status(self, message: str) -> None:
        self.messages.put(message)

    def _drain_messages(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            self.set_status(message)
        self.after(200, self._drain_messages)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _on_close(self) -> None:
        if self.connection_check_after_id is not None:
            self.after_cancel(self.connection_check_after_id)
            self.connection_check_after_id = None
        self.destroy()


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def first_reachable_url(candidates: tuple[str, ...], path: str) -> str:
    for base_url in candidates:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        request = Request(url, headers={"Accept": "application/json,text/html;q=0.9,*/*;q=0.8"})
        try:
            with urlopen(request, timeout=0.75) as response:
                if 200 <= response.status < 400:
                    return base_url
        except HTTPError as exc:
            if 200 <= exc.code < 400:
                return base_url
        except (OSError, TimeoutError, URLError):
            continue
    return ""


def set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except Exception:
        return


def format_timestamp(value: str) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    if cleaned.replace(".", "", 1).isdigit():
        try:
            numeric = float(cleaned)
        except ValueError:
            return value
        if numeric > 1_000_000_000_000_000_000:
            numeric /= 1_000_000_000
        elif numeric > 1_000_000_000_000_000:
            numeric /= 1_000_000
        elif numeric > 1_000_000_000_000:
            numeric /= 1_000
        try:
            return datetime.fromtimestamp(numeric).strftime("%Y-%m-%d %I:%M %p")
        except (OSError, OverflowError, ValueError):
            return value
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return value
    local_time = parsed.astimezone() if parsed.tzinfo else parsed
    return local_time.strftime("%Y-%m-%d %I:%M %p")


def is_controller_disconnect_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "controller was not found",
            "could not find controller folder",
            "dummy mission folder disappeared",
            "windows did not delete waypoint item",
            "shell namespace 'this pc' is unavailable",
            "device is not ready",
            "has been disconnected",
            "object invoked has disconnected",
            "rpc server is unavailable",
            "0x80010108",
            "0x800706ba",
        )
    )


def is_creator_auth_error(error: CreatorApiError) -> bool:
    if error.status_code in (401, 403):
        return True
    lowered = str(error).lower()
    return any(
        marker in lowered
        for marker in (
            "token has expired",
            "jwt expired",
            "session expired",
            "not authenticated",
            "invalid token",
            "unauthorized",
            "forbidden",
        )
    )


def friendly_waiting_message(detail: str = "") -> str:
    suffix = f"\n\nLast check: {detail}" if detail and "controller was not found" not in detail.lower() else ""
    return (
        "Waiting for a DJI RC 2 controller.\n\n"
        "Plug in the controller by USB, unlock it, and choose file transfer if Windows or Android asks."
        f"{suffix}"
    )


def main() -> None:
    app = ScanAirImporterApp()
    app.mainloop()
