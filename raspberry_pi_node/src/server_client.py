"""HTTP(S) client for the central server's node API (see docs/api.md).

The node always initiates connections; the server never connects to the Pi.
Every request carries ``X-Node-ID`` and ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)
API_PREFIX = "/api/v1"


class ServerUnavailable(Exception):
    """Network failure, timeout or 5xx: retry later with backoff."""


class ApiError(Exception):
    """The server answered with a 4xx error."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(f"HTTP {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ServerClient:
    def __init__(self, base_url: str, node_id: str, token: str | None, *, timeout_s: float = 10,
                 verify_tls: bool = True, ca_bundle: str | None = None, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.token = token
        self.timeout_s = timeout_s
        self.verify: bool | str = ca_bundle if ca_bundle else verify_tls
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": f"temperature-node/{__version__}", "X-Node-ID": node_id})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> Any:
        url = f"{self.base_url}{API_PREFIX}{path}"
        try:
            resp = self.session.request(method, url, json=json_body, params=params, timeout=self.timeout_s,
                                        verify=self.verify, headers={"X-Request-ID": str(uuid.uuid4())})
        except requests.RequestException as exc:
            raise ServerUnavailable(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 500:
            raise ServerUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        if resp.status_code == 204:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400:
            err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
            raise ApiError(resp.status_code, err.get("code", "http_error"),
                           err.get("message", resp.reason or ""), err.get("details"))
        return body

    # ------------------------------------------------------------ endpoints
    def submit_readings(self, readings: list[dict]) -> dict:
        return self._request("POST", "/readings", json_body={"node_id": self.node_id, "readings": readings})

    def submit_events(self, events: list[dict]) -> dict:
        return self._request("POST", "/events", json_body={"node_id": self.node_id, "events": events})

    def heartbeat(self, payload: dict) -> dict:
        return self._request("POST", "/heartbeat", json_body=payload) or {}

    def fetch_config(self, current_version: int) -> dict | None:
        return self._request("GET", "/config", params={"current_version": current_version})

    def ack_config(self, version: int, status: str, *, errors: list[str] | None = None,
                   ignored_keys: list[str] | None = None) -> dict:
        return self._request("POST", "/config/ack", json_body={
            "version": version, "status": status, "errors": errors or [], "ignored_keys": ignored_keys or [],
        })

    def fetch_commands(self) -> list[dict]:
        body = self._request("GET", "/commands") or {}
        return body.get("commands", [])

    def report_command_result(self, command_id: str, status: str, result: dict) -> dict:
        return self._request("POST", f"/commands/{command_id}/result", json_body={"status": status, "result": result})
