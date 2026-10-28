from __future__ import annotations

from flask import Blueprint, jsonify, render_template

from ..models import FaultEvent, Node
from ..services.security import permission_required
from ..services.settings import get_setting
from ..services.status import controller_snapshot, node_status
from ..services.timeutil import iso_utc, utcnow

bp = Blueprint("dashboard", __name__)


def dashboard_data() -> dict:
    now = utcnow()
    nodes = Node.query.order_by(Node.site, Node.uid).all()
    node_rows, controllers = [], []
    for node in nodes:
        status = node_status(node, now)
        node_rows.append({
            "id": node.id, "uid": node.uid, "name": node.name or node.uid, "site": node.site,
            "status": status.to_dict(), "last_seen_at": iso_utc(node.last_seen_at),
            "last_upload_at": iso_utc(node.last_upload_at), "software_version": node.software_version,
            "queue_pending": node.queue_pending, "simulation": node.simulation,
            "config_version": node.reported_config_version, "clock_synchronized": node.clock_synchronized,
        })
        for ctrl in node.controllers:
            if ctrl.enabled:
                controllers.append(controller_snapshot(ctrl, node, now))
    codes = [c["status"]["code"] for c in controllers]
    summary = {
        "nodes_total": len(nodes),
        "nodes_online": sum(1 for n in node_rows if n["status"]["code"] == "online"),
        "nodes_offline": sum(1 for n in node_rows if n["status"]["code"] in ("offline", "never_seen")),
        "nodes_disabled": sum(1 for n in node_rows if n["status"]["code"] == "disabled"),
        "controllers_total": len(controllers),
        "controllers_ok": codes.count("ok"),
        "controllers_with_faults": sum(1 for c in codes if c in ("fault", "probe_fault")),
        "controllers_comm_failure": codes.count("comm_failure"),
        "controllers_stale": sum(1 for c in codes if c in ("stale", "node_offline")),
        "active_fault_events": FaultEvent.query.filter_by(status="active", kind="fault").count(),
    }
    return {"generated_at": iso_utc(now), "summary": summary, "nodes": node_rows, "controllers": controllers,
            "refresh_s": get_setting("dashboard_refresh_s")}


@bp.get("/")
@permission_required("view_dashboard")
def index():
    return render_template("dashboard.html", data=dashboard_data())


@bp.get("/ui-api/dashboard")
@permission_required("view_dashboard")
def dashboard_json():
    return jsonify(dashboard_data())
