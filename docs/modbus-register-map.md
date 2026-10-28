# Modbus register map – PVR Controls SZ-7526TX-REA

**Sources:** *Product Register Map for SZ-7526TX-REA* (PVR/DSG/PRM, Issue 01, Rev 00, 24-11-2023) and the *SZ-7526TX Instruction Manual* (REV3, 23-11-2023).
**Implementation:** [`raspberry_pi_node/src/registers.py`](../raspberry_pi_node/src/registers.py) (definitions), [`decoder.py`](../raspberry_pi_node/src/decoder.py) (decoding rules), [`modbus_client.py`](../raspberry_pi_node/src/modbus_client.py) (transport).

Nothing below goes beyond what the two documents state. Each place where the documents are silent or inconsistent is listed in [Ambiguities](#ambiguities-and-how-they-are-handled), together with what the software does and how to check it on real hardware.

## Communication settings

| Item | Register map | Software |
|---|---|---|
| Physical port | TTL port compatible with an RS-485 slave | A USB-RS485 adapter or RS-485 HAT on the Pi |
| Protocol | Modbus RTU, half duplex, asynchronous | pymodbus 3.11.3 `ModbusSerialClient`, RTU framer |
| Framing | 1 start bit, 8 data bits, 1 stop bit; parity None/Even/Odd | `bytesize: 8` and `stopbits: 1` are enforced by validation; parity `N`/`E`/`O` |
| Baud rate | settable: 1 = 9600, 2 = 19200, 3 = 38400 | `9600`, `19200` or `38400` |
| Device ID | settable, Disable (0) to 250 | `slave_id` 1–250 (0 disables communication on the controller) |
| Function codes | 01, 03, 05, 06, 16 | 03 for polling; 01 for coil reads (diagnostics); 05 only for guarded commands; 06/16 never used |
| Limit | "At a time only 50 registers are accessible" | Every read is ≤ 50 registers. The client refuses a larger request before sending it. |

**Initial values versus real settings.** The instruction manual lists the factory setting of `did`, `bd` and `PAr` as "–": **no factory default is documented**. The software defaults (slave 1, 9600 baud, no parity) are only a starting point. The real values must be read from the controller's Program Mode (parameters `did`, `bd`, `PAr`) or found with `validate_controller --scan`. The validation tool reads them back from registers 40054–40056 and flags any mismatch.

## Addressing

pymodbus sends the zero-based **PDU address**. The register map uses Modicon reference numbers.

| Datasheet | PDU address sent | Example |
|---|---|---|
| Holding register 4xxxx | 4xxxx − 40001 | 40006 → 5 |
| Coil n | n − 1 | coil 5 → 4 |

The register map does not say which convention the controller uses, so the node **verifies it on every poll**. Register 40001 must read **7507** ("PVR Key") and 40003 must read **115** ("Controller Identification Code"). If they do not, the poll is reported as `identity_mismatch` and no temperature is published. An off-by-one in either direction shifts these values (or reads past the documented block), so a wrong convention can never produce plausible-looking data. A per-controller `register_overrides.address_offset` (−1/0/+1) exists for the case where commissioning proves the vendor uses a different base. Change it only on that evidence.

## Read plan

| Read | Registers | Count | Function | When |
|---|---|---|---|---|
| Status block | 40001–40020 | 20 | 03 | every poll (`poll_interval_s`, default 10 s) |
| Status segments (fallback) | 40001–40006, 40008, 40010–40012, 40014, 40016–40020 | 6+1+3+1+5 | 03 | if the controller rejects the block with exception 2 (illegal address) because of the "NA" registers |
| Parameters | 40031–40068 | 38 | 03 | every `parameter_read_interval_s` (default 1 h), and on request |
| Coils | 1–5 | 5 | 01 | validation tool and diagnostics only |

`register_overrides.read_strategy` can force `block` or `segments`. The default `auto` tries the block and switches to segments permanently (for that controller) after an illegal-address exception.

## Read-only registers 40001–40020

| Ref | Name | Type | Decoding |
|---|---|---|---|
| 40001 | PVR Key | uint16 | identity check, must equal 7507 |
| 40002 | Software Revision | uint16 | reported as-is |
| 40003 | Controller Identification Code | uint16 | identity check, must equal 115 |
| 40004 | Device (Slave) ID | int16 | cross-check, should equal the polled slave ID (`device_id_mismatch` otherwise) |
| 40005 | rS | int16 | 0 → ×0.1 °C, 1 → ×1 °C, anything else → `unknown_resolution` (temperatures withheld) |
| 40006 | Control Temperature (display temperature) | int16 | signed, × rS factor. **1001 = probe fail low, 1002 = probe fail high** (compared on the raw value before scaling). This is the primary `temperature_c`. |
| 40007 | NA | – | read inside the block, never interpreted |
| 40008 | Actual Temperature | *not specified* | assumed int16 × rS factor (see ambiguity A2). Stored as `actual_temperature_c`. |
| 40009 | NA | – | – |
| 40010 | System Status / Delay Status | uint16 bitfield | see below |
| 40011 | Relay status | "Unsigned Char" | bit0 compressor relay, bit1 alarm relay |
| 40012 | Fault status | "Unsigned Char" | see below |
| 40013 | NA | – | – |
| 40014 | Compressor Run Hours | uint16 | hours |
| 40015 | NA | – | – |
| 40016 | P8H balanced defrost time (hours) | uint16 | as-is |
| 40017 | P8m balanced defrost time (minutes) | uint16 | as-is |
| 40018 | Set point (read only) | int16 | × rS factor |
| 40019 | Temp Log High | int16 | × rS factor. **1007 = log reset** (value null). |
| 40020 | Temp Log Low | int16 | × rS factor. **1007 = log reset** (value null). |

Measuring range (manual): −45.0 °C to 100.0 °C. A control temperature outside it is withheld with quality `invalid` / flag `out_of_range`.

### 40010 System status bits

| Bit | Meaning | Note |
|---|---|---|
| 0 | POWER OFF | documented as "Extra" |
| 1 | Keypad lock | 1 = locked |
| 2 | Resolution_1 | **1 = 0.1 °C, 0 = 1 °C** (opposite polarity to rS) |
| 3 | Resolution_2 | "Extra" |
| 4 | Temperature unit selection | "Extra" |
| 5 | Defrost | 1 = defrost on |
| 6, 7 | NA | – |
| 8 | Compressor restart time delay (P6) in progress | 1 = in progress |
| 9 | Compressor minimum ON time delay (ot) in progress | 1 = in progress |
| 10 | Ht/Lt power-on delay (Adt) in progress | 1 = in progress |

### 40011 Relay status bits
bit0 = compressor relay, bit1 = alarm relay (1 = ON).

### 40012 Fault status bits (1 = fault present)

| Bit | Code used by the software | Fault |
|---|---|---|
| 0 | `probe_fail_low` | Control probe fail low (PFL) |
| 1 | `probe_fail_high` | Control probe fail high (PFH) |
| 2 | `high_temp` | High temperature (Ht) |
| 3 | `low_temp` | Low temperature (Lt) |
| 4 | `door_open` | Door open (do) |
| 5 | `compressor_fault` | Compressor fault (CF) |
| 6 | `manual_compressor_fault` | Compressor fault awaiting manual reset |

Bits 7–15 are undocumented. If any is set, the flag `undocumented_fault_bits` is raised and the raw value is kept.

## Data quality rules

| Situation | `comm_status` | `quality` | Temperature |
|---|---|---|---|
| Normal | ok | good | value |
| rS and 40010 bit2 disagree, device ID ≠ slave ID, or node clock not synchronised | ok | suspect | value (scaled per rS) |
| 40006 = 1001/1002, or fault bit 0/1 set | ok | probe_fault | null |
| Out of the measuring range, undocumented rS, or identity check failed | ok / identity_mismatch | invalid | null |
| No/invalid reply, Modbus exception, or port error | timeout / exception / invalid_response / port_error / error | comm_error | null |

Values are never estimated or carried forward: a failed poll is stored as a record with null values.

## Parameters 40031–40068 (read-only for this software)

SP, P1, P2, P2d, P3, P3d, HS, LS, P4, P5, P6, ot, E1, Cn, Cy, P7, P8, AF0, CF1/do1, CF2/do2, CF3, CF4, do3, did, bd, Par, Ccr, ddF, nd, dd, AL, Adt, Add, rS, PA, LP, Ver, FS. Descriptions, units and option labels come from the instruction manual and are listed in `registers.PARAMETERS`. Temperature parameters (SP, P2, P2d, P3, P3d, HS, LS, P4, P5; register map note 3) are scaled by rS.

**PA (40065, keypad password) is never transmitted, stored or logged.**

Snapshots are sent to the server only when a value changes. The server records them in the controller configuration history and audits each detected change as "parameter change detected on device".

**Why parameters are never written remotely.** The parameters have interdependent limits (SP within LS+1…HS−1, P2 ≥ P3+1, and so on) and "can't be modified" conditions (e1/P1/P7/AF0/CF2/CF3/did/AL). `LP = 1` locks writes. `FS` restores factory defaults. Changing `did`/`bd`/`Par` would cut communication. The register map lists them as R/W, but a remote write path would add risk with little benefit. Change parameters on the controller keypad; the audit trail will record the change from the next snapshot.

## Coils (function 01 read / 05 write)

| Coil | Function (register map) | Software use |
|---|---|---|
| 1 | Power – "Extra" | **never written** (function undocumented) |
| 2 | Alarm relay mute – reads 1 while alarm relay is ON; write 0 to mute | command `alarm_mute` |
| 3 | Manual fault reset – reads 1 while a manual-reset fault is present; write 0 to reset | command `fault_reset` |
| 4 | Temp log reset – always 0; write 1 to reset | command `temp_log_reset` |
| 5 | Defrost – write 1 to start, 0 to stop | commands `defrost_start` / `defrost_stop` |

Writes are **disabled by default** and need all four of the following:
1. `safety.allow_remote_control: true` in the node's **local** config file. The server cannot set this.
2. The server setting *Allow controller commands*.
3. The `control_controllers` permission.
4. A typed confirmation and a reason.

Writes are never retried automatically and are always audited. Polling never writes.

## Ambiguities and how they are handled

| # | Ambiguity in the documents | Handling | How to validate on hardware |
|---|---|---|---|
| A1 | Zero- vs one-based addressing not stated | Zero-based, verified on every poll via 7507/115 | `validate_controller` reports PASS/FAIL for addressing and tries offsets ±1 if it fails |
| A2 | 40008 "Actual Temperature" has no data type or scaling, and is not in note 2 | Decoded as int16 × rS. Stored separately; never used as the primary temperature. Values outside the measuring range are withheld. | Compare 40008 with the display in normal operation and during door-open hold (`do3`) or display delay (`dd`) |
| A3 | Reading "NA" registers inside a block | Block read first; automatic fallback to documented segments on exception 2 | The tool reports which strategy worked |
| A4 | 40011/40012 are "Unsigned Char" in a 16-bit register | Bits read from the low byte; a non-zero high byte is flagged `unexpected_high_byte` | Raw values are shown by the tool |
| A5 | 40010 bit2 uses the opposite polarity to rS | rS is authoritative (notes 2 and 3); disagreement → quality `suspect` | Tool cross-checks rS against bit2 |
| A6 | Sentinels 1001/1002 documented only for 40006 | Applied only to 40006. 40008 relies on the range check. | Disconnect the probe on a bench unit: 40006 should read 1001 and fault bit0 should be set |
| A7 | No factory defaults for did/bd/PAr | Software defaults are labelled as such everywhere | Read the parameters on the keypad; tool checks bd/PAr/did |
| A8 | Manual mentions "9600 to 57600 bps" but lists only 1–3 | Only 9600/19200/38400 are accepted | – |
| A9 | 1 stop bit even without parity (the Modbus spec suggests 2) | 1 stop bit, as documented | Communication works at the configured parity |
| A10 | Coil 1 "Power" documented only as "Extra" | Never written | – |
| A11 | Bit4 "Temperature unit selection" is "Extra"; the manual describes °C only | All values treated as °C; raw status kept | – |
| A12 | CRC errors are not distinguishable from silence in pymodbus (bad frames are discarded) | Both reported as `timeout`, with detail text | Tested with a simulator that corrupts CRCs |
