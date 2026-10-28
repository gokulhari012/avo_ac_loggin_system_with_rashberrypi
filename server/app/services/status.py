"""Classification of node/controller state for dashboards.

A latest value is only presented as live when it is recent AND the node is
online AND the controller answered; every other case gets an explicit status
with a text label (colour is never the only signal).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from ..models import Controller, Node
from .settings import get_setting
from .timeutil import iso_utc, utcnow


@dataclass
class Status:
    code: str
    label: str
    level: str    # bootstrap contextual class: success / warning / danger / secondary / info
    icon: str     # short text symbol shown next to the label
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


NODE_STATUSES = {
    "disabled": Status("disabled", "Disabled", "secondary", "⏸"),
    "never_seen": Status("never_seen", "Never connected", "secondary", "?"),
    "online": Status("online", "Online", "success", "●"),
    "offline": Status("offline", "Offline", "danger", "✕"),
}


def node_offline_threshold() -> timedelta:
    return timedelta(seconds=get_setting("node_offline_after_s"))


def node_status(node: Node, now: datetime | None = None) -> Status:
    now = now or utcnow()
    if not node.enabled:
        return NODE_STATUSES["disabled"]
    if node.last_seen_at is None:
        return NODE_STATUSES["never_seen"]
    if now - node.last_seen_at > node_offline_threshold():
        return Status(**{**asdict(NODE_STATUSES["offline"]), "detail": "no contact since " + iso_utc(node.last_seen_at)})
    return NODE_STATUSES["online"]


def stale_threshold(ctrl: Controller) -> timedelta:
    base = get_setting("stale_after_s")
    interval = ctrl.poll_interval_s or 0
    return timedelta(seconds=max(base, 3 * interval))


def controller_status(ctrl: Controller, node: Node, now: datetime | None = None) -> Status:
    now = now or utcnow()
    if not node.enabled or not ctrl.enabled:
        return Status("disabled", "Disabled", "secondary", "⏸")
    if ctrl.last_reading_at is None:
        return Status("no_data", "No data yet", "secondary", "?", "no reading has been received")
    if node_status(node, now).code == "offline":
        return Status("node_offline", "Node offline", "danger", "✕",
                      "values below are the last ones received, not live")
    age = now - ctrl.last_reading_at
    if age > stale_threshold(ctrl):
        return Status("stale", "Stale data", "warning", "⌛",
                      f"latest reading is {int(age.total_seconds())} s old")
    if ctrl.last_comm_status and ctrl.last_comm_status != "ok":
        return Status("comm_failure", "Controller not responding", "danger", "⚠",
                      f"{ctrl.last_comm_status}: {ctrl.last_comm_error or ''}".strip(": "))
    if ctrl.last_quality == "probe_fault":
        return Status("probe_fault", "Probe fault", "danger", "⚠", "controller reports a temperature-probe failure")
    if ctrl.last_quality == "invalid":
        return Status("invalid", "Invalid measurement", "danger", "⚠", "value outside documented range or "
                                                                       "failed validation")
    faults = [f for f in ctrl.fault_list if f not in ("probe_fail_low", "probe_fail_high")]
    if faults:
        return Status("fault", "Controller fault", "danger", "▲", ", ".join(faults))
    if ctrl.last_quality == "suspect":
        return Status("suspect", "Check data", "warning", "!", "reading passed but a consistency check flagged it")
    return Status("ok", "Normal", "success", "✓")


def controller_snapshot(ctrl: Controller, node: Node, now: datetime | None = None) -> dict:
    now = now or utcnow()
    status = controller_status(ctrl, node, now)
    live = status.code in ("ok", "fault", "suspect")
    return {
        "id": ctrl.id, "uid": ctrl.uid, "name": ctrl.label, "node_id": node.id, "node_uid": node.uid,
        "node_name": node.name or node.uid, "site": node.site, "equipment": node.equipment,
        "status": status.to_dict(), "live": live,
        "temperature_c": ctrl.last_temperature_c, "setpoint_c": ctrl.last_setpoint_c,
        "compressor_on": ctrl.last_compressor_on, "alarm_on": ctrl.last_alarm_on,
        "defrost_active": ctrl.last_defrost_active, "faults": ctrl.fault_list,
        "quality": ctrl.last_quality, "comm_status": ctrl.last_comm_status,
        "last_reading_at": iso_utc(ctrl.last_reading_at), "last_good_at": iso_utc(ctrl.last_good_at),
        "age_s": int((now - ctrl.last_reading_at).total_seconds()) if ctrl.last_reading_at else None,
        "source": ctrl.last_source, "simulated": ctrl.last_source == "simulated",
    }
