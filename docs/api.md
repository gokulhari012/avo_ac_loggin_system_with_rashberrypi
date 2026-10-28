# Node API (v1)

Base path `/api/v1`. JSON only (`Content-Type: application/json`). Request size limit `MAX_REQUEST_BYTES` (4 MB); batch limit `API_MAX_BATCH` (1000 items).

**Authentication** (all endpoints except `/ping`):

```
X-Node-ID: NODE-001
Authorization: Bearer <token shown once at registration>
X-Request-ID: <optional, echoed back and stored in audit records>
```

**Errors** always look like:

```json
{"error": {"code": "invalid_credentials", "message": "node credentials were not accepted", "details": null, "request_id": "4f1c..."}}
```

| HTTP | code | Meaning / node behaviour |
|---|---|---|
| 400 | `invalid_request`, `invalid_json` | Malformed envelope. The node halves the batch to isolate the bad record. |
| 401 | `unauthorized`, `unknown_node`, `invalid_credentials` | Not registered or wrong token. The node keeps buffering and backs off. |
| 403 | `node_disabled`, `node_mismatch` | Disabled by an administrator, or the body `node_id` is not the authenticated node. |
| 404 | `unknown_version`, `unknown_command` | – |
| 413 | `batch_too_large`, `request_entity_too_large` | The node halves the batch. |
| 415 | `unsupported_media_type` | – |
| 5xx | `internal_error` | Retried with exponential backoff. |

## GET /ping
Unauthenticated health check: `{"status": "ok", "server_time": "...Z", "version": "1.0.0"}`.

## POST /readings

```json
{"node_id": "NODE-001", "readings": [{
  "reading_id": "5d0c9a8e-...-uuid4",       "controller_id": "ctrl-1", "slave_id": 1, "model": "SZ-7526TX-REA",
  "acquired_at": "2026-10-27T10:00:00.123Z", "source": "controller", "config_version": 3,
  "comm_status": "ok", "comm_error": null, "quality": "good", "quality_flags": [],
  "temperature_c": -18.2, "display_temperature_c": -18.2, "actual_temperature_c": -18.3, "setpoint_c": -18.0,
  "temp_log_high_c": -15.0, "temp_log_low_c": -20.0, "resolution_rs": 0,
  "compressor_on": true, "alarm_on": false, "defrost_active": false, "keypad_locked": false,
  "fault_flags": 0, "faults": [], "fault_descriptions": [], "system_status": 4, "relay_status": 1,
  "compressor_run_hours": 1234, "balanced_defrost_hours": 5, "balanced_defrost_minutes": 30,
  "delays": {"restart_delay": false, "min_on_delay": false, "ht_lt_power_on_delay": false},
  "software_revision": 12, "warnings": [], "poll_duration_ms": 41,
  "raw_registers": {"40001": 7507, "40006": 65354}
}]}
```

Enumerations: `source` = controller|simulated; `comm_status` = ok|timeout|exception|invalid_response|port_error|identity_mismatch|error; `quality` = good|suspect|probe_fault|invalid|comm_error. `temperature_c` must be null when `comm_status` is not `ok`.

Response (200, sent only after the database commit):

```json
{"accepted": ["<uuid>", ...], "duplicates": ["<uuid>", ...], "rejected": [{"id": "<uuid>", "error": "temperature_c must be ..."}]}
```

`accepted` and `duplicates` both mean the reading is durably stored. `rejected` records are permanently invalid; the node keeps them locally with status `rejected` and stops sending them.

## POST /events

`{"node_id": "...", "events": [{"event_id": "<uuid>", "event_type": "comm_lost", "severity": "error", "message": "...", "controller_id": "ctrl-1", "occurred_at": "...Z", "details": {...}}]}`. The response has the same shape as for readings.

Event types the node sends: `node_started`, `modbus_error`, `comm_lost`, `comm_restored`, `server_connection_restored` (outage start/end, failed attempts, backlog), `data_gap` (buffer full or node not running, with from/to/records_lost), `parameter_snapshot`, `config_applied`, `config_rejected`, `controller_command`.

## POST /heartbeat

The node sends its health: `software_version`, `node_time`, `uptime_s`, `config_version`, `config_error`, `simulation`, `clock_synchronized`, `queue`, `upload`, `config_sync`, `controllers` (per controller: last success/failure, consecutive failures, comm state), `recent_errors`, `system` (hostname, CPU temperature, disk, memory, local IP, Wi-Fi signal) and `effective_config` (secrets redacted).

Response: `{"server_time": "...Z", "config_version": 4, "pending_commands": 0}`. A `config_version` different from the node's tells it to fetch configuration.

## GET /config?current_version=N
`204` when nothing newer exists, otherwise `{"version": 4, "config": {...remote overlay...}, "created_at": "...Z"}` (the version is then marked `delivered`). The overlay may contain only `node` (descriptive fields), `server` (url, intervals, batch size, timeouts, backoff), `buses`, `controllers`, `buffer` (retention, limits, full behaviour) and `logging` (level, diagnostic registers).

## POST /config/ack
`{"version": 4, "status": "applied" | "rejected", "errors": ["..."], "ignored_keys": ["server.token"]}`. The ack is audited. If an ack is lost, the next heartbeat reporting `config_version = 4` also marks the version applied.

## GET /commands, POST /commands/{command_id}/result
`GET` returns pending, unexpired commands (`command_id`, `controller_id`, `command`, `expires_at`, `requested_by`) and marks them `sent`. The result body is `{"status": "succeeded" | "failed" | "rejected" | "expired", "result": {...}}`. Commands: `identity_check`, `read_parameters` (read-only) and `alarm_mute`, `fault_reset`, `temp_log_reset`, `defrost_start`, `defrost_stop` (coil writes, which need local enablement on the node).

## Browser JSON (session-authenticated, not for nodes)
`GET /ui-api/dashboard` (dashboard data) and `GET /ui-api/series?controller=ID&range=1h|6h|24h|7d|30d|custom&start=&end=` (chart, statistics, gaps, recent readings).
