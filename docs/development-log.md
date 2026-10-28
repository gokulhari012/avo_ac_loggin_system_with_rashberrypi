# Development log

Built and verified on Windows 11, Python 3.13 (node code also compiled and smoke-tested on Python 3.10). All test counts below come from actual runs.

## Phase 1: Documentation and design
- Read both PDFs. Extracted the register map into `raspberry_pi_node/src/registers.py`: 16 documented read-only registers, 38 parameters, 5 coils, the bitfields of 40010/40011/40012, sentinels 1001/1002/1007, identity values 7507/115, the 50-register limit, and the serial options.
- Documented the scaling rules, special values, status bits, every ambiguity (A1–A12) and a validation step for each in [modbus-register-map.md](modbus-register-map.md) and [hardware-validation.md](hardware-validation.md).
- Architecture and database schema: [architecture.md](architecture.md).

## Phase 2: Modbus communication
- `decoder.py` (pure decoding), `modbus_client.py` (bus lock, explicit retries, error classification, block → segment fallback, guarded writes), and `simulator/mock_controller.py` (an independent RTU slave with its own CRC and framing).
- Probed pymodbus 3.11.3 behaviour first (no response → `ModbusIOException`, unopenable port → `ConnectionException`, exception responses); the client is built on those observations.
- **Tests:** 73 passed (`test_decoder.py`, `test_modbus_client.py`). Fixes during the phase were test-side only (helper kwargs; the offset test now accepts both legitimate outcomes).

## Phase 3: Raspberry Pi node
- Config manager (local + versioned remote overlay, validation, atomic persistence), SQLite queue, uploader (backoff, idempotent acks, batch halving), heartbeat/health, config sync, commands, supervisor with systemd notify/watchdog, validation CLI, systemd unit, example config.
- **Tests:** 113 passed, including whole-node runs against a fake HTTP server. **Bugs found and fixed:** a structurally malformed remote config crashed `normalize()` (now rejected cleanly); a bootstrap config without controllers was refused (now allowed, so the node waits for server config).

## Phase 4: Flask server
- Models (17 tables), Alembic migration with append-only audit triggers (SQLite and PostgreSQL), node API, ingestion with fault-transition tracking, node configuration versioning, auth/RBAC, CLI.
- **Verified:** migration upgrade → downgrade → upgrade; `flask db check` shows no drift; the triggers block UPDATE/DELETE on `audit_events`.
- **Tests:** 30 API tests passed on first run.

## Phase 5: Dashboard
- Dashboard, monitoring (Chart.js, min/max decimation, gaps, fault and failed-poll markers), Data Logs (server-side filters/sort/pagination), node and controller pages. All static assets vendored; strict CSP (no inline scripts).
- Rendered every page from a realistic dataset and **inspected screenshots** (headless Chrome). Fixed: the probe-fault card was styled as stale, browser dates were not ISO, the chart axis did not span the selected range, labels were truncated, and "None" was shown in heartbeat rows. **Bug found:** a heartbeat missing a section crashed the node page (fixed, regression test added).

## Phase 6: Reporting and auditing
- Excel (openpyxl streaming), PDF (ReportLab: numbered pages, repeated headers, chunked tables, trend charts), eight audit/operational reports labelled RECORDED/CALCULATED/INFERRED, and an export audit with SHA-256.
- **Inspected the generated PDFs** as images. Fixed: ReportLab parses 3-digit hex colours wrongly (`#ccc` became blue borders), and the legend colours did not match the lines.

## Phase 7: Testing and deployment
- `tests_e2e/test_end_to_end.py`: a real Waitress server process (migrations, CSRF on) plus the real node plus the simulator. Registration through the web UI → config v1 applied and acknowledged → live readings on the dashboard → **server hard-killed** → node buffers → server restarted → backlog uploaded with no loss and no duplicates → reconnection event → identical Excel/PDF record counts → audit report → expected audit actions. **Passed (≈17 s).**
- Simulator run: 2 simulated nodes × 2 controllers registered themselves through the UI, applied config, and streamed labelled `simulated` data.
- Windows scripts parse-checked with the PowerShell parser; `setup_raspberry_pi.sh` syntax-checked with `bash -n`; backup script run, including integrity check and retention.
- **Bug found while writing the scripts:** `server/.env` was loaded *after* the `Config` class read the environment, so `.env` settings were ignored. Fixed by loading `.env` in `config.py`; verified with a temporary `.env`.

**Final results:** node 113 passed · server 117 passed · end-to-end 1 passed.

## Outstanding issues and limitations (not verified in this environment)
1. **No physical controller or Raspberry Pi was available.** Every protocol assumption is verified against the documents and the simulator only. The hardware validation procedure must be completed on site, especially ambiguity A1 (addressing; checked automatically by the identity registers), A2 (40008 scaling) and A3 (NA registers in a block).
2. `setup_raspberry_pi.sh` and the systemd unit (watchdog, hardening) have not been run on a Pi. `setup_windows.ps1` / `install_windows_service.ps1` were parse-checked but not executed end to end here (the service script needs Administrator rights). Their individual steps (venv, pip, migrations, create-admin, Waitress start) were exercised.
3. PostgreSQL support (the `DATABASE_URL` switch and the PG audit trigger in the migration) is implemented but not tested against a PostgreSQL server.
4. HTTPS is provided by a reverse proxy (documented Caddy setup), not tested here.
5. Alerts are visible dashboard states; there are no e-mail/SMS notifications.
6. Manual data correction is deliberately not supported (readings are immutable), and remote parameter writes are deliberately not implemented.
7. Export files are not stored on the server; only their metadata and SHA-256 are.
8. Very large SQLite databases (tens of millions of rows) will make unfiltered Data Logs counts slower; date filters (default 24 h) keep normal use fast. Use PostgreSQL for large multi-site deployments.
9. Run a single server process (the offline-detection monitor runs inside it).
