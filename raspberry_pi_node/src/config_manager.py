"""Node configuration: local YAML file + versioned remote overlay from the server.

* The LOCAL file (``/etc/temperature-node/config.yaml``) holds identity, the
  server credential, file paths and safety switches.  Only someone with shell
  access to the Pi can change these.
* The server may push a versioned REMOTE overlay that changes the operational
  settings listed in ``REMOTE_ALLOWED``.  Anything else in a remote payload is
  ignored and reported back in the acknowledgement.
* A candidate configuration is validated in full before it is applied; an
  invalid candidate is rejected and the last known good configuration stays
  active.  Applied overlays are persisted atomically so they survive restarts.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import registers as R

TOKEN_ENV = "TEMP_NODE_TOKEN"
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

DEFAULTS: dict[str, Any] = {
    "node": {"id": None, "name": "", "description": "", "site": "", "location": "", "equipment": ""},
    "server": {
        "url": None,
        "token": None,
        "verify_tls": True,
        "ca_bundle": None,
        "request_timeout_s": 10,
        "upload_interval_s": 5,
        "batch_size": 200,
        "heartbeat_interval_s": 30,
        "config_sync_interval_s": 60,
        "backoff_initial_s": 2,
        "backoff_max_s": 300,
    },
    "buses": [],
    "controllers": [],
    "buffer": {
        "path": "/var/lib/temperature-node/queue.db",
        "retention_days_delivered": 7,
        "max_pending_records": 2_000_000,
        "full_behavior": "drop_oldest",
        "min_free_disk_mb": 100,
    },
    "logging": {
        "level": "INFO",
        "file": None,
        "format": "text",
        "max_bytes": 5_000_000,
        "backup_count": 10,
        "diagnostic_registers": False,
    },
    "safety": {"allow_remote_control": False},
    "state_dir": "/var/lib/temperature-node",
    "simulation": False,
}

BUS_DEFAULTS: dict[str, Any] = {
    "name": "rs485-1",
    "port": "/dev/ttyUSB0",
    "baudrate": 9600,
    "parity": "N",
    "bytesize": 8,
    "stopbits": 1,
    "timeout_s": 1.0,
    "retries": 2,
    "inter_request_delay_ms": 50,
}

CONTROLLER_DEFAULTS: dict[str, Any] = {
    "id": "ctrl-1",
    "name": "",
    "model": R.MODEL_SZ7526TX_REA,
    "bus": "rs485-1",
    "slave_id": 1,
    "enabled": True,
    "poll_interval_s": 10,
    "parameter_read_interval_s": 3600,
    "include_raw_registers": True,
    "comm_loss_threshold": 3,
    "register_overrides": {"address_offset": 0, "read_strategy": "auto"},
}

# Sections/keys the server is allowed to change.  "*" = whole section.
REMOTE_ALLOWED: dict[str, Any] = {
    "node": {"name", "description", "site", "location", "equipment"},
    "server": {"url", "request_timeout_s", "upload_interval_s", "batch_size", "heartbeat_interval_s",
               "config_sync_interval_s", "backoff_initial_s", "backoff_max_s"},
    "buses": "*",
    "controllers": "*",
    "buffer": {"retention_days_delivered", "max_pending_records", "full_behavior", "min_free_disk_mb"},
    "logging": {"level", "diagnostic_registers"},
}

SECRET_KEYS = {"token", "password", "secret"}


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _check_structure(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        raise ConfigError(["configuration must be a mapping"])
    for key, default in DEFAULTS.items():
        value = cfg.get(key)
        if value is None:
            continue
        if isinstance(default, dict) and not isinstance(value, dict):
            raise ConfigError([f"{key} must be a mapping"])
        if isinstance(default, list) and (not isinstance(value, list) or
                                          not all(isinstance(item, dict) for item in value)):
            raise ConfigError([f"{key} must be a list of mappings"])


def normalize(cfg: dict) -> dict:
    """Fill defaults so every key exists (list items included)."""
    _check_structure(cfg or {})
    out = _merge(DEFAULTS, cfg or {})
    out["buses"] = [_merge(BUS_DEFAULTS, b) for b in out.get("buses") or []]
    out["controllers"] = [_merge(CONTROLLER_DEFAULTS, c) for c in out.get("controllers") or []]
    for bus in out["buses"]:
        if isinstance(bus.get("parity"), str):
            bus["parity"] = bus["parity"].strip().upper()[:1]
    if isinstance(out["logging"].get("level"), str):
        out["logging"]["level"] = out["logging"]["level"].upper()
    return out


def _num(errors: list[str], where: str, value: Any, lo: float, hi: float, integer: bool = False) -> None:
    ok_type = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok_type or not lo <= value <= hi:
        kind = "an integer" if integer else "a number"
        errors.append(f"{where} must be {kind} between {lo} and {hi} (got {value!r})")


def validate(cfg: dict, *, check_ports: bool = False, require_controllers: bool = True) -> list[str]:
    """Return a list of human-readable problems; empty means valid.

    ``require_controllers=False`` accepts a bootstrap file that only holds the
    node identity and server connection; the node then waits for the server to
    push buses/controllers.
    """
    errors: list[str] = []
    node, server, buffer, logcfg = cfg["node"], cfg["server"], cfg["buffer"], cfg["logging"]

    if not isinstance(node.get("id"), str) or not NODE_ID_RE.match(node["id"]):
        errors.append("node.id is required: 1-64 chars, letters/digits/._- , starting with a letter or digit")
    for key in ("name", "description", "site", "location", "equipment"):
        if not isinstance(node.get(key) or "", str) or len(node.get(key) or "") > 200:
            errors.append(f"node.{key} must be text of at most 200 characters")

    url = server.get("url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
        errors.append("server.url must be an http:// or https:// URL")
    _num(errors, "server.request_timeout_s", server["request_timeout_s"], 1, 120)
    _num(errors, "server.upload_interval_s", server["upload_interval_s"], 1, 3600)
    _num(errors, "server.batch_size", server["batch_size"], 1, 1000, integer=True)
    _num(errors, "server.heartbeat_interval_s", server["heartbeat_interval_s"], 5, 3600)
    _num(errors, "server.config_sync_interval_s", server["config_sync_interval_s"], 10, 86400)
    _num(errors, "server.backoff_initial_s", server["backoff_initial_s"], 0.5, 600)
    _num(errors, "server.backoff_max_s", server["backoff_max_s"], 5, 3600)
    if server.get("ca_bundle") and not Path(server["ca_bundle"]).is_file():
        errors.append(f"server.ca_bundle {server['ca_bundle']} does not exist")

    buses = cfg["buses"]
    bus_names = [b.get("name") for b in buses]
    if not buses and require_controllers:
        errors.append("at least one entry in buses is required")
    if len(set(bus_names)) != len(bus_names):
        errors.append("bus names must be unique")
    ports = [b.get("port") for b in buses]
    if len(set(ports)) != len(ports):
        errors.append("two buses cannot use the same serial port")
    for i, bus in enumerate(buses):
        where = f"buses[{i}]"
        if not isinstance(bus.get("name"), str) or not NODE_ID_RE.match(bus["name"]):
            errors.append(f"{where}.name is invalid")
        port = bus.get("port")
        if not isinstance(port, str) or not port.strip():
            errors.append(f"{where}.port is required")
        elif check_ports and port.startswith("/dev/") and not os.path.exists(port):
            errors.append(f"{where}.port {port} does not exist on this node")
        _num(errors, f"{where}.timeout_s", bus["timeout_s"], 0.05, 10)
        _num(errors, f"{where}.retries", bus["retries"], 0, 10, integer=True)
        _num(errors, f"{where}.inter_request_delay_ms", bus["inter_request_delay_ms"], 0, 5000, integer=True)

    controllers = cfg["controllers"]
    if not controllers and require_controllers:
        errors.append("at least one entry in controllers is required")
    ids = [c.get("id") for c in controllers]
    if len(set(ids)) != len(ids):
        errors.append("controller ids must be unique")
    addresses = [(c.get("bus"), c.get("slave_id")) for c in controllers]
    if len(set(addresses)) != len(addresses):
        errors.append("two controllers on the same bus cannot share a slave_id")
    buses_by_name = {b.get("name"): b for b in buses}
    for i, ctrl in enumerate(controllers):
        where = f"controllers[{i}]"
        if not isinstance(ctrl.get("id"), str) or not NODE_ID_RE.match(ctrl["id"]):
            errors.append(f"{where}.id is invalid")
        profile = R.PROFILES.get(ctrl.get("model"))
        if profile is None:
            errors.append(f"{where}.model {ctrl.get('model')!r} is not supported (known: {', '.join(R.PROFILES)})")
        bus = buses_by_name.get(ctrl.get("bus"))
        if bus is None:
            errors.append(f"{where}.bus {ctrl.get('bus')!r} does not match any bus name")
        lo, hi = R.SLAVE_ID_RANGE
        _num(errors, f"{where}.slave_id", ctrl.get("slave_id"), lo, hi, integer=True)
        _num(errors, f"{where}.poll_interval_s", ctrl["poll_interval_s"], 1, 3600)
        _num(errors, f"{where}.parameter_read_interval_s", ctrl["parameter_read_interval_s"], 0, 604800)
        _num(errors, f"{where}.comm_loss_threshold", ctrl["comm_loss_threshold"], 1, 100, integer=True)
        overrides = ctrl.get("register_overrides") or {}
        if overrides.get("address_offset", 0) not in (-1, 0, 1):
            errors.append(f"{where}.register_overrides.address_offset must be -1, 0 or 1")
        if overrides.get("read_strategy", "auto") not in ("auto", "block", "segments"):
            errors.append(f"{where}.register_overrides.read_strategy must be auto, block or segments")
        if profile and bus:
            if bus.get("baudrate") not in profile.baudrates:
                errors.append(f"buses '{bus['name']}'.baudrate must be one of {profile.baudrates} for {profile.model}")
            if bus.get("parity") not in profile.parities:
                errors.append(f"buses '{bus['name']}'.parity must be one of {profile.parities} for {profile.model}")
            if bus.get("bytesize") not in profile.bytesizes:
                errors.append(f"buses '{bus['name']}'.bytesize must be one of {profile.bytesizes} for {profile.model}")
            if bus.get("stopbits") not in profile.stopbits:
                errors.append(f"buses '{bus['name']}'.stopbits must be one of {profile.stopbits} for {profile.model}")

    _num(errors, "buffer.retention_days_delivered", buffer["retention_days_delivered"], 0, 3650)
    _num(errors, "buffer.max_pending_records", buffer["max_pending_records"], 1000, 50_000_000, integer=True)
    _num(errors, "buffer.min_free_disk_mb", buffer["min_free_disk_mb"], 0, 100_000)
    if buffer["full_behavior"] not in ("drop_oldest", "stop_collecting"):
        errors.append("buffer.full_behavior must be drop_oldest or stop_collecting")
    if logcfg["level"] not in LOG_LEVELS:
        errors.append(f"logging.level must be one of {LOG_LEVELS}")
    if logcfg.get("format") not in ("text", "json"):
        errors.append("logging.format must be text or json")
    if not isinstance(cfg["safety"].get("allow_remote_control"), bool):
        errors.append("safety.allow_remote_control must be true or false")
    return errors


def filter_remote(remote: dict) -> tuple[dict, list[str]]:
    """Keep only the keys the server may change; return (filtered, ignored_keys)."""
    kept: dict = {}
    ignored: list[str] = []
    for section, value in (remote or {}).items():
        allowed = REMOTE_ALLOWED.get(section)
        if allowed is None:
            ignored.append(section)
        elif allowed == "*":
            kept[section] = value
        elif isinstance(value, dict):
            kept[section] = {k: v for k, v in value.items() if k in allowed}
            ignored += [f"{section}.{k}" for k in value if k not in allowed]
        else:
            ignored.append(section)
    return kept, ignored


def redact(obj: Any) -> Any:
    """Deep copy with secrets replaced, safe for logs and heartbeats."""
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ConfigManager:
    def __init__(self, local_path: str | os.PathLike, env: dict | None = None, check_ports: bool = True):
        self.local_path = Path(local_path)
        self.env = os.environ if env is None else env
        self.check_ports = check_ports
        self.local: dict = {}
        self.remote: dict = {}
        self.version = 0
        self.effective: dict = {}
        self.remote_load_error: str | None = None

    @property
    def remote_path(self) -> Path:
        return Path(self.effective.get("state_dir") or self.local.get("state_dir") or DEFAULTS["state_dir"]) \
            / "remote_config.json"

    def load(self) -> dict:
        """Load local file and persisted remote overlay.  Raises ConfigError if
        even the local file alone is invalid (the node cannot run)."""
        with open(self.local_path, encoding="utf-8") as fh:
            self.local = yaml.safe_load(fh) or {}
        if self.env.get(TOKEN_ENV):
            self.local.setdefault("server", {})["token"] = self.env[TOKEN_ENV]
        base = normalize(self.local)
        self.effective = base
        errors = validate(base, check_ports=False, require_controllers=False)
        remote_path = self.remote_path
        if remote_path.exists():
            try:
                stored = json.loads(remote_path.read_text(encoding="utf-8"))
                candidate = self.build_candidate(stored["config"])
                cand_errors = validate(candidate, check_ports=False)
                if cand_errors:
                    raise ConfigError(cand_errors)
                self.remote, self.version, self.effective = stored["config"], int(stored["version"]), candidate
                return self.effective
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # A corrupt/invalid overlay must not stop data collection: fall back to local.
                self.remote_load_error = f"stored remote configuration ignored, using local file: {exc}"
        if errors:
            raise ConfigError(errors)
        return self.effective

    def build_candidate(self, remote: dict) -> dict:
        filtered, _ = filter_remote(remote)
        return normalize(_merge(self.local, filtered))

    def evaluate_remote(self, remote: dict) -> tuple[dict, list[str], list[str]]:
        """Return (candidate, errors, ignored_keys) without changing anything."""
        filtered, ignored = filter_remote(remote)
        try:
            candidate = normalize(_merge(self.local, filtered))
            errors = validate(candidate, check_ports=self.check_ports)
        except ConfigError as exc:
            candidate, errors = {}, exc.errors
        except (TypeError, AttributeError, KeyError) as exc:
            candidate, errors = {}, [f"malformed configuration: {exc}"]
        return candidate, errors, ignored

    def commit_remote(self, version: int, remote: dict, candidate: dict) -> None:
        _atomic_write_json(self.remote_path, {"version": version, "config": remote})
        self.remote, self.version, self.effective = remote, version, candidate

    def public_view(self) -> dict:
        view = redact(self.effective)
        view["config_version"] = self.version
        return view
