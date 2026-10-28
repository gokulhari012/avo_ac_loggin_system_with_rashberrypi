"""Temperature-history exports (Excel and PDF) with an audit record per export.

* Both formats are produced from ``ReadingFilter.query()``; an ``as_of``
  cutoff on ``received_at`` freezes the record set, so data that arrives while
  a report is generated cannot make two exports of the same view differ.
* Exports are chronological (oldest first).
* Size limits are documented (EXPORT_MAX_ROWS_XLSX / EXPORT_MAX_ROWS_PDF).
  A larger request is REFUSED with a clear message - never silently truncated.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from flask import current_app
from flask_login import current_user
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.platypus import PageBreak, Paragraph, Spacer

from ..extensions import db
from ..models import Controller, ExportRecord, FaultEvent, TemperatureReading
from ..models.common import dumps
from . import pdf_layout as P
from .audit import audit, request_ip
from .queries import ReadingFilter
from .settings import get_setting
from .timeutil import display_zone, duration_text, fmt_local, iso_utc, utcnow


class ExportRejected(Exception):
    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome
        self.message = message


# ------------------------------------------------------------------ export audit records
def start_export(report_type: str, fmt: str, filters: dict, date_from=None, date_to=None) -> ExportRecord:
    record = ExportRecord(report_type=report_type, format=fmt, filters_json=dumps(filters), date_from=date_from,
                          date_to=date_to, user_id=getattr(current_user, "id", None),
                          username=getattr(current_user, "username", "system"), source_ip=request_ip(),
                          outcome="started")
    db.session.add(record)
    db.session.commit()
    return record


def finish_export(record: ExportRecord, path: str, count: int, file_name: str) -> None:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block)
    record.outcome, record.completed_at, record.record_count = "success", utcnow(), count
    record.file_name, record.file_size, record.sha256 = file_name, os.path.getsize(path), sha.hexdigest()
    audit("export", "data_exported", resource_type="export", resource_id=record.export_uid,
          details={"report_type": record.report_type, "format": record.format, "records": count,
                   "filters": record.filters, "sha256": record.sha256, "file_name": file_name})
    db.session.commit()


def fail_export(record: ExportRecord, outcome: str, error: str) -> None:
    db.session.rollback()
    record = db.session.merge(record)
    record.outcome, record.completed_at, record.error = outcome, utcnow(), error[:1000]
    audit("export", "data_export_failed", resource_type="export", resource_id=record.export_uid,
          outcome="failure", details={"report_type": record.report_type, "format": record.format,
                                      "error": error[:500], "filters": record.filters})
    db.session.commit()


def temp_path(suffix: str) -> str:
    directory = os.path.join(current_app.config["EXPORT_DIR"], "tmp")
    os.makedirs(directory, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=suffix, dir=directory)
    os.close(fd)
    return path


# ------------------------------------------------------------------ accumulation
@dataclass
class ControllerSummary:
    node_uid: str
    node_name: str
    controller_uid: str
    controller_name: str
    site: str
    equipment: str
    readings: int = 0
    valid: int = 0
    total: float = 0.0
    min_c: float | None = None
    max_c: float | None = None
    comm_failures: int = 0
    with_faults: int = 0
    simulated: int = 0
    first: datetime | None = None
    last: datetime | None = None
    gaps: list = field(default_factory=list)
    chart: list = field(default_factory=list)

    def add(self, r: TemperatureReading, gap_after: timedelta | None) -> tuple[datetime, datetime] | None:
        gap = None
        if gap_after is not None and self.last is not None and r.acquired_at - self.last > gap_after:
            gap = (self.last, r.acquired_at)
            self.gaps.append(gap)
        self.readings += 1
        self.first = self.first or r.acquired_at
        self.last = r.acquired_at
        if r.temperature_c is not None:
            self.valid += 1
            self.total += r.temperature_c
            self.min_c = r.temperature_c if self.min_c is None else min(self.min_c, r.temperature_c)
            self.max_c = r.temperature_c if self.max_c is None else max(self.max_c, r.temperature_c)
        if r.comm_status != "ok":
            self.comm_failures += 1
        if r.fault_codes:
            self.with_faults += 1
        if r.source == "simulated":
            self.simulated += 1
        return gap

    @property
    def avg_c(self) -> float | None:
        return round(self.total / self.valid, 2) if self.valid else None


def _gap_analysis_applicable(f: ReadingFilter) -> bool:
    """Gaps are meaningful only if the filter selects whole time series."""
    return not any((f.min_temp is not None, f.max_temp is not None, f.fault, f.comm, f.compressor, f.alarm,
                    f.quality, f.ingest))


def _gap_after(controller_id: int, cache: dict[int, timedelta]) -> timedelta:
    if controller_id not in cache:
        ctrl = db.session.get(Controller, controller_id)
        interval = (ctrl.poll_interval_s if ctrl else None) or 10
        cache[controller_id] = timedelta(seconds=max(get_setting("gap_factor") * interval, 30))
    return cache[controller_id]


def _iter_rows(f: ReadingFilter):
    q = f.query(ordered=False).order_by(TemperatureReading.acquired_at.asc(), TemperatureReading.id.asc())
    yield from q.yield_per(2000)


def _row_values(r: TemperatureReading, node_uid, node_name, ctrl_uid, ctrl_name, zone) -> list:
    return [fmt_local(r.acquired_at, zone=zone), iso_utc(r.acquired_at), node_uid, ctrl_name or ctrl_uid,
            r.temperature_c, r.setpoint_c, r.actual_temperature_c,
            None if r.compressor_on is None else ("ON" if r.compressor_on else "OFF"),
            None if r.alarm_on is None else ("ON" if r.alarm_on else "OFF"),
            None if r.defrost_active is None else ("ON" if r.defrost_active else "OFF"),
            r.fault_codes.replace(",", ", "), r.comm_status, r.comm_error or "", r.quality,
            r.quality_flags.replace(",", ", "), r.source, "yes" if r.backfilled else "no", r.config_version,
            r.reading_uid]


READING_HEADERS = ["Timestamp (local)", "Timestamp (UTC)", "Node", "Controller", "Temperature °C", "Set point °C",
                   "Actual temp °C (40008)", "Compressor", "Alarm", "Defrost", "Faults", "Comm status",
                   "Comm error", "Quality", "Quality flags", "Source", "Backfilled", "Config version", "Reading ID"]


def export_readings(f: ReadingFilter, fmt: str, include_raw: bool = False) -> tuple[str, str, ExportRecord]:
    if fmt not in ("xlsx", "pdf"):
        raise ValueError("format must be xlsx or pdf")
    if f.as_of is None:
        f.as_of = utcnow()
    record = start_export("temperature_history", fmt, {**f.to_json(), "include_raw": include_raw}, f.start, f.end)
    count = f.count()
    limit = current_app.config["EXPORT_MAX_ROWS_XLSX" if fmt == "xlsx" else "EXPORT_MAX_ROWS_PDF"]
    if count > limit:
        message = (f"{count:,} records match, above the {fmt.upper()} limit of {limit:,}. Narrow the date range or "
                   f"filters{', or use Excel' if fmt == 'pdf' else ''}. Nothing was truncated.")
        fail_export(record, "rejected_too_large", message)
        raise ExportRejected("rejected_too_large", message)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    file_name = f"temperature-history-{stamp}-{record.export_uid[:8]}.{fmt}"
    path = temp_path("." + fmt)
    try:
        if fmt == "xlsx":
            written = _write_xlsx(f, path, record, count, include_raw)
        else:
            written = _write_pdf(f, path, record, count)
    except Exception as exc:
        os.remove(path)
        fail_export(record, "failed", f"{type(exc).__name__}: {exc}")
        raise
    if written != count:  # data cannot change thanks to as_of; guard anyway
        os.remove(path)
        fail_export(record, "failed", f"record count changed during export ({count} -> {written})")
        raise ExportRejected("failed", "The record set changed during export; please retry.")
    finish_export(record, path, count, file_name)
    return path, file_name, record


def _report_meta(record: ExportRecord, f: ReadingFilter, count: int) -> list[tuple[str, str]]:
    zone = display_zone()
    return [("Report ID", record.export_uid), ("Generated", f"{fmt_local(record.created_at)} ({zone.key}) / "
                                                            f"{iso_utc(record.created_at)}"),
            ("Generated by", record.username), ("Time zone", zone.key),
            ("Period", f"{fmt_local(f.start) if f.start else 'beginning'} – {fmt_local(f.end) if f.end else 'now'}"),
            ("Records", f"{count:,}")] + [(f"Filter: {k}", v) for k, v in f.describe()]


def _summaries(rows_iter, f: ReadingFilter, on_row=None) -> dict[int, ControllerSummary]:
    summaries: dict[int, ControllerSummary] = {}
    gaps_ok = _gap_analysis_applicable(f)
    thresholds: dict[int, timedelta] = {}
    for row in rows_iter:
        r, node_uid, node_name, site, equipment, ctrl_uid, ctrl_name = row
        s = summaries.get(r.controller_id)
        if s is None:
            s = summaries[r.controller_id] = ControllerSummary(node_uid, node_name, ctrl_uid, ctrl_name, site,
                                                               equipment)
        gap = s.add(r, _gap_after(r.controller_id, thresholds) if gaps_ok else None)
        if on_row:
            on_row(row, s, gap)
    return summaries


def _summary_rows(summaries) -> list[list]:
    return [[s.node_uid, s.controller_name or s.controller_uid, s.site, s.equipment, s.readings, s.valid,
             s.min_c, s.max_c, s.avg_c, s.comm_failures, s.with_faults, len(s.gaps),
             duration_text(sum((b - a).total_seconds() for a, b in s.gaps)), s.simulated]
            for s in summaries.values()]


SUMMARY_HEADERS = ["Node", "Controller", "Site", "Equipment", "Readings", "Valid temps", "Min °C", "Max °C",
                   "Avg °C", "Comm failures", "Readings with faults", "Gaps", "Gap time", "Simulated"]


def _fault_events(f: ReadingFilter, controller_ids) -> list[FaultEvent]:
    if not controller_ids:
        return []
    q = FaultEvent.query.filter(FaultEvent.controller_id.in_(list(controller_ids)))
    if f.end:
        q = q.filter(FaultEvent.started_at <= f.end)
    if f.start:
        q = q.filter((FaultEvent.cleared_at.is_(None)) | (FaultEvent.cleared_at >= f.start))
    return q.order_by(FaultEvent.started_at).limit(5000).all()


# ------------------------------------------------------------------ Excel
def _header_cells(ws, headers):
    cells = []
    for h in headers:
        c = WriteOnlyCell(ws, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F3A5F")
        c.alignment = Alignment(vertical="top", wrap_text=True)
        cells.append(c)
    return cells


def _write_xlsx(f: ReadingFilter, path: str, record: ExportRecord, count: int, include_raw: bool) -> int:
    zone = display_zone()
    wb = Workbook(write_only=True)
    summary_ws = wb.create_sheet("Summary")
    readings_ws = wb.create_sheet("Readings")
    gaps_ws = wb.create_sheet("Gaps")
    faults_ws = wb.create_sheet("Fault events")

    headers = READING_HEADERS + (["Raw registers"] if include_raw else [])
    widths = [20, 21, 14, 22, 13, 12, 14, 11, 8, 8, 28, 16, 30, 12, 22, 11, 10, 9, 38, 60]
    for i, width in enumerate(widths[:len(headers)]):
        readings_ws.column_dimensions[chr(65 + i)].width = width
    readings_ws.freeze_panes = "A2"
    readings_ws.auto_filter.ref = f"A1:{chr(64 + len(headers))}{max(count + 1, 2)}"
    readings_ws.append(_header_cells(readings_ws, headers))

    written = 0

    def on_row(row, _summary, _gap):
        nonlocal written
        r, node_uid, node_name, _site, _equipment, ctrl_uid, ctrl_name = row
        values = _row_values(r, node_uid, node_name, ctrl_uid, ctrl_name, zone)
        values[0] = _local_naive(r.acquired_at, zone)  # real Excel datetime in the display time zone
        if include_raw:
            values.append(r.raw_registers or "")
        readings_ws.append(values)
        written += 1

    summaries = _summaries(_iter_rows(f), f, on_row)

    summary_ws.column_dimensions["A"].width = 26
    for col in "BCDEFGHIJKLMN":
        summary_ws.column_dimensions[col].width = 16
    title = WriteOnlyCell(summary_ws, value="Temperature History Report")
    title.font = Font(bold=True, size=14)
    summary_ws.append([title])
    for key, value in _report_meta(record, f, count):
        summary_ws.append([key, value])
    summary_ws.append([])
    summary_ws.append(["Per-controller summary (calculated from the exported readings)"])
    summary_ws.append(_header_cells(summary_ws, SUMMARY_HEADERS))
    for row in _summary_rows(summaries):
        summary_ws.append(row)
    summary_ws.append([])
    for line in _notes(f, summaries):
        summary_ws.append([line])

    gaps_ws.column_dimensions["A"].width = 14
    gaps_ws.column_dimensions["B"].width = 22
    gaps_ws.column_dimensions["C"].width = 20
    gaps_ws.column_dimensions["D"].width = 20
    gaps_ws.column_dimensions["E"].width = 14
    gaps_ws.append(_header_cells(gaps_ws, ["Node", "Controller", "No data after", "Next data at", "Duration"]))
    for s in summaries.values():
        for a, b in s.gaps:
            gaps_ws.append([s.node_uid, s.controller_name or s.controller_uid, _local_naive(a, zone),
                            _local_naive(b, zone), duration_text((b - a).total_seconds())])

    faults_ws.append(_header_cells(faults_ws, ["Controller", "Kind", "Code", "Description", "Started", "Cleared",
                                               "Duration", "Status"]))
    for col, width in zip("ABCDEFGH", (22, 8, 22, 36, 20, 20, 14, 10)):
        faults_ws.column_dimensions[col].width = width
    for fe in _fault_events(f, summaries.keys()):
        faults_ws.append([f"{fe.controller.node.uid}/{fe.controller.label}", fe.kind, fe.code, fe.description,
                          _local_naive(fe.started_at, zone), _local_naive(fe.cleared_at, zone),
                          duration_text(((fe.cleared_at or utcnow()) - fe.started_at).total_seconds()), fe.status])
    wb.save(path)
    return written


def _local_naive(dt, zone):
    from .timeutil import to_local
    local = to_local(dt, zone)
    return None if local is None else local.replace(tzinfo=None)


def _notes(f: ReadingFilter, summaries) -> list[str]:
    notes = ["Readings are recorded values as reported by the controller via the node. Statistics, gaps and "
             "durations are CALCULATED by the server from those readings.",
             "Temperature is the controller's control (display) temperature, register 40006. Empty temperature "
             "cells mean no valid value (communication failure or probe fault) - values are never estimated."]
    if _gap_analysis_applicable(f):
        notes.append(f"A gap is a period between consecutive readings longer than {get_setting('gap_factor')} x the "
                     "controller's poll interval (minimum 30 s).")
    else:
        notes.append("Gap analysis is not shown because the filters exclude some readings.")
    simulated = sum(s.simulated for s in summaries.values())
    if simulated:
        notes.append(f"WARNING: {simulated} readings in this report are SIMULATED, not real measurements.")
    return notes


# ------------------------------------------------------------------ PDF
def _write_pdf(f: ReadingFilter, path: str, record: ExportRecord, count: int) -> int:
    zone = display_zone()
    table_rows: list[list] = []
    gap_rows: set[int] = set()
    chart_points: dict[int, list] = {}
    written = 0
    stride = max(1, count // 600)

    def on_row(row, summary, gap):
        nonlocal written
        r, node_uid, _node_name, _site, _equipment, ctrl_uid, ctrl_name = row
        if gap is not None:
            gap_rows.add(len(table_rows))
            table_rows.append([f"No data from {fmt_local(gap[0], zone=zone)} to {fmt_local(gap[1], zone=zone)} "
                               f"({duration_text((gap[1] - gap[0]).total_seconds())}) - {node_uid}/"
                               f"{ctrl_name or ctrl_uid}"] + [""] * 10)
            chart_points.setdefault(r.controller_id, []).append((gap[0], None, None))
        table_rows.append([fmt_local(r.acquired_at, zone=zone), node_uid, ctrl_name or ctrl_uid,
                           "" if r.temperature_c is None else f"{r.temperature_c:.1f}",
                           "" if r.setpoint_c is None else f"{r.setpoint_c:.1f}",
                           {True: "ON", False: "OFF"}.get(r.compressor_on, ""),
                           {True: "ON", False: "OFF"}.get(r.alarm_on, ""), r.fault_codes.replace(",", ", "),
                           r.comm_status, r.quality, "sim" if r.source == "simulated" else ""])
        if written % stride == 0 or r.temperature_c is None:
            chart_points.setdefault(r.controller_id, []).append((r.acquired_at, r.temperature_c, r.setpoint_c))
        written += 1

    summaries = _summaries(_iter_rows(f), f, on_row)
    doc = P.document(path, "Temperature History Report")
    story = [Paragraph("Temperature History Report", P.STYLES["title"]),
             P.kv_table(_report_meta(record, f, count)), Spacer(1, 6),
             Paragraph("Summary statistics (calculated)", P.STYLES["h2"])]
    story += P.data_tables(SUMMARY_HEADERS, [[("" if v is None else v) for v in row]
                                             for row in _summary_rows(summaries)])
    for line in _notes(f, summaries):
        story.append(P.note(line))

    faults = _fault_events(f, summaries.keys())
    story.append(Paragraph("Fault and alarm events (recorded transitions)", P.STYLES["h2"]))
    story += P.data_tables(["Controller", "Code", "Description", "Started", "Cleared", "Duration", "Status"],
                           [[f"{fe.controller.node.uid}/{fe.controller.label}", fe.code, fe.description,
                             fmt_local(fe.started_at, zone=zone), fmt_local(fe.cleared_at, zone=zone) or "active",
                             duration_text(((fe.cleared_at or utcnow()) - fe.started_at).total_seconds()), fe.status]
                            for fe in faults[:300]],
                           [P.CONTENT_WIDTH * w for w in (0.18, 0.12, 0.22, 0.14, 0.14, 0.1, 0.1)])
    if len(faults) > 300:
        story.append(P.note(f"{len(faults)} fault events in total; the first 300 are listed. Use the Excel export "
                            "or the fault report for all of them."))
    all_gaps = [(s, g) for s in summaries.values() for g in s.gaps]
    story.append(Paragraph("Communication gaps / missing data (calculated)", P.STYLES["h2"]))
    if not _gap_analysis_applicable(f):
        story.append(P.note("Not applicable: the filters exclude some readings."))
    else:
        story += P.data_tables(["Controller", "No data after", "Next data at", "Duration"],
                               [[f"{s.node_uid}/{s.controller_name or s.controller_uid}", fmt_local(a, zone=zone),
                                 fmt_local(b, zone=zone), duration_text((b - a).total_seconds())]
                                for s, (a, b) in all_gaps[:300]])

    charted = list(summaries.items())[:6]
    for controller_id, s in charted:
        points = chart_points.get(controller_id, [])
        story.append(P.trend_chart(
            [{"label": "Temperature °C", "points": [(t, v) for t, v, _ in points]},
             {"label": "Set point °C", "points": [(t, sp) for t, _, sp in points], "dashed": True}],
            f"Trend: {s.node_uid} / {s.controller_name or s.controller_uid}", zone))
    if len(summaries) > len(charted):
        story.append(P.note(f"Charts are shown for the first {len(charted)} controllers."))

    story += [PageBreak(), Paragraph("Readings (chronological)", P.STYLES["h2"]),
              P.note("Highlighted rows mark periods without data. Empty temperature = no valid value.")]
    widths = [P.CONTENT_WIDTH * w for w in (0.13, 0.09, 0.14, 0.07, 0.07, 0.06, 0.06, 0.16, 0.09, 0.09, 0.04)]
    story += P.data_tables(["Time", "Node", "Controller", "Temp °C", "SP °C", "Comp", "Alarm", "Faults", "Comm",
                            "Quality", "Src"], table_rows, widths, chunk=200, highlight_rows=gap_rows)
    doc.build(story, canvasmaker=P.numbered_canvas_factory(
        f"Temperature History Report · ID {record.export_uid} · generated {fmt_local(record.created_at)} {zone.key}"))
    return written
