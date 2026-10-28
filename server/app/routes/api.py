"""Node API (JSON).  Documented in docs/api.md.

Authentication: ``X-Node-ID: <node uid>`` + ``Authorization: Bearer <token>``.
Unknown node IDs are recorded as *discovered* (never trusted).  Every error
is returned as ``{"error": {"code", "message", "details", "request_id"}}``.
"""

from __future__ import annotations

import re
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .. import __version__
from ..extensions import csrf, db
from ..models import CommunicationEvent, ControllerCommand, DiscoveredNode, Node
from ..models.common import dumps, utcnow
from ..services.audit import audit
from ..services.ingestion import ValidationError, ingest_events, ingest_readings, process_heartbeat
from ..services.node_config import apply_ack, offer_for_node
from ..services.security import token_matches

bp = Blueprint("api", __name__, url_prefix="/api/v1")
csrf.exempt(bp)

NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COMMAND_RESULT_STATUSES = {"succeeded", "failed", "rejected", "expired"}
MAX_DISCOVERED = 500


def api_error(status: int, code: str, message: str, details=None):
    return jsonify({"error": {"code": code, "message": message, "details": details,
                              "request_id": g.get("request_id")}}), status


@bp.before_request
def authenticate_node():
    if request.endpoint == "api.ping":
        return None
    node_uid = request.headers.get("X-Node-ID", "")
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not NODE_ID_RE.match(node_uid):
        return api_error(401, "unauthorized", "missing or malformed X-Node-ID header")
    node = Node.query.filter_by(uid=node_uid).first()
    if node is None:
        _record_discovery(node_uid)
        return api_error(401, "unknown_node", "this node is not registered on the server")
    if not token or not token_matches(token, node.token_hash):
        _record_auth_failure(node)
        return api_error(401, "invalid_credentials", "node credentials were not accepted")
    if not node.enabled:
        return api_error(403, "node_disabled", "this node has been disabled by an administrator")
    g.node = node
    return None


def _record_discovery(node_uid: str) -> None:
    now = utcnow()
    row = DiscoveredNode.query.filter_by(uid=node_uid).first()
    if row is None:
        if DiscoveredNode.query.count() >= MAX_DISCOVERED:
            return
        row = DiscoveredNode(uid=node_uid, first_seen_at=now, attempts=0)
        db.session.add(row)
    row.last_seen_at, row.last_ip = now, request.remote_addr
    row.attempts = (row.attempts or 0) + 1
    if request.path.endswith("/heartbeat"):
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            system = body.get("system") if isinstance(body.get("system"), dict) else {}
            row.hostname = str(system.get("hostname") or "")[:128] or row.hostname
            row.software_version = str(body.get("software_version") or "")[:32] or row.software_version
    db.session.commit()


def _record_auth_failure(node: Node) -> None:
    now = utcnow()
    throttled = node.last_auth_failure_at and now - node.last_auth_failure_at < timedelta(minutes=10)
    node.auth_failures = (node.auth_failures or 0) + 1
    node.last_auth_failure_at = now
    if not throttled:  # log the first failure of a burst, not every retry
        db.session.add(CommunicationEvent(origin="server", node_id=node.id, occurred_at=now,
                                          event_type="api_auth_failure", severity="error",
                                          message=f"Rejected API credentials for node {node.uid}",
                                          details_json=dumps({"ip": request.remote_addr, "path": request.path})))
        audit("auth", "node_auth_failed", actor_type="node", actor_id=node.uid, actor_name=node.uid,
              resource_type="node", resource_id=node.uid, outcome="denied",
              details={"path": request.path, "total_failures": node.auth_failures})
    db.session.commit()


def _json_object():
    if not request.is_json:
        return None, api_error(415, "unsupported_media_type", "Content-Type must be application/json")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, api_error(400, "invalid_json", "request body must be a JSON object")
    return body, None


def _batch(body: dict, key: str):
    if body.get("node_id") not in (None, g.node.uid):
        return None, api_error(403, "node_mismatch", "node_id in body does not match the authenticated node")
    items = body.get(key)
    if not isinstance(items, list) or not items:
        return None, api_error(400, "invalid_request", f"'{key}' must be a non-empty list")
    limit = current_app.config["API_MAX_BATCH"]
    if len(items) > limit:
        return None, api_error(413, "batch_too_large", f"at most {limit} items per request", {"max": limit})
    return items, None


@bp.get("/ping")
def ping():
    return jsonify({"status": "ok", "server_time": utcnow().isoformat() + "Z", "version": __version__})


@bp.post("/readings")
def submit_readings():
    body, error = _json_object()
    if error:
        return error
    items, error = _batch(body, "readings")
    if error:
        return error
    return jsonify(ingest_readings(g.node, items))


@bp.post("/events")
def submit_events():
    body, error = _json_object()
    if error:
        return error
    items, error = _batch(body, "events")
    if error:
        return error
    return jsonify(ingest_events(g.node, items))


@bp.post("/heartbeat")
def heartbeat():
    body, error = _json_object()
    if error:
        return error
    if body.get("node_id") not in (None, g.node.uid):
        return api_error(403, "node_mismatch", "node_id in body does not match the authenticated node")
    try:
        return jsonify(process_heartbeat(g.node, body, request.remote_addr))
    except ValidationError as exc:
        return api_error(400, "invalid_request", str(exc))


@bp.get("/config")
def get_config():
    raw = request.args.get("current_version", "")
    try:
        current = int(raw) if raw != "" else None
    except ValueError:
        return api_error(400, "invalid_request", "current_version must be an integer")
    offer = offer_for_node(g.node, current)
    g.node.last_seen_at = utcnow()
    db.session.commit()
    if offer is None:
        return "", 204
    return jsonify({"version": offer.version, "config": offer.config,
                    "created_at": offer.created_at.isoformat() + "Z"})


@bp.post("/config/ack")
def ack_config():
    body, error = _json_object()
    if error:
        return error
    version, status = body.get("version"), body.get("status")
    errors, ignored = body.get("errors") or [], body.get("ignored_keys") or []
    if not isinstance(version, int) or status not in ("applied", "rejected") \
            or not isinstance(errors, list) or not isinstance(ignored, list):
        return api_error(400, "invalid_request", "expected {version:int, status:applied|rejected, errors:[], "
                                                 "ignored_keys:[]}")
    try:
        apply_ack(g.node, version, status, [str(e)[:500] for e in errors[:50]], [str(k)[:100] for k in ignored[:50]])
    except LookupError as exc:
        return api_error(404, "unknown_version", str(exc))
    db.session.commit()
    return jsonify({"ok": True})


@bp.get("/commands")
def get_commands():
    now = utcnow()
    commands = (ControllerCommand.query
                .filter(ControllerCommand.node_id == g.node.id, ControllerCommand.status == "pending",
                        ControllerCommand.expires_at > now)
                .order_by(ControllerCommand.requested_at).limit(20).all())
    out = []
    for cmd in commands:
        cmd.status, cmd.sent_at = "sent", now
        out.append({"command_id": cmd.command_uid, "controller_id": cmd.controller.uid, "command": cmd.command,
                    "expires_at": cmd.expires_at.isoformat() + "Z",
                    "requested_by": cmd.requested_by.username if cmd.requested_by else None})
    g.node.last_seen_at = now
    db.session.commit()
    return jsonify({"commands": out})


@bp.post("/commands/<command_uid>/result")
def command_result(command_uid: str):
    if not UUID_RE.match(command_uid):
        return api_error(400, "invalid_request", "command id must be a UUID")
    body, error = _json_object()
    if error:
        return error
    cmd = ControllerCommand.query.filter_by(command_uid=command_uid, node_id=g.node.id).first()
    if cmd is None:
        return api_error(404, "unknown_command", "no such command for this node")
    status, result = body.get("status"), body.get("result") or {}
    if status not in COMMAND_RESULT_STATUSES or not isinstance(result, dict):
        return api_error(400, "invalid_request", f"status must be one of {sorted(COMMAND_RESULT_STATUSES)}")
    if cmd.status in COMMAND_RESULT_STATUSES:
        return jsonify({"ok": True, "duplicate": True})
    cmd.status, cmd.completed_at, cmd.result_json = status, utcnow(), dumps(result)[:20_000]
    audit("command", f"command_{status}", actor_type="node", actor_id=g.node.uid, actor_name=g.node.uid,
          resource_type="controller", resource_id=f"{g.node.uid}/{cmd.controller.uid}",
          outcome="success" if status == "succeeded" else "failure",
          details={"command": cmd.command, "command_id": cmd.command_uid, "result": result})
    db.session.commit()
    return jsonify({"ok": True})
