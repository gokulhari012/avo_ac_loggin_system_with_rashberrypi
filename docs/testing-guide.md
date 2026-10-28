# Testing guide

Each application has its own virtualenv and test suite. No physical controller is needed: the simulator (`simulator/mock_controller.py`) implements the documented register map as a Modbus RTU slave, with its own CRC and framing, over TCP. The node's real pymodbus RTU stack talks to it through a pyserial `socket://` URL.

## Running the tests

```powershell
# Node (Windows paths; on Linux use .venv/bin/python)
cd raspberry_pi_node
python -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest

# Server
cd server
python -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest

# End-to-end: real server process + real node + simulator (uses both virtualenvs)
cd <project root>
raspberry_pi_node\.venv\Scripts\python -m pytest tests_e2e -v
```

## What is covered

| Area | File | Examples |
|---|---|---|
| Register addressing | `raspberry_pi_node/tests/test_decoder.py` | 40006→5, coil 5→4, 50-register limit, segments cover every documented register |
| Signed/unsigned, negatives, rS scaling | `test_decoder.py` | 65354 → −18.2 °C (rS = 0), −18 °C (rS = 1), no float noise |
| Special values | `test_decoder.py` | 1001/1002 probe fail, 1007 log reset, out of range |
| Status/relay/fault bits | `test_decoder.py` | each documented bit; undocumented bits flagged |
| Modbus over real RTU framing | `test_modbus_client.py` | identity, wrong slave → timeout, CRC-corrupted replies, exception responses, block → segment fallback, address offset caught, writes refused by default, polling never writes |
| Retries/timeouts/invalid responses | `test_modbus_client.py` | scripted failures: retry then success, retries exhausted, permanent exceptions not retried, port errors close the port |
| Local queue | `test_local_queue.py` | FIFO, persistence across restart, idempotent inserts, drop-oldest / stop-collecting with `data_gap` events |
| Configuration | `test_config_manager.py` | serial validation, token from env, bootstrap config, remote overlay cannot touch token/safety/paths, corrupt stored overlay → local fallback |
| Whole node | `test_node_app.py` | normal polling, server down → buffering → backfill without loss, lost ACK → no duplicates, restart while queued, config applied/rejected, comm loss/recovery, commands gated by local safety, bad token, supervisor restarts dead workers |
| Node API | `server/tests/test_api.py` | auth failures/discovery/disabled, validation, size and batch limits, idempotent duplicates, backfill flag, fault transitions, parameter-change detection, heartbeat, config fetch/ack, commands |
| Web/security | `test_web.py` | login/logout/lockout, open redirect, CSRF, headers, cookie flags, session invalidation, RBAC per role, no tracebacks, admin safeguards |
| Node management UI | `test_nodes_ui.py` | registration (token shown once, hash stored), config versions + audit diff, advanced JSON, enable/disable, token rotation, connectivity test, command gating |
| Filters/time zones | `test_filters.py` | display time zone → UTC, every filter, NULL ordering, `as_of` freezing, SQL injection attempt |
| Exports | `test_exports.py` | Excel content/sheets/freeze panes, PDF, identical record sets, empty sets, 30 000-row Excel, PDF limit refusal, SHA-256 + audit |
| Reports/status/audit | `test_reports_status.py` | excursions, missing data, inferred downtime, all 8 reports in both formats, every dashboard status, offline detection, retention, audit immutability (ORM and SQL triggers), secret redaction |
| Acceptance | `tests_e2e/test_end_to_end.py` | the full flow described in [development-log.md](development-log.md) |

## Simulator and demo

```powershell
# 1. Start the server (scripts\start_server.ps1); setup_windows.ps1 already created the first administrator.
# 2. Run two simulated nodes with two controllers each; they register themselves through the web UI:
raspberry_pi_node\.venv\Scripts\python simulator\simulated_node.py --server http://127.0.0.1:8000 --register --admin admin --nodes 2 --controllers 2
```

Options: `--time-scale` (speeds up the thermal model), `--drop-rate 0.05` (5 % unanswered Modbus requests), `--poll`. The `demo` scenario periodically opens the door (raising Ht) and simulates a probe failure. Simulated data is labelled **SIMULATED** throughout the UI and exports.

A stand-alone simulated controller (for example for a bench test of the Pi's RS-485 adapter through a second adapter) is available:

```bash
python simulator/mock_controller.py --serial /dev/ttyUSB1 --slave-ids 1 --baudrate 9600      # RS-485 bench test
python simulator/mock_controller.py --port 5020 --slave-ids 1,2                             # TCP: socket://127.0.0.1:5020
```

**Simulator limits.** It models only documented behaviour plus a crude thermal model. Real-controller behaviour must still be confirmed with [hardware-validation.md](hardware-validation.md).
