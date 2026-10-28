"""Background maintenance: node offline/online transitions and retention."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from ..extensions import db
from ..models import ControllerCommand, Node, NodeHeartbeat
from .ingestion import mark_offline
from .settings import get_setting
from .status import node_offline_threshold
from .timeutil import utcnow

log = logging.getLogger(__name__)


def run_monitor_cycle(now=None) -> dict:
    now = now or utcnow()
    went_offline = []
    threshold = node_offline_threshold()
    for node in Node.query.filter_by(enabled=True, online=True):
        if node.last_seen_at is None or now - node.last_seen_at > threshold:
            mark_offline(node, now)
            went_offline.append(node.uid)
    expired = (ControllerCommand.query
               .filter(ControllerCommand.status.in_(("pending", "sent")), ControllerCommand.expires_at < now)
               .update({"status": "expired"}, synchronize_session=False))
    db.session.commit()
    return {"went_offline": went_offline, "commands_expired": expired}


def purge_heartbeats(now=None) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(days=get_setting("heartbeat_retention_days"))
    removed = NodeHeartbeat.query.filter(NodeHeartbeat.received_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return removed


class Monitor(threading.Thread):
    def __init__(self, app, interval_s: float = 30.0):
        super().__init__(name="server-monitor", daemon=True)
        self.app = app
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self._last_purge = None

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            with self.app.app_context():
                try:
                    result = run_monitor_cycle()
                    if result["went_offline"]:
                        log.warning("nodes went offline: %s", result["went_offline"])
                    if self._last_purge is None or utcnow() - self._last_purge > timedelta(hours=24):
                        self._last_purge = utcnow()
                        removed = purge_heartbeats()
                        if removed:
                            log.info("purged %d old heartbeat rows", removed)
                except Exception:  # noqa: BLE001 - keep monitoring
                    db.session.rollback()
                    log.exception("monitor cycle failed")
                finally:
                    db.session.remove()
