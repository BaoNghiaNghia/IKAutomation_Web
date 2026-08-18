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
        self.root.title("IK Auto — Multi Profile")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = max(920, round(screen_width * 0.86))
        height = max(620, round(screen_height * 0.82))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(900, 580)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        # Keep the original light/system appearance.  It is easier to scan
        # across many profiles and follows the user's Windows theme.
        style = ttk.Style(self.root)
        style.configure("Card.TFrame", relief="solid", borderwidth=1)
        style.configure("CardValue.TLabel", font=("Segoe UI", 15, "bold"))

        self.page_canvas = tk.Canvas(self.root, highlightthickness=0, borderwidth=0)
        page_scrollbar = ttk.Scrollbar(
            self.root,
            orient="vertical",
            command=self.page_canvas.yview,
        )
        self.page_canvas.configure(yscrollcommand=page_scrollbar.set)
        page_scrollbar.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.page_canvas)
        self._content_window = self.page_canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self.content.bind("<Configure>", self._update_page_scrollregion)
        self.page_canvas.bind("<Configure>", self._resize_page_content)
        self.root.bind_all("<MouseWheel>", self._scroll_dashboard, add="+")
        parent = self.content

        header = ttk.Frame(parent, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="IK AUTO", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="Add profile", command=self._add_profile).pack(side="right")

        overview = ttk.Frame(parent, padding=(12, 0, 12, 10))
        overview.pack(fill="x")
        cards = (
            ("Tổng profile", self.total_profiles_text),
            ("Đang mở", self.open_profiles_text),
            ("RAM Chrome (Auto)", self.ram_usage_text),
            ("CPU Chrome", self.cpu_usage_text),
        )
        for index, (title, variable) in enumerate(cards):
            card = ttk.Frame(overview, padding=(14, 8), style="Card.TFrame")
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(
                card,
                textvariable=variable,
                style="CardValue.TLabel",
                font=("Segoe UI", 15, "bold"),
            ).pack(anchor="w")
            overview.columnconfigure(index, weight=1)
        toolbar = ttk.Frame(parent, padding=(12, 0, 12, 10))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Open all", command=self.runner.open_all).pack(side="left")
        # ttk.Button(toolbar, text="Đọc tất cả", command=self.runner.read_all).pack(
        #     side="left", padx=6
        # )
        ttk.Button(toolbar, text="Close all", command=self.runner.stop_all).pack(side="left")

        automation_toolbar = ttk.Frame(parent, padding=(12, 0, 12, 10))
        automation_toolbar.pack(fill="x")
        ttk.Label(automation_toolbar, text="Game viewport:").pack(side="left")
        ttk.Entry(automation_toolbar, textvariable=self.viewport_width, width=7).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(automation_toolbar, text="×").pack(side="left")
        ttk.Entry(automation_toolbar, textvariable=self.viewport_height, width=7).pack(
            side="left", padx=(2, 6)
        )
        ttk.Label(automation_toolbar, text="px").pack(side="left")
        ttk.Checkbutton(
            automation_toolbar,
            text="Auto resize khi mở",
            variable=self.auto_resize,
        ).pack(side="left", padx=10)
        ttk.Button(
            automation_toolbar,
            text="Lưu + fix size tất cả",
            command=self._apply_size_all,
        ).pack(side="left")
        ttk.Separator(automation_toolbar, orient="vertical").pack(
            side="left", fill="y", padx=14
        )
        ttk.Label(automation_toolbar, text="Profile master:").pack(side="left")
        self.master_box = ttk.Combobox(
            automation_toolbar,
            textvariable=self.sync_master,
            state="readonly",
            width=15,
        )
        self.master_box.pack(side="left", padx=6)
        ttk.Button(
            automation_toolbar,
            textvariable=self.sync_button_text,
            command=self._toggle_sync,
        ).pack(side="left")
        ttk.Label(
            automation_toolbar,
            textvariable=self.sync_status,
            foreground="#1f5f99",
        ).pack(side="left", padx=10)

        window_toolbar = ttk.Frame(parent, padding=(12, 0, 12, 10))
        window_toolbar.pack(fill="x")
        ttk.Button(
            window_toolbar,
            text="Sắp xếp cửa sổ",
            command=self._arrange_windows,
        ).pack(side="left")
        ttk.Checkbutton(
            window_toolbar,
            text="Pin",
            variable=self.pin_windows,
            command=self._toggle_pin_windows,
        ).pack(side="left", padx=10)
        ttk.Button(
            window_toolbar,
            textvariable=self.drag_button_text,
            command=self._toggle_all_drag_items,
        ).pack(side="left")
        ttk.Separator(window_toolbar, orient="vertical").pack(
            side="left", fill="y", padx=14
        )
        ttk.Label(window_toolbar, text="Tốc độ 2048:").pack(side="left")
        self.auto_2048_speed_box = ttk.Combobox(
            window_toolbar,
            textvariable=self.auto_2048_speed_text,
            values=tuple(AUTO_2048_SPEED_LABELS.values()),
            state="readonly",
            width=18,
        )
        self.auto_2048_speed_box.pack(side="left", padx=6)
        ttk.Button(
            window_toolbar,
            text="Áp dụng",
            command=self._apply_auto_2048_speed,
        ).pack(side="left")

        table_container = ttk.Frame(parent, padding=(12, 0, 12, 8))
        table_container.pack(fill="x")
        self.table = table_container
        headers = (
            ("Profile", 24),
            ("Mode", 10),
            ("Status", 42),
            ("Tài nguyên", 20),
            ("Controls", 48),
        )
        for column, (label, width) in enumerate(headers):
            ttk.Label(
                table_container,
                text=label,
                width=width,
                font=("Segoe UI", 9, "bold"),
            ).grid(row=0, column=column, sticky="w", padx=3, pady=(0, 6))
        table_container.columnconfigure(2, weight=1)

        coordinate_frame = ttk.LabelFrame(parent, text="Lấy tọa độ dev", padding=8)
        coordinate_frame.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Label(
            coordinate_frame,
            textvariable=self.coordinate_text,
            font=("Consolas", 10, "bold"),
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(coordinate_frame, text="Copy x,y", command=self._copy_coordinate_xy).pack(
            side="right"
        )
        ttk.Button(
            coordinate_frame,
            text="Copy JSON",
            command=self._copy_coordinate_json,
        ).pack(side="right", padx=6)

        log_frame = ttk.LabelFrame(parent, text="Nhật ký dashboard — 10 dòng gần nhất", padding=8)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log = ScrolledText(log_frame, height=16, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def _update_page_scrollregion(self, _event: tk.Event[tk.Misc]) -> None:
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _resize_page_content(self, event: tk.Event[tk.Misc]) -> None:
        self.page_canvas.itemconfigure(self._content_window, width=event.width)

    def _scroll_dashboard(self, event: tk.Event[tk.Misc]) -> str | None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is not None and widget.winfo_class() in {"Text", "TCombobox"}:
            return None
        delta = int(-event.delta / 120) if event.delta else 0
        if delta:
            self.page_canvas.yview_scroll(delta, "units")
            return "break"
        return None

    def _draw_rows(self) -> None:
        for child in self.table.grid_slaves():
            if int(child.grid_info()["row"]) > 0:
                child.destroy()
        self.rows.clear()
        for row_index, profile in enumerate(self.config.profiles, start=1):
            status = tk.StringVar(value="Đã dừng")
            resources = tk.StringVar(value="—")
            ttk.Label(self.table, text=f"{profile.name}\n{profile.id}", width=24).grid(
                row=row_index, column=0, sticky="w", padx=3, pady=5
            )
            ttk.Label(self.table, text=profile.mode.value, width=10).grid(
                row=row_index, column=1, sticky="w", padx=3
            )
            ttk.Label(self.table, textvariable=status, width=42).grid(
                row=row_index, column=2, sticky="ew", padx=3
            )
            ttk.Label(self.table, textvariable=resources, width=20).grid(
                row=row_index, column=3, sticky="w", padx=3
            )
            controls = ttk.Frame(self.table)
            controls.grid(row=row_index, column=4, sticky="w", padx=3)
            ttk.Button(
                controls,
                text="Mở",
                command=lambda profile_id=profile.id: self._submit(profile_id, CommandKind.OPEN),
                width=6,
            ).pack(side="left")
            # ttk.Button(
            #     controls,
            #     text="Đọc data",
            #     command=lambda profile_id=profile.id: self._submit(profile_id, CommandKind.READ),
            #     width=8,
            # ).pack(side="left", padx=3)
            auto_2048_button = ttk.Button(
                controls,
                text="Auto 2048",
                command=lambda profile_id=profile.id: self._toggle_auto_2048(profile_id),
                width=11,
            )
            auto_2048_button.pack(side="left", padx=(3, 0))
            ttk.Button(
                controls,
                text="Screen shot",
                command=lambda profile_id=profile.id: self._submit(
                    profile_id, CommandKind.SCREENSHOT
                ),
                width=12,
            ).pack(side="left", padx=3)
            inspect_button = ttk.Button(
                controls,
                text="Check",
                command=lambda profile_id=profile.id: self._toggle_inspector(profile_id),
                width=6,
            )
            inspect_button.pack(side="left", padx=3)
            ttk.Button(
                controls,
                text="Delete",
                command=lambda profile_id=profile.id: self._remove_profile(profile_id),
                width=7,
            ).pack(side="left", padx=(3, 0))
            self.rows[profile.id] = ProfileRow(
                profile,
                status,
                resources,
                inspect_button,
                auto_2048_button,
            )
        profile_ids = [profile.id for profile in self.config.profiles]
        self.master_box.configure(values=profile_ids)
        if profile_ids and self.sync_master.get() not in profile_ids:
            self.sync_master.set(profile_ids[0])

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
