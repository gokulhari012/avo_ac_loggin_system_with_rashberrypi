"""Configuration synchronisation with the server.

Flow: heartbeat reply announces ``config_version`` -> this worker fetches the
version -> the node validates and applies it (or rejects it, keeping the last
known good configuration) -> the result is acknowledged to the server.
A failed acknowledgement is persisted and retried.
"""

from __future__ import annotations

import logging
import threading

from .local_queue import new_event, utc_now_iso
from .server_client import ApiError, ServerUnavailable

log = logging.getLogger(__name__)


class ConfigSyncWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(name="config-sync", daemon=True)
        self.app = app

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001
                log.exception("unexpected configuration sync error")
            interval = self.app.cfg["server"]["config_sync_interval_s"]
            self.app.config_sync_trigger.wait(interval)
            self.app.config_sync_trigger.clear()

    def sync_once(self) -> None:
        app = self.app
        health = app.health
        health.update("config_sync", last_check_at=utc_now_iso())
        try:
            client = app.server_client()
            self._retry_pending_ack(client)
            offer = client.fetch_config(app.cm.version)
        except (ServerUnavailable, ApiError) as exc:
            health.update("config_sync", last_error=str(exc)[:500])
            return
        if not offer or offer.get("version") in (None, app.cm.version):
            health.update("config_sync", last_success_at=utc_now_iso(), last_error=None)
            return
        version = int(offer["version"])
        if version == health.config_sync.get("last_rejected_version"):
            return  # already evaluated and rejected; wait for a newer version
        status, errors, ignored = app.apply_remote_config(version, offer.get("config") or {})
        if status == "applied":
            health.update("config_sync", last_applied_version=version, last_success_at=utc_now_iso(),
                          last_error=None)
            app.queue.put_event(new_event("config_applied", f"Configuration version {version} applied",
                                          details={"version": version, "ignored_keys": ignored}))
        else:
            health.update("config_sync", last_rejected_version=version, last_error="; ".join(errors)[:500])
            app.queue.put_event(new_event("config_rejected", f"Configuration version {version} rejected",
                                          severity="error", details={"version": version, "errors": errors}))
        ack = {"version": version, "status": status, "errors": errors, "ignored_keys": ignored}
        try:
            app.server_client().ack_config(version, status, errors=errors, ignored_keys=ignored)
        except (ServerUnavailable, ApiError) as exc:
            log.warning("config ack for v%s failed, will retry: %s", version, exc)
            app.queue.set_state("pending_config_ack", ack)

    def _retry_pending_ack(self, client) -> None:
        pending = self.app.queue.get_state("pending_config_ack")
        if pending:
            client.ack_config(pending["version"], pending["status"], errors=pending["errors"],
                              ignored_keys=pending["ignored_keys"])
            self.app.queue.set_state("pending_config_ack", None)
