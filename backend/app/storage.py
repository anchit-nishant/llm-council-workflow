from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from .schemas import CouncilConfig, RunEvent, RunSnapshot, RunSummary


class SQLiteStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS configs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    stage TEXT,
                    node_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_events_run_id_id
                ON events (run_id, id);
                """
            )
            self._conn.commit()

    def upsert_config(self, config: CouncilConfig) -> CouncilConfig:
        payload_json = json.dumps(config.model_dump(mode="json"))
        # `CouncilConfig` has no timestamps, so store current UTC on every write.
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO configs (id, name, description, version, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    version = excluded.version,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    config.id,
                    config.name,
                    config.description,
                    config.version,
                    payload_json,
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()
        return config

    def get_config(self, config_id: str) -> CouncilConfig | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM configs WHERE id = ?", (config_id,)
            ).fetchone()
        if not row:
            return None
        return CouncilConfig.model_validate_json(row["payload_json"])

    def list_configs(self) -> list[CouncilConfig]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM configs ORDER BY updated_at DESC"
            ).fetchall()
        return [CouncilConfig.model_validate_json(row["payload_json"]) for row in rows]

    def delete_config(self, config_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM configs WHERE id = ?", (config_id,))
            self._conn.commit()
        return cursor.rowcount > 0

    def create_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        payload_json = json.dumps(snapshot.model_dump(mode="json"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (id, query, status, config_id, snapshot_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_id,
                    snapshot.query,
                    snapshot.status,
                    snapshot.config_snapshot.id,
                    payload_json,
                    snapshot.error,
                    snapshot.created_at.isoformat(),
                    snapshot.updated_at.isoformat(),
                ),
            )
            self._conn.commit()
        return snapshot

    def save_run_snapshot(self, snapshot: RunSnapshot) -> RunSnapshot:
        payload_json = json.dumps(snapshot.model_dump(mode="json"))
        with self._lock:
            self._conn.execute(
                """
                UPDATE runs
                SET status = ?, snapshot_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    snapshot.status,
                    payload_json,
                    snapshot.error,
                    snapshot.updated_at.isoformat(),
                    snapshot.run_id,
                ),
            )
            self._conn.commit()
        return snapshot

    def get_run(self, run_id: str) -> RunSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        snapshot = RunSnapshot.model_validate_json(row["snapshot_json"])
        return snapshot.model_copy(update={"latest_event_id": self._latest_event_id(run_id)})

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT snapshot_json
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        snapshots = [RunSnapshot.model_validate_json(row["snapshot_json"]) for row in rows]
        items: list[RunSummary] = []
        for snapshot in snapshots:
            preview = None
            if snapshot.final_answer:
                preview = snapshot.final_answer[:180]
            items.append(
                RunSummary(
                    run_id=snapshot.run_id,
                    query=snapshot.query,
                    status=snapshot.status,
                    config_id=snapshot.config_snapshot.id,
                    config_name=snapshot.config_snapshot.name,
                    created_at=snapshot.created_at,
                    updated_at=snapshot.updated_at,
                    final_answer_preview=preview,
                )
            )
        return items

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if not row:
                return False
            if row["status"] in {"pending", "running"}:
                raise ValueError("Cannot delete an active run. Cancel it first.")
            self._conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            self._conn.commit()
        return True

    def clear_run_history(self) -> int:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id
                FROM runs
                WHERE status NOT IN ('pending', 'running')
                """
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            if not run_ids:
                return 0
            self._conn.executemany(
                "DELETE FROM events WHERE run_id = ?",
                [(run_id,) for run_id in run_ids],
            )
            self._conn.executemany(
                "DELETE FROM runs WHERE id = ?",
                [(run_id,) for run_id in run_ids],
            )
            self._conn.commit()
        return len(run_ids)

    def append_event(self, event: RunEvent) -> RunEvent:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO events (run_id, event_type, stage, node_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.type,
                    event.stage,
                    event.node_id,
                    json.dumps(event.payload),
                    event.timestamp.isoformat(),
                ),
            )
            self._conn.commit()
            event_id = int(cursor.lastrowid)
        return event.model_copy(update={"id": event_id})

    def list_events(self, run_id: str, after_id: int = 0) -> list[RunEvent]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, run_id, event_type, stage, node_id, payload_json, created_at
                FROM events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (run_id, after_id),
            ).fetchall()
        events: list[RunEvent] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            events.append(
                RunEvent(
                    id=row["id"],
                    run_id=row["run_id"],
                    type=row["event_type"],
                    stage=row["stage"],
                    node_id=row["node_id"],
                    timestamp=row["created_at"],
                    payload=payload,
                )
            )
        return events

    def _latest_event_id(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS latest_event_id FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["latest_event_id"]) if row else 0

    def request_cancel(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET cancel_requested = 1 WHERE id = ?",
                (run_id,),
            )
            self._conn.commit()

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return bool(row["cancel_requested"]) if row else False
