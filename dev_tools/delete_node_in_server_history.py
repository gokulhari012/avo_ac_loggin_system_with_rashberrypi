import sqlite3

# DB = r"\home\temp\temperature-logging-system\instance\temperature.db"
DB = "/home/temp/temperature-logging-system/instance/temperature.db"
# NODE_UID = "NODE-001"
NODE_UID = "temp_control_unit_1"

db = sqlite3.connect(DB)
try:
    node = db.execute("SELECT id FROM nodes WHERE uid=?", (NODE_UID,)).fetchone()
    if node is None:
        print(f"No node found with uid {NODE_UID!r}")
    else:
        node_id = node[0]
        print("node row id:", node_id)

        related_tables = (
            ("temperature_readings", "node_id"),
            ("communication_events", "node_id"),
            ("fault_events", "node_id"),
            ("controller_commands", "node_id"),
            ("node_config_versions", "node_id"),
            ("node_heartbeats", "node_id"),
        )
        for table, column in related_tables:
            count = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (node_id,)
            ).fetchone()[0]
            print(f"{table}: {count} rows")

        controller_count = db.execute(
            "SELECT COUNT(*) FROM controllers WHERE node_id=?", (node_id,)
        ).fetchone()[0]
        history_count = db.execute(
            "SELECT COUNT(*) FROM controller_config_history "
            "WHERE controller_id IN (SELECT id FROM controllers WHERE node_id=?)",
            (node_id,),
        ).fetchone()[0]
        print(f"controller_config_history: {history_count} rows")
        print(f"controllers: {controller_count} rows")

        if input("delete this node and all its history? type yes: ") == "yes":
            with db:
                db.execute(
                    "DELETE FROM controller_config_history "
                    "WHERE controller_id IN "
                    "(SELECT id FROM controllers WHERE node_id=?)",
                    (node_id,),
                )
                for table, column in related_tables:
                    db.execute(
                        f"DELETE FROM {table} WHERE {column}=?", (node_id,)
                    )
                db.execute("DELETE FROM controllers WHERE node_id=?", (node_id,))
                db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            print("done")
finally:
    db.close()
