# delete_controllers.py  (run with server\.venv\Scripts\python)
import sqlite3

# DB = r"\home\temp\temperature-logging-system\instance\temperature.db"
DB = "/home/temp/temperature-logging-system/instance/temperature.db"
# NODE_UID = "NODE-001"
NODE_UID = "temp_control_unit_1"
CONTROLLER_UIDS = ["ctrl-1", "ctrl-2"]   # the test ones ONLY

db = sqlite3.connect(DB)
marks = ",".join("?" * len(CONTROLLER_UIDS))
ids = [r[0] for r in db.execute(
    f"SELECT c.id FROM controllers c JOIN nodes n ON n.id=c.node_id "
    f"WHERE n.uid=? AND c.uid IN ({marks})", (NODE_UID, *CONTROLLER_UIDS))]
print("controller row ids:", ids)
if ids:
    m = ",".join("?" * len(ids))
    for t in ("temperature_readings", "communication_events", "fault_events",
              "controller_config_history", "controller_commands"):
        n = db.execute(f"SELECT COUNT(*) FROM {t} WHERE controller_id IN ({m})", ids).fetchone()[0]
        print(f"{t}: {n} rows")
    if input("delete all of this? type yes: ") == "yes":
        for t in ("temperature_readings", "communication_events", "fault_events",
                  "controller_config_history", "controller_commands"):
            db.execute(f"DELETE FROM {t} WHERE controller_id IN ({m})", ids)
        db.execute(f"DELETE FROM controllers WHERE id IN ({m})", ids)
        db.commit()
        print("done")
