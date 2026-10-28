"""Whole-node tests: real NodeApp + simulated controller (RTU over TCP) + fake HTTP server."""

import sqlite3
import time
import uuid

import pytest
import yaml

from src.main import NodeApp
from tests.fake_server import FakeServer


def wait_for(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def server():
    srv = FakeServer().start()
    yield srv
    srv.stop()


def write_config(tmp_path, server, mock_url, **patch):
    cfg = {
        "node": {"id": "NODE-T1", "name": "Test node"},
        "server": {"url": server.url, "token": "test-token-123", "upload_interval_s": 1, "batch_size": 50,
                   "heartbeat_interval_s": 5, "config_sync_interval_s": 10, "backoff_initial_s": 0.5,
                   "backoff_max_s": 5, "request_timeout_s": 2},
        "buses": [{"name": "rs485-1", "port": mock_url, "timeout_s": 0.3, "retries": 0,
                   "inter_request_delay_ms": 0}],
        "controllers": [{"id": "ctrl-1", "bus": "rs485-1", "slave_id": 1, "poll_interval_s": 1,
                         "parameter_read_interval_s": 0, "comm_loss_threshold": 2}],
        "buffer": {"path": str(tmp_path / "queue.db")},
        "logging": {"level": "DEBUG"},
        "state_dir": str(tmp_path / "state"),
        "simulation": True,
    }
    for key, value in patch.items():
        cfg[key] = {**cfg.get(key, {}), **value} if isinstance(value, dict) else value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture
def make_app(tmp_path, server, mock_bus):
    apps = []

    def make(**patch):
        app = NodeApp(str(write_config(tmp_path, server, mock_bus[0].url, **patch)), env={},
                      check_ports=False, configure_logging=False)
        apps.append(app)
        return app

    yield make
    for app in apps:
        if not app.stop_event.is_set():
            app.shutdown()


def all_local_reading_ids(tmp_path, status=None):
    with sqlite3.connect(tmp_path / "queue.db") as db:
        if status:
            return [r[0] for r in db.execute("SELECT item_id FROM readings WHERE status=?", (status,))]
        return [r[0] for r in db.execute("SELECT item_id FROM readings")]


def assert_no_loss_no_extras(tmp_path, server):
    delivered = set(all_local_reading_ids(tmp_path, "delivered"))
    everything = set(all_local_reading_ids(tmp_path))
    on_server = set(server.state.readings)
    assert delivered <= on_server <= everything  # acked => stored; server has nothing the node did not send


def test_normal_polling_and_upload(make_app, server):
    app = make_app()
    app.start()
    assert wait_for(lambda: len(server.state.readings) >= 3)
    r = next(iter(server.state.readings.values()))
    assert r["source"] == "simulated" and r["comm_status"] == "ok" and r["quality"] == "good"
    assert r["controller_id"] == "ctrl-1" and r["slave_id"] == 1 and r["config_version"] == 0
    assert isinstance(r["temperature_c"], float) and r["setpoint_c"] == -18.0
    assert r["raw_registers"]["40001"] == 7507 and r["acquired_at"].endswith("Z")
    assert wait_for(lambda: any(e["event_type"] == "node_started" for e in server.state.events.values()))
    assert wait_for(lambda: server.state.heartbeats)
    hb = server.state.heartbeats[0]
    assert hb["node_id"] == "NODE-T1" and hb["simulation"] is True
    assert hb["effective_config"]["server"]["token"] == "***"


def test_outage_buffers_and_backfills_without_loss(make_app, server, tmp_path):
    app = make_app()
    app.start()
    assert wait_for(lambda: len(server.state.readings) >= 2)
    server.state.mode = "down"
    assert wait_for(lambda: app.queue.stats()["pending_readings"] >= 3, timeout=10)
    assert app.queue.get_state("outage_started_at") is not None
    server.state.mode = "up"
    assert wait_for(lambda: app.queue.stats()["pending_readings"] == 0, timeout=20)

    def restored():
        return [e for e in server.state.events.values() if e["event_type"] == "server_connection_restored"]

    assert wait_for(restored, timeout=10)
    assert restored()[0]["details"]["failed_attempts"] >= 1
    app.shutdown()
    assert_no_loss_no_extras(tmp_path, server)


def test_lost_acknowledgement_does_not_duplicate(make_app, server, tmp_path):
    app = make_app()
    server.state.mode = "lose_ack"
    app.start()
    assert wait_for(lambda: server.state.reading_posts >= 3, timeout=15)
    server.state.mode = "up"
    assert wait_for(lambda: app.queue.stats()["pending_readings"] == 0, timeout=20)
    app.shutdown()
    assert_no_loss_no_extras(tmp_path, server)


def test_restart_while_records_queued(tmp_path, server, mock_bus):
    server.state.mode = "down"
    path = write_config(tmp_path, server, mock_bus[0].url)
    first = NodeApp(str(path), env={}, check_ports=False, configure_logging=False)
    first.start()
    assert wait_for(lambda: first.queue.stats()["pending_readings"] >= 3, timeout=10)
    first.shutdown()
    queued = set(all_local_reading_ids(tmp_path))

    server.state.mode = "up"
    second = NodeApp(str(path), env={}, check_ports=False, configure_logging=False)
    second.start()
    try:
        assert wait_for(lambda: queued <= set(server.state.readings), timeout=20)
    finally:
        second.shutdown()


def test_remote_config_applied_and_acknowledged(make_app, server):
    server.state.config_offer = {"version": 2, "config": {
        "node": {"name": "Renamed by server"},
        "controllers": [{"id": "ctrl-1", "bus": "rs485-1", "slave_id": 1, "poll_interval_s": 2,
                         "parameter_read_interval_s": 0}],
        "server": {"token": "attempted-overwrite"},
    }}
    app = make_app()
    app.start()
    assert wait_for(lambda: server.state.acks, timeout=15)
    ack = server.state.acks[0]
    assert ack == {"version": 2, "status": "applied", "errors": [], "ignored_keys": ["server.token"]}
    assert app.cm.version == 2 and app.cfg["node"]["name"] == "Renamed by server"
    assert app.cfg["server"]["token"] == "test-token-123"
    assert wait_for(lambda: any(r["config_version"] == 2 for r in server.state.readings.values()))


def test_invalid_remote_config_rejected_and_last_good_kept(make_app, server):
    server.state.config_offer = {"version": 3, "config": {
        "buses": [{"name": "rs485-1", "port": "socket://127.0.0.1:1", "baudrate": 4800}],
        "controllers": [{"id": "ctrl-1", "bus": "rs485-1", "slave_id": 1}],
    }}
    app = make_app()
    app.start()
    assert wait_for(lambda: server.state.acks, timeout=15)
    ack = server.state.acks[0]
    assert ack["status"] == "rejected" and any("baudrate" in e for e in ack["errors"])
    assert app.cm.version == 0
    count = len(server.state.readings)
    assert wait_for(lambda: len(server.state.readings) > count + 1)  # still collecting with old config
    assert wait_for(lambda: any(e["event_type"] == "config_rejected" for e in server.state.events.values()))


def test_controller_communication_loss_and_recovery(make_app, server, mock_bus):
    _, bus, _ = mock_bus
    app = make_app()
    app.start()
    assert wait_for(lambda: len(server.state.readings) >= 1)
    bus.offline = True
    assert wait_for(lambda: any(e["event_type"] == "comm_lost" for e in server.state.events.values()), timeout=15)
    failed = [r for r in server.state.readings.values() if r["comm_status"] != "ok"]
    assert failed and all(r["temperature_c"] is None and r["quality"] == "comm_error" for r in failed)
    assert failed[0]["comm_status"] == "timeout"
    bus.offline = False
    assert wait_for(lambda: any(e["event_type"] == "comm_restored" for e in server.state.events.values()),
                    timeout=15)


def test_write_commands_rejected_unless_locally_enabled(make_app, server, mock_bus):
    _, _, controller = mock_bus
    server.state.commands = [
        {"command_id": str(uuid.uuid4()), "controller_id": "ctrl-1", "command": "defrost_start"},
        {"command_id": str(uuid.uuid4()), "controller_id": "ctrl-1", "command": "identity_check"},
    ]
    app = make_app()
    app.start()
    assert wait_for(lambda: len(server.state.command_results) == 2, timeout=15)
    write_result = server.state.command_results[server.state.commands[0]["command_id"]]
    read_result = server.state.command_results[server.state.commands[1]["command_id"]]
    assert write_result["status"] == "rejected" and "allow_remote_control" in write_result["result"]["error"]
    assert read_result["status"] == "succeeded" and read_result["result"]["identity_ok"] is True
    assert controller.coil_writes == []


def test_write_command_when_locally_enabled(make_app, server, mock_bus):
    _, _, controller = mock_bus
    cid = str(uuid.uuid4())
    server.state.commands = [{"command_id": cid, "controller_id": "ctrl-1", "command": "temp_log_reset"}]
    app = make_app(safety={"allow_remote_control": True})
    app.start()
    assert wait_for(lambda: cid in server.state.command_results, timeout=15)
    assert server.state.command_results[cid]["status"] == "succeeded"
    assert controller.coil_writes == [(4, True)]


def test_bad_token_keeps_data_buffered(make_app, server):
    server.state.token = "a-different-token"
    app = make_app()
    app.start()
    assert wait_for(lambda: app.queue.stats()["pending_readings"] >= 2)
    assert wait_for(lambda: "credentials" in (app.health.upload["last_error"] or ""))
    assert server.state.readings == {}


def test_supervisor_restarts_dead_worker(make_app):
    app = make_app()
    app.start()
    collector = app.collectors[0]
    app._gen_stop.set()          # simulate the generation dying
    collector.join(timeout=10)
    app.uploader.join(timeout=10)
    assert app.supervise_once() is False
    assert app.collectors[0] is not collector and app.collectors[0].is_alive()
