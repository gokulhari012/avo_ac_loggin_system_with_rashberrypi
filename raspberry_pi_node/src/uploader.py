"""Store-and-forward upload of queued readings and events.

* Oldest first, so the server sees each controller's history in order.
* A record leaves the "pending" state only when the server lists it as
  ``accepted`` or ``duplicate`` (both mean: durably stored).  Retrying a batch
  is therefore always safe.
* Network/5xx failures back off exponentially (with jitter) up to
  ``server.backoff_max_s``.  The outage start is persisted, so even across a
  node restart the server is told how long the outage lasted once
  connectivity returns.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from .local_queue import new_event, utc_now_iso
from .server_client import ApiError, ServerUnavailable

log = logging.getLogger(__name__)


class Backoff:
    def __init__(self, initial: float, maximum: float, factor: float = 2.0, rng: random.Random | None = None):
        self.initial, self.maximum, self.factor = initial, maximum, factor
        self.current = initial
        self.rng = rng or random.Random()

    def next_delay(self) -> float:
        delay = min(self.current, self.maximum)
        self.current = min(self.current * self.factor, self.maximum)
        return delay * self.rng.uniform(0.8, 1.2)

    def reset(self) -> None:
        self.current = self.initial


class Uploader(threading.Thread):
    def __init__(self, app, stop_event: threading.Event):
        super().__init__(name="uploader", daemon=True)
        self.app = app
        self.queue = app.queue
        self.health = app.health
        self.stop_event = stop_event
        server = app.cfg["server"]
        self.configured_batch = server["batch_size"]
        self.batch_size = self.configured_batch
        self.backoff = Backoff(server["backoff_initial_s"], server["backoff_max_s"])
        self._last_purge = 0.0

    def run(self) -> None:
        while not self.stop_event.is_set():
            delay = self.cycle()
            self.stop_event.wait(delay)

    def cycle(self) -> float:
        """One upload attempt; returns how long to wait before the next one."""
        server = self.app.cfg["server"]
        try:
            client = self.app.server_client()
            sent_events = self._send("events", client)
            sent_readings = self._send("readings", client)
            self._connection_ok()
            self.backoff.reset()
            self._purge()
            if sent_events or sent_readings:
                self.health.upload_succeeded(sent_readings)
                stats = self.queue.stats()
                if stats["pending_readings"] or stats["pending_events"]:
                    return 0.05  # drain backlog quickly
            return server["upload_interval_s"]
        except ServerUnavailable as exc:
            return self._failed(str(exc))
        except ApiError as exc:
            if exc.status_code == 401:
                msg = "server rejected node credentials (check node id/token)"
            elif exc.status_code == 403:
                msg = f"server refused node: {exc.message}"
            else:
                msg = str(exc)
            return self._failed(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected upload error")
            return self._failed(f"{type(exc).__name__}: {exc}")

    def _send(self, kind: str, client) -> int:
        batch = self.queue.next_batch(kind, self.batch_size)
        if not batch:
            return 0
        id_key = "reading_id" if kind == "readings" else "event_id"
        ids = [item[id_key] for item in batch]
        try:
            reply = client.submit_readings(batch) if kind == "readings" else client.submit_events(batch)
        except ApiError as exc:
            if exc.status_code in (400, 413):
                if len(batch) > 1:
                    # Envelope-level rejection: narrow down to the offending record.
                    self.batch_size = max(1, len(batch) // 2)
                    log.warning("server refused %s batch of %d (%s); retrying with %d",
                                kind, len(batch), exc.code, self.batch_size)
                    return 0
                self.queue.mark_rejected(kind, {ids[0]: f"{exc.code}: {exc.message}"})
                log.error("server permanently rejected %s %s: %s", kind, ids[0], exc)
                return 1
            self.queue.record_failure(kind, ids, str(exc))
            raise
        except ServerUnavailable as exc:
            self.queue.record_failure(kind, ids, str(exc))
            raise
        reply = reply or {}
        delivered = list(reply.get("accepted", [])) + list(reply.get("duplicates", []))
        rejected = {r.get("id"): r.get("error", "rejected") for r in reply.get("rejected", []) if r.get("id")}
        sent = set(ids)
        self.queue.mark_delivered(kind, [i for i in delivered if i in sent])
        if rejected:
            log.error("server rejected %d %s: %s", len(rejected), kind, list(rejected.items())[:3])
            self.queue.mark_rejected(kind, rejected)
        unaccounted = set(ids) - set(delivered) - set(rejected)
        if unaccounted:
            log.warning("%d %s not acknowledged by server; will retry", len(unaccounted), kind)
        if self.batch_size < self.configured_batch:
            self.batch_size = min(self.configured_batch, self.batch_size * 2)
        return len(delivered)

    def _failed(self, message: str) -> float:
        self.health.upload_failed(message)
        if self.queue.get_state("outage_started_at") is None:
            self.queue.set_state("outage_started_at", utc_now_iso())
            self.queue.set_state("outage_failures", 0)
            log.warning("server unreachable, buffering locally: %s", message)
        self.queue.set_state("outage_failures", int(self.queue.get_state("outage_failures", 0)) + 1)
        self.queue.set_state("outage_last_error", message[:500])
        return self.backoff.next_delay()

    def _connection_ok(self) -> None:
        started = self.queue.get_state("outage_started_at")
        if started is None:
            return
        stats = self.queue.stats()
        self.queue.put_event(new_event(
            "server_connection_restored", f"Connection to server restored (outage since {started})",
            severity="warning",
            details={"outage_started_at": started, "outage_ended_at": utc_now_iso(),
                     "failed_attempts": self.queue.get_state("outage_failures", 0),
                     "last_error": self.queue.get_state("outage_last_error"),
                     "backlog_readings": stats["pending_readings"], "dropped_total": stats["dropped_total"]}))
        for key in ("outage_started_at", "outage_failures", "outage_last_error"):
            self.queue.set_state(key, None)
        log.info("server connection restored; uploading %d buffered readings", stats["pending_readings"])

    def _purge(self) -> None:
        if time.monotonic() - self._last_purge < 3600:
            return
        self._last_purge = time.monotonic()
        removed = self.queue.purge_delivered(self.app.cfg["buffer"]["retention_days_delivered"])
        if removed:
            log.info("purged %d delivered records from local buffer", removed)
