"""Node API: authentication, validation, idempotent ingestion, config sync, commands."""

import uuid
from datetime import timedelta

from app.extensions import db
from app.models import (AuditEvent, CommunicationEvent, Controller, ControllerCommand, ControllerConfigHistory,
                        DiscoveredNode, FaultEvent, Node, NodeConfigVersion, NodeHeartbeat, TemperatureReading)
from app.services.node_config import create_config_version, default_config
from tests.conftest import comm_failure, make_node, node_headers, reading, utcnow


def post(client, path, body, headers=None):
    return client.post(f"/api/v1{path}", json=body, headers=headers or node_headers())


# ------------------------------------------------------------------ authentication
def test_ping_is_public(client):
    assert client.get("/api/v1/ping").json["status"] == "ok"


def test_missing_credentials(client, app):
    make_node()
    r = client.post("/api/v1/readings", json={"readings": [reading()]})
    assert r.status_code == 401 and r.json["error"]["code"] == "unauthorized"


def test_wrong_token_rejected_and_logged(client, app):
    make_node()
    r = post(client, "/readings", {"readings": [reading()]}, node_headers(token="wrong-token"))
    assert r.status_code == 401 and r.json["error"]["code"] == "invalid_credentials"
    node = Node.query.filter_by(uid="NODE-1").one()
    assert node.auth_failures == 1
    assert CommunicationEvent.query.filter_by(event_type="api_auth_failure").count() == 1
    assert AuditEvent.query.filter_by(action="node_auth_failed").count() == 1
    assert TemperatureReading.query.count() == 0


def test_unknown_node_recorded_as_discovered(client, app):
    r = post(client, "/heartbeat", {"system": {"hostname": "raspberrypi"}, "software_version": "1.0.0"},
             node_headers(uid="NEW-PI"))
    assert r.status_code == 401 and r.json["error"]["code"] == "unknown_node"
    d = DiscoveredNode.query.filter_by(uid="NEW-PI").one()
    assert d.hostname == "raspberrypi" and d.attempts == 1


def test_node_cannot_post_for_another_node(client, app):
    make_node()
    make_node("NODE-2", "other-token-0123456789")
    r = post(client, "/readings", {"node_id": "NODE-2", "readings": [reading()]})
    assert r.status_code == 403 and r.json["error"]["code"] == "node_mismatch"


def test_disabled_node_refused(client, app):
    node, _ = make_node()
    node.enabled = False
    db.session.commit()
    r = post(client, "/readings", {"readings": [reading()]})
    assert r.status_code == 403 and r.json["error"]["code"] == "node_disabled"


# ------------------------------------------------------------------ validation
def test_non_json_rejected(client, app):
    make_node()
    r = client.post("/api/v1/readings", data="x", headers={**node_headers(), "Content-Type": "text/plain"})
    assert r.status_code == 415


def test_empty_batch_rejected(client, app):
    make_node()
    assert post(client, "/readings", {"readings": []}).status_code == 400


def test_batch_limit(client, app):
    make_node()
    app.config["API_MAX_BATCH"] = 3
    r = post(client, "/readings", {"readings": [reading() for _ in range(4)]})
    assert r.status_code == 413 and r.json["error"]["details"]["max"] == 3


def test_request_size_limit(client, app):
    make_node()
    app.config["MAX_CONTENT_LENGTH"] = 2000
    r = post(client, "/readings", {"readings": [reading() for _ in range(20)]})
    assert r.status_code == 413


def test_invalid_records_rejected_individually(client, app):
    make_node()
    good = reading()
    bad_temp = reading(temperature_c="hot")
    bad_id = reading(reading_id="not-a-uuid")
    bad_temp_on_failure = comm_failure()
    bad_temp_on_failure["temperature_c"] = 5.0
    r = post(client, "/readings", {"readings": [good, bad_temp, bad_id, bad_temp_on_failure]})
    assert r.status_code == 200
    assert r.json["accepted"] == [good["reading_id"]]
    assert {x["id"] for x in r.json["rejected"]} == {bad_temp["reading_id"], "not-a-uuid",
                                                      bad_temp_on_failure["reading_id"]}
    assert TemperatureReading.query.count() == 1


# ------------------------------------------------------------------ ingestion
def test_ingest_and_persist_fields(client, app):
    node, _ = make_node()
    r1 = reading(temp=-18.2)
    r = post(client, "/readings", {"readings": [r1]})
    assert r.status_code == 200 and r.json == {"accepted": [r1["reading_id"]], "duplicates": [], "rejected": []}
    row = TemperatureReading.query.one()
    assert row.temperature_c == -18.2 and row.setpoint_c == -18.0 and row.compressor_on is True
    assert row.node_id == node.id and row.controller.uid == "ctrl-1" and row.source == "controller"
    assert row.raw["40001"] == 7507 and row.backfilled is False
    ctrl = Controller.query.filter_by(uid="ctrl-1").one()
    assert ctrl.last_temperature_c == -18.2 and ctrl.last_comm_status == "ok"


def test_duplicate_batch_is_idempotent(client, app):
    make_node()
    batch = [reading(), reading()]
    first = post(client, "/readings", {"readings": batch}).json
    second = post(client, "/readings", {"readings": batch}).json
    assert len(first["accepted"]) == 2 and second["accepted"] == []
    assert sorted(second["duplicates"]) == sorted(r["reading_id"] for r in batch)
    assert TemperatureReading.query.count() == 2


def test_duplicate_within_batch(client, app):
    make_node()
    r1 = reading()
    result = post(client, "/readings", {"readings": [r1, dict(r1)]}).json
    assert result["accepted"] == [r1["reading_id"]] and result["duplicates"] == [r1["reading_id"]]


def test_backfilled_reading_flagged_and_does_not_overwrite_latest(client, app):
    make_node()
    now = utcnow()
    post(client, "/readings", {"readings": [reading(at=now, temp=-18.0)]})
    post(client, "/readings", {"readings": [reading(at=now - timedelta(hours=2), temp=-5.0)]})
    old = TemperatureReading.query.filter_by(temperature_c=-5.0).one()
    assert old.backfilled is True
    assert Controller.query.one().last_temperature_c == -18.0


def test_unknown_controller_auto_registered(client, app):
    make_node()
    post(client, "/readings", {"readings": [reading(controller="ctrl-new")]})
    ctrl = Controller.query.filter_by(uid="ctrl-new").one()
    assert ctrl.auto_registered is True
    assert AuditEvent.query.filter_by(action="controller_auto_registered").count() == 1


def test_comm_failure_readings_stored_with_null_temperature(client, app):
    make_node()
    post(client, "/readings", {"readings": [reading(at=utcnow() - timedelta(seconds=20)), comm_failure()]})
    failed = TemperatureReading.query.filter_by(comm_status="timeout").one()
    assert failed.temperature_c is None and failed.quality == "comm_error"
    ctrl = Controller.query.one()
    assert ctrl.last_comm_status == "timeout" and ctrl.last_temperature_c == -18.2  # last good kept separately


def test_fault_transitions_create_events_and_audit(client, app):
    make_node()
    t0 = utcnow() - timedelta(minutes=5)
    batch = [reading(at=t0), reading(at=t0 + timedelta(seconds=10), faults=["high_temp"], fault_flags=4, alarm_on=True),
             comm_failure(at=t0 + timedelta(seconds=20)),
             reading(at=t0 + timedelta(seconds=30), faults=["high_temp"], fault_flags=4, alarm_on=True),
             reading(at=t0 + timedelta(seconds=40))]
    post(client, "/readings", {"readings": batch})
    events = FaultEvent.query.order_by(FaultEvent.kind).all()
    assert [(e.kind, e.code, e.status) for e in events] == [("alarm", "alarm_relay", "cleared"),
                                                            ("fault", "high_temp", "cleared")]
    ht = [e for e in events if e.code == "high_temp"][0]
    assert ht.started_at == t0 + timedelta(seconds=10) and ht.cleared_at == t0 + timedelta(seconds=40)
    actions = {a.action for a in AuditEvent.query.filter_by(category="fault")}
    assert actions == {"fault_raised", "fault_cleared", "alarm_on", "alarm_off"}


def test_probe_fault_reading(client, app):
    make_node()
    r = reading(temp=None, quality="probe_fault", faults=["probe_fail_low"], fault_flags=1)
    r["display_temperature_c"] = None
    assert post(client, "/readings", {"readings": [r]}).json["accepted"]
    assert FaultEvent.query.filter_by(code="probe_fail_low", status="active").count() == 1


# ------------------------------------------------------------------ events
def test_events_ingested_idempotently_and_audited(client, app):
    make_node()
    ev = {"event_id": str(uuid.uuid4()), "event_type": "comm_lost", "severity": "error", "message": "lost",
          "controller_id": "ctrl-1", "occurred_at": "2026-01-01T00:00:00Z", "details": {"comm_status": "timeout"}}
    assert post(client, "/events", {"events": [ev]}).json["accepted"] == [ev["event_id"]]
    assert post(client, "/events", {"events": [ev]}).json["duplicates"] == [ev["event_id"]]
    assert CommunicationEvent.query.filter_by(event_type="comm_lost").count() == 1
    assert AuditEvent.query.filter_by(action="controller_comm_lost").count() == 1


def test_parameter_snapshot_creates_history_and_detects_change(client, app):
    make_node()

    def snapshot(sp, digest):
        return {"event_id": str(uuid.uuid4()), "event_type": "parameter_snapshot", "severity": "info",
                "message": "snap", "controller_id": "ctrl-1", "occurred_at": "2026-01-01T00:00:00Z",
                "details": {"digest": digest, "rs": 0, "parameters": {
                    "SP": {"value": sp, "reference": 40031}, "PA": {"value": None, "redacted": True}}}}

    post(client, "/events", {"events": [snapshot(-18.0, "a")]})
    post(client, "/events", {"events": [snapshot(-20.0, "b")]})
    history = ControllerConfigHistory.query.filter_by(source="device_snapshot").order_by(
        ControllerConfigHistory.id).all()
    assert len(history) == 2
    change = AuditEvent.query.filter_by(action="controller_parameters_changed").one()
    assert change.previous == {"SP": -18.0} and change.new == {"SP": -20.0}


# ------------------------------------------------------------------ heartbeat & config
def heartbeat_body(version=0, **extra):
    return {"node_id": "NODE-1", "software_version": "1.0.0", "node_time": "2026-01-01T00:00:00Z",
            "uptime_s": 60, "config_version": version, "simulation": False, "clock_synchronized": True,
            "queue": {"pending_readings": 3}, "upload": {"consecutive_failures": 0},
            "controllers": [{"controller_id": "ctrl-1", "last_success_at": "2026-01-01T00:00:00Z"}],
            "system": {"hostname": "pi-1"}, "effective_config": {"server": {"token": "***"}}, **extra}


def test_heartbeat_updates_node_and_reports_desired_version(client, app):
    node, _ = make_node()
    r = post(client, "/heartbeat", heartbeat_body())
    assert r.status_code == 200 and r.json["config_version"] == 1 and r.json["pending_commands"] == 0
    node = Node.query.one()
    assert node.online and node.hostname == "pi-1" and node.queue_pending == 3
    assert NodeHeartbeat.query.count() == 1
    assert CommunicationEvent.query.filter_by(event_type="node_online").count() == 1


def test_software_version_change_audited(client, app):
    make_node()
    post(client, "/heartbeat", heartbeat_body())
    post(client, "/heartbeat", heartbeat_body(software_version="1.1.0"))
    assert AuditEvent.query.filter_by(action="software_version_changed").count() == 1


def test_config_fetch_and_ack(client, app):
    node, _ = make_node()
    offer = client.get("/api/v1/config?current_version=0", headers=node_headers())
    assert offer.status_code == 200 and offer.json["version"] == 1
    assert NodeConfigVersion.query.one().status == "delivered"
    assert client.get("/api/v1/config?current_version=1", headers=node_headers()).status_code == 204
    ack = post(client, "/config/ack", {"version": 1, "status": "applied", "errors": [], "ignored_keys": []})
    assert ack.status_code == 200
    cv = NodeConfigVersion.query.one()
    assert cv.status == "applied" and cv.acked_at is not None
    assert AuditEvent.query.filter_by(action="node_config_applied").count() == 1


def test_config_rejection_recorded(client, app):
    make_node()
    post(client, "/config/ack", {"version": 1, "status": "rejected", "errors": ["port missing"], "ignored_keys": []})
    cv = NodeConfigVersion.query.one()
    assert cv.status == "rejected" and "port missing" in cv.ack_errors
    assert Node.query.one().config_error == "port missing"


def test_new_version_supersedes_pending(client, app):
    node, _ = make_node()
    create_config_version(node, default_config(node), reason="second")
    db.session.commit()
    statuses = [v.status for v in NodeConfigVersion.query.order_by(NodeConfigVersion.version)]
    assert statuses == ["superseded", "pending"]


def test_heartbeat_marks_version_applied_when_ack_lost(client, app):
    make_node()
    client.get("/api/v1/config?current_version=0", headers=node_headers())
    post(client, "/heartbeat", heartbeat_body(version=1))
    assert NodeConfigVersion.query.one().status == "applied"


def test_ack_unknown_version(client, app):
    make_node()
    r = post(client, "/config/ack", {"version": 99, "status": "applied", "errors": [], "ignored_keys": []})
    assert r.status_code == 404


# ------------------------------------------------------------------ commands
def test_command_delivery_and_result(client, app):
    node, _ = make_node()
    post(client, "/readings", {"readings": [reading()]})
    ctrl = Controller.query.one()
    cmd = ControllerCommand(node_id=node.id, controller_id=ctrl.id, command="identity_check",
                            expires_at=utcnow() + timedelta(minutes=5))
    db.session.add(cmd)
    db.session.commit()
    assert post(client, "/heartbeat", heartbeat_body()).json["pending_commands"] == 1
    listed = client.get("/api/v1/commands", headers=node_headers()).json["commands"]
    assert listed[0]["command"] == "identity_check" and listed[0]["controller_id"] == "ctrl-1"
    r = post(client, f"/commands/{cmd.command_uid}/result", {"status": "succeeded", "result": {"identity_ok": True}})
    assert r.status_code == 200
    assert ControllerCommand.query.one().status == "succeeded"
    assert AuditEvent.query.filter_by(action="command_succeeded").count() == 1


def test_expired_commands_not_delivered(client, app):
    node, _ = make_node()
    post(client, "/readings", {"readings": [reading()]})
    db.session.add(ControllerCommand(node_id=node.id, controller_id=Controller.query.one().id,
                                     command="defrost_start", expires_at=utcnow() - timedelta(seconds=1)))
    db.session.commit()
    assert client.get("/api/v1/commands", headers=node_headers()).json["commands"] == []
