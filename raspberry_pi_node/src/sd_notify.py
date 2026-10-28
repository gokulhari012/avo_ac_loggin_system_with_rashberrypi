"""Minimal systemd notification (READY / WATCHDOG) without extra dependencies."""

from __future__ import annotations

import os
import socket


def notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
        return True
    except (OSError, AttributeError):  # AttributeError: AF_UNIX missing on Windows
        return False


def watchdog_interval_s() -> float | None:
    usec = os.environ.get("WATCHDOG_USEC")
    return int(usec) / 2_000_000 if usec and usec.isdigit() else None
