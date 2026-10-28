import time
import uuid

from src.local_queue import LocalQueue, new_event


def reading(i=0, controller="ctrl-1"):
    return {"reading_id": str(uuid.uuid4()), "controller_id": controller,
            "acquired_at": f"2026-01-01T00:00:{i % 60:02d}.000Z", "temperature_c": -18.0 + i / 10}


def test_fifo_and_delivery(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    records = [reading(i) for i in range(5)]
    for r in records:
        assert q.put_reading(r)
    batch = q.next_batch("readings", 3)
    assert [b["reading_id"] for b in batch] == [r["reading_id"] for r in records[:3]]
    q.mark_delivered("readings", [b["reading_id"] for b in batch])
    assert q.stats()["pending_readings"] == 2
    assert [b["reading_id"] for b in q.next_batch("readings", 10)] == [r["reading_id"] for r in records[3:]]


def test_persists_across_restart(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    records = [reading(i) for i in range(4)]
    for r in records:
        q.put_reading(r)
    q.mark_delivered("readings", [records[0]["reading_id"]])
    q.set_state("outage_started_at", "2026-01-01T00:00:00Z")
    q.close()

    q2 = LocalQueue(tmp_path / "q.db")  # simulated process restart
    assert q2.stats()["pending_readings"] == 3
    assert [b["reading_id"] for b in q2.next_batch("readings", 10)] == [r["reading_id"] for r in records[1:]]
    assert q2.get_state("outage_started_at") == "2026-01-01T00:00:00Z"


def test_duplicate_insert_ignored(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    r = reading()
    q.put_reading(r)
    q.put_reading(r)
    assert q.stats()["pending_readings"] == 1


def test_mark_delivered_is_idempotent(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    r = reading()
    q.put_reading(r)
    q.mark_delivered("readings", [r["reading_id"]])
    q.mark_delivered("readings", [r["reading_id"]])
    assert q.stats()["pending_readings"] == 0


def test_rejected_records_leave_queue_but_are_kept(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    r = reading()
    q.put_reading(r)
    q.mark_rejected("readings", {r["reading_id"]: "validation failed"})
    stats = q.stats()
    assert stats["pending_readings"] == 0 and stats["rejected_readings"] == 1


def test_drop_oldest_when_full_reports_gap(tmp_path):
    q = LocalQueue(tmp_path / "q.db", max_pending_records=1000, full_behavior="drop_oldest")
    records = [reading(i) for i in range(1005)]
    for r in records:
        assert q.put_reading(r)
    stats = q.stats()
    assert stats["pending_readings"] <= 1000 and stats["dropped_total"] >= 5
    remaining = {b["reading_id"] for b in q.next_batch("readings", 2000)}
    assert records[0]["reading_id"] not in remaining and records[-1]["reading_id"] in remaining
    gaps = [e for e in q.next_batch("events", 100) if e["event_type"] == "data_gap"]
    assert gaps and gaps[0]["details"]["reason"] == "buffer_full_drop_oldest"
    assert sum(g["details"]["records_lost"] for g in gaps) == stats["dropped_total"]


def test_stop_collecting_when_full_reports_gap(tmp_path):
    q = LocalQueue(tmp_path / "q.db", max_pending_records=1000, full_behavior="stop_collecting")
    records = [reading(i) for i in range(1003)]
    results = [q.put_reading(r) for r in records]
    assert results[:1000] == [True] * 1000 and results[1000:] == [False] * 3
    q.mark_delivered("readings", [r["reading_id"] for r in records[:10]])
    assert q.put_reading(reading())  # space again: accepted, episode closed
    gaps = [e for e in q.next_batch("events", 100) if e["event_type"] == "data_gap"]
    assert gaps and gaps[0]["details"]["records_lost"] == 3


def test_purge_delivered(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    r = reading()
    q.put_reading(r)
    q.mark_delivered("readings", [r["reading_id"]])
    time.sleep(0.01)
    assert q.purge_delivered(older_than_days=0) == 1


def test_events_queue(tmp_path):
    q = LocalQueue(tmp_path / "q.db")
    e = new_event("comm_lost", "lost", severity="error", controller_id="ctrl-1")
    q.put_event(e)
    q.put_event(e)
    assert [x["event_id"] for x in q.next_batch("events", 10)] == [e["event_id"]]
