"""Run one or more SIMULATED Raspberry Pi nodes against a real server.

Each simulated node is the real node software (raspberry_pi_node/src) polling
a simulated SZ-7526TX-REA over RTU-over-TCP.  All readings are labelled
"simulated" on the server.  Run it with the NODE virtualenv:

  # register automatically through the web UI (prompts for the admin password):
  raspberry_pi_node\\.venv\\Scripts\\python simulator\\simulated_node.py --server http://127.0.0.1:8000 \\
      --register --admin admin --nodes 2 --controllers 2

  # or use a node you registered yourself (serial port socket://127.0.0.1:5020):
  ... simulated_node.py --server http://127.0.0.1:8000 --node-id SIM-1 --token <token>
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "raspberry_pi_node"))
sys.path.insert(0, str(HERE))

from mock_controller import Bus, MockController, TcpRtuServer  # noqa: E402
from server_ui import ServerUI  # noqa: E402

from src.logging_config import setup_logging  # noqa: E402
from src.main import NodeApp  # noqa: E402

RUN_DIR = HERE / "run"


def write_config(node_id: str, server: str, token: str, mock_url: str, slave_ids: list[int], poll: float) -> Path:
    state = RUN_DIR / node_id
    state.mkdir(parents=True, exist_ok=True)
    cfg = {
        "node": {"id": node_id, "name": f"Simulated {node_id}", "site": "Simulation"},
        "server": {"url": server, "token": token, "upload_interval_s": 2, "heartbeat_interval_s": 10,
                   "config_sync_interval_s": 30, "backoff_max_s": 30},
        "buses": [{"name": "rs485-1", "port": mock_url, "timeout_s": 0.5, "retries": 1, "inter_request_delay_ms": 20}],
        "controllers": [{"id": f"ctrl-{s}", "bus": "rs485-1", "slave_id": s, "poll_interval_s": poll,
                         "parameter_read_interval_s": 600} for s in slave_ids],
        "buffer": {"path": str(state / "queue.db"), "min_free_disk_mb": 50},
        "logging": {"level": "INFO", "file": str(state / "node.log")},
        "state_dir": str(state),
        "simulation": True,
    }
    path = state / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Simulated temperature nodes (data is labelled SIMULATED)")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--controllers", type=int, default=1, help="controllers per node (slave IDs 1..N)")
    ap.add_argument("--node-id", default=None, help="node ID (single node, with --token)")
    ap.add_argument("--token", default=None)
    ap.add_argument("--register", action="store_true", help="register the nodes through the web UI")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--base-port", type=int, default=5020)
    ap.add_argument("--poll", type=float, default=10)
    ap.add_argument("--time-scale", type=float, default=5.0, help="speed up the thermal model")
    ap.add_argument("--drop-rate", type=float, default=0.0, help="fraction of Modbus requests left unanswered")
    args = ap.parse_args()
    setup_logging({"level": "INFO"})

    slave_ids = list(range(1, args.controllers + 1))
    ui = None
    if args.register:
        ui = ServerUI(args.server)
        password = os.environ.get("SIM_ADMIN_PASSWORD") or getpass.getpass(f"Password for {args.admin}: ")
        ui.login(args.admin, password)
    elif not (args.node_id and args.token):
        ap.error("either --register or both --node-id and --token are required")

    apps, servers = [], []
    for i in range(args.nodes):
        node_id = args.node_id if args.node_id and args.nodes == 1 else f"SIM-NODE-{i + 1}"
        controllers = [MockController(s, 0, scenario="demo", time_scale=args.time_scale, start_temp_c=-16.0 - s)
                       for s in slave_ids]
        mock = TcpRtuServer(Bus(controllers, drop_rate=args.drop_rate), port=args.base_port + i).start()
        servers.append(mock)
        token = args.token
        if ui is not None:
            token = ui.register_node(node_id, f"Simulated node {i + 1}", mock.url, slave_ids,
                                     poll_interval_s=args.poll)
            print(f"registered {node_id}; token stored in {RUN_DIR / node_id / 'config.yaml'}")
        config = write_config(node_id, args.server, token, mock.url, slave_ids, args.poll)
        app = NodeApp(str(config), env={}, check_ports=False, configure_logging=False)
        app.start()
        apps.append(app)
        print(f"{node_id}: simulated controller(s) {slave_ids} on {mock.url}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print("Simulated nodes running - Ctrl+C to stop.")
    while not stop.wait(15):
        for app in apps:
            app.supervise_once()
    for app in apps:
        app.shutdown()
    for mock in servers:
        mock.stop()
    return 0


if __name__ == "__main__":
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    sys.exit(main())
