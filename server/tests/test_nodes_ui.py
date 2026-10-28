"""Node management through the web UI."""

import json
import re

from app.extensions import db
from app.models import AuditEvent, Controller, ControllerCommand, Node, NodeConfigVersion
from app.services.security import hash_token
from app.services.settings import set_setting
from tests.conftest import make_node, node_headers, reading


def register_form(**overrides):
    data = {"uid": "PI-COLD-1", "name": "Cold room node", "site": "Plant A", "location": "Bldg 2",
            "equipment": "Cold Room 1", "upload_interval_s": "5", "batch_size": "200", "heartbeat_interval_s": "30",
            "config_sync_interval_s": "60", "request_timeout_s": "10", "backoff_max_s": "300",
            "bus_name": "rs485-1", "port": "/dev/ttyUSB0", "baudrate": "9600", "parity": "N", "timeout_s": "1.0",
            "retries": "2", "inter_request_delay_ms": "50",
            "controllers-0-uid": "ctrl-1", "controllers-0-name": "Cold room", "controllers-0-slave_id": "1",
            "controllers-0-poll_interval_s": "10", "controllers-0-parameter_read_interval_s": "3600",
            "controllers-0-address_offset": "0", "controllers-0-read_strategy": "auto", "controllers-0-enabled": "y",
            "retention_days_delivered": "7", "max_pending_records": "2000000", "full_behavior": "drop_oldest",
            "min_free_disk_mb": "100", "log_level": "INFO", "reason": "commissioning"}
    data.update(overrides)
    return data


def test_register_node_shows_token_once_and_stores_hash(as_user, users, client):
    admin = as_user("admin")
    r = admin.post("/nodes/new", data=register_form())
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    token = re.search(rb'id="token" value="([^"]+)"', r.data).group(1).decode()
    node = Node.query.filter_by(uid="PI-COLD-1").one()
    assert node.token_hash == hash_token(token) and token not in node.token_hash
    cv = NodeConfigVersion.query.filter_by(node_id=node.id).one()
    assert cv.version == 1 and cv.config["buses"][0]["port"] == "/dev/ttyUSB0"
    assert Controller.query.filter_by(node_id=node.id, uid="ctrl-1").one().slave_id == 1
    registered = AuditEvent.query.filter_by(action="node_registered").one()
    assert token not in (registered.new_value or "") + (registered.details_json or "")
    # the token works against the API
    hb = client.post("/api/v1/heartbeat", json={"node_id": "PI-COLD-1"}, headers=node_headers("PI-COLD-1", token))
    assert hb.status_code == 200 and hb.json["config_version"] == 1


def test_register_rejects_invalid_serial_settings(as_user, users):
    admin = as_user("admin")
    r = admin.post("/nodes/new", data=register_form(**{"controllers-0-slave_id": "300"}))
    assert r.status_code == 200 and Node.query.count() == 0


def test_register_duplicate_id(as_user, users):
    make_node("PI-COLD-1")
    r = as_user("admin").post("/nodes/new", data=register_form())
    assert b"already exists" in r.data


def test_edit_config_creates_new_version_with_audit_diff(as_user, users):
    node, _ = make_node()
    admin = as_user("admin")
    r = admin.post(f"/nodes/{node.id}/config", data=register_form(uid="", name="Renamed", **{
        "controllers-0-poll_interval_s": "30", "reason": "slower polling"}))
    assert r.status_code == 302
    versions = NodeConfigVersion.query.filter_by(node_id=node.id).order_by(NodeConfigVersion.version).all()
    assert [v.version for v in versions] == [1, 2] and versions[0].status == "superseded"
    assert versions[1].config["controllers"][0]["poll_interval_s"] == 30.0
    event = AuditEvent.query.filter_by(action="node_config_created").order_by(AuditEvent.id.desc()).first()
    assert event.reason == "slower polling" and "controllers" in event.new
    assert Node.query.one().name == "Renamed"


def test_edit_config_requires_reason(as_user, users):
    node, _ = make_node()
    r = as_user("admin").post(f"/nodes/{node.id}/config", data=register_form(uid="", reason=""))
    assert r.status_code == 200 and NodeConfigVersion.query.count() == 1


def test_advanced_json_config(as_user, users):
    node, _ = make_node()
    cfg = {"buses": [{"name": "a", "port": "/dev/ttyUSB0", "baudrate": 9600, "parity": "N", "bytesize": 8,
                      "stopbits": 1, "timeout_s": 1, "retries": 1, "inter_request_delay_ms": 50},
                     {"name": "b", "port": "/dev/ttyUSB1", "baudrate": 19200, "parity": "E", "bytesize": 8,
                      "stopbits": 1, "timeout_s": 1, "retries": 1, "inter_request_delay_ms": 50}],
           "controllers": [{"id": "c1", "model": "SZ-7526TX-REA", "bus": "a", "slave_id": 1, "poll_interval_s": 10},
                           {"id": "c2", "model": "SZ-7526TX-REA", "bus": "b", "slave_id": 1, "poll_interval_s": 10}]}
    r = as_user("admin").post(f"/nodes/{node.id}/config", data=register_form(
        uid="", advanced_json=json.dumps(cfg), reason="two buses"))
    assert r.status_code == 302
    assert {c.uid for c in Controller.query.filter_by(node_id=node.id, enabled=True)} == {"c1", "c2"}


def test_advanced_json_cannot_set_token_or_safety(as_user, users):
    node, _ = make_node()
    cfg = {"safety": {"allow_remote_control": True}, "server": {"token": "x"}, "buses": [], "controllers": []}
    r = as_user("admin").post(f"/nodes/{node.id}/config", data=register_form(
        uid="", advanced_json=json.dumps(cfg), reason="sneaky"))
    assert r.status_code == 200 and b"not allowed" in r.data and NodeConfigVersion.query.count() == 1


def test_disable_and_enable_node(as_user, users, client):
    node, token = make_node()
    admin = as_user("admin")
    admin.post(f"/nodes/{node.id}/disable", data={"reason": "maintenance"})
    assert Node.query.one().enabled is False
    assert client.post("/api/v1/readings", json={"readings": [reading()]},
                       headers=node_headers()).status_code == 403
    admin.post(f"/nodes/{node.id}/enable", data={"reason": "back"})
    assert Node.query.one().enabled is True
    actions = [a.action for a in AuditEvent.query.filter_by(category="node")]
    assert "node_disabled" in actions and "node_enabled" in actions


def test_token_rotation_revokes_old_token(as_user, users, client):
    node, old = make_node()
    r = as_user("admin").post(f"/nodes/{node.id}/token", data={"reason": "suspected leak"})
    new = re.search(rb'id="token" value="([^"]+)"', r.data).group(1).decode()
    assert client.post("/api/v1/heartbeat", json={}, headers=node_headers(token=old)).status_code == 401
    assert client.post("/api/v1/heartbeat", json={}, headers=node_headers(token=new)).status_code == 200


def test_connectivity_test_queues_read_only_commands(as_user, users, client):
    node, _ = make_node()
    client.post("/api/v1/readings", json={"readings": [reading()]}, headers=node_headers())
    as_user("admin").post(f"/nodes/{node.id}/test", data={"reason": "check"})
    assert [c.command for c in ControllerCommand.query] == ["identity_check"]


def test_write_command_needs_setting_permission_and_confirmation(as_user, users, client, app):
    node, _ = make_node()
    client.post("/api/v1/readings", json={"readings": [reading()]}, headers=node_headers())
    ctrl = Controller.query.filter_by(uid="ctrl-1").one()
    admin = as_user("admin")
    url = f"/controllers/{ctrl.id}/command"
    # remote control disabled by default -> refused and audited
    assert admin.post(url, data={"command": "defrost_start", "reason": "test", "confirm": "ctrl-1"}).status_code == 403
    assert AuditEvent.query.filter_by(action="command_refused").count() == 1
    set_setting("remote_control_enabled", "true", "admin")
    db.session.commit()
    # wrong confirmation -> not queued
    admin.post(url, data={"command": "defrost_start", "reason": "test", "confirm": "nope"})
    assert ControllerCommand.query.count() == 0
    admin.post(url, data={"command": "defrost_start", "reason": "test", "confirm": "ctrl-1"})
    assert ControllerCommand.query.one().command == "defrost_start"
    # operators cannot send commands even when enabled
    operator = as_user("operator")
    assert operator.post(url, data={"command": "alarm_mute", "reason": "x", "confirm": "ctrl-1"}).status_code == 403


def test_discovered_node_listed(as_user, users, client):
    client.post("/api/v1/heartbeat", json={"system": {"hostname": "pi-x"}}, headers=node_headers("UNKNOWN-9", "t"))
    r = as_user("admin").get("/nodes")
    assert b"UNKNOWN-9" in r.data and b"pi-x" in r.data
