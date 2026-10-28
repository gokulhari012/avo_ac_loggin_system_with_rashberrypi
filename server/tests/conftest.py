import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import create_app  # noqa: E402
from app.cli import create_user, seed_roles  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Node  # noqa: E402
from app.models.audit import AUDIT_TRIGGERS_SQLITE  # noqa: E402
from app.services.node_config import create_config_version, default_config  # noqa: E402
from app.services.security import hash_token  # noqa: E402

PASSWORD = "Correct-Horse-42"


@pytest.fixture
def app(tmp_path):
    app = create_app(TestConfig, EXPORT_DIR=str(tmp_path / "exports"))

    # Tests hold one app context open (for direct DB access), so Flask's `g`
    # would leak between requests/clients.  Production gets a fresh app
    # context per request; emulate that for per-request caches.
    @app.before_request
    def _fresh_request_globals():
        from flask import g
        g.pop("_login_user", None)
        g.pop("_settings_cache", None)

    with app.app_context():
        db.create_all()
        for statement in AUDIT_TRIGGERS_SQLITE:
            db.session.execute(text(statement))
        seed_roles()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def users(app):
    return {role: create_user(name, PASSWORD, role) for name, role in
            (("admin", "Administrator"), ("operator", "Operator"), ("auditor", "Auditor"))}


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


@pytest.fixture
def as_user(client, users):
    def _login(role_user):
        client.post("/logout")
        response = login(client, role_user)
        assert response.status_code == 302, response.data[:500]
        return client
    return _login


def utc_iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_node(uid="NODE-1", token="node-token-0123456789", with_config=True, **kwargs) -> tuple[Node, str]:
    node = Node(uid=uid, name=kwargs.get("name", uid), site=kwargs.get("site", "Plant A"),
                equipment=kwargs.get("equipment", "Cold Room"), token_hash=hash_token(token), token_hint=token[-4:])
    db.session.add(node)
    db.session.flush()
    if with_config:
        create_config_version(node, default_config(node), reason="test")
    db.session.commit()
    return node, token


def node_headers(uid="NODE-1", token="node-token-0123456789"):
    return {"X-Node-ID": uid, "Authorization": f"Bearer {token}"}


def reading(controller="ctrl-1", at: datetime | None = None, temp=-18.2, **overrides) -> dict:
    at = at or utcnow()
    r = {
        "reading_id": str(uuid.uuid4()), "controller_id": controller, "slave_id": 1, "model": "SZ-7526TX-REA",
        "acquired_at": utc_iso(at), "source": "controller", "config_version": 1, "comm_status": "ok",
        "comm_error": None, "quality": "good", "quality_flags": [], "temperature_c": temp,
        "display_temperature_c": temp, "actual_temperature_c": temp, "setpoint_c": -18.0,
        "temp_log_high_c": -15.0, "temp_log_low_c": -20.0, "resolution_rs": 0, "compressor_on": True,
        "alarm_on": False, "defrost_active": False, "keypad_locked": False, "fault_flags": 0, "faults": [],
        "system_status": 4, "relay_status": 1, "compressor_run_hours": 12,
        "raw_registers": {"40001": 7507, "40003": 115, "40006": (round(temp * 10)) & 0xFFFF if temp is not None else 0},
    }
    r.update(overrides)
    return r


def comm_failure(controller="ctrl-1", at=None, status="timeout") -> dict:
    r = reading(controller, at, temp=None)
    for key in list(r):
        if key.endswith("_c") or key in ("compressor_on", "alarm_on", "defrost_active", "keypad_locked",
                                         "fault_flags", "system_status", "relay_status", "compressor_run_hours",
                                         "resolution_rs"):
            r[key] = None
    r.update(comm_status=status, comm_error="no response", quality="comm_error", raw_registers=None)
    return r
