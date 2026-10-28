"""Polling: one BusCollector thread per RS-485 port.

Every poll attempt -- successful or not -- produces exactly one reading
record in the local queue, so the server can show communication failures
explicitly instead of silently missing data.  Controller commands (from the
server) are executed by the same thread between polls, so traffic on the
half-duplex bus can never overlap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue as queue_mod
import threading
import time
import uuid
from concurrent.futures import Future
from datetime import datetime, timezone
from typing import Any, Callable

from . import registers as R
from .decoder import QUALITY_COMM_ERROR, QUALITY_GOOD, QUALITY_SUSPECT
from .health_monitor import clock_synchronized
from .local_queue import LocalQueue, new_event, utc_now_iso
from .modbus_client import CommError, ControllerClient, SerialBus, SerialSettings, WritesDisabled

log = logging.getLogger(__name__)

READ_ONLY_COMMANDS = {"identity_check", "read_parameters"}
WRITE_COMMANDS = set(R.COMMAND_COIL_WRITES)


class ControllerRuntime:
    def __init__(self, cfg: dict, client: ControllerClient):
        self.cfg = cfg
        self.id = cfg["id"]
        self.client = client
        self.next_poll = time.monotonic()
        self.next_param_read = time.monotonic() + 5
        self.consecutive_failures = 0
        self.comm_state = "unknown"
        self.lost_since: str | None = None
        self.last_rs: int | None = None
        self.params_digest: str | None = None
        self.polls = 0
        self.failures = 0


def build_reading(*, node_id: str, ctrl_cfg: dict, acquired_at: str, config_version: int, source: str,
                  decoded=None, raw: dict | None = None, error: CommError | None = None,
                  clock_ok: bool | None = None, duration_ms: int | None = None) -> dict:
    """Assemble one reading record.  Unavailable values are None, never invented."""
    record: dict[str, Any] = {
        "reading_id": str(uuid.uuid4()),
        "node_id": node_id,
        "controller_id": ctrl_cfg["id"],
        "slave_id": ctrl_cfg["slave_id"],
        "model": ctrl_cfg["model"],
        "acquired_at": acquired_at,
        "source": source,
        "config_version": config_version,
        "poll_duration_ms": duration_ms,
        "comm_status": "ok" if error is None else error.status,
        "comm_error": None if error is None else str(error)[:500],
        "temperature_c": None, "display_temperature_c": None, "actual_temperature_c": None,
        "setpoint_c": None, "temp_log_high_c": None, "temp_log_low_c": None, "resolution_rs": None,
        "compressor_on": None, "alarm_on": None, "defrost_active": None, "keypad_locked": None,
        "fault_flags": None, "faults": [], "fault_descriptions": [],
        "system_status": None, "relay_status": None, "delays": None,
        "compressor_run_hours": None, "balanced_defrost_hours": None, "balanced_defrost_minutes": None,
        "software_revision": None,
        "quality": QUALITY_COMM_ERROR if error is not None else QUALITY_GOOD,
        "quality_flags": [], "warnings": [],
        "raw_registers": None,
    }
    if raw is not None and ctrl_cfg.get("include_raw_registers", True):
        record["raw_registers"] = {str(k): v for k, v in sorted(raw.items())}
    if decoded is not None:
        flags = decoded.system_flags
        record.update(
            temperature_c=decoded.temperature_c,
            display_temperature_c=decoded.control.value_c,
            actual_temperature_c=decoded.actual.value_c,
            setpoint_c=decoded.setpoint.value_c,
            temp_log_high_c=decoded.log_high.value_c,
            temp_log_low_c=decoded.log_low.value_c,
            resolution_rs=decoded.rs,
            compressor_on=decoded.compressor_on,
            alarm_on=decoded.alarm_on,
            defrost_active=flags.get("defrost"),
            keypad_locked=flags.get("keypad_locked"),
            fault_flags=decoded.fault_status,
            faults=list(decoded.faults),
            fault_descriptions=[R.FAULT_DESCRIPTIONS.get(f, f) for f in decoded.faults],
            system_status=decoded.system_status,
            relay_status=decoded.relay_status,
            delays={k: flags.get(k) for k in ("restart_delay", "min_on_delay", "ht_lt_power_on_delay")},
            compressor_run_hours=decoded.compressor_run_hours,
            balanced_defrost_hours=decoded.balanced_defrost_hours,
            balanced_defrost_minutes=decoded.balanced_defrost_minutes,
            software_revision=decoded.software_revision,
            quality=decoded.quality,
            quality_flags=list(decoded.quality_flags),
            warnings=list(decoded.warnings),
        )
    if clock_ok is False:
        record["quality_flags"].append("clock_unsynchronized")
        if record["quality"] == QUALITY_GOOD:
            record["quality"] = QUALITY_SUSPECT
    return record


class BusCollector(threading.Thread):
    def __init__(self, bus_cfg: dict, controller_cfgs: list[dict], *, queue: LocalQueue, health, node_id: str,
                 config_version: int, simulation: bool, allow_writes: bool, diagnostic: bool,
                 stop_event: threading.Event, client_factory: Callable | None = None,
                 clock_check: Callable[[], bool | None] = clock_synchronized):
        super().__init__(name=f"bus-{bus_cfg['name']}", daemon=True)
        self.bus_cfg = bus_cfg
        self.queue = queue
        self.health = health
        self.node_id = node_id
        self.config_version = config_version
        self.source = "simulated" if simulation else "controller"
        self.stop_event = stop_event
        self.clock_check = clock_check
        self.commands: queue_mod.Queue = queue_mod.Queue()
        self.wakeup = threading.Event()
        self.bus = SerialBus(SerialSettings(
            port=bus_cfg["port"], baudrate=bus_cfg["baudrate"], parity=bus_cfg["parity"],
            bytesize=bus_cfg["bytesize"], stopbits=bus_cfg["stopbits"], timeout_s=bus_cfg["timeout_s"],
            inter_request_delay_ms=bus_cfg["inter_request_delay_ms"],
        ), client_factory=client_factory)
        self.runtimes: dict[str, ControllerRuntime] = {}
        for i, cfg in enumerate(c for c in controller_cfgs if c.get("enabled", True)):
            overrides = cfg.get("register_overrides") or {}
            client = ControllerClient(
                self.bus, cfg["slave_id"], retries=bus_cfg["retries"],
                address_offset=overrides.get("address_offset", 0),
                read_strategy=overrides.get("read_strategy", "auto"),
                allow_writes=allow_writes, diagnostic_logging=diagnostic, model=cfg["model"],
            )
            rt = ControllerRuntime(cfg, client)
            rt.next_poll += i * 0.5  # stagger controllers sharing the bus
            self.runtimes[cfg["id"]] = rt
            health.update_controller(cfg["id"], slave_id=cfg["slave_id"], bus=bus_cfg["name"], comm_state="unknown")

    # ---------------------------------------------------------------- commands
    def submit_command(self, command: dict) -> Future:
        fut: Future = Future()
        self.commands.put((command, fut))
        self.wakeup.set()
        return fut

    def _run_commands(self) -> None:
        while True:
            try:
                command, fut = self.commands.get_nowait()
            except queue_mod.Empty:
                return
            try:
                fut.set_result(self.execute_command(command))
            except Exception as exc:  # noqa: BLE001
                fut.set_result(("failed", {"error": f"{type(exc).__name__}: {exc}"}))

    def execute_command(self, command: dict) -> tuple[str, dict]:
        rt = self.runtimes.get(command.get("controller_id"))
        name = command.get("command")
        if rt is None:
            return "rejected", {"error": "controller not configured or disabled on this node"}
        try:
            if name == "identity_check":
                regs = rt.client.read_holding(40001, 5)
                return "succeeded", {
                    "pvr_key": regs[40001], "software_revision": regs[40002], "controller_code": regs[40003],
                    "device_id": regs[40004], "rs": regs[40005],
                    "identity_ok": regs[40001] == R.PVR_KEY_VALUE and regs[40003] == R.CONTROLLER_ID_CODE,
                }
            if name == "read_parameters":
                raw, params = rt.client.read_parameters(rt.last_rs)
                self._store_parameters(rt, raw, params, force=True)
                return "succeeded", {"parameters": params}
            if name in WRITE_COMMANDS:
                result = rt.client.write_command(name)
                self.queue.put_event(new_event(
                    "controller_command", f"Executed {name} on controller {rt.id}", severity="warning",
                    controller_id=rt.id, details={"command_id": command.get("command_id"), **result}))
                return "succeeded", result
            return "rejected", {"error": f"unknown command {name!r}"}
        except WritesDisabled as exc:
            return "rejected", {"error": str(exc)}
        except CommError as exc:
            return "failed", {"error": str(exc), "comm_status": exc.status}

    # ---------------------------------------------------------------- polling
    def run(self) -> None:
        log.info("bus %s on %s: polling %s", self.bus_cfg["name"], self.bus_cfg["port"], list(self.runtimes))
        try:
            while not self.stop_event.is_set():
                try:
                    self._run_commands()
                    if not self.runtimes:
                        self.wakeup.wait(1.0)
                        self.wakeup.clear()
                        continue
                    rt = min(self.runtimes.values(), key=lambda r: r.next_poll)
                    delay = rt.next_poll - time.monotonic()
                    if delay > 0:
                        self.wakeup.wait(min(delay, 1.0))
                        self.wakeup.clear()
                        continue
                    self.poll(rt)
                    interval = rt.cfg["poll_interval_s"]
                    rt.next_poll += interval
                    if rt.next_poll < time.monotonic():  # fell behind: skip, do not burst
                        rt.next_poll = time.monotonic() + interval
                    self._maybe_read_parameters(rt)
                except Exception:  # noqa: BLE001 - a bug in one poll must not stop collection
                    log.exception("unexpected error in bus %s loop", self.bus_cfg["name"])
                    self.stop_event.wait(1.0)
        finally:
            self.bus.close()

    def poll(self, rt: ControllerRuntime) -> dict:
        acquired_at = utc_now_iso()
        started = time.monotonic()
        clock_ok = self.clock_check()
        raw = decoded = None
        error: CommError | None = None
        try:
            raw, decoded = rt.client.poll()
        except CommError as exc:
            error = exc
        duration_ms = int((time.monotonic() - started) * 1000)
        record = build_reading(node_id=self.node_id, ctrl_cfg=rt.cfg, acquired_at=acquired_at,
                               config_version=self.config_version, source=self.source, decoded=decoded,
                               raw=raw, error=error, clock_ok=clock_ok, duration_ms=duration_ms)
        stored = self.queue.put_reading(record)
        rt.polls += 1
        if error is None:
            rt.last_rs = decoded.rs
            self._on_success(rt, acquired_at, record)
        else:
            rt.failures += 1
            self._on_failure(rt, acquired_at, error)
        if not stored:
            log.error("reading for %s not stored: local buffer full (stop_collecting)", rt.id)
        return record

    def _on_success(self, rt: ControllerRuntime, at: str, record: dict) -> None:
        if rt.comm_state == "lost":
            self.queue.put_event(new_event(
                "comm_restored", f"Controller {rt.id} (slave {rt.cfg['slave_id']}) communication restored",
                controller_id=rt.id, occurred_at=at,
                details={"lost_since": rt.lost_since, "failed_polls": rt.consecutive_failures}))
            log.info("controller %s communication restored", rt.id)
        rt.comm_state, rt.lost_since, rt.consecutive_failures = "ok", None, 0
        self.health.update_controller(
            rt.id, comm_state="ok", last_success_at=at, consecutive_failures=0, last_comm_status="ok",
            last_temperature_c=record["temperature_c"], last_quality=record["quality"],
            polls_total=rt.polls, failures_total=rt.failures)

    def _on_failure(self, rt: ControllerRuntime, at: str, error: CommError) -> None:
        rt.consecutive_failures += 1
        threshold = rt.cfg.get("comm_loss_threshold", 3)
        details = {"comm_status": error.status, "error": str(error)[:500], "slave_id": rt.cfg["slave_id"],
                   "port": self.bus_cfg["port"], "exception_code": error.exception_code}
        if rt.consecutive_failures == 1:
            rt.lost_since = at
            self.queue.put_event(new_event("modbus_error", f"Controller {rt.id}: {error}", severity="warning",
                                           controller_id=rt.id, occurred_at=at, details=details))
        if rt.consecutive_failures == threshold and rt.comm_state != "lost":
            rt.comm_state = "lost"
            self.queue.put_event(new_event(
                "comm_lost", f"Controller {rt.id} (slave {rt.cfg['slave_id']}) not responding after "
                             f"{threshold} attempts", severity="error", controller_id=rt.id, occurred_at=at,
                details={**details, "since": rt.lost_since}))
            log.error("controller %s communication lost: %s", rt.id, error)
        else:
            log.warning("controller %s poll failed (%d in a row): %s", rt.id, rt.consecutive_failures, error)
        self.health.update_controller(
            rt.id, comm_state=rt.comm_state if rt.comm_state == "lost" else "failing", last_failure_at=at,
            consecutive_failures=rt.consecutive_failures, last_comm_status=error.status,
            last_error=str(error)[:300], polls_total=rt.polls, failures_total=rt.failures)
        self.health.record_error(f"modbus:{rt.id}", str(error))

    def _maybe_read_parameters(self, rt: ControllerRuntime) -> None:
        interval = rt.cfg.get("parameter_read_interval_s", 0)
        if not interval or rt.comm_state != "ok" or time.monotonic() < rt.next_param_read:
            return
        rt.next_param_read = time.monotonic() + interval
        try:
            raw, params = rt.client.read_parameters(rt.last_rs)
        except CommError as exc:
            log.warning("parameter read for %s failed: %s", rt.id, exc)
            return
        self._store_parameters(rt, raw, params)

    def _store_parameters(self, rt: ControllerRuntime, raw: dict, params: dict, force: bool = False) -> None:
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        if digest == rt.params_digest and not force:
            return
        rt.params_digest = digest
        self.queue.put_event(new_event(
            "parameter_snapshot", f"Controller {rt.id} parameter snapshot (read from device)",
            controller_id=rt.id, details={"parameters": params, "raw": {str(k): v for k, v in raw.items()},
                                          "rs": rt.last_rs, "digest": digest}))


def utc_parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
