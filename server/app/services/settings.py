"""System settings editable by administrators (stored in system_settings)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from flask import current_app, g, has_app_context

from ..extensions import db
from ..models import SystemSetting
from ..models.common import utcnow


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    parse: Callable[[str], Any]
    default: Callable[[], Any]
    help: str
    minimum: float | None = None
    maximum: float | None = None


def _bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


SETTINGS: dict[str, SettingDef] = {s.key: s for s in (
    SettingDef("display_timezone", "Display time zone", str, lambda: current_app.config["DISPLAY_TIMEZONE"],
               "IANA name, e.g. Asia/Kolkata. Data is always stored in UTC."),
    SettingDef("node_offline_after_s", "Node offline after (s)", int, lambda: 120,
               "A node with no heartbeat for this long is shown OFFLINE.", 30, 86400),
    SettingDef("stale_after_s", "Reading stale after (s)", int, lambda: 120,
               "Minimum age before a latest reading is flagged STALE (3x the poll interval is used if larger).",
               10, 86400),
    SettingDef("gap_factor", "Data gap factor", float, lambda: 3.0,
               "A gap between readings longer than this many poll intervals is reported as missing data.", 1.5, 100),
    SettingDef("dashboard_refresh_s", "Dashboard refresh (s)", int, lambda: 10,
               "How often dashboards and monitoring pages refresh without reloading.", 2, 3600),
    SettingDef("heartbeat_retention_days", "Heartbeat history retention (days)", int, lambda: 90,
               "Heartbeat rows older than this are purged. Readings and audit events are never purged.", 1, 3650),
    SettingDef("remote_control_enabled", "Allow controller commands", _bool,
               lambda: current_app.config["ENABLE_REMOTE_CONTROL"],
               "Master switch for alarm mute / fault reset / defrost commands. Each node must ALSO allow it "
               "in its local configuration file."),
)}


def _cache() -> dict | None:
    if not has_app_context():
        return None
    if "_settings_cache" not in g:
        g._settings_cache = {}
    return g._settings_cache


def get_setting(key: str) -> Any:
    """Read a setting (cached for the current request/app context)."""
    cache = _cache()
    if cache is not None and key in cache:
        return cache[key]
    definition = SETTINGS[key]
    row = db.session.get(SystemSetting, key)
    try:
        value = definition.default() if row is None else definition.parse(row.value)
    except (TypeError, ValueError):
        value = definition.default()
    if cache is not None:
        cache[key] = value
    return value


def all_settings() -> dict[str, Any]:
    return {key: get_setting(key) for key in SETTINGS}


def set_setting(key: str, raw_value: str, username: str) -> tuple[Any, Any]:
    """Validate and store; returns (previous, new).  Raises ValueError."""
    from .timeutil import valid_timezone

    definition = SETTINGS[key]
    value = definition.parse(raw_value)
    if key == "display_timezone" and not valid_timezone(value):
        raise ValueError(f"unknown time zone {value!r}")
    if definition.minimum is not None and not definition.minimum <= value <= definition.maximum:
        raise ValueError(f"{definition.label} must be between {definition.minimum} and {definition.maximum}")
    previous = get_setting(key)
    row = db.session.get(SystemSetting, key) or SystemSetting(key=key)
    row.value, row.updated_at, row.updated_by = str(value), utcnow(), username
    db.session.add(row)
    cache = _cache()
    if cache is not None:
        cache.pop(key, None)
    return previous, value
