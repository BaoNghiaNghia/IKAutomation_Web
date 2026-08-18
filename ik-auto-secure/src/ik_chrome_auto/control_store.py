"""Durable control-plane storage.

SQLite is intentionally used for local development and contract tests. The
public methods form the repository boundary to be implemented by PostgreSQL in
deployment; no Agent needs database credentials.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from ik_chrome_auto.web_control import CommandEnvelope, DeviceRunState, DeviceSnapshot


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    accepted: bool
    duplicate: bool


class SqliteControlStore:
    """Transactional command, audit and sequenced-event store for one control plane."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                  device_id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS commands (
                  command_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  kind TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  actor_role TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  device_ids_json TEXT NOT NULL,
                  expected_version INTEGER NOT NULL,
                  requested_at TEXT NOT NULL,
                  deadline_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  accepted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  actor_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  entity_id TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  detail_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_events (
                  event_id TEXT PRIMARY KEY,
                  agent_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  device_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  UNIQUE(run_id, device_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS device_snapshots (
                  run_id TEXT NOT NULL,
                  device_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  state TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  PRIMARY KEY (run_id, device_id)
                );
                """
            )

    def register_device(self, device_id: str, agent_id: str, *, version: int = 0) -> None:
        if not device_id or not agent_id or version < 0:
            raise ValueError("device registration không hợp lệ")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO devices(device_id, agent_id, version) VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET agent_id=excluded.agent_id, version=excluded.version
                """,
                (device_id, agent_id, version),
            )

    def submit_command(self, command: CommandEnvelope, now: datetime) -> CommandReceipt:
        command.validate(now)
        accepted_at = now.astimezone(UTC).isoformat()
        with self._connection() as connection:
            self._assert_device_scope(connection, command)
            existing = connection.execute(
                "SELECT command_id FROM commands WHERE idempotency_key = ?",
                (command.idempotency_key,),
            ).fetchone()
            if existing is not None:
                return CommandReceipt(str(existing["command_id"]), False, True)
            connection.execute(
                """
                INSERT INTO commands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.command_id,
                    command.idempotency_key,
                    command.kind.value,
                    command.actor_id,
                    command.actor_role.value,
                    command.agent_id,
                    json.dumps(command.device_ids),
                    command.expected_version,
                    command.requested_at.astimezone(UTC).isoformat(),
                    command.deadline_at.astimezone(UTC).isoformat(),
                    json.dumps(command.payload, sort_keys=True),
                    accepted_at,
                ),
            )
            self._audit(connection, command.actor_id, command.kind.value, command.command_id, accepted_at, command.payload)
            return CommandReceipt(command.command_id, True, False)

    def record_snapshot(self, snapshot: DeviceSnapshot) -> bool:
        """Insert an event once and advance the materialized snapshot only by sequence."""
        snapshot.validate()
        occurred_at = snapshot.occurred_at.astimezone(UTC).isoformat()
        payload = json.dumps(snapshot.payload, sort_keys=True)
        with self._connection() as connection:
            known = connection.execute(
                "SELECT agent_id FROM devices WHERE device_id = ?", (snapshot.device_id,)
            ).fetchone()
            if known is None or known["agent_id"] != snapshot.agent_id:
                raise PermissionError("Agent không sở hữu device của snapshot")
            duplicate = connection.execute(
                "SELECT 1 FROM device_events WHERE event_id = ?", (snapshot.event_id,)
            ).fetchone()
            if duplicate is not None:
                return False
            latest = connection.execute(
                "SELECT sequence FROM device_snapshots WHERE run_id = ? AND device_id = ?",
                (snapshot.run_id, snapshot.device_id),
            ).fetchone()
            connection.execute(
                """INSERT INTO device_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.event_id, snapshot.agent_id, snapshot.run_id, snapshot.device_id,
                    snapshot.sequence, snapshot.state.value, occurred_at, payload,
                ),
            )
            if latest is not None and snapshot.sequence <= int(latest["sequence"]):
                return False
            connection.execute(
                """
                INSERT INTO device_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, device_id) DO UPDATE SET
                  agent_id=excluded.agent_id, sequence=excluded.sequence, state=excluded.state,
                  occurred_at=excluded.occurred_at, payload_json=excluded.payload_json
                """,
                (
                    snapshot.run_id, snapshot.device_id, snapshot.agent_id, snapshot.sequence,
                    snapshot.state.value, occurred_at, payload,
                ),
            )
            return True

    def latest_snapshot(self, run_id: str, device_id: str) -> DeviceSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_snapshots WHERE run_id = ? AND device_id = ?", (run_id, device_id)
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def audit_entries(self) -> Iterator[sqlite3.Row]:
        with self._connection() as connection:
            yield from connection.execute("SELECT * FROM audit_logs ORDER BY audit_id")

    def _assert_device_scope(self, connection: sqlite3.Connection, command: CommandEnvelope) -> None:
        for device_id in command.device_ids:
            row = connection.execute(
                "SELECT agent_id, version FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None or row["agent_id"] != command.agent_id:
                raise PermissionError("Device không thuộc agent được chỉ định")
            if int(row["version"]) != command.expected_version:
                raise RuntimeError("Device version không khớp expected_version")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_id: str,
        action: str,
        entity_id: str,
        occurred_at: str,
        detail: dict[str, object],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_logs(actor_id, action, entity_id, occurred_at, detail_json) VALUES (?, ?, ?, ?, ?)",
            (actor_id, action, entity_id, occurred_at, json.dumps(detail, sort_keys=True)),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> DeviceSnapshot:
        return DeviceSnapshot(
            event_id="materialized:" + row["run_id"] + ":" + row["device_id"],
            agent_id=str(row["agent_id"]), run_id=str(row["run_id"]), device_id=str(row["device_id"]),
            sequence=int(row["sequence"]), state=DeviceRunState(row["state"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]), payload=json.loads(row["payload_json"]),
        )
