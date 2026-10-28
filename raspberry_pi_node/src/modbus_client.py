"""Modbus RTU communication with SZ-7526TX-REA controllers (pymodbus 3.11).

Design rules:

* One :class:`SerialBus` per physical RS-485 port.  Every transaction holds the
  bus lock, so requests to controllers sharing a half-duplex line never overlap.
* pymodbus' own retry loop is disabled (``retries=0``); retries are done here so
  they are explicit, counted and logged.
* Reading is the normal path.  The only write path is :meth:`ControllerClient.
  write_command`, which refuses to run unless ``allow_writes`` is set in the
  node's LOCAL configuration file, and which only maps to coil writes documented
  in the register map.
* Any failure is raised as :class:`CommError` carrying a ``status`` string; the
  caller records it and carries on.  Nothing here terminates the process.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import decoder
from . import registers as R

log = logging.getLogger(__name__)

COMM_OK = "ok"
COMM_TIMEOUT = "timeout"              # no (valid) reply: dead line, wrong slave ID/baud/parity, CRC error
COMM_EXCEPTION = "exception"          # controller answered with a Modbus exception code
COMM_INVALID = "invalid_response"     # reply with unexpected shape
COMM_PORT = "port_error"              # serial port missing / cannot be opened
COMM_IDENTITY = "identity_mismatch"   # replied, but 40001/40003 are not 7507/115
COMM_ERROR = "error"                  # anything else

# Exception codes after which a retry cannot succeed.
_PERMANENT_EXCEPTION_CODES = {1, 2, 3}


class CommError(Exception):
    def __init__(self, status: str, message: str, exception_code: int | None = None):
        super().__init__(message)
        self.status = status
        self.exception_code = exception_code


class WritesDisabled(PermissionError):
    pass


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int = 9600
    parity: str = "N"
    bytesize: int = 8
    stopbits: int = 1
    timeout_s: float = 1.0
    inter_request_delay_ms: int = 50


def _default_client_factory(settings: SerialSettings):
    from pymodbus.client import ModbusSerialClient

    return ModbusSerialClient(
        settings.port,
        baudrate=settings.baudrate,
        parity=settings.parity,
        bytesize=settings.bytesize,
        stopbits=settings.stopbits,
        timeout=settings.timeout_s,
        retries=0,
    )


class SerialBus:
    """Serialises all traffic on one RS-485 port and owns its pymodbus client."""

    def __init__(self, settings: SerialSettings, client_factory: Callable[[SerialSettings], Any] | None = None):
        self.settings = settings
        self._factory = client_factory or _default_client_factory
        self._client = None
        self._last_end = 0.0
        self.lock = threading.RLock()

    def _connected_client(self):
        if self._client is None:
            self._client = self._factory(self.settings)
        if not self._client.connect():
            self.close()
            raise CommError(COMM_PORT, f"cannot open serial port {self.settings.port}")
        return self._client

    def execute(self, fn: Callable[[Any], Any]) -> Any:
        """Run one Modbus transaction exclusively on this bus."""
        from pymodbus.exceptions import ConnectionException, ModbusIOException

        with self.lock:
            gap = self.settings.inter_request_delay_ms / 1000 - (time.monotonic() - self._last_end)
            if gap > 0:
                time.sleep(gap)
            try:
                return fn(self._connected_client())
            except CommError:
                raise
            except ConnectionException as exc:
                self.close()
                raise CommError(COMM_PORT, f"serial connection failed: {exc}") from exc
            except ModbusIOException as exc:
                raise CommError(COMM_TIMEOUT, f"no valid response: {exc}") from exc
            except (OSError, ValueError) as exc:  # pyserial errors, e.g. USB adapter unplugged
                self.close()
                raise CommError(COMM_PORT, f"serial I/O error: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - library raised something undocumented
                self.close()
                raise CommError(COMM_ERROR, f"{type(exc).__name__}: {exc}") from exc
            finally:
                self._last_end = time.monotonic()

    def close(self) -> None:
        with self.lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None


class ControllerClient:
    """Read (and, only when explicitly allowed, command) one controller."""

    def __init__(
        self,
        bus: SerialBus,
        slave_id: int,
        *,
        retries: int = 2,
        retry_delay_s: float = 0.2,
        address_offset: int = 0,
        read_strategy: str = "auto",
        allow_writes: bool = False,
        diagnostic_logging: bool = False,
        model: str = R.MODEL_SZ7526TX_REA,
    ):
        if not R.SLAVE_ID_RANGE[0] <= slave_id <= R.SLAVE_ID_RANGE[1]:
            raise ValueError(f"slave ID {slave_id} outside {R.SLAVE_ID_RANGE}")
        if read_strategy not in ("auto", "block", "segments"):
            raise ValueError(f"unknown read_strategy {read_strategy!r}")
        self.bus = bus
        self.slave_id = slave_id
        self.retries = retries
        self.retry_delay_s = retry_delay_s
        self.address_offset = address_offset
        self.read_strategy = read_strategy
        self.allow_writes = allow_writes
        self.diagnostic_logging = diagnostic_logging
        self.profile = R.PROFILES[model]
        self._use_segments = read_strategy == "segments"

    # ------------------------------------------------------------ low level
    def _transaction(self, description: str, request: Callable[[Any], Any], *, retry: bool = True):
        attempts = 1 + (self.retries if retry else 0)
        last: CommError | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.bus.execute(request)
                if response.isError():
                    code = getattr(response, "exception_code", None)
                    raise CommError(COMM_EXCEPTION, f"{description}: Modbus exception code {code}", code)
                return response
            except CommError as exc:
                last = exc
                permanent = exc.status == COMM_EXCEPTION and exc.exception_code in _PERMANENT_EXCEPTION_CODES
                log.debug("slave %s %s attempt %d/%d failed: %s", self.slave_id, description, attempt, attempts, exc)
                if permanent or attempt == attempts:
                    break
                time.sleep(self.retry_delay_s * attempt)
        assert last is not None
        raise last

    def read_holding(self, reference: int, count: int) -> dict[int, int]:
        """Read ``count`` holding registers starting at datasheet reference ``reference``."""
        if not 1 <= count <= R.MAX_REGISTERS_PER_READ:
            raise ValueError(f"count {count} violates the {R.MAX_REGISTERS_PER_READ}-register limit")
        address = R.holding_address(reference, self.address_offset)
        response = self._transaction(
            f"read {reference}+{count}",
            lambda c: c.read_holding_registers(address, count=count, device_id=self.slave_id),
        )
        values = list(getattr(response, "registers", None) or [])
        if len(values) != count or any(not 0 <= v <= 0xFFFF for v in values):
            raise CommError(COMM_INVALID, f"expected {count} registers from {reference}, got {values!r}")
        return {reference + i: v for i, v in enumerate(values)}

    # ------------------------------------------------------------ reads
    def read_status_registers(self) -> dict[int, int]:
        if not self._use_segments:
            try:
                return self.read_holding(*self.profile.status_block)
            except CommError as exc:
                if self.read_strategy == "auto" and exc.exception_code == 2:
                    log.warning("slave %s rejected block read (illegal address); switching to "
                                "documented-register segments", self.slave_id)
                    self._use_segments = True
                else:
                    raise
        regs: dict[int, int] = {}
        for start, count in self.profile.status_segments:
            regs.update(self.read_holding(start, count))
        return regs

    def poll(self) -> tuple[dict[int, int], decoder.StatusDecode]:
        """One status poll: raw registers plus decoded values."""
        raw = self.read_status_registers()
        decoded = decoder.decode_status(raw, expected_slave_id=self.slave_id)
        if self.diagnostic_logging:
            log.info("slave %s raw=%s", self.slave_id, {k: raw[k] for k in sorted(raw)})
            log.info("slave %s decoded temp=%s rs=%s quality=%s flags=%s faults=%s", self.slave_id,
                     decoded.temperature_c, decoded.rs, decoded.quality, decoded.quality_flags, decoded.faults)
        if not decoded.identity_ok:
            raise CommError(COMM_IDENTITY, decoded.warnings[0] if decoded.warnings else "identity mismatch")
        return raw, decoded

    def read_parameters(self, rs: int | None) -> tuple[dict[int, int], dict[str, dict]]:
        raw = self.read_holding(*self.profile.parameter_block)
        raw_public = {k: v for k, v in raw.items() if not R.PARAMETERS_BY_REF[k].sensitive}
        return raw_public, decoder.decode_parameters(raw, rs)

    def read_coils(self) -> dict[str, bool]:
        first, count = self.profile.coil_block
        address = R.coil_address(first, self.address_offset)
        response = self._transaction(
            "read coils", lambda c: c.read_coils(address, count=count, device_id=self.slave_id)
        )
        bits = list(getattr(response, "bits", None) or [])
        if len(bits) < count:
            raise CommError(COMM_INVALID, f"expected {count} coils, got {bits!r}")
        return decoder.decode_coils(bits[:count])

    # ------------------------------------------------------------ writes
    def write_command(self, command: str) -> dict:
        """Execute one documented coil command.  Never retried automatically."""
        if not self.allow_writes:
            raise WritesDisabled("controller writes are disabled in the node's local configuration")
        if command not in R.COMMAND_COIL_WRITES:
            raise ValueError(f"unsupported command {command!r}")
        coil, value = R.COMMAND_COIL_WRITES[command]
        address = R.coil_address(coil, self.address_offset)
        log.warning("WRITE slave %s coil %s (address %s) <- %s for command %s",
                    self.slave_id, coil, address, int(value), command)
        response = self._transaction(
            f"write coil {coil}",
            lambda c: c.write_coil(address, value, device_id=self.slave_id),
            retry=False,
        )
        echoed = list(getattr(response, "bits", []) or [])
        return {"coil": coil, "address": address, "value": value, "echo": bool(echoed[0]) if echoed else None}
