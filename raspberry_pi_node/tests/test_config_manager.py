import json

import pytest
import yaml

from src.config_manager import ConfigError, ConfigManager, filter_remote, normalize, redact, validate


def local_config(tmp_path, **overrides):
    cfg = {
        "node": {"id": "NODE-T1", "name": "Test"},
        "server": {"url": "http://127.0.0.1:9", "token": "secret-token"},
        "buses": [{"name": "rs485-1", "port": "socket://127.0.0.1:5020"}],
        "controllers": [{"id": "ctrl-1", "bus": "rs485-1", "slave_id": 1}],
        "buffer": {"path": str(tmp_path / "q.db")},
        "state_dir": str(tmp_path / "state"),
    }
    cfg.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def test_valid_example_config():
    with open("config.example.yaml", encoding="utf-8") as fh:
        cfg = normalize(yaml.safe_load(fh))
    assert validate(cfg) == []


@pytest.mark.parametrize("bus_patch,expected", [
    ({"baudrate": 57600}, "baudrate"),
    ({"parity": "X"}, "parity"),
    ({"stopbits": 2}, "stopbits"),
    ({"bytesize": 7}, "bytesize"),
    ({"timeout_s": 0}, "timeout_s"),
])
def test_invalid_serial_settings(tmp_path, bus_patch, expected):
    cfg = normalize(yaml.safe_load(local_config(tmp_path).read_text()))
    cfg["buses"][0].update(bus_patch)
    assert any(expected in e for e in validate(cfg))


@pytest.mark.parametrize("slave_id", [0, 251, "1", None])
def test_invalid_slave_id(tmp_path, slave_id):
    cfg = normalize(yaml.safe_load(local_config(tmp_path).read_text()))
    cfg["controllers"][0]["slave_id"] = slave_id
    assert any("slave_id" in e for e in validate(cfg))


def test_duplicate_slave_on_same_bus(tmp_path):
    cfg = normalize(yaml.safe_load(local_config(tmp_path).read_text()))
    cfg["controllers"].append({**cfg["controllers"][0], "id": "ctrl-2"})
    assert any("share a slave_id" in e for e in validate(cfg))


def test_token_from_environment(tmp_path):
    cm = ConfigManager(local_config(tmp_path), env={"TEMP_NODE_TOKEN": "from-env"}, check_ports=False)
    assert cm.load()["server"]["token"] == "from-env"


def test_bootstrap_config_without_controllers_is_accepted(tmp_path):
    path = local_config(tmp_path, buses=[], controllers=[])
    cm = ConfigManager(path, env={}, check_ports=False)
    assert cm.load()["controllers"] == []


def test_invalid_local_config_raises(tmp_path):
    cm = ConfigManager(local_config(tmp_path, server={"url": "ftp://x"}), env={}, check_ports=False)
    with pytest.raises(ConfigError):
        cm.load()


def test_remote_overlay_cannot_touch_local_only_keys():
    kept, ignored = filter_remote({
        "server": {"url": "http://new:8000", "token": "stolen"},
        "safety": {"allow_remote_control": True},
        "buffer": {"path": "/tmp/x", "full_behavior": "stop_collecting"},
        "controllers": [],
    })
    assert kept["server"] == {"url": "http://new:8000"}
    assert "safety" not in kept and kept["buffer"] == {"full_behavior": "stop_collecting"}
    assert set(ignored) == {"server.token", "safety", "buffer.path"}


def test_commit_and_reload_remote(tmp_path):
    cm = ConfigManager(local_config(tmp_path), env={}, check_ports=False)
    cm.load()
    remote = {"controllers": [{"id": "ctrl-9", "bus": "rs485-1", "slave_id": 9, "poll_interval_s": 30}],
              "node": {"name": "Renamed"}}
    candidate, errors, ignored = cm.evaluate_remote(remote)
    assert errors == [] and ignored == []
    cm.commit_remote(4, remote, candidate)

    cm2 = ConfigManager(local_config(tmp_path), env={}, check_ports=False)
    cfg = cm2.load()
    assert cm2.version == 4 and cfg["controllers"][0]["slave_id"] == 9 and cfg["node"]["name"] == "Renamed"
    assert cfg["server"]["token"] == "secret-token"  # local secret preserved


def test_invalid_remote_is_reported_not_applied(tmp_path):
    cm = ConfigManager(local_config(tmp_path), env={}, check_ports=False)
    cm.load()
    _, errors, _ = cm.evaluate_remote({"buses": [{"name": "rs485-1", "port": "x", "baudrate": 1200}]})
    assert errors and cm.version == 0


def test_corrupt_stored_remote_falls_back_to_local(tmp_path):
    path = local_config(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / "remote_config.json").write_text(json.dumps({"version": 3, "config": {"buses": "garbage"}}))
    cm = ConfigManager(path, env={}, check_ports=False)
    cfg = cm.load()
    assert cm.version == 0 and cfg["controllers"][0]["id"] == "ctrl-1" and cm.remote_load_error


def test_redaction():
    view = redact({"server": {"token": "abc", "url": "u"}, "list": [{"password": "p"}]})
    assert view["server"]["token"] == "***" and view["list"][0]["password"] == "***"


def test_missing_port_detected_when_checking(tmp_path):
    cfg = normalize(yaml.safe_load(local_config(tmp_path).read_text()))
    cfg["buses"][0]["port"] = "/dev/ttyDOESNOTEXIST"
    assert any("does not exist" in e for e in validate(cfg, check_ports=True))
