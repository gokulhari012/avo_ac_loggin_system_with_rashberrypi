from __future__ import annotations

import os
from datetime import timedelta

from flask import Blueprint, abort, after_this_request, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user
from flask_wtf import FlaskForm

from ..models import AuditEvent, Controller, Node
from ..services.exports import ExportRejected
from ..services.reports import REPORT_TYPES, ReportParams, render_report
from ..services.security import permission_required
from ..services.timeutil import local_input_value, parse_local_input, utcnow

bp = Blueprint("reports", __name__)


@bp.get("/reports")
@permission_required("export_data")
def index():
    now = utcnow()
    categories = [c for (c,) in AuditEvent.query.with_entities(AuditEvent.category).distinct()]
    available = {k: v for k, v in REPORT_TYPES.items() if current_user.can(v["permission"])}
    return render_template("reports/index.html", types=available, form=FlaskForm(),
                           nodes=Node.query.order_by(Node.uid).all(),
                           controllers=Controller.query.join(Node).order_by(Node.uid, Controller.uid).all(),
                           default_start=local_input_value(now - timedelta(days=7)),
                           default_end=local_input_value(now), categories=sorted(categories))


@bp.post("/reports/generate")
@permission_required("export_data")
def generate():
    form = FlaskForm()
    if not form.validate_on_submit():
        flash("The form expired; please try again.", "warning")
        return redirect(url_for("reports.index"))
    report_type = request.form.get("report_type", "")
    fmt = request.form.get("format", "pdf")
    if report_type == "temperature_history":
        args = {k: v for k, v in request.form.items() if k in ("start", "end", "node", "controller") and v}
        return redirect(url_for("logs.index", **args))
    if report_type not in REPORT_TYPES or fmt not in ("xlsx", "pdf"):
        abort(400, "unknown report type or format")
    if not current_user.can(REPORT_TYPES[report_type]["permission"]):
        abort(403)
    try:
        start = parse_local_input(request.form.get("start"))
        end = parse_local_input(request.form.get("end")) or utcnow()
    except ValueError:
        flash("Dates must be in the form YYYY-MM-DDTHH:MM.", "danger")
        return redirect(url_for("reports.index"))
    if start is None or start >= end:
        flash("Choose a start date before the end date.", "danger")
        return redirect(url_for("reports.index"))

    def number(name):
        value = (request.form.get(name) or "").strip()
        try:
            return float(value) if value else None
        except ValueError:
            return None

    audit_types = ("audit_trail", "config_changes", "user_activity", "export_activity")
    params = ReportParams(start=start, end=end, node_id=request.form.get("node", type=int) or None,
                          controller_id=request.form.get("controller", type=int) or None)
    if report_type == "excursions":
        params.high_c, params.low_c = number("high_c"), number("low_c")
    if report_type in audit_types:
        params.category = request.form.get("category") or None if report_type != "export_activity" else None
        params.actor = (request.form.get("actor") or "").strip()[:64] or None
        params.outcome = request.form.get("outcome") or None if report_type in ("audit_trail",
                                                                                "user_activity") else None
    try:
        path, file_name, _record = render_report(report_type, params, fmt)
    except ExportRejected as exc:
        flash(exc.message, "warning")
        return redirect(url_for("reports.index"))

    @after_this_request
    def _cleanup(response):
        response.call_on_close(lambda: os.path.exists(path) and os.remove(path))
        return response

    mimetype = "application/pdf" if fmt == "pdf" else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=file_name, max_age=0)
