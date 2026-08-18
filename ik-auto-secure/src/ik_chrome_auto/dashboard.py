"""Modern Fluent desktop dashboard."""
from __future__ import annotations

import json
import os
import queue
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, CheckBox, ComboBox, FluentIcon as FIF, LineEdit, PasswordLineEdit, PrimaryPushButton, PrimaryToolButton, PushButton, StrongBodyLabel, SubtitleLabel, ToolButton

from ik_chrome_auto.config import ensure_data_dirs, load_config, save_config, unique_profile_id
from ik_chrome_auto.interaction import format_coordinate
from ik_chrome_auto.models import Auto2048Speed, CommandKind, ProfileConfig, ProfileMode, WorkerSnapshot, WorkerState
from ik_chrome_auto.runner import AUTO_2048_TIMINGS, MultiProfileRunner

SPEED_LABELS = {key: f"{value.label} ({value.move_delay_seconds:.2f}s)" for key, value in AUTO_2048_TIMINGS.items()}


def minimize_launch_console() -> None:
    if os.name != "nt" or os.environ.get("IK_AUTO_MINIMIZE_CONSOLE") != "1":
        return
    try:
        import ctypes
        window = ctypes.windll.kernel32.GetConsoleWindow()
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
        self._auto_arrange_targets: set[str] | None = None
        self._auto_arrange_states: dict[str, WorkerState] = {}
        self._auto_arrange_deadline = 0.0
        self.drag_visible = True
        self.scrollbars_visible = True
        self._last_resources = self._last_trim = 0.0
        self._build()
        self._draw_rows()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(200)

    def _build(self) -> None:
        self.setWindowTitle("IK Auto — Browser Control")
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
        accounts = self._card(); al = QVBoxLayout(accounts); al.addWidget(StrongBodyLabel("Tài khoản Chrome")); al.addWidget(self._muted("Mỗi tài khoản có một phiên browser riêng, lưu cục bộ.")); actions = QHBoxLayout(); manage = PrimaryPushButton("Quản lý tài khoản"); manage.clicked.connect(self._manage_accounts); open_all = PushButton("Mở tất cả"); open_all.clicked.connect(self._open_all_and_arrange); stop_all = PushButton("Dừng tất cả"); stop_all.clicked.connect(self.runner.stop_all); actions.addWidget(manage); actions.addWidget(open_all); actions.addWidget(stop_all); al.addLayout(actions); ll.addWidget(accounts)
        arrange_card = self._card(); acl = QVBoxLayout(arrange_card); acl.addWidget(StrongBodyLabel("Sắp xếp cửa sổ")); acl.addWidget(self._muted("Chọn số cửa sổ trên mỗi hàng rồi áp dụng cho Chrome đang mở.")); row = QHBoxLayout(); row.addWidget(QLabel("Số cửa sổ / hàng")); row.addStretch(); self.windows_per_row = ComboBox(); self.windows_per_row.addItems(["2","3","4","5","6"]); self.windows_per_row.setCurrentText(str(self.config.browser.windows_per_row)); self.windows_per_row.currentTextChanged.connect(self._apply_windows_per_row); row.addWidget(self.windows_per_row); acl.addLayout(row); apply = PrimaryPushButton("Áp dụng & sắp xếp"); apply.clicked.connect(self._arrange); acl.addWidget(apply); tools = QHBoxLayout(); self.drag = PushButton("Ẩn nút kéo"); self.drag.clicked.connect(self._toggle_drag); self.scrollbars = PushButton("Ẩn thanh cuộn"); self.scrollbars.clicked.connect(self._toggle_scrollbars); self.pin = CheckBox("Luôn nổi trên các cửa sổ khác"); self.pin.stateChanged.connect(lambda _s: self.runner.set_all_topmost(self.pin.isChecked())); tools.addWidget(self.drag); tools.addWidget(self.scrollbars); acl.addLayout(tools); acl.addWidget(self.pin); ll.addWidget(arrange_card)
        automation = self._card(); au = QVBoxLayout(automation); au.addWidget(StrongBodyLabel("Tự động hóa")); au.addWidget(self._muted("Sync thao tác · Auto 2048")); sync = QHBoxLayout(); self.master = ComboBox(); sync.addWidget(self.master,1); self.sync = PushButton("Bật sync chuột"); self.sync.clicked.connect(self._toggle_sync); sync.addWidget(self.sync); au.addLayout(sync); self.sync_status = self._muted("Sync đang tắt"); au.addWidget(self.sync_status); speed = QHBoxLayout(); self.speed = ComboBox(); self.speed.addItems(list(SPEED_LABELS.values())); self.speed.setCurrentText(SPEED_LABELS[self.config.auto_2048_speed]); save_speed = PushButton("Lưu tốc độ"); save_speed.clicked.connect(self._save_speed); speed.addWidget(self.speed,1); speed.addWidget(save_speed); au.addLayout(speed); ll.addWidget(automation); ll.addStretch()
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(8); body.addWidget(right,1)
        metrics = QHBoxLayout(); self.total, self.opened, self.ram, self.cpu = QLabel("0"), QLabel("0"), QLabel("0 MB"), QLabel("0.0%");
        for label, value in (("Tổng profile",self.total),("Đang mở",self.opened),("RAM Chrome",self.ram),("CPU Chrome",self.cpu)):
            card = self._card(); ml=QVBoxLayout(card); ml.addWidget(self._muted(label)); value.setStyleSheet("font-size:19px;font-weight:700;"); ml.addWidget(value); metrics.addWidget(card)
        rl.addLayout(metrics)
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
    def _muted(text: str) -> QLabel:
        label=QLabel(text); label.setStyleSheet("color:#62758e; background:transparent;"); label.setWordWrap(True); return label

    def _draw_rows(self) -> None:
        while self.table_layout.count():
            item=self.table_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self.rows.clear(); ids=[p.id for p in self.config.profiles]; self.master.clear(); self.master.addItems(ids)
        for profile in self.config.profiles:
            card=self._card(); layout=QVBoxLayout(card); top=QHBoxLayout(); top.addWidget(StrongBodyLabel(profile.name)); top.addWidget(self._muted(f"{profile.id} · {profile.mode.value}")); top.addStretch(); badge=QLabel("Đã dừng"); badge.setStyleSheet("background:#f1f5f9;color:#475569;border-radius:10px;padding:3px 8px;"); top.addWidget(badge); layout.addLayout(top); status=self._muted("Đã dừng"); resource=self._muted("—"); details=QHBoxLayout(); details.addWidget(status,1); details.addWidget(resource); layout.addLayout(details); buttons=QHBoxLayout(); open_btn=PrimaryPushButton("Mở"); open_btn.clicked.connect(lambda _=False,pid=profile.id:self.runner.submit(pid,CommandKind.OPEN)); buttons.addWidget(open_btn); auto=PushButton("Auto 2048"); auto.clicked.connect(lambda _=False,pid=profile.id:self._toggle_auto(pid)); buttons.addWidget(auto); shot=PushButton("Ảnh"); shot.clicked.connect(lambda _=False,pid=profile.id:self.runner.submit(pid,CommandKind.SCREENSHOT)); buttons.addWidget(shot); inspect=PushButton("Đo"); inspect.clicked.connect(lambda _=False,pid=profile.id:self._toggle_inspector(pid)); buttons.addWidget(inspect); buttons.addStretch(); delete=self._icon_button(FIF.DELETE,"Xóa profile"); delete.clicked.connect(lambda _=False,pid=profile.id:self._remove_profile(pid)); buttons.addWidget(delete); layout.addLayout(buttons); index=len(self.rows); self.table_layout.addWidget(card, index // 2, index % 2); self.rows[profile.id]=ProfileRow(status,resource,badge,inspect,auto)
        self.table_layout.setRowStretch((len(self.rows) + 1) // 2, 1)

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
    def _toggle_sync(self) -> None:
        if self.runner.sync_enabled: self.runner.disable_sync(); self.sync.setText("Bật sync chuột"); self.sync_status.setText("Sync đang tắt"); return
        master=self.master.currentText(); opened=[p.id for p in self.config.profiles if self.runner.has_open_session(p.id)]
        if master not in opened or len(opened)<2: self._warning("Chưa đủ profile","Hãy mở master và ít nhất một follower trước khi bật sync."); return
        self.runner.enable_sync(master); self.sync.setText("Tắt sync chuột"); self.sync_status.setText(f"MASTER: {master} → {len(opened)-1} follower")
    def _save_speed(self) -> None:
        speed=next((key for key,label in SPEED_LABELS.items() if label==self.speed.currentText()),Auto2048Speed.BALANCED); self.runner.set_auto_2048_speed(speed); self.config.auto_2048_speed=speed; save_config(self.config)
    def _toggle_auto(self,pid:str) -> None:
        row=self.rows[pid]
        if pid in self.auto_profiles: self.auto_profiles.remove(pid); row.auto.setText("Auto 2048"); self.runner.submit(pid,CommandKind.STOP_2048)
        else: self.auto_profiles.add(pid); row.auto.setText("Dừng 2048"); self.runner.submit(pid,CommandKind.START_2048)
    def _toggle_inspector(self,pid:str) -> None:
        if self.inspecting_profile_id==pid: self.runner.set_inspector(pid,False); self.rows[pid].inspect.setText("Đo"); self.inspecting_profile_id=None; return
        if not self.runner.has_open_session(pid): self._warning("Profile chưa mở","Hãy bấm Mở profile trước khi bật đo tọa độ."); return
        if self.inspecting_profile_id: self.runner.set_inspector(self.inspecting_profile_id,False); self.rows[self.inspecting_profile_id].inspect.setText("Đo")
        self.inspecting_profile_id=pid; self.runner.set_inspector(pid,True); self.rows[pid].inspect.setText("Tắt đo"); self.coordinate.setText(f"[{pid}] Click vào game để lấy tọa độ…")
    def _manage_accounts(self) -> None:
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
        profile=self.config.profile(pid)
        if QMessageBox.question(self,"Bỏ profile",f"Bỏ '{profile.name}' khỏi dashboard?\n\nCredential mã hóa cũng sẽ bị xóa.")!=QMessageBox.StandardButton.Yes: return
        worker=self.runner.workers.get(pid)
        if worker: worker.shutdown()
        try:
            from ik_chrome_auto.credential_store import WindowsCredentialStore
            WindowsCredentialStore().delete(pid)
        except Exception as error: self._append_log(str(error))
        self.config.profiles=[p for p in self.config.profiles if p.id!=pid]; save_config(self.config); self.runner.sync_profiles(); self._draw_rows()
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
                    row.status.setText(snap.message); text,bg,fg=self._state(snap.state); row.badge.setText(text); row.badge.setStyleSheet(f"background:{bg};color:{fg};border-radius:10px;padding:3px 8px;")
                    if snap.state==WorkerState.STOPPED or "2048" in snap.message: self.auto_profiles.discard(snap.profile_id); row.auto.setText("Auto 2048")
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
        row=CardWidget(); row.setFixedHeight(104); row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed); layout=QVBoxLayout(row); layout.setContentsMargins(12,8,12,8); layout.setSpacing(7); header=QHBoxLayout(); header.addWidget(StrongBodyLabel(f"Tài khoản {len(self.rows)+1:02d}")); header.addStretch(); remove=PushButton("×"); remove.setToolTip("Xóa tài khoản"); remove.setFixedSize(32,28); header.addWidget(remove); layout.addLayout(header); fields=QHBoxLayout(); fields.setSpacing(8); username=LineEdit(); username.setPlaceholderText("Username / email"); username.setText(username_value); password=PasswordLineEdit(); password.setPlaceholderText("Password"); password.setText(password_value); fields.addWidget(username); fields.addWidget(password); layout.addLayout(fields); remove.clicked.connect(lambda:self.remove_row(row)); self.rows.append((profile_id,username,password,row)); self.list.addWidget(row)
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
    app=QApplication.instance() or QApplication([]); app.setApplicationName("IK Auto"); dashboard=Dashboard(config_path); dashboard.show(); QTimer.singleShot(250,minimize_launch_console); app.exec()
