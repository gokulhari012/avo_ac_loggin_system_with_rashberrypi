"""A minimal in-process implementation of the server's node API for node tests.

Modes:
  "up"       normal operation
  "down"     connections are dropped without a response (Wi-Fi/server outage)
  "error"    every request returns HTTP 503
  "lose_ack" readings are stored but the response is a 500, so the node must
             retry and the server must answer "duplicate"
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeServerState:
    def __init__(self, node_id: str, token: str):
        self.node_id = node_id
        self.token = token
        self.mode = "up"
        self.readings: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.heartbeats: list[dict] = []
        self.config_offer: dict | None = None
        self.acks: list[dict] = []
        self.commands: list[dict] = []
        self.command_results: dict[str, dict] = {}
        self.reading_posts = 0
        self.lock = threading.Lock()


def make_handler(state: FakeServerState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=None):
            data = json.dumps(body or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _gate(self) -> bool:
            if state.mode == "down":
                self.close_connection = True
                self.connection.close()
                return False
            if state.mode == "error":
                self._send(503, {"error": {"code": "unavailable", "message": "maintenance"}})
                return False
            auth = self.headers.get("Authorization", "")
            if self.headers.get("X-Node-ID") != state.node_id or auth != f"Bearer {state.token}":
                self._send(401, {"error": {"code": "unauthorized", "message": "bad credentials"}})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if not self._gate():
                return
            url = urlparse(self.path)
            if url.path == "/api/v1/config":
                current = int(parse_qs(url.query).get("current_version", ["0"])[0])
                offer = state.config_offer
                if offer and offer["version"] != current:
                    return self._send(200, offer)
                self.send_response(204)
                self.end_headers()
                return None
            if url.path == "/api/v1/commands":
                with state.lock:
                    pending = [c for c in state.commands if c["command_id"] not in state.command_results]
                return self._send(200, {"commands": pending})
            return self._send(404)

        def do_POST(self):
            if not self._gate():
                return
            body = self._body()
            path = urlparse(self.path).path
            if path == "/api/v1/readings":
                accepted, duplicates = [], []
                with state.lock:
                    state.reading_posts += 1
                    for r in body["readings"]:
                        if r["reading_id"] in state.readings:
                            duplicates.append(r["reading_id"])
                        else:
                            state.readings[r["reading_id"]] = r
                            accepted.append(r["reading_id"])
                if state.mode == "lose_ack":
                    return self._send(500, {"error": {"code": "internal", "message": "ack lost"}})
                return self._send(200, {"accepted": accepted, "duplicates": duplicates, "rejected": []})
            if path == "/api/v1/events":
                accepted, duplicates = [], []
                with state.lock:
                    for e in body["events"]:
                        (duplicates if e["event_id"] in state.events else accepted).append(e["event_id"])
                        state.events.setdefault(e["event_id"], e)
                return self._send(200, {"accepted": accepted, "duplicates": duplicates, "rejected": []})
            if path == "/api/v1/heartbeat":
                state.heartbeats.append(body)
                with state.lock:
                    pending = len([c for c in state.commands if c["command_id"] not in state.command_results])
                version = state.config_offer["version"] if state.config_offer else body.get("config_version")
                return self._send(200, {"config_version": version, "pending_commands": pending})
            if path == "/api/v1/config/ack":
                state.acks.append(body)
                return self._send(200, {"ok": True})
            if path.startswith("/api/v1/commands/") and path.endswith("/result"):
                cid = path.split("/")[4]
                state.command_results[cid] = body
                return self._send(200, {"ok": True})
            return self._send(404)

    return Handler


class FakeServer:
    def __init__(self, node_id="NODE-T1", token="test-token-123"):
        self.state = FakeServerState(node_id, token)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
