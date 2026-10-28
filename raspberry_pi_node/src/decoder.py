"""Pure decoding of raw SZ-7526TX-REA register values.

Nothing in this module performs I/O, so every rule documented in the
register map can be unit-tested with literal register values.

Decoding rules (register map notes 2 and 3):

* Registers documented as "Signed Int" are two's-complement 16-bit values.
* Temperatures are scaled by 0.1 when rS (40005) = 0 and by 1 when rS = 1.
* Special values are compared on the raw signed value BEFORE scaling:
  40006 = 1001 / 1002 means control probe fail low / high, and
  40019 / 40020 = 1007 means the temperature log has been reset.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping

from . import registers as R

QUALITY_GOOD = "good"
QUALITY_SUSPECT = "suspect"
QUALITY_PROBE_FAULT = "probe_fault"
QUALITY_INVALID = "invalid"
QUALITY_COMM_ERROR = "comm_error"


def to_int16(raw: int) -> int:
    """Interpret a raw 16-bit register value as two's-complement signed."""
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"register value {raw} outside 0..65535")
    return raw - 0x10000 if raw & 0x8000 else raw


def scale_factor(rs: int | None) -> float | None:
    """rS = 0 -> 0.1 degC per count, rS = 1 -> 1 degC per count; otherwise unknown."""
    return {0: 0.1, 1: 1.0}.get(rs)  # type: ignore[arg-type]


def apply_scale(signed: int, factor: float) -> float:
    # round() removes binary floating-point noise such as -18.200000000000003
    return round(signed * factor, 1) if factor != 1.0 else float(signed)


@dataclass
class Temperature:
    value_c: float | None
    raw: int | None
    status: str  # ok | probe_fail_low | probe_fail_high | log_reset | out_of_range | unknown_resolution | missing


def decode_temperature(
    raw: int | None,
    rs: int | None,
    *,
    probe_sentinels: bool = False,
    log_sentinel: bool = False,
    range_check: bool = True,
) -> Temperature:
    if raw is None:
        return Temperature(None, None, "missing")
    signed = to_int16(raw)
    if probe_sentinels:
        if signed == R.PROBE_FAIL_LOW_RAW:
            return Temperature(None, raw, "probe_fail_low")
        if signed == R.PROBE_FAIL_HIGH_RAW:
            return Temperature(None, raw, "probe_fail_high")
    if log_sentinel and signed == R.TEMP_LOG_RESET_RAW:
        return Temperature(None, raw, "log_reset")
    factor = scale_factor(rs)
    if factor is None:
        return Temperature(None, raw, "unknown_resolution")
    value = apply_scale(signed, factor)
    low, high = R.MEASURING_RANGE_C
    if range_check and not low <= value <= high:
        return Temperature(None, raw, "out_of_range")
    return Temperature(value, raw, "ok")


def decode_bits(value: int, bits: tuple[R.Bit, ...]) -> dict[str, bool]:
    return {b.key: bool(value >> b.bit & 1) for b in bits}


def undocumented_bits(value: int, bits: tuple[R.Bit, ...]) -> list[int]:
    known = {b.bit for b in bits}
    return [n for n in range(16) if value >> n & 1 and n not in known]


@dataclass
class StatusDecode:
    identity_ok: bool
    pvr_key: int | None
    software_revision: int | None
    controller_code: int | None
    device_id: int | None
    rs: int | None
    resolution_c: float | None
    control: Temperature
    actual: Temperature
    setpoint: Temperature
    log_high: Temperature
    log_low: Temperature
    system_status: int | None
    system_flags: dict[str, bool]
    relay_status: int | None
    compressor_on: bool | None
    alarm_on: bool | None
    fault_status: int | None
    faults: list[str]
    compressor_run_hours: int | None
    balanced_defrost_hours: int | None
    balanced_defrost_minutes: int | None
    quality: str
    quality_flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def temperature_c(self) -> float | None:
        """Primary measurement: the control (display) temperature."""
        return self.control.value_c

    def to_dict(self) -> dict:
        return asdict(self)


_QUALITY_RANK = {QUALITY_GOOD: 0, QUALITY_SUSPECT: 1, QUALITY_PROBE_FAULT: 2, QUALITY_INVALID: 3}


def _worse(current: str, candidate: str) -> str:
    return candidate if _QUALITY_RANK[candidate] > _QUALITY_RANK[current] else current


def decode_status(regs: Mapping[int, int], *, expected_slave_id: int | None = None) -> StatusDecode:
    """Decode the read-only block 40001-40020 keyed by datasheet reference."""
    g = regs.get
    quality = QUALITY_GOOD
    flags: list[str] = []
    warnings: list[str] = []

    pvr_key = g(40001)
    controller_code = g(40003)
    identity_ok = pvr_key == R.PVR_KEY_VALUE and controller_code == R.CONTROLLER_ID_CODE
    if not identity_ok:
        quality = QUALITY_INVALID
        flags.append("identity_mismatch")
        warnings.append(
            f"Identity check failed: 40001={pvr_key} (expected {R.PVR_KEY_VALUE}), "
            f"40003={controller_code} (expected {R.CONTROLLER_ID_CODE}). Wrong device, "
            "wrong address offset or wrong model - temperatures withheld."
        )

    device_id = to_int16(g(40004)) if g(40004) is not None else None
    rs = to_int16(g(40005)) if g(40005) is not None else None
    factor = scale_factor(rs)
    if factor is None:
        quality = QUALITY_INVALID
        flags.append("unknown_resolution")
        warnings.append(f"rS register 40005 = {rs}; only 0 and 1 are documented - temperatures withheld")

    control = decode_temperature(g(40006), rs, probe_sentinels=True)
    actual = decode_temperature(g(40008), rs)
    setpoint = decode_temperature(g(40018), rs, range_check=False)
    log_high = decode_temperature(g(40019), rs, log_sentinel=True, range_check=False)
    log_low = decode_temperature(g(40020), rs, log_sentinel=True, range_check=False)

    system_status = g(40010)
    relay_status = g(40011)
    fault_status = g(40012)
    system_flags = decode_bits(system_status, R.SYSTEM_STATUS_BITS) if system_status is not None else {}
    relay_flags = decode_bits(relay_status, R.RELAY_STATUS_BITS) if relay_status is not None else {}
    fault_flags = decode_bits(fault_status, R.FAULT_STATUS_BITS) if fault_status is not None else {}
    faults = [k for k, v in fault_flags.items() if v]

    for ref, value in ((40011, relay_status), (40012, fault_status)):
        if value is not None and value > 0xFF:
            flags.append("unexpected_high_byte")
            warnings.append(f"{ref} is documented as Unsigned Char but reads {value}")
    if fault_status is not None and undocumented_bits(fault_status, R.FAULT_STATUS_BITS):
        flags.append("undocumented_fault_bits")
        warnings.append(f"40012 has undocumented bits set: {undocumented_bits(fault_status, R.FAULT_STATUS_BITS)}")

    if control.status in ("probe_fail_low", "probe_fail_high"):
        quality = _worse(quality, QUALITY_PROBE_FAULT)
        if control.status not in faults:
            faults.append(control.status)
    elif fault_flags.get("probe_fail_low") or fault_flags.get("probe_fail_high"):
        # Fault register says the probe failed although 40006 is not a sentinel:
        # the temperature cannot be trusted.
        quality = _worse(quality, QUALITY_PROBE_FAULT)
        warnings.append("Fault status reports probe failure; control temperature withheld")
        control = Temperature(None, control.raw, "probe_fault_flag")
    elif control.status == "out_of_range":
        quality = _worse(quality, QUALITY_INVALID)
        flags.append("out_of_range")
        warnings.append(f"40006 raw {control.raw} is outside the measuring range")
    elif control.status == "missing":
        quality = _worse(quality, QUALITY_INVALID)
        flags.append("missing_temperature")

    # Cross-checks between independent registers.
    if rs in (0, 1) and "resolution_tenths" in system_flags:
        tenths_by_rs = rs == 0
        if system_flags["resolution_tenths"] != tenths_by_rs:
            quality = _worse(quality, QUALITY_SUSPECT)
            flags.append("resolution_mismatch")
            warnings.append(
                f"rS (40005) = {rs} but 40010 bit2 = {int(system_flags['resolution_tenths'])}; "
                "scaling follows rS as the register map specifies"
            )
    if expected_slave_id is not None and device_id is not None and device_id != expected_slave_id:
        quality = _worse(quality, QUALITY_SUSPECT)
        flags.append("device_id_mismatch")
        warnings.append(f"Device ID register 40004 = {device_id}, polled as slave {expected_slave_id}")

    if not identity_ok or factor is None:
        control = Temperature(None, control.raw, control.status if control.status != "ok" else "withheld")
        actual = Temperature(None, actual.raw, "withheld")
        setpoint = Temperature(None, setpoint.raw, "withheld")
        log_high = Temperature(None, log_high.raw, "withheld")
        log_low = Temperature(None, log_low.raw, "withheld")

    return StatusDecode(
        identity_ok=identity_ok,
        pvr_key=pvr_key,
        software_revision=g(40002),
        controller_code=controller_code,
        device_id=device_id,
        rs=rs,
        resolution_c=factor,
        control=control,
        actual=actual,
        setpoint=setpoint,
        log_high=log_high,
        log_low=log_low,
        system_status=system_status,
        system_flags=system_flags,
        relay_status=relay_status,
        compressor_on=relay_flags.get("compressor"),
        alarm_on=relay_flags.get("alarm"),
        fault_status=fault_status,
        faults=faults,
        compressor_run_hours=g(40014),
        balanced_defrost_hours=g(40016),
        balanced_defrost_minutes=g(40017),
        quality=quality,
        quality_flags=flags,
        warnings=warnings,
    )


def decode_parameters(regs: Mapping[int, int], rs: int | None) -> dict[str, dict]:
    """Decode programmable parameters 40031-40068 for display/audit.

    The keypad password (PA, 40065) is replaced by None and marked redacted.
    """
    factor = scale_factor(rs)
    out: dict[str, dict] = {}
    for p in R.PARAMETERS:
        raw = regs.get(p.reference)
        entry: dict = {"reference": p.reference, "description": p.description, "unit": p.unit}
        if p.sensitive:
            entry.update(value=None, raw=None, redacted=True)
        elif raw is None:
            entry.update(value=None, raw=None)
        else:
            signed = to_int16(raw)
            if p.temperature:
                value = apply_scale(signed, factor) if factor is not None else None
            else:
                value = signed
            entry.update(value=value, raw=raw)
            if p.options:
                entry["label"] = p.options.get(signed, f"undocumented ({signed})")
        out[p.code] = entry
    return out


def decode_coils(bits: list[bool]) -> dict[str, bool]:
    """Map coil values read from coil 1 onward to their documented keys."""
    return {c.key: bool(bits[i]) for i, c in enumerate(R.COILS) if i < len(bits)}
