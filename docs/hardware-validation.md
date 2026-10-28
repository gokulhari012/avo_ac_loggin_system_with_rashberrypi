# Hardware validation and commissioning procedure

Do this **before** a node's data is relied on, and again after replacing a controller, adapter or wiring. The validation tool is **read-only**: it never writes to the controller. Record the results in the sheet at the end and keep it with the site's commissioning records.

The tool runs on the Pi:

```bash
sudo systemctl stop temperature-node            # the serial port can only have one user
cd /opt/temperature-node/raspberry_pi_node
sudo -u tempnode /opt/temperature-node/venv/bin/python -m src.validate_controller --port /dev/ttyUSB0 --slave 1 --baud 9600 --parity N
```

## 1. Wiring and adapter

- [ ] RS-485 **A/B (D+/D−)** from the adapter to the controller's TTL/RS-485 terminals, as in the controller's wiring diagram. If nothing answers, swap A and B; this is the most common fault.
- [ ] Common/GND connected if the adapter provides it.
- [ ] 120 Ω termination only at the two physical ends of a long bus. Short single-drop benches usually work without it.
- [ ] Cable kept away from mains and contactor wiring (manual: "Cautions – wiring").
- [ ] The adapter is detected: `dmesg | tail` and `ls -l /dev/serial/by-id/`. Prefer the stable `/dev/serial/by-id/...` path in `config.yaml`.
- [ ] For a GPIO RS-485 HAT on `/dev/serial0`: serial console disabled and serial port enabled (`sudo raspi-config` → Interface Options → Serial Port: login shell **No**, hardware **Yes**).

## 2. Controller communication parameters

On the controller keypad (Program Mode; see the manual "Index"), note:
- [ ] `did` (device ID, **0 disables communication**) = ____
- [ ] `bd` (1 = 9600, 2 = 19200, 3 = 38400) = ____
- [ ] `PAr` (0 = none, 1 = even, 2 = odd) = ____

If unknown, scan (read-only identity reads):

```bash
python -m src.validate_controller --port /dev/ttyUSB0 --scan --scan-ids 1-20
```

## 3. Addressing convention and identity (ambiguity A1)

Run the full validation. Expected:

```
[PASS] addressing: 40001=7507 and 40003=115 at PDU addresses 0 and 2 (zero-based convention)
```

- [ ] PASS. If FAIL, the tool tries offsets −1/+1 and tells you which (if any) works. Set `register_overrides.address_offset` **only** if it reports a match, and record it here.

## 4. Serial settings read back from the controller

- [ ] `device id` PASS (40004 equals the polled slave ID)
- [ ] `baud parameter` PASS, `parity parameter` PASS, `did parameter` PASS

## 5. Resolution and temperature decoding

- [ ] `resolution` PASS, rS = ____ (0 → 0.1 °C, 1 → 1 °C)
- [ ] `resolution bit` PASS (40010 bit2 agrees with rS, ambiguity A5)
- [ ] **Display comparison:** the "Control (display) temperature" printed equals the controller display exactly (same digits, same sign). Repeat with `--watch 10` for several minutes while the temperature changes.
- [ ] **Negative temperature:** with the probe below 0 °C (cold room, or the probe in an ice/salt bath on a bench unit), the printed value is negative and matches the display. This proves signed decoding.
- [ ] **Actual temperature 40008 (ambiguity A2):** record whether it matches the display in steady state: ____. If it does not, note it; the system does not rely on it.
- [ ] (Bench unit only, with approval) Change `rS` and confirm the printed value keeps matching the display at the new resolution. Restore `rS` afterwards.

## 6. Status, relay and fault bits

Observe each while the tool runs with `--watch 5`:
- [ ] Compressor relay bit follows the compressor LED.
- [ ] Defrost: start a manual defrost from the keypad (hold the DEFROST key 2 s). `defrost` flag = 1 and coil 5 agrees (the tool warns if not).
- [ ] Alarm relay bit follows the alarm icon/relay.
- [ ] Keypad lock flag matches the `LP` parameter.
- [ ] (Bench unit) Disconnect the probe: the display shows **PFL**, the tool shows `probe_fault`, 40006 raw = 1001, `probe_fail_low` in faults. Reconnect.
- [ ] (Optional) Ht/Lt: temporarily narrow P2/P3 on a bench unit. The `high_temp`/`low_temp` faults appear and clear according to P2d/P3d.
- [ ] Door/compressor digital input (if AF0 ≠ 0): operate the contact and confirm `door_open` / `compressor_fault`.

## 7. Communication failure behaviour

- [ ] With the node running, unplug the RS-485 cable for about a minute. The server shows "Controller not responding", records `comm_lost`, and stores failed polls with no temperature. Reconnect: `comm_restored` is recorded.
- [ ] Set a wrong slave ID or parity in a test config: polls time out (no data is invented).

## 8. End-to-end

- [ ] Start the service: `sudo systemctl start temperature-node`.
- [ ] Dashboard shows the controller **Normal**, with no SIMULATED badge and a temperature equal to the display.
- [ ] Unplug the Pi's network for 5 minutes, then reconnect. The Data Logs show no gap; readings from the outage are marked "buffered", and the node page shows the reconnection event.
- [ ] Export Excel and PDF for the test period. Values match the display observations above.

## Commissioning record

| Item | Value / result | Initials / date |
|---|---|---|
| Site / equipment | | |
| Node ID / Pi hostname | | |
| Controller serial / software revision (40002) | | |
| Port path | | |
| did / bd / PAr | | |
| Addressing (offset) | | |
| rS | | |
| Display comparison (°C, display vs system) | | |
| Negative temperature check | | |
| 40008 behaviour | | |
| Fault checks done | | |
| Outage test | | |
