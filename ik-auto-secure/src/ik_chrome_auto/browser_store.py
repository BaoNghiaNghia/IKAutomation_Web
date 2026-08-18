"""SQLite development store for browser-profile commands and event projections."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from ik_chrome_auto.browser_control import BrowserCommand, ProfileRunState, ProfileSnapshot


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    accepted: bool
    duplicate: bool


class SqliteBrowserControlStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS browser_profiles (browser_profile_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (command_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, worker_id TEXT NOT NULL, profile_ids TEXT NOT NULL, expected_version INTEGER NOT NULL, payload TEXT NOT NULL, accepted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL, command_id TEXT NOT NULL, occurred_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profile_events (event_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, run_id TEXT NOT NULL, browser_profile_id TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run_id,browser_profile_id,sequence));
            CREATE TABLE IF NOT EXISTS profile_snapshots (run_id TEXT NOT NULL, browser_profile_id TEXT NOT NULL, worker_id TEXT NOT NULL, sequence INTEGER NOT NULL, state TEXT NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,browser_profile_id));
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def register_profile(self, browser_profile_id: str, worker_id: str, *, version: int = 0) -> None:
        with self._connection() as db:
            db.execute("INSERT INTO browser_profiles VALUES(?,?,?) ON CONFLICT(browser_profile_id) DO UPDATE SET worker_id=excluded.worker_id,version=excluded.version", (browser_profile_id, worker_id, version))

    def submit_command(self, command: BrowserCommand, now: datetime) -> CommandReceipt:
        command.validate(now)
        with self._connection() as db:
            duplicate = db.execute("SELECT command_id FROM commands WHERE idempotency_key=?", (command.idempotency_key,)).fetchone()
            if duplicate:
                return CommandReceipt(str(duplicate["command_id"]), False, True)
            for profile_id in command.browser_profile_ids:
                row = db.execute("SELECT worker_id,version FROM browser_profiles WHERE browser_profile_id=?", (profile_id,)).fetchone()
                if row is None or row["worker_id"] != command.worker_id:
                    raise PermissionError("Browser profile không thuộc worker")
                if row["version"] != command.expected_version:
                    raise RuntimeError("Browser profile version không khớp")
            at = now.astimezone(UTC).isoformat()
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?)", (command.command_id, command.idempotency_key, command.worker_id, json.dumps(command.browser_profile_ids), command.expected_version, json.dumps(command.payload), at))
            db.execute("INSERT INTO audit_logs(actor_id,action,command_id,occurred_at) VALUES(?,?,?,?)", (command.actor_id, command.kind.value, command.command_id, at))
            return CommandReceipt(command.command_id, True, False)

    def record_snapshot(self, snapshot: ProfileSnapshot) -> bool:
        snapshot.validate()
        with self._connection() as db:
            owner = db.execute("SELECT worker_id FROM browser_profiles WHERE browser_profile_id=?", (snapshot.browser_profile_id,)).fetchone()
            if owner is None or owner["worker_id"] != snapshot.worker_id:
                raise PermissionError("Worker không sở hữu browser profile")
            if db.execute("SELECT 1 FROM profile_events WHERE event_id=?", (snapshot.event_id,)).fetchone():
                return False
            latest = db.execute("SELECT sequence FROM profile_snapshots WHERE run_id=? AND browser_profile_id=?", (snapshot.run_id, snapshot.browser_profile_id)).fetchone()
            values = (snapshot.event_id, snapshot.worker_id, snapshot.run_id, snapshot.browser_profile_id, snapshot.sequence, snapshot.state.value, snapshot.occurred_at.astimezone(UTC).isoformat(), json.dumps(snapshot.payload))
            db.execute("INSERT INTO profile_events VALUES(?,?,?,?,?,?,?,?)", values)
            if latest is not None and snapshot.sequence <= latest["sequence"]:
                return False
            db.execute(
                "INSERT INTO profile_snapshots VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,browser_profile_id) DO UPDATE SET worker_id=excluded.worker_id,sequence=excluded.sequence,state=excluded.state,occurred_at=excluded.occurred_at,payload=excluded.payload",
                (values[2], values[3], values[1], values[4], values[5], values[6], values[7]),
            )
            return True
