from __future__ import annotations

import json
from datetime import timedelta

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..forms import CommandForm, NodeConfigForm, NodeRegisterForm, ReasonForm
from ..models import (CommunicationEvent, Controller, ControllerCommand, ControllerConfigHistory, DiscoveredNode,
                      FaultEvent, Node, NodeConfigVersion, NodeHeartbeat, TemperatureReading)
from ..models.common import loads
from ..services.audit import audit
from ..services.node_config import (DEFAULT_CONTROLLER, ConfigValidationError, config_from_reported,
                                    create_config_version, default_config, latest_version)
from ..services.security import generate_node_token, hash_token, permission_required
from ..services.settings import get_setting
from ..services.status import controller_snapshot, node_status
from ..services.timeutil import utcnow

bp = Blueprint("nodes", __name__)

READ_ONLY_COMMANDS = {"identity_check", "read_parameters"}
COMMAND_TTL = timedelta(minutes=10)


def _token_page(node: Node, token: str, new: bool):
    """The token is shown once; make sure no cache keeps a copy."""
    response = make_response(render_template("nodes/token.html", node=node, token=token, new=new))
    response.headers["Cache-Control"] = "no-store"
    return response


def _node_or_404(node_id: int) -> Node:
    node = db.session.get(Node, node_id)
    if node is None:
        abort(404)
    return node


# ---------------------------------------------------------------- config <-> form
def config_to_form(form: NodeConfigForm, cfg: dict, node: Node | None = None) -> None:
    details = {**(cfg.get("node") or {})}
    if node is not None:
        details = {k: getattr(node, k) for k in ("name", "description", "site", "location", "equipment")}
    for key, value in details.items():
        getattr(form, key).data = value
    server = cfg.get("server", {})
    form.server_url.data = server.get("url", "")
    for key in ("upload_interval_s", "batch_size", "heartbeat_interval_s", "config_sync_interval_s",
                "request_timeout_s", "backoff_max_s"):
        if key in server:
            getattr(form, key).data = server[key]
    bus = (cfg.get("buses") or [{}])[0]
    form.bus_name.data = bus.get("name", "rs485-1")
    for key in ("port", "baudrate", "parity", "timeout_s", "retries", "inter_request_delay_ms"):
        if key in bus:
            getattr(form, key).data = bus[key]
    while len(form.controllers.entries):
        form.controllers.pop_entry()
    for ctrl in cfg.get("controllers") or []:
        form.controllers.append_entry()
        entry = form.controllers.entries[-1].form
        entry.uid.data, entry.name.data = ctrl.get("id"), ctrl.get("name", "")
        entry.slave_id.data, entry.poll_interval_s.data = ctrl.get("slave_id"), ctrl.get("poll_interval_s")
        entry.parameter_read_interval_s.data = ctrl.get("parameter_read_interval_s", 3600)
        overrides = ctrl.get("register_overrides") or {}
        entry.address_offset.data = str(overrides.get("address_offset", 0))
        entry.read_strategy.data = overrides.get("read_strategy", "auto")
        entry.enabled.data = ctrl.get("enabled", True)
    form.controllers.append_entry()  # one blank row to add a controller
    buffer, logcfg = cfg.get("buffer", {}), cfg.get("logging", {})
    for key in ("retention_days_delivered", "max_pending_records", "full_behavior", "min_free_disk_mb"):
        if key in buffer:
            getattr(form, key).data = buffer[key]
    form.log_level.data = logcfg.get("level", "INFO")
    form.diagnostic_registers.data = logcfg.get("diagnostic_registers", False)
    if len(cfg.get("buses") or []) > 1:
        form.advanced_json.data = json.dumps(cfg, indent=2)


def form_to_config(form: NodeConfigForm) -> dict:
    if (form.advanced_json.data or "").strip():
        try:
            cfg = json.loads(form.advanced_json.data)
        except ValueError as exc:
            raise ConfigValidationError([f"advanced JSON is not valid JSON: {exc}"]) from exc
        if isinstance(cfg, dict):
            cfg.setdefault("node", {}).update({k: getattr(form, k).data or "" for k in
                                                ("name", "description", "site", "location", "equipment")})
        return cfg
    server = {"upload_interval_s": float(form.upload_interval_s.data), "batch_size": form.batch_size.data,
              "heartbeat_interval_s": float(form.heartbeat_interval_s.data),
              "config_sync_interval_s": float(form.config_sync_interval_s.data),
              "request_timeout_s": float(form.request_timeout_s.data), "backoff_max_s": float(form.backoff_max_s.data)}
    if (form.server_url.data or "").strip():
        server["url"] = form.server_url.data.strip()
    bus = {"name": form.bus_name.data.strip(), "port": form.port.data.strip(), "baudrate": form.baudrate.data,
           "parity": form.parity.data, "bytesize": 8, "stopbits": 1, "timeout_s": float(form.timeout_s.data),
           "retries": form.retries.data, "inter_request_delay_ms": form.inter_request_delay_ms.data}
    controllers = []
    for entry in form.controllers.entries:
        c = entry.form
        if not (c.uid.data or "").strip():
            continue
        if c.slave_id.data is None:
            raise ConfigValidationError([f"controller {c.uid.data}: slave ID is required"])
        controllers.append({
            **DEFAULT_CONTROLLER, "id": c.uid.data.strip(), "name": (c.name.data or "").strip(), "bus": bus["name"],
            "slave_id": c.slave_id.data,
            "poll_interval_s": float(c.poll_interval_s.data or DEFAULT_CONTROLLER["poll_interval_s"]),
            "parameter_read_interval_s": c.parameter_read_interval_s.data
            if c.parameter_read_interval_s.data is not None else 3600,
            "enabled": bool(c.enabled.data),
            "register_overrides": {"address_offset": int(c.address_offset.data), "read_strategy": c.read_strategy.data},
        })
    return {
        "node": {k: (getattr(form, k).data or "").strip() for k in ("name", "description", "site", "location",
                                                                     "equipment")},
        "server": server, "buses": [bus], "controllers": controllers,
        "buffer": {"retention_days_delivered": float(form.retention_days_delivered.data),
                   "max_pending_records": form.max_pending_records.data, "full_behavior": form.full_behavior.data,
                   "min_free_disk_mb": form.min_free_disk_mb.data},
        "logging": {"level": form.log_level.data, "diagnostic_registers": bool(form.diagnostic_registers.data)},
    }


# ---------------------------------------------------------------- nodes
@bp.get("/nodes")
@permission_required("view_nodes")
def index():
    now = utcnow()
    nodes = Node.query.order_by(Node.site, Node.uid).all()
    rows = [(n, node_status(n, now), latest_version(n)) for n in nodes]
    registered = {n.uid for n in nodes}
    discovered = [d for d in DiscoveredNode.query.order_by(DiscoveredNode.last_seen_at.desc()).limit(100)
                  if d.uid not in registered][:50]
    return render_template("nodes/list.html", rows=rows, discovered=discovered)


@bp.route("/nodes/new", methods=["GET", "POST"])
@permission_required("manage_nodes")
def register():
    form = NodeRegisterForm()
    if request.method == "GET":
        placeholder = Node(name="", description="", site="", location="", equipment="")
        config_to_form(form, default_config(placeholder))
        form.uid.data = request.args.get("uid", "")
        form.reason.data = "Initial registration"
    if form.validate_on_submit():
        uid = form.uid.data.strip()
        if Node.query.filter_by(uid=uid).first():
            form.uid.errors.append("A node with this ID already exists.")
        else:
            try:
                cfg = form_to_config(form)
                token = generate_node_token()
                node = Node(uid=uid, name=form.name.data.strip(), description=form.description.data or "",
                            site=form.site.data or "", location=form.location.data or "",
                            equipment=form.equipment.data or "", token_hash=hash_token(token), token_hint=token[-4:],
                            created_by_id=current_user.id)
                db.session.add(node)
                db.session.flush()
                audit("node", "node_registered", resource_type="node", resource_id=uid,
                      new={"uid": uid, "name": node.name, "site": node.site, "equipment": node.equipment},
                      reason=form.reason.data)
                create_config_version(node, cfg, user=current_user, reason=form.reason.data or "Initial registration")
                DiscoveredNode.query.filter_by(uid=uid).delete()
                db.session.commit()
                return _token_page(node, token, new=True)
            except ConfigValidationError as exc:
                db.session.rollback()
                for error in exc.errors:
                    flash(error, "danger")
    return render_template("nodes/config.html", form=form, node=None, register=True)


@bp.get("/nodes/<int:node_id>")
@permission_required("view_nodes")
def detail(node_id: int):
    node = _node_or_404(node_id)
    now = utcnow()
    controllers = [(c, controller_snapshot(c, node, now)) for c in node.controllers]
    versions = NodeConfigVersion.query.filter_by(node_id=node.id).order_by(NodeConfigVersion.version.desc()).limit(20).all()
    events = (CommunicationEvent.query.filter_by(node_id=node.id).order_by(CommunicationEvent.occurred_at.desc())
              .limit(30).all())
    heartbeats = (NodeHeartbeat.query.filter_by(node_id=node.id).order_by(NodeHeartbeat.received_at.desc())
                  .limit(10).all())
    commands = (ControllerCommand.query.filter_by(node_id=node.id).order_by(ControllerCommand.requested_at.desc())
                .limit(10).all())
    return render_template("nodes/detail.html", node=node, status=node_status(node, now), controllers=controllers,
                           versions=versions, events=events, heartbeats=heartbeats, commands=commands,
                           hb=node.heartbeat, latest=latest_version(node), reason_form=ReasonForm(), now=now)


@bp.route("/nodes/<int:node_id>/config", methods=["GET", "POST"])
@permission_required("manage_nodes")
def edit_config(node_id: int):
    node = _node_or_404(node_id)
    form = NodeConfigForm()
    latest = latest_version(node)
    if request.method == "GET":
        source = latest.config if latest else (config_from_reported(node) or default_config(node))
        config_to_form(form, source, node)
        form.reason.data = ""
    if form.validate_on_submit():
        try:
            cfg = form_to_config(form)
            if latest and cfg == latest.config:
                flash("No changes - configuration not versioned.", "info")
                return redirect(url_for("nodes.detail", node_id=node.id))
            version = create_config_version(node, cfg, user=current_user, reason=form.reason.data)
            db.session.commit()
            flash(f"Configuration version {version.version} created. The node will fetch it at its next check "
                  "and report whether it was applied.", "success")
            return redirect(url_for("nodes.detail", node_id=node.id))
        except ConfigValidationError as exc:
            db.session.rollback()
            for error in exc.errors:
                flash(error, "danger")
    return render_template("nodes/config.html", form=form, node=node, register=False, latest=latest)


@bp.get("/nodes/<int:node_id>/config/<int:version>")
@permission_required("view_nodes")
def config_version(node_id: int, version: int):
    node = _node_or_404(node_id)
    cv = NodeConfigVersion.query.filter_by(node_id=node.id, version=version).first_or_404()
    previous = NodeConfigVersion.query.filter_by(node_id=node.id, version=version - 1).first()
    return render_template("nodes/config_version.html", node=node, cv=cv, previous=previous)


@bp.post("/nodes/<int:node_id>/<action>")
@permission_required("manage_nodes")
def node_action(node_id: int, action: str):
    node = _node_or_404(node_id)
    form = ReasonForm()
    if not form.validate_on_submit():
        abort(400, "invalid form submission")
    reason = form.reason.data or ""
    if action in ("enable", "disable"):
        enabled = action == "enable"
        if node.enabled != enabled:
            audit("node", f"node_{action}d", resource_type="node", resource_id=node.uid,
                  previous={"enabled": node.enabled}, new={"enabled": enabled}, reason=reason)
            node.enabled = enabled
            db.session.commit()
        flash(f"Node {node.uid} {'enabled' if enabled else 'disabled'}."
              + ("" if enabled else " It can no longer submit data; it keeps buffering locally."), "info")
        return redirect(url_for("nodes.detail", node_id=node.id))
    if action == "token":
        token = generate_node_token()
        node.token_hash, node.token_hint, node.token_created_at = hash_token(token), token[-4:], utcnow()
        audit("node", "node_token_rotated", resource_type="node", resource_id=node.uid, reason=reason,
              details={"note": "previous token revoked immediately"})
        db.session.commit()
        return _token_page(node, token, new=False)
    if action == "test":
        queued = 0
        for ctrl in node.controllers:
            if ctrl.enabled:
                _queue_command(node, ctrl, "identity_check", reason or "connectivity test")
                queued += 1
        db.session.commit()
        flash(f"Queued a read-only identity check for {queued} controller(s). Results appear under Commands "
              "once the node reports (normally within one heartbeat interval).", "info")
        return redirect(url_for("nodes.detail", node_id=node.id))
    abort(404)


# ---------------------------------------------------------------- controllers
@bp.get("/controllers/<int:controller_id>")
@permission_required("view_nodes")
def controller_detail(controller_id: int):
    ctrl = db.session.get(Controller, controller_id) or abort(404)
    node = ctrl.node
    latest = (TemperatureReading.query.filter_by(controller_id=ctrl.id)
              .order_by(TemperatureReading.acquired_at.desc()).first())
    history = (ControllerConfigHistory.query.filter_by(controller_id=ctrl.id)
               .order_by(ControllerConfigHistory.recorded_at.desc()).limit(20).all())
    faults = (FaultEvent.query.filter_by(controller_id=ctrl.id).order_by(FaultEvent.started_at.desc()).limit(20).all())
    commands = (ControllerCommand.query.filter_by(controller_id=ctrl.id)
                .order_by(ControllerCommand.requested_at.desc()).limit(20).all())
    form = CommandForm()
    return render_template("controllers/detail.html", ctrl=ctrl, node=node, latest=latest,
                           snapshot=controller_snapshot(ctrl, node), history=history, faults=faults,
                           commands=commands, form=form, remote_control=get_setting("remote_control_enabled"))


def _queue_command(node: Node, ctrl: Controller, command: str, reason: str) -> ControllerCommand:
    cmd = ControllerCommand(node_id=node.id, controller_id=ctrl.id, command=command, reason=reason,
                            requested_by_id=current_user.id, expires_at=utcnow() + COMMAND_TTL)
    db.session.add(cmd)
    db.session.flush()
    audit("command", "command_requested", resource_type="controller", resource_id=f"{node.uid}/{ctrl.uid}",
          reason=reason, details={"command": command, "command_id": cmd.command_uid,
                                  "expires_at": cmd.expires_at.isoformat()})
    return cmd


@bp.post("/controllers/<int:controller_id>/command")
@permission_required("view_nodes")
def controller_command(controller_id: int):
    ctrl = db.session.get(Controller, controller_id) or abort(404)
    form = CommandForm()
    form._expected_confirm = ctrl.uid
    if not form.validate_on_submit():
        for errors in form.errors.values():
            for error in errors:
                flash(error, "danger")
        return redirect(url_for("nodes.controller_detail", controller_id=ctrl.id))
    command = form.command.data
    if command in READ_ONLY_COMMANDS:
        allowed = current_user.can("manage_nodes") or current_user.can("control_controllers")
    else:
        allowed = current_user.can("control_controllers") and get_setting("remote_control_enabled")
    if not allowed:
        audit("command", "command_refused", resource_type="controller", resource_id=f"{ctrl.node.uid}/{ctrl.uid}",
              outcome="denied", reason=form.reason.data,
              details={"command": command, "remote_control_enabled": get_setting("remote_control_enabled")})
        db.session.commit()
        abort(403, "Not permitted: controller commands need the 'control_controllers' permission and the "
                   "'Allow controller commands' system setting.")
    if not ctrl.enabled or not ctrl.node.enabled:
        flash("Controller or node is disabled.", "warning")
        return redirect(url_for("nodes.controller_detail", controller_id=ctrl.id))
    _queue_command(ctrl.node, ctrl, command, form.reason.data)
    db.session.commit()
    flash(f"Command '{command}' queued. It expires in {int(COMMAND_TTL.total_seconds() // 60)} minutes if the node "
          "does not collect it. The node rejects write commands unless remote control is enabled in its local "
          "configuration file.", "info")
    return redirect(url_for("nodes.controller_detail", controller_id=ctrl.id))


@bp.get("/events")
@permission_required("view_nodes")
def events():
    q = CommunicationEvent.query
    node_id = request.args.get("node", type=int)
    event_type = request.args.get("type", "")
    severity = request.args.get("severity", "")
    if node_id:
        q = q.filter_by(node_id=node_id)
    if event_type:
        q = q.filter_by(event_type=event_type[:48])
    if severity:
        q = q.filter_by(severity=severity[:16])
    page = max(request.args.get("page", 1, type=int), 1)
    total = q.count()
    rows = q.order_by(CommunicationEvent.occurred_at.desc()).offset((page - 1) * 100).limit(100).all()
    types = [t for (t,) in db.session.query(CommunicationEvent.event_type).distinct().order_by(
        CommunicationEvent.event_type)]
    return render_template("nodes/events.html", rows=rows, total=total, page=page,
                           pages=max((total + 99) // 100, 1), nodes=Node.query.order_by(Node.uid).all(), types=types,
                           args={"node": node_id or "", "type": event_type, "severity": severity}, loads=loads)
