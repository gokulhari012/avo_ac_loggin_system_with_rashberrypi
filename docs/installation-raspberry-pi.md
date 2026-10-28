# Installing a Raspberry Pi node

Supported: Raspberry Pi 3B+/4/5/Zero 2 W with **Raspberry Pi OS Bookworm or newer** (Python ≥ 3.10; pymodbus 3.11 does not support older Python versions). A wired network is more reliable than Wi-Fi where possible.

## Hardware
- **RS-485 interface:** a USB-RS485 adapter (FTDI or CH340 based, with automatic direction control) is the simplest option and appears as `/dev/ttyUSB0`. An RS-485 HAT on the GPIO UART appears as `/dev/serial0`; enable it with `raspi-config` → Interface Options → Serial Port (login shell **No**, hardware **Yes**).
- Wire A/B to the controller per its wiring diagram. See [hardware-validation.md](hardware-validation.md) for termination and checks.
- Use a good power supply and a quality SD card (or USB SSD). The local buffer uses durable writes, but cheap cards and power cuts are the most common cause of Pi failures.

## Deploying the first node

### 1. Prepare the Pi
1. Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager. In its settings, set a hostname (e.g. `tempnode-coldroom1`), enable SSH, and enter the Wi-Fi SSID/password and your country.
2. Boot, SSH in, and update: `sudo apt update && sudo apt full-upgrade -y`.
3. Give the Pi a DHCP reservation on the router (easier support).
4. Turn off Wi-Fi power saving (it causes dropouts):
   `sudo nmcli connection modify "$(nmcli -t -f NAME connection show --active | head -1)" 802-11-wireless.powersave 2`.
5. Check the time is synchronised: `timedatectl` should show `System clock synchronized: yes`.

### 2. Register the node on the server
In the web UI: **Nodes → Register node**. Enter a node ID (e.g. `NODE-001`), name, site and equipment, the serial port (`/dev/ttyUSB0`), and baud/parity matching the controller's `bd`/`PAr`. Add one controller row with its slave ID (`did`). Save, then **copy the token**: it is shown only once.

### 3. Install the software
Copy the project to the Pi (e.g. `git clone`, or `scp -r` the folder), then:

```bash
cd ~/temperature-logging-system          # the project folder
sudo bash scripts/setup_raspberry_pi.sh
```

This creates the `tempnode` system user (member of `dialout`), `/opt/temperature-node` with a virtualenv, `/etc/temperature-node/config.yaml` and `node.env`, `/var/lib/temperature-node` (buffer) and `/var/log/temperature-node`, and installs and enables the `temperature-node` systemd service.

### 4. Configure

```bash
sudo nano /etc/temperature-node/config.yaml     # node.id = the ID registered on the server; server.url = http://<server-ip>:8000; port/baud/parity/slave_id
sudo nano /etc/temperature-node/node.env        # TEMP_NODE_TOKEN=<token from step 2>
cd /opt/temperature-node/raspberry_pi_node
sudo -u tempnode env $(sudo grep TEMP_NODE_TOKEN /etc/temperature-node/node.env) /opt/temperature-node/venv/bin/python -m src.main --config /etc/temperature-node/config.yaml --check-config
```

A minimal `config.yaml` (identity + server) is enough: the node then waits for its configuration from the server. Normally you also fill in `buses`/`controllers` so data collection starts even before the server is reachable.

### 5. Validate against the controller
Follow [hardware-validation.md](hardware-validation.md). At minimum, run the read-only tool and compare the temperature with the display, including a negative value.

### 6. Start and verify

```bash
sudo systemctl restart temperature-node
journalctl -u temperature-node -f                # live log; also /var/log/temperature-node/node.log
systemctl status temperature-node
```

On the server, the node should show **Online**, configuration *v1 applied*, and the controller **Normal** with live temperatures (no SIMULATED badge).

## Adding further nodes
Repeat steps 1–6 with a new node ID per Pi. Several controllers on one RS-485 line: give each a unique `did` (slave ID) on the controller, with the same baud/parity, and add one controller row per slave ID. A node can also drive several RS-485 adapters: use the *Advanced JSON* section of the node configuration page (one `buses` entry per port).

Unregistered Pis that try to connect appear under **Nodes → Unregistered devices**, with a *Register…* shortcut. Their data is not accepted until they are registered and given a token.

## Operations
| Task | Command |
|---|---|
| Status | `systemctl status temperature-node` |
| Logs | `journalctl -u temperature-node --since "1 hour ago"` |
| Restart | `sudo systemctl restart temperature-node` |
| Buffered (unsent) readings | `sqlite3 /var/lib/temperature-node/queue.db "select status, count(*) from readings group by status"` |
| Active remote configuration | `cat /var/lib/temperature-node/remote_config.json` |
| Upgrade | copy the new project version, then `sudo bash scripts/setup_raspberry_pi.sh` (config and buffer are kept) |
| Token rotated on server | update `TEMP_NODE_TOKEN` in `/etc/temperature-node/node.env`, restart. Data is buffered in the meantime. |

The service runs as `tempnode` with `ProtectSystem=strict`. It can only write its state and log directories. systemd restarts it 5 s after any exit, or when the 120 s watchdog is not fed.
