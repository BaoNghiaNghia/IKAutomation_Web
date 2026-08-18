from __future__ import annotations

import json
import queue
import tkinter as tk
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog

import customtkinter as ctk

from ik_chrome_auto.config import (
    ensure_data_dirs,
    load_config,
    save_config,
    unique_profile_id,
)
from ik_chrome_auto.interaction import format_coordinate, validate_viewport
from ik_chrome_auto.models import (
    Auto2048Speed,
    CommandKind,
    ProfileConfig,
    ProfileMode,
    WorkerSnapshot,
    WorkerState,
)
from ik_chrome_auto.runner import AUTO_2048_TIMINGS, MultiProfileRunner
AUTO_2048_SPEED_LABELS = {
    speed: f"{timing.label} ({timing.move_delay_seconds:.2f}s)"
    for speed, timing in AUTO_2048_TIMINGS.items()
}

@dataclass(slots=True)
class ProfileRow:
    profile: ProfileConfig
    status: tk.StringVar
    resources: tk.StringVar
    state_badge: tk.Label
    inspect_button: ctk.CTkButton
    auto_2048_button: ctk.CTkButton


class Dashboard:
    def __init__(self, root: tk.Tk, config_path: Path) -> None:
        self.root = root
        self.config = load_config(config_path)
        ensure_data_dirs(self.config)
        self.updates: queue.Queue[WorkerSnapshot] = queue.Queue()
        self.coordinate_updates: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue()
        self.runner = MultiProfileRunner(
            self.config,
            self.updates.put,
            on_coordinate=lambda profile_id, event: self.coordinate_updates.put(
                (profile_id, event)
            ),
        )
        self.rows: dict[str, ProfileRow] = {}
        self.viewport_width = tk.StringVar(value=str(self.config.browser.viewport_width))
        self.viewport_height = tk.StringVar(value=str(self.config.browser.viewport_height))
        self.auto_resize = tk.BooleanVar(value=self.config.browser.auto_resize)
        self.config.browser.low_memory_mode = True
        save_config(self.config)
        self.total_profiles_text = tk.StringVar(value="0")
        self.open_profiles_text = tk.StringVar(value="0")
        self.ram_usage_text = tk.StringVar(value="0 MB")
        self.cpu_usage_text = tk.StringVar(value="0.0%")
        self._last_resource_poll = 0.0
        self._last_ram_trim = 0.0
        default_master = self.config.profiles[0].id if self.config.profiles else ""
        self.sync_master = tk.StringVar(value=default_master)
        self.sync_button_text = tk.StringVar(value="Bật sync chuột")
        self.sync_status = tk.StringVar(value="Sync đang tắt")
        self.coordinate_text = tk.StringVar(value="Chưa đo tọa độ")
        self.last_coordinate: tuple[str, dict[str, object]] | None = None
        self.inspecting_profile_id: str | None = None
        self.auto_2048_profiles: set[str] = set()
        self.auto_2048_speed_text = tk.StringVar(
            value=AUTO_2048_SPEED_LABELS[self.config.auto_2048_speed]
        )
        self.drag_items_visible = True
        self.drag_button_text = tk.StringVar(value="Ẩn drag tất cả")
        self.pin_windows = tk.BooleanVar(value=False)
        self.table = ctk.CTkFrame(root)
        self.log: ctk.CTkTextbox
        self._log_lines: deque[str] = deque(maxlen=10)
        self._build()
        self._draw_rows()
        self.root.after(200, self._poll_updates)

    def _build(self) -> None:
        self.root.title("IK Auto — Browser Control")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = screen_width // 2
        height = screen_height // 2
        # Leave room for the Windows taskbar while anchoring the compact
        # control panel at the lower-right corner of the active display.
        left = max(0, screen_width - width - 12)
        top = max(0, screen_height - height - 48)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(640, 420)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        ctk.set_appearance_mode("light")
        self.root.configure(fg_color="#eaf1f8")

        parent = ctk.CTkFrame(self.root, fg_color="transparent")
        parent.pack(fill="both", expand=True, padx=12, pady=10)
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            header, text="IK Auto", text_color="#20324a", font=("Segoe UI", 22, "bold")
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="Browser control",
            text_color="#5b6f87",
            font=("Segoe UI", 14),
        ).pack(side="left", padx=(8, 0), pady=(5, 0))
        ctk.CTkLabel(
            header,
            text="● Local & secure",
            fg_color="#d8f5e7",
            text_color="#157a50",
            corner_radius=12,
            padx=9,
            pady=4,
            font=("Segoe UI", 13, "bold"),
        ).pack(side="right")

        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=260)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left_panel = ctk.CTkScrollableFrame(
            body,
            fg_color="transparent",
            scrollbar_button_color="#b8c8dc",
            scrollbar_button_hover_color="#8fa9c7",
        )
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right_panel = ctk.CTkFrame(body, fg_color="transparent")
        right_panel.grid(row=0, column=1, sticky="nsew")
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(1, weight=1)

        profiles_card = self._card(left_panel)
        profiles_card.pack(fill="x", pady=(0, 7))
        profile_head = ctk.CTkFrame(profiles_card, fg_color="transparent")
        profile_head.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(profile_head, text="Chrome profiles", text_color="#20324a", font=("Segoe UI", 16, "bold")).pack(side="left")
        self._button(profile_head, "+ Thêm", self._add_profile, "primary").pack(side="right")
        ctk.CTkLabel(profiles_card, text="Phiên browser độc lập, lưu cục bộ.", text_color="#6d8098", font=("Segoe UI", 12)).pack(anchor="w", padx=12, pady=(2, 7))
        action_row = ctk.CTkFrame(profiles_card, fg_color="transparent")
        action_row.pack(fill="x", padx=12, pady=(0, 10))
        self._button(action_row, "Mở tất cả", self.runner.open_all, "primary").pack(side="left", fill="x", expand=True)
        self._button(action_row, "Dừng", self.runner.stop_all, "danger").pack(side="left", padx=(6, 0))

        viewport_card = self._card(left_panel)
        viewport_card.pack(fill="x", pady=(0, 7))
        self._section_label(viewport_card, "Cấu hình browser", "Viewport và cửa sổ.")
        viewport_row = ctk.CTkFrame(viewport_card, fg_color="transparent")
        viewport_row.pack(fill="x", padx=12, pady=(6, 5))
        ctk.CTkEntry(viewport_row, textvariable=self.viewport_width, width=62, height=34, corner_radius=11, font=("Segoe UI", 13)).pack(side="left")
        ctk.CTkLabel(viewport_row, text="×", text_color="#6d8098", width=16).pack(side="left")
        ctk.CTkEntry(viewport_row, textvariable=self.viewport_height, width=62, height=34, corner_radius=11, font=("Segoe UI", 13)).pack(side="left")
        self._button(viewport_row, "Áp dụng", self._apply_size_all, "soft").pack(side="right")
        ctk.CTkCheckBox(viewport_card, text="Tự resize khi mở", variable=self.auto_resize, font=("Segoe UI", 13), checkbox_width=21, checkbox_height=21).pack(anchor="w", padx=12)
        window_row = ctk.CTkFrame(viewport_card, fg_color="transparent")
        window_row.pack(fill="x", padx=12, pady=(6, 0))
        self._button(window_row, "Sắp xếp", self._arrange_windows, "soft").pack(side="left")
        self.drag_button = self._button(window_row, self.drag_button_text.get(), self._toggle_all_drag_items, "soft")
        self.drag_button.pack(side="left", padx=(5, 0))
        ctk.CTkCheckBox(viewport_card, text="Ghim cửa sổ", variable=self.pin_windows, command=self._toggle_pin_windows, font=("Segoe UI", 13), checkbox_width=21, checkbox_height=21).pack(anchor="w", padx=12, pady=(4, 10))

        automation_card = self._card(left_panel)
        automation_card.pack(fill="x")
        self._section_label(automation_card, "Tự động hóa", "Sync thao tác · Auto 2048")
        master_row = ctk.CTkFrame(automation_card, fg_color="transparent")
        master_row.pack(fill="x", padx=12, pady=(6, 4))
        self.master_box = ctk.CTkComboBox(master_row, variable=self.sync_master, width=130, height=34, state="readonly", corner_radius=11, font=("Segoe UI", 13))
        self.master_box.pack(side="left", fill="x", expand=True)
        self.sync_button = self._button(master_row, self.sync_button_text.get(), self._toggle_sync, "soft")
        self.sync_button.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(automation_card, textvariable=self.sync_status, text_color="#4b78c2", font=("Segoe UI", 12)).pack(anchor="w", padx=12)
        ctk.CTkFrame(automation_card, height=1, fg_color="#d9e3ef").pack(fill="x", padx=12, pady=7)
        ctk.CTkLabel(automation_card, text="Tốc độ Auto 2048", text_color="#52657d", font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=12)
        speed_row = ctk.CTkFrame(automation_card, fg_color="transparent")
        speed_row.pack(fill="x", padx=12, pady=(4, 10))
        self.auto_2048_speed_box = ctk.CTkComboBox(speed_row, variable=self.auto_2048_speed_text, values=tuple(AUTO_2048_SPEED_LABELS.values()), width=165, height=34, state="readonly", corner_radius=11, font=("Segoe UI", 13))
        self.auto_2048_speed_box.pack(side="left", fill="x", expand=True)
        self._button(speed_row, "Lưu", self._apply_auto_2048_speed, "soft").pack(side="left", padx=(6, 0))

        overview = self._card(right_panel)
        overview.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        cards = (
            ("Tổng profile", self.total_profiles_text),
            ("Đang mở", self.open_profiles_text),
            ("RAM Chrome (Auto)", self.ram_usage_text),
            ("CPU Chrome", self.cpu_usage_text),
        )
        for index, (title, variable) in enumerate(cards):
            card = ctk.CTkFrame(overview, fg_color="#f7faff", corner_radius=14)
            card.grid(row=0, column=index, sticky="ew", padx=(8 if index else 0, 0), pady=8)
            ctk.CTkLabel(card, text=title, text_color="#6d8098", font=("Segoe UI", 12)).pack(anchor="w", padx=10, pady=(7, 0))
            ctk.CTkLabel(card, textvariable=variable, text_color="#20324a", font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=10, pady=(0, 7))
            overview.columnconfigure(index, weight=1)
        progress_card = self._card(right_panel)
        progress_card.grid(row=1, column=0, sticky="nsew")
        progress_head = ctk.CTkFrame(progress_card, fg_color="transparent")
        progress_head.pack(fill="x", padx=12, pady=(10, 5))
        ctk.CTkLabel(progress_head, text="Tiến trình profile", text_color="#20324a", font=("Segoe UI", 16, "bold")).pack(side="left")
        ctk.CTkLabel(progress_head, textvariable=self.open_profiles_text, fg_color="#dceaff", text_color="#4171bd", corner_radius=10, padx=8, pady=3, font=("Segoe UI", 12, "bold")).pack(side="right")
        self.table = ctk.CTkScrollableFrame(progress_card, fg_color="transparent", scrollbar_button_color="#b8c8dc", scrollbar_button_hover_color="#8fa9c7")
        self.table.pack(fill="both", expand=True, padx=(9, 5), pady=(0, 9))

        bottom = ctk.CTkFrame(right_panel, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(0, weight=2)
        bottom.columnconfigure(1, weight=3)
        coordinate_frame = self._card(bottom)
        coordinate_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        coord_head = ctk.CTkFrame(coordinate_frame, fg_color="transparent")
        coord_head.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(coord_head, text="Lấy tọa độ", text_color="#20324a", font=("Segoe UI", 14, "bold")).pack(side="left")
        self._button(coord_head, "Copy x,y", self._copy_coordinate_xy, "soft").pack(side="right")
        self._button(coord_head, "JSON", self._copy_coordinate_json, "soft").pack(side="right", padx=5)
        ctk.CTkLabel(coordinate_frame, textvariable=self.coordinate_text, text_color="#52657d", font=("Consolas", 12), anchor="w", justify="left", wraplength=336).pack(fill="x", padx=10, pady=(5, 8))
        log_frame = self._card(bottom)
        log_frame.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(log_frame, text="Nhật ký gần nhất", text_color="#20324a", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        self.log = ctk.CTkTextbox(log_frame, height=77, state="disabled", font=("Consolas", 12), corner_radius=11, fg_color="#f3f7fc", text_color="#52657d")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 8))

    @staticmethod
    def _card(parent: tk.Misc) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, fg_color="#f9fcff", border_color="#ffffff", border_width=1, corner_radius=18)

    @staticmethod
    def _section_label(parent: tk.Misc, title: str, description: str) -> None:
        ctk.CTkLabel(parent, text=title, text_color="#20324a", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(parent, text=description, text_color="#6d8098", font=("Segoe UI", 12), wraplength=240, justify="left").pack(anchor="w", padx=12, pady=(2, 0))

    @staticmethod
    def _button(parent: tk.Misc, text: str, command: object, kind: str) -> ctk.CTkButton:
        palette = {
            "primary": ("#6f9ef8", "#517fd9", "#ffffff"),
            "danger": ("#ef7d83", "#d85f68", "#ffffff"),
            "soft": ("#e8f0fb", "#d8e5f5", "#4c6686"),
        }
        foreground, hover, text_color = palette[kind]
        return ctk.CTkButton(parent, text=text, command=command, height=34, corner_radius=11, border_spacing=4, fg_color=foreground, hover_color=hover, text_color=text_color, font=("Segoe UI", 12, "bold"))

    def _refresh_sync_button(self) -> None:
        self.sync_button.configure(text=self.sync_button_text.get())

    def _draw_rows(self) -> None:
        for child in self.table.winfo_children():
            child.destroy()
        self.rows.clear()
        for profile in self.config.profiles:
            status = tk.StringVar(value="Đã dừng")
            resources = tk.StringVar(value="—")
            item = ctk.CTkFrame(
                self.table,
                fg_color="#f8fbff",
                border_color="#ffffff",
                border_width=1,
                corner_radius=14,
            )
            item.pack(fill="x", pady=(0, 5), padx=(0, 3))
            top = ctk.CTkFrame(item, fg_color="transparent")
            top.pack(fill="x", padx=10, pady=(7, 0))
            identity = ctk.CTkFrame(top, fg_color="transparent")
            identity.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(identity, text=profile.name, text_color="#20324a", font=("Segoe UI", 13, "bold")).pack(side="left")
            ctk.CTkLabel(identity, text=f"  {profile.id} · {profile.mode.value}", text_color="#6d8098", font=("Segoe UI", 12)).pack(side="left")
            state_badge = ctk.CTkLabel(top, text="Đã dừng", fg_color="#e8eef5", text_color="#52657d", corner_radius=10, font=("Segoe UI", 12, "bold"), padx=8, pady=2)
            state_badge.pack(side="right")
            detail = ctk.CTkFrame(item, fg_color="transparent")
            detail.pack(fill="x", padx=10, pady=(2, 4))
            ctk.CTkLabel(detail, textvariable=status, text_color="#52657d", font=("Segoe UI", 12), anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(detail, textvariable=resources, text_color="#7f90a5", font=("Segoe UI", 12)).pack(side="right")
            controls = ctk.CTkFrame(item, fg_color="transparent")
            controls.pack(fill="x", padx=10, pady=(0, 7))
            self._button(controls, "Mở", lambda profile_id=profile.id: self._submit(profile_id, CommandKind.OPEN), "primary").pack(side="left")
            auto_2048_button = self._button(controls, "Auto 2048", lambda profile_id=profile.id: self._toggle_auto_2048(profile_id), "soft")
            auto_2048_button.pack(side="left", padx=(5, 0))
            self._button(controls, "Ảnh", lambda profile_id=profile.id: self._submit(profile_id, CommandKind.SCREENSHOT), "soft").pack(side="left", padx=5)
            inspect_button = self._button(controls, "Đo", lambda profile_id=profile.id: self._toggle_inspector(profile_id), "soft")
            inspect_button.pack(side="left", padx=5)
            self._button(controls, "Xóa", lambda profile_id=profile.id: self._remove_profile(profile_id), "danger").pack(side="right")
            self.rows[profile.id] = ProfileRow(
                profile,
                status,
                resources,
                state_badge,
                inspect_button,
                auto_2048_button,
            )
        profile_ids = [profile.id for profile in self.config.profiles]
        self.master_box.configure(values=profile_ids)
        if profile_ids and self.sync_master.get() not in profile_ids:
            self.sync_master.set(profile_ids[0])

    @staticmethod
    def _state_style(state: WorkerState) -> tuple[str, str, str]:
        if state in {WorkerState.READY, WorkerState.COMPLETED}:
            return ("Sẵn sàng", "#dcfce7", "#15803d")
        if state in {WorkerState.STARTING, WorkerState.RUNNING}:
            return ("Đang chạy", "#dbeafe", "#2563eb")
        if state == WorkerState.ERROR:
            return ("Cần chú ý", "#fee2e2", "#b91c1c")
        return ("Đã dừng", "#f1f5f9", "#475569")

    def _set_row_state(self, row: ProfileRow, state: WorkerState) -> None:
        label, background, foreground = self._state_style(state)
        row.state_badge.configure(text=label, fg_color=background, text_color=foreground)

    def _submit(self, profile_id: str, kind: CommandKind) -> None:
        self.runner.submit(profile_id, kind)

    def _parse_viewport(self) -> tuple[int, int]:
        try:
            width = int(self.viewport_width.get().strip())
            height = int(self.viewport_height.get().strip())
            return validate_viewport(width, height)
        except (TypeError, ValueError) as error:
            raise ValueError(str(error) or "Width/height phải là số nguyên hợp lệ") from error

    def _save_viewport_config(self, width: int, height: int) -> None:
        self.config.browser.viewport_width = width
        self.config.browser.viewport_height = height
        self.config.browser.auto_resize = bool(self.auto_resize.get())
        self.config.browser.low_memory_mode = True
        save_config(self.config)

    def _apply_size_all(self) -> None:
        try:
            width, height = self._parse_viewport()
        except ValueError as error:
            messagebox.showerror("Size không hợp lệ", str(error), parent=self.root)
            return
        self._save_viewport_config(width, height)
        self.runner.resize_all(width, height)
        self._append_log(
            f"Đã lưu viewport {width}×{height} px; đang áp cho tất cả profile đang mở"
        )

    def _apply_size_profile(self, profile_id: str) -> None:
        try:
            width, height = self._parse_viewport()
        except ValueError as error:
            messagebox.showerror("Size không hợp lệ", str(error), parent=self.root)
            return
        self._save_viewport_config(width, height)
        self.runner.submit(profile_id, CommandKind.RESIZE, width=width, height=height)
        self._append_log(f"[{profile_id}] áp viewport {width}×{height} px")

    def _toggle_sync(self) -> None:
        if self.runner.sync_enabled:
            self.runner.disable_sync()
            self.sync_button_text.set("Bật sync chuột")
            self._refresh_sync_button()
            self.sync_status.set("Sync đang tắt")
            self._append_log("Đã tắt đồng bộ chuột")
            return
        master_id = self.sync_master.get()
        if not master_id:
            messagebox.showwarning("Thiếu master", "Hãy chọn profile master.", parent=self.root)
            return
        opened = [
            profile.id
            for profile in self.config.profiles
            if self.runner.has_open_session(profile.id)
        ]
        if master_id not in opened or len(opened) < 2:
            messagebox.showwarning(
                "Chưa đủ profile",
                "Hãy mở profile master và ít nhất một profile follower trước khi bật sync.",
                parent=self.root,
            )
            return
        self.runner.enable_sync(master_id)
        self.sync_button_text.set("Tắt sync chuột")
        self._refresh_sync_button()
        self.sync_status.set(f"MASTER: {master_id} → {len(opened) - 1} follower")
        self._append_log(
            f"Đã bật sync chuột: master={master_id}, followers="
            + ", ".join(item for item in opened if item != master_id)
        )

    def _toggle_inspector(self, profile_id: str) -> None:
        if self.inspecting_profile_id == profile_id:
            self.runner.set_inspector(profile_id, False)
            row = self.rows.get(profile_id)
            if row:
                row.inspect_button.configure(text="Đo")
            self.inspecting_profile_id = None
            self.coordinate_text.set("Đã tắt đo tọa độ")
            return
        if not self.runner.has_open_session(profile_id):
            messagebox.showwarning(
                "Profile chưa mở",
                "Hãy bấm Mở profile trước khi bật đo tọa độ.",
                parent=self.root,
            )
            return
        if self.inspecting_profile_id:
            previous = self.inspecting_profile_id
            self.runner.set_inspector(previous, False)
            previous_row = self.rows.get(previous)
            if previous_row:
                previous_row.inspect_button.configure(text="Đo")
        self.inspecting_profile_id = profile_id
        self.runner.set_inspector(profile_id, True)
        row = self.rows.get(profile_id)
        if row:
            row.inspect_button.configure(text="Tắt đo")
        self.coordinate_text.set(f"[{profile_id}] Click vào vị trí trong game để lấy tọa độ…")
        self._append_log(f"[{profile_id}] đã bật chế độ đo tọa độ; click game sẽ bị chặn")

    def _toggle_auto_2048(self, profile_id: str) -> None:
        row = self.rows.get(profile_id)
        if profile_id in self.auto_2048_profiles:
            self.auto_2048_profiles.discard(profile_id)
            if row:
                row.auto_2048_button.configure(text="Auto 2048")
            self.runner.submit(profile_id, CommandKind.STOP_2048)
            self._append_log(f"[{profile_id}] yêu cầu dừng Auto 2048")
            return
        self.auto_2048_profiles.add(profile_id)
        if row:
            row.auto_2048_button.configure(text="Dừng 2048")
        self.runner.submit(profile_id, CommandKind.START_2048)
        timing = AUTO_2048_TIMINGS[self.config.auto_2048_speed]
        self._append_log(
            f"[{profile_id}] bật Auto 2048 Smart | tốc độ={timing.label}"
        )

    def _apply_auto_2048_speed(self) -> None:
        selected = self.auto_2048_speed_text.get()
        speed = next(
            (
                candidate
                for candidate, label in AUTO_2048_SPEED_LABELS.items()
                if label == selected
            ),
            Auto2048Speed.BALANCED,
        )
        self.runner.set_auto_2048_speed(speed)
        self.config.auto_2048_speed = speed
        save_config(self.config)
        timing = AUTO_2048_TIMINGS[speed]
        self._append_log(
            f"Đã đặt tốc độ Auto 2048: {timing.label} "
            f"(chờ sau vuốt {timing.move_delay_seconds:.2f}s)"
        )

    def _trim_ram(self) -> None:
        try:
            process_count = self.runner.trim_all_profile_memory()
        except Exception as error:
            messagebox.showerror("Không tối ưu được RAM", str(error), parent=self.root)
            return
        self._last_resource_poll = 0.0
        self._append_log(
            f"Đã yêu cầu Windows trim working set của {process_count} process Chrome. "
            "RAM có thể tăng lại khi game tải dữ liệu đang cần."
        )

    def _refresh_resource_overview(self) -> None:
        overview = self.runner.resource_overview()
        self.total_profiles_text.set(str(overview.total_profiles))
        self.open_profiles_text.set(str(overview.opened_profiles))
        self.ram_usage_text.set(f"{overview.ram_bytes / 1_048_576:.0f} MB")
        self.cpu_usage_text.set(f"{overview.cpu_percent:.1f}%")
        for profile in overview.profiles:
            row = self.rows.get(profile.profile_id)
            if row is None:
                continue
            if not profile.opened:
                row.resources.set("—")
                continue
            row.resources.set(
                f"{profile.ram_bytes / 1_048_576:.0f} MB | "
                f"{profile.cpu_percent:.1f}%"
            )

    def _toggle_all_drag_items(self) -> None:
        self.drag_items_visible = not self.drag_items_visible
        opened = self.runner.set_all_drag_items_visible(self.drag_items_visible)
        self.drag_button_text.set(
            "Ẩn drag tất cả" if self.drag_items_visible else "Hiện drag tất cả"
        )
        self.drag_button.configure(text=self.drag_button_text.get())
        action = "hiện" if self.drag_items_visible else "ẩn"
        self._append_log(
            f"Đã {action} HTML button #drag-item trên {opened} profile đang mở"
        )

    def _toggle_pin_windows(self) -> None:
        enabled = bool(self.pin_windows.get())
        opened = self.runner.set_all_topmost(enabled)
        action = "ghim" if enabled else "bỏ ghim"
        self._append_log(f"Đã {action} {opened} cửa sổ profile")

    def _arrange_windows(self) -> None:
        try:
            count = self.runner.arrange_windows()
        except Exception as error:
            messagebox.showerror("Không sắp xếp được", str(error), parent=self.root)
            return
        if count == 0:
            messagebox.showwarning(
                "Chưa có cửa sổ",
                "Hãy mở ít nhất một profile trước khi sắp xếp.",
                parent=self.root,
            )
            return
        self._append_log(f"Đang sắp xếp {count} cửa sổ từ trái sang phải, trên xuống dưới")

    def _copy_coordinate_xy(self) -> None:
        if self.last_coordinate is None:
            return
        _profile_id, event = self.last_coordinate
        canvas = event.get("canvas")
        if isinstance(canvas, dict):
            value = f"{canvas.get('pixel_x_rounded')},{canvas.get('pixel_y_rounded')}"
        else:
            viewport = event.get("viewport", {})
            value = f"{viewport.get('x')},{viewport.get('y')}"
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._append_log(f"Đã copy tọa độ: {value}")

    def _copy_coordinate_json(self) -> None:
        if self.last_coordinate is None:
            return
        profile_id, event = self.last_coordinate
        value = json.dumps(
            {"profile_id": profile_id, **event},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._append_log("Đã copy JSON tọa độ")

    def _add_profile(self) -> None:
        name = simpledialog.askstring("Thêm Chrome profile", "Tên tài khoản/profile:", parent=self.root)
        if not name:
            return
        profile_id = unique_profile_id(name, {profile.id for profile in self.config.profiles})
        profile = ProfileConfig(
            id=profile_id,
            name=name.strip(),
            mode=ProfileMode.MANAGED,
            user_data_dir=(self.config.data_dir / "profiles" / profile_id).resolve(),
            enabled=True,
        )
        self.config.profiles.append(profile)
        save_config(self.config)
        profile.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.runner.sync_profiles()
        self._draw_rows()
        self._append_log(f"Đã thêm profile {name} ({profile_id})")

    def _remove_profile(self, profile_id: str) -> None:
        profile = self.config.profile(profile_id)
        confirmed = messagebox.askyesno(
            "Bỏ profile",
            (
                f"Bỏ '{profile.name}' khỏi dashboard?\n\n"
                "Thư mục cookie/cache vẫn được giữ lại và không bị xóa."
            ),
            parent=self.root,
        )
        if not confirmed:
            return
        if self.runner.sync_master_id == profile_id:
            self.runner.disable_sync()
            self.sync_button_text.set("Bật sync chuột")
            self._refresh_sync_button()
            self.sync_status.set("Sync đang tắt")
        if self.inspecting_profile_id == profile_id:
            self.runner.set_inspector(profile_id, False)
            self.inspecting_profile_id = None
        worker = self.runner.workers.get(profile_id)
        if worker:
            worker.shutdown()
        self.config.profiles = [item for item in self.config.profiles if item.id != profile_id]
        save_config(self.config)
        self.runner.sync_profiles()
        self._draw_rows()
        self._append_log(f"Đã bỏ profile {profile.name}; dữ liệu trên đĩa vẫn còn")

    def _poll_updates(self) -> None:
        try:
            while True:
                snapshot = self.updates.get_nowait()
                row = self.rows.get(snapshot.profile_id)
                if row:
                    row.status.set(snapshot.message)
                    self._set_row_state(row, snapshot.state)
                if snapshot.state == WorkerState.STOPPED:
                    if self.inspecting_profile_id == snapshot.profile_id:
                        self.inspecting_profile_id = None
                        if row:
                            row.inspect_button.configure(text="Đo")
                    if self.runner.sync_master_id == snapshot.profile_id:
                        self.runner.disable_sync()
                        self.sync_button_text.set("Bật sync chuột")
                        self._refresh_sync_button()
                        self.sync_status.set("Sync đang tắt")
                auto_finished = (
                    snapshot.state == WorkerState.STOPPED
                    or (
                        "2048" in snapshot.message
                        and snapshot.state in {
                            WorkerState.READY,
                            WorkerState.COMPLETED,
                            WorkerState.ERROR,
                        }
                    )
                )
                if auto_finished:
                    self.auto_2048_profiles.discard(snapshot.profile_id)
                    if row:
                        row.auto_2048_button.configure(text="Auto 2048")
                detail = f" — {snapshot.detail}" if snapshot.detail else ""
                self._append_log(
                    f"[{snapshot.profile_id}] {snapshot.state.value}: {snapshot.message}{detail}"
                )
        except queue.Empty:
            pass
        try:
            while True:
                profile_id, event = self.coordinate_updates.get_nowait()
                self.last_coordinate = (profile_id, event)
                summary = format_coordinate(profile_id, event)
                self.coordinate_text.set(summary)
                element = event.get("element", {})
                selector = element.get("selector", "") if isinstance(element, dict) else ""
                suffix = f" | selector={selector}" if selector else ""
                self._append_log(f"TỌA ĐỘ: {summary}{suffix}")
        except queue.Empty:
            pass
        now = time.monotonic()
        if now - self._last_resource_poll >= 2.0:
            try:
                self._refresh_resource_overview()
            except Exception as error:
                self._append_log(f"Không đo được tài nguyên Chrome: {error}")
            self._last_resource_poll = now
        if now - self._last_ram_trim >= 60.0:
            try:
                self.runner.trim_all_profile_memory()
            except Exception as error:
                self._append_log(f"Auto tối ưu RAM tạm lỗi: {error}")
            self._last_ram_trim = now
        self.root.after(200, self._poll_updates)

    def _append_log(self, message: str) -> None:
        self._log_lines.append(message)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(self._log_lines) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        if self.inspecting_profile_id:
            self.runner.set_inspector(self.inspecting_profile_id, False)
        self.runner.shutdown()
        self.root.destroy()


def run_dashboard(config_path: Path) -> None:
    root = ctk.CTk()
    Dashboard(root, config_path)
    root.mainloop()
