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
from PySide6.QtGui import QAction, QCloseEvent, QColor, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, CheckBox, ComboBox, FluentIcon as FIF, LineEdit, PasswordLineEdit, PrimaryPushButton, PrimaryToolButton, PushButton, StrongBodyLabel, SubtitleLabel, ToolButton

from ik_chrome_auto.config import ensure_data_dirs, load_config, save_config, unique_profile_id
from ik_chrome_auto.interaction import format_coordinate
from ik_chrome_auto.models import Auto2048Speed, CommandKind, ProfileConfig, ProfileMode, WorkerSnapshot, WorkerState
from ik_chrome_auto.runner import AUTO_2048_TIMINGS, MultiProfileRunner
from ik_chrome_auto.two_factor import TwoFactorEnrollment, TwoFactorService

# Windows Hello implementation remains available in windows_auth.py, but is
# intentionally disabled while Google Authenticator is the primary verifier.
WINDOWS_HELLO_ENABLED = False

SPEED_LABELS = {key: f"{value.label} ({value.move_delay_seconds:.2f}s)" for key, value in AUTO_2048_TIMINGS.items()}
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
    inspect: PushButton
    auto: PushButton
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
        self.inspecting_profile_id: str | None = None
        self.auto_profiles: set[str] = set()
        self.farm_profiles: set[str] = set()
        self._auto_arrange_targets: set[str] | None = None
        self._auto_arrange_states: dict[str, WorkerState] = {}
        self._auto_arrange_deadline = 0.0
        self.drag_visible = True
        self.scrollbars_visible = False
        self._last_resources = self._last_trim = 0.0
        self._build()
        self._draw_rows()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    def _build(self) -> None:
        self.setWindowTitle("IK Auto — Browser Control")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "ik_auto.ico")))
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.setGeometry(screen.right() - screen.width() // 2 + 1, screen.bottom() - screen.height() // 2 + 1, screen.width() // 2, screen.height() // 2)
        self.setMinimumSize(780, 520)
        self.setStyleSheet("""
            Dashboard { background: #eef4fb; color: #172b4d; font-family: Inter, Segoe UI; font-size: 13px; }
            CardWidget { background: #ffffff; border: 1px solid #dce6f3; border-radius: 14px; }
            QLabel { background: transparent; color: #172b4d; }
            QScrollArea, QScrollArea > QWidget > QWidget { border: none; background: transparent; }
            QScrollBar:vertical { width: 6px; margin: 3px 1px; background: transparent; }
            QScrollBar::handle:vertical { min-height: 24px; border-radius: 3px; background: #b8c7d9; }
            QScrollBar::handle:vertical:hover { background: #8fa6c2; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { height: 6px; margin: 1px 3px; background: transparent; }
            QScrollBar::handle:horizontal { min-width: 24px; border-radius: 3px; background: #b8c7d9; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QTextEdit { background: #f5f8fc; border: 1px solid #dce6f3; border-radius: 9px; color: #52657d; }
        """)
        root = QVBoxLayout(self); root.setContentsMargins(14, 12, 14, 14); root.setSpacing(10)
        head = QHBoxLayout(); title = SubtitleLabel("IK Auto"); title.setStyleSheet("font-size:22px;font-weight:700;"); head.addWidget(title); head.addWidget(QLabel("Browser control")); head.addStretch(); secure = QLabel("●  Local & secure"); secure.setStyleSheet("background:#d9f7e8;color:#087443;border-radius:12px;padding:5px 10px;font-weight:600;"); head.addWidget(secure); root.addLayout(head)
        body = QHBoxLayout(); root.addLayout(body, 1)
        left = QWidget(); left.setMinimumWidth(340); left.setMaximumWidth(390); ll = QVBoxLayout(left); ll.setContentsMargins(0,0,0,0); ll.setSpacing(8); body.addWidget(left)
        overview = self._card(); overview.setMaximumHeight(108); ol = QGridLayout(overview); ol.setContentsMargins(12,10,12,10); ol.setHorizontalSpacing(12); ol.setVerticalSpacing(6)
        self.total, self.opened, self.ram, self.cpu = QLabel("0"), QLabel("0"), QLabel("0 MB"), QLabel("0.0%")
        for index, (label, value) in enumerate((("Tổng profile",self.total),("Đang mở",self.opened),("RAM Chrome",self.ram),("CPU Chrome",self.cpu))):
            cell = QWidget(); cl = QVBoxLayout(cell); cl.setContentsMargins(0,0,0,0); cl.setSpacing(1); caption = self._muted(label); caption.setStyleSheet("color:#62758e;background:transparent;font-size:11px;"); value.setStyleSheet("font-size:16px;font-weight:700;"); cl.addWidget(caption); cl.addWidget(value); ol.addWidget(cell, index // 2, index % 2)
        ll.addWidget(overview)
        accounts = self._card(); al = QVBoxLayout(accounts); al.addWidget(StrongBodyLabel("Tài khoản Chrome")); al.addWidget(self._muted("Mỗi tài khoản có một phiên browser riêng, lưu cục bộ.")); actions = QHBoxLayout(); actions.setSpacing(7); manage = PushButton("Quản lý tài khoản"); manage.setFixedSize(140, 34); manage.setStyleSheet("QPushButton { background:#ffffff; border:1px solid #8ad7d9; border-radius:8px; color:#087f8c; } QPushButton:hover { background:#effcfb; border-color:#0ea5a5; }"); manage.clicked.connect(self._manage_accounts); open_all = PrimaryPushButton("Mở tất cả"); open_all.setFixedSize(100, 34); open_all.setStyleSheet("QPushButton { background:#3b82f6; border:1px solid #3b82f6; border-radius:8px; color:#ffffff; } QPushButton:hover { background:#2563eb; border-color:#2563eb; }"); open_all.clicked.connect(self._open_all_and_arrange); stop_all = self._icon_button(FIF.CLOSE, "Dừng tất cả profile đang chạy"); stop_all.setIcon(FIF.CLOSE.icon(color=QColor("#ffffff"))); stop_all.setStyleSheet("QToolButton { background:#ef4444; border:1px solid #ef4444; border-radius:8px; color:#ffffff; } QToolButton:hover { background:#dc2626; border-color:#dc2626; }"); stop_all.clicked.connect(self.runner.stop_all); actions.addWidget(manage); actions.addWidget(open_all); actions.addWidget(stop_all); actions.addStretch(); al.addLayout(actions); ll.addWidget(accounts)
        arrange_card = self._card(); acl = QVBoxLayout(arrange_card); acl.addWidget(StrongBodyLabel("Sắp xếp cửa sổ")); acl.addWidget(self._muted("Chọn số cửa sổ trên mỗi hàng rồi áp dụng cho Chrome đang mở.")); row = QHBoxLayout(); row.addWidget(QLabel("Số cửa sổ / hàng")); row.addStretch(); self.windows_per_row = ComboBox(); self.windows_per_row.addItems(["2","3","4","5","6"]); self.windows_per_row.setCurrentText(str(self.config.browser.windows_per_row)); self.windows_per_row.currentTextChanged.connect(self._apply_windows_per_row); row.addWidget(self.windows_per_row); acl.addLayout(row); apply = PrimaryPushButton("Áp dụng & sắp xếp"); apply.clicked.connect(self._arrange); acl.addWidget(apply); tools = QHBoxLayout(); self.drag = PushButton("Ẩn nút kéo"); self.drag.clicked.connect(self._toggle_drag); self.scrollbars = PushButton("Hiện thanh cuộn"); self.scrollbars.clicked.connect(self._toggle_scrollbars); self.pin = CheckBox("Luôn nổi trên các cửa sổ khác"); self.pin.stateChanged.connect(lambda _s: self.runner.set_all_topmost(self.pin.isChecked())); tools.addWidget(self.drag); tools.addWidget(self.scrollbars); acl.addLayout(tools); acl.addWidget(self.pin); ll.addWidget(arrange_card)
        automation = self._card(); au = QVBoxLayout(automation); au.addWidget(StrongBodyLabel("Tự động hóa")); au.addWidget(self._muted("Sync thao tác · Auto 2048")); sync = QHBoxLayout(); self.master = ComboBox(); sync.addWidget(self.master,1); self.sync = PushButton("Bật sync chuột"); self.sync.clicked.connect(self._toggle_sync); sync.addWidget(self.sync); au.addLayout(sync); self.sync_status = self._muted("Sync đang tắt"); au.addWidget(self.sync_status); speed = QHBoxLayout(); self.speed = ComboBox(); self.speed.addItems(list(SPEED_LABELS.values())); self.speed.setCurrentText(SPEED_LABELS[self.config.auto_2048_speed]); save_speed = PushButton("Lưu tốc độ"); save_speed.clicked.connect(self._save_speed); speed.addWidget(self.speed,1); speed.addWidget(save_speed); au.addLayout(speed); ll.addWidget(automation); ll.addStretch()
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(8); body.addWidget(right,1)
        progress = self._card(); pl = QVBoxLayout(progress); ph = QHBoxLayout(); ph.addWidget(StrongBodyLabel("Tiến trình profile")); ph.addStretch(); self.open_badge = QLabel("0 đang mở"); self.open_badge.setStyleSheet("background:#e2edff;color:#2767bd;border-radius:10px;padding:3px 8px;"); ph.addWidget(self.open_badge); pl.addLayout(ph); self.table_layout = QGridLayout(); self.table_layout.setSpacing(8); self.table_layout.setColumnStretch(0, 1); self.table_layout.setColumnStretch(1, 1); content=QWidget(); content.setLayout(self.table_layout); self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setWidget(content); pl.addWidget(self.scroll,1); rl.addWidget(progress,1)
        foot=QHBoxLayout(); coords=self._card(); cl=QVBoxLayout(coords); ch=QHBoxLayout(); ch.addWidget(StrongBodyLabel("Lấy tọa độ")); ch.addStretch(); bjson=PushButton("JSON"); bjson.clicked.connect(self._copy_json); bxy=PushButton("Copy x,y"); bxy.clicked.connect(self._copy_xy); ch.addWidget(bjson); ch.addWidget(bxy); cl.addLayout(ch); self.coordinate= self._muted("Chưa đo tọa độ"); cl.addWidget(self.coordinate); foot.addWidget(coords,2); logs=self._card(); logl=QVBoxLayout(logs); logl.addWidget(StrongBodyLabel("Nhật ký gần nhất")); self.log=QTextEdit(); self.log.setReadOnly(True); self.log.setFixedHeight(86); logl.addWidget(self.log); foot.addWidget(logs,3); rl.addLayout(foot)

    @staticmethod
    def _card() -> CardWidget: return CardWidget()
    @staticmethod
    def _icon_button(icon: FIF, tooltip: str, primary: bool = False) -> ToolButton:
        button = PrimaryToolButton() if primary else ToolButton()
        button.setIcon(icon)
        button.setToolTip(tooltip)
        button.setFixedSize(34, 34)
        return button

    @staticmethod
    def _compact_profile_button(button: PushButton) -> PushButton:
        """Keep per-profile controls compact without shrinking global actions."""
        button.setFixedHeight(29)
        button.setMinimumWidth(0)
        return button
    @staticmethod
    def _muted(text: str) -> QLabel:
        label=QLabel(text); label.setStyleSheet("color:#62758e; background:transparent;"); label.setWordWrap(True); return label

    def _draw_rows(self) -> None:
        while self.table_layout.count():
            item=self.table_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.rows.clear(); self.master.clear()
        for profile in self.config.profiles:
            self.master.addItem(self._masked_profile_username(profile), profile.id)
        for profile in self.config.profiles:
            card=self._card(); layout=QVBoxLayout(card); top=QHBoxLayout(); top.addWidget(StrongBodyLabel(profile.name)); top.addWidget(self._muted(f"{profile.id} · {profile.mode.value}")); top.addStretch(); badge=QLabel("Đã dừng"); badge.setStyleSheet("background:#f1f5f9;color:#475569;border-radius:10px;padding:3px 8px;"); top.addWidget(badge); layout.addLayout(top); status=self._muted("Đã dừng"); resource=self._muted("—"); details=QHBoxLayout(); details.addWidget(status,1); details.addWidget(resource); layout.addLayout(details); buttons=QHBoxLayout(); buttons.setSpacing(5); open_btn=self._compact_profile_button(PrimaryPushButton("Mở")); open_btn.clicked.connect(lambda _=False,pid=profile.id:self.runner.submit(pid,CommandKind.OPEN)); buttons.addWidget(open_btn); farm=self._compact_profile_button(PushButton("Farm")); farm.clicked.connect(lambda _=False,pid=profile.id:self._toggle_farm(pid)); buttons.addWidget(farm); auto=self._compact_profile_button(PushButton("Auto 2048")); auto.clicked.connect(lambda _=False,pid=profile.id:self._toggle_auto(pid)); buttons.addWidget(auto); shot=self._compact_profile_button(PushButton("Ảnh")); shot.clicked.connect(lambda _=False,pid=profile.id:self.runner.submit(pid,CommandKind.SCREENSHOT)); buttons.addWidget(shot); inspect=self._compact_profile_button(PushButton("Đo")); inspect.clicked.connect(lambda _=False,pid=profile.id:self._toggle_inspector(pid)); buttons.addWidget(inspect); buttons.addStretch(); delete=self._icon_button(FIF.DELETE,"Xóa profile"); delete.setFixedSize(29,29); delete.clicked.connect(lambda _=False,pid=profile.id:self._remove_profile(pid)); buttons.addWidget(delete); layout.addLayout(buttons); index=len(self.rows); self.table_layout.addWidget(card, index // 2, index % 2); self.rows[profile.id]=ProfileRow(status,resource,badge,inspect,auto,farm,card)
        self.table_layout.setRowStretch((len(self.rows) + 1) // 2, 1)

    @staticmethod
    def _mask_username(username: str) -> str:
        value = username.strip()
        if not value:
            return "Chưa có username"
        visible = value[:6]
        return visible + "*" * max(3, len(value) - len(visible))

    def _masked_profile_username(self, profile: ProfileConfig) -> str:
        try:
            from ik_chrome_auto.credential_store import WindowsCredentialStore

            credential = WindowsCredentialStore().load(profile.id)
            if credential is not None:
                return self._mask_username(credential.username)
        except Exception:
            pass
        return self._mask_username(profile.name)

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
        self.drag_visible=not self.drag_visible; self.runner.set_all_drag_items_visible(self.drag_visible); self.drag.setText("Ẩn nút kéo" if self.drag_visible else "Hiện nút kéo")
    def _toggle_scrollbars(self) -> None:
        self.scrollbars_visible = not self.scrollbars_visible
        count = self.runner.set_all_scrollbars_visible(self.scrollbars_visible)
        self.scrollbars.setText("Ẩn thanh cuộn" if self.scrollbars_visible else "Hiện thanh cuộn")
        self._append_log(
            ("Đã hiện" if self.scrollbars_visible else "Đã ẩn")
            + f" thanh cuộn trên {count} Chrome đang mở"
        )
    @staticmethod
    def _set_profile_card_state(row: ProfileRow, state: WorkerState) -> None:
        if state in {WorkerState.STARTING, WorkerState.READY, WorkerState.RUNNING, WorkerState.COMPLETED}:
            row.card.setStyleSheet("CardWidget { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #dcfce7,stop:1 #ffffff); }")
        else:
            row.card.setStyleSheet("")
    def _toggle_sync(self) -> None:
        if self.runner.sync_enabled: self.runner.disable_sync(); self.sync.setText("Bật sync chuột"); self.sync_status.setText("Sync đang tắt"); return
        master=str(self.master.currentData() or ""); opened=[p.id for p in self.config.profiles if self.runner.has_open_session(p.id)]
        if master not in opened or len(opened)<2: self._warning("Chưa đủ profile","Hãy mở master và ít nhất một follower trước khi bật sync."); return
        self.runner.enable_sync(master); self.sync.setText("Tắt sync chuột"); self.sync_status.setText(f"MASTER: {master} → {len(opened)-1} follower")
    def _save_speed(self) -> None:
        speed=next((key for key,label in SPEED_LABELS.items() if label==self.speed.currentText()),Auto2048Speed.BALANCED); self.runner.set_auto_2048_speed(speed); self.config.auto_2048_speed=speed; save_config(self.config)
    def _toggle_auto(self,pid:str) -> None:
        row=self.rows[pid]
        if pid in self.auto_profiles: self.auto_profiles.remove(pid); row.auto.setText("Auto 2048"); self.runner.submit(pid,CommandKind.STOP_2048)
        else: self.auto_profiles.add(pid); row.auto.setText("Dừng 2048"); self.runner.submit(pid,CommandKind.START_2048)
    def _toggle_farm(self, pid: str) -> None:
        row = self.rows[pid]
        if pid in self.farm_profiles:
            self.farm_profiles.remove(pid); row.farm.setText("Farm"); self.runner.submit(pid, CommandKind.STOP_FARM)
        else:
            self.farm_profiles.add(pid); row.farm.setText("Dừng Farm"); self.runner.submit(pid, CommandKind.START_FARM)
    def _toggle_inspector(self,pid:str) -> None:
        if self.inspecting_profile_id==pid: self.runner.set_inspector(pid,False); self.rows[pid].inspect.setText("Đo"); self.inspecting_profile_id=None; return
        if not self.runner.has_open_session(pid): self._warning("Profile chưa mở","Hãy bấm Mở profile trước khi bật đo tọa độ."); return
        if self.inspecting_profile_id: self.runner.set_inspector(self.inspecting_profile_id,False); self.rows[self.inspecting_profile_id].inspect.setText("Đo")
        self.inspecting_profile_id=pid; self.runner.set_inspector(pid,True); self.rows[pid].inspect.setText("Tắt đo"); self.coordinate.setText(f"[{pid}] Click vào game để lấy tọa độ…")
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
                preview=username[:6]+("…" if len(username)>6 else ""); profile.name=f"Tài khoản {index:02d} · {preview}"; store.save(AccountCredential(pid,username,password)); updated.append(profile)
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
                snap=self.updates.get_nowait(); self._auto_arrange_states[snap.profile_id] = snap.state; row=self.rows.get(snap.profile_id)
                if row:
                    row.status.setText(snap.message); text,bg,fg=self._state(snap.state); row.badge.setText(text); row.badge.setStyleSheet(f"background:{bg};color:{fg};border-radius:10px;padding:3px 8px;"); self._set_profile_card_state(row,snap.state)
                    if snap.state==WorkerState.STOPPED or "2048" in snap.message: self.auto_profiles.discard(snap.profile_id); row.auto.setText("Auto 2048")
                    if snap.state in {WorkerState.STOPPED, WorkerState.ERROR} or "Đã dừng Auto Farm" in snap.message: self.farm_profiles.discard(snap.profile_id); row.farm.setText("Farm")
                self._append_log(f"[{snap.profile_id}] {snap.message}")
        except queue.Empty: pass
        try:
            while True:
                pid,event=self.coordinate_updates.get_nowait(); self.last_coordinate=(pid,event); self.coordinate.setText(format_coordinate(pid,event))
        except queue.Empty: pass
        self._finish_auto_arrange_if_ready()
        now=time.monotonic()
        if now-self._last_resources>=2:
            try:
                overview=self.runner.resource_overview(); self.total.setText(str(overview.total_profiles)); self.opened.setText(str(overview.opened_profiles)); self.open_badge.setText(f"{overview.opened_profiles} đang mở"); self.ram.setText(f"{overview.ram_bytes/1_048_576:.0f} MB"); self.cpu.setText(f"{overview.cpu_percent:.1f}%")
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
        self.resize(430, 590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(12)
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
        return pixmap.scaled(220, 220, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

    def _validate(self) -> None:
        if TwoFactorService.verify_code(self.enrollment.secret, self.code.text()):
            self.accept()
        else:
            QMessageBox.warning(self, "Mã chưa đúng", "Mã 6 số không hợp lệ. Hãy kiểm tra thời gian trên điện thoại rồi thử lại.")

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
        self.setFixedWidth(390)
        layout = QVBoxLayout(self); layout.setContentsMargins(26, 24, 26, 24); layout.setSpacing(12)
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
        self.resize(430, 420)
        layout=QVBoxLayout(self); layout.setContentsMargins(26,24,26,24); layout.setSpacing(12)
        layout.addWidget(SubtitleLabel("Lưu mã khôi phục ngay"))
        note=QLabel("Mỗi mã chỉ dùng một lần khi mất Google Authenticator. Các mã này sẽ không hiển thị lại."); note.setWordWrap(True); note.setStyleSheet("color:#b45309;background:transparent;"); layout.addWidget(note)
        values=QTextEdit(); values.setReadOnly(True); values.setPlainText("\n".join(codes)); values.setFixedHeight(220); layout.addWidget(values)
        actions=QHBoxLayout(); copy=PushButton("Sao chép"); copy.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(codes))); actions.addWidget(copy); actions.addStretch(); close=PrimaryPushButton("Đã lưu an toàn"); close.clicked.connect(self.accept); actions.addWidget(close); layout.addLayout(actions)


class AccountManagerDialog(QDialog):
    """Central CRUD form. Secrets only live in this dialog until saved to Windows Vault."""
    def __init__(self,parent:Dashboard,profiles:list[ProfileConfig])->None:
        super().__init__(parent); self.rows:list[tuple[str|None,LineEdit,PasswordLineEdit,QWidget]]=[]; self.setWindowTitle("Quản lý tài khoản game"); self.resize(720,520); layout=QVBoxLayout(self); layout.addWidget(SubtitleLabel("Quản lý tài khoản game")); layout.addWidget(QLabel("Thêm, sửa hoặc xóa tài khoản. Password chỉ được lưu mã hóa trong Windows Credential Manager.")); self.list=QVBoxLayout(); self.list.setAlignment(Qt.AlignmentFlag.AlignTop); self.list.setSpacing(8); content=QWidget(); content.setLayout(self.list); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(content); layout.addWidget(scroll,1); control=QHBoxLayout(); add=PushButton("+ Thêm tài khoản"); add.clicked.connect(lambda:self.add_row()); control.addWidget(add); control.addStretch(); self.show=CheckBox("Hiện password"); self.show.stateChanged.connect(self.toggle_password); control.addWidget(self.show); layout.addLayout(control); actions=QHBoxLayout(); actions.addStretch(); cancel=PushButton("Hủy"); cancel.clicked.connect(self.reject); save=PrimaryPushButton("Lưu thay đổi"); save.clicked.connect(self.validate); actions.addWidget(cancel); actions.addWidget(save); layout.addLayout(actions)
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
    def add_row(self,profile_id:str|None=None,username_value:str="",password_value:str="")->None:
        row=CardWidget(); row.setFixedHeight(104); row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed); layout=QVBoxLayout(row); layout.setContentsMargins(12,8,12,8); layout.setSpacing(7); header=QHBoxLayout(); header.addWidget(StrongBodyLabel(f"Tài khoản {len(self.rows)+1:02d}")); header.addStretch(); remove=PushButton("×"); remove.setToolTip("Xóa tài khoản"); remove.setFixedSize(32,28); header.addWidget(remove); layout.addLayout(header); fields=QHBoxLayout(); fields.setSpacing(8); username=LineEdit(); username.addAction(QAction(FIF.PEOPLE.icon(),"Username",username),QLineEdit.ActionPosition.LeadingPosition); username.setPlaceholderText("Username / email"); username.setText(username_value); password=PasswordLineEdit(); password.addAction(QAction(FIF.FINGERPRINT.icon(),"Password",password),QLineEdit.ActionPosition.LeadingPosition); password.setPlaceholderText("Password"); password.setText(password_value); fields.addWidget(username); fields.addWidget(password); layout.addLayout(fields); remove.clicked.connect(lambda:self.remove_row(row)); self.rows.append((profile_id,username,password,row)); self.list.addWidget(row)
    def remove_row(self,row:QWidget)->None:
        self.rows=[item for item in self.rows if item[3] is not row]; self.list.removeWidget(row); row.deleteLater()
    def toggle_password(self,_state:int)->None:
        mode=LineEdit.EchoMode.Normal if self.show.isChecked() else LineEdit.EchoMode.Password
        for _,_,password,_ in self.rows: password.setEchoMode(mode)
    def validate(self)->None:
        values=self.accounts()
        if not values: QMessageBox.warning(self,"Thiếu thông tin","Hãy giữ hoặc thêm ít nhất một tài khoản."); return
        if any(not username or not password for _,username,password in values): QMessageBox.warning(self,"Thiếu thông tin","Mỗi tài khoản cần có đủ username và password."); return
        self.accept()
    def accounts(self)->list[tuple[str|None,str,str]]: return [(profile_id,username.text().strip(),password.text()) for profile_id,username,password,_ in self.rows]


def run_dashboard(config_path:Path)->None:
    capture_launch_terminal_window(); app=QApplication.instance() or QApplication([]); app.setApplicationName("IK Auto"); app.setWindowIcon(QIcon(str(Path(__file__).with_name("assets") / "ik_auto.ico"))); dashboard=Dashboard(config_path); dashboard.show(); QTimer.singleShot(500,minimize_launch_console); app.exec()
