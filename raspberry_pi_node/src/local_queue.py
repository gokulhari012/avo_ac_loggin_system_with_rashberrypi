"""Durable local store-and-forward queue (SQLite).

Readings and events are written here first and only marked delivered after
the server acknowledged durable storage.  The database uses WAL journaling
with ``synchronous=FULL`` so an acknowledged insert survives a power cut
(Raspberry Pis frequently lose power without a clean shutdown).

Capacity policy (``buffer.full_behavior``) when the pending backlog reaches
``max_pending_records`` or free disk drops below ``min_free_disk_mb``:

* ``drop_oldest``     -- delete the oldest pending readings and record a
                         ``data_gap`` event describing exactly what was lost;
* ``stop_collecting`` -- refuse new readings and record a ``data_gap`` event.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KINDS = ("readings", "events")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    controller_id TEXT,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_readings_status_seq ON readings(status, seq);
CREATE INDEX IF NOT EXISTS ix_readings_delivered ON readings(status, delivered_at);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    controller_id TEXT,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_status_seq ON events(status, seq);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_event(event_type: str, message: str, *, severity: str = "info", controller_id: str | None = None,
              details: dict | None = None, occurred_at: str | None = None) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "severity": severity,
        "message": message,
        "controller_id": controller_id,
        "occurred_at": occurred_at or utc_now_iso(),
        "details": details or {},
    }


class LocalQueue:
    def __init__(self, path: str | Path, *, max_pending_records: int = 2_000_000,
                 full_behavior: str = "drop_oldest", min_free_disk_mb: float = 100):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self.configure(max_pending_records=max_pending_records, full_behavior=full_behavior,
                       min_free_disk_mb=min_free_disk_mb)
        self._pending = self._count("readings", "pending")
        self._disk_checked_at = 0.0
        self._disk_low = False
        self._refusing = False

    def configure(self, *, max_pending_records: int, full_behavior: str, min_free_disk_mb: float) -> None:
        self.max_pending = max_pending_records
        self.full_behavior = full_behavior
        self.min_free_disk_mb = min_free_disk_mb

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ helpers
    def _count(self, kind: str, status: str) -> int:
        return self._db.execute(f"SELECT COUNT(*) FROM {kind} WHERE status=?", (status,)).fetchone()[0]

    def _disk_is_low(self) -> bool:
        now = time.monotonic()
        if now - self._disk_checked_at > 30:
            self._disk_checked_at = now
            try:
                free_mb = shutil.disk_usage(self.path.parent).free / 1_048_576
                self._disk_low = free_mb < self.min_free_disk_mb
            except OSError:
                self._disk_low = False
        return self._disk_low

    def _insert(self, kind: str, item_id: str, controller_id: str | None, occurred_at: str, payload: dict) -> bool:
        cur = self._db.execute(
            f"INSERT OR IGNORE INTO {kind}(item_id, controller_id, occurred_at, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (item_id, controller_id, occurred_at, json.dumps(payload, separators=(",", ":")), utc_now_iso()),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ writes
    def put_reading(self, reading: dict) -> bool:
        """Store a reading.  Returns False only when refused by stop_collecting."""
        with self._lock:
            full = self._pending >= self.max_pending or self._disk_is_low()
            if full and self.full_behavior == "stop_collecting":
                if not self._refusing:
                    self._refusing = True
                    self.set_state("refused_since", reading["acquired_at"])
                self.set_state("refused_count", int(self.get_state("refused_count", 0)) + 1)
                return False
            if self._refusing:
                self._close_refusal_episode(reading["acquired_at"])
            if full:
                self._drop_oldest()
            if self._insert("readings", reading["reading_id"], reading.get("controller_id"),
                            reading["acquired_at"], reading):
                self._pending += 1
            return True

    def _close_refusal_episode(self, now_iso: str) -> None:
        count = int(self.get_state("refused_count", 0))
        since = self.get_state("refused_since")
        self._refusing = False
        self.set_state("refused_count", 0)
        self.put_event(new_event(
            "data_gap", f"Local buffer full: {count} readings were not recorded",
            severity="error", details={"reason": "buffer_full_stop_collecting", "from": since,
                                       "to": now_iso, "records_lost": count},
        ))

    def _drop_oldest(self) -> None:
        n = max(1, self.max_pending // 100)  # free 1% so we do not drop on every insert
        rows = self._db.execute(
            "SELECT seq, occurred_at FROM readings WHERE status='pending' ORDER BY seq LIMIT ?", (n,)
        ).fetchall()
        if not rows:
            return
        self._db.execute("DELETE FROM readings WHERE status='pending' AND seq <= ?", (rows[-1][0],))
        self._pending -= len(rows)
        self.set_state("dropped_total", int(self.get_state("dropped_total", 0)) + len(rows))
        self.put_event(new_event(
            "data_gap", f"Local buffer full: oldest {len(rows)} undelivered readings discarded",
            severity="error",
            details={"reason": "buffer_full_drop_oldest", "from": rows[0][1], "to": rows[-1][1],
                     "records_lost": len(rows), "disk_low": self._disk_low},
        ))

    def put_event(self, event: dict) -> None:
        with self._lock:
            self._insert("events", event["event_id"], event.get("controller_id"), event["occurred_at"], event)

    # ------------------------------------------------------------------ reads
    def next_batch(self, kind: str, limit: int) -> list[dict]:
        assert kind in KINDS
        with self._lock:
            rows = self._db.execute(
                f"SELECT payload FROM {kind} WHERE status='pending' ORDER BY seq LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark_delivered(self, kind: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        with self._lock:
            now = utc_now_iso()
            changed = 0
            for chunk in _chunks(item_ids, 500):
                marks = ",".join("?" * len(chunk))
                cur = self._db.execute(
                    f"UPDATE {kind} SET status='delivered', delivered_at=? "
                    f"WHERE status='pending' AND item_id IN ({marks})", (now, *chunk))
                changed += cur.rowcount
            if kind == "readings":
                self._pending -= changed

    def mark_rejected(self, kind: str, rejected: dict[str, str]) -> None:
        """Move records the server permanently refused out of the send queue (kept for inspection)."""
        with self._lock:
            for item_id, error in rejected.items():
                cur = self._db.execute(
                    f"UPDATE {kind} SET status='rejected', last_error=? WHERE status='pending' AND item_id=?",
                    (str(error)[:1000], item_id))
                if kind == "readings":
                    self._pending -= cur.rowcount

    def record_failure(self, kind: str, item_ids: list[str], error: str) -> None:
        with self._lock:
            for chunk in _chunks(item_ids, 500):
                marks = ",".join("?" * len(chunk))
                self._db.execute(
                    f"UPDATE {kind} SET attempts=attempts+1, last_error=? WHERE item_id IN ({marks})",
                    (error[:1000], *chunk))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            oldest = self._db.execute(
                "SELECT MIN(occurred_at) FROM readings WHERE status='pending'").fetchone()[0]
            return {
                "pending_readings": self._pending,
                "pending_events": self._count("events", "pending"),
                "rejected_readings": self._count("readings", "rejected"),
                "oldest_pending_at": oldest,
                "dropped_total": int(self.get_state("dropped_total", 0)),
                "refusing_new_readings": self._refusing,
                "max_pending_records": self.max_pending,
            }

    def purge_delivered(self, older_than_days: float) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="milliseconds")
        cutoff = cutoff.replace("+00:00", "Z")
        with self._lock:
            removed = 0
            for kind in KINDS:
                removed += self._db.execute(
                    f"DELETE FROM {kind} WHERE status='delivered' AND delivered_at < ?", (cutoff,)).rowcount
            return removed

    # ------------------------------------------------------------------ state
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM state WHERE key=?", (key,))
            else:
                self._db.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]
