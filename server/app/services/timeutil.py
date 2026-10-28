"""Time handling: store naive UTC, display in the configured time zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models.common import utcnow

__all__ = ["utcnow", "parse_iso_utc", "to_local", "fmt_local", "parse_local_input", "display_zone",
           "iso_utc", "duration_text", "valid_timezone"]


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def display_zone() -> ZoneInfo:
    from .settings import get_setting
    try:
        return ZoneInfo(get_setting("display_timezone"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive input is treated as UTC.  Returns naive UTC."""
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp must be an ISO-8601 string")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso_utc(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_local(dt: datetime | None, zone: ZoneInfo | None = None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(zone or display_zone())


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S", zone: ZoneInfo | None = None) -> str:
    local = to_local(dt, zone)
    return "" if local is None else local.strftime(fmt)


def parse_local_input(value: str | None, zone: ZoneInfo | None = None) -> datetime | None:
    """Parse an HTML datetime-local value ('2026-01-31T14:30') entered in the
    display time zone; return naive UTC."""
    if not value:
        return None
    local = datetime.fromisoformat(value.strip())
    if local.tzinfo is None:
        local = local.replace(tzinfo=zone or display_zone())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def local_input_value(dt: datetime | None, zone: ZoneInfo | None = None) -> str:
    return fmt_local(dt, "%Y-%m-%dT%H:%M", zone)


def duration_text(seconds: float | timedelta | None) -> str:
    if seconds is None:
        return ""
    if isinstance(seconds, timedelta):
        seconds = seconds.total_seconds()
    seconds = int(round(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
