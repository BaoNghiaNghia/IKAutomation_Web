from __future__ import annotations

import json
import queue
import tkinter as tk
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

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
    inspect_button: ttk.Button
    auto_2048_button: ttk.Button


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
        self.table = ttk.Frame(root)
        self.log: ScrolledText
        self._log_lines: deque[str] = deque(maxlen=10)
        self._build()
        self._draw_rows()
        self.root.after(200, self._poll_updates)

    def _build(self) -> None:
        self.root.title("IK Auto — Browser Control")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = max(1040, round(screen_width * 0.72))
        height = max(700, round(screen_height * 0.78))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(1040, 680)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        style = ttk.Style(self.root)
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(11, 6))
        style.configure("Danger.TButton", font=("Segoe UI", 9, "bold"), padding=(11, 6))
        style.configure("Compact.TButton", font=("Segoe UI", 9), padding=(7, 4))
        style.configure("CardTitle.TLabel", foreground="#64748b", font=("Segoe UI", 9))
        style.configure("CardValue.TLabel", foreground="#0f172a", font=("Segoe UI", 15, "bold"))
        self.root.configure(bg="#f5f7fb")

        parent = tk.Frame(self.root, bg="#f5f7fb", padx=18, pady=14)
        parent.pack(fill="both", expand=True)
        header = tk.Frame(parent, bg="#f5f7fb")
        header.pack(fill="x", pady=(0, 12))
        tk.Label(
            header,
            text="Điều khiển browser Infinity Kingdom",
            bg="#f5f7fb",
            fg="#0f172a",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="Chrome profiles · local only",
            bg="#e0ecff",
            fg="#2563eb",
            padx=10,
            pady=5,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right")

        body = tk.Frame(parent, bg="#f5f7fb")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0, minsize=340)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left_panel = tk.Frame(body, bg="#f5f7fb")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right_panel = tk.Frame(body, bg="#f5f7fb")
        right_panel.grid(row=0, column=1, sticky="nsew")
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(1, weight=1)

        profiles_card = self._card(left_panel)
        profiles_card.pack(fill="x", pady=(0, 10))
        profile_head = tk.Frame(profiles_card, bg="#ffffff")
        profile_head.pack(fill="x")
        tk.Label(profile_head, text="Chrome profiles", bg="#ffffff", fg="#0f172a", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(profile_head, text="+ Thêm", command=self._add_profile, style="Accent.TButton").pack(side="right")
        tk.Label(profiles_card, text="Mỗi profile là một phiên browser độc lập.", bg="#ffffff", fg="#64748b", font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 9))
        action_row = tk.Frame(profiles_card, bg="#ffffff")
        action_row.pack(fill="x")
        ttk.Button(action_row, text="Mở tất cả", command=self.runner.open_all, style="Accent.TButton").pack(side="left", fill="x", expand=True)
        ttk.Button(action_row, text="Dừng tất cả", command=self.runner.stop_all, style="Danger.TButton").pack(side="left", fill="x", expand=True, padx=(8, 0))

        viewport_card = self._card(left_panel)
        viewport_card.pack(fill="x", pady=(0, 10))
        self._section_label(viewport_card, "Cấu hình browser", "Kích thước game được áp khi mở profile.")
        viewport_row = tk.Frame(viewport_card, bg="#ffffff")
        viewport_row.pack(fill="x", pady=(8, 7))
        ttk.Entry(viewport_row, textvariable=self.viewport_width, width=7).pack(side="left")
        tk.Label(viewport_row, text="×", bg="#ffffff", fg="#64748b").pack(side="left", padx=5)
        ttk.Entry(viewport_row, textvariable=self.viewport_height, width=7).pack(side="left")
        tk.Label(viewport_row, text="px", bg="#ffffff", fg="#64748b").pack(side="left", padx=5)
        ttk.Button(viewport_row, text="Áp dụng", command=self._apply_size_all, style="Compact.TButton").pack(side="right")
        ttk.Checkbutton(viewport_card, text="Tự resize khi mở", variable=self.auto_resize).pack(anchor="w")
        ttk.Button(viewport_card, text="Sắp xếp cửa sổ", command=self._arrange_windows, style="Compact.TButton").pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(viewport_card, text="Ghim cửa sổ lên trên", variable=self.pin_windows, command=self._toggle_pin_windows).pack(anchor="w", pady=(5, 0))
        ttk.Button(viewport_card, textvariable=self.drag_button_text, command=self._toggle_all_drag_items, style="Compact.TButton").pack(anchor="w", pady=(5, 0))

        automation_card = self._card(left_panel)
        automation_card.pack(fill="x")
        self._section_label(automation_card, "Tự động hóa", "Đồng bộ thao tác và Auto 2048.")
        master_row = tk.Frame(automation_card, bg="#ffffff")
        master_row.pack(fill="x", pady=(8, 5))
        self.master_box = ttk.Combobox(master_row, textvariable=self.sync_master, state="readonly", width=16)
        self.master_box.pack(side="left", fill="x", expand=True)
        ttk.Button(master_row, textvariable=self.sync_button_text, command=self._toggle_sync, style="Compact.TButton").pack(side="left", padx=(6, 0))
        tk.Label(automation_card, textvariable=self.sync_status, bg="#ffffff", fg="#2563eb", font=("Segoe UI", 9)).pack(anchor="w")
        ttk.Separator(automation_card, orient="horizontal").pack(fill="x", pady=9)
        tk.Label(automation_card, text="Tốc độ Auto 2048", bg="#ffffff", fg="#475569", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        speed_row = tk.Frame(automation_card, bg="#ffffff")
        speed_row.pack(fill="x", pady=(5, 0))
        self.auto_2048_speed_box = ttk.Combobox(speed_row, textvariable=self.auto_2048_speed_text, values=tuple(AUTO_2048_SPEED_LABELS.values()), state="readonly", width=18)
        self.auto_2048_speed_box.pack(side="left", fill="x", expand=True)
        ttk.Button(speed_row, text="Lưu", command=self._apply_auto_2048_speed, style="Compact.TButton").pack(side="left", padx=(6, 0))

        overview = self._card(right_panel)
        overview.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        cards = (
            ("Tổng profile", self.total_profiles_text),
            ("Đang mở", self.open_profiles_text),
            ("RAM Chrome (Auto)", self.ram_usage_text),
            ("CPU Chrome", self.cpu_usage_text),
        )
        for index, (title, variable) in enumerate(cards):
            card = tk.Frame(overview, bg="#ffffff", padx=12, pady=8)
            card.grid(row=0, column=index, sticky="ew")
            tk.Label(card, text=title, bg="#ffffff", fg="#64748b", font=("Segoe UI", 9)).pack(anchor="w")
            tk.Label(card, textvariable=variable, bg="#ffffff", fg="#0f172a", font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(2, 0))
            overview.columnconfigure(index, weight=1)
        progress_card = self._card(right_panel)
        progress_card.grid(row=1, column=0, sticky="nsew")
        progress_head = tk.Frame(progress_card, bg="#ffffff")
        progress_head.pack(fill="x", pady=(0, 8))
        tk.Label(progress_head, text="Tiến trình profile", bg="#ffffff", fg="#0f172a", font=("Segoe UI", 11, "bold")).pack(side="left")
        tk.Label(progress_head, textvariable=self.open_profiles_text, bg="#eef6ff", fg="#2563eb", font=("Segoe UI", 9, "bold"), padx=9, pady=3).pack(side="right")
        profile_list = tk.Frame(progress_card, bg="#ffffff")
        profile_list.pack(fill="both", expand=True)
        self.profile_canvas = tk.Canvas(profile_list, bg="#ffffff", highlightthickness=0)
        profile_scrollbar = ttk.Scrollbar(
            profile_list, orient="vertical", command=self.profile_canvas.yview
        )
        self.profile_canvas.configure(yscrollcommand=profile_scrollbar.set)
        profile_scrollbar.pack(side="right", fill="y")
        self.profile_canvas.pack(side="left", fill="both", expand=True)
        self.table = tk.Frame(self.profile_canvas, bg="#ffffff")
        self._profile_table_window = self.profile_canvas.create_window(
            (0, 0), window=self.table, anchor="nw"
        )
        self.table.bind("<Configure>", self._update_profile_scrollregion)
        self.profile_canvas.bind("<Configure>", self._resize_profile_table)
        self.profile_canvas.bind("<MouseWheel>", self._scroll_profile_list)

        bottom = tk.Frame(right_panel, bg="#f5f7fb")
        bottom.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        coordinate_frame = self._card(bottom)
        coordinate_frame.pack(fill="x", pady=(0, 10))
        coord_head = tk.Frame(coordinate_frame, bg="#ffffff")
        coord_head.pack(fill="x")
        tk.Label(coord_head, text="Lấy tọa độ", bg="#ffffff", fg="#0f172a", font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(coord_head, text="Copy x,y", command=self._copy_coordinate_xy, style="Compact.TButton").pack(side="right")
        ttk.Button(coord_head, text="Copy JSON", command=self._copy_coordinate_json, style="Compact.TButton").pack(side="right", padx=5)
        tk.Label(coordinate_frame, textvariable=self.coordinate_text, bg="#ffffff", fg="#475569", font=("Consolas", 9), anchor="w", justify="left", wraplength=700).pack(fill="x", pady=(5, 0))
        log_frame = self._card(bottom)
        log_frame.pack(fill="x")
        tk.Label(log_frame, text="Nhật ký gần nhất", bg="#ffffff", fg="#0f172a", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
        self.log = ScrolledText(log_frame, height=6, state="disabled", font=("Consolas", 8), relief="flat", bg="#f8fafc", fg="#334155", padx=8, pady=6)
        self.log.pack(fill="x")

    @staticmethod
    def _card(parent: tk.Misc) -> tk.Frame:
        return tk.Frame(parent, bg="#ffffff", highlightbackground="#e2e8f0", highlightthickness=1, padx=14, pady=12)

    @staticmethod
    def _section_label(parent: tk.Misc, title: str, description: str) -> None:
        tk.Label(parent, text=title, bg="#ffffff", fg="#0f172a", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(parent, text=description, bg="#ffffff", fg="#64748b", font=("Segoe UI", 9), wraplength=290, justify="left").pack(anchor="w", pady=(3, 0))

    def _update_profile_scrollregion(self, _event: tk.Event[tk.Misc]) -> None:
        self.profile_canvas.configure(scrollregion=self.profile_canvas.bbox("all"))

    def _resize_profile_table(self, event: tk.Event[tk.Misc]) -> None:
        self.profile_canvas.itemconfigure(self._profile_table_window, width=event.width)

    def _scroll_profile_list(self, event: tk.Event[tk.Misc]) -> str | None:
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self.profile_canvas.yview_scroll(delta, "units")
            return "break"
        return None

    def _draw_rows(self) -> None:
        for child in self.table.winfo_children():
            child.destroy()
        self.rows.clear()
        for profile in self.config.profiles:
            status = tk.StringVar(value="Đã dừng")
            resources = tk.StringVar(value="—")
            item = tk.Frame(
                self.table,
                bg="#ffffff",
                highlightbackground="#e2e8f0",
                highlightthickness=1,
                padx=11,
                pady=8,
            )
            item.pack(fill="x", pady=(0, 7))
            top = tk.Frame(item, bg="#ffffff")
            top.pack(fill="x")
            identity = tk.Frame(top, bg="#ffffff")
            identity.pack(side="left", fill="x", expand=True)
            tk.Label(identity, text=profile.name, bg="#ffffff", fg="#0f172a", font=("Segoe UI", 10, "bold")).pack(side="left")
            tk.Label(identity, text=f"  {profile.id} · {profile.mode.value}", bg="#ffffff", fg="#64748b", font=("Segoe UI", 8)).pack(side="left")
            state_badge = tk.Label(top, text="Đã dừng", bg="#f1f5f9", fg="#475569", font=("Segoe UI", 8, "bold"), padx=8, pady=3)
            state_badge.pack(side="right")
            detail = tk.Frame(item, bg="#ffffff")
            detail.pack(fill="x", pady=(5, 7))
            tk.Label(detail, textvariable=status, bg="#ffffff", fg="#475569", font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(detail, textvariable=resources, bg="#ffffff", fg="#64748b", font=("Segoe UI", 8)).pack(side="right")
            controls = tk.Frame(item, bg="#ffffff")
            controls.pack(fill="x")
            ttk.Button(
                controls,
                text="Mở",
                command=lambda profile_id=profile.id: self._submit(profile_id, CommandKind.OPEN),
                style="Compact.TButton",
            ).pack(side="left")
            auto_2048_button = ttk.Button(
                controls,
                text="Auto 2048",
                command=lambda profile_id=profile.id: self._toggle_auto_2048(profile_id),
                style="Compact.TButton",
            )
            auto_2048_button.pack(side="left", padx=(5, 0))
            ttk.Button(
                controls,
                text="Ảnh",
                command=lambda profile_id=profile.id: self._submit(
                    profile_id, CommandKind.SCREENSHOT
                ),
                style="Compact.TButton",
            ).pack(side="left", padx=5)
            inspect_button = ttk.Button(
                controls,
                text="Đo",
                command=lambda profile_id=profile.id: self._toggle_inspector(profile_id),
                style="Compact.TButton",
            )
            inspect_button.pack(side="left", padx=5)
            ttk.Button(
                controls,
                text="Xóa",
                command=lambda profile_id=profile.id: self._remove_profile(profile_id),
                style="Compact.TButton",
            ).pack(side="right")
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
        row.state_badge.configure(text=label, bg=background, fg=foreground)

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
    root = tk.Tk()
    Dashboard(root, config_path)
    root.mainloop()
