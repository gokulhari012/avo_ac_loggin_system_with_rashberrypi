"""Trend data, statistics and communication-gap detection for one controller.

All values here are CALCULATED from recorded readings; reports label them so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func

from ..extensions import db
from ..models import Controller, TemperatureReading
from .settings import get_setting


@dataclass
class Gap:
    start: datetime      # last reading attempt before the gap
    end: datetime        # first reading attempt after the gap
    seconds: float

    def to_dict(self) -> dict:
        return {"start": _ms(self.start), "end": _ms(self.end), "duration_s": round(self.seconds)}


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def gap_threshold(ctrl: Controller) -> timedelta:
    interval = ctrl.poll_interval_s or 10
    return timedelta(seconds=max(get_setting("gap_factor") * interval, 30))


def find_gaps(times: Iterable[datetime], threshold: timedelta) -> list[Gap]:
    gaps, previous = [], None
    for t in times:
        if previous is not None and t - previous > threshold:
            gaps.append(Gap(previous, t, (t - previous).total_seconds()))
        previous = t
    return gaps


def range_stats(controller_id: int, start: datetime, end: datetime) -> dict:
    R = TemperatureReading
    base = db.session.query(R).filter(R.controller_id == controller_id, R.acquired_at >= start,
                                      R.acquired_at <= end)
    total = base.count()
    valid = (db.session.query(func.min(R.temperature_c), func.max(R.temperature_c), func.avg(R.temperature_c),
                              func.count(R.temperature_c))
             .filter(R.controller_id == controller_id, R.acquired_at >= start, R.acquired_at <= end,
                     R.temperature_c.isnot(None)).one())
    by_quality = dict(db.session.query(R.quality, func.count()).filter(
        R.controller_id == controller_id, R.acquired_at >= start, R.acquired_at <= end).group_by(R.quality).all())
    comm_failures = base.filter(R.comm_status != "ok").count()
    with_faults = base.filter(R.fault_codes != "").count()
    first_last = (db.session.query(func.min(R.acquired_at), func.max(R.acquired_at))
                  .filter(R.controller_id == controller_id, R.acquired_at >= start, R.acquired_at <= end).one())
    return {
        "readings": total, "valid_temperatures": valid[3] or 0,
        "min_c": None if valid[0] is None else round(valid[0], 2),
        "max_c": None if valid[1] is None else round(valid[1], 2),
        "avg_c": None if valid[2] is None else round(valid[2], 2),
        "by_quality": by_quality, "comm_failures": comm_failures, "readings_with_faults": with_faults,
        "first_at": first_last[0], "last_at": first_last[1],
    }


def controller_series(ctrl: Controller, start: datetime, end: datetime, max_points: int = 1500) -> dict:
    R = TemperatureReading
    rows = (db.session.query(R.acquired_at, R.temperature_c, R.setpoint_c, R.comm_status, R.quality,
                             R.fault_codes, R.compressor_on, R.alarm_on)
            .filter(R.controller_id == ctrl.id, R.acquired_at >= start, R.acquired_at <= end)
            .order_by(R.acquired_at).all())
    threshold = gap_threshold(ctrl)
    gaps = find_gaps((r[0] for r in rows), threshold)
    gap_starts = {g.start for g in gaps}
    faults, comm_errors = [], []
    prev_faults = ""
    for r in rows:
        if r.comm_status != "ok":
            comm_errors.append([_ms(r.acquired_at)])
        elif r.fault_codes and r.fault_codes != prev_faults:
            faults.append([_ms(r.acquired_at), r.temperature_c, r.fault_codes])
        if r.comm_status == "ok":
            prev_faults = r.fault_codes

    decimated = len(rows) > max_points
    points: list[list] = []
    if not decimated:
        for r in rows:
            points.append([_ms(r.acquired_at), r.temperature_c, r.setpoint_c])
            if r.acquired_at in gap_starts:
                points.append([_ms(r.acquired_at) + 1, None, None])  # break the line across the gap
    else:
        # Min/max per time bucket preserves excursions that averaging would hide.
        span = max((end - start).total_seconds(), 1)
        buckets = max_points // 2
        size = span / buckets
        current, bucket_rows = None, []

        def flush():
            valid = [b for b in bucket_rows if b.temperature_c is not None]
            if not valid:
                points.append([_ms(bucket_rows[0].acquired_at), None, None])
                return
            lo = min(valid, key=lambda b: b.temperature_c)
            hi = max(valid, key=lambda b: b.temperature_c)
            for b in sorted({lo, hi}, key=lambda b: b.acquired_at):
                points.append([_ms(b.acquired_at), b.temperature_c, b.setpoint_c])

        for r in rows:
            index = int((r.acquired_at - start).total_seconds() // size)
            if current is not None and index != current:
                flush()
                bucket_rows = []
            if current is not None and index > current + 1:
                points.append([_ms(r.acquired_at) - 1, None, None])
            current = index
            bucket_rows.append(r)
            if r.acquired_at in gap_starts:
                flush()
                bucket_rows = []
                points.append([_ms(r.acquired_at) + 1, None, None])
                current = None
        if bucket_rows:
            flush()

    stats = range_stats(ctrl.id, start, end)
    stats["gaps"] = len(gaps)
    stats["gap_seconds"] = round(sum(g.seconds for g in gaps))
    for key in ("first_at", "last_at"):
        stats[key] = _ms(stats[key]) if stats[key] else None
    return {"points": points, "faults": faults[:2000], "comm_errors": comm_errors[:5000],
            "gaps": [g.to_dict() for g in gaps[:1000]], "stats": stats, "decimated": decimated,
            "count": len(rows), "gap_threshold_s": threshold.total_seconds()}
