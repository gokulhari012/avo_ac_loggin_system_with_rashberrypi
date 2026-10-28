"""Hardware validation / commissioning tool.  READ-ONLY: it never writes.

Examples
  python -m src.validate_controller --port /dev/ttyUSB0 --slave 1 --baud 9600 --parity N
  python -m src.validate_controller --port /dev/ttyUSB0 --scan --scan-ids 1-10
  python -m src.validate_controller --port /dev/ttyUSB0 --slave 1 --watch 10

It verifies, against the physical controller:
  1. addressing convention (40001 = 7507 and 40003 = 115 at zero-based offsets),
  2. serial settings (did/bd/PAr parameters read back from the device),
  3. resolution (rS) and its agreement with status bit 2,
  4. temperature decoding - compare the printed value with the display,
  5. fault/relay bits and coil cross-checks.
Follow docs/hardware-validation.md while running it.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time

from . import decoder
from . import registers as R
from .modbus_client import CommError, ControllerClient, SerialBus, SerialSettings

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def report(status: str, check: str, detail: str) -> None:
    results.append((status, check, detail))
    print(f"[{status}] {check}: {detail}")


def make_client(port: str, slave: int, baud: int, parity: str, timeout: float, offset: int = 0) -> ControllerClient:
    bus = SerialBus(SerialSettings(port=port, baudrate=baud, parity=parity, timeout_s=timeout))
    return ControllerClient(bus, slave, retries=1, address_offset=offset, read_strategy="auto")


def scan(port: str, ids: list[int], timeout: float) -> None:
    print(f"Scanning {port}: baud {R.SUPPORTED_BAUDRATES} x parity {R.SUPPORTED_PARITIES} x ids {ids[0]}..{ids[-1]}")
    found = False
    for baud, parity in itertools.product(R.SUPPORTED_BAUDRATES, R.SUPPORTED_PARITIES):
        for slave in ids:
            client = make_client(port, slave, baud, parity, timeout)
            client.retries = 0
            try:
                regs = client.read_holding(40001, 3)
                print(f"  response: baud={baud} parity={parity} slave={slave} -> 40001={regs[40001]} "
                      f"40003={regs[40003]}")
                found = True
            except CommError:
                pass
            finally:
                client.bus.close()
    if not found:
        print("No controller answered.  Check wiring (A/B swapped?), termination, power, and that "
              "parameter 'did' is not 0 (0 disables communication).")


def validate(args) -> int:
    client = make_client(args.port, args.slave, args.baud, args.parity, args.timeout)
    print(f"Validating slave {args.slave} on {args.port} at {args.baud} baud, parity {args.parity}, 8 data, 1 stop")

    # 1. Identity / addressing ---------------------------------------------------
    try:
        ident = client.read_holding(40001, 3)
    except CommError as exc:
        report(FAIL, "communication", f"{exc.status}: {exc}")
        print("Hints: verify baud/parity/slave ID against the controller's bd, PAr and did parameters; "
              "run with --scan; swap RS-485 A/B; check the adapter appears as the given port.")
        return 1
    if ident[40001] == R.PVR_KEY_VALUE and ident[40003] == R.CONTROLLER_ID_CODE:
        report(PASS, "addressing", "40001=7507 and 40003=115 at PDU addresses 0 and 2 (zero-based convention)")
    else:
        report(FAIL, "addressing", f"40001={ident[40001]}, 40003={ident[40003]} (expected 7507 / 115)")
        for offset in (-1, 1):
            try:
                alt = make_client(args.port, args.slave, args.baud, args.parity, args.timeout, offset)
                regs = alt.read_holding(40001, 3)
                if regs[40001] == R.PVR_KEY_VALUE and regs[40003] == R.CONTROLLER_ID_CODE:
                    report(WARN, "addressing", f"identity found with address_offset={offset}; set "
                                               f"register_overrides.address_offset: {offset} for this controller")
            except (CommError, ValueError):
                pass
        return 1

    # 2. Full status block --------------------------------------------------------
    try:
        raw, decoded = client.poll()
    except CommError as exc:
        report(FAIL, "status block", str(exc))
        return 1
    strategy = "segments (controller rejects reads spanning NA registers)" if client._use_segments else "single block"
    report(PASS, "status read", f"40001-40020 read as {strategy}")
    print("\nRaw registers (unsigned as received):")
    for ref in sorted(raw):
        info = R.STATUS_BY_REF.get(ref)
        name = info.name if info else "NA"
        print(f"  {ref}  {raw[ref]:>6}  (signed {decoder.to_int16(raw[ref]):>6})  {name}")

    # 3. Resolution -----------------------------------------------------------------
    if decoded.rs in (0, 1):
        report(PASS, "resolution", f"rS={decoded.rs} -> factor {decoded.resolution_c} degC/count")
    else:
        report(FAIL, "resolution", f"rS={decoded.rs} is undocumented")
    if "resolution_mismatch" in decoded.quality_flags:
        report(WARN, "resolution bit", decoded.warnings[-1])
    elif "resolution_tenths" in decoded.system_flags:
        report(PASS, "resolution bit", "40010 bit2 agrees with rS")

    # 4. Device ID / serial parameters -------------------------------------------
    if decoded.device_id == args.slave:
        report(PASS, "device id", f"40004={decoded.device_id} matches polled slave ID")
    else:
        report(WARN, "device id", f"40004={decoded.device_id} but polled slave {args.slave}")
    try:
        _, params = client.read_parameters(decoded.rs)
        bd = params["bd"]["value"]
        par = params["Par"]["value"]
        expected_bd = {9600: 1, 19200: 2, 38400: 3}[args.baud]
        expected_par = {"N": 0, "E": 1, "O": 2}[args.parity]
        report(PASS if bd == expected_bd else WARN, "baud parameter", f"bd={bd} ({params['bd'].get('label')})")
        report(PASS if par == expected_par else WARN, "parity parameter", f"Par={par} ({params['Par'].get('label')})")
        report(PASS if params["did"]["value"] == args.slave else WARN, "did parameter", f"did={params['did']['value']}")
        print("\nParameters 40031-40068 (PA password redacted):")
        for code, p in params.items():
            value = "***" if p.get("redacted") else p["value"]
            print(f"  {p['reference']}  {code:<8} {value!s:>8} {p['unit']:<5} {p.get('label', '')}  {p['description']}")
    except CommError as exc:
        report(WARN, "parameters", f"could not read 40031-40068: {exc}")

    # 5. Temperature vs display -----------------------------------------------------
    print("\nDecoded values - COMPARE WITH THE CONTROLLER DISPLAY NOW:")
    print(f"  Control (display) temperature : {decoded.temperature_c} degC  [{decoded.control.status}]")
    print(f"  Actual temperature (40008)    : {decoded.actual.value_c} degC  [{decoded.actual.status}] (scaling assumed)")
    print(f"  Set point                     : {decoded.setpoint.value_c} degC")
    print(f"  Log high / low                : {decoded.log_high.value_c} / {decoded.log_low.value_c} degC")
    print(f"  Compressor relay / alarm relay: {decoded.compressor_on} / {decoded.alarm_on}")
    print(f"  Faults                        : {decoded.faults or 'none'}")
    print(f"  System flags                  : { {k: v for k, v in decoded.system_flags.items() if v} }")
    print(f"  Compressor run hours          : {decoded.compressor_run_hours}")

    # 6. Coils cross-check ----------------------------------------------------------
    try:
        coils = client.read_coils()
        report(PASS, "coils read", str(coils))
        if coils["defrost"] != bool(decoded.system_flags.get("defrost")):
            report(WARN, "coil 5 vs 40010 bit5", f"defrost coil={coils['defrost']} status bit={decoded.system_flags.get('defrost')}")
        if coils["alarm_relay_mute"] != bool(decoded.alarm_on):
            report(WARN, "coil 2 vs 40011 bit1", f"alarm coil={coils['alarm_relay_mute']} relay bit={decoded.alarm_on}")
    except CommError as exc:
        report(WARN, "coils read", str(exc))

    for warning in decoded.warnings:
        report(WARN, "decoder", warning)

    if args.watch:
        print(f"\nWatching every {args.watch}s (Ctrl+C to stop).  Compare each line with the display.")
        try:
            while True:
                time.sleep(args.watch)
                try:
                    _, d = client.poll()
                    print(f"  {time.strftime('%H:%M:%S')}  temp={d.temperature_c!s:>6}  sp={d.setpoint.value_c}  "
                          f"comp={int(bool(d.compressor_on))} alarm={int(bool(d.alarm_on))} faults={d.faults} "
                          f"quality={d.quality}")
                except CommError as exc:
                    print(f"  {time.strftime('%H:%M:%S')}  {exc.status}: {exc}")
        except KeyboardInterrupt:
            pass

    client.bus.close()
    failures = sum(1 for r in results if r[0] == FAIL)
    warnings = sum(1 for r in results if r[0] == WARN)
    print(f"\nSummary: {failures} FAIL, {warnings} WARN, {len(results) - failures - warnings} PASS")
    return 1 if failures else 0


def parse_ids(text: str) -> list[int]:
    if "-" in text:
        lo, hi = (int(x) for x in text.split("-", 1))
        return list(range(lo, hi + 1))
    return [int(x) for x in text.split(",")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only SZ-7526TX-REA commissioning checks")
    ap.add_argument("--port", required=True, help="e.g. /dev/ttyUSB0, /dev/serial0, socket://127.0.0.1:5020")
    ap.add_argument("--slave", type=int, default=1)
    ap.add_argument("--baud", type=int, default=9600, choices=R.SUPPORTED_BAUDRATES)
    ap.add_argument("--parity", default="N", choices=R.SUPPORTED_PARITIES)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--scan", action="store_true", help="search baud/parity/slave combinations")
    ap.add_argument("--scan-ids", default="1-10")
    ap.add_argument("--watch", type=float, default=0, help="keep polling every N seconds")
    args = ap.parse_args(argv)
    if args.scan:
        scan(args.port, parse_ids(args.scan_ids), min(args.timeout, 0.3))
        return 0
    return validate(args)


if __name__ == "__main__":
    sys.exit(main())
