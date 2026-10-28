from __future__ import annotations

from flask import Blueprint, abort, render_template, request
from flask_login import current_user

from ..models import AuditEvent, ExportRecord
from ..services.security import permission_required
from ..services.timeutil import parse_local_input

bp = Blueprint("audit", __name__)


@bp.get("/audit")
@permission_required("view_audit")
def index():
    q = AuditEvent.query
    args = {k: (request.args.get(k) or "").strip()[:64] for k in ("start", "end", "category", "actor", "outcome",
                                                                  "action", "resource")}
    try:
        start, end = parse_local_input(args["start"]), parse_local_input(args["end"])
    except ValueError:
        start = end = None
    if start:
        q = q.filter(AuditEvent.occurred_at >= start)
    if end:
        q = q.filter(AuditEvent.occurred_at <= end)
    if args["category"]:
        q = q.filter(AuditEvent.category == args["category"])
    if args["actor"]:
        q = q.filter(AuditEvent.actor_name == args["actor"])
    if args["outcome"]:
        q = q.filter(AuditEvent.outcome == args["outcome"])
    if args["action"]:
        q = q.filter(AuditEvent.action.contains(args["action"]))
    if args["resource"]:
        q = q.filter(AuditEvent.resource_id.contains(args["resource"]))
    page = max(request.args.get("page", 1, type=int), 1)
    total = q.count()
    rows = q.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).offset((page - 1) * 100).limit(100).all()
    categories = [c for (c,) in AuditEvent.query.with_entities(AuditEvent.category).distinct()]
    return render_template("audit/list.html", rows=rows, total=total, page=page, pages=max((total + 99) // 100, 1),
                           args=args, categories=sorted(categories))


@bp.get("/audit/<event_uid>")
@permission_required("view_audit")
def detail(event_uid: str):
    event = AuditEvent.query.filter_by(event_uid=event_uid[:36]).first_or_404()
    return render_template("audit/detail.html", e=event)


@bp.get("/exports")
@permission_required("export_data")
def exports():
    q = ExportRecord.query
    if not current_user.can("view_audit"):
        q = q.filter(ExportRecord.user_id == current_user.id)  # users see their own exports
    page = max(request.args.get("page", 1, type=int), 1)
    total = q.count()
    rows = q.order_by(ExportRecord.created_at.desc()).offset((page - 1) * 100).limit(100).all()
    return render_template("audit/exports.html", rows=rows, total=total, page=page,
                           pages=max((total + 99) // 100, 1), all_users=current_user.can("view_audit"))


@bp.get("/exports/<export_uid>")
@permission_required("export_data")
def export_detail(export_uid: str):
    record = ExportRecord.query.filter_by(export_uid=export_uid[:36]).first_or_404()
    if record.user_id != current_user.id and not current_user.can("view_audit"):
        abort(403)
    return render_template("audit/export_detail.html", r=record)
