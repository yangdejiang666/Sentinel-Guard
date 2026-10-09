from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

TABLES = {"telemetry", "detections", "events", "transitions", "commands", "devices"}
IDENTITIES = {"telemetry": "event_id", "detections": "event_id", "events": "event_id", "commands": "command_id", "devices": "device_id"}


def dt_to_text(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Database:
    """Small local store. All connections are committed and closed after use."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._lock, self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, metric TEXT NOT NULL, device_id TEXT NOT NULL,
                    event_time TEXT NOT NULL, ingest_time TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_metric_time ON telemetry(metric, event_time DESC);
                CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry(device_id, event_time DESC);
                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, event_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, requested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    stream_key TEXT PRIMARY KEY, sequence INTEGER NOT NULL, event_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_requests (
                    request_id TEXT PRIMARY KEY, command_id TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _table(table: str) -> None:
        if table not in TABLES:
            raise ValueError("unsupported database table")

    def check(self) -> bool:
        with self._lock, self.connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1

    def get(self, table: str, identity: str) -> dict[str, Any] | None:
        self._table(table)
        if table not in IDENTITIES:
            raise ValueError("table has no external identity")
        with self._lock, self.connect() as conn:
            row = conn.execute(
                f"SELECT id, payload FROM {table} WHERE {IDENTITIES[table]} = ?", (identity,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if table in {"events", "commands", "transitions"}:
            payload["id"] = row["id"]
        return payload

    def get_telemetry_event(self, event_id: str) -> dict[str, Any] | None:
        """Compatibility helper used by the ingestion de-duplication path."""
        return self.get("telemetry", event_id)

    def get_json_event(self, table: str, event_id: str) -> dict[str, Any] | None:
        """Fetch an externally identified JSON event without exposing SQL names."""
        if table not in {"detections", "events", "commands"}:
            raise ValueError("unsupported event table")
        return self.get(table, event_id)

    def insert_json(self, table: str, unique_field: str, unique_value: str,
                    payload: dict[str, Any], time_field: str, time_value: datetime) -> int:
        self._table(table)
        times = {"detections": "event_time", "events": "occurred_at", "commands": "requested_at", "devices": "updated_at"}
        if IDENTITIES.get(table) != unique_field or times.get(table) != time_field:
            raise ValueError("invalid table fields")
        with self._lock, self.connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({unique_field}, payload, {time_field}) VALUES (?, ?, ?)",
                (unique_value, json.dumps(payload, ensure_ascii=False), dt_to_text(time_value)),
            )
            return int(conn.execute(
                f"SELECT id FROM {table} WHERE {unique_field} = ?", (unique_value,)
            ).fetchone()[0])

    def insert_telemetry(self, payload: dict[str, Any]) -> int:
        with self._lock, self.connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO telemetry (event_id, payload, metric, device_id, event_time, ingest_time)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (payload["event_id"], json.dumps(payload, ensure_ascii=False), payload["metric"],
                  payload["device_id"], payload["event_time"], payload["ingest_time"]))
            return int(conn.execute(
                "SELECT id FROM telemetry WHERE event_id = ?", (payload["event_id"],)
            ).fetchone()[0])

    def insert_safety_event(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Atomically persist an immutable finding, preserving any acknowledgement."""
        ignored = {"id", "acknowledged", "acknowledged_by", "acknowledged_at",
                   "handling_status", "assignee", "revision", "handling_history"}
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, payload FROM events WHERE event_id = ?", (payload["event_id"],)
            ).fetchone()
            if row:
                existing = json.loads(row["payload"])
                if {key: value for key, value in existing.items() if key not in ignored} != {
                    key: value for key, value in payload.items() if key not in ignored
                }:
                    raise ValueError("event_id conflicts with an existing event")
                existing["id"] = row["id"]
                return existing, False
            cursor = conn.execute(
                "INSERT INTO events (event_id, payload, occurred_at) VALUES (?, ?, ?)",
                (payload["event_id"], json.dumps(payload, ensure_ascii=False), payload["occurred_at"]),
            )
            return {**payload, "id": int(cursor.lastrowid)}, True

    def insert_transition(self, payload: dict[str, Any], time_value: datetime) -> int:
        with self._lock, self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO transitions (payload, occurred_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), dt_to_text(time_value)),
            )
            return int(cursor.lastrowid)

    def act_on_event(self, event_id: str, action: dict[str, Any], site_id: str,
                     source_mode: str) -> tuple[dict[str, Any], bool] | None:
        """Serialize workflow updates across connections; retries never duplicate audit entries."""
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, payload FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if not row:
                return None
            record = json.loads(row["payload"])
            if record.get("site_id", "demo-site") != site_id or record.get("source_mode", "live") != source_mode:
                return None
            history = record.setdefault("handling_history", [])
            for entry in history:
                if entry["request_id"] == action["request_id"]:
                    if any(entry.get(key) != value for key, value in action.items()):
                        raise ValueError("request_id conflicts with an existing action")
                    return {**record, "id": row["id"]}, False
            if record.get("revision", 0) != action["expected_revision"]:
                raise ValueError("event revision changed; reload before retry")
            status = record.get("handling_status", "open")
            allowed = {
                "claim": {"open"}, "note": {"open", "in_progress", "pending_review"},
                "submit_review": {"in_progress"}, "close": {"pending_review"}, "reopen": {"closed"},
            }
            operation = action["action"]
            if status not in allowed[operation]:
                raise ValueError(f"invalid event transition: {status} -> {operation}")
            if operation in {"note", "submit_review"} and record.get("assignee") and action["actor"] != record["assignee"]:
                raise ValueError("only the declared assignee can update this handling record")
            if operation == "close":
                if not action["evidence_ref"].strip():
                    raise ValueError("closing requires a field review evidence reference")
                if action["actor"] == record.get("assignee"):
                    raise ValueError("reviewer must differ from the declared assignee")
            statuses = {"claim": "in_progress", "submit_review": "pending_review", "close": "closed", "reopen": "open"}
            record["handling_status"] = statuses.get(operation, status)
            if operation == "claim":
                record["assignee"] = action["actor"]
            elif operation == "reopen":
                record["assignee"] = None
            record["revision"] = record.get("revision", 0) + 1
            history.append({**action, "at": dt_to_text(datetime.now(timezone.utc)),
                            "from_status": status, "to_status": record["handling_status"]})
            conn.execute("UPDATE events SET payload = ? WHERE event_id = ?",
                         (json.dumps(record, ensure_ascii=False), event_id))
            return {**record, "id": row["id"]}, True

    def search_events(self, *, site_id: str, source_mode: str, q: str = "",
                      severity: str = "all", acknowledgement: str = "all",
                      kind: str = "all", handling: str = "all", sort: str = "recent",
                      page: int = 1, page_size: int = 20) -> dict[str, Any]:
        clauses = ["COALESCE(json_extract(payload, '$.site_id'), 'demo-site') = ?",
                   "COALESCE(json_extract(payload, '$.source_mode'), 'live') = ?"]
        params: list[Any] = [site_id, source_mode]
        if q:
            fields = ("title", "message", "source_device_id", "event_id", "correlation_id", "payload.rule_id")
            clauses.append("(" + " OR ".join(f"instr(lower(COALESCE(json_extract(payload, '$.{field}'), '')), lower(?)) > 0" for field in fields) + ")")
            params.extend([q] * len(fields))
        for field, value in (("severity", severity), ("handling_status", handling)):
            if value != "all":
                clauses.append(f"COALESCE(json_extract(payload, '$.{field}'), 'open') = ?")
                params.append(value)
        if acknowledgement != "all":
            clauses.append("COALESCE(json_extract(payload, '$.acknowledged'), 0) = ?")
            params.append(int(acknowledgement == "acknowledged"))
        if kind != "all":
            if kind == "other":
                clauses.append("json_extract(payload, '$.event_type') NOT IN ('anomaly', 'industrial_alert')")
            else:
                clauses.append("json_extract(payload, '$.event_type') = ?")
                params.append(kind)
        where = " AND ".join(clauses)
        order = "occurred_at DESC, id DESC"
        if sort == "priority":
            order = """(CASE WHEN COALESCE(json_extract(payload, '$.acknowledged'), 0) = 0 THEN 10 ELSE 0 END +
                CASE json_extract(payload, '$.severity') WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END) DESC, """ + order
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}", params).fetchone()[0]
            page = min(page, max(1, (total + page_size - 1) // page_size))
            rows = conn.execute(f"SELECT id, payload FROM events WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                                (*params, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [{**json.loads(row["payload"]), "id": row["id"]} for row in rows],
                "total": total, "page": page, "page_size": page_size}

    def save_device(self, payload: dict[str, Any], updated_at: datetime) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("""
                INSERT INTO devices (device_id, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """, (payload["device_id"], json.dumps(payload, ensure_ascii=False), dt_to_text(updated_at)))

    def save_command(self, payload: dict[str, Any]) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE commands SET payload = ? WHERE command_id = ?",
                (json.dumps(payload, ensure_ascii=False), payload["command_id"]),
            )

    def update_command_feedback(self, feedback: dict[str, Any]) -> dict[str, Any] | None:
        """Apply a hardware acknowledgement using an explicit command state machine."""
        command = self.get("commands", feedback["command_id"])
        if command is None:
            return None

        current_status = command["status"]
        next_status = feedback["status"]
        if current_status == next_status:
            return command
        allowed = {
            "accepted": {"acknowledged", "completed", "failed", "timeout"},
            "sent": {"acknowledged", "completed", "failed", "timeout"},
            "acknowledged": {"completed", "failed", "timeout"},
            "completed": set(),
            "failed": set(),
            "timeout": set(),
        }
        if next_status not in allowed.get(current_status, set()):
            raise ValueError(f"invalid command transition: {current_status} -> {next_status}")

        at = feedback["event_time"]
        command["status"] = next_status
        command["execution_message"] = feedback["execution_message"]
        command.setdefault("status_history", []).append({
            "status": next_status,
            "at": at,
            "message": feedback["execution_message"],
        })
        if next_status in {"acknowledged", "completed"}:
            command["acknowledged_at"] = at
        if next_status == "completed":
            command["completed_at"] = at
        self.save_command(command)
        return command

    def mark_command_timeout(self, command_id: str, now: datetime) -> dict[str, Any] | None:
        """Move an outstanding command to timeout; terminal commands remain unchanged."""
        command = self.get("commands", command_id)
        if command is None or command.get("status") not in {"accepted", "sent", "acknowledged"}:
            return command
        at = dt_to_text(now)
        command["status"] = "timeout"
        command["execution_message"] = "执行器未在配置时限内返回完成回执"
        command.setdefault("status_history", []).append({
            "status": "timeout",
            "at": at,
            "message": command["execution_message"],
        })
        self.save_command(command)
        return command

    def command_for_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT command_id FROM command_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self.get("commands", row[0]) if row else None

    def remember_request(self, request_id: str, command_id: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO command_requests VALUES (?, ?)", (request_id, command_id))

    def get_cursor(self, key: str) -> tuple[int, datetime] | None:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT sequence, event_time FROM cursors WHERE stream_key = ?", (key,)).fetchone()
        return (row[0], datetime.fromisoformat(row[1])) if row else None

    def save_cursor(self, key: str, sequence: int, event_time: datetime) -> None:
        with self._lock, self.connect() as conn:
            conn.execute("""
                INSERT INTO cursors VALUES (?, ?, ?)
                ON CONFLICT(stream_key) DO UPDATE SET sequence = excluded.sequence, event_time = excluded.event_time
            """, (key, sequence, dt_to_text(event_time)))

    def acknowledge(self, event_id: str, operator: str, now: datetime) -> dict[str, Any] | None:
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id, payload FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            if not payload.get("acknowledged"):
                payload.update(acknowledged=True, acknowledged_by=operator, acknowledged_at=dt_to_text(now))
                conn.execute("UPDATE events SET payload = ? WHERE event_id = ?",
                             (json.dumps(payload, ensure_ascii=False), event_id))
            payload["id"] = row["id"]
            return payload

    def update_event_acknowledgement(
        self,
        event_id: str,
        acknowledged: bool,
        operator: str = "operator",
    ) -> dict[str, Any] | None:
        """Backward-compatible event acknowledgement entry point."""
        if not acknowledged:
            return self.get("events", event_id)
        return self.acknowledge(event_id, operator, datetime.now(timezone.utc))

    def list_json(self, table: str, limit: int = 100, *, site_id: str | None = None,
                  source_mode: str | None = None) -> list[dict[str, Any]]:
        self._table(table)
        clauses, params = [], []
        for field, value, default in (("site_id", site_id, "demo-site"), ("source_mode", source_mode, "live")):
            if value is not None:
                clauses.append(f"COALESCE(json_extract(payload, '$.{field}'), ?) = ?")
                params.extend((default, value))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT id, payload FROM {table} {where} ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(limit, 1000))),
            ).fetchall()
        items = []
        for row in rows:
            item = json.loads(row["payload"])
            # Records written by the initial platform prototype did not yet
            # carry the required model metadata. Keep them auditable without
            # misrepresenting them as a known deployed model.
            if table == "detections":
                item.setdefault("model_version", "legacy-unknown")
            if table in {"events", "commands", "transitions"}:
                item["id"] = row["id"]
            items.append(item)
        return items

    def list_telemetry(self, metric: str | None = None, device_id: str | None = None,
                       limit: int = 120, start: datetime | None = None,
                       end: datetime | None = None, *, site_id: str | None = None,
                       source_mode: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, op, value in [
            ("metric", "=", metric), ("device_id", "=", device_id),
            ("event_time", ">=", dt_to_text(start)), ("event_time", "<=", dt_to_text(end)),
        ]:
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        for field, value, default in (("site_id", site_id, "demo-site"), ("source_mode", source_mode, "live")):
            if value is not None:
                clauses.append(f"COALESCE(json_extract(payload, '$.{field}'), ?) = ?")
                params.extend((default, value))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM telemetry {where} ORDER BY event_time DESC, id DESC LIMIT ?",
                (*params, max(1, min(limit, 1000))),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows][::-1]

