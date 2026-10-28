import sys
from pathlib import Path

import pytest

NODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = NODE_ROOT.parent
for p in (NODE_ROOT, REPO_ROOT / "simulator"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from mock_controller import Bus, MockController, TcpRtuServer  # noqa: E402

from src.modbus_client import ControllerClient, SerialBus, SerialSettings  # noqa: E402


@pytest.fixture
def mock_bus():
    """A simulated RS-485 line with one controller (slave 1, rS=0) served over TCP."""
    controller = MockController(slave_id=1, rs=0, seed=1)
    bus = Bus([controller])
    server = TcpRtuServer(bus).start()
    yield server, bus, controller
    server.stop()


@pytest.fixture
def client_for(mock_bus):
    server, _, _ = mock_bus
    buses = []

    def make(slave_id=1, **kwargs):
        bus = SerialBus(SerialSettings(port=server.url, timeout_s=0.3, inter_request_delay_ms=0))
        buses.append(bus)
        kwargs.setdefault("retry_delay_s", 0.01)
        return ControllerClient(bus, slave_id, **kwargs)

    yield make
    for b in buses:
        b.close()
