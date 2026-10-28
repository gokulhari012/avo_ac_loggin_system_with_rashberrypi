"""Ingestion of node data: readings, events and heartbeats.

Idempotency: every reading/event carries a UUID generated on the node.
A UUID that already exists is answered as ``duplicate`` (= already durably
stored), so a node may safely retry any batch.  The response is only sent
after the transaction commits.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (CommunicationEvent, Controller, ControllerCommand, ControllerConfigHistory, FaultEvent,
                      Node, NodeHeartbeat, TemperatureReading)
from ..models.common import dumps, loads, utcnow
from .audit import audit, diff
from .node_config import latest_version
from .security import redact
from .timeutil import parse_iso_utc

log = logging.getLogger(__name__)

COMM_STATUSES = {"ok", "timeout", "exception", "invalid_response", "port_error", "identity_mismatch", "error"}
QUALITIES = {"good", "suspect", "probe_fault", "invalid", "comm_error"}
SOURCES = {"controller", "simulated"}
SEVERITIES = {"debug", "info", "warning", "error", "critical"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CODE_RE = re.compile(r"^[a-z0-9_]{1,48}$")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

FAULT_DESCRIPTIONS = {
    "probe_fail_low": "Control probe fail low (PFL)",
    "probe_fail_high": "Control probe fail high (PFH)",
    "high_temp": "High temperature fault (Ht)",
    "low_temp": "Low temperature fault (Lt)",
    "door_open": "Door open fault (do)",
    "compressor_fault": "Compressor fault (CF)",
    "manual_compressor_fault": "Compressor fault awaiting manual reset",
    "alarm_relay": "Alarm relay ON",
}

_FLOATS = ("temperature_c", "display_temperature_c", "actual_temperature_c", "setpoint_c", "temp_log_high_c",
           "temp_log_low_c")
_BOOLS = ("compressor_on", "alarm_on", "defrost_active", "keypad_locked")
_INTS = {"fault_flags": (0, 0xFFFF), "system_status": (0, 0xFFFF), "relay_status": (0, 0xFFFF),
         "compressor_run_hours": (0, 0xFFFF), "resolution_rs": (-32768, 32767), "config_version": (0, 10**9),
         "slave_id": (0, 255), "poll_duration_ms": (0, 10**7), "balanced_defrost_hours": (0, 0xFFFF),
         "balanced_defrost_minutes": (0, 0xFFFF), "software_revision": (0, 0xFFFF)}


class ValidationError(ValueError):
    pass


def _is_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def validate_reading(item) -> dict:
    """Return a clean reading or raise ValidationError (per-record rejection)."""
    if not isinstance(item, dict):
        raise ValidationError("reading must be an object")
    if not _is_uuid(item.get("reading_id")):
        raise ValidationError("reading_id must be a UUID")
    if not isinstance(item.get("controller_id"), str) or not ID_RE.match(item["controller_id"]):
        raise ValidationError("controller_id is invalid")
    try:
        acquired = parse_iso_utc(item.get("acquired_at"))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"acquired_at: {exc}") from exc
    if acquired.year < 2000:
        raise ValidationError("acquired_at is before 2000 (node clock not set?)")
    for key, allowed in (("source", SOURCES), ("comm_status", COMM_STATUSES), ("quality", QUALITIES)):
        if item.get(key) not in allowed:
            raise ValidationError(f"{key} must be one of {sorted(allowed)}")
    clean = {"reading_id": item["reading_id"].lower(), "controller_id": item["controller_id"],
             "acquired_at": acquired, "source": item["source"], "comm_status": item["comm_status"],
             "quality": item["quality"]}
    for key in _FLOATS:
        value = item.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not -100.0 <= value <= 200.0):
            raise ValidationError(f"{key} must be a number between -100 and 200 or null")
        clean[key] = None if value is None else float(value)
    for key in _BOOLS:
        value = item.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValidationError(f"{key} must be true, false or null")
        clean[key] = value
    for key, (lo, hi) in _INTS.items():
        value = item.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi):
            raise ValidationError(f"{key} must be an integer between {lo} and {hi} or null")
        clean[key] = value
    for key in ("faults", "quality_flags"):
        value = item.get(key) or []
        if not isinstance(value, list) or len(value) > 32 or not all(isinstance(v, str) and CODE_RE.match(v)
                                                                       for v in value):
            raise ValidationError(f"{key} must be a list of short codes")
        clean[key] = value
    raw = item.get("raw_registers")
    if raw is not None:
        if not isinstance(raw, dict) or len(raw) > 100 or not all(
                isinstance(k, str) and k.isdigit() and isinstance(v, int) and 0 <= v <= 0xFFFF
                for k, v in raw.items()):
            raise ValidationError("raw_registers must map register numbers to 0..65535")
    clean["raw_registers"] = raw
    comm_error = item.get("comm_error")
    clean["comm_error"] = str(comm_error)[:500] if comm_error else None
    warnings = item.get("warnings") or []
    clean["extra"] = {k: item.get(k) for k in ("slave_id", "model", "poll_duration_ms", "balanced_defrost_hours",
                                               "balanced_defrost_minutes", "software_revision", "delays")
                      if item.get(k) is not None}
    if isinstance(warnings, list) and warnings:
        clean["extra"]["warnings"] = [str(w)[:300] for w in warnings[:10]]
    if clean["comm_status"] != "ok" and clean["temperature_c"] is not None:
        raise ValidationError("temperature_c must be null when comm_status is not ok")
    return clean


def _controller_for(node: Node, uid: str, cache: dict[str, Controller]) -> Controller:
    ctrl = cache.get(uid)
    if ctrl is None:
        ctrl = Controller.query.filter_by(node_id=node.id, uid=uid).first()
        if ctrl is None:
            ctrl = Controller(node_id=node.id, uid=uid, name=uid, auto_registered=True)
            db.session.add(ctrl)
            db.session.flush()
            audit("node", "controller_auto_registered", actor_type="node", actor_id=node.uid, actor_name=node.uid,
                  resource_type="controller", resource_id=f"{node.uid}/{uid}",
                  details={"note": "first data received for a controller not in the server configuration"})
        cache[uid] = ctrl
    return ctrl


def ingest_readings(node: Node, items: list, _resolve_conflicts: bool = True) -> dict:
    received = utcnow()
    backfill_after = timedelta(seconds=current_app.config["BACKFILL_THRESHOLD_S"])
    accepted: list[str] = []
    duplicates: list[str] = []
    rejected: list[dict] = []
    valid: list[dict] = []
    seen: set[str] = set()
    for item in items:
        try:
            clean = validate_reading(item)
        except ValidationError as exc:
            rid = item.get("reading_id") if isinstance(item, dict) else None
            rejected.append({"id": rid, "error": str(exc)})
            continue
        if clean["reading_id"] in seen:
            duplicates.append(clean["reading_id"])
            continue
        seen.add(clean["reading_id"])
        valid.append(clean)

    ids = [r["reading_id"] for r in valid]
    existing: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        existing.update(uid for (uid,) in db.session.query(TemperatureReading.reading_uid)
                        .filter(TemperatureReading.reading_uid.in_(chunk)))
    controllers: dict[str, Controller] = {}
    new_rows: list[tuple[dict, TemperatureReading]] = []
    for r in valid:
        if r["reading_id"] in existing:
            duplicates.append(r["reading_id"])
            continue
        ctrl = _controller_for(node, r["controller_id"], controllers)
        row = TemperatureReading(
            reading_uid=r["reading_id"], node_id=node.id, controller_id=ctrl.id, acquired_at=r["acquired_at"],
            received_at=received, backfilled=(received - r["acquired_at"]) > backfill_after, source=r["source"],
            config_version=r["config_version"], comm_status=r["comm_status"], comm_error=r["comm_error"],
            quality=r["quality"], quality_flags=",".join(r["quality_flags"]),
            temperature_c=r["temperature_c"], actual_temperature_c=r["actual_temperature_c"],
            setpoint_c=r["setpoint_c"], temp_log_high_c=r["temp_log_high_c"], temp_log_low_c=r["temp_log_low_c"],
            resolution_rs=r["resolution_rs"], compressor_on=r["compressor_on"], alarm_on=r["alarm_on"],
            defrost_active=r["defrost_active"], keypad_locked=r["keypad_locked"], fault_flags=r["fault_flags"],
            fault_codes=",".join(r["faults"]), system_status=r["system_status"], relay_status=r["relay_status"],
            compressor_run_hours=r["compressor_run_hours"],
            raw_registers=json.dumps(r["raw_registers"], sort_keys=True) if r["raw_registers"] else None,
            extra_json=dumps(r["extra"]) if r["extra"] else None,
        )
        db.session.add(row)
        new_rows.append((r, row))
        accepted.append(r["reading_id"])

    by_controller: dict[int, list[TemperatureReading]] = {}
    for _, row in new_rows:
        by_controller.setdefault(row.controller_id, []).append(row)
    for ctrl in controllers.values():
        rows = sorted(by_controller.get(ctrl.id, []), key=lambda x: x.acquired_at)
        if rows:
            _update_latest(ctrl, rows)
            _track_faults(node, ctrl, rows)
    if accepted:
        node.last_upload_at = received
    node.last_seen_at = received
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if not _resolve_conflicts:
            raise
        # A concurrent request stored some of the same readings first; resolve row by row.
        log.warning("concurrent duplicate ingestion for %s; retrying individually", node.uid)
        return _ingest_one_by_one(node, items)
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


def _ingest_one_by_one(node: Node, items: list) -> dict:
    result = {"accepted": [], "duplicates": [], "rejected": []}
    for item in items:
        try:
            single = ingest_readings(node, [item], _resolve_conflicts=False)
        except IntegrityError:
            rid = item.get("reading_id") if isinstance(item, dict) else None
            if rid and TemperatureReading.query.filter_by(reading_uid=str(rid).lower()).first() is not None:
                result["duplicates"].append(rid)
                continue
            raise  # not a duplicate: surface as a 500 so the node retries later
        for key in result:
            result[key].extend(single[key])
    return result


def _update_latest(ctrl: Controller, rows: list[TemperatureReading]) -> None:
    newest = rows[-1]
    if ctrl.last_reading_at is not None and newest.acquired_at < ctrl.last_reading_at:
        return  # an older backfilled batch must not overwrite newer live state
    ctrl.last_reading_at = newest.acquired_at
    ctrl.last_reading_uid = newest.reading_uid
    ctrl.last_quality = newest.quality
    ctrl.last_comm_status = newest.comm_status
    ctrl.last_comm_error = newest.comm_error
    ctrl.last_source = newest.source
    good = [r for r in rows if r.comm_status == "ok"]
    if good:
        g = good[-1]
        ctrl.last_good_at = g.acquired_at
        ctrl.last_temperature_c = g.temperature_c
        ctrl.last_setpoint_c = g.setpoint_c
        ctrl.last_compressor_on = g.compressor_on
        ctrl.last_alarm_on = g.alarm_on
        ctrl.last_defrost_active = g.defrost_active
        ctrl.last_fault_codes = g.fault_codes


def _track_faults(node: Node, ctrl: Controller, rows: list[TemperatureReading]) -> None:
    active = {(f.kind, f.code): f for f in FaultEvent.query.filter_by(controller_id=ctrl.id, status="active")}
    for r in rows:
        if r.comm_status != "ok" or r.fault_flags is None:
            continue  # state unknown while the controller cannot be read
        if ctrl.fault_state_at is not None and r.acquired_at <= ctrl.fault_state_at:
            continue  # out-of-order data must not create false transitions
        current = {("fault", code) for code in r.faults}
        if r.alarm_on:
            current.add(("alarm", "alarm_relay"))
        for key in current - set(active):
            kind, code = key
            event = FaultEvent(node_id=node.id, controller_id=ctrl.id, kind=kind, code=code,
                               description=FAULT_DESCRIPTIONS.get(code, code), status="active",
                               started_at=r.acquired_at, start_reading_uid=r.reading_uid,
                               start_temperature_c=r.temperature_c)
            db.session.add(event)
            active[key] = event
            audit("fault", "alarm_on" if kind == "alarm" else "fault_raised", actor_type="node",
                  actor_id=node.uid, actor_name=node.uid, resource_type="controller",
                  resource_id=f"{node.uid}/{ctrl.uid}", outcome="success",
                  details={"code": code, "description": event.description, "at": r.acquired_at.isoformat(),
                           "temperature_c": r.temperature_c, "reading_uid": r.reading_uid})
        for key in set(active) - current:
            event = active.pop(key)
            event.status, event.cleared_at, event.clear_reading_uid = "cleared", r.acquired_at, r.reading_uid
            audit("fault", "alarm_off" if key[0] == "alarm" else "fault_cleared", actor_type="node",
                  actor_id=node.uid, actor_name=node.uid, resource_type="controller",
                  resource_id=f"{node.uid}/{ctrl.uid}",
                  details={"code": key[1], "started_at": event.started_at.isoformat(),
                           "cleared_at": r.acquired_at.isoformat()})
        ctrl.fault_state_at = r.acquired_at


# ---------------------------------------------------------------------------- events
_AUDITED_EVENTS = {
    "comm_lost": ("communication", "controller_comm_lost"),
    "comm_restored": ("communication", "controller_comm_restored"),
    "server_connection_restored": ("communication", "node_reconnected"),
    "data_gap": ("communication", "data_gap_reported"),
    "node_started": ("node", "node_started"),
    "controller_command": ("command", "controller_command_executed"),
}


def validate_event(item) -> dict:
    if not isinstance(item, dict):
        raise ValidationError("event must be an object")
    if not _is_uuid(item.get("event_id")):
        raise ValidationError("event_id must be a UUID")
    if not isinstance(item.get("event_type"), str) or not EVENT_TYPE_RE.match(item["event_type"]):
        raise ValidationError("event_type is invalid")
    if item.get("severity", "info") not in SEVERITIES:
        raise ValidationError(f"severity must be one of {sorted(SEVERITIES)}")
    try:
        occurred = parse_iso_utc(item.get("occurred_at"))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"occurred_at: {exc}") from exc
    controller_id = item.get("controller_id")
    if controller_id is not None and (not isinstance(controller_id, str) or not ID_RE.match(controller_id)):
        raise ValidationError("controller_id is invalid")
    details = item.get("details") or {}
    if not isinstance(details, dict) or len(json.dumps(details, default=str)) > 64_000:
        raise ValidationError("details must be an object smaller than 64 kB")
    return {"event_id": item["event_id"].lower(), "event_type": item["event_type"],
            "severity": item.get("severity", "info"), "message": str(item.get("message", ""))[:1000],
            "occurred_at": occurred, "controller_id": controller_id, "details": details}


def ingest_events(node: Node, items: list) -> dict:
    accepted, duplicates, rejected = [], [], []
    controllers: dict[str, Controller] = {}
    for item in items:
        try:
            ev = validate_event(item)
        except ValidationError as exc:
            rejected.append({"id": item.get("event_id") if isinstance(item, dict) else None, "error": str(exc)})
            continue
        if CommunicationEvent.query.filter_by(event_uid=ev["event_id"]).first() is not None:
            duplicates.append(ev["event_id"])
            continue
        ctrl = _controller_for(node, ev["controller_id"], controllers) if ev["controller_id"] else None
        details = ev["details"]
        if ev["event_type"] == "parameter_snapshot" and ctrl is not None:
            _store_parameter_snapshot(node, ctrl, details, ev["occurred_at"])
            details = {"digest": details.get("digest"), "rs": details.get("rs")}
        db.session.add(CommunicationEvent(
            event_uid=ev["event_id"], origin="node", node_id=node.id, controller_id=ctrl.id if ctrl else None,
            occurred_at=ev["occurred_at"], event_type=ev["event_type"], severity=ev["severity"],
            message=ev["message"], details_json=dumps(redact(details))))
        if ev["event_type"] in _AUDITED_EVENTS:
            category, action = _AUDITED_EVENTS[ev["event_type"]]
            audit(category, action, actor_type="node", actor_id=node.uid, actor_name=node.uid,
                  resource_type="controller" if ctrl else "node",
                  resource_id=f"{node.uid}/{ctrl.uid}" if ctrl else node.uid,
                  outcome="failure" if ev["severity"] in ("error", "critical") else "success",
                  details={"message": ev["message"], "occurred_at": ev["occurred_at"].isoformat(), **details})
        accepted.append(ev["event_id"])
    node.last_seen_at = utcnow()
    db.session.commit()
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


def _store_parameter_snapshot(node: Node, ctrl: Controller, details: dict, at: datetime) -> None:
    params = details.get("parameters")
    digest = details.get("digest")
    if not isinstance(params, dict) or digest == ctrl.parameters_digest:
        ctrl.parameters_read_at = at
        return
    params = redact(params)
    previous = ctrl.parameters
    old_values = {k: v.get("value") for k, v in previous.items() if isinstance(v, dict)}
    new_values = {k: v.get("value") for k, v in params.items() if isinstance(v, dict)}
    before, after = diff(old_values, new_values)
    db.session.add(ControllerConfigHistory(
        controller_id=ctrl.id, source="device_snapshot", actor=node.uid,
        previous_json=dumps(before) if previous else None, new_json=dumps(new_values),
        changed_keys=dumps(sorted(after)) if previous else dumps([]),
        reason="initial parameter snapshot" if not previous else "parameter change detected on device"))
    if previous and after:
        audit("controller", "controller_parameters_changed", actor_type="node", actor_id=node.uid,
              actor_name=node.uid, resource_type="controller", resource_id=f"{node.uid}/{ctrl.uid}",
              previous=before, new=after, details={"source": "device snapshot (registers 40031-40068)"})
    ctrl.parameters_json = dumps(params)
    ctrl.parameters_digest = digest
    ctrl.parameters_read_at = at


# ---------------------------------------------------------------------------- heartbeat
_HEARTBEAT_SUMMARY_KEYS = ("queue", "upload", "config_sync", "controllers", "system", "clock_synchronized",
                           "recent_errors", "simulation", "uptime_s", "started_at")


def process_heartbeat(node: Node, payload: dict, ip: str | None) -> dict:
    now = utcnow()
    if not isinstance(payload, dict):
        raise ValidationError("heartbeat must be an object")
    node_time = None
    try:
        if payload.get("node_time"):
            node_time = parse_iso_utc(payload["node_time"])
    except (ValueError, TypeError):
        node_time = None
    version = payload.get("software_version")
    version = str(version)[:32] if version else None
    if version and node.software_version and version != node.software_version:
        audit("node", "software_version_changed", actor_type="node", actor_id=node.uid, actor_name=node.uid,
              resource_type="node", resource_id=node.uid, previous={"software_version": node.software_version},
              new={"software_version": version})
    if version:
        node.software_version = version
    probe = bool(payload.get("probe"))
    node.last_seen_at = now
    node.last_ip = ip
    if not probe:
        queue = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
        upload = payload.get("upload") if isinstance(payload.get("upload"), dict) else {}
        system = payload.get("system") if isinstance(payload.get("system"), dict) else {}
        config_version = payload.get("config_version")
        node.last_heartbeat_at = now
        node.hostname = str(system.get("hostname") or node.hostname or "")[:128] or None
        node.reported_config_version = config_version if isinstance(config_version, int) else None
        node.queue_pending = queue.get("pending_readings") if isinstance(queue.get("pending_readings"), int) else None
        node.clock_synchronized = payload.get("clock_synchronized") \
            if isinstance(payload.get("clock_synchronized"), bool) else None
        node.clock_skew_s = (now - node_time).total_seconds() if node_time else None
        node.simulation = bool(payload.get("simulation"))
        node.config_error = str(payload["config_error"])[:2000] if payload.get("config_error") else None
        polls = [c.get("last_success_at") for c in payload.get("controllers") or []
                 if isinstance(c, dict) and c.get("last_success_at")]
        if polls:
            try:
                node.last_poll_ok_at = max(parse_iso_utc(p) for p in polls)
            except (ValueError, TypeError):
                pass
        summary = redact({k: payload.get(k) for k in _HEARTBEAT_SUMMARY_KEYS})
        stored = redact({**summary, "effective_config": payload.get("effective_config"),
                         "config_version": config_version, "software_version": version})
        node.last_heartbeat_json = dumps(stored)[:200_000]
        db.session.add(NodeHeartbeat(
            node_id=node.id, received_at=now, node_time=node_time,
            uptime_s=payload.get("uptime_s") if isinstance(payload.get("uptime_s"), int) else None,
            software_version=version, config_version=node.reported_config_version,
            queue_pending=node.queue_pending,
            upload_failures=upload.get("consecutive_failures")
            if isinstance(upload.get("consecutive_failures"), int) else None,
            clock_synchronized=node.clock_synchronized, ip=ip,
            summary_json=dumps({"queue": queue, "upload": upload, "system": system})[:20_000]))
        # Ack lost but node reports the version as running: record it as applied.
        if node.reported_config_version:
            latest = latest_version(node)
            if latest and latest.version == node.reported_config_version and latest.status in ("pending",
                                                                                                "delivered"):
                latest.status, latest.acked_at = "applied", now
    if not node.online:
        mark_online(node, now)
    db.session.commit()
    latest = latest_version(node)
    pending = ControllerCommand.query.filter(ControllerCommand.node_id == node.id,
                                             ControllerCommand.status == "pending",
                                             ControllerCommand.expires_at > now).count()
    return {"server_time": now.isoformat() + "Z", "config_version": latest.version if latest else None,
            "pending_commands": pending}


def mark_online(node: Node, now: datetime) -> None:
    previous_change = node.online_changed_at
    node.online, node.online_changed_at = True, now
    db.session.add(CommunicationEvent(origin="server", node_id=node.id, occurred_at=now, event_type="node_online",
                                      severity="info", message=f"Node {node.uid} is online",
                                      details_json=dumps({"offline_since": previous_change.isoformat()
                                                          if previous_change else None})))
    audit("communication", "node_online", actor_type="system", resource_type="node", resource_id=node.uid,
          details={"offline_since": previous_change.isoformat() if previous_change else None})


def mark_offline(node: Node, now: datetime) -> None:
    node.online, node.online_changed_at = False, now
    db.session.add(CommunicationEvent(origin="server", node_id=node.id, occurred_at=now, event_type="node_offline",
                                      severity="error", message=f"Node {node.uid} stopped reporting",
                                      details_json=dumps({"last_seen_at": node.last_seen_at.isoformat()
                                                          if node.last_seen_at else None})))
    audit("communication", "node_offline", actor_type="system", resource_type="node", resource_id=node.uid,
          outcome="failure",
          details={"last_seen_at": node.last_seen_at.isoformat() if node.last_seen_at else None})


def heartbeat_effective(node: Node) -> dict:
    return loads(node.last_heartbeat_json, {})
