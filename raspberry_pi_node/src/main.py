"""Node entry point and supervisor.

Run:  python -m src.main --config /etc/temperature-node/config.yaml

Threads:
  bus-<name>   one per RS-485 port: Modbus polling + command execution
  uploader     store-and-forward of readings/events
  heartbeat    health reporting (also announces config versions / commands)
  config-sync  fetch, validate, apply and acknowledge configuration
  commands     controller command requests from the server
  main         supervision: restarts dead workers, systemd watchdog, liveness
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone

from . import __version__, sd_notify
from .collector import BusCollector
from .commands import CommandWorker
from .config_manager import ConfigError, ConfigManager
from .config_sync import ConfigSyncWorker
from .health_monitor import HealthMonitor, HeartbeatWorker
from .local_queue import LocalQueue, new_event, utc_now_iso
from .logging_config import setup_logging
from .server_client import ApiError, ServerClient, ServerUnavailable
from .uploader import Uploader

log = logging.getLogger("node")


class NodeApp:
    def __init__(self, config_path: str, *, env: dict | None = None, check_ports: bool = True,
                 client_factory=None, configure_logging: bool = True):
        self.cm = ConfigManager(config_path, env=env, check_ports=check_ports)
        self.modbus_client_factory = client_factory
        self.configure_logging = configure_logging
        self.stop_event = threading.Event()
        self.config_sync_trigger = threading.Event()
        self.command_trigger = threading.Event()
        self.last_config_error: str | None = None
        self._lock = threading.RLock()
        self._gen_stop = threading.Event()
        self.collectors: list[BusCollector] = []
        self.uploader: Uploader | None = None
        self.services: list[threading.Thread] = []
        self._clients: dict[int, tuple[tuple, ServerClient]] = {}

    @property
    def cfg(self) -> dict:
        return self.cm.effective

    # ------------------------------------------------------------------ helpers
    def server_client(self) -> ServerClient:
        """A per-thread client, rebuilt when connection settings change."""
        server, node_id = self.cfg["server"], self.cfg["node"]["id"]
        key = (server["url"], server.get("token"), server["request_timeout_s"], server["verify_tls"],
               server.get("ca_bundle"), node_id)
        tid = threading.get_ident()
        cached = self._clients.get(tid)
        if cached and cached[0] == key:
            return cached[1]
        if cached:
            cached[1].close()
        # Close clients of threads that no longer exist (workers are replaced on reconfiguration).
        alive = {t.ident for t in threading.enumerate()}
        for stale in [t for t in self._clients if t not in alive]:
            self._clients.pop(stale)[1].close()
        client = ServerClient(server["url"], node_id, server.get("token"), timeout_s=server["request_timeout_s"],
                              verify_tls=server["verify_tls"], ca_bundle=server.get("ca_bundle"))
        self._clients[tid] = (key, client)
        return client

    def collector_for(self, controller_id: str | None) -> BusCollector | None:
        for collector in self.collectors:
            if controller_id in collector.runtimes:
                return collector
        return None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        cfg = self.cm.load()
        if self.configure_logging:
            setup_logging(cfg["logging"])
        if self.cm.remote_load_error:
            log.error(self.cm.remote_load_error)
            self.last_config_error = self.cm.remote_load_error
        buf = cfg["buffer"]
        self.queue = LocalQueue(buf["path"], max_pending_records=buf["max_pending_records"],
                                full_behavior=buf["full_behavior"], min_free_disk_mb=buf["min_free_disk_mb"])
        self.health = HealthMonitor(cfg["node"]["id"], cfg["simulation"])
        log.info("temperature node %s v%s starting (config v%s, simulation=%s, remote control %s)",
                 cfg["node"]["id"], __version__, self.cm.version, cfg["simulation"],
                 "ENABLED" if cfg["safety"]["allow_remote_control"] else "disabled")
        if not cfg["server"].get("token"):
            log.warning("no server token configured: data is buffered locally until the node is registered")
        self._record_startup()
        self._start_generation()
        self.services = [HeartbeatWorker(self), ConfigSyncWorker(self), CommandWorker(self)]
        for worker in self.services:
            worker.start()

    def _record_startup(self) -> None:
        now = utc_now_iso()
        last_alive = self.queue.get_state("last_alive_at")
        previous_version = self.queue.get_state("software_version")
        intervals = [c["poll_interval_s"] for c in self.cfg["controllers"]] or [10]
        if last_alive:
            gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last_alive.replace("Z", "+00:00")))
            if gap.total_seconds() > max(60, 3 * min(intervals)):
                self.queue.put_event(new_event(
                    "data_gap", f"Node was not running from {last_alive} to {now}", severity="warning",
                    details={"reason": "node_not_running", "from": last_alive, "to": now,
                             "duration_s": int(gap.total_seconds())}))
        self.queue.put_event(new_event(
            "node_started", f"Node software {__version__} started", details={
                "software_version": __version__, "previous_software_version": previous_version,
                "config_version": self.cm.version, "simulation": self.cfg["simulation"]}))
        self.queue.set_state("software_version", __version__)
        self.queue.set_state("last_alive_at", now)

    def _start_generation(self) -> None:
        cfg = self.cfg
        self._gen_stop = threading.Event()
        by_bus: dict[str, list[dict]] = defaultdict(list)
        for ctrl in cfg["controllers"]:
            by_bus[ctrl["bus"]].append(ctrl)
        self.collectors = [
            BusCollector(bus, by_bus.get(bus["name"], []), queue=self.queue, health=self.health,
                         node_id=cfg["node"]["id"], config_version=self.cm.version, simulation=cfg["simulation"],
                         allow_writes=cfg["safety"]["allow_remote_control"],
                         diagnostic=cfg["logging"]["diagnostic_registers"], stop_event=self._gen_stop,
                         client_factory=self.modbus_client_factory)
            for bus in cfg["buses"] if by_bus.get(bus["name"])
        ]
        if not self.collectors:
            log.warning("no controllers configured: waiting for configuration from the server")
        self.uploader = Uploader(self, self._gen_stop)
        for thread in [*self.collectors, self.uploader]:
            thread.start()

    def _stop_generation(self) -> None:
        self._gen_stop.set()
        for thread in [*self.collectors, self.uploader]:
            if thread is not None:
                thread.join(timeout=60)

    def apply_remote_config(self, version: int, remote: dict) -> tuple[str, list[str], list[str]]:
        candidate, errors, ignored = self.cm.evaluate_remote(remote)
        if not errors and candidate["server"]["url"] != self.cfg["server"]["url"]:
            errors = self._test_server_url(candidate)
        if errors:
            log.error("configuration v%s rejected, keeping v%s: %s", version, self.cm.version, errors)
            self.last_config_error = f"v{version} rejected: " + "; ".join(errors)
            return "rejected", errors, ignored
        with self._lock:
            log.warning("applying configuration v%s (was v%s)", version, self.cm.version)
            self._stop_generation()
            self.cm.commit_remote(version, remote, candidate)
            if self.configure_logging:
                setup_logging(candidate["logging"])
            buf = candidate["buffer"]
            self.queue.configure(max_pending_records=buf["max_pending_records"],
                                 full_behavior=buf["full_behavior"], min_free_disk_mb=buf["min_free_disk_mb"])
            self._start_generation()
            self.last_config_error = None
        return "applied", [], ignored

    def _test_server_url(self, candidate: dict) -> list[str]:
        server = candidate["server"]
        client = ServerClient(server["url"], candidate["node"]["id"], server.get("token"),
                              timeout_s=server["request_timeout_s"], verify_tls=server["verify_tls"],
                              ca_bundle=server.get("ca_bundle"))
        try:
            client.heartbeat({"node_id": candidate["node"]["id"], "software_version": __version__,
                              "node_time": utc_now_iso(), "config_version": self.cm.version, "probe": True})
            return []
        except (ServerUnavailable, ApiError) as exc:
            return [f"new server.url {server['url']} failed an authenticated test request: {exc}"]
        finally:
            client.close()

    def supervise_once(self) -> bool:
        """Restart dead workers; return True when everything is alive."""
        with self._lock:
            dead = [t.name for t in [*self.collectors, self.uploader] if t is not None and not t.is_alive()]
            if dead and not self.stop_event.is_set():
                log.error("worker threads died unexpectedly (%s); restarting them", dead)
                self._stop_generation()
                self._start_generation()
            healthy = not dead and all(t.is_alive() for t in self.services)
        self.queue.set_state("last_alive_at", utc_now_iso())
        return healthy

    def run(self) -> int:
        self.start()
        sd_notify.notify("READY=1\nSTATUS=collecting")
        interval = min(sd_notify.watchdog_interval_s() or 15, 15)
        while not self.stop_event.wait(interval):
            if self.supervise_once():
                sd_notify.notify("WATCHDOG=1")
        self.shutdown()
        return 0

    def shutdown(self) -> None:
        log.info("shutting down")
        sd_notify.notify("STOPPING=1")
        self.stop_event.set()
        self.config_sync_trigger.set()
        self.command_trigger.set()
        self._stop_generation()
        for worker in self.services:
            worker.join(timeout=15)
        self.queue.set_state("last_alive_at", utc_now_iso())
        self.queue.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Temperature monitoring node (SZ-7526TX-REA over Modbus RTU)")
    ap.add_argument("--config", default="/etc/temperature-node/config.yaml")
    ap.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    app = NodeApp(args.config)
    if args.check_config:
        try:
            app.cm.load()
        except ConfigError as exc:
            print("INVALID configuration:\n  - " + "\n  - ".join(exc.errors))
            return 2
        print(f"configuration OK (remote version {app.cm.version})")
        return 0

    def _stop(signum, _frame):
        log.info("signal %s received", signum)
        app.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        return app.run()
    except ConfigError as exc:
        print("INVALID configuration:\n  - " + "\n  - ".join(exc.errors), file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"configuration file not found: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
