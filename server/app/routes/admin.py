from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf import FlaskForm

from ..extensions import db
from ..forms import UserForm
from ..models import Permission, Role, User
from ..services.audit import audit, diff
from ..services.security import PERMISSIONS, permission_required
from ..services.settings import SETTINGS, all_settings, set_setting
from ..services.timeutil import utcnow
from .auth import password_problems

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _user_state(user: User) -> dict:
    return {"username": user.username, "full_name": user.full_name, "email": user.email,
            "role": user.role.name if user.role else None, "active": user.is_active_flag}


def _active_admins(exclude_id: int | None = None) -> int:
    q = User.query.join(Role).filter(User.is_active_flag.is_(True), Role.permissions.any(code="manage_users"))
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return q.count()


@bp.get("/users")
@permission_required("manage_users")
def users():
    return render_template("admin/users.html", users=User.query.order_by(User.username).all(), now=utcnow())


@bp.route("/users/new", methods=["GET", "POST"])
@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@permission_required("manage_users")
def user_form(user_id: int | None = None):
    user = db.session.get(User, user_id) if user_id else None
    if user_id and user is None:
        abort(404)
    form = UserForm(obj=user)
    form.role_id.choices = [(r.id, r.name) for r in Role.query.order_by(Role.name)]
    if request.method == "GET" and user is not None:
        form.is_active.data = user.is_active_flag
    if form.validate_on_submit():
        username = form.username.data.strip()
        clash = User.query.filter(User.username == username, User.id != (user.id if user else 0)).first()
        new_role = db.session.get(Role, form.role_id.data)
        errors = []
        if clash:
            errors.append("Username already in use.")
        if user is None and not form.password.data:
            errors.append("A password is required for a new user.")
        if form.password.data and (problems := password_problems(form.password.data, username)):
            errors.append("Password needs: " + "; ".join(problems))
        if user is not None and user.id == current_user.id and (not form.is_active.data or
                                                                "manage_users" not in new_role.permission_codes()):
            errors.append("You cannot deactivate yourself or remove your own user-management permission.")
        losing_admin = user is not None and user.can("manage_users") and (
            not form.is_active.data or "manage_users" not in new_role.permission_codes())
        if losing_admin and _active_admins(exclude_id=user.id) == 0:
            errors.append("At least one active administrator must remain.")
        if errors:
            for e in errors:
                flash(e, "danger")
        else:
            before = _user_state(user) if user else {}
            if user is None:
                user = User(username=username)
                db.session.add(user)
            user.username, user.full_name = username, form.full_name.data or ""
            user.email, user.role = form.email.data or None, new_role
            if user.is_active_flag != bool(form.is_active.data) or (before and before["role"] != new_role.name):
                user.session_version = (user.session_version or 0) + 1   # force re-login with new rights
            user.is_active_flag = bool(form.is_active.data)
            if form.password.data:
                user.set_password(form.password.data)
                user.failed_login_count, user.locked_until = 0, None
            db.session.flush()
            previous, new = diff(before, _user_state(user))
            if form.password.data:
                new["password"] = "changed"
            audit("user_admin", "user_created" if not before else "user_updated", resource_type="user",
                  resource_id=user.username, previous=previous, new=new, reason=form.reason.data)
            db.session.commit()
            flash(f"User {user.username} saved.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, user=user)


@bp.route("/roles", methods=["GET", "POST"])
@permission_required("manage_users")
def roles():
    form = FlaskForm()
    all_roles = Role.query.order_by(Role.name).all()
    perms = {p.code: p for p in Permission.query.all()}
    if form.validate_on_submit():
        reason = (request.form.get("reason") or "")[:500]
        for role in all_roles:
            wanted = {code for code in PERMISSIONS if request.form.get(f"perm-{role.id}-{code}") == "on"}
            current = role.permission_codes()
            if wanted == current:
                continue
            if role.id == current_user.role_id and "manage_users" not in wanted:
                flash(f"You cannot remove 'manage_users' from your own role ({role.name}).", "danger")
                continue
            role.permissions = [perms[c] for c in sorted(wanted) if c in perms]
            audit("user_admin", "role_permissions_changed", resource_type="role", resource_id=role.name,
                  previous=sorted(current), new=sorted(wanted), reason=reason)
        db.session.commit()
        flash("Role permissions saved.", "success")
        return redirect(url_for("admin.roles"))
    return render_template("admin/roles.html", roles=all_roles, form=form, permissions=PERMISSIONS)


@bp.route("/settings", methods=["GET", "POST"])
@permission_required("manage_settings")
def settings():
    form = FlaskForm()
    values = all_settings()
    if form.validate_on_submit():
        reason = (request.form.get("reason") or "")[:500]
        changed = 0
        for key, definition in SETTINGS.items():
            if key == "remote_control_enabled":   # checkbox
                raw = "true" if request.form.get(key) == "on" else "false"
            else:
                raw = (request.form.get(key) or "").strip()
                if raw == "":
                    continue
            try:
                if definition.parse(raw) == values[key]:
                    continue
                previous, new = set_setting(key, raw, current_user.username)
            except (ValueError, TypeError) as exc:
                flash(f"{definition.label}: {exc}", "danger")
                continue
            if previous != new:
                changed += 1
                audit("settings", "setting_changed", resource_type="setting", resource_id=key,
                      previous={key: previous}, new={key: new}, reason=reason)
        db.session.commit()
        flash(f"{changed} setting(s) changed." if changed else "No changes.", "success" if changed else "info")
        return redirect(url_for("admin.settings"))
    return render_template("admin/settings.html", form=form, settings=SETTINGS, values=values)
