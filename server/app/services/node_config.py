"""Versioned node configuration (desired state pushed to nodes).

The payload is the node's *remote overlay* (see raspberry_pi_node/src/
config_manager.py REMOTE_ALLOWED).  Validation here mirrors the node's own
rules so mistakes are caught in the browser, but the node re-validates and
remains the final authority (it rejects and keeps its last good config).
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlparse

from ..extensions import db
from ..models import Controller, ControllerConfigHistory, Node, NodeConfigVersion
from ..models.common import dumps, loads, utcnow
from .audit import audit, diff

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Serial options documented in the SZ-7526TX-REA register map.
MODELS: dict[str, dict[str, tuple]] = {
    "SZ-7526TX-REA": {"baudrates": (9600, 19200, 38400), "parities": ("N", "E", "O"),
                      "bytesizes": (8,), "stopbits": (1,)},
}

DEFAULT_SERVER = {"upload_interval_s": 5, "batch_size": 200, "heartbeat_interval_s": 30,
                  "config_sync_interval_s": 60, "request_timeout_s": 10, "backoff_initial_s": 2,
                  "backoff_max_s": 300}
DEFAULT_BUS = {"name": "rs485-1", "port": "/dev/ttyUSB0", "baudrate": 9600, "parity": "N", "bytesize": 8,
               "stopbits": 1, "timeout_s": 1.0, "retries": 2, "inter_request_delay_ms": 50}
DEFAULT_CONTROLLER = {"id": "ctrl-1", "name": "", "model": "SZ-7526TX-REA", "bus": "rs485-1", "slave_id": 1,
                      "enabled": True, "poll_interval_s": 10, "parameter_read_interval_s": 3600,
                      "include_raw_registers": True, "comm_loss_threshold": 3,
                      "register_overrides": {"address_offset": 0, "read_strategy": "auto"}}
DEFAULT_BUFFER = {"retention_days_delivered": 7, "max_pending_records": 2_000_000,
                  "full_behavior": "drop_oldest", "min_free_disk_mb": 100}
DEFAULT_LOGGING = {"level": "INFO", "diagnostic_registers": False}

ALLOWED_SECTIONS = {"node", "server", "buses", "controllers", "buffer", "logging"}
ALLOWED_SERVER_KEYS = set(DEFAULT_SERVER) | {"url"}


class ConfigValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def default_config(node: Node) -> dict:
    return {
        "node": {"name": node.name, "description": node.description, "site": node.site,
                 "location": node.location, "equipment": node.equipment},
        "server": dict(DEFAULT_SERVER),
        "buses": [dict(DEFAULT_BUS)],
        "controllers": [copy.deepcopy(DEFAULT_CONTROLLER)],
        "buffer": dict(DEFAULT_BUFFER),
        "logging": dict(DEFAULT_LOGGING),
    }


def config_from_reported(node: Node) -> dict | None:
    """Build a starting config from the effective configuration the node reported."""
    reported = node.heartbeat.get("effective_config")
    if not isinstance(reported, dict):
        return None
    cfg = default_config(node)
    server = reported.get("server") or {}
    cfg["server"].update({k: server[k] for k in DEFAULT_SERVER if k in server})
    if isinstance(reported.get("buses"), list) and reported["buses"]:
        cfg["buses"] = [{k: b.get(k, DEFAULT_BUS[k]) for k in DEFAULT_BUS} for b in reported["buses"]]
    if isinstance(reported.get("controllers"), list) and reported["controllers"]:
        cfg["controllers"] = [{**copy.deepcopy(DEFAULT_CONTROLLER), **{k: c[k] for k in DEFAULT_CONTROLLER if k in c}}
                              for c in reported["controllers"]]
    for section, defaults in (("buffer", DEFAULT_BUFFER), ("logging", DEFAULT_LOGGING)):
        reported_section = reported.get(section) or {}
        cfg[section].update({k: reported_section[k] for k in defaults if k in reported_section})
    return cfg


def _num(errors, where, value, lo, hi, integer=False):
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok or not lo <= value <= hi:
        errors.append(f"{where} must be {'an integer' if integer else 'a number'} between {lo} and {hi}")


def validate_config(cfg: Any) -> list[str]:
    if not isinstance(cfg, dict):
        return ["configuration must be a JSON object"]
    errors: list[str] = []
    unknown = set(cfg) - ALLOWED_SECTIONS
    if unknown:
        errors.append(f"sections not allowed in a remote configuration: {', '.join(sorted(unknown))} "
                      "(identity, credentials, paths and safety switches are local to the node)")
    node = cfg.get("node", {})
    if not isinstance(node, dict):
        errors.append("node must be an object")
    else:
        for key, value in node.items():
            if key not in ("name", "description", "site", "location", "equipment"):
                errors.append(f"node.{key} cannot be set remotely")
            elif not isinstance(value, str) or len(value) > 200:
                errors.append(f"node.{key} must be text of at most 200 characters")

    server = cfg.get("server", {})
    if not isinstance(server, dict):
        errors.append("server must be an object")
        server = {}
    for key in set(server) - ALLOWED_SERVER_KEYS:
        errors.append(f"server.{key} cannot be set remotely")
    if "url" in server:
        parsed = urlparse(server["url"]) if isinstance(server["url"], str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
            errors.append("server.url must be an http:// or https:// URL")
    limits = {"request_timeout_s": (1, 120, False), "upload_interval_s": (1, 3600, False),
              "batch_size": (1, 1000, True), "heartbeat_interval_s": (5, 3600, False),
              "config_sync_interval_s": (10, 86400, False), "backoff_initial_s": (0.5, 600, False),
              "backoff_max_s": (5, 3600, False)}
    for key, (lo, hi, integer) in limits.items():
        if key in server:
            _num(errors, f"server.{key}", server[key], lo, hi, integer)

    buses = cfg.get("buses", [])
    controllers = cfg.get("controllers", [])
    if not isinstance(buses, list) or not buses or not all(isinstance(b, dict) for b in buses):
        errors.append("buses must be a non-empty list")
        buses = []
    if not isinstance(controllers, list) or not controllers or not all(isinstance(c, dict) for c in controllers):
        errors.append("controllers must be a non-empty list")
        controllers = []
    names = [b.get("name") for b in buses]
    if len(set(names)) != len(names):
        errors.append("bus names must be unique")
    ports = [b.get("port") for b in buses]
    if len(set(ports)) != len(ports):
        errors.append("two buses cannot use the same serial port")
    for i, bus in enumerate(buses):
        where = f"buses[{i}]"
        if not isinstance(bus.get("name"), str) or not ID_RE.match(bus["name"]):
            errors.append(f"{where}.name is invalid (letters, digits, . _ -)")
        if not isinstance(bus.get("port"), str) or not bus["port"].strip() or len(bus["port"]) > 200:
            errors.append(f"{where}.port is required")
        _num(errors, f"{where}.timeout_s", bus.get("timeout_s"), 0.05, 10)
        _num(errors, f"{where}.retries", bus.get("retries"), 0, 10, True)
        _num(errors, f"{where}.inter_request_delay_ms", bus.get("inter_request_delay_ms"), 0, 5000, True)

    by_name = {b.get("name"): b for b in buses}
    ids = [c.get("id") for c in controllers]
    if len(set(ids)) != len(ids):
        errors.append("controller ids must be unique")
    pairs = [(c.get("bus"), c.get("slave_id")) for c in controllers]
    if len(set(pairs)) != len(pairs):
        errors.append("two controllers on the same bus cannot share a slave ID")
    for i, ctrl in enumerate(controllers):
        where = f"controllers[{i}]"
        if not isinstance(ctrl.get("id"), str) or not ID_RE.match(ctrl["id"]):
            errors.append(f"{where}.id is invalid (letters, digits, . _ -)")
        model = MODELS.get(ctrl.get("model"))
        if model is None:
            errors.append(f"{where}.model must be one of {', '.join(MODELS)}")
        bus = by_name.get(ctrl.get("bus"))
        if bus is None:
            errors.append(f"{where}.bus must name one of the configured buses")
        _num(errors, f"{where}.slave_id", ctrl.get("slave_id"), 1, 250, True)
        _num(errors, f"{where}.poll_interval_s", ctrl.get("poll_interval_s"), 1, 3600)
        _num(errors, f"{where}.parameter_read_interval_s", ctrl.get("parameter_read_interval_s", 0), 0, 604800)
        _num(errors, f"{where}.comm_loss_threshold", ctrl.get("comm_loss_threshold", 3), 1, 100, True)
        if not isinstance(ctrl.get("name", ""), str) or len(ctrl.get("name", "")) > 128:
            errors.append(f"{where}.name must be text of at most 128 characters")
        overrides = ctrl.get("register_overrides") or {}
        if overrides.get("address_offset", 0) not in (-1, 0, 1):
            errors.append(f"{where}.register_overrides.address_offset must be -1, 0 or 1")
        if overrides.get("read_strategy", "auto") not in ("auto", "block", "segments"):
            errors.append(f"{where}.register_overrides.read_strategy must be auto, block or segments")
        if model and bus:
            for key, allowed_key, label in (("baudrate", "baudrates", "baud rate"), ("parity", "parities", "parity"),
                                            ("bytesize", "bytesizes", "data bits"),
                                            ("stopbits", "stopbits", "stop bits")):
                if bus.get(key) not in model[allowed_key]:
                    errors.append(f"bus '{bus.get('name')}' {label} must be one of {model[allowed_key]} "
                                  f"for {ctrl.get('model')}")

    buffer = cfg.get("buffer", {})
    if isinstance(buffer, dict):
        if "retention_days_delivered" in buffer:
            _num(errors, "buffer.retention_days_delivered", buffer["retention_days_delivered"], 0, 3650)
        if "max_pending_records" in buffer:
            _num(errors, "buffer.max_pending_records", buffer["max_pending_records"], 1000, 50_000_000, True)
        if "min_free_disk_mb" in buffer:
            _num(errors, "buffer.min_free_disk_mb", buffer["min_free_disk_mb"], 0, 100_000)
        if buffer.get("full_behavior", "drop_oldest") not in ("drop_oldest", "stop_collecting"):
            errors.append("buffer.full_behavior must be drop_oldest or stop_collecting")
        for key in set(buffer) - set(DEFAULT_BUFFER):
            errors.append(f"buffer.{key} cannot be set remotely")
    logging_cfg = cfg.get("logging", {})
    if isinstance(logging_cfg, dict):
        if logging_cfg.get("level", "INFO") not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            errors.append("logging.level must be DEBUG, INFO, WARNING or ERROR")
        for key in set(logging_cfg) - set(DEFAULT_LOGGING):
            errors.append(f"logging.{key} cannot be set remotely")
    return errors


def latest_version(node: Node) -> NodeConfigVersion | None:
    return (NodeConfigVersion.query.filter_by(node_id=node.id)
            .order_by(NodeConfigVersion.version.desc()).first())


def create_config_version(node: Node, config: dict, *, user=None, reason: str = "") -> NodeConfigVersion:
    errors = validate_config(config)
    if errors:
        raise ConfigValidationError(errors)
    previous = latest_version(node)
    number = (previous.version if previous else 0) + 1
    for older in NodeConfigVersion.query.filter(NodeConfigVersion.node_id == node.id,
                                                NodeConfigVersion.status.in_(("pending", "delivered"))):
        older.status = "superseded"
    version = NodeConfigVersion(node_id=node.id, version=number, config_json=dumps(config), reason=reason or "",
                                created_by_id=getattr(user, "id", None), status="pending")
    db.session.add(version)
    details = config.get("node") or {}
    for key in ("name", "description", "site", "location", "equipment"):
        if key in details:
            setattr(node, key, details[key])
    actor = getattr(user, "username", "system")
    sync_controllers(node, config, number, actor, reason)
    before, after = diff(previous.config if previous else {}, config)
    audit("config", "node_config_created", resource_type="node", resource_id=node.uid,
          previous=before, new=after, reason=reason,
          details={"version": number, "previous_version": previous.version if previous else None})
    return version


def sync_controllers(node: Node, config: dict, version: int, actor: str, reason: str = "") -> None:
    """Make Controller rows match the configuration; record per-controller history."""
    existing = {c.uid: c for c in node.controllers}
    buses = {b["name"]: b for b in config.get("buses", [])}
    seen = set()
    for item in config.get("controllers", []):
        seen.add(item["id"])
        ctrl = existing.get(item["id"])
        bus = buses.get(item["bus"], {})
        new_state = {"name": item.get("name") or item["id"], "model": item["model"], "bus_name": item["bus"],
                     "slave_id": item["slave_id"], "poll_interval_s": float(item["poll_interval_s"]),
                     "enabled": bool(item.get("enabled", True)), "port": bus.get("port"),
                     "baudrate": bus.get("baudrate"), "parity": bus.get("parity"),
                     "register_overrides": item.get("register_overrides")}
        if ctrl is None:
            ctrl = Controller(node=node, uid=item["id"])
            db.session.add(ctrl)
            old_state = {}
        else:
            old_state = loads(_last_controller_json(ctrl), None) or {
                "name": ctrl.name, "model": ctrl.model, "bus_name": ctrl.bus_name, "slave_id": ctrl.slave_id,
                "poll_interval_s": ctrl.poll_interval_s, "enabled": ctrl.enabled}
        for key in ("name", "model", "bus_name", "slave_id", "poll_interval_s", "enabled"):
            setattr(ctrl, key, new_state[key])
        ctrl.auto_registered = False
        before, after = diff(old_state, new_state)
        if after:
            db.session.flush()
            db.session.add(ControllerConfigHistory(
                controller_id=ctrl.id, source="server_config", actor=actor, config_version=version,
                previous_json=dumps(before) if old_state else None, new_json=dumps(new_state),
                changed_keys=dumps(sorted(after) if old_state else ["created"]), reason=reason))
            if before:
                audit("config", "controller_settings_changed", resource_type="controller",
                      resource_id=f"{node.uid}/{ctrl.uid}", previous=before, new=after, reason=reason,
                      details={"config_version": version})
    for uid, ctrl in existing.items():
        if uid not in seen and ctrl.enabled:
            ctrl.enabled = False
            db.session.add(ControllerConfigHistory(
                controller_id=ctrl.id, source="server_config", actor=actor, config_version=version,
                previous_json=dumps({"enabled": True}), new_json=dumps({"enabled": False}),
                changed_keys=dumps(["enabled"]), reason=reason or "removed from node configuration"))


def _last_controller_json(ctrl: Controller) -> str | None:
    row = (ControllerConfigHistory.query.filter_by(controller_id=ctrl.id, source="server_config")
           .order_by(ControllerConfigHistory.id.desc()).first())
    return row.new_json if row else None


def offer_for_node(node: Node, current_version: int | None) -> NodeConfigVersion | None:
    latest = latest_version(node)
    if latest is None or latest.version == current_version:
        return None
    if latest.status == "pending":
        latest.status, latest.delivered_at = "delivered", utcnow()
    return latest


def apply_ack(node: Node, version: int, status: str, errors: list[str], ignored: list[str]) -> NodeConfigVersion:
    cv = NodeConfigVersion.query.filter_by(node_id=node.id, version=version).first()
    if cv is None:
        raise LookupError(f"configuration version {version} does not exist for {node.uid}")
    if cv.status != "superseded":
        cv.status = "applied" if status == "applied" else "rejected"
    cv.acked_at = utcnow()
    cv.ack_errors = dumps(errors) if errors else None
    cv.ignored_keys = dumps(ignored) if ignored else None
    if status == "applied":
        node.reported_config_version = version
        node.config_error = None
    else:
        node.config_error = "; ".join(errors)[:2000]
    audit("config", "node_config_applied" if status == "applied" else "node_config_rejected",
          actor_type="node", actor_id=node.uid, actor_name=node.uid, resource_type="node", resource_id=node.uid,
          outcome="success" if status == "applied" else "failure",
          details={"version": version, "errors": errors, "ignored_keys": ignored})
    return cv
