# Troubleshooting

Start with the tools: `scripts\diagnose_server.ps1` on Windows, and `journalctl -u temperature-node -n 100` on the Pi. The node page on the server shows the node's own view (heartbeat, per-controller communication state, recent errors, configuration errors).

## Serial / RS-485 / controller

| Symptom | Likely cause | Check / fix |
|---|---|---|
| `port_error: cannot open serial port /dev/ttyUSB0` | adapter missing or renamed; permissions | `ls -l /dev/serial/by-id/`, then use that stable path. The service user must be in `dialout` (the installer does this). Stop other programs using the port (only one user at a time). |
| `timeout: no valid response` on every poll | wiring, A/B swapped, wrong slave ID/baud/parity, `did = 0`, no power | Stop the service and run `python -m src.validate_controller --port ... --scan`. Swap A/B. Check `did`/`bd`/`PAr` on the controller. |
| Intermittent timeouts | noise, missing termination on long cable, loose terminal, too short a timeout | Increase `timeout_s` (1–2 s) and `inter_request_delay_ms` (100). Add termination at the bus ends. Route away from power cables. Note: CRC errors are reported as timeouts. |
| `identity_mismatch` | another device answers at that ID, wrong model, or an addressing offset | Run the validation tool. It reports 40001/40003 and tries offsets ±1. Set `address_offset` only if it finds a match. |
| `exception code 2` once, then fine | controller rejects a block spanning "NA" registers | Expected with `read_strategy: auto`: it switches to segments automatically |
| Quality `suspect` with `resolution_mismatch` | rS and status bit 2 disagree | Report it to the controller vendor. Scaling follows rS as documented. Compare with the display. |
| Quality `suspect` with `clock_unsynchronized` | Pi clock not NTP-synchronised | Check network/NTP: `timedatectl`, `systemctl status systemd-timesyncd` |
| Temperature 10× too large or too small | rS changed on the controller | rS is read on every poll, so values follow automatically. Check 40005 in the raw registers. |
| Shown as probe fault | probe disconnected or damaged (display shows PFL/PFH) | Check the probe. The system deliberately shows no temperature in this case. |

## Wi-Fi / network

| Symptom | Fix |
|---|---|
| Node goes offline periodically | Disable Wi-Fi power saving (see the installation guide). Check the Wi-Fi signal on the node page (aim for better than −70 dBm). Consider Ethernet. |
| `server unreachable, buffering locally` in the node log | Data is safe in the buffer. Check `server.url`, then `curl http://<server>:8000/api/v1/ping` from the Pi, then the server firewall. |
| Node can ping the server PC but not reach the API | Windows firewall rule missing, or network profile is *Public* (run `diagnose_server.ps1`), or the server bound to `127.0.0.1`: set `HOST=0.0.0.0` |
| Server IP changed | Use a DHCP reservation or fixed IP. Update `server.url` locally or push a new URL from the server (the node tests it before switching). |

## Authentication / registration

| Symptom | Fix |
|---|---|
| Node log: `server rejected node credentials` | Wrong token or node ID. On the server the node shows *auth failures*. Install the correct token, or issue a new one (old tokens stop working immediately). |
| Node log: `server refused node: this node has been disabled` | Enable it on the node page. Data is still buffered on the Pi. |
| Node appears under *Unregistered devices* | Register it with exactly that node ID |
| Configuration stays *delivered*, never *applied* | The node rejected it (see *Node errors* on the version page), or it has not reconnected. Typical cause: a serial port that does not exist on the Pi. |

## Windows server

| Symptom | Fix |
|---|---|
| Server does not start at boot | Task Scheduler → *TemperatureMonitoringServer* → History. Run `scripts\start_server.ps1` manually to see the error. Check `server\logs\server.log`. |
| `Address already in use` | Another program uses the port (`Get-NetTCPConnection -LocalPort 8000`). Change `PORT`. |
| Pages show "Internal error (reference …)" | Find that request ID in `server\logs\server.log` |
| `database is locked` warnings | Many simultaneous large exports on SQLite. They retry for up to 10 s. For heavy use, move to PostgreSQL. |
| Export refused: "N records match, above the limit" | Narrow the date range or filters, or use Excel (higher limit). Exports are never silently truncated. |
| Times look shifted | Check *Administration → System settings → Display time zone*. Data is stored in UTC. A node with a wrong clock shows *clock skew* on its page. |

## Data questions

- **Gaps in charts.** A line break means no reading attempts were recorded: the node was not running or its buffer overflowed (a `data_gap` event explains which). Failed polls are shown as grey crosses. The *Missing data* report lists both.
- **"buffered" badge.** The reading was delivered later than `BACKFILL_THRESHOLD_S` after acquisition, during an outage. Its timestamp is the original acquisition time.
- **SIMULATED badge.** The node runs with `simulation: true` (simulator). Never use such data as measurements.
