from __future__ import annotations

import queue
import threading
import tkinter as tk
import os
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .dji_mtp import (
    DjiControllerError,
    create_waypoint_backup,
    get_controller_slot_mapping,
    list_backup_files,
    list_device_files,
    restore_waypoint_backup,
    sync_files,
    update_controller_slot_mapping,
    verify_dummy_slots,
    verify_controller,
)
from .drag_drop import FileDropAdapter, make_tk_root_class
from .server import ImportServer
from .store import ProjectStore


class ScanAirImporterApp(make_tk_root_class()):
    def __init__(self) -> None:
        super().__init__()
        self.title("ScanAir DJI Importer")
        self.geometry("1040x680")
        self.minsize(900, 560)

        self.store = ProjectStore()
        self.store.ensure_default_project()
        self.messages: queue.Queue[str] = queue.Queue()
        self.server = ImportServer(self.store, self.enqueue_status)
        self.server.start()
        self.drop_target = FileDropAdapter(lambda paths: self.after(0, lambda: self.import_dropped_files(paths)))

        self.project_var = tk.StringVar()
        self.status_var = tk.StringVar(value=f"Ready. Import endpoint: {self.server.url}")
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
        self.slot_manager_window: tk.Toplevel | None = None
        self.operational_widgets: list[tk.Widget] = []

        self._build_ui()
        self._enable_drag_and_drop()
        self.set_operational_enabled(False)
        self.show_dummy_setup_popup()
        self.refresh_projects()
        self.after(500, self.verify_dummy_slots_async)
        self.schedule_connection_check()
        self.after(200, self._drain_messages)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="ScanAir DJI Importer", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

        project_panel = ttk.LabelFrame(root, text="Projects", padding=12)
        project_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        project_panel.rowconfigure(1, weight=1)

        self.new_project_button = ttk.Button(project_panel, text="New Project", command=self.create_project)
        self.new_project_button.grid(row=0, column=0, sticky="ew")
        self.project_list = tk.Listbox(project_panel, width=30, exportselection=False)
        self.project_list.grid(row=1, column=0, sticky="nsew", pady=10)
        self.project_list.bind("<<ListboxSelect>>", lambda _event: self.select_project_from_list())
        self.set_active_button = ttk.Button(project_panel, text="Set Active", command=self.set_selected_active)
        self.set_active_button.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.delete_project_button = ttk.Button(project_panel, text="Delete Project", command=self.delete_project)
        self.delete_project_button.grid(row=3, column=0, sticky="ew")

        main = ttk.Frame(root)
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        active_bar = ttk.LabelFrame(main, text="Active Project", padding=12)
        active_bar.grid(row=0, column=0, sticky="ew")
        active_bar.columnconfigure(1, weight=1)
        ttk.Label(active_bar, text="Website imports into:").grid(row=0, column=0, sticky="w")
        ttk.Label(active_bar, textvariable=self.project_var, font=("Segoe UI", 11, "bold")).grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.import_button = ttk.Button(active_bar, text="Import KMZ Files", command=self.import_files)
        self.import_button.grid(row=0, column=2, padx=(8, 0))
        self.drop_label = ttk.Label(
            active_bar,
            text="Drop KMZ files here to import into the active project",
            anchor="center",
            relief=tk.RIDGE,
            padding=(12, 10),
        )
        self.drop_label.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        files_panel = ttk.LabelFrame(main, text="Stored KMZ Files", padding=12)
        files_panel.grid(row=1, column=0, sticky="nsew", pady=12)
        files_panel.rowconfigure(0, weight=1)
        files_panel.columnconfigure(0, weight=1)
        columns = ("name", "size", "modified")
        self.files_tree = ttk.Treeview(files_panel, columns=columns, show="headings", selectmode="extended")
        self.files_tree.heading("name", text="Name")
        self.files_tree.heading("size", text="Size")
        self.files_tree.heading("modified", text="Modified")
        self.files_tree.column("name", width=360, anchor="w")
        self.files_tree.column("size", width=100, anchor="e")
        self.files_tree.column("modified", width=220, anchor="w")
        self.files_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(files_panel, orient=tk.VERTICAL, command=self.files_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.files_tree.configure(yscrollcommand=scrollbar.set)
        file_buttons = ttk.Frame(files_panel)
        file_buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.remove_files_button = ttk.Button(file_buttons, text="Remove Selected", command=self.remove_selected_files)
        self.remove_files_button.pack(side=tk.LEFT)
        self.open_project_button = ttk.Button(file_buttons, text="Open Project Folder", command=self.open_active_project_folder)
        self.open_project_button.pack(side=tk.LEFT, padx=(8, 0))

        device_panel = ttk.LabelFrame(main, text="DJI RC 2 Sync", padding=12)
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
        self.sync_button = ttk.Button(device_panel, text="Sync Active Project", command=self.sync_active_project)
        self.sync_button.grid(row=2, column=3)
        self.sync_progress = ttk.Progressbar(device_panel, mode="indeterminate")
        self.sync_progress.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.sync_progress.grid_remove()

        backup_panel = ttk.LabelFrame(main, text="Backup Manager", padding=12)
        backup_panel.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        backup_panel.columnconfigure(0, weight=1)
        self.backup_list = tk.Listbox(backup_panel, height=4, exportselection=False)
        self.backup_list.grid(row=0, column=0, rowspan=4, sticky="ew", padx=(0, 10))
        self.backup_button = ttk.Button(backup_panel, text="Backup Waypoints", command=self.backup_waypoints)
        self.backup_button.grid(row=0, column=1, sticky="ew", pady=(0, 4))
        self.restore_button = ttk.Button(backup_panel, text="Restore Selected", command=self.restore_selected_backup)
        self.restore_button.grid(row=1, column=1, sticky="ew", pady=(0, 4))
        self.remove_backup_button = ttk.Button(backup_panel, text="Remove Selected", command=self.remove_selected_backup)
        self.remove_backup_button.grid(row=2, column=1, sticky="ew", pady=(0, 4))
        ttk.Button(backup_panel, text="Refresh Backups", command=self.refresh_backups).grid(row=3, column=1, sticky="ew")
        self.operational_widgets = [
            self.new_project_button,
            self.set_active_button,
            self.delete_project_button,
            self.import_button,
            self.remove_files_button,
            self.open_project_button,
            self.show_device_files_button,
            self.manage_slots_button,
            self.sync_button,
            self.backup_button,
            self.restore_button,
            self.remove_backup_button,
        ]

    def _enable_drag_and_drop(self) -> None:
        registered = [
            self.drop_target.register(self, self, self.drop_label, self.files_tree),
        ]
        if any(registered):
            self.set_status(f"Ready. Drop KMZ files into the app or use {self.server.url}")
        else:
            self.drop_label.configure(text="Use Import KMZ Files to add files. Drag-and-drop requires tkinterdnd2.")

    def set_operational_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in self.operational_widgets:
            widget["state"] = state

    def show_dummy_setup_popup(self) -> None:
        if self.dummy_popup and self.dummy_popup.winfo_exists():
            self.dummy_popup.deiconify()
            self.dummy_popup.lift()
            return
        popup = tk.Toplevel(self)
        popup.title("DJI RC 2 Setup Required")
        popup.geometry("660x520")
        popup.minsize(580, 420)
        popup.resizable(True, True)
        popup.attributes("-topmost", True)
        popup.protocol("WM_DELETE_WINDOW", popup.lift)
        self.dummy_popup = popup
        outer = ttk.Frame(popup, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        frame = ttk.Frame(canvas, padding=(0, 0, 10, 0))
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        ttk.Label(frame, text="Create 10 DJI Fly dummy missions", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        instructions = (
            "On the RC 2, create one waypoint mission in DJI Fly with at least 2 waypoints.\n\n"
            "Use Save As / duplicate until there are 10 identical copies of that same path.\n\n"
            "ScanAir sorts those copies by KMZ creation time, newest first, and remembers their controller-generated IDs for this physical controller."
        )
        ttk.Label(frame, text=instructions, wraplength=470, justify=tk.LEFT).pack(anchor="w", pady=(12, 12))
        ttk.Label(frame, textvariable=self.slot_status_var, wraplength=590, justify=tk.LEFT).pack(anchor="w", pady=(0, 14))
        ttk.Button(frame, text="Re-check Controller", command=self.verify_dummy_slots_async).pack(anchor="e")

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
                self.apply_dummy_verification_async(False, str(exc), "Controller identity not checked")
                return
            message = f"{path}\n\n{verification.message}"
            self.apply_dummy_verification_async(verification.ok, message, verification.controller_label)

        threading.Thread(target=runner, daemon=True).start()

    def apply_dummy_verification_async(self, ok: bool, message: str, controller_label: str) -> None:
        def apply() -> None:
            self.apply_dummy_verification(ok, message, controller_label)

        self.after(0, apply)

    def apply_dummy_verification(self, ok: bool, message: str, controller_label: str) -> None:
        self.verification_in_progress = False
        self.controller_connected = ok
        self.dummy_verified = ok
        self.slot_status_var.set(message)
        self.controller_identity_var.set(f"Controller identity: {controller_label}")
        self.set_operational_enabled(ok)
        if ok:
            self.device_var.set("Dummy slots verified for connected controller")
            self.set_status("Ready. Dummy slot IDs verified.")
            if self.dummy_popup and self.dummy_popup.winfo_exists():
                self.dummy_popup.destroy()
            self.bring_main_window_to_front()
        else:
            self.device_var.set("Dummy slots not verified")
            if "controller was not found" in message.lower():
                self.set_status("DJI RC 2 disconnected. Plug it back in, unlock it, and choose file transfer.")
            else:
                self.set_status("Setup required: create/duplicate 10 identical dummy missions on the RC 2.")
            self.show_dummy_setup_popup()
        self.schedule_connection_check()

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
        self.slot_status_var.set(message)
        self.set_operational_enabled(False)
        self.set_status("DJI RC 2 disconnected. Plug it back in, unlock it, and choose file transfer.")
        self.show_dummy_setup_popup()

    def bring_main_window_to_front(self) -> None:
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(800, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def require_dummy_slots(self) -> bool:
        if self.dummy_verified:
            return True
        self.show_dummy_setup_popup()
        self.set_status("Connect and verify the DJI RC 2 before using the importer.")
        return False

    def refresh_projects(self) -> None:
        active = self.store.get_active_project_name()
        self.project_var.set(active or "No project selected")
        self.project_list.delete(0, tk.END)
        for project in self.store.list_projects():
            label = f"* {project.name}" if project.active else f"  {project.name}"
            self.project_list.insert(tk.END, label)
            if project.active:
                self.project_list.selection_clear(0, tk.END)
                self.project_list.selection_set(tk.END)
        self.refresh_files()
        self.refresh_backups()

    def refresh_files(self) -> None:
        self.files_tree.delete(*self.files_tree.get_children())
        active = self.store.get_active_project_name()
        if not active:
            return
        for stored in self.store.get_project(active).files:
            self.files_tree.insert("", tk.END, values=(stored.name, format_size(stored.size), stored.modified_at))

    def selected_project_name(self) -> str | None:
        selection = self.project_list.curselection()
        if not selection:
            return None
        raw = self.project_list.get(selection[0])
        return raw.replace("*", "", 1).strip()

    def select_project_from_list(self) -> None:
        project = self.selected_project_name()
        if project:
            self.project_var.set(project)

    def set_selected_active(self) -> None:
        project = self.selected_project_name()
        if not project:
            return
        self.store.set_active_project(project)
        self.set_status(f"Active project set to {project}.")
        self.refresh_projects()

    def create_project(self) -> None:
        if not self.require_dummy_slots():
            return
        name = simpledialog.askstring("New Project", "Project name:", parent=self)
        if not name:
            return
        try:
            self.store.create_project(name)
            self.store.set_active_project(name)
            self.set_status(f"Created project {name}.")
            self.refresh_projects()
        except Exception as exc:
            messagebox.showerror("Project Error", str(exc), parent=self)

    def delete_project(self) -> None:
        if not self.require_dummy_slots():
            return
        project = self.selected_project_name()
        if not project:
            return
        if not messagebox.askyesno("Delete Project", f"Delete project '{project}' and its stored KMZ files?", parent=self):
            return
        try:
            self.store.delete_project(project)
            if not self.store.list_project_names():
                self.store.create_project("Default")
                self.store.set_active_project("Default")
            self.set_status(f"Deleted project {project}.")
            self.refresh_projects()
        except Exception as exc:
            messagebox.showerror("Project Error", str(exc), parent=self)

    def import_files(self) -> None:
        if not self.require_dummy_slots():
            return
        paths = filedialog.askopenfilenames(
            parent=self,
            title="Import DJI KMZ files",
            filetypes=[("DJI KMZ files", "*.kmz"), ("All files", "*.*")],
        )
        if not paths:
            return
        try:
            self.import_paths([Path(path) for path in paths])
        except Exception as exc:
            messagebox.showerror("Import Error", str(exc), parent=self)

    def import_dropped_files(self, paths: list[Path]) -> None:
        if not self.require_dummy_slots():
            return
        try:
            self.import_paths(paths)
        except Exception as exc:
            messagebox.showerror("Drop Import Error", str(exc), parent=self)

    def import_paths(self, paths: list[Path]) -> None:
        kmz_paths = [path for path in paths if path.is_file() and path.suffix.lower() == ".kmz"]
        skipped = len(paths) - len(kmz_paths)
        if not kmz_paths:
            raise ValueError("Drop one or more .kmz files to import into the active project.")
        imported = self.store.add_files(kmz_paths)
        suffix = f" Skipped {skipped} non-KMZ item(s)." if skipped else ""
        self.set_status(f"Imported {len(imported)} KMZ file(s) into {self.store.get_active_project_name()}.{suffix}")
        self.refresh_projects()

    def remove_selected_files(self) -> None:
        if not self.require_dummy_slots():
            return
        active = self.store.get_active_project_name()
        if not active:
            return
        selected = self.files_tree.selection()
        if not selected:
            return
        names = [self.files_tree.item(item, "values")[0] for item in selected]
        if not messagebox.askyesno("Remove Files", f"Remove {len(names)} stored KMZ file(s) from {active}?", parent=self):
            return
        try:
            for name in names:
                self.store.delete_file(active, name)
            self.set_status(f"Removed {len(names)} KMZ file(s).")
            self.refresh_projects()
        except Exception as exc:
            messagebox.showerror("File Error", str(exc), parent=self)

    def open_active_project_folder(self) -> None:
        if not self.require_dummy_slots():
            return
        active = self.store.get_active_project_name()
        if not active:
            return
        os.startfile(self.store.project_files_path(active))

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
        window.geometry("820x440")
        window.minsize(720, 360)
        self.slot_manager_window = window

        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=controller_label, wraplength=760).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        columns = ("slot", "local_name", "package", "kmz")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("slot", text="Sync #")
        tree.heading("local_name", text="Local Name")
        tree.heading("package", text="Controller Folder ID")
        tree.heading("kmz", text="KMZ ID")
        tree.column("slot", width=70, anchor="center", stretch=False)
        tree.column("local_name", width=180, anchor="w")
        tree.column("package", width=230, anchor="w")
        tree.column("kmz", width=230, anchor="w")
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
                    values=(index, slot.get("local_name") or "", slot.get("package_name") or "", slot.get("kmz_name") or ""),
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
        if not self.require_dummy_slots():
            return
        active = self.store.get_active_project_name()
        files = self.store.active_files()
        if not active or not files:
            messagebox.showwarning("Sync", "The active project has no KMZ files to sync.", parent=self)
            return
        prompt = (
            f"Sync {len(files)} KMZ file(s) from '{active}' to the DJI RC 2?\n\n"
            "This overwrites the matching remembered dummy slots in sequence. Unused slots are left unchanged."
        )
        if not messagebox.askyesno("Sync Active Project", prompt, parent=self):
            return
        self.start_sync_progress()
        self.run_background("Syncing active project to DJI RC 2...", lambda: self._sync_files(files))

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

    def refresh_backups(self) -> None:
        if not hasattr(self, "backup_list"):
            return
        self.backup_list.delete(0, tk.END)
        for backup in list_backup_files():
            size_mb = backup.stat().st_size / (1024 * 1024)
            self.backup_list.insert(tk.END, f"{backup.name} ({size_mb:.1f} MB)")

    def selected_backup_path(self) -> Path | None:
        selection = self.backup_list.curselection()
        backups = list_backup_files()
        if not selection or selection[0] >= len(backups):
            return None
        return backups[selection[0]]

    def backup_waypoints(self) -> None:
        if not self.require_dummy_slots():
            return
        self.run_background("Backing up DJI RC 2 waypoint folder...", self._backup_waypoints)

    def _backup_waypoints(self) -> None:
        backup_path = create_waypoint_backup()
        self.enqueue_status(f"Created backup: {backup_path.name}")
        self.after(0, self.refresh_backups)

    def restore_selected_backup(self) -> None:
        if not self.require_dummy_slots():
            return
        backup_path = self.selected_backup_path()
        if not backup_path:
            messagebox.showwarning("Restore Backup", "Select a backup to restore.", parent=self)
            return
        prompt = (
            f"Restore {backup_path.name} to the DJI RC 2?\n\n"
            "This deletes everything currently in the controller waypoint folder before copying the backup back."
        )
        if not messagebox.askyesno("Restore Backup", prompt, parent=self):
            return
        self.run_background("Restoring DJI RC 2 waypoint backup...", lambda: self._restore_backup(backup_path))

    def _restore_backup(self, backup_path: Path) -> None:
        result = restore_waypoint_backup(backup_path)
        self.enqueue_status(f"Restored backup; deleted {len(result.deleted)} item(s), restored {len(result.restored)} item(s).")

    def remove_selected_backup(self) -> None:
        if not self.require_dummy_slots():
            return
        backup_path = self.selected_backup_path()
        if not backup_path:
            messagebox.showwarning("Remove Backup", "Select a backup to remove.", parent=self)
            return
        if not messagebox.askyesno("Remove Backup", f"Delete backup '{backup_path.name}' from this computer?", parent=self):
            return
        try:
            backup_path.unlink()
            self.set_status(f"Removed backup: {backup_path.name}")
            self.refresh_backups()
        except OSError as exc:
            messagebox.showerror("Remove Backup", str(exc), parent=self)

    def run_background(self, started_message: str, target) -> None:
        self.set_status(started_message)
        self.controller_operation_active = True

        def runner() -> None:
            try:
                target()
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
            self.refresh_projects()
        self.after(200, self._drain_messages)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _on_close(self) -> None:
        if self.connection_check_after_id is not None:
            self.after_cancel(self.connection_check_after_id)
            self.connection_check_after_id = None
        self.drop_target.unregister_all()
        self.server.stop()
        self.destroy()


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


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


def main() -> None:
    app = ScanAirImporterApp()
    app.mainloop()
