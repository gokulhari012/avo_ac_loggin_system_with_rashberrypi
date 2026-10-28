# Temperature Monitoring and Data Logging System

Industrial temperature monitoring for **PVR Controls SZ-7526TX-REA** controllers. Raspberry Pi nodes read the controllers over **RS-485 / Modbus RTU**, buffer the readings durably, and forward them to a **Windows Flask server**. The server provides live dashboards, history, node management, Excel/PDF exports and an append-only audit trail.

```
 SZ-7526TX-REA ──RS-485── Raspberry Pi node ──Wi-Fi/LAN──► Windows server (Flask + Waitress) ──► SQLite / PostgreSQL
                         (polls, buffers in SQLite,         (API, dashboard, reports, audit)
                          uploads when online)                        ▲
                                                                 web browser
```

## Highlights
- **Datasheet-driven Modbus layer.** Addresses, types, rS scaling, the 1001/1002/1007 sentinels and status bits come from the register map. The addressing convention is checked on every poll against the identity registers (40001 = 7507, 40003 = 115). Reads stay within the 50-register limit and on one bus never overlap. Writes are never part of polling. [Register map](docs/modbus-register-map.md)
- **No data loss through outages.** Every poll, including failed ones, is committed to a local SQLite queue (WAL, fsync) and deleted only after the server confirms durable storage. Retries are idempotent (node-generated UUIDs). Outages and data gaps are reported explicitly.
- **Honest data.** Missing values stay empty, never estimated. Stale, offline, comm-failure, probe-fault and invalid states are each labelled distinctly. Simulated data is badged everywhere.
- **Managed fleet.** Token-authenticated nodes; versioned remote configuration that the node validates, applies or rejects, and acknowledges; health, clock-sync and Wi-Fi metrics; discovery of unregistered devices.
- **Audit-ready.** Append-only audit trail (enforced in the ORM and by database triggers); every export recorded with its filters, record count and SHA-256; eight audit/operational reports labelled recorded, calculated or inferred.
- **Secure by default.** RBAC (Administrator/Operator/Auditor) enforced server-side, CSRF, lockout, strict CSP, secrets never logged. Remote controller commands are off unless enabled on both server and node.

## Quick start

**Windows server** (details: [installation-windows.md](docs/installation-windows.md))

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Lan        # venv, .env, database, first admin, firewall
powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1              # then open http://<server-ip>:8000
powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1    # (Administrator) start at boot
```

**Raspberry Pi node** (details: [installation-raspberry-pi.md](docs/installation-raspberry-pi.md))
1. On the server: **Nodes → Register node** and copy the one-time token.
2. On the Pi: `sudo bash scripts/setup_raspberry_pi.sh`, then edit `/etc/temperature-node/config.yaml` (node ID, server URL, serial settings) and `/etc/temperature-node/node.env` (token).
3. Run the read-only [hardware validation](docs/hardware-validation.md), then `sudo systemctl restart temperature-node`.

**Try it without hardware** (after the server is running):

```powershell
raspberry_pi_node\.venv\Scripts\python simulator\simulated_node.py --server http://127.0.0.1:8000 --register --admin admin --nodes 2 --controllers 2
```

(Create the node virtualenv first: `cd raspberry_pi_node; py -3 -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt`.)

## Repository layout

```
raspberry_pi_node/        edge application (independent of the server)
  src/                    registers, decoder, modbus_client, collector, local_queue, uploader, server_client,
                          config_manager, config_sync, health_monitor, commands, logging_config, main,
                          validate_controller (commissioning tool)
  tests/                  113 tests (simulated controller, fake server)
  config.example.yaml, temperature-node.service, requirements*.txt
server/                   central server (Flask)
  app/                    models/, routes/, services/, templates/, static/ (Bootstrap, Chart.js vendored)
  migrations/             Alembic (Flask-Migrate) incl. append-only audit triggers
  tests/                  117 tests
  run.py, .env.example, requirements*.txt
simulator/                mock_controller.py (RTU slave over TCP/serial), simulated_node.py, server_ui.py
tests_e2e/                end-to-end acceptance test (real server process + node + simulator)
scripts/                  setup_windows.ps1, start/stop/diagnose_server.ps1, install_windows_service.ps1,
                          setup_raspberry_pi.sh, create_admin.py, backup_database.py
docs/                     see below
```

## Documentation
| Document | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Components, data path, schema, security model, design decisions |
| [modbus-register-map.md](docs/modbus-register-map.md) | Registers, scaling, bits, quality rules, write policy, **datasheet ambiguities** |
| [hardware-validation.md](docs/hardware-validation.md) | Commissioning procedure and record sheet |
| [installation-windows.md](docs/installation-windows.md) | Server install, LAN/firewall, start at boot, HTTPS, backups, upgrades |
| [installation-raspberry-pi.md](docs/installation-raspberry-pi.md) | First node, adding nodes, operations |
| [configuration-guide.md](docs/configuration-guide.md) | Node, server and system settings; remote config lifecycle; roles |
| [api.md](docs/api.md) | Node API reference |
| [troubleshooting.md](docs/troubleshooting.md) | Serial, Wi-Fi, firewall, authentication, Windows startup, data questions |
| [testing-guide.md](docs/testing-guide.md) | Running tests, coverage, simulator |
| [development-log.md](docs/development-log.md) | Phase summaries, verification results, **outstanding issues** |

## Tests

```powershell
cd raspberry_pi_node; .venv\Scripts\python -m pytest      # 113 passed
cd server; .venv\Scripts\python -m pytest                 # 117 passed
raspberry_pi_node\.venv\Scripts\python -m pytest tests_e2e  # 1 passed (full system incl. server outage)
```

## Before production
This software has been verified against the documents and a simulator, **not yet against a physical SZ-7526TX-REA or on Raspberry Pi hardware**. Complete [hardware-validation.md](docs/hardware-validation.md) for each controller and read the limitations in [development-log.md](docs/development-log.md#outstanding-issues-and-limitations-not-verified-in-this-environment). The system does not claim compliance with any regulatory standard.
