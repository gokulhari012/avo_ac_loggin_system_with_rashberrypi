"""Data Logs filters, sorting, pagination and time-zone handling."""

from datetime import datetime, timedelta

from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.services.queries import ReadingFilter
from app.services.settings import set_setting
from app.services.timeutil import parse_local_input, to_local
from tests.conftest import comm_failure, make_node, node_headers, reading, utcnow

BASE = datetime(2026, 3, 1, 12, 0, 0)


def ingest(client, readings, uid="NODE-1", token="node-token-0123456789"):
    r = client.post("/api/v1/readings", json={"readings": readings}, headers=node_headers(uid, token))
    assert r.status_code == 200 and not r.json["rejected"], r.json


def dataset(client):
    make_node()
    make_node("NODE-2", "second-token-0123456789", site="Plant B", equipment="Freezer")
    ingest(client, [
        reading(at=BASE, temp=-18.0),
        reading(at=BASE + timedelta(minutes=1), temp=-10.0, faults=["high_temp"], fault_flags=4, alarm_on=True),
        reading(at=BASE + timedelta(minutes=2), temp=-17.0, compressor_on=False,
                faults=["manual_compressor_fault"], fault_flags=64),
        comm_failure(at=BASE + timedelta(minutes=3)),
    ])
    ingest(client, [reading(at=BASE + timedelta(minutes=1), temp=4.0, source="simulated"),
                    reading(at=BASE + timedelta(minutes=2), temp=None, quality="probe_fault",
                            faults=["probe_fail_low"], fault_flags=1)],
           "NODE-2", "second-token-0123456789")


def run(args) -> list:
    f = ReadingFilter.from_args(MultiDict(args), default_hours=None)
    assert not f.errors, f.errors
    return [row[0] for row in f.query().all()]


def test_time_zone_conversion_of_inputs(app):
    set_setting("display_timezone", "Asia/Kolkata", "test")
    db.session.commit()
    assert parse_local_input("2026-03-01T17:30") == BASE  # IST = UTC+5:30
    assert to_local(BASE).strftime("%H:%M") == "17:30"


def test_date_range_filter_uses_display_zone(client, app):
    dataset(client)
    set_setting("display_timezone", "Asia/Kolkata", "test")
    db.session.commit()
    rows = run({"start": "2026-03-01T17:31", "end": "2026-03-01T17:32"})  # = 12:01-12:02 UTC
    assert {r.acquired_at for r in rows} == {BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)}


def test_filters(client, app):
    dataset(client)
    assert len(run({})) == 6
    assert len(run({"node": "2"})) == 2
    assert len(run({"site": "Plant B"})) == 2
    assert len(run({"equipment": "Freezer"})) == 2
    assert {r.temperature_c for r in run({"min_temp": "-12", "max_temp": "0"})} == {-10.0}
    assert len(run({"fault": "any"})) == 3
    assert len(run({"fault": "none"})) == 2
    # compressor_fault must not match manual_compressor_fault
    assert run({"fault": "compressor_fault"}) == []
    assert len(run({"fault": "manual_compressor_fault"})) == 1
    assert [r.comm_status for r in run({"comm": "error"})] == ["timeout"]
    assert len(run({"comm": "timeout"})) == 1
    assert len(run({"compressor": "off"})) == 1
    assert len(run({"alarm": "on"})) == 1
    assert len(run({"quality": "probe_fault"})) == 1
    assert len(run({"source": "simulated"})) == 1


def test_sorting(client, app):
    dataset(client)
    temps = [r.temperature_c for r in run({"sort": "temperature", "dir": "asc"})]
    assert temps[:4] == [-18.0, -17.0, -10.0, 4.0] and temps[4:] == [None, None]  # NULLs last
    times = [r.acquired_at for r in run({"sort": "time", "dir": "desc"})]
    assert times == sorted(times, reverse=True)


def test_as_of_freezes_record_set(client, app):
    dataset(client)
    as_of = utcnow()
    f = ReadingFilter.from_args(MultiDict({"as_of": as_of.isoformat() + "Z"}), default_hours=None)
    before = f.count()
    ingest(client, [reading(at=BASE + timedelta(minutes=5))])
    assert f.count() == before


def test_invalid_input_reported_not_crashing(app):
    f = ReadingFilter.from_args(MultiDict({"start": "yesterday", "min_temp": "cold"}))
    assert len(f.errors) == 2


def test_logs_page_counts_filters_and_pagination(as_user, users, client):
    dataset(client)
    page = as_user("operator").get("/logs?start=2026-03-01T00:00&per_page=50&node=1")
    assert page.status_code == 200
    assert b"4 matching records" in page.data and b"Node: NODE-1" in page.data
    assert b"Reset all" in page.data


def test_logs_pagination(as_user, users, client):
    make_node()
    ingest(client, [reading(at=BASE + timedelta(seconds=i)) for i in range(120)])
    page2 = as_user("operator").get("/logs?start=2026-03-01T00:00&per_page=50&page=3")
    assert page2.status_code == 200 and page2.data.count(b"<tr>") >= 20 and b"page 3 of 3" in page2.data


def test_sql_injection_attempt_is_harmless(as_user, users, client):
    dataset(client)
    r = as_user("operator").get("/logs?site=' OR 1=1 --&start=2026-03-01T00:00")
    assert r.status_code == 200 and b"0 matching records" in r.data
