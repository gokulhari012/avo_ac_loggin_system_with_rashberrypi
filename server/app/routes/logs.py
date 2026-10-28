from __future__ import annotations

import os

from flask import Blueprint, after_this_request, flash, redirect, render_template, request, send_file, url_for
from flask_wtf import FlaskForm

from ..extensions import db
from ..models import Controller, Node
from ..services.exports import ExportRejected, export_readings
from ..services.queries import COMM_CHOICES, FAULT_CODES, QUALITY_CHOICES, SORTS, ReadingFilter
from ..services.security import permission_required
from ..services.timeutil import iso_utc, utcnow

bp = Blueprint("logs", __name__)
PAGE_SIZES = (50, 100, 250, 500)


def filter_options() -> dict:
    nodes = Node.query.order_by(Node.uid).all()
    return {
        "nodes": nodes,
        "controllers": Controller.query.join(Node).order_by(Node.uid, Controller.uid).all(),
        "sites": sorted({n.site for n in nodes if n.site}),
        "equipment": sorted({n.equipment for n in nodes if n.equipment}),
        "faults": FAULT_CODES, "comm": COMM_CHOICES, "quality": QUALITY_CHOICES, "sorts": SORTS,
        "page_sizes": PAGE_SIZES,
    }


@bp.get("/logs")
@permission_required("view_data")
def index():
    f = ReadingFilter.from_args(request.args)
    if f.as_of is None:
        f.as_of = utcnow()   # freeze this view so paging and exports see the same record set
    per_page = request.args.get("per_page", 100, type=int)
    per_page = per_page if per_page in PAGE_SIZES else 100
    page = max(request.args.get("page", 1, type=int), 1)
    rows, total = [], 0
    if not f.errors:
        total = f.count()
        rows = f.query().offset((page - 1) * per_page).limit(per_page).all()
    pages = max((total + per_page - 1) // per_page, 1)
    return render_template("logs.html", f=f, rows=rows, total=total, page=min(page, pages), pages=pages,
                           per_page=per_page, options=filter_options(), export_form=FlaskForm(),
                           args=f.to_args(), refresh_args=f.to_args(as_of=""))


@bp.post("/logs/export/<fmt>")
@permission_required("view_data", "export_data")
def export(fmt: str):
    form = FlaskForm()
    if not form.validate_on_submit():
        flash("The export form expired; please try again.", "warning")
        return redirect(url_for("logs.index"))
    f = ReadingFilter.from_args(request.form)
    back = url_for("logs.index", **f.to_args())
    if f.errors:
        flash("; ".join(f.errors), "danger")
        return redirect(back)
    try:
        path, file_name, _record = export_readings(f, fmt, include_raw=request.form.get("include_raw") == "1")
    except ExportRejected as exc:
        flash(exc.message, "warning")
        return redirect(back)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(back)

    @after_this_request
    def _cleanup(response):
        response.call_on_close(lambda: os.path.exists(path) and os.remove(path))
        return response

    mimetype = "application/pdf" if fmt == "pdf" else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=file_name, max_age=0)
