"""Modern Fluent desktop dashboard."""
from __future__ import annotations

import json
import os
import queue
import time
from io import BytesIO
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QColor, QCursor, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, CheckBox, ComboBox, FluentIcon as FIF, LineEdit, PasswordLineEdit, PrimaryPushButton, PrimaryToolButton, PushButton, StrongBodyLabel, SubtitleLabel, ToolButton

from ik_chrome_auto.config import ensure_data_dirs, load_config, save_config, unique_profile_id
from ik_chrome_auto.farm_launch_policy import FarmLaunchPolicy
from ik_chrome_auto.interaction import format_coordinate
from ik_chrome_auto.models import CommandKind, ProfileConfig, ProfileMode, WorkerSnapshot, WorkerState
from ik_chrome_auto.runner import MultiProfileRunner
from ik_chrome_auto.two_factor import TwoFactorEnrollment, TwoFactorService
from ik_chrome_auto.windows import get_system_memory_status

# Windows Hello implementation remains available in windows_auth.py, but is
# intentionally disabled while Google Authenticator is the primary verifier.
WINDOWS_HELLO_ENABLED = False

# Scale only the desktop controller. Chrome/game windows use their own native
# geometry and screenshot resolution and must not inherit this factor.
TOOL_UI_SCALE = 0.805


def _ui_px(value: int | float, minimum: int = 1) -> int:
    """Convert a design pixel value to the compact tool UI scale."""
    return max(minimum, round(value * TOOL_UI_SCALE))


def _ui_pt(value: int | float, minimum: float = 5.0) -> float:
    """Convert a design point size to the compact tool UI scale."""
    return max(minimum, round(value * TOOL_UI_SCALE, 2))


class FullRowCheckBox(CheckBox):
    """Toggle when clicking anywhere inside the checkbox widget bounds."""

    def hitButton(self, position) -> bool:  # noqa: N802 - Qt virtual name
        return self.rect().contains(position)


_launch_terminal_window = 0


def capture_launch_terminal_window() -> None:
    """Remember the foreground terminal before the dashboard takes focus."""
    global _launch_terminal_window
    if os.name != "nt" or os.environ.get("IK_AUTO_MINIMIZE_CONSOLE") != "1":
        return
    try:
        import ctypes
        candidate = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        class_name = ctypes.create_unicode_buffer(128)
        ctypes.windll.user32.GetClassNameW(candidate, class_name, len(class_name))
        if class_name.value in {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}:
            _launch_terminal_window = candidate
        else:
            _launch_terminal_window = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except OSError:
        _launch_terminal_window = 0


def minimize_launch_console() -> None:
    if os.name != "nt" or os.environ.get("IK_AUTO_MINIMIZE_CONSOLE") != "1":
        return
    try:
        import ctypes
        window = _launch_terminal_window or ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 6)
    except OSError:
        pass


@dataclass(slots=True)
class ProfileRow:
    status: QLabel
    resource: QLabel
    badge: QLabel
    farm: PushButton
    card: CardWidget


class Dashboard(QWidget):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config = load_config(config_path)
        ensure_data_dirs(self.config)
        self.updates: queue.Queue[WorkerSnapshot] = queue.Queue()
        self.coordinate_updates: queue.Queue[tuple[str, dict[str, object]]] = queue.Queue()
        self.runner = MultiProfileRunner(self.config, self.updates.put, on_coordinate=lambda pid, event: self.coordinate_updates.put((pid, event)))
        self.rows: dict[str, ProfileRow] = {}
        self._log_lines: deque[str] = deque(maxlen=10)
        self.last_coordinate: tuple[str, dict[str, object]] | None = None
        self.farm_profiles: set[str] = set()
        self._sync_target_profiles: set[str] = set()
        self._farm_launch_profiles: set[str] = set()
        self._farm_launcher_phase = "launch"
        self._farm_all_running = False
        self._farm_open_states: dict[str, WorkerState] = {}
        self._farm_open_queue: deque[str] = deque()
        self._farm_next_open_at = 0.0
        self._farm_open_deadline = 0.0
        self._farm_launch_policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
        self._farm_batch_profiles: set[str] = set()
        self._farm_batch_submitted = 0
        self._farm_batch_resume_at = 0.0
        self._farm_resource_pause_started = 0.0
        self._farm_resource_pause_reason: str | None = None
        self._latest_profile_cpu_percent = 0.0
        self._farm_close_queue: deque[str] = deque()
        self._farm_close_in_flight: str | None = None
        self._farm_close_deadline = 0.0
        self._auto_arrange_targets: set[str] | None = None
        self._auto_arrange_states: dict[str, WorkerState] = {}
        self._auto_arrange_deadline = 0.0
        self.drag_visible = False
        self.scrollbars_visible = False
        self._responsive_icon_buttons: list[ToolButton] = []
        self._responsive_profile_buttons: list[PushButton] = []
        self._overview_typography: list[tuple[QLabel, QLabel]] = []
        self._last_resources = self._last_trim = 0.0
        self._build()
        self._draw_rows()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    def _build(self) -> None:
        self.setWindowTitle("IK Auto — Browser Control")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "ik_auto.ico")))
        active_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if active_screen is None:
            raise RuntimeError("Không tìm thấy màn hình để hiển thị dashboard")
        self._apply_responsive_geometry(active_screen)
        root = QVBoxLayout(self); root.setContentsMargins(*self._responsive_margins); root.setSpacing(self._responsive_spacing)
        self._root_layout = root
        head = QHBoxLayout(); title = SubtitleLabel("IK Auto"); self._dashboard_title = title; title.setStyleSheet(f"font-size:{self._responsive_title_font_pt}pt;font-weight:700;"); head.addWidget(title); head.addWidget(QLabel("Browser control")); head.addStretch(); secure = QLabel("●  Local & secure"); self._secure_badge = secure; secure.setStyleSheet(f"background:#d9f7e8;color:#087443;border-radius:{_ui_px(12)}px;padding:{_ui_px(5)}px {_ui_px(10)}px;font-weight:600;"); head.addWidget(secure); root.addLayout(head)
        body = QHBoxLayout(); root.addLayout(body, 1)
        left = QWidget(); self._left_sidebar = left; left.setMinimumWidth(self._responsive_sidebar_min); left.setMaximumWidth(self._responsive_sidebar_max); ll = QVBoxLayout(left); self._left_layout = ll; ll.setContentsMargins(0,0,0,0); ll.setSpacing(self._responsive_spacing); body.addWidget(left)
        overview = self._card(); overview.setMaximumHeight(_ui_px(108)); ol = QGridLayout(overview); ol.setContentsMargins(_ui_px(12),_ui_px(10),_ui_px(12),_ui_px(10)); ol.setHorizontalSpacing(_ui_px(12)); ol.setVerticalSpacing(_ui_px(6))
        self.total, self.opened, self.ram, self.cpu = QLabel("0"), QLabel("0"), QLabel("0 MB"), QLabel("0.0%")
        for index, (label, value) in enumerate((("Tổng profile",self.total),("Đang mở",self.opened),("RAM Chrome",self.ram),("CPU Chrome",self.cpu))):
            cell = QWidget(); cl = QVBoxLayout(cell); cl.setContentsMargins(0,0,0,0); cl.setSpacing(1); caption = self._muted(label); caption.setStyleSheet(f"color:#62758e;background:transparent;font-size:{self._responsive_caption_font_pt}pt;"); value.setStyleSheet(f"font-size:{self._responsive_value_font_pt}pt;font-weight:700;"); self._overview_typography.append((caption, value)); cl.addWidget(caption); cl.addWidget(value); ol.addWidget(cell, index // 2, index % 2)
        ll.addWidget(overview)
        accounts = self._card(); al = QVBoxLayout(accounts); al.addWidget(StrongBodyLabel("Tài khoản Chrome")); al.addWidget(self._muted("Mỗi tài khoản có một phiên browser riêng, lưu cục bộ."))
        manage = PushButton("Quản lý"); self._manage_button = manage; manage.setFixedHeight(self._responsive_control_height); manage.setStyleSheet(f"QPushButton {{ background:#ffffff; border:1px solid #8ad7d9; border-radius:{_ui_px(8)}px; color:#087f8c; }} QPushButton:hover {{ background:#effcfb; border-color:#0ea5a5; }}"); manage.clicked.connect(self._manage_accounts)
        self.farm_launcher = PrimaryPushButton("Khởi động"); self.farm_launcher.setFixedHeight(self._responsive_control_height); self.farm_launcher.setStyleSheet(f"QPushButton {{ background:#2563eb; border:1px solid #2563eb; border-radius:{_ui_px(8)}px; color:#ffffff; font-weight:600; }} QPushButton:hover {{ background:#1d4ed8; border-color:#1d4ed8; }} QPushButton:disabled {{ background:#93c5fd; border-color:#93c5fd; color:#eff6ff; }}"); self.farm_launcher.clicked.connect(self._farm_launcher_action)
        self.farm_all_button = PushButton("Farms"); self.farm_all_button.setFixedHeight(self._responsive_control_height); self.farm_all_button.setEnabled(False); self.farm_all_button.clicked.connect(self._farm_all_action)
        primary_actions = QHBoxLayout(); primary_actions.setSpacing(_ui_px(7)); primary_actions.addWidget(manage, 1); primary_actions.addWidget(self.farm_launcher, 1); primary_actions.addWidget(self.farm_all_button, 1); al.addLayout(primary_actions); ll.addWidget(accounts)
        arrange_card = self._card(); acl = QVBoxLayout(arrange_card); acl.addWidget(StrongBodyLabel("Sắp xếp cửa sổ")); row = QHBoxLayout(); row.addWidget(QLabel("Số cửa sổ / hàng")); row.addStretch(); self.windows_per_row = ComboBox(); self.windows_per_row.addItems(["2","3","4","5","6"]); self.windows_per_row.setCurrentText(str(self.config.browser.windows_per_row)); self.windows_per_row.currentTextChanged.connect(self._apply_windows_per_row); row.addWidget(self.windows_per_row); acl.addLayout(row)
        arrange_actions = QHBoxLayout(); arrange_actions.setSpacing(_ui_px(7)); apply = PrimaryPushButton("Sắp xếp"); apply.clicked.connect(self._arrange); arrange_actions.addWidget(apply, 1); self.drag = self._icon_button(FIF.MOVE, "Hiện nút kéo"); self.drag.clicked.connect(self._toggle_drag); arrange_actions.addWidget(self.drag); self.scrollbars = self._icon_button(FIF.SCROLL, "Hiện thanh cuộn"); self.scrollbars.clicked.connect(self._toggle_scrollbars); arrange_actions.addWidget(self.scrollbars); acl.addLayout(arrange_actions)
        self.pin = FullRowCheckBox("Luôn nổi trên các cửa sổ khác"); self.pin.stateChanged.connect(lambda _s: self.runner.set_all_topmost(self.pin.isChecked())); acl.addWidget(self.pin); ll.addWidget(arrange_card)
        automation = self._card()
        au = QVBoxLayout(automation)
        sync_header = QHBoxLayout()
        sync_header.addWidget(StrongBodyLabel("Đồng bộ chuột - bàn phím"))
        sync_header.addStretch()
        self.sync_status_icon = QLabel("●")
        self.sync_status_icon.setFixedWidth(_ui_px(22))
        self.sync_status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_sync_status_indicator(False)
        sync_header.addWidget(self.sync_status_icon)
        self.sync_section_toggle = self._icon_button(FIF.CHEVRON_RIGHT, "Mở rộng Đồng bộ chuột - bàn phím")
        self.sync_section_toggle.clicked.connect(self._toggle_sync_section)
        sync_header.addWidget(self.sync_section_toggle)
        au.addLayout(sync_header)
        self.sync_section_expanded = False
        self.sync_section = QWidget()
        sync_section_layout = QVBoxLayout(self.sync_section)
        sync_section_layout.setContentsMargins(0, 0, 0, 0)
        sync_section_layout.setSpacing(_ui_px(7))
        sync_section_layout.addWidget(self._muted("Chọn profile master để đồng bộ chuột và bàn phím."))
        sync = QHBoxLayout()
        self.master = ComboBox()
        sync.addWidget(self.master, 1)
        self.sync = PushButton("Bật đồng bộ")
        self.sync.clicked.connect(self._toggle_sync)
        sync.addWidget(self.sync)
        sync_section_layout.addLayout(sync)
        self.sync_status = self._muted("Sync đang tắt")
        sync_section_layout.addWidget(self.sync_status)
        self.sync_section.hide()
        au.addWidget(self.sync_section)
        ll.addWidget(automation)
        ll.addStretch()
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(_ui_px(8)); body.addWidget(right,1)
        progress = self._card(); pl = QVBoxLayout(progress); ph = QHBoxLayout(); ph.addWidget(StrongBodyLabel("Tiến trình profile")); ph.addStretch(); self.open_badge = QLabel("0 đang mở"); self.open_badge.setStyleSheet(f"background:#e2edff;color:#2767bd;border-radius:{_ui_px(10)}px;padding:{_ui_px(3)}px {_ui_px(8)}px;"); ph.addWidget(self.open_badge); pl.addLayout(ph); self.table_layout = QGridLayout(); self.table_layout.setSpacing(_ui_px(8)); self.table_layout.setColumnStretch(0, 1); self.table_layout.setColumnStretch(1, 1); content=QWidget(); content.setLayout(self.table_layout); self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setWidget(content); pl.addWidget(self.scroll,1); rl.addWidget(progress,1)
        foot=QHBoxLayout(); coords=self._card(); cl=QVBoxLayout(coords); ch=QHBoxLayout(); ch.addWidget(StrongBodyLabel("Lấy tọa độ")); ch.addStretch(); bjson=PushButton("JSON"); bjson.clicked.connect(self._copy_json); bxy=PushButton("Copy x,y"); bxy.clicked.connect(self._copy_xy); ch.addWidget(bjson); ch.addWidget(bxy); cl.addLayout(ch); self.coordinate= self._muted("Chưa đo tọa độ"); cl.addWidget(self.coordinate); foot.addWidget(coords,2); logs=self._card(); logl=QVBoxLayout(logs); logl.addWidget(StrongBodyLabel("Nhật ký gần nhất")); self.log=QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(_ui_px(86)); logl.addWidget(self.log); foot.addWidget(logs,3); coords.hide(); logs.hide(); rl.addLayout(foot)
        self._apply_responsive_style()

    @staticmethod
    def _responsive_metrics(screen_width: int, screen_height: int) -> tuple[int, int, int, int, bool]:
        """Return a screen-safe window and sidebar size in Qt logical pixels."""
        safe_width = max(480, screen_width - 16)
        safe_height = max(360, screen_height - 16)
        compact = screen_width < 1500 or screen_height < 900
        dense = Dashboard._is_dense_screen(screen_width, screen_height)
        if dense:
            width = min(safe_width, max(min(700, safe_width), round(screen_width * 0.78)))
            height = min(safe_height, max(min(440, safe_height), round(screen_height * 0.80)))
            sidebar_min = max(195, min(260, round(width * 0.30)))
            sidebar_max = max(sidebar_min, min(285, round(width * 0.34)))
        else:
            ratio = 0.72 if compact else 0.50
            width = min(safe_width, max(min(780, safe_width), round(screen_width * ratio)))
            height = min(safe_height, max(min(520, safe_height), round(screen_height * ratio)))
            sidebar_min = max(240, min(340, round(width * 0.31)))
            sidebar_max = max(sidebar_min, min(390, round(width * 0.36)))
        return (
            _ui_px(width),
            _ui_px(height),
            _ui_px(sidebar_min),
            _ui_px(sidebar_max),
            compact,
        )

    @staticmethod
    def _is_dense_screen(screen_width: int, screen_height: int) -> bool:
        return screen_width < 1100 or screen_height < 650

    @staticmethod
    def _responsive_typography(screen_width: int, screen_height: int) -> tuple[float, float, float, float, float, int]:
        if Dashboard._is_dense_screen(screen_width, screen_height):
            return _ui_pt(7.25), _ui_pt(7.0), _ui_pt(12.0), _ui_pt(6.5), _ui_pt(9.25), _ui_px(30)
        if screen_width < 1500 or screen_height < 900:
            return _ui_pt(8.0), _ui_pt(7.5), _ui_pt(14.0), _ui_pt(7.0), _ui_pt(10.25), _ui_px(32)
        return _ui_pt(8.75), _ui_pt(8.0), _ui_pt(15.0), _ui_pt(7.5), _ui_pt(11.0), _ui_px(34)

    def _apply_responsive_geometry(self, screen) -> None:
        available = screen.availableGeometry()
        width, height, sidebar_min, sidebar_max, compact = self._responsive_metrics(
            available.width(), available.height()
        )
        (
            self._responsive_font_pt,
            self._responsive_tooltip_font_pt,
            self._responsive_title_font_pt,
            self._responsive_caption_font_pt,
            self._responsive_value_font_pt,
            self._responsive_control_height,
        ) = self._responsive_typography(available.width(), available.height())
        self._responsive_sidebar_min = sidebar_min
        self._responsive_sidebar_max = sidebar_max
        dense = self._is_dense_screen(available.width(), available.height())
        design_margins = (6, 5, 6, 6) if dense else (9, 8, 9, 9) if compact else (14, 12, 14, 14)
        self._responsive_margins = tuple(_ui_px(value) for value in design_margins)
        self._responsive_spacing = _ui_px(6 if dense else 8 if compact else 10)
        # Clear the previous monitor's minimum before moving to a smaller one.
        self.setMinimumSize(0, 0)
        self.setMinimumSize(min(_ui_px(640), width), min(_ui_px(420), height))
        self.setGeometry(
            available.right() - width + 1,
            available.bottom() - height + 1,
            width,
            height,
        )
        if hasattr(self, "_left_sidebar"):
            self._left_sidebar.setMinimumWidth(sidebar_min)
            self._left_sidebar.setMaximumWidth(sidebar_max)
            self._apply_responsive_style()

    def _apply_responsive_style(self) -> None:
        self.setStyleSheet(f"""
            Dashboard {{ background: #eef4fb; color: #172b4d; font-family: Inter, Segoe UI; font-size: {self._responsive_font_pt}pt; }}
            CardWidget {{ background: #ffffff; border: 1px solid #dce6f3; border-radius: {_ui_px(14)}px; }}
            QLabel {{ background: transparent; color: #172b4d; }}
            QScrollArea, QScrollArea > QWidget > QWidget {{ border: none; background: transparent; }}
            QScrollBar:vertical {{ width: {_ui_px(6)}px; margin: {_ui_px(3)}px {_ui_px(1)}px; background: transparent; }}
            QScrollBar::handle:vertical {{ min-height: {_ui_px(24)}px; border-radius: {_ui_px(3)}px; background: #b8c7d9; }}
            QScrollBar::handle:vertical:hover {{ background: #8fa6c2; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ height: {_ui_px(6)}px; margin: {_ui_px(1)}px {_ui_px(3)}px; background: transparent; }}
            QScrollBar::handle:horizontal {{ min-width: {_ui_px(24)}px; border-radius: {_ui_px(3)}px; background: #b8c7d9; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
            QTextEdit {{ background: #f5f8fc; border: 1px solid #dce6f3; border-radius: {_ui_px(9)}px; color: #52657d; }}
            QToolTip {{ background: #ffffff; color: #172b4d; border: 1px solid #d6e2f0; border-radius: {_ui_px(10)}px; padding: {_ui_px(8)}px; font-family: Inter, Segoe UI; font-size: {self._responsive_tooltip_font_pt}pt; }}
        """)
        self._root_layout.setContentsMargins(*self._responsive_margins)
        self._root_layout.setSpacing(self._responsive_spacing)
        self._left_layout.setSpacing(self._responsive_spacing)
        self._dashboard_title.setStyleSheet(f"font-size:{self._responsive_title_font_pt}pt;font-weight:700;")
        dense = self._responsive_control_height == _ui_px(30)
        badge_padding = f"{_ui_px(3)}px {_ui_px(8)}px" if dense else f"{_ui_px(5)}px {_ui_px(10)}px"
        self._secure_badge.setStyleSheet(
            f"background:#d9f7e8;color:#087443;border-radius:{_ui_px(12)}px;padding:{badge_padding};font-weight:600;"
        )
        for caption, value in self._overview_typography:
            caption.setStyleSheet(
                f"color:#62758e;background:transparent;font-size:{self._responsive_caption_font_pt}pt;"
            )
            value.setStyleSheet(f"font-size:{self._responsive_value_font_pt}pt;font-weight:700;")
        self._manage_button.setFixedHeight(self._responsive_control_height)
        self.farm_launcher.setFixedHeight(self._responsive_control_height)
        self.farm_all_button.setFixedHeight(self._responsive_control_height)
        for button in self._responsive_icon_buttons:
            button.setFixedSize(self._responsive_control_height, self._responsive_control_height)
        profile_height = max(_ui_px(26), self._responsive_control_height - _ui_px(3))
        for button in self._responsive_profile_buttons:
            button.setFixedHeight(profile_height)

    def _on_screen_changed(self, screen) -> None:
        if screen is not None:
            self._apply_responsive_geometry(screen)

    @staticmethod
    def _card() -> CardWidget: return CardWidget()
    def _icon_button(self, icon: FIF, tooltip: str, primary: bool = False) -> ToolButton:
        button = PrimaryToolButton() if primary else ToolButton()
        button.setIcon(icon)
        button.setToolTip(tooltip)
        button.setFixedSize(self._responsive_control_height, self._responsive_control_height)
        self._responsive_icon_buttons.append(button)
        return button

    def _compact_profile_button(self, button: PushButton) -> PushButton:
        """Keep per-profile controls compact without shrinking global actions."""
        button.setFixedHeight(max(_ui_px(26), self._responsive_control_height - _ui_px(3)))
        button.setMinimumWidth(0)
        self._responsive_profile_buttons.append(button)
        return button
    @staticmethod
    def _muted(text: str) -> QLabel:
        label=QLabel(text); label.setStyleSheet("color:#62758e; background:transparent;"); label.setWordWrap(True); return label

    @staticmethod
    def _add_master_profile_option(combo: ComboBox, label: str, profile_id: str) -> None:
        """Store the profile ID as combo user data, not as a Fluent icon."""
        combo.addItem(label, userData=profile_id)

    def _draw_rows(self) -> None:
        while self.table_layout.count():
            item=self.table_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.rows.clear(); self.master.clear()
        for profile in self.config.profiles:
            self._add_master_profile_option(
                self.master,
                self._masked_profile_username(profile),
                profile.id,
            )
        for account_number, profile in enumerate(self.config.profiles, start=1):
            card=self._card(); self._set_roster_tooltip(card, ()); layout=QVBoxLayout(card); top=QHBoxLayout(); top.addWidget(StrongBodyLabel(self._profile_display_name(profile, account_number))); top.addStretch(); badge=QLabel("Đã dừng"); badge.setStyleSheet(f"background:#f1f5f9;color:#475569;border-radius:{_ui_px(10)}px;padding:{_ui_px(3)}px {_ui_px(8)}px;"); top.addWidget(badge); layout.addLayout(top); status=self._muted("Đã dừng"); resource=self._muted("—"); details=QHBoxLayout(); details.addWidget(status,1); details.addWidget(resource); layout.addLayout(details); buttons=QHBoxLayout(); buttons.setSpacing(_ui_px(5)); open_btn=self._compact_profile_button(PrimaryPushButton("Mở")); open_btn.clicked.connect(lambda _=False,pid=profile.id:self.runner.submit(pid,CommandKind.OPEN)); buttons.addWidget(open_btn); farm=self._compact_profile_button(PushButton("Farm")); farm.clicked.connect(lambda _=False,pid=profile.id:self._toggle_farm(pid)); buttons.addWidget(farm); buttons.addStretch(); delete=self._icon_button(FIF.DELETE,"Xóa profile"); delete.setFixedSize(_ui_px(29),_ui_px(29)); delete.clicked.connect(lambda _=False,pid=profile.id:self._remove_profile(pid)); buttons.addWidget(delete); layout.addLayout(buttons); index=len(self.rows); self.table_layout.addWidget(card, index // 2, index % 2); self.rows[profile.id]=ProfileRow(status,resource,badge,farm,card)
        self.table_layout.setRowStretch((len(self.rows) + 1) // 2, 1)

    @staticmethod
    def _mask_username(username: str) -> str:
        value = username.strip()
        if not value:
            return "Chưa có username"
        visible = value[:6]
        return visible + "*" * (len(value) - len(visible))

    def _masked_profile_username(self, profile: ProfileConfig) -> str:
        try:
            from ik_chrome_auto.credential_store import WindowsCredentialStore

            credential = WindowsCredentialStore().load(profile.id)
            if credential is not None:
                return self._mask_username(credential.username)
        except Exception:
            pass
        return self._mask_username(profile.name)

    def _profile_display_name(self, profile: ProfileConfig, account_number: int) -> str:
        masked_username = self._masked_profile_username(profile)
        if masked_username == "Chưa có username":
            return profile.name
        return f"Tài khoản {account_number:02d} · {masked_username}"

    def _apply_windows_per_row(self, _value: str | None=None) -> int:
        value=int(self.windows_per_row.currentText()); self.config.browser.windows_per_row=value; save_config(self.config); return value
    def _open_all_and_arrange(self) -> None:
        self._apply_windows_per_row()
        self._auto_arrange_targets = {profile.id for profile in self.config.profiles if profile.enabled}
        self._auto_arrange_states.clear()
        self._auto_arrange_deadline = time.monotonic() + self.config.browser.startup_timeout_ms / 1000
        self.runner.open_all()
        self._append_log("Đang mở tất cả profile; sẽ tự sắp xếp Chrome khi sẵn sàng")
    def _finish_auto_arrange_if_ready(self) -> None:
        targets = self._auto_arrange_targets
        if targets is None:
            return
        if not targets:
            self._auto_arrange_targets = None
            return
        ready = {
            profile_id
            for profile_id, state in self._auto_arrange_states.items()
            if state in {WorkerState.READY, WorkerState.COMPLETED}
        }
        settled = ready | {
            profile_id for profile_id, state in self._auto_arrange_states.items() if state == WorkerState.ERROR
        }
        if settled != targets and time.monotonic() < self._auto_arrange_deadline:
            return
        self._auto_arrange_targets = None
        try:
            count = self.runner.arrange_windows(self.config.browser.windows_per_row)
        except Exception as error:
            self._append_log(f"Không tự sắp xếp được Chrome: {error}")
            return
        self._append_log(f"Đã tự sắp xếp {count} Chrome theo grid {self.config.browser.windows_per_row} cột")
    def _arrange(self) -> None:
        try: count=self.runner.arrange_windows(self._apply_windows_per_row())
        except Exception as error: self._error("Không sắp xếp được",str(error)); return
        self._append_log("Chưa có cửa sổ để sắp xếp" if not count else f"Đã sắp xếp {count} cửa sổ")
    def _toggle_drag(self) -> None:
        self.drag_visible=not self.drag_visible; self.runner.set_all_drag_items_visible(self.drag_visible); self.drag.setToolTip("Ẩn nút kéo" if self.drag_visible else "Hiện nút kéo")
    def _toggle_scrollbars(self) -> None:
        self.scrollbars_visible = not self.scrollbars_visible
        count = self.runner.set_all_scrollbars_visible(self.scrollbars_visible)
        self.scrollbars.setToolTip("Ẩn thanh cuộn" if self.scrollbars_visible else "Hiện thanh cuộn")
        self._append_log(
            ("Đã hiện" if self.scrollbars_visible else "Đã ẩn")
            + f" thanh cuộn trên {count} Chrome đang mở"
        )
    @staticmethod
    def _set_profile_card_state(row: ProfileRow, state: WorkerState) -> None:
        if state in {WorkerState.STARTING, WorkerState.READY, WorkerState.RUNNING, WorkerState.COMPLETED}:
            row.card.setStyleSheet(
                "CardWidget { "
                "background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                "stop:0 #ffffff,stop:0.58 #fff7ed,stop:1 #fb923c);"
                "border:1px solid #fdba74; }"
            )
        else:
            row.card.setStyleSheet("")
    def _toggle_sync(self) -> None:
        if self.runner.sync_enabled:
            self.runner.disable_sync()
            self.master.setEnabled(True)
            self.sync.setText("Bật đồng bộ")
            self.sync_status.setText("Sync đang tắt")
            self._set_sync_status_indicator(False)
            return
        master = str(self.master.currentData() or "")
        opened_profiles = [
            profile for profile in self.config.profiles
            if self.runner.has_open_session(profile.id)
        ]
        if master not in {profile.id for profile in opened_profiles}:
            self._warning("Master chưa mở", "Hãy mở profile master trước khi bật đồng bộ.")
            return
        followers = [profile for profile in opened_profiles if profile.id != master]
        if not followers:
            self._warning("Chưa đủ profile", "Hãy mở ít nhất một profile nhận điều khiển.")
            return
        dialog = FarmProfileDialog(
            self,
            followers,
            self._sync_target_profiles & {profile.id for profile in followers},
            window_title="Chọn thiết bị nhận đồng bộ",
            heading="Thiết bị được điều khiển",
            description="Chọn các profile sẽ nhận thao tác chuột và bàn phím từ profile master.",
            confirm_text="Bật đồng bộ",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_profile_ids()
        if not selected:
            self._warning("Chưa chọn thiết bị", "Chọn ít nhất một profile nhận điều khiển.")
            return
        self._sync_target_profiles = selected
        self.runner.enable_sync(master, selected)
        self.master.setEnabled(False)
        self.sync.setText("Tắt đồng bộ")
        self.sync_status.setText(f"MASTER: {self.master.currentText()} → {len(selected)} thiết bị")
        self._set_sync_status_indicator(True)
    def _set_sync_status_indicator(self, enabled: bool) -> None:
        color = "#16a34a" if enabled else "#94a3b8"
        state = "bật" if enabled else "tắt"
        self.sync_status_icon.setStyleSheet(
            f"color:{color}; background:transparent; font-size:{_ui_px(22)}px; font-weight:700;"
        )
        self.sync_status_icon.setToolTip(f"Đồng bộ chuột - bàn phím đang {state}")
    def _toggle_sync_section(self) -> None:
        self.sync_section_expanded = not self.sync_section_expanded
        self.sync_section.setVisible(self.sync_section_expanded)
        self.sync_section_toggle.setIcon(
            FIF.CHEVRON_DOWN_MED if self.sync_section_expanded else FIF.CHEVRON_RIGHT
        )
        self.sync_section_toggle.setToolTip(
            "Thu gọn Đồng bộ chuột - bàn phím"
            if self.sync_section_expanded
            else "Mở rộng Đồng bộ chuột - bàn phím"
        )
    def _toggle_farm(self, pid: str) -> None:
        row = self.rows[pid]
        if pid in self.farm_profiles:
            self.farm_profiles.remove(pid); row.farm.setText("Farm"); self.runner.submit(pid, CommandKind.STOP_FARM)
        else:
            self.farm_profiles.add(pid); row.farm.setText("Dừng Farm"); self.runner.submit(pid, CommandKind.START_FARM)

    def _farm_launcher_action(self) -> None:
        """Open selected profile tabs, or close them when already prepared."""
        if self._farm_launcher_phase == "launch":
            selected = self._choose_farm_profiles()
            if not selected:
                return
            self._farm_launch_profiles = selected
            self._farm_open_states = {
                profile_id: WorkerState.READY
                for profile_id in selected
                if self.runner.has_open_session(profile_id)
            }
            self._farm_open_queue = deque(
                profile.id
                for profile in self.config.profiles
                if profile.id in selected and not self.runner.has_open_session(profile.id)
            )
            pending = len(self._farm_open_queue)
            self.farm_launcher.setEnabled(False)
            self.farm_launcher.setText("Đang mở...")
            self.farm_all_button.setEnabled(False)
            self._farm_launcher_phase = "opening"
            self._farm_next_open_at = 0.0
            self._farm_batch_profiles.clear()
            self._farm_batch_submitted = 0
            self._farm_batch_resume_at = 0.0
            self._farm_resource_pause_started = 0.0
            self._farm_resource_pause_reason = None
            try:
                memory = get_system_memory_status()
                self._farm_launch_policy = FarmLaunchPolicy.for_total_memory(memory.total_bytes)
            except Exception:
                self._farm_launch_policy = FarmLaunchPolicy.for_total_memory(32 * 1_073_741_824)
            startup_timeout = self.config.browser.startup_timeout_ms / 1000
            self._farm_open_deadline = time.monotonic() + self._farm_launch_policy.estimated_timeout_seconds(
                pending,
                startup_timeout,
            )
            self._advance_farm_opening()
            policy = self._farm_launch_policy
            self._append_log(
                f"Đang mở {len(selected)} tab profile"
                + (
                    f" ({pending} tab mới; {policy.batch_size} tab/đợt; "
                    f"cách nhau {policy.profile_interval_seconds:.2f}s; "
                    f"nghỉ {policy.batch_pause_seconds:.0f}s giữa các đợt)"
                    if pending else ""
                )
            )
            return
        if self._farm_launcher_phase == "ready":
            # Tabs may be farming either through the bulk button or through an
            # individual profile control. Always stop every tracked Farm job
            # before beginning the serial Chrome shutdown.
            if self.farm_profiles:
                self._stop_all_farms()
            # Closing every worker at once makes Chrome race to tear down its
            # profiles. Queue an explicit STOP per selected profile instead.
            self._farm_close_queue = deque(
                profile.id for profile in self.config.profiles
                if profile.id in self._farm_launch_profiles
            )
            if not self._farm_close_queue:
                self._finish_farm_stopping()
                return
            self.farm_launcher.setEnabled(False)
            self.farm_launcher.setText("Đang đóng tab…")
            self._farm_launcher_phase = "stopping"
            self.farm_all_button.setEnabled(False)
            self._farm_close_in_flight = None
            self._farm_close_deadline = 0.0
            self._advance_farm_stopping()
            self._append_log(f"Đang đóng lần lượt {len(self._farm_close_queue)} tab profile")

    def _farm_all_action(self) -> None:
        """Start or stop Farm without changing the selected Chrome sessions."""
        if self._farm_all_running:
            self._stop_all_farms()
            return
        opened = self._opened_profile_ids()
        if len(opened) <= 1:
            self.farm_all_button.setEnabled(False)
            return
        for profile_id in opened:
            if profile_id not in self.farm_profiles:
                self.farm_profiles.add(profile_id)
                self.runner.submit(profile_id, CommandKind.START_FARM)
            row = self.rows.get(profile_id)
            if row:
                row.farm.setText("Dừng Farm")
        self._farm_all_running = True
        self.farm_all_button.setText("Dừng Farm")
        self.farm_all_button.setStyleSheet(f"QPushButton {{ background:#ef4444; border:1px solid #ef4444; border-radius:{_ui_px(8)}px; color:#ffffff; font-weight:600; }} QPushButton:hover {{ background:#dc2626; border-color:#dc2626; }}")
        self._append_log(f"Đã gửi lệnh chạy Farm cho {len(opened)} profile")

    def _stop_all_farms(self) -> None:
        for profile_id in tuple(self.farm_profiles):
            self.runner.submit(profile_id, CommandKind.STOP_FARM)
            row = self.rows.get(profile_id)
            if row:
                row.farm.setText("Farm")
        self.farm_profiles.clear()
        self._farm_all_running = False
        self.farm_all_button.setText("Farms")
        self.farm_all_button.setStyleSheet("")
        self.farm_all_button.setEnabled(
            self._farm_launcher_phase not in {"opening", "stopping"}
            and self._opened_profile_count() > 1
        )
        self._append_log("Đã gửi lệnh dừng Farm cho toàn bộ profile")

    def _opened_profile_ids(self) -> set[str]:
        return {
            profile.id for profile in self.config.profiles
            if self.runner.has_open_session(profile.id)
        }

    def _opened_profile_count(self) -> int:
        return len(self._opened_profile_ids())

    def _finish_farm_opening_if_ready(self) -> None:
        """Keep the launcher locked until selected tabs have opened and tiled."""
        if self._farm_launcher_phase != "opening":
            return
        if self._farm_open_queue:
            return
        targets = self._farm_launch_profiles
        ready = {
            profile_id for profile_id, state in self._farm_open_states.items()
            if state in {WorkerState.READY, WorkerState.COMPLETED}
        }
        failed = {
            profile_id for profile_id, state in self._farm_open_states.items()
            if state == WorkerState.ERROR
        }
        if ready != targets and not failed and time.monotonic() < self._farm_open_deadline:
            return
        if ready != targets:
            missing = targets - ready
            self._abort_farm_opening("Không mở đủ tab", f"Chưa sẵn sàng: {', '.join(sorted(missing))}")
            return
        try:
            # Farm follows the user-selected layout setting. The runner keeps
            # profile order, therefore placement is left → right, top → down.
            columns = self._apply_windows_per_row()
            count = self.runner.arrange_windows(
                columns,
                profile_ids=self._farm_launch_profiles,
            )
        except Exception as error:
            self._abort_farm_opening("Không sắp xếp được tab", str(error), critical=True)
            return
        self._farm_launcher_phase = "ready"
        self.farm_launcher.setEnabled(True)
        self.farm_launcher.setText("Đóng tabs")
        self.farm_all_button.setEnabled(self._opened_profile_count() > 1)
        self._append_log(
            f"Đã mở và xếp {count} tab theo {columns} cột (trái → phải, trên → dưới)"
        )

    def _advance_farm_opening(self) -> None:
        """Open profiles in resource-guarded batches instead of one startup burst."""
        if self._farm_launcher_phase != "opening" or not self._farm_open_queue:
            return
        now = time.monotonic()
        if now > self._farm_open_deadline:
            waiting = sorted(
                profile_id
                for profile_id in self._farm_launch_profiles
                if self._farm_open_states.get(profile_id) not in {WorkerState.READY, WorkerState.COMPLETED}
            )
            self._abort_farm_opening(
                "Mở profile quá thời gian",
                "Các profile chưa sẵn sàng: " + ", ".join(waiting),
            )
            return
        policy = self._farm_launch_policy
        if self._farm_batch_submitted >= policy.batch_size:
            terminal_states = {WorkerState.READY, WorkerState.COMPLETED, WorkerState.ERROR}
            if any(self._farm_open_states.get(profile_id) not in terminal_states for profile_id in self._farm_batch_profiles):
                return
            if now < self._farm_batch_resume_at:
                return
            self._append_log(f"Đợt {len(self._farm_batch_profiles)} tab đã ổn định; bắt đầu đợt tiếp theo")
            self._farm_batch_profiles.clear()
            self._farm_batch_submitted = 0
        if now < self._farm_next_open_at:
            return
        reason: str | None = None
        try:
            memory = get_system_memory_status()
            reason = policy.resource_block_reason(
                available_memory_bytes=memory.available_bytes,
                memory_load_percent=memory.load_percent,
                profile_cpu_percent=self._latest_profile_cpu_percent,
            )
        except Exception as error:
            self._append_log(f"Không đọc được tài nguyên hệ thống: {error}")
        if reason:
            if not self._farm_resource_pause_started:
                self._farm_resource_pause_started = now
                self._farm_resource_pause_reason = reason
                self.farm_launcher.setText("Tạm dừng do tải cao…")
                self._append_log(f"Tạm dừng mở tab: {reason}")
            if now - self._farm_resource_pause_started >= policy.resource_pause_timeout_seconds:
                self._abort_farm_opening(
                    "Đã dừng mở thêm profile",
                    f"Tài nguyên không hồi phục sau {policy.resource_pause_timeout_seconds:.0f} giây: {reason}",
                )
            return
        if self._farm_resource_pause_started:
            self._append_log(f"Tài nguyên đã ổn định; tiếp tục mở tab (trước đó: {self._farm_resource_pause_reason})")
            self._farm_resource_pause_started = 0.0
            self._farm_resource_pause_reason = None
            self.farm_launcher.setText("Đang mở...")
        profile_id = self._farm_open_queue.popleft()
        self.runner.submit(profile_id, CommandKind.OPEN)
        self._farm_batch_profiles.add(profile_id)
        self._farm_batch_submitted += 1
        self._farm_next_open_at = now + policy.profile_interval_seconds
        if self._farm_batch_submitted >= policy.batch_size and self._farm_open_queue:
            self._farm_batch_resume_at = now + policy.batch_pause_seconds
        self._append_log(
            f"Đang mở tab {profile_id} "
            f"({self._farm_batch_submitted}/{policy.batch_size} trong đợt); "
            f"còn {len(self._farm_open_queue)} tab"
        )

    def _abort_farm_opening(self, title: str, message: str, *, critical: bool = False) -> None:
        """Reset only the launch controller; already opened profiles stay recoverable."""
        self._farm_launcher_phase = "launch"
        self._farm_launch_profiles.clear()
        self._farm_open_queue.clear()
        self._farm_batch_profiles.clear()
        self._farm_batch_submitted = 0
        self._farm_batch_resume_at = 0.0
        self._farm_resource_pause_started = 0.0
        self._farm_resource_pause_reason = None
        self._farm_all_running = False
        self.farm_launcher.setEnabled(True)
        self.farm_launcher.setText("Khởi động")
        self.farm_all_button.setEnabled(False)
        self._set_farm_launcher_launch_style()
        (self._error if critical else self._warning)(title, message)

    def _advance_farm_stopping(self) -> None:
        """Close the selected Chrome windows one at a time."""
        if self._farm_launcher_phase != "stopping":
            return
        if self._farm_close_in_flight is not None:
            if time.monotonic() <= self._farm_close_deadline:
                return
            self._append_log(
                f"{self._farm_close_in_flight} đóng quá lâu; tiếp tục tab kế tiếp"
            )
            self._farm_close_in_flight = None
        while self._farm_close_queue:
            profile_id = self._farm_close_queue.popleft()
            if not self.runner.has_open_session(profile_id):
                self._append_log(f"{profile_id} đã đóng")
                continue
            self.runner.submit(profile_id, CommandKind.STOP)
            self._farm_close_in_flight = profile_id
            self._farm_close_deadline = time.monotonic() + 12.0
            self._append_log(
                f"Đang đóng tab {profile_id}; còn {len(self._farm_close_queue)} tab trong hàng đợi"
            )
            return
        self._finish_farm_stopping()

    def _finish_farm_stopping(self) -> None:
        """Restore the launcher only after the serial close queue is empty."""
        self.farm_profiles.clear()
        self._farm_launch_profiles.clear()
        self._farm_open_queue.clear()
        self._farm_batch_profiles.clear()
        self._farm_batch_submitted = 0
        self._farm_batch_resume_at = 0.0
        self._farm_resource_pause_started = 0.0
        self._farm_resource_pause_reason = None
        self._farm_close_queue.clear()
        self._farm_close_in_flight = None
        self._farm_close_deadline = 0.0
        self._farm_launcher_phase = "launch"
        self.farm_launcher.setEnabled(True)
        self.farm_launcher.setText("Khởi động")
        self._farm_all_running = False
        self.farm_all_button.setText("Farms")
        self.farm_all_button.setStyleSheet("")
        self.farm_all_button.setEnabled(False)
        self._set_farm_launcher_launch_style()
        self._append_log("Đã đóng toàn bộ tab profile")

    def _set_farm_launcher_launch_style(self) -> None:
        self.farm_launcher.setStyleSheet(f"QPushButton {{ background:#2563eb; border:1px solid #2563eb; border-radius:{_ui_px(8)}px; color:#ffffff; font-weight:600; }} QPushButton:hover {{ background:#1d4ed8; border-color:#1d4ed8; }} QPushButton:disabled {{ background:#93c5fd; border-color:#93c5fd; color:#eff6ff; }}")

    def _choose_farm_profiles(self) -> set[str]:
        """Return profiles chosen for the next Farm run, without starting them."""
        profiles = [profile for profile in self.config.profiles if profile.enabled]
        if not profiles:
            self._warning("Chưa có profile", "Hãy thêm ít nhất một tài khoản game trước khi khởi động.")
            return set()
        dialog = FarmProfileDialog(self, profiles, self._farm_launch_profiles or self.farm_profiles)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return set()
        selected = dialog.selected_profile_ids()
        if not selected:
            self._warning("Chưa chọn profile", "Chọn ít nhất một profile để khởi động.")
            return set()
        return selected

    def _manage_accounts(self) -> None:
        if not self._authorize_two_factor("xem hoặc chỉnh sửa tài khoản game"): return
        dialog=AccountManagerDialog(self, self.config.profiles)
        if dialog.exec()!=QDialog.DialogCode.Accepted: return
        entries=dialog.accounts(); existing={p.id for p in self.config.profiles}; original={p.id:p for p in self.config.profiles if p.mode==ProfileMode.MANAGED}; unmanaged=[p for p in self.config.profiles if p.mode!=ProfileMode.MANAGED]; kept_ids={entry[0] for entry in entries if entry[0]}; removed_ids=set(original)-kept_ids
        try:
            from ik_chrome_auto.credential_store import AccountCredential, WindowsCredentialStore
            store=WindowsCredentialStore(); updated=[]; added=0
            for index,(pid,username,password) in enumerate(entries,start=1):
                if pid is None:
                    pid=unique_profile_id(f"account-{index}",existing); existing.add(pid); profile=ProfileConfig(pid,"",ProfileMode.MANAGED,(self.config.data_dir/"profiles"/pid).resolve(),enabled=True); added+=1
                else: profile=original[pid]
                preview=self._mask_username(username); profile.name=f"Tài khoản {index:02d} · {preview}"; store.save(AccountCredential(pid,username,password)); updated.append(profile)
            for pid in removed_ids:
                worker=self.runner.workers.get(pid)
                if worker: worker.shutdown()
                store.delete(pid)
            self.config.profiles=[*updated,*unmanaged]; save_config(self.config)
            for profile in updated:
                if profile.user_data_dir: profile.user_data_dir.mkdir(parents=True,exist_ok=True)
            self.runner.sync_profiles(); self._draw_rows(); self._append_log(f"Đã lưu {len(updated)} tài khoản" + (f", thêm {added}" if added else ""))
        except Exception as error: self._error("Không lưu được tài khoản",str(error))
    def _remove_profile(self,pid:str) -> None:
        if not self._authorize_two_factor("xóa tài khoản game"): return
        profile=self.config.profile(pid)
        if QMessageBox.question(self,"Bỏ profile",f"Bỏ '{profile.name}' khỏi dashboard?\n\nCredential mã hóa cũng sẽ bị xóa.")!=QMessageBox.StandardButton.Yes: return
        worker=self.runner.workers.get(pid)
        if worker: worker.shutdown()
        try:
            from ik_chrome_auto.credential_store import WindowsCredentialStore
            WindowsCredentialStore().delete(pid)
        except Exception as error: self._append_log(str(error))
        self.config.profiles=[p for p in self.config.profiles if p.id!=pid]; save_config(self.config); self.runner.sync_profiles(); self._draw_rows()
    def _authorize_two_factor(self, action: str) -> bool:
        """Google Authenticator is the only active gate for sensitive actions."""
        try:
            service = TwoFactorService()
            if not service.is_configured():
                enrollment = TwoFactorSetupDialog.setup(self)
                if enrollment is None:
                    return False
                if not service.confirm_enrollment(enrollment[0], enrollment[1]):
                    self._warning("Mã chưa đúng", "Mã Google Authenticator không hợp lệ. Hãy thử lại.")
                    return False
                RecoveryCodesDialog(self, enrollment[0].recovery_codes).exec()
                self._append_log("Đã bật Google Authenticator cho các thao tác nhạy cảm")
                return True
            dialog = TwoFactorVerifyDialog(self, action)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            if dialog.using_recovery:
                if not service.consume_recovery_code(dialog.code.text()):
                    self._warning("Mã khôi phục không đúng", "Mã này không hợp lệ hoặc đã được sử dụng.")
                    return False
                enrollment = TwoFactorSetupDialog.setup(self, recovery=True)
                if enrollment is None or not service.confirm_enrollment(enrollment[0], enrollment[1]):
                    self._warning("Chưa hoàn tất", "Hãy thiết lập lại Google Authenticator trước khi tiếp tục.")
                    return False
                RecoveryCodesDialog(self, enrollment[0].recovery_codes).exec()
                return True
            if not service.verify_current_code(dialog.code.text()):
                self._warning("Mã chưa đúng", "Mã Google Authenticator không hợp lệ hoặc đã hết hạn.")
                return False
            return True
        except Exception as error:
            self._warning("Không xác thực được", str(error)); return False
    def _copy_xy(self) -> None:
        if not self.last_coordinate:return
        _,event=self.last_coordinate; canvas=event.get("canvas"); value=f"{canvas.get('pixel_x_rounded')},{canvas.get('pixel_y_rounded')}" if isinstance(canvas,dict) else ""
        QApplication.clipboard().setText(value)
    def _copy_json(self) -> None:
        if self.last_coordinate: QApplication.clipboard().setText(json.dumps({"profile_id":self.last_coordinate[0],**self.last_coordinate[1]},ensure_ascii=False,indent=2,default=str))
    def _poll(self) -> None:
        try:
            while True:
                snap=self.updates.get_nowait(); self._auto_arrange_states[snap.profile_id] = snap.state
                if self._farm_launcher_phase == "opening" and snap.profile_id in self._farm_launch_profiles:
                    self._farm_open_states[snap.profile_id] = snap.state
                if (
                    self._farm_launcher_phase == "stopping"
                    and snap.profile_id == self._farm_close_in_flight
                    and snap.state == WorkerState.STOPPED
                ):
                    self._append_log(f"Đã đóng tab {snap.profile_id}")
                    self._farm_close_in_flight = None
                row=self.rows.get(snap.profile_id)
                if row:
                    row.status.setText(snap.message); text,bg,fg=self._state(snap.state); row.badge.setText(text); row.badge.setStyleSheet(f"background:{bg};color:{fg};border-radius:{_ui_px(10)}px;padding:{_ui_px(3)}px {_ui_px(8)}px;"); self._set_profile_card_state(row,snap.state)
                    self._set_roster_tooltip(row.card, snap.farm_roster)
                    if snap.state in {WorkerState.STOPPED, WorkerState.ERROR} or "Đã dừng Auto Farm" in snap.message: self.farm_profiles.discard(snap.profile_id); row.farm.setText("Farm")
                self._append_log(f"[{snap.profile_id}] {snap.message}")
        except queue.Empty: pass
        try:
            while True:
                pid,event=self.coordinate_updates.get_nowait(); self.last_coordinate=(pid,event); self.coordinate.setText(format_coordinate(pid,event))
        except queue.Empty: pass
        self._finish_auto_arrange_if_ready()
        self._advance_farm_opening()
        self._finish_farm_opening_if_ready()
        self._advance_farm_stopping()
        now=time.monotonic()
        if now-self._last_resources>=2:
            try:
                overview=self.runner.resource_overview(); self._latest_profile_cpu_percent=overview.cpu_percent; self.total.setText(str(overview.total_profiles)); self.opened.setText(str(overview.opened_profiles)); self.open_badge.setText(f"{overview.opened_profiles} đang mở"); self.ram.setText(f"{overview.ram_bytes/1_048_576:.0f} MB"); self.cpu.setText(f"{overview.cpu_percent:.1f}%")
                if self._farm_all_running:
                    self.farm_all_button.setEnabled(True)
                else:
                    self.farm_all_button.setEnabled(
                        self._farm_launcher_phase not in {"opening", "stopping"}
                        and self._opened_profile_count() > 1
                    )
                for item in overview.profiles:
                    if item.profile_id in self.rows:self.rows[item.profile_id].resource.setText("—" if not item.opened else f"{item.ram_bytes/1_048_576:.0f} MB | {item.cpu_percent:.1f}%")
            except Exception as error:self._append_log(str(error))
            self._last_resources=now
        if now-self._last_trim>=60:
            try:self.runner.trim_all_profile_memory()
            except Exception:pass
            self._last_trim=now
    @staticmethod
    def _state(state:WorkerState)->tuple[str,str,str]:
        if state in {WorkerState.READY,WorkerState.COMPLETED}:return "Sẵn sàng","#dcfce7","#15803d"
        if state in {WorkerState.STARTING,WorkerState.RUNNING}:return "Đang chạy","#dbeafe","#2563eb"
        if state==WorkerState.ERROR:return "Cần chú ý","#fee2e2","#b91c1c"
        return "Đã dừng","#f1f5f9","#475569"
    def _append_log(self,message:str)->None:
        self._log_lines.append(message); value="\n".join(self._log_lines)
        if hasattr(self.log,"setPlainText"): self.log.setPlainText(value)
        else:
            self.log.configure(state="normal"); self.log.delete("1.0","end"); self.log.insert("end",value+"\n"); self.log.see("end"); self.log.configure(state="disabled")

    @staticmethod
    def _set_roster_tooltip(card: CardWidget, roster: tuple[tuple[int, str], ...]) -> None:
        if not roster:
            tooltip = "<b>Trạng thái đội</b><br><span style='color:#62758e'>Chưa có dữ liệu quét roster.</span>"
        else:
            rows = []
            for team, state in roster:
                ready = state == "ready"
                label = "Sẵn sàng" if ready else "Đang bận"
                background = "#dcfce7" if ready else "#fef3c7"
                color = "#15803d" if ready else "#a16207"
                rows.append(
                    f"<tr><td><span style='background:#eaf1fb;color:#2e5f9e;padding:3px 7px;border-radius:8px'>Đội {team}</span></td>"
                    f"<td>&nbsp;<span style='background:{background};color:{color};padding:3px 7px;border-radius:8px'>{label}</span></td></tr>"
                )
            tooltip = "<b>Trạng thái đội</b><br><table cellspacing='5'>" + "".join(rows) + "</table>"
        card.setToolTip(tooltip)
        for label in card.findChildren(QLabel):
            label.setToolTip(tooltip)
    def _warning(self,title:str,message:str)->None: QMessageBox.warning(self,title,message)
    def _error(self,title:str,message:str)->None: QMessageBox.critical(self,title,message)
    def closeEvent(self,event:QCloseEvent)->None:
        self.timer.stop(); self.runner.shutdown(); event.accept()


class TwoFactorSetupDialog(QDialog):
    """Enrollment screen for Google Authenticator-compatible TOTP apps."""
    def __init__(self, parent: QWidget, recovery: bool = False) -> None:
        super().__init__(parent)
        self.enrollment = TwoFactorService().begin_enrollment()
        self.setWindowTitle("Thiết lập Google Authenticator")
        self.setModal(True)
        self.resize(_ui_px(430), _ui_px(590))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_ui_px(26), _ui_px(24), _ui_px(26), _ui_px(24))
        layout.setSpacing(_ui_px(12))
        layout.addWidget(SubtitleLabel("Thiết lập lại bảo mật" if recovery else "Bảo vệ bằng Google Authenticator"))
        message = "Mã khôi phục đã được dùng. Quét QR mới để thay thế thiết bị cũ." if recovery else "Quét QR bằng Google Authenticator, rồi nhập mã 6 số để xác nhận."
        layout.addWidget(self._description(message))
        qr = QLabel()
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr.setPixmap(self._qr_pixmap(self.enrollment.provisioning_uri))
        layout.addWidget(qr)
        layout.addWidget(self._description("Không quét được QR? Nhập secret này thủ công:"))
        secret = LineEdit(); secret.setText(self.enrollment.secret); secret.setReadOnly(True)
        layout.addWidget(secret)
        self.code = LineEdit(); self.code.setPlaceholderText("Mã 6 số từ Authenticator"); self.code.setMaxLength(6)
        layout.addWidget(self.code)
        actions = QHBoxLayout(); actions.addStretch(); cancel = PushButton("Hủy"); cancel.clicked.connect(self.reject); confirm = PrimaryPushButton("Xác nhận"); confirm.clicked.connect(self._validate); actions.addWidget(cancel); actions.addWidget(confirm); layout.addLayout(actions)

    @staticmethod
    def _description(text: str) -> QLabel:
        label = QLabel(text); label.setWordWrap(True); label.setStyleSheet("color:#62758e;background:transparent;"); return label

    @staticmethod
    def _qr_pixmap(uri: str) -> QPixmap:
        import qrcode
        image = qrcode.make(uri)
        data = BytesIO(); image.save(data, format="PNG")
        pixmap = QPixmap(); pixmap.loadFromData(data.getvalue(), "PNG")
        return pixmap.scaled(_ui_px(220), _ui_px(220), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

    def _validate(self) -> None:
        if TwoFactorService.verify_enrollment_code(self.enrollment.secret, self.code.text()):
            self.accept()
        else:
            QMessageBox.warning(
                self,
                "Mã chưa đúng",
                "Mã 6 số không hợp lệ. Hãy bật thời gian tự động trên Windows và điện thoại, "
                "chờ mã mới rồi thử lại.",
            )

    @classmethod
    def setup(cls, parent: QWidget, recovery: bool = False) -> tuple[TwoFactorEnrollment, str] | None:
        dialog = cls(parent, recovery)
        return (dialog.enrollment, dialog.code.text()) if dialog.exec() == QDialog.DialogCode.Accepted else None


class TwoFactorVerifyDialog(QDialog):
    def __init__(self, parent: QWidget, action: str) -> None:
        super().__init__(parent)
        self.using_recovery = False
        self.setWindowTitle("Xác thực bảo mật")
        self.setModal(True)
        self.setFixedWidth(_ui_px(390))
        layout = QVBoxLayout(self); layout.setContentsMargins(_ui_px(26), _ui_px(24), _ui_px(26), _ui_px(24)); layout.setSpacing(_ui_px(12))
        layout.addWidget(SubtitleLabel("Xác nhận danh tính"))
        note = QLabel(f"Nhập mã từ Google Authenticator để {action}."); note.setWordWrap(True); note.setStyleSheet("color:#62758e;background:transparent;"); layout.addWidget(note)
        self.code = LineEdit(); self.code.setPlaceholderText("Mã 6 số"); self.code.setMaxLength(16); layout.addWidget(self.code)
        self.recovery = PushButton("Không có mã? Dùng recovery code"); self.recovery.clicked.connect(self._use_recovery); layout.addWidget(self.recovery)
        actions = QHBoxLayout(); actions.addStretch(); cancel=PushButton("Hủy"); cancel.clicked.connect(self.reject); confirm=PrimaryPushButton("Xác nhận"); confirm.clicked.connect(self.accept); actions.addWidget(cancel); actions.addWidget(confirm); layout.addLayout(actions)

    def _use_recovery(self) -> None:
        self.using_recovery = True
        self.code.clear(); self.code.setMaxLength(14); self.code.setPlaceholderText("Ví dụ: ABCD-EFGH-JKLM")
        self.recovery.setVisible(False)


class RecoveryCodesDialog(QDialog):
    def __init__(self, parent: QWidget, codes: tuple[str, ...]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lưu mã khôi phục")
        self.setModal(True)
        self.resize(_ui_px(430), _ui_px(420))
        layout=QVBoxLayout(self); layout.setContentsMargins(_ui_px(26),_ui_px(24),_ui_px(26),_ui_px(24)); layout.setSpacing(_ui_px(12))
        layout.addWidget(SubtitleLabel("Lưu mã khôi phục ngay"))
        note=QLabel("Mỗi mã chỉ dùng một lần khi mất Google Authenticator. Các mã này sẽ không hiển thị lại."); note.setWordWrap(True); note.setStyleSheet("color:#b45309;background:transparent;"); layout.addWidget(note)
        values=QTextEdit(); values.setReadOnly(True); values.setPlainText("\n".join(codes)); values.setFixedHeight(_ui_px(220)); layout.addWidget(values)
        actions=QHBoxLayout(); copy=PushButton("Sao chép"); copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(codes))); actions.addWidget(copy); actions.addStretch(); close=PrimaryPushButton("Đã lưu an toàn"); close.clicked.connect(self.accept); actions.addWidget(close); layout.addLayout(actions)


class FarmProfileDialog(QDialog):
    """Reusable two-column picker for profile-based actions."""

    def __init__(
        self,
        parent: Dashboard,
        profiles: list[ProfileConfig],
        selected: set[str],
        *,
        window_title: str = "Chọn profile khởi động",
        heading: str = "Khởi động",
        description: str = "Chọn các profile muốn mở. Profile đang chạy sẽ được giữ nguyên.",
        confirm_text: str = "Khởi động",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(window_title)
        column_count = self._column_count(len(profiles))
        self.setMinimumWidth(_ui_px(470 if column_count == 1 else 760))
        self.resize(_ui_px(500 if column_count == 1 else 820), _ui_px(430 if column_count == 1 else 520))
        self._checks: dict[str, CheckBox] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_ui_px(22), _ui_px(20), _ui_px(22), _ui_px(18))
        layout.setSpacing(_ui_px(12))
        header = QHBoxLayout()
        title = SubtitleLabel(heading)
        title.setStyleSheet(f"font-size:{_ui_px(20)}px;font-weight:700;")
        header.addWidget(title)
        header.addStretch()
        check_all = PushButton("Chọn tất cả")
        clear_all = PushButton("Bỏ chọn")
        check_all.clicked.connect(self._check_all)
        clear_all.clicked.connect(self._clear_all)
        header.addWidget(check_all)
        header.addWidget(clear_all)
        layout.addLayout(header)
        subtitle = QLabel(description)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#62758e;")
        layout.addWidget(subtitle)

        content = QWidget()
        rows = QGridLayout(content)
        rows.setContentsMargins(_ui_px(4), _ui_px(4), _ui_px(4), _ui_px(4))
        rows.setSpacing(_ui_px(7))
        for index, profile in enumerate(profiles):
            check = FullRowCheckBox(profile.name)
            check.setChecked(profile.id in selected)
            check.setToolTip(profile.id)
            check.setStyleSheet(f"CheckBox {{ background:#f7faff; border:1px solid #dce6f3; border-radius:{_ui_px(9)}px; padding:{_ui_px(10)}px {_ui_px(12)}px; font-weight:600; }} CheckBox:hover {{ background:#eef6ff; border-color:#9ec5fe; }}")
            self._checks[profile.id] = check
            rows.addWidget(check, index // column_count, index % column_count)
        for column in range(column_count):
            rows.setColumnStretch(column, 1)
        rows.setRowStretch((len(profiles) + column_count - 1) // column_count, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel = PushButton("Hủy")
        cancel.clicked.connect(self.reject)
        run = PrimaryPushButton(confirm_text)
        run.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(run)
        layout.addLayout(footer)

    @staticmethod
    def _column_count(profile_count: int) -> int:
        return 2 if profile_count > 10 else 1

    def _check_all(self) -> None:
        for check in self._checks.values():
            check.setChecked(True)

    def _clear_all(self) -> None:
        for check in self._checks.values():
            check.setChecked(False)

    def selected_profile_ids(self) -> set[str]:
        return {profile_id for profile_id, check in self._checks.items() if check.isChecked()}


class AccountManagerDialog(QDialog):
    """Central CRUD form. Secrets only live in this dialog until saved to Windows Vault."""
    def __init__(self,parent:Dashboard,profiles:list[ProfileConfig])->None:
        super().__init__(parent); self.rows:list[tuple[str|None,LineEdit,PasswordLineEdit,QWidget]]=[]; self._username_warnings:dict[LineEdit,QLabel]={}; self.setWindowTitle("Quản lý tài khoản game"); self.resize(_ui_px(720),_ui_px(520)); layout=QVBoxLayout(self); layout.addWidget(SubtitleLabel("Quản lý tài khoản game")); layout.addWidget(QLabel("Thêm, sửa hoặc xóa tài khoản. Password chỉ được lưu mã hóa trong Windows Credential Manager.")); self.search=LineEdit(); self.search.setPlaceholderText("Tìm theo tên tài khoản hoặc username / email"); self.search.addAction(QAction(FIF.SEARCH.icon(),"Tìm kiếm",self.search),QLineEdit.ActionPosition.LeadingPosition); self.search.textChanged.connect(self._filter_rows); search_row=QHBoxLayout(); search_row.setContentsMargins(0,0,0,_ui_px(10)); search_row.addStretch(1); search_row.addWidget(self.search,1); layout.addLayout(search_row); self.list=QVBoxLayout(); self.list.setAlignment(Qt.AlignmentFlag.AlignTop); self.list.setSpacing(_ui_px(8)); content=QWidget(); content.setLayout(self.list); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(content); layout.addWidget(scroll,1); control=QHBoxLayout(); add=PushButton("+ Thêm tài khoản"); add.clicked.connect(lambda:self.add_row()); control.addWidget(add); control.addStretch(); self.show=FullRowCheckBox("Hiện password"); self.show.stateChanged.connect(self.toggle_password); control.addWidget(self.show); layout.addLayout(control); actions=QHBoxLayout(); actions.addStretch(); cancel=PushButton("Hủy"); cancel.clicked.connect(self.reject); save=PrimaryPushButton("Lưu thay đổi"); save.clicked.connect(self.validate); actions.addWidget(cancel); actions.addWidget(save); layout.addLayout(actions)
        for profile in profiles:
            if profile.mode != ProfileMode.MANAGED: continue
            username=password=""
            try:
                from ik_chrome_auto.credential_store import WindowsCredentialStore
                credential=WindowsCredentialStore().load(profile.id)
                if credential: username,password=credential.username,credential.password
            except Exception: pass
            self.add_row(profile.id,username,password)
        if not self.rows: self.add_row()
        self._filter_rows()
    def add_row(self,profile_id:str|None=None,username_value:str="",password_value:str="")->None:
        row=CardWidget(); row.setFixedHeight(_ui_px(104)); row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed); layout=QVBoxLayout(row); layout.setContentsMargins(_ui_px(12),_ui_px(8),_ui_px(12),_ui_px(8)); layout.setSpacing(_ui_px(7)); header=QHBoxLayout(); header.addWidget(StrongBodyLabel(f"Tài khoản {len(self.rows)+1:02d}")); header.addStretch(); remove=PushButton("×"); remove.setToolTip("Xóa tài khoản"); remove.setFixedSize(_ui_px(32),_ui_px(28)); header.addWidget(remove); layout.addLayout(header); fields=QHBoxLayout(); fields.setSpacing(_ui_px(8)); username=LineEdit(); username.addAction(QAction(FIF.PEOPLE.icon(),"Username",username),QLineEdit.ActionPosition.LeadingPosition); username.setPlaceholderText("Username / email"); username.setText(username_value); username_box=QVBoxLayout(); username_box.setContentsMargins(0,0,0,0); username_box.setSpacing(_ui_px(2)); username_box.addWidget(username); warning=QLabel(" "); warning.setStyleSheet(f"color:transparent;background:transparent;font-size:{_ui_px(10)}px;"); warning.setFixedHeight(_ui_px(14)); username_box.addWidget(warning); password=PasswordLineEdit(); password.addAction(QAction(FIF.FINGERPRINT.icon(),"Password",password),QLineEdit.ActionPosition.LeadingPosition); password.setPlaceholderText("Password"); password.setText(password_value); fields.addLayout(username_box,1); fields.addWidget(password,1,Qt.AlignmentFlag.AlignTop); layout.addLayout(fields); remove.clicked.connect(lambda:self.remove_row(row)); username.textChanged.connect(self._filter_rows); username.textChanged.connect(self._update_username_validation); self.rows.append((profile_id,username,password,row)); self._username_warnings[username]=warning; self.list.addWidget(row); self._filter_rows(); self._update_username_validation()
    def remove_row(self,row:QWidget)->None:
        removed = [item for item in self.rows if item[3] is row]; self.rows=[item for item in self.rows if item[3] is not row];
        for _, username, _, _ in removed: self._username_warnings.pop(username, None)
        self.list.removeWidget(row); row.deleteLater(); self._filter_rows(); self._update_username_validation()
    def _filter_rows(self) -> None:
        query = self.search.text().strip().casefold()
        for index, (_profile_id, username, _password, row) in enumerate(self.rows, start=1):
            haystack = f"tài khoản {index:02d} {username.text()}".casefold()
            row.setVisible(not query or query in haystack)
    def _update_username_validation(self, *_args: object) -> bool:
        values:dict[str,int]={}
        for _, username, _, row in self.rows:
            value=username.text().strip().casefold()
            if value: values[value]=values.get(value,0)+1
        valid=True
        for _, username, _, _ in self.rows:
            raw_value = username.text()
            has_whitespace = any(character.isspace() for character in raw_value)
            duplicate=bool((value:=raw_value.strip().casefold()) and values.get(value,0)>1)
            warning=self._username_warnings.get(username)
            if warning:
                has_error = has_whitespace or duplicate
                warning.setText(
                    "Username không được có khoảng trắng."
                    if has_whitespace
                    else "Username đã tồn tại ở một tài khoản khác."
                    if duplicate
                    else " "
                )
                warning.setStyleSheet(
                    f"color:#dc2626;background:transparent;font-size:{_ui_px(10)}px;"
                    if has_error
                    else f"color:transparent;background:transparent;font-size:{_ui_px(10)}px;"
                )
            row.setFixedHeight(_ui_px(122 if has_whitespace or duplicate else 104))
            valid=valid and not has_whitespace and not duplicate
        return valid
    def toggle_password(self,_state:int)->None:
        mode=LineEdit.EchoMode.Normal if self.show.isChecked() else LineEdit.EchoMode.Password
        for _,_,password,_ in self.rows: password.setEchoMode(mode)
    def validate(self)->None:
        values=self.accounts()
        if not values: QMessageBox.warning(self,"Thiếu thông tin","Hãy giữ hoặc thêm ít nhất một tài khoản."); return
        if any(not username or not password for _,username,password in values): QMessageBox.warning(self,"Thiếu thông tin","Mỗi tài khoản cần có đủ username và password."); return
        if not self._update_username_validation(): QMessageBox.warning(self,"Username không hợp lệ","Username không được có khoảng trắng và phải khác nhau giữa các tài khoản."); return
        self.accept()
    def accounts(self)->list[tuple[str|None,str,str]]: return [(profile_id,username.text().strip(),password.text()) for profile_id,username,password,_ in self.rows]


def run_dashboard(config_path:Path)->None:
    capture_launch_terminal_window()
    app = QApplication.instance()
    if app is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication([])
    if not app.property("ikAutoCompactUiApplied"):
        compact_font = app.font()
        current_size = compact_font.pointSizeF()
        if current_size > 0:
            compact_font.setPointSizeF(_ui_pt(current_size))
            app.setFont(compact_font)
        app.setProperty("ikAutoCompactUiApplied", True)
    app.setApplicationName("IK Auto")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "ik_auto.ico")))
    dashboard = Dashboard(config_path)
    dashboard.show()
    if dashboard.windowHandle() is not None:
        dashboard.windowHandle().screenChanged.connect(dashboard._on_screen_changed)
    QTimer.singleShot(500, minimize_launch_console)
    app.exec()
