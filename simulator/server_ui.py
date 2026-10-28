"""Drive the server's web UI like a browser (login with CSRF, register nodes,
download exports).  Used by the simulator and the end-to-end test."""

from __future__ import annotations

import re

import requests

CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="csrf_token"')
TOKEN_RE = re.compile(r'id="token" value="([^"]+)"')


class ServerUI:
    def __init__(self, base_url: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.timeout = timeout

    def _csrf(self, path: str) -> str:
        page = self.session.get(self.base + path, timeout=self.timeout)
        page.raise_for_status()
        match = CSRF_RE.search(page.text)
        if not match:
            raise RuntimeError(f"no CSRF token on {path}")
        return match.group(1) or match.group(2)

    def login(self, username: str, password: str) -> None:
        token = self._csrf("/login")
        r = self.session.post(self.base + "/login", data={"csrf_token": token, "username": username,
                                                          "password": password}, timeout=self.timeout)
        if "/login" in r.url:
            raise RuntimeError("login failed")

    def register_node(self, uid: str, name: str, port: str, slave_ids=(1,), *, site="Simulation",
                      equipment="Simulated cold room", poll_interval_s=10, reason="simulated node") -> str:
        """Register a node through the web form; returns the one-time token."""
        data = {"csrf_token": self._csrf("/nodes/new"), "uid": uid, "name": name, "description": "SIMULATED",
                "site": site, "location": "", "equipment": equipment, "upload_interval_s": "2", "batch_size": "200",
                "heartbeat_interval_s": "10", "config_sync_interval_s": "30", "request_timeout_s": "10",
                "backoff_max_s": "30", "bus_name": "rs485-1", "port": port, "baudrate": "9600", "parity": "N",
                "timeout_s": "0.5", "retries": "1", "inter_request_delay_ms": "20",
                "retention_days_delivered": "7", "max_pending_records": "2000000", "full_behavior": "drop_oldest",
                "min_free_disk_mb": "50", "log_level": "INFO", "reason": reason}
        for i, slave in enumerate(slave_ids):
            data.update({f"controllers-{i}-uid": f"ctrl-{slave}", f"controllers-{i}-name": f"Simulated {slave}",
                         f"controllers-{i}-slave_id": str(slave), f"controllers-{i}-poll_interval_s": str(poll_interval_s),
                         f"controllers-{i}-parameter_read_interval_s": "600", f"controllers-{i}-address_offset": "0",
                         f"controllers-{i}-read_strategy": "auto", f"controllers-{i}-enabled": "y"})
        r = self.session.post(self.base + "/nodes/new", data=data, timeout=self.timeout)
        match = TOKEN_RE.search(r.text)
        if not match:
            raise RuntimeError(f"registration of {uid} failed (HTTP {r.status_code})")
        return match.group(1)

    def get_json(self, path: str) -> dict:
        r = self.session.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post_download(self, path: str, form_path: str, data: dict) -> requests.Response:
        payload = {"csrf_token": self._csrf(form_path), **data}
        return self.session.post(self.base + path, data=payload, timeout=120)
