"""Simulated PVR Controls SZ-7526TX-REA controller (Modbus RTU slave).

This is a test/demo tool.  It implements the register map independently of
the node's Modbus client (own CRC, own framing) so tests exercise the real
pymodbus RTU code path end to end.  It speaks raw RTU frames over:

* TCP  -- ``--port 5020`` and point the node at ``socket://127.0.0.1:5020``
* a real serial port -- ``--serial COM5`` / ``--serial /dev/ttyUSB1`` for bench
  testing RS-485 adapters without the physical controller.

Only behaviour documented in the register map is modelled.  Where the
datasheet is silent (e.g. reads spanning undocumented addresses) the mock
picks one behaviour and the node must cope with both (see ``strict_na``).

Every value produced here is SIMULATED.  Nodes using it must be configured
with ``simulation: true`` so readings are labelled as simulated on the server.
"""

from __future__ import annotations

import argparse
import logging
import random
import socket
import struct
import threading
import time

log = logging.getLogger("mock_controller")

PVR_KEY = 7507
CONTROLLER_ID_CODE = 115

# Exception codes (Modbus application protocol spec).
ILLEGAL_FUNCTION = 1
ILLEGAL_ADDRESS = 2
ILLEGAL_VALUE = 3


class ModbusError(Exception):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def add_crc(frame: bytes) -> bytes:
    return frame + struct.pack("<H", crc16(frame))


def check_crc(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


# Factory settings from the instruction manual, with demo-friendly overrides
# for SP/P2/P3 so faults actually occur during a simulation.
DEFAULT_PARAMETERS_C = {  # temperature parameters in degC
    40031: -18.0,  # SP
    40033: -10.0,  # P2  high temperature limit (manual default 99.0)
    40034: 1.0,    # P2d
    40035: -30.0,  # P3  low temperature limit (manual default -40.0)
    40036: 1.0,    # P3d
    40037: 99.0,   # HS
    40038: -40.0,  # LS
    40039: 2.0,    # P4 differential
    40040: 0.0,    # P5 calibration
}
DEFAULT_PARAMETERS_INT = {
    40032: 0, 40041: 3, 40042: 0, 40043: 2, 40044: 4, 40045: 10, 40046: 30, 40047: 6,
    40048: 1, 40049: 5, 40050: 1, 40051: 0, 40052: 5, 40053: 0, 40054: 1, 40055: 1,
    40056: 0, 40057: 0, 40058: 0, 40059: 0, 40060: 0, 40061: 1, 40062: 0, 40063: 60,
    40064: 0, 40065: 0, 40066: 0, 40067: 12, 40068: 0,
}


class MockController:
    """Register bank plus a crude cold-room thermal model."""

    def __init__(
        self,
        slave_id: int = 1,
        rs: int = 0,
        *,
        scenario: str = "normal",
        start_temp_c: float = -16.0,
        ambient_c: float = 30.0,
        time_scale: float = 1.0,
        strict_na: bool = False,
        seed: int | None = None,
    ):
        self.slave_id = slave_id
        self.rs = rs
        self.scenario = scenario
        self.time_scale = time_scale
        self.strict_na = strict_na
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.temp_c = start_temp_c
        self.ambient_c = ambient_c
        self.compressor = False
        self.alarm_muted = False
        self.faults: set[str] = set()
        self.forced_faults: set[str] = set()
        self.door_open = False
        self.defrost_until: float | None = None
        self.run_seconds = 0.0
        self.log_high: float | None = None
        self.log_low: float | None = None
        self.sim_time = 0.0
        self.last_off_at = -1e9
        self.overrides: dict[int, int] = {}   # raw register overrides for tests
        self.params: dict[int, int] = {}
        for ref, value in DEFAULT_PARAMETERS_INT.items():
            self.params[ref] = value
        self.params[40054] = slave_id
        self.params[40064] = rs
        for ref, value in DEFAULT_PARAMETERS_C.items():
            self.params[ref] = self._encode_temp(value)
        self.coil_writes: list[tuple[int, bool]] = []
        self.register_writes: list[tuple[int, int]] = []
        self._last_tick = time.monotonic()

    # ------------------------------------------------------------ helpers
    @property
    def factor(self) -> float:
        return 0.1 if self.rs == 0 else 1.0

    def _encode_temp(self, value_c: float) -> int:
        return int(round(value_c / self.factor)) & 0xFFFF

    def _param_c(self, ref: int) -> float:
        raw = self.params[ref]
        signed = raw - 0x10000 if raw & 0x8000 else raw
        return signed * self.factor

    def set_rs(self, rs: int) -> None:
        with self.lock:
            temps = {ref: self._param_c(ref) for ref in DEFAULT_PARAMETERS_C}
            self.rs = rs
            self.params[40064] = rs
            for ref, value in temps.items():
                self.params[ref] = self._encode_temp(value)

    def set_fault(self, name: str, active: bool = True) -> None:
        with self.lock:
            (self.forced_faults.add if active else self.forced_faults.discard)(name)

    # ------------------------------------------------------------ dynamics
    def tick(self) -> None:
        with self.lock:
            now = time.monotonic()
            dt = (now - self._last_tick) * self.time_scale
            self._last_tick = now
            steps = max(1, int(dt))
            for _ in range(min(steps, 3600)):
                self._step(dt / steps)

    def _step(self, dt: float) -> None:
        self.sim_time += dt
        t = self.sim_time
        if self.scenario == "demo":
            self.door_open = 600 <= t % 900 < 780
            if 1800 <= t % 2400 < 1860:
                self.forced_faults.add("probe_fail_low")
            else:
                self.forced_faults.discard("probe_fail_low")
        sp, diff = self._param_c(40031), self._param_c(40039)
        defrosting = self.defrost_until is not None and t < self.defrost_until
        if self.defrost_until is not None and t >= self.defrost_until:
            self.defrost_until = None
        restart_ok = t - self.last_off_at >= self.params[40041] * 60 / max(self.time_scale, 1)
        if defrosting:
            if self.compressor:
                self.last_off_at = t
            self.compressor = False
        elif self.compressor and self.temp_c <= sp:
            self.compressor = False
            self.last_off_at = t
        elif not self.compressor and self.temp_c >= sp + diff and restart_ok:
            self.compressor = True
        leak = 0.0004 * (5 if self.door_open else 1)
        if defrosting:
            leak *= 3
        cooling = -0.06 if self.compressor else 0.0
        self.temp_c += (cooling + leak * (self.ambient_c - self.temp_c)) * dt
        self.temp_c += self.rng.uniform(-0.02, 0.02)
        if self.compressor:
            self.run_seconds += dt
        self._update_faults()
        if not self._probe_failed():
            self.log_high = self.temp_c if self.log_high is None else max(self.log_high, self.temp_c)
            self.log_low = self.temp_c if self.log_low is None else min(self.log_low, self.temp_c)

    def _probe_failed(self) -> bool:
        return bool({"probe_fail_low", "probe_fail_high"} & (self.faults | self.forced_faults))

    def _update_faults(self) -> None:
        p2, p2d = self._param_c(40033), self._param_c(40034)
        p3, p3d = self._param_c(40035), self._param_c(40036)
        before = set(self.faults)
        if self.temp_c >= p2:
            self.faults.add("high_temp")
        elif self.temp_c <= p2 - p2d:
            self.faults.discard("high_temp")
        if self.temp_c <= p3:
            self.faults.add("low_temp")
        elif self.temp_c >= p3 + p3d:
            self.faults.discard("low_temp")
        if self.door_open and self.params[40048] in (1, 2):
            self.faults.add("door_open")
        else:
            self.faults.discard("door_open")
        if (self.faults | self.forced_faults) - before:
            self.alarm_muted = False  # a new fault re-arms the alarm

    # ------------------------------------------------------------ registers
    _FAULT_BITS = {"probe_fail_low": 0, "probe_fail_high": 1, "high_temp": 2, "low_temp": 3,
                   "door_open": 4, "compressor_fault": 5, "manual_compressor_fault": 6}

    def status_registers(self) -> dict[int, int]:
        faults = self.faults | self.forced_faults
        fault_bits = sum(1 << self._FAULT_BITS[f] for f in faults if f in self._FAULT_BITS)
        alarm = bool(faults) and self.params[40061] != 0 and not self.alarm_muted
        defrosting = self.defrost_until is not None
        if "probe_fail_low" in faults:
            control = 1001
        elif "probe_fail_high" in faults:
            control = 1002
        else:
            control = self._encode_temp(self.temp_c)
        system = (int(self.params[40066] == 1) << 1) | (int(self.rs == 0) << 2) | (int(defrosting) << 5)
        system |= int(self.sim_time - self.last_off_at < self.params[40041] * 60) << 8
        regs = {
            40001: PVR_KEY, 40002: self.params[40067], 40003: CONTROLLER_ID_CODE,
            40004: self.slave_id, 40005: self.rs, 40006: control & 0xFFFF,
            40007: 0, 40008: self._encode_temp(self.temp_c), 40009: 0, 40010: system,
            40011: int(self.compressor) | int(alarm) << 1, 40012: fault_bits, 40013: 0,
            40014: int(self.run_seconds // 3600), 40015: 0, 40016: 5, 40017: 30,
            40018: self.params[40031],
            40019: 1007 if self.log_high is None else self._encode_temp(self.log_high),
            40020: 1007 if self.log_low is None else self._encode_temp(self.log_low),
        }
        regs.update(self.overrides)
        return regs

    def read_holding(self, address: int, count: int) -> list[int]:
        if not 1 <= count <= 50:          # register map note 1
            raise ModbusError(ILLEGAL_VALUE)
        refs = range(40001 + address, 40001 + address + count)
        status = self.status_registers()
        values = []
        for ref in refs:
            if ref in status:
                if self.strict_na and ref in (40007, 40009, 40013, 40015):
                    raise ModbusError(ILLEGAL_ADDRESS)
                values.append(status[ref])
            elif ref in self.params:
                values.append(0 if ref == 40065 else self.params[ref])
            else:
                raise ModbusError(ILLEGAL_ADDRESS)
        return values

    def read_coils(self, address: int, count: int) -> list[bool]:
        if address < 0 or address + count > 5:
            raise ModbusError(ILLEGAL_ADDRESS)
        faults = self.faults | self.forced_faults
        alarm = bool(faults) and self.params[40061] != 0 and not self.alarm_muted
        coils = [False, alarm, "manual_compressor_fault" in faults, False, self.defrost_until is not None]
        return coils[address:address + count]

    def write_coil(self, address: int, value: bool) -> None:
        if not 0 <= address <= 4:
            raise ModbusError(ILLEGAL_ADDRESS)
        self.coil_writes.append((address + 1, value))
        if address == 1 and not value:
            self.alarm_muted = True
        elif address == 2 and not value:
            self.forced_faults.discard("manual_compressor_fault")
        elif address == 3 and value:
            self.log_high = self.log_low = None
        elif address == 4:
            self.defrost_until = self.sim_time + self.params[40046] * 60 if value else None

    def write_register(self, address: int, value: int) -> None:
        ref = 40001 + address
        if ref not in self.params or ref == 40067:
            raise ModbusError(ILLEGAL_ADDRESS)
        self.register_writes.append((ref, value))
        self.params[ref] = value & 0xFFFF

    # ------------------------------------------------------------ RTU frames
    def handle_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        try:
            if fc == 0x03:
                address, count = struct.unpack(">HH", pdu[1:5])
                values = self.read_holding(address, count)
                return struct.pack(">BB", fc, 2 * len(values)) + b"".join(struct.pack(">H", v) for v in values)
            if fc == 0x01:
                address, count = struct.unpack(">HH", pdu[1:5])
                bits = self.read_coils(address, count)
                packed = bytearray((len(bits) + 7) // 8)
                for i, bit in enumerate(bits):
                    if bit:
                        packed[i // 8] |= 1 << (i % 8)
                return struct.pack(">BB", fc, len(packed)) + bytes(packed)
            if fc == 0x05:
                address, value = struct.unpack(">HH", pdu[1:5])
                if value not in (0x0000, 0xFF00):
                    raise ModbusError(ILLEGAL_VALUE)
                self.write_coil(address, value == 0xFF00)
                return pdu[:5]
            if fc == 0x06:
                address, value = struct.unpack(">HH", pdu[1:5])
                self.write_register(address, value)
                return pdu[:5]
            if fc == 0x10:
                address, count, nbytes = struct.unpack(">HHB", pdu[1:6])
                values = struct.unpack(f">{count}H", pdu[6:6 + nbytes])
                for i, v in enumerate(values):
                    self.write_register(address + i, v)
                return pdu[:5]
            raise ModbusError(ILLEGAL_FUNCTION)
        except ModbusError as exc:
            return bytes([fc | 0x80, exc.code])


class Bus:
    """One RS-485 segment: several slaves, plus optional fault injection."""

    def __init__(self, controllers: list[MockController], drop_rate: float = 0.0,
                 corrupt_rate: float = 0.0, seed: int | None = None):
        self.controllers = {c.slave_id: c for c in controllers}
        self.drop_rate = drop_rate
        self.corrupt_rate = corrupt_rate
        self.offline = False
        self.rng = random.Random(seed)
        self.frames_handled = 0

    def handle_frame(self, frame: bytes) -> bytes | None:
        if self.offline or not check_crc(frame):
            return None
        slave = frame[0]
        controller = self.controllers.get(slave)
        if controller is None:
            return None
        if self.drop_rate and self.rng.random() < self.drop_rate:
            return None
        with controller.lock:
            controller.tick()
            reply = add_crc(bytes([slave]) + controller.handle_pdu(frame[1:-2]))
        self.frames_handled += 1
        if self.corrupt_rate and self.rng.random() < self.corrupt_rate:
            reply = reply[:-1] + bytes([reply[-1] ^ 0xFF])
        return reply


def expected_request_length(buf: bytes) -> int | None:
    if len(buf) < 2:
        return None
    if buf[1] in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        return 8
    if buf[1] in (0x0F, 0x10):
        return 9 + buf[6] if len(buf) >= 7 else None
    return len(buf)  # unknown function: hand over what we have


def _serve_stream(recv, send, bus: Bus, stop: threading.Event) -> None:
    buf = b""
    while not stop.is_set():
        try:
            chunk = recv()
        except (socket.timeout, TimeoutError):
            buf = b""  # line went silent mid-frame: discard, like a 3.5-char gap
            continue
        except OSError:
            return
        if chunk is None:
            continue
        if chunk == b"":
            return
        buf += chunk
        while (needed := expected_request_length(buf)) is not None and len(buf) >= needed:
            frame, buf = buf[:needed], buf[needed:]
            reply = bus.handle_frame(frame)
            if reply:
                send(reply)


class TcpRtuServer:
    """Raw RTU frames over TCP (pyserial ``socket://`` URL on the client side)."""

    def __init__(self, bus: Bus, host: str = "127.0.0.1", port: int = 0):
        self.bus = bus
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.host, self.port = self.sock.getsockname()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._accept_loop, name="mock-rtu-tcp", daemon=True)

    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.port}"

    def start(self) -> "TcpRtuServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        self.sock.close()

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            conn.settimeout(0.25)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn:
            _serve_stream(lambda: conn.recv(1024), conn.sendall, self.bus, self.stop_event)


def serve_serial(bus: Bus, port: str, baudrate: int, parity: str) -> None:
    import serial  # pyserial, only needed for bench testing with real adapters

    ser = serial.Serial(port, baudrate=baudrate, parity=parity, bytesize=8, stopbits=1, timeout=0.05)
    stop = threading.Event()

    def recv():
        data = ser.read(256)
        if not data:
            raise TimeoutError
        return data

    log.info("Serving simulated controller(s) on %s at %d baud, parity %s", port, baudrate, parity)
    _serve_stream(recv, ser.write, bus, stop)


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated SZ-7526TX-REA Modbus RTU controller")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5020, help="TCP port for socket:// clients")
    ap.add_argument("--serial", help="serve on a real serial port instead of TCP (bench test)")
    ap.add_argument("--baudrate", type=int, default=9600)
    ap.add_argument("--parity", default="N", choices=["N", "E", "O"])
    ap.add_argument("--slave-ids", default="1", help="comma-separated slave IDs on this bus")
    ap.add_argument("--rs", type=int, default=0, choices=[0, 1])
    ap.add_argument("--scenario", default="demo", choices=["normal", "demo"])
    ap.add_argument("--time-scale", type=float, default=1.0, help=">1 runs the thermal model faster")
    ap.add_argument("--drop-rate", type=float, default=0.0, help="fraction of requests left unanswered")
    ap.add_argument("--corrupt-rate", type=float, default=0.0, help="fraction of replies with bad CRC")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    controllers = [
        MockController(int(s), args.rs, scenario=args.scenario, time_scale=args.time_scale,
                       start_temp_c=-16.0 - i)
        for i, s in enumerate(args.slave_ids.split(","))
    ]
    bus = Bus(controllers, drop_rate=args.drop_rate, corrupt_rate=args.corrupt_rate)
    if args.serial:
        serve_serial(bus, args.serial, args.baudrate, args.parity)
        return
    server = TcpRtuServer(bus, args.host, args.port).start()
    log.info("SIMULATED controller(s) %s listening on %s", args.slave_ids, server.url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
