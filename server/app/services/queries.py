"""Reading filters shared by the Data Logs page, Excel export and PDF export.

All three build their record set from ``ReadingFilter.query()``, so an Excel
and a PDF exported from the same page (same filters and the same ``as_of``
cutoff on received_at) contain exactly the same readings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import literal, or_

from ..extensions import db
from ..models import Controller, Node, TemperatureReading
from .timeutil import fmt_local, iso_utc, local_input_value, parse_iso_utc, parse_local_input, utcnow

FAULT_CODES = {
    "probe_fail_low": "Probe fail low (PFL)", "probe_fail_high": "Probe fail high (PFH)",
    "high_temp": "High temperature (Ht)", "low_temp": "Low temperature (Lt)", "door_open": "Door open (do)",
    "compressor_fault": "Compressor fault (CF)", "manual_compressor_fault": "Compressor fault - manual reset",
}
COMM_CHOICES = {"ok": "OK", "error": "Any failure", "timeout": "Timeout / no response",
                "exception": "Modbus exception", "invalid_response": "Invalid response", "port_error": "Port error",
                "identity_mismatch": "Identity mismatch", "error_other": "Other error"}
QUALITY_CHOICES = {"good": "Good", "suspect": "Suspect", "probe_fault": "Probe fault", "invalid": "Invalid",
                   "comm_error": "Communication error"}
SORTS = {"time": "Timestamp", "temperature": "Temperature", "node": "Node", "controller": "Controller",
         "faults": "Fault status"}

READING_COLUMNS = (TemperatureReading, Node.uid, Node.name, Node.site, Node.equipment, Controller.uid,
                   Controller.name)


@dataclass
class ReadingFilter:
    start: datetime | None = None
    end: datetime | None = None
    node_id: int | None = None
    controller_id: int | None = None
    site: str | None = None
    equipment: str | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    fault: str | None = None
    comm: str | None = None
    compressor: str | None = None
    alarm: str | None = None
    quality: str | None = None
    source: str | None = None
    ingest: str | None = None
    as_of: datetime | None = None
    sort: str = "time"
    direction: str = "desc"
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_args(cls, args, *, default_hours: int | None = 24) -> "ReadingFilter":
        f = cls()

        def text(name, maxlen=128):
            value = (args.get(name) or "").strip()
            return value[:maxlen] or None

        def number(name, cast):
            value = text(name)
            if value is None:
                return None
            try:
                return cast(value)
            except ValueError:
                f.errors.append(f"{name} must be a number")
                return None

        try:
            f.start = parse_local_input(text("start"))
            f.end = parse_local_input(text("end"))
        except ValueError:
            f.errors.append("dates must be in the form YYYY-MM-DDTHH:MM")
        if f.start is None and f.end is None and default_hours and "start" not in args:
            f.start = utcnow() - timedelta(hours=default_hours)
        if f.start and f.end and f.start > f.end:
            f.errors.append("start must be before end")
        f.node_id = number("node", int)
        f.controller_id = number("controller", int)
        f.site, f.equipment = text("site"), text("equipment")
        f.min_temp, f.max_temp = number("min_temp", float), number("max_temp", float)
        f.fault = text("fault") if text("fault") in ({"any", "none"} | set(FAULT_CODES)) else None
        f.comm = text("comm") if text("comm") in COMM_CHOICES else None
        f.compressor = text("compressor") if text("compressor") in ("on", "off") else None
        f.alarm = text("alarm") if text("alarm") in ("on", "off") else None
        f.quality = text("quality") if text("quality") in QUALITY_CHOICES else None
        f.source = text("source") if text("source") in ("controller", "simulated") else None
        f.ingest = text("ingest") if text("ingest") in ("live", "backfilled") else None
        if text("as_of"):
            try:
                f.as_of = parse_iso_utc(text("as_of"))
            except ValueError:
                f.errors.append("as_of is invalid")
        f.sort = text("sort") if text("sort") in SORTS else "time"
        f.direction = "asc" if text("dir") == "asc" else "desc"
        return f

    # ------------------------------------------------------------------ query
    def query(self, *, ordered: bool = True):
        R = TemperatureReading
        q = (db.session.query(*READING_COLUMNS)
             .join(Node, Node.id == R.node_id).join(Controller, Controller.id == R.controller_id))
        if self.start:
            q = q.filter(R.acquired_at >= self.start)
        if self.end:
            q = q.filter(R.acquired_at <= self.end)
        if self.as_of:
            q = q.filter(R.received_at <= self.as_of)
        if self.node_id:
            q = q.filter(R.node_id == self.node_id)
        if self.controller_id:
            q = q.filter(R.controller_id == self.controller_id)
        if self.site:
            q = q.filter(Node.site == self.site)
        if self.equipment:
            q = q.filter(or_(Node.equipment == self.equipment, Controller.name == self.equipment))
        if self.min_temp is not None:
            q = q.filter(R.temperature_c >= self.min_temp)
        if self.max_temp is not None:
            q = q.filter(R.temperature_c <= self.max_temp)
        if self.fault == "any":
            q = q.filter(R.fault_codes != "")
        elif self.fault == "none":
            q = q.filter(R.fault_codes == "", R.comm_status == "ok")
        elif self.fault:
            q = q.filter((literal(",") + R.fault_codes + literal(",")).like(f"%,{self.fault},%"))
        if self.comm == "ok":
            q = q.filter(R.comm_status == "ok")
        elif self.comm == "error":
            q = q.filter(R.comm_status != "ok")
        elif self.comm == "error_other":
            q = q.filter(R.comm_status == "error")
        elif self.comm:
            q = q.filter(R.comm_status == self.comm)
        if self.compressor:
            q = q.filter(R.compressor_on.is_(self.compressor == "on"))
        if self.alarm:
            q = q.filter(R.alarm_on.is_(self.alarm == "on"))
        if self.quality:
            q = q.filter(R.quality == self.quality)
        if self.source:
            q = q.filter(R.source == self.source)
        if self.ingest:
            q = q.filter(R.backfilled.is_(self.ingest == "backfilled"))
        if ordered:
            column = {"time": R.acquired_at, "temperature": R.temperature_c, "node": Node.uid,
                      "controller": Controller.uid, "faults": R.fault_flags}[self.sort]
            if self.direction == "asc":
                q = q.order_by(column.asc().nulls_last(), R.id.asc())
            else:
                q = q.order_by(column.desc().nulls_last(), R.id.desc())
        return q

    def count(self) -> int:
        return self.query(ordered=False).count()

    # ------------------------------------------------------------------ presentation
    def to_args(self, **overrides) -> dict:
        args = {
            "start": local_input_value(self.start) if self.start else "",
            "end": local_input_value(self.end) if self.end else "",
            "node": self.node_id or "", "controller": self.controller_id or "", "site": self.site or "",
            "equipment": self.equipment or "",
            "min_temp": "" if self.min_temp is None else self.min_temp,
            "max_temp": "" if self.max_temp is None else self.max_temp,
            "fault": self.fault or "", "comm": self.comm or "", "compressor": self.compressor or "",
            "alarm": self.alarm or "", "quality": self.quality or "", "source": self.source or "",
            "ingest": self.ingest or "", "sort": self.sort, "dir": self.direction,
            "as_of": iso_utc(self.as_of) or "",
        }
        args.update(overrides)
        return {k: v for k, v in args.items() if v not in ("", None)}

    def describe(self) -> list[tuple[str, str]]:
        out = []
        if self.start:
            out.append(("From", fmt_local(self.start)))
        if self.end:
            out.append(("To", fmt_local(self.end)))
        if self.node_id:
            node = db.session.get(Node, self.node_id)
            out.append(("Node", node.label if node else f"#{self.node_id}"))
        if self.controller_id:
            ctrl = db.session.get(Controller, self.controller_id)
            out.append(("Controller", f"{ctrl.node.uid}/{ctrl.label}" if ctrl else f"#{self.controller_id}"))
        for label, value in (("Site", self.site), ("Equipment", self.equipment)):
            if value:
                out.append((label, value))
        if self.min_temp is not None:
            out.append(("Min temperature", f"{self.min_temp} °C"))
        if self.max_temp is not None:
            out.append(("Max temperature", f"{self.max_temp} °C"))
        if self.fault:
            out.append(("Fault", {"any": "Any fault", "none": "No fault"}.get(self.fault, FAULT_CODES.get(self.fault, self.fault))))
        if self.comm:
            out.append(("Communication", COMM_CHOICES[self.comm]))
        if self.compressor:
            out.append(("Compressor", self.compressor.upper()))
        if self.alarm:
            out.append(("Alarm", self.alarm.upper()))
        if self.quality:
            out.append(("Data quality", QUALITY_CHOICES[self.quality]))
        if self.source:
            out.append(("Source", self.source))
        if self.ingest:
            out.append(("Ingestion", "Backfilled from node buffer" if self.ingest == "backfilled" else "Live"))
        if self.as_of:
            out.append(("Data received up to", fmt_local(self.as_of)))
        return out

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("errors")
        return {k: (iso_utc(v) if isinstance(v, datetime) else v) for k, v in data.items() if v not in (None, "")}
