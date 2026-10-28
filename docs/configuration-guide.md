# Configuration guide

## Node configuration

The node merges two sources:

1. **Local file** `/etc/temperature-node/config.yaml` (fully commented example: [`config.example.yaml`](../raspberry_pi_node/config.example.yaml)). Only someone with shell access to the Pi can change it.
2. **Remote overlay** pushed by the server as numbered versions, stored in `<state_dir>/remote_config.json`. It may change only the keys marked *remote* below; anything else is ignored and reported back.

| Key | Default | Remote? | Notes |
|---|---|---|---|
| `node.id` | – | no | Unique, must equal the ID registered on the server |
| `node.name`, `description`, `site`, `location`, `equipment` | "" | yes | Descriptive |
| `server.url` | – | yes | A new URL is tested with an authenticated request **before** switching; if that fails the version is rejected |
| `server.token` | – | **no** | Prefer the env var `TEMP_NODE_TOKEN` in `/etc/temperature-node/node.env` |
| `server.verify_tls`, `server.ca_bundle` | true / null | no | For HTTPS with an internal CA |
| `server.request_timeout_s` | 10 | yes | 1–120 |
| `server.upload_interval_s` | 5 | yes | Upload period when there is no backlog (a backlog is drained continuously) |
| `server.batch_size` | 200 | yes | Readings per request, 1–1000 |
| `server.heartbeat_interval_s` | 30 | yes | 5–3600 |
| `server.config_sync_interval_s` | 60 | yes | Also triggered immediately by a heartbeat reply |
| `server.backoff_initial_s` / `backoff_max_s` | 2 / 300 | yes | Exponential backoff with ±20 % jitter |
| `buses[]` | – | yes | `name`, `port`, `baudrate` (9600/19200/38400), `parity` (N/E/O), `bytesize` (8), `stopbits` (1), `timeout_s`, `retries`, `inter_request_delay_ms` |
| `controllers[]` | – | yes | `id`, `name`, `model` (SZ-7526TX-REA), `bus`, `slave_id` (1–250), `enabled`, `poll_interval_s`, `parameter_read_interval_s` (0 = off), `include_raw_registers`, `comm_loss_threshold`, `register_overrides.address_offset` (−1/0/1), `register_overrides.read_strategy` (auto/block/segments) |
| `buffer.path` | /var/lib/temperature-node/queue.db | no | |
| `buffer.retention_days_delivered` | 7 | yes | Delivered records kept locally for inspection |
| `buffer.max_pending_records` | 2 000 000 | yes | About 230 days of one controller at 10 s |
| `buffer.full_behavior` | drop_oldest | yes | `drop_oldest` or `stop_collecting`; both raise a `data_gap` event with the exact period and count |
| `buffer.min_free_disk_mb` | 100 | yes | Below this the buffer is treated as full |
| `logging.level` | INFO | yes | |
| `logging.diagnostic_registers` | false | yes | Logs raw and decoded registers for every poll |
| `logging.file`, `format`, `max_bytes`, `backup_count` | – | no | Rotating file plus journald |
| `safety.allow_remote_control` | **false** | **no** | Allows coil commands from the server |
| `state_dir` | /var/lib/temperature-node | no | |
| `simulation` | false | no | `true` labels every reading `simulated` |

### Remote configuration lifecycle
1. An administrator saves the node configuration form (a reason is required). The server validates it with the same rules as the node and stores it as **version N** (`pending`); older undelivered versions become `superseded`. The change is audited with before/after values.
2. The next heartbeat tells the node that version N exists. The node fetches it (`delivered`).
3. The node validates the full merged configuration. That includes checking that `/dev/...` serial ports exist and testing a new server URL.
   - Valid: the node stops its polling/upload threads, writes the version atomically, restarts with it (buffered data is untouched), and acknowledges `applied`.
   - Invalid: nothing changes, the node keeps the last good version, and acknowledges `rejected` with the error list.
4. The node page shows *running vN / latest vM*, the status and any errors. If an ack is lost, the next heartbeat (which reports the running version) completes the record.

## Server configuration (`server\.env`)
Every key is documented in [`.env.example`](../server/.env.example). The most important:

| Key | Default | Meaning |
|---|---|---|
| `HOST` / `PORT` | 127.0.0.1 / 8000 | Bind address; `0.0.0.0` for LAN |
| `SECRET_KEY` | generated | Session signing key |
| `DATABASE_URL` | SQLite in `server\instance` | or PostgreSQL |
| `DISPLAY_TIMEZONE` | UTC | Initial display time zone (editable in System settings) |
| `SESSION_TIMEOUT_MINUTES` | 30 | Idle timeout |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | 5 / 15 | Account lockout |
| `EXPORT_MAX_ROWS_XLSX` / `EXPORT_MAX_ROWS_PDF` | 500 000 / 20 000 | Larger exports are **refused with a message**, never truncated |
| `BACKFILL_THRESHOLD_S` | 120 | Readings arriving later than this after acquisition are marked *buffered* |
| `ENABLE_REMOTE_CONTROL` | false | Initial value of the command master switch |
| `TRUST_PROXY`, `TRUSTED_PROXY`, `SESSION_COOKIE_SECURE` | off | Behind an HTTPS reverse proxy |

## System settings (Administration → System settings)
Editable at runtime, audited: display time zone, *node offline after* (default 120 s), *reading stale after* (default 120 s; 3 × the poll interval if larger), *data gap factor* (3 × poll interval), dashboard refresh, heartbeat retention (readings and audit events are never purged), and *Allow controller commands*.

## Roles

| Permission | Administrator | Operator | Auditor |
|---|:-:|:-:|:-:|
| view_dashboard, view_data, export_data, view_nodes | ✓ | ✓ | ✓ |
| view_audit | ✓ | | ✓ |
| manage_nodes, control_controllers, manage_users, manage_settings | ✓ | | |

Administrators can change the matrix under *Roles & permissions*. The change is audited, and the last active administrator cannot be removed.
