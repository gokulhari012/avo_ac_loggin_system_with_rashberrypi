"""Node health metrics and the heartbeat worker.

System metrics are read from /proc and /sys (no extra dependencies); on
platforms where a source is missing the metric is reported as null.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .local_queue import utc_now_iso
from .server_client import ApiError, ServerUnavailable

log = logging.getLogger(__name__)

_clock_cache: tuple[float, bool | None] = (0.0, None)


def clock_synchronized() -> bool | None:
    """True/False when known, None when this platform cannot tell.

    A Raspberry Pi has no battery-backed clock: after a power cut without
    network its time can be wrong until NTP syncs, which would corrupt the
    timestamps of buffered readings.  Readings taken while unsynchronised are
    flagged ``clock_unsynchronized``.
    """
    global _clock_cache
    now = time.monotonic()
    if now - _clock_cache[0] < 60:
        return _clock_cache[1]
    result: bool | None = None
    if datetime.now(timezone.utc).year < 2024:
        result = False
    elif Path("/run/systemd/timesync/synchronized").exists():
        result = True
    elif platform.system() == "Linux":
        try:
            out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip() in ("yes", "no"):
                result = out.stdout.strip() == "yes"
        except (OSError, subprocess.SubprocessError):
            result = None
    _clock_cache = (now, result)
    return result


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def system_metrics(state_dir: str, server_url: str | None) -> dict[str, Any]:
    metrics: dict[str, Any] = {"hostname": socket.gethostname(), "platform": platform.platform()}
    cpu = _read("/sys/class/thermal/thermal_zone0/temp")
    metrics["cpu_temp_c"] = round(int(cpu) / 1000, 1) if cpu and cpu.isdigit() else None
    try:
        metrics["disk_free_mb"] = round(shutil.disk_usage(state_dir).free / 1_048_576)
    except OSError:
        metrics["disk_free_mb"] = None
    metrics["load_avg"] = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
    meminfo = _read("/proc/meminfo")
    metrics["mem_available_mb"] = None
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                metrics["mem_available_mb"] = int(line.split()[1]) // 1024
    uptime = _read("/proc/uptime")
    metrics["system_uptime_s"] = int(float(uptime.split()[0])) if uptime else None
    metrics["local_ip"] = None
    host = urlparse(server_url).hostname if server_url else None
    if host:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((host, 9))  # UDP connect sends nothing; reveals the outgoing interface
                metrics["local_ip"] = s.getsockname()[0]
        except OSError:
            pass
    wireless = _read("/proc/net/wireless")
    metrics["wifi"] = None
    if wireless:
        for line in wireless.splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4:
                metrics["wifi"] = {"interface": parts[0].rstrip(":"), "link_quality": float(parts[2].rstrip(".")),
                                   "signal_dbm": float(parts[3].rstrip("."))}
                break
    return metrics


class HealthMonitor:
    """Thread-safe store of node health, written by workers, read by heartbeats."""

    def __init__(self, node_id: str, simulation: bool):
        self.node_id = node_id
        self.simulation = simulation
        self.started_at = utc_now_iso()
        self._started_mono = time.monotonic()
        self._lock = threading.Lock()
        self.controllers: dict[str, dict[str, Any]] = {}
        self.upload: dict[str, Any] = {"last_success_at": None, "last_failure_at": None, "consecutive_failures": 0,
                                       "failed_total": 0, "uploaded_readings_total": 0, "last_error": None}
        self.config_sync: dict[str, Any] = {"last_check_at": None, "last_success_at": None, "last_error": None,
                                            "last_applied_version": None, "last_rejected_version": None}
        self.heartbeat_state: dict[str, Any] = {"last_success_at": None, "last_error": None}
        self.recent_errors: deque = deque(maxlen=20)

    def uptime_s(self) -> int:
        return int(time.monotonic() - self._started_mono)

    def update_controller(self, controller_id: str, **fields: Any) -> None:
        with self._lock:
            self.controllers.setdefault(controller_id, {}).update(fields)

    def record_error(self, source: str, message: str) -> None:
        with self._lock:
            self.recent_errors.append({"at": utc_now_iso(), "source": source, "message": message[:500]})

    def upload_succeeded(self, count: int) -> None:
        with self._lock:
            self.upload.update(last_success_at=utc_now_iso(), consecutive_failures=0)
            self.upload["uploaded_readings_total"] += count

    def upload_failed(self, error: str) -> None:
        with self._lock:
            self.upload["last_failure_at"] = utc_now_iso()
            self.upload["consecutive_failures"] += 1
            self.upload["failed_total"] += 1
            self.upload["last_error"] = error[:500]
        self.record_error("upload", error)

    def update(self, section: str, **fields: Any) -> None:
        with self._lock:
            getattr(self, section).update(fields)

    def snapshot(self, *, queue_stats: dict, config_version: int, config_view: dict,
                 state_dir: str, server_url: str | None, config_error: str | None = None) -> dict:
        with self._lock:
            controllers = [{"controller_id": k, **v} for k, v in sorted(self.controllers.items())]
            payload = {
                "node_id": self.node_id,
                "software_version": __version__,
                "node_time": utc_now_iso(),
                "started_at": self.started_at,
                "uptime_s": self.uptime_s(),
                "config_version": config_version,
                "config_error": config_error,
                "simulation": self.simulation,
                "clock_synchronized": clock_synchronized(),
                "queue": queue_stats,
                "upload": dict(self.upload),
                "config_sync": dict(self.config_sync),
                "controllers": controllers,
                "recent_errors": list(self.recent_errors),
                "effective_config": config_view,
            }
        payload["system"] = system_metrics(state_dir, server_url)
        return payload


class HeartbeatWorker(threading.Thread):
    """Periodically reports health; the reply tells the node about pending
    configuration versions and controller commands."""

    def __init__(self, app):
        super().__init__(name="heartbeat", daemon=True)
        self.app = app

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            cfg = self.app.cfg
            try:
                client = self.app.server_client()
                payload = self.app.health.snapshot(
                    queue_stats=self.app.queue.stats(), config_version=self.app.cm.version,
                    config_view=self.app.cm.public_view(), state_dir=cfg["state_dir"],
                    server_url=cfg["server"]["url"], config_error=self.app.last_config_error,
                )
                reply = client.heartbeat(payload)
                self.app.health.update("heartbeat_state", last_success_at=utc_now_iso(), last_error=None)
                desired = reply.get("config_version")
                if isinstance(desired, int) and desired != self.app.cm.version \
                        and desired != self.app.health.config_sync.get("last_rejected_version"):
                    self.app.config_sync_trigger.set()
                if reply.get("pending_commands"):
                    self.app.command_trigger.set()
            except (ServerUnavailable, ApiError) as exc:
                self.app.health.update("heartbeat_state", last_error=str(exc)[:500])
                log.warning("heartbeat failed: %s", exc)
            except Exception:  # noqa: BLE001 - never let the heartbeat thread die
                log.exception("unexpected heartbeat error")
            self.app.stop_event.wait(cfg["server"]["heartbeat_interval_s"])
