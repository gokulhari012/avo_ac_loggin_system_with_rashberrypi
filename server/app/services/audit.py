"""Append-only audit trail helper."""

from __future__ import annotations

import logging
from typing import Any

from flask import g, has_request_context, request
from flask_login import current_user

from ..extensions import db
from ..models import AuditEvent
from ..models.common import dumps, utcnow
from .security import redact

log = logging.getLogger("audit")


def request_ip() -> str | None:
    return request.remote_addr if has_request_context() else None


def request_id() -> str | None:
    return getattr(g, "request_id", None) if has_request_context() else None


def audit(category: str, action: str, *, actor_type: str | None = None, actor_id: Any = None,
          actor_name: str | None = None, resource_type: str | None = None, resource_id: Any = None,
          previous: Any = None, new: Any = None, outcome: str = "success", reason: str | None = None,
          details: dict | None = None, commit: bool = False) -> AuditEvent:
    """Record an audit event in the current session.  Secrets are redacted.

    Defaults the actor to the logged-in user, else ``system``.
    """
    if actor_type is None:
        if has_request_context() and getattr(current_user, "is_authenticated", False):
            actor_type, actor_id, actor_name = "user", current_user.id, current_user.username
        elif has_request_context() and getattr(g, "node", None) is not None:
            actor_type, actor_id, actor_name = "node", g.node.uid, g.node.uid
        else:
            actor_type, actor_name = "system", actor_name or "system"
    event = AuditEvent(
        occurred_at=utcnow(), actor_type=actor_type, actor_id=None if actor_id is None else str(actor_id),
        actor_name=actor_name or "", category=category, action=action, resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        previous_value=dumps(redact(previous)), new_value=dumps(redact(new)), outcome=outcome,
        reason=(reason or None) and reason[:500], source_ip=request_ip(), request_id=request_id(),
        details_json=dumps(redact(details)) if details else None,
    )
    db.session.add(event)
    log.info("audit %s.%s actor=%s:%s resource=%s:%s outcome=%s", category, action, actor_type, actor_name,
             resource_type, resource_id, outcome)
    if commit:
        db.session.commit()
    return event


def diff(previous: dict | None, new: dict | None) -> tuple[dict, dict]:
    """Return only the keys whose values changed (shallow)."""
    previous, new = previous or {}, new or {}
    keys = sorted(set(previous) | set(new))
    changed = [k for k in keys if previous.get(k) != new.get(k)]
    return {k: previous.get(k) for k in changed}, {k: new.get(k) for k in changed}
