# Architecture

```
 SZ-7526TX-REA ──RS-485── Raspberry Pi node ──Wi-Fi/LAN (HTTP/HTTPS, node-initiated)──► Windows server ──► DB
   (Modbus RTU slave)       (Python service)                                              (Flask + Waitress)  │
                                                                                                             ▼
                                                                         Browser: dashboard, logs, reports, audit
```

The two applications are independent. The node never imports server code, and the server only knows the JSON API. Every connection is opened by the node, so the server never needs to reach the Pi.

## Raspberry Pi node (`raspberry_pi_node/src`)

| Component | Module | Responsibility |
|---|---|---|
| Register map | `registers.py` | Datasheet facts: addresses, types, bits, sentinels, blocks ≤ 50 |
| Decoder | `decoder.py` | Pure functions: int16, rS scaling, sentinels, bitfields, quality rules |
| Modbus transport | `modbus_client.py` | One `SerialBus` (lock) per port, explicit retries, error classification, guarded writes |
| Polling | `collector.py` | One `BusCollector` thread per port: scheduled polls, records, comm-loss events, parameter snapshots, commands between polls |
| Local buffer | `local_queue.py` | SQLite (WAL, `synchronous=FULL`): readings, events, persistent state; capacity and disk-full policy |
| Server communication | `server_client.py`, `uploader.py` | JSON API client; FIFO batch upload, exponential backoff, idempotent acknowledgement |
| Health | `health_monitor.py` | Metrics, system stats (`/proc`, `/sys`), clock sync, heartbeat thread |
| Configuration | `config_manager.py`, `config_sync.py` | Local YAML + versioned remote overlay, validation, apply/reject, acknowledgement |
| Commands | `commands.py` | Server-requested diagnostics/commands with local safety gate |
| Supervisor | `main.py` | Starts and restarts threads, applies new config, systemd notify/watchdog |
| Logging | `logging_config.py` | Rotating files, journald, secret redaction |

**Data path.** Poll → `build_reading()` → `LocalQueue.put_reading()`, committed to disk → `Uploader` sends the oldest batch → the server commits → the server answers `accepted`/`duplicate` → the node marks the records delivered. Until that answer arrives a record stays pending, survives restarts, and is re-sent. Re-sending is harmless because the server de-duplicates on the node-generated reading UUID.

**Failure isolation.** A Modbus error produces a reading with `comm_status != ok` and null values, never an exception that stops the loop. A server outage only grows the queue. A dead thread is restarted by the supervisor, and systemd restarts the process if the watchdog stops being fed.

**Clock.** The Pi has no battery-backed clock. Readings taken while NTP is not synchronised carry the flag `clock_unsynchronized` (quality `suspect`). The server also computes clock skew from each heartbeat.

## Windows server (`server/app`)

| Layer | Contents |
|---|---|
| Routes | `api.py` (node API, token auth, no CSRF), `auth.py`, `dashboard.py`, `monitor.py`, `logs.py`, `nodes.py`, `reports.py`, `audit_views.py`, `admin.py` |
| Services | ingestion (validation, idempotency, fault tracking), node_config (versions/ack), status (live/stale/offline classification), queries (one filter for page + exports), series (chart, stats, gaps), exports (Excel/PDF), reports (audit reports), audit, settings, monitor (offline detection, retention) |
| Models | SQLAlchemy models in `models/`, migrations in `server/migrations` (Alembic via Flask-Migrate) |
| UI | Jinja2 + Bootstrap 5 + Chart.js, all vendored under `static/vendor` (works without internet); JS in static files (strict CSP) |
| Runtime | Waitress WSGI server (`run.py`), background monitor thread, rotating logs |

## Database schema

| Table | Purpose | Key indexes |
|---|---|---|
| `users`, `roles`, `permissions`, `role_permissions` | RBAC | username |
| `nodes` | identity, token hash, latest health | uid, site, equipment, last_seen_at |
| `controllers` | per node; latest state (denormalised for the dashboard) | (node_id, uid) unique |
| `node_config_versions` | **node configuration history**: versioned desired config + delivery/ack status | (node_id, version) unique, status |
| `controller_config_history` | server-side controller changes and device parameter changes | controller_id, recorded_at |
| `temperature_readings` | every poll attempt, never updated | reading_uid unique; (controller_id, acquired_at); (node_id, acquired_at); acquired_at; quality; comm_status; source; backfilled |
| `communication_events` | node-reported and server-generated events | (node_id, occurred_at), event_type |
| `node_heartbeats` | heartbeat history (retention configurable) | (node_id, received_at) |
| `fault_events` | fault/alarm periods derived from register transitions | (controller_id, started_at), status, code |
| `audit_events` | append-only audit trail (ORM guard + DB triggers) | occurred_at, category, action, actor, resource |
| `export_records` | every export/report: who, filters, count, SHA-256, outcome | created_at, report_type |
| `controller_commands` | requested commands and results | node_id, status |
| `discovered_nodes` | unregistered devices that tried to connect | uid |
| `system_settings` | admin-editable settings | key |

**Provenance.** Each reading carries `reading_uid` (generated on the node at acquisition), `node_id`, `controller_id`, the node's `config_version`, `slave_id` and model, `source` (controller or simulated), `acquired_at` (node clock), `received_at` (server clock), `backfilled`, and the raw register values.

**Time.** All timestamps are stored as naive UTC. The display time zone is a setting, and form inputs are interpreted in that zone.

## Security model

- Node API: `X-Node-ID` + `Authorization: Bearer <token>`. Only the SHA-256 of the token is stored, compared in constant time. Unknown IDs are recorded as *discovered*, never trusted. Disabled nodes get 403. The body `node_id` must match the authenticated node.
- Web: Flask-Login sessions (HttpOnly, SameSite=Lax, optional Secure, idle timeout), CSRF on all forms, lockout after repeated failures, server-side RBAC on every route, strict CSP, no tracebacks shown to users (errors carry a request ID).
- Secrets: `SECRET_KEY` from `.env` or a generated instance file; node token in `/etc/temperature-node/node.env` (mode 640). Redaction is applied to logs and audit records.
- Controller writes: see [modbus-register-map.md](modbus-register-map.md#coils-function-01-read--05-write). Disabled by default and gated in four places.

## Decisions and deviations from the brief

| Topic | Decision | Reason |
|---|---|---|
| Modbus library | pymodbus 3.11.3 (pinned) | Maintained; its serial client accepts `socket://` URLs, so the simulator exercises the real RTU path |
| Excel | openpyxl write-only (no pandas) | Streams 500k rows with constant memory; pandas would hold the whole dataset |
| PDF charts | ReportLab native graphics | No matplotlib dependency |
| HTTPS | Waitress serves HTTP; TLS via a reverse proxy (Caddy example in installation-windows.md) | Waitress has no TLS. The node supports `https://` and custom CA bundles. |
| Remote parameter writes | not implemented | See the register map document |
| Export files | not stored on the server; SHA-256 recorded | Avoids an unbounded file store; the hash proves a copy's integrity |
| Windows service | Task Scheduler (built in); NSSM documented as alternative | No third-party binary required |
