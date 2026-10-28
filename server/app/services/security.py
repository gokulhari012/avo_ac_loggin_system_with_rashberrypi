"""Permissions, role defaults, and node credentials."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import wraps

from flask import abort
from flask_login import current_user, login_required

PERMISSIONS: dict[str, str] = {
    "view_dashboard": "View dashboard and live monitoring",
    "view_data": "View historical readings and data logs",
    "export_data": "Export data and generate temperature reports",
    "view_nodes": "View nodes, controllers and their health",
    "manage_nodes": "Register, configure, enable/disable nodes and rotate credentials",
    "control_controllers": "Send commands to controllers (alarm mute, fault reset, defrost)",
    "view_audit": "View the audit trail and generate audit reports",
    "manage_users": "Manage users, roles and permissions",
    "manage_settings": "Change system settings",
}

ROLE_DEFAULTS: dict[str, tuple[str, set[str]]] = {
    "Administrator": ("Manages users, nodes, configuration, permissions and settings", set(PERMISSIONS)),
    "Operator": ("Monitors temperatures, inspects faults and generates reports",
                 {"view_dashboard", "view_data", "export_data", "view_nodes"}),
    "Auditor": ("Reads historical data and audit logs and generates authorised reports",
                {"view_dashboard", "view_data", "export_data", "view_nodes", "view_audit"}),
}


def permission_required(*codes: str):
    """Server-side enforcement; hiding buttons in templates is cosmetic only."""

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not all(current_user.can(code) for code in codes):
                from .audit import audit
                audit("auth", "permission_denied", outcome="denied", details={"required": list(codes)},
                      commit=True)
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def generate_node_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)


SECRET_FIELD_NAMES = {"token", "token_hash", "password", "password_hash", "secret", "secret_key", "PA"}


def redact(value):
    """Remove secrets from structures before they are stored in audit/logs."""
    if isinstance(value, dict):
        return {k: ("[redacted]" if k in SECRET_FIELD_NAMES and v not in (None, "") else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
