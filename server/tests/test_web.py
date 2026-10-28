"""Web UI: authentication, sessions, permissions, CSRF, headers, page rendering."""

import pytest
from sqlalchemy import text

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import AuditEvent, Controller, User
from tests.conftest import PASSWORD, login, make_node, node_headers, reading

PAGES = ["/", "/monitor", "/logs", "/nodes", "/events", "/reports", "/audit", "/exports", "/admin/users",
         "/admin/roles", "/admin/settings", "/account/password", "/nodes/new"]


def seed_data(client):
    make_node()
    client.post("/api/v1/readings", json={"readings": [reading(), reading(controller="ctrl-2", temp=None,
                                                                          quality="probe_fault",
                                                                          faults=["probe_fail_high"])]},
                headers=node_headers())


def test_anonymous_redirected_to_login(client, users):
    for page in ("/", "/logs", "/nodes", "/audit"):
        r = client.get(page)
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_ui_api_requires_login(client, users):
    assert client.get("/ui-api/dashboard").status_code == 302


def test_login_success_and_logout_audited(client, users):
    assert login(client, "admin").status_code == 302
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/").status_code == 302
    actions = [a.action for a in AuditEvent.query.filter_by(category="auth").order_by(AuditEvent.id)]
    assert "login" in actions and "logout" in actions


def test_failed_login_audited_without_password(client, users):
    r = login(client, "admin", "wrong-password-1")
    assert r.status_code == 200 and b"Invalid username or password" in r.data
    event = AuditEvent.query.filter_by(action="login_failed").one()
    assert "wrong-password-1" not in (event.details_json or "")


def test_lockout_after_repeated_failures(client, users, app):
    for _ in range(app.config["LOGIN_MAX_FAILURES"]):
        login(client, "operator", "nope-nope-123")
    r = login(client, "operator")  # correct password, but locked
    assert b"locked" in r.data
    assert client.get("/").status_code == 302


def test_open_redirect_blocked(client, users):
    r = client.post("/login?next=https://evil.example/", data={"username": "admin", "password": PASSWORD})
    assert r.headers["Location"] == "/"


def test_all_pages_render_for_admin(as_user, users):
    client = as_user("admin")
    seed_data(client)
    node_id = db.session.execute(text("select id from nodes")).scalar()
    ctrl_id = Controller.query.filter_by(uid="ctrl-1").one().id
    for page in PAGES + [f"/nodes/{node_id}", f"/nodes/{node_id}/config", f"/nodes/{node_id}/config/1",
                         f"/controllers/{ctrl_id}", f"/ui-api/series?controller={ctrl_id}", "/ui-api/dashboard"]:
        r = client.get(page)
        assert r.status_code == 200, (page, r.status_code, r.data[:300])


@pytest.mark.parametrize("role,allowed,denied", [
    ("operator", ["/", "/logs", "/monitor", "/nodes", "/reports", "/exports"],
     ["/audit", "/admin/users", "/admin/settings", "/nodes/new"]),
    ("auditor", ["/", "/logs", "/audit", "/reports", "/exports"],
     ["/admin/users", "/admin/roles", "/admin/settings", "/nodes/new"]),
])
def test_role_permissions_enforced_server_side(as_user, users, role, allowed, denied):
    client = as_user(role)
    for page in allowed:
        assert client.get(page).status_code == 200, page
    for page in denied:
        assert client.get(page).status_code == 403, page
    assert AuditEvent.query.filter_by(action="permission_denied").count() >= len(denied)


def test_operator_cannot_post_admin_actions(as_user, users):
    client = as_user("operator")
    make_node()
    assert client.post("/nodes/1/disable", data={"reason": "x"}).status_code == 403
    assert client.post("/admin/settings", data={"display_timezone": "UTC"}).status_code == 403


def test_csrf_enforced(tmp_path):
    app = create_app(TestConfig, WTF_CSRF_ENABLED=True, EXPORT_DIR=str(tmp_path))
    with app.app_context():
        db.create_all()
        from app.cli import create_user, seed_roles
        seed_roles()
        create_user("admin", PASSWORD, "Administrator")
        client = app.test_client()
        r = client.post("/login", data={"username": "admin", "password": PASSWORD})
        assert r.status_code == 400  # missing CSRF token
        db.session.remove()
        db.drop_all()


def test_security_headers(client, users):
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Request-ID"]


def test_session_cookie_flags(client, users):
    r = login(client, "admin")
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_password_change_invalidates_old_sessions(app, users):
    first, second = app.test_client(), app.test_client()
    login(first, "admin")
    login(second, "admin")
    r = first.post("/account/password", data={"current_password": PASSWORD, "new_password": "Another-Pass-77",
                                              "confirm": "Another-Pass-77"})
    assert r.status_code == 302
    assert first.get("/").status_code == 200      # this session continues
    assert second.get("/").status_code == 302     # other session signed out


def test_errors_do_not_leak_tracebacks(as_user, users, app, monkeypatch):
    client = as_user("admin")

    def boom():
        raise RuntimeError("secret internals")

    monkeypatch.setitem(app.view_functions, "dashboard.index", boom)
    r = client.get("/")
    assert r.status_code == 500 and b"secret internals" not in r.data and b"Traceback" not in r.data


def test_admin_cannot_remove_last_admin(as_user, users):
    client = as_user("admin")
    admin = User.query.filter_by(username="admin").one()
    operator_role = User.query.filter_by(username="operator").one().role_id
    r = client.post(f"/admin/users/{admin.id}", data={"username": "admin", "role_id": operator_role,
                                                      "is_active": "y"})
    assert r.status_code == 200 and b"cannot" in r.data
    assert User.query.filter_by(username="admin").one().role.name == "Administrator"


def test_admin_creates_user_and_audit_has_no_password(as_user, users):
    client = as_user("admin")
    role_id = User.query.filter_by(username="operator").one().role_id
    r = client.post("/admin/users/new", data={"username": "newop", "full_name": "New Op", "role_id": role_id,
                                              "is_active": "y", "password": "Strong-Pass-123", "reason": "hire"})
    assert r.status_code == 302
    event = AuditEvent.query.filter_by(action="user_created", resource_id="newop").one()
    assert "Strong-Pass-123" not in (event.new_value or "") and event.reason == "hire"


def test_weak_password_rejected(as_user, users):
    client = as_user("admin")
    role_id = User.query.filter_by(username="operator").one().role_id
    r = client.post("/admin/users/new", data={"username": "weak", "role_id": role_id, "is_active": "y",
                                              "password": "short"})
    assert r.status_code == 200 and User.query.filter_by(username="weak").first() is None


def test_settings_change_audited_and_validated(as_user, users):
    client = as_user("admin")
    client.post("/admin/settings", data={"display_timezone": "Asia/Kolkata", "reason": "site in India"})
    event = AuditEvent.query.filter_by(action="setting_changed").one()
    assert event.new == {"display_timezone": "Asia/Kolkata"}
    r = client.post("/admin/settings", data={"display_timezone": "Mars/Base"}, follow_redirects=True)
    assert b"unknown time zone" in r.data


def test_role_permission_change_audited(as_user, users):
    client = as_user("admin")
    from app.models import Role
    operator = Role.query.filter_by(name="Operator").one()
    data = {f"perm-{operator.id}-{p.code}": "on" for p in operator.permissions}
    data[f"perm-{operator.id}-view_audit"] = "on"
    for role in Role.query.filter(Role.id != operator.id):
        data.update({f"perm-{role.id}-{p.code}": "on" for p in role.permissions})
    client.post("/admin/roles", data={**data, "reason": "operators review audit"})
    assert "view_audit" in Role.query.filter_by(name="Operator").one().permission_codes()
    assert AuditEvent.query.filter_by(action="role_permissions_changed", resource_id="Operator").count() == 1


def test_node_page_renders_with_minimal_heartbeat(as_user, users, client):
    """A heartbeat with missing sections (old/odd node software) must not break the node page."""
    node, _ = make_node()
    client.post("/api/v1/heartbeat", json={"node_id": "NODE-1"}, headers=node_headers())
    assert as_user("operator").get(f"/nodes/{node.id}").status_code == 200
