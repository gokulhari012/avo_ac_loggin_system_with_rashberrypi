"""Modbus client tests.

Most tests go through the real pymodbus RTU framer to the simulated controller
over a socket, so frame encoding, CRC and address translation are exercised
for real.  Retry/error classification tests use a scripted fake client.
"""

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from src import modbus_client as M


def test_status_poll_end_to_end(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.temp_c = -18.24
    client = client_for()
    raw, decoded = client.poll()
    assert raw[40001] == 7507 and raw[40003] == 115
    assert decoded.identity_ok and decoded.quality == "good"
    assert -18.4 <= decoded.temperature_c <= -18.1
    assert decoded.setpoint.value_c == -18.0


def test_rs1_end_to_end(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.set_rs(1)
    controller.temp_c = -5.4
    _, decoded = client_for().poll()
    assert decoded.rs == 1 and decoded.temperature_c in (-6.0, -5.0)
    assert decoded.setpoint.value_c == -18.0


def test_probe_failure_end_to_end(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.set_fault("probe_fail_high")
    _, decoded = client_for().poll()
    assert decoded.temperature_c is None and decoded.quality == "probe_fault"
    assert "probe_fail_high" in decoded.faults


def test_wrong_slave_id_times_out(client_for):
    client = client_for(slave_id=9, retries=1)
    with pytest.raises(M.CommError) as err:
        client.poll()
    assert err.value.status == M.COMM_TIMEOUT


def test_identity_mismatch_raises(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.overrides[40001] = 1234
    with pytest.raises(M.CommError) as err:
        client_for().poll()
    assert err.value.status == M.COMM_IDENTITY


def test_address_offset_mistake_never_yields_a_reading(client_for):
    # A one-off address error shifts every register.  Depending on the device it
    # either reads past the documented block (exception) or returns shifted
    # values that fail the 7507/115 identity check - never a "good" reading.
    with pytest.raises(M.CommError) as err:
        client_for(address_offset=1).poll()
    assert err.value.status in (M.COMM_IDENTITY, M.COMM_EXCEPTION)


def test_shifted_registers_fail_identity(client_for):
    shifted = client_for(address_offset=1).read_holding(40001, 3)
    assert shifted[40001] != 7507 and shifted[40003] != 115


def test_auto_falls_back_to_segments_when_na_rejected(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.strict_na = True
    client = client_for()
    _, decoded = client.poll()
    assert decoded.quality == "good" and client._use_segments is True


def test_block_strategy_does_not_fall_back(client_for, mock_bus):
    _, _, controller = mock_bus
    controller.strict_na = True
    with pytest.raises(M.CommError) as err:
        client_for(read_strategy="block").poll()
    assert err.value.status == M.COMM_EXCEPTION and err.value.exception_code == 2


def test_more_than_50_registers_refused_locally(client_for):
    with pytest.raises(ValueError):
        client_for().read_holding(40001, 51)


def test_parameters_and_coils(client_for):
    client = client_for()
    raw, params = client.read_parameters(rs=0)
    assert 40065 not in raw  # keypad password never leaves the client
    assert params["SP"]["value"] == -18.0 and params["PA"]["redacted"]
    coils = client.read_coils()
    assert set(coils) == {"power", "alarm_relay_mute", "manual_fault_reset", "temp_log_reset", "defrost"}


def test_writes_disabled_by_default(client_for, mock_bus):
    _, _, controller = mock_bus
    with pytest.raises(M.WritesDisabled):
        client_for().write_command("defrost_start")
    assert controller.coil_writes == []


def test_write_command_when_allowed(client_for, mock_bus):
    _, _, controller = mock_bus
    result = client_for(allow_writes=True).write_command("temp_log_reset")
    assert controller.coil_writes == [(4, True)]
    assert result["coil"] == 4 and result["address"] == 3


def test_unknown_command_rejected(client_for):
    with pytest.raises(ValueError):
        client_for(allow_writes=True).write_command("power_off")


def test_polling_never_writes(client_for, mock_bus):
    _, _, controller = mock_bus
    client = client_for()
    for _ in range(3):
        client.poll()
    client.read_parameters(0)
    client.read_coils()
    assert controller.coil_writes == [] and controller.register_writes == []


def test_bad_crc_replies_are_reported_as_timeouts(mock_bus, client_for):
    _, bus, _ = mock_bus
    bus.corrupt_rate = 1.0
    with pytest.raises(M.CommError) as err:
        client_for(retries=0).poll()
    assert err.value.status == M.COMM_TIMEOUT


# --------------------------------------------------------------- scripted fake
class FakeResponse:
    def __init__(self, registers=None, error_code=None):
        self.registers = registers or []
        self.exception_code = error_code

    def isError(self):
        return self.exception_code is not None


class ScriptedClient:
    """Fake pymodbus client returning/raising a scripted sequence."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.closed = 0

    def connect(self):
        return True

    def close(self):
        self.closed += 1

    def read_holding_registers(self, address, count, device_id):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def scripted(script, retries=2):
    fake = ScriptedClient(script)
    bus = M.SerialBus(M.SerialSettings(port="fake", inter_request_delay_ms=0), client_factory=lambda s: fake)
    return M.ControllerClient(bus, 1, retries=retries, retry_delay_s=0), fake


def test_retry_then_success():
    good = FakeResponse(registers=[7507, 1, 115, 1, 0, 100] + [0] * 14)
    client, fake = scripted([ModbusIOException("t/o"), ModbusIOException("t/o"), good])
    raw = client.read_status_registers()
    assert raw[40006] == 100 and fake.calls == 3


def test_retries_exhausted():
    client, fake = scripted([ModbusIOException("t/o")] * 3, retries=2)
    with pytest.raises(M.CommError) as err:
        client.read_status_registers()
    assert err.value.status == M.COMM_TIMEOUT and fake.calls == 3


def test_permanent_exception_not_retried():
    client, fake = scripted([ExceptionResponse(3, 3)], retries=5)
    client.read_strategy = "block"
    with pytest.raises(M.CommError) as err:
        client.read_status_registers()
    assert err.value.exception_code == 3 and fake.calls == 1


def test_busy_exception_is_retried():
    good = FakeResponse(registers=[0] * 20)
    client, fake = scripted([ExceptionResponse(3, 6), good])
    client.read_status_registers()
    assert fake.calls == 2


def test_short_response_is_invalid():
    client, _ = scripted([FakeResponse(registers=[1, 2, 3])], retries=0)
    with pytest.raises(M.CommError) as err:
        client.read_status_registers()
    assert err.value.status == M.COMM_INVALID


def test_connection_exception_closes_port():
    client, fake = scripted([ConnectionException("unplugged")], retries=0)
    with pytest.raises(M.CommError) as err:
        client.read_status_registers()
    assert err.value.status == M.COMM_PORT and fake.closed == 1


def test_unexpected_library_error_is_contained():
    client, _ = scripted([RuntimeError("boom")], retries=0)
    with pytest.raises(M.CommError) as err:
        client.read_status_registers()
    assert err.value.status == M.COMM_ERROR


def test_missing_port_reports_port_error():
    bus = M.SerialBus(M.SerialSettings(port="/dev/does-not-exist-ttyUSB9", timeout_s=0.2))
    client = M.ControllerClient(bus, 1, retries=0)
    with pytest.raises(M.CommError) as err:
        client.poll()
    assert err.value.status == M.COMM_PORT


def test_invalid_slave_ids_rejected():
    bus = M.SerialBus(M.SerialSettings(port="fake"))
    for bad in (0, 251):
        with pytest.raises(ValueError):
            M.ControllerClient(bus, bad)
