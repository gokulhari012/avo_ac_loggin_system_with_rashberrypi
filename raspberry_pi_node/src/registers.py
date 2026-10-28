"""Register map for the PVR Controls SZ-7526TX-REA temperature controller.

Source documents (both must be consulted before changing anything here):

* ``SZ-7526TX-REA_PRM.pdf`` -- Product Register Map, document PVR/DSG/PRM,
  Issue 01, Rev 00, 24-11-2023.
* ``SZ-7526TX_0.pdf`` -- Instruction manual, REV3 23.11.2023.

Addressing convention
---------------------
The register map lists holding registers in Modicon reference notation
(40001 ... 40068) and coils as 1 ... 5.  A Modbus RTU frame carries a
*zero-based* protocol (PDU) address, and pymodbus expects that zero-based
address.  The translation used here is the standard one:

    holding register 4xxxx  ->  PDU address = 4xxxx - 40001     (40006 -> 5)
    coil n                  ->  PDU address = n - 1             (coil 5 -> 4)

The datasheet does not state the convention explicitly.  It is verified at
runtime: register 40001 must read 7507 ("PVR Key") and 40003 must read 115
("Controller Identification Code").  If those identity values do not match,
the node reports ``identity_mismatch`` instead of publishing temperatures.
``address_offset`` (controller ``register_overrides``) exists only so a
commissioning engineer can correct a vendor off-by-one *after* the hardware
validation procedure proves it is needed; it defaults to 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODEL_SZ7526TX_REA = "SZ-7526TX-REA"

HOLDING_REFERENCE_BASE = 40001
COIL_REFERENCE_BASE = 1

# Register map note 1: "At a time only 50 registers are accessible."
MAX_REGISTERS_PER_READ = 50

# Identity values documented in the register map comments column.
PVR_KEY_VALUE = 7507          # 40001
CONTROLLER_ID_CODE = 115      # 40003

# Special raw values (compared BEFORE scaling).
PROBE_FAIL_LOW_RAW = 1001     # 40006: control probe fail, low side
PROBE_FAIL_HIGH_RAW = 1002    # 40006: control probe fail, high side
TEMP_LOG_RESET_RAW = 1007     # 40019/40020: log has been reset (no value)

# Instruction manual "Measuring Range": -45.0 to 100.0 degC.
MEASURING_RANGE_C = (-45.0, 100.0)

# Serial options documented in the register map ("Communication Settings").
SUPPORTED_BAUDRATES = (9600, 19200, 38400)       # bd parameter 1 / 2 / 3
SUPPORTED_PARITIES = ("N", "E", "O")             # PAr parameter 0 / 1 / 2
SUPPORTED_BYTESIZES = (8,)                       # "8 Data Bits"
SUPPORTED_STOPBITS = (1,)                        # "1 Stop Bit"
SLAVE_ID_RANGE = (1, 250)                        # did: 0 disables comms


def holding_address(reference: int, offset: int = 0) -> int:
    """Translate a datasheet holding-register reference (4xxxx) to a PDU address."""
    if not 40001 <= reference <= 49999:
        raise ValueError(f"{reference} is not a 4xxxx holding-register reference")
    address = reference - HOLDING_REFERENCE_BASE + offset
    if address < 0:
        raise ValueError(f"offset {offset} makes address of {reference} negative")
    return address


def coil_address(reference: int, offset: int = 0) -> int:
    """Translate a datasheet coil number (1-based) to a PDU address."""
    if reference < COIL_REFERENCE_BASE:
        raise ValueError(f"coil reference {reference} must be >= 1")
    address = reference - COIL_REFERENCE_BASE + offset
    if address < 0:
        raise ValueError(f"offset {offset} makes address of coil {reference} negative")
    return address


@dataclass(frozen=True)
class Register:
    reference: int
    key: str
    name: str
    data_type: str          # "uint16", "int16", "uint8" (datasheet "Unsigned Char"), "unspecified"
    access: str = "R"       # "R" or "RW"
    temperature: bool = False
    note: str = ""


# Read-only block 40001-40020.  40007/40009/40013/40015 are "NA" in the map;
# they are read as part of a block but never interpreted.
STATUS_REGISTERS: tuple[Register, ...] = (
    Register(40001, "pvr_key", "PVR Key", "uint16", note="Fixed identity value 7507"),
    Register(40002, "software_revision", "Software Revision", "uint16"),
    Register(40003, "controller_code", "Controller Identification Code", "uint16",
             note="Fixed identity value 115"),
    Register(40004, "device_id", "Device (Slave) ID", "int16", note="dis(0) to 250"),
    Register(40005, "rs", "rS (resolution)", "int16", note="0 = 0.1 degC, 1 = 1 degC"),
    Register(40006, "control_temperature", "Control Temperature (Display Temperature)", "int16",
             temperature=True, note="1001 = probe fail low, 1002 = probe fail high"),
    Register(40008, "actual_temperature", "Actual Temperature", "unspecified", temperature=True,
             note="Data type and scaling not specified in the map; decoded as int16 "
                  "with rS scaling (ASSUMPTION - verify during commissioning)"),
    Register(40010, "system_status", "System Status / Delay Status (bitwise)", "uint16"),
    Register(40011, "relay_status", "Relay status (bitwise)", "uint8"),
    Register(40012, "fault_status", "Fault status (bitwise)", "uint8"),
    Register(40014, "compressor_run_hours", "Compressor Run Hours", "uint16"),
    Register(40016, "balanced_defrost_hours", "P8H Balanced Defrost Time Hours", "uint16"),
    Register(40017, "balanced_defrost_minutes", "P8m Balanced Defrost Time Mins", "uint16"),
    Register(40018, "setpoint", "Set point (read only)", "int16", temperature=True),
    Register(40019, "temp_log_high", "Temp Log High", "int16", temperature=True,
             note="Last high temperature; 1007 = log reset"),
    Register(40020, "temp_log_low", "Temp Log Low", "int16", temperature=True,
             note="Last low temperature; 1007 = log reset"),
)
STATUS_BY_REF = {r.reference: r for r in STATUS_REGISTERS}
STATUS_BY_KEY = {r.key: r for r in STATUS_REGISTERS}
NA_REFERENCES = (40007, 40009, 40013, 40015)

# One block read covers every documented read-only register (20 <= 50).
STATUS_BLOCK = (40001, 20)
# Fallback when the controller rejects a block that spans "NA" registers:
# contiguous runs of documented registers only.
STATUS_SEGMENTS: tuple[tuple[int, int], ...] = (
    (40001, 6), (40008, 1), (40010, 3), (40014, 1), (40016, 5),
)


@dataclass(frozen=True)
class Parameter:
    reference: int
    code: str
    description: str
    temperature: bool = False   # register map note 3 (scaled by rS)
    unit: str = ""
    options: dict[int, str] = field(default_factory=dict)
    sensitive: bool = False     # never transmitted or logged
    note: str = ""


_ON_OFF = {0: "no", 1: "yes"}

# Programmable parameters 40031-40068 (R/W).  Descriptions/units come from the
# instruction manual.  The node only READS these; see docs/modbus-register-map.md
# for why remote parameter writes are intentionally not implemented.
PARAMETERS: tuple[Parameter, ...] = (
    Parameter(40031, "SP", "Compressor relay set point", True, "degC"),
    Parameter(40032, "P1", "Heating or cooling mode", options={0: "Cooling (CoL)", 1: "Heating (HEt)"}),
    Parameter(40033, "P2", "High temperature limit", True, "degC"),
    Parameter(40034, "P2d", "Differential to reset high temperature fault", True, "degC"),
    Parameter(40035, "P3", "Low temperature limit", True, "degC"),
    Parameter(40036, "P3d", "Differential to reset low temperature fault", True, "degC"),
    Parameter(40037, "HS", "Maximum set point limit", True, "degC"),
    Parameter(40038, "LS", "Minimum set point limit", True, "degC"),
    Parameter(40039, "P4", "Differential for compressor relay ON", True, "degC"),
    Parameter(40040, "P5", "Probe calibration", True, "degC"),
    Parameter(40041, "P6", "Compressor restart delay", unit="min"),
    Parameter(40042, "ot", "Minimum compressor ON time", unit="min"),
    Parameter(40043, "E1", "Compressor relay status on probe failure",
              options={0: "OFF", 1: "ON", 2: "Duty cycle (CYC)"}),
    Parameter(40044, "Cn", "Compressor OFF time during probe fault", unit="min"),
    Parameter(40045, "Cy", "Compressor ON time during probe fault", unit="min"),
    Parameter(40046, "P7", "Defrost duration", unit="min"),
    Parameter(40047, "P8", "Defrost frequency", unit="h"),
    Parameter(40048, "AF0", "AUX digital input function",
              options={0: "Disabled (dIS)", 1: "Door open = open contact (doP)",
                       2: "Door open = closed contact (dCL)",
                       3: "Compressor fault = open contact (CoP)",
                       4: "Compressor fault = closed contact (CCL)"}),
    Parameter(40049, "CF1/do1", "AUX digital input fault sensing delay", unit="s"),
    Parameter(40050, "CF2/do2", "Output action on AUX digital input fault",
              options={0: "Indication only (Enb)", 1: "Indication + alarm (EnA)",
                       2: "Compressor OFF + alarm (CoF)"}),
    Parameter(40051, "CF3", "Compressor fault reset mode", options={0: "Auto (AUt)", 1: "Manual (nAn)"}),
    Parameter(40052, "CF4", "Compressor retrials before manual reset"),
    Parameter(40053, "do3", "Temperature hold delay on door open", unit="s"),
    Parameter(40054, "did", "Device ID (0 = communication disabled)"),
    Parameter(40055, "bd", "Baud rate", options={1: "9600", 2: "19200", 3: "38400"}),
    Parameter(40056, "Par", "Parity", options={0: "None", 1: "Even", 2: "Odd"}),
    Parameter(40057, "Ccr", "Reset compressor run hours", options=_ON_OFF),
    Parameter(40058, "ddF", "Display during defrost",
              options={0: "Control probe temperature (Pr1)", 1: "Defrost label (dF)",
                       2: "Held temperature (CHt)"}),
    Parameter(40059, "nd", "Default display", options={0: "Control probe temperature", 1: "Set point"}),
    Parameter(40060, "dd", "Display delay (rising temperature)", unit="s"),
    Parameter(40061, "AL", "Alarm relay activation",
              options={0: "Disabled (dIS)", 1: "Ht, Lt and other faults (AHL)",
                       2: "Ht and other faults (AHt)", 3: "Lt and other faults (ALt)"}),
    Parameter(40062, "Adt", "Ht/Lt alarm delay at power ON", unit="min"),
    Parameter(40063, "Add", "Alarm relay delay at door open", unit="s"),
    Parameter(40064, "rS", "Controller resolution", options={0: "0.1 degC", 1: "1 degC"}),
    Parameter(40065, "PA", "Keypad password", sensitive=True,
              note="Value is never transmitted, stored or logged"),
    Parameter(40066, "LP", "Keypad lock", options={0: "Unlocked (UnL)", 1: "Locked (LoC)"}),
    Parameter(40067, "Ver", "Software version (read only)"),
    Parameter(40068, "FS", "Restore factory defaults", options=_ON_OFF),
)
PARAMETERS_BY_REF = {p.reference: p for p in PARAMETERS}
PARAMETER_BLOCK = (40031, 38)   # 40031..40068, 38 <= 50


@dataclass(frozen=True)
class Coil:
    reference: int
    key: str
    name: str
    note: str


COILS: tuple[Coil, ...] = (
    Coil(1, "power", "Power", "Documented only as 'Extra'; function unknown - never written"),
    Coil(2, "alarm_relay_mute", "Alarm Relay Mute",
         "Reads 1 while the alarm relay is ON; write 0 to mute"),
    Coil(3, "manual_fault_reset", "Manual Fault Reset",
         "Reads 1 while a manual-reset fault is present; write 0 to reset"),
    Coil(4, "temp_log_reset", "Temp Log Reset", "Always reads 0; write 1 to reset the temperature log"),
    Coil(5, "defrost", "Defrost", "Write 1 to start defrost, 0 to stop"),
)
COILS_BY_KEY = {c.key: c for c in COILS}
COIL_BLOCK = (1, 5)

# The only controller writes this software can ever perform, each mapped to
# exactly the coil/value the register map documents.  Coil 1 ("Power",
# documented as "Extra") and all parameter registers are deliberately absent.
COMMAND_COIL_WRITES: dict[str, tuple[int, bool]] = {
    "alarm_mute": (2, False),
    "fault_reset": (3, False),
    "temp_log_reset": (4, True),
    "defrost_start": (5, True),
    "defrost_stop": (5, False),
}


@dataclass(frozen=True)
class Bit:
    bit: int
    key: str
    name: str
    note: str = ""


# 40010 System Status / Delay Status
SYSTEM_STATUS_BITS: tuple[Bit, ...] = (
    Bit(0, "power_off", "POWER OFF", "Documented as 'Extra'"),
    Bit(1, "keypad_locked", "KEYPAD LOCK", "1 = keypad locked"),
    Bit(2, "resolution_tenths", "Resolution_1", "1 = 0.1 degC, 0 = 1 degC (inverse polarity of rS)"),
    Bit(3, "resolution_2", "Resolution_2", "Documented as 'Extra'"),
    Bit(4, "temperature_unit", "Temperature Unit selection", "Documented as 'Extra'"),
    Bit(5, "defrost", "DEFROST", "1 = defrost ON"),
    Bit(8, "restart_delay", "Compressor restart time delay (P6)", "1 = delay in process"),
    Bit(9, "min_on_delay", "Compressor minimum ON time delay (ot)", "1 = delay in process"),
    Bit(10, "ht_lt_power_on_delay", "Ht/Lt power ON delay (Adt)", "1 = delay in process"),
)

# 40011 Relay status
RELAY_STATUS_BITS: tuple[Bit, ...] = (
    Bit(0, "compressor", "COMPRESSOR", "1 = relay ON"),
    Bit(1, "alarm", "ALARM", "1 = relay ON"),
)

# 40012 Fault status
FAULT_STATUS_BITS: tuple[Bit, ...] = (
    Bit(0, "probe_fail_low", "Control probe fail low (PFL)"),
    Bit(1, "probe_fail_high", "Control probe fail high (PFH)"),
    Bit(2, "high_temp", "High temperature fault (Ht)"),
    Bit(3, "low_temp", "Low temperature fault (Lt)"),
    Bit(4, "door_open", "Door open fault (do)"),
    Bit(5, "compressor_fault", "Compressor fault (CF)"),
    Bit(6, "manual_compressor_fault", "Compressor fault awaiting manual reset"),
)
FAULT_DESCRIPTIONS = {b.key: b.name for b in FAULT_STATUS_BITS}


@dataclass(frozen=True)
class ControllerProfile:
    model: str
    status_block: tuple[int, int]
    status_segments: tuple[tuple[int, int], ...]
    parameter_block: tuple[int, int]
    coil_block: tuple[int, int]
    baudrates: tuple[int, ...]
    parities: tuple[str, ...]
    bytesizes: tuple[int, ...]
    stopbits: tuple[int, ...]


PROFILES = {
    MODEL_SZ7526TX_REA: ControllerProfile(
        model=MODEL_SZ7526TX_REA,
        status_block=STATUS_BLOCK,
        status_segments=STATUS_SEGMENTS,
        parameter_block=PARAMETER_BLOCK,
        coil_block=COIL_BLOCK,
        baudrates=SUPPORTED_BAUDRATES,
        parities=SUPPORTED_PARITIES,
        bytesizes=SUPPORTED_BYTESIZES,
        stopbits=SUPPORTED_STOPBITS,
    ),
}


def _check_blocks() -> None:
    for start, count in (STATUS_BLOCK, PARAMETER_BLOCK, *STATUS_SEGMENTS):
        assert 1 <= count <= MAX_REGISTERS_PER_READ, (start, count)


_check_blocks()
