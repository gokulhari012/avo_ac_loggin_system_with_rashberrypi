# Installing the server on Windows

Tested on Windows 11 with Python 3.13. Python 3.10 or newer is required.

## 1. Prerequisites
- Install **Python 3.11+** from python.org with *"Add python.exe to PATH"* and the *py launcher* ticked.
- Copy the project folder to its permanent location, e.g. `C:\TemperatureMonitoring\`. Keep the path short and readable by the account that will run the server.
- Give the PC a **fixed IP address** (or a DHCP reservation) so nodes can always find it.

## 2. Install

In PowerShell, from the project folder:

```powershell
# Local only (the browser on this PC; nodes cannot connect yet):
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

# LAN deployment (needed for Raspberry Pi nodes). Run PowerShell as Administrator for the firewall rule:
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Lan -Port 8000
```

The script:
1. creates `server\.venv` and installs the pinned packages from `server\requirements.txt`
2. creates `server\.env` from `.env.example` with a random `SECRET_KEY`, restricted to the current user, Administrators and SYSTEM
3. creates or upgrades the database (`server\instance\temperature.db`) and seeds the roles
4. asks you to create the first **Administrator**
5. with `-Lan`: sets `HOST=0.0.0.0` and adds an inbound firewall rule for TCP 8000 on Domain/Private networks

Review `server\.env` afterwards (time zone, session timeout, limits). See the [configuration guide](configuration-guide.md).

## 3. Local-only vs LAN access

| Mode | `HOST` | Who can connect |
|---|---|---|
| Local only | `127.0.0.1` | browsers on this PC; **nodes cannot connect** |
| LAN | `0.0.0.0` | any device that can reach this PC on `PORT` (subject to the firewall) |

If Windows marks the network as **Public**, the firewall rule (Domain/Private) does not apply. Set the network to Private (Settings → Network → properties), or add `-Profile Public` to the rule only if you understand the exposure. Do not expose the server to the internet. Remote sites should connect through a VPN.

## 4. Start, stop, run at boot

Windows blocks `.ps1` scripts by default ("running scripts is disabled on this system"), so run each one through `powershell -ExecutionPolicy Bypass -File`. This allows that single run only and does not change any system setting.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1             # foreground (Ctrl+C to stop) - good for the first test
powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1  # (Administrator) start at boot as SYSTEM, auto-restart every minute if it exits
powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1              # stop the task / process
powershell -ExecutionPolicy Bypass -File scripts\diagnose_server.ps1          # checks venv, .env, port, API ping, firewall, network profile, disk, last log lines
powershell -ExecutionPolicy Bypass -File scripts\install_windows_service.ps1 -Uninstall
```

The server uses **Waitress** (a production WSGI server), never Flask's development server. Logs are written to `server\logs\server.log` (rotating). Open `http://<server-ip>:8000/` and sign in.

**Alternative: a real Windows service with NSSM.** Download `nssm.exe` (nssm.cc), then:
`nssm install TemperatureMonitoring "C:\TemperatureMonitoring\server\.venv\Scripts\python.exe" run.py`,
set *Startup directory* to `...\server`, and `nssm start TemperatureMonitoring`.
Use either NSSM or the scheduled task, not both.

## 5. HTTPS (recommended on shared networks)

Waitress does not terminate TLS, so put a reverse proxy in front of it. With [Caddy](https://caddyserver.com) (a single exe):

```
# Caddyfile
https://tempserver.plant.local {
    tls internal                     # or your company certificate: tls cert.pem key.pem
    reverse_proxy 127.0.0.1:8000
}
```

Then in `server\.env`: `HOST=127.0.0.1`, `TRUST_PROXY=true`, `TRUSTED_PROXY=127.0.0.1`, `SESSION_COOKIE_SECURE=true`. Open TCP 443 instead of 8000 in the firewall. On each node, set `server.url: https://tempserver.plant.local`. For `tls internal`, copy Caddy's root certificate to the Pi and set `server.ca_bundle: /etc/temperature-node/ca.crt`.

## 6. Users
Create more users under **Administration → Users** (roles: Administrator, Operator, Auditor), or run `server\.venv\Scripts\python scripts\create_admin.py --username alice --role Operator`. Users are deactivated, never deleted, so the audit trail stays attributable.

## 7. Backups

```powershell
server\.venv\Scripts\python scripts\backup_database.py --dest D:\Backups\Temperature --keep 30
```

This takes a consistent online copy (SQLite backup API, safe while running), checks its integrity, and keeps the newest N copies. Schedule it daily in Task Scheduler (*Action:* the command above, *Start in:* the project folder). Store backups off the PC.

## 8. Upgrading
1. Back up the database.
2. `scripts\stop_server.ps1`.
3. Replace the program files (keep `server\.env`, `server\instance\`, `server\logs\`).
4. `server\.venv\Scripts\python -m pip install -r server\requirements.txt`.
5. Start the server. Migrations run automatically (`AUTO_MIGRATE=true`), or run `flask --app run db upgrade` in `server\`.

## 9. PostgreSQL (optional)
Install PostgreSQL, create a database and user, `pip install "psycopg[binary]==3.2.3"`, set `DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/tempmon`, then run `flask --app run db upgrade`. The migration installs the append-only audit trigger for PostgreSQL as well.
