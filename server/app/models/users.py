from __future__ import annotations

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from .common import utcnow

role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("permission_id", db.Integer, db.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(db.Model):
    __tablename__ = "permissions"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    permissions = db.relationship("Permission", secondary=role_permissions, lazy="selectin")

    def permission_codes(self) -> set[str]:
        return {p.code for p in self.permissions}


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(128), nullable=False, default="")
    email = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    role = db.relationship("Role", lazy="joined")
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime, default=utcnow)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    # Bumped on password change/deactivation so existing sessions are invalidated.
    session_version = db.Column(db.Integer, nullable=False, default=1)

    @property
    def is_active(self) -> bool:  # Flask-Login
        return bool(self.is_active_flag)

    def get_id(self) -> str:
        return f"{self.id}:{self.session_version}"

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)
        self.password_changed_at = utcnow()
        self.session_version = (self.session_version or 0) + 1

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def can(self, permission: str) -> bool:
        return self.is_active and self.role is not None and permission in self.role.permission_codes()

    @property
    def display_name(self) -> str:
        return self.full_name or self.username
