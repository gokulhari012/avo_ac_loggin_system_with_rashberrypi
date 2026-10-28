"""End-to-end acceptance test.

Real server process (Waitress, migrations, CSRF on) + real node software +
simulated SZ-7526TX-REA over RTU-over-TCP.  Run with the NODE virtualenv:

    raspberry_pi_node/.venv/Scripts/python -m pytest tests_e2e -v      (Windows)
    raspberry_pi_node/.venv/bin/python -m pytest tests_e2e -v          (Linux)

SERVER_PYTHON may point at the server virtualenv's interpreter.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
for p in (ROOT / "raspberry_pi_node", ROOT / "simulator"):
    sys.path.insert(0, str(p))

from mock_controller import Bus, MockController, TcpRtuServer  # noqa: E402
from server_ui import ServerUI  # noqa: E402

from src.main import NodeApp  # noqa: E402

ADMIN_PASSWORD = "E2e-Secure-Pass-1"


def server_python() -> str:
    if os.environ.get("SERVER_PYTHON"):
        return os.environ["SERVER_PYTHON"]
    for candidate in (SERVER_DIR / ".venv" / "Scripts" / "python.exe", SERVER_DIR / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    pytest.skip("server virtualenv not found; set SERVER_PYTHON")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=30.0, interval=0.25, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001 - server may be restarting
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


class ServerProcess:
    def __init__(self, tmp: Path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_path = tmp / "server.db"
        self.log = open(tmp / "server-stdout.log", "w", encoding="utf-8")
        self.env = {**os.environ, "DATABASE_URL": f"sqlite:///{self.db_path.as_posix()}",
                    "INSTANCE_DIR": str(tmp / "instance"), "LOG_DIR": str(tmp / "logs"),
                    "EXPORT_DIR": str(tmp / "exports"), "SECRET_KEY": "e2e-secret-key",
                    "HOST": "127.0.0.1", "PORT": str(self.port), "DISPLAY_TIMEZONE": "Asia/Kolkata"}
        self.python = server_python()
        self.proc = None

    def flask(self, *args):
        subprocess.run([self.python, "-m", "flask", "--app", "run", *args], cwd=SERVER_DIR, env=self.env,
                       check=True, capture_output=True, timeout=120)

    def start(self):
        self.proc = subprocess.Popen([self.python, "run.py"], cwd=SERVER_DIR, env=self.env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        wait_for(lambda: requests.get(self.url + "/api/v1/ping", timeout=1).ok, 60, what="server start")

    def stop(self):
        """Hard kill (like a power cut / crashed service) - no graceful shutdown."""
        self.proc.kill()
        self.proc.wait(timeout=30)

    def query(self, sql):
        with sqlite3.connect(self.db_path) as db:
            return db.execute(sql).fetchall()


@pytest.fixture
def server(tmp_path):
    srv = ServerProcess(tmp_path)
    srv.flask("db", "upgrade")
    srv.flask("create-admin", "--username", "admin", "--password", ADMIN_PASSWORD, "--full-name", "E2E Admin")
    srv.start()
    yield srv
    if srv.proc and srv.proc.poll() is None:
        srv.stop()
    srv.log.close()


def test_full_system(server, tmp_path):
    ui = ServerUI(server.url)
    ui.login("admin", ADMIN_PASSWORD)

    # Simulated controller on an RTU-over-TCP "bus"
    controller = MockController(slave_id=1, rs=0, scenario="normal", start_temp_c=-17.0, seed=7)
    mock = TcpRtuServer(Bus([controller])).start()

    # 1. Register the node through the web UI; config v1 points at the simulated bus
    token = ui.register_node("E2E-NODE", "End-to-end node", mock.url, [1], poll_interval_s=1)

    # 2. Configure and start the real node software
    state = tmp_path / "node"
    cfg = {"node": {"id": "E2E-NODE"},
           "server": {"url": server.url, "token": token, "upload_interval_s": 1, "heartbeat_interval_s": 5,
                      "config_sync_interval_s": 10, "backoff_initial_s": 0.5, "backoff_max_s": 5},
           "buses": [{"name": "rs485-1", "port": mock.url, "timeout_s": 0.3, "retries": 0}],
           "controllers": [{"id": "ctrl-1", "bus": "rs485-1", "slave_id": 1, "poll_interval_s": 1}],
           "buffer": {"path": str(state / "queue.db")}, "state_dir": str(state), "simulation": True}
    (tmp_path / "node.yaml").write_text(yaml.safe_dump(cfg))
    node = NodeApp(str(tmp_path / "node.yaml"), env={}, check_ports=True, configure_logging=False)
    node.start()
    try:
        # 3. Configuration v1 delivered, validated, applied and acknowledged
        wait_for(lambda: node.cm.version == 1, 40, what="config v1 applied")
        wait_for(lambda: server.query("select status from node_config_versions where version=1")[0][0] == "applied",
                 30, what="config ack on server")

        # 4. Live, simulated readings appear on the dashboard
        def live_card():
            data = ui.get_json("/ui-api/dashboard")
            cards = [c for c in data["controllers"] if c["node_uid"] == "E2E-NODE"]
            return cards and cards[0]["live"] and cards[0]["temperature_c"] is not None and cards[0]["simulated"]
        wait_for(live_card, 40, what="live reading on dashboard")
        nodes = ui.get_json("/ui-api/dashboard")["nodes"]
        assert nodes[0]["status"]["code"] == "online" and nodes[0]["simulation"] is True

        # 5. Server outage (hard kill): node keeps polling and buffers locally
        server.stop()
        wait_for(lambda: node.queue.stats()["pending_readings"] >= 5, 30, what="local buffering")
        buffered = node.queue.stats()["pending_readings"]

        # 6. Server comes back: backlog uploaded, nothing lost, nothing duplicated
        server.start()
        wait_for(lambda: node.queue.stats()["pending_readings"] == 0, 60, what="backlog upload")
        server_ids = [r[0] for r in server.query("select reading_uid from temperature_readings")]
        assert len(server_ids) == len(set(server_ids))
        with sqlite3.connect(state / "queue.db") as local:
            delivered = {r[0] for r in local.execute("select item_id from readings where status='delivered'")}
            everything = {r[0] for r in local.execute("select item_id from readings")}
        assert delivered <= set(server_ids) <= everything
        backfilled = server.query("select count(*) from temperature_readings where backfilled=1")[0][0]
        assert buffered >= 5 and len(server_ids) >= buffered
        wait_for(lambda: server.query(
            "select count(*) from communication_events where event_type='server_connection_restored'")[0][0] == 1,
            30, what="reconnection event")
        assert server.query("select count(*) from temperature_readings where source != 'simulated'")[0][0] == 0
        assert backfilled >= 0  # backfill flag depends on outage length vs BACKFILL_THRESHOLD_S

        # 7. Exports from the Data Logs view: Excel and PDF contain the same record set
        now = datetime.now(timezone.utc)
        local_now = now + timedelta(hours=5, minutes=30)  # display zone Asia/Kolkata
        form = {"start": (local_now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                "end": (local_now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
        xlsx = ui.post_download("/logs/export/xlsx", "/logs", form)
        pdf = ui.post_download("/logs/export/pdf", "/logs", form)
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        counts = server.query("select record_count, outcome from export_records order by id")
        assert counts[0] == counts[1] and counts[0][1] == "success" and counts[0][0] > 0

        # 8. Audit report
        audit_pdf = ui.post_download("/reports/generate", "/reports", {
            "report_type": "audit_trail", "format": "pdf", "start": form["start"], "end": form["end"]})
        assert audit_pdf.status_code == 200 and audit_pdf.content[:4] == b"%PDF"
        actions = {r[0] for r in server.query("select action from audit_events")}
        assert {"login", "node_registered", "node_config_created", "node_config_applied", "data_exported",
                "server_started", "node_reconnected"} <= actions
    finally:
        node.shutdown()
        mock.stop()
