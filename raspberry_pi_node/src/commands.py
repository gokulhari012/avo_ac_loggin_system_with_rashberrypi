"""Controller commands requested from the server.

Read-only diagnostics (``identity_check``, ``read_parameters``) are always
allowed.  Coil commands (alarm mute, fault reset, temperature-log reset,
defrost start/stop) run only when ``safety.allow_remote_control: true`` is
set in the node's LOCAL config file - the server cannot enable this remotely.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeout  # distinct class on Python 3.10
from datetime import datetime, timezone

from .collector import READ_ONLY_COMMANDS, WRITE_COMMANDS
from .server_client import ApiError, ServerUnavailable

log = logging.getLogger(__name__)


class CommandWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(name="commands", daemon=True)
        self.app = app

    def run(self) -> None:
        while not self.app.stop_event.is_set():
            self.app.command_trigger.wait(60)
            self.app.command_trigger.clear()
            try:
                self.process_once()
            except (ServerUnavailable, ApiError) as exc:
                log.warning("command fetch failed: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("unexpected command worker error")

    def process_once(self) -> int:
        client = self.app.server_client()
        handled = 0
        for command in client.fetch_commands():
            status, result = self.evaluate(command)
            client.report_command_result(command["command_id"], status, result)
            handled += 1
        return handled

    def evaluate(self, command: dict) -> tuple[str, dict]:
        name = command.get("command")
        expires = command.get("expires_at")
        if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
            return "expired", {"error": "command expired before the node received it"}
        if name not in READ_ONLY_COMMANDS | WRITE_COMMANDS:
            return "rejected", {"error": f"unsupported command {name!r}"}
        if name in WRITE_COMMANDS and not self.app.cfg["safety"]["allow_remote_control"]:
            return "rejected", {"error": "remote control is disabled in this node's local configuration "
                                         "(safety.allow_remote_control: false)"}
        collector = self.app.collector_for(command.get("controller_id"))
        if collector is None:
            return "rejected", {"error": "controller not configured or disabled on this node"}
        log.warning("executing command %s (%s) for controller %s requested by %s", command["command_id"], name,
                    command.get("controller_id"), command.get("requested_by"))
        try:
            return collector.submit_command(command).result(timeout=60)
        except FutureTimeout:
            return "failed", {"error": "timed out waiting for the bus"}
