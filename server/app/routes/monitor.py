from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, jsonify, render_template, request

from ..extensions import db
from ..models import Controller, Node, TemperatureReading
from ..services.security import permission_required
from ..services.series import controller_series
from ..services.settings import get_setting
from ..services.status import controller_snapshot
from ..services.timeutil import fmt_local, iso_utc, parse_local_input, utcnow

bp = Blueprint("monitor", __name__)

RANGES = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
          "7d": timedelta(days=7), "30d": timedelta(days=30)}


@bp.get("/monitor")
@permission_required("view_dashboard", "view_data")
def index():
    nodes = Node.query.order_by(Node.uid).all()
    choices = [{"id": c.id, "label": f"{n.name or n.uid} / {c.label}", "node_id": n.id, "site": n.site}
               for n in nodes for c in n.controllers]
    selected = request.args.get("controller", type=int) or (choices[0]["id"] if choices else None)
    return render_template("monitor.html", choices=choices, selected=selected, ranges=list(RANGES),
                           refresh_s=get_setting("dashboard_refresh_s"))


@bp.get("/ui-api/series")
@permission_required("view_dashboard", "view_data")
def series():
    ctrl = db.session.get(Controller, request.args.get("controller", type=int) or 0)
    if ctrl is None:
        abort(404, "controller not found")
    now = utcnow()
    preset = request.args.get("range", "24h")
    try:
        if preset == "custom":
            start = parse_local_input(request.args.get("start")) or now - RANGES["24h"]
            end = parse_local_input(request.args.get("end")) or now
        else:
            start, end = now - RANGES.get(preset, RANGES["24h"]), now
    except ValueError:
        abort(400, "invalid date")
    if start >= end:
        abort(400, "start must be before end")
    if end - start > timedelta(days=366):
        abort(400, "range limited to one year; use Data Logs or reports for longer periods")
    data = controller_series(ctrl, start, end)
    recent = (TemperatureReading.query.filter(TemperatureReading.controller_id == ctrl.id,
                                              TemperatureReading.acquired_at >= start,
                                              TemperatureReading.acquired_at <= end)
              .order_by(TemperatureReading.acquired_at.desc()).limit(50).all())
    data["recent"] = [{"time": fmt_local(r.acquired_at), "temperature_c": r.temperature_c, "setpoint_c": r.setpoint_c,
                       "compressor_on": r.compressor_on, "alarm_on": r.alarm_on, "faults": r.faults,
                       "comm_status": r.comm_status, "quality": r.quality, "source": r.source,
                       "backfilled": r.backfilled} for r in recent]
    data["controller"] = controller_snapshot(ctrl, ctrl.node, now)
    data["range"] = {"start": iso_utc(start), "end": iso_utc(end), "start_local": fmt_local(start),
                     "end_local": fmt_local(end)}
    data["timezone"] = get_setting("display_timezone")
    return jsonify(data)
