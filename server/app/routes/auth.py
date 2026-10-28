from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..forms import LoginForm, PasswordChangeForm
from ..models import User
from ..services.audit import audit
from ..services.timeutil import utcnow

bp = Blueprint("auth", __name__)


def password_problems(password: str, username: str = "") -> list[str]:
    problems = []
    if len(password) < current_app.config["PASSWORD_MIN_LENGTH"]:
        problems.append(f"at least {current_app.config['PASSWORD_MIN_LENGTH']} characters")
    if password.isdigit() or password.isalpha():
        problems.append("a mix of letters and digits or symbols")
    if username and username.lower() in password.lower():
        problems.append("must not contain the username")
    return problems


def _safe_next(target: str | None) -> str:
    if target:
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return url_for("dashboard.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        user = User.query.filter_by(username=username).first()
        now = utcnow()
        if user and user.locked_until and user.locked_until > now:
            audit("auth", "login_failed", actor_type="user", actor_id=user.id, actor_name=username,
                  outcome="denied", details={"reason": "account locked"}, commit=True)
            flash("Account temporarily locked after repeated failures. Try again later.", "danger")
        elif user and user.is_active and user.check_password(form.password.data):
            user.failed_login_count, user.locked_until, user.last_login_at = 0, None, now
            login_user(user)
            audit("auth", "login", actor_type="user", actor_id=user.id, actor_name=user.username, commit=True)
            return redirect(_safe_next(request.args.get("next")))
        else:
            details = {"reason": "unknown user" if user is None else
                       ("inactive user" if not user.is_active else "wrong password")}
            if user is not None:
                user.failed_login_count = (user.failed_login_count or 0) + 1
                if user.failed_login_count >= current_app.config["LOGIN_MAX_FAILURES"]:
                    user.locked_until = now + timedelta(minutes=current_app.config["LOGIN_LOCKOUT_MINUTES"])
                    user.failed_login_count = 0
                    details["locked_until"] = user.locked_until.isoformat()
            audit("auth", "login_failed", actor_type="user", actor_id=user.id if user else None,
                  actor_name=username[:64], outcome="failure", details=details, commit=True)
            flash("Invalid username or password.", "danger")
    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    audit("auth", "logout", commit=True)
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = PasswordChangeForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            audit("auth", "password_change", outcome="failure", details={"reason": "wrong current password"},
                  commit=True)
            flash("Current password is incorrect.", "danger")
        elif problems := password_problems(form.new_password.data, current_user.username):
            flash("New password needs: " + "; ".join(problems), "danger")
        else:
            user = db.session.get(User, current_user.id)
            user.set_password(form.new_password.data)
            audit("auth", "password_change", resource_type="user", resource_id=user.username)
            db.session.commit()
            login_user(user)  # session_version changed; keep this session, invalidate others
            flash("Password changed.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("account/password.html", form=form)
