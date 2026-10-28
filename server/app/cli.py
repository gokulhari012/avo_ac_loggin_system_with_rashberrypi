"""Flask CLI commands:  flask --app run <command>"""

from __future__ import annotations

import getpass

import click
from flask import Flask

from .extensions import db
from .models import Permission, Role, User
from .services.audit import audit
from .services.security import PERMISSIONS, ROLE_DEFAULTS


def seed_roles() -> None:
    """Create missing permissions and default roles (idempotent; never removes grants)."""
    perms = {p.code: p for p in Permission.query.all()}
    for code, description in PERMISSIONS.items():
        if code not in perms:
            perms[code] = Permission(code=code, description=description)
            db.session.add(perms[code])
    for name, (description, codes) in ROLE_DEFAULTS.items():
        role = Role.query.filter_by(name=name).first()
        if role is None:
            role = Role(name=name, description=description, permissions=[perms[c] for c in sorted(codes)])
            db.session.add(role)
    db.session.commit()


def create_user(username: str, password: str, role_name: str, full_name: str = "") -> User:
    from .routes.auth import password_problems

    if User.query.filter_by(username=username).first():
        raise click.ClickException(f"user {username!r} already exists")
    problems = password_problems(password, username)
    if problems:
        raise click.ClickException("password needs: " + "; ".join(problems))
    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        raise click.ClickException(f"unknown role {role_name!r}")
    user = User(username=username, full_name=full_name, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    audit("user_admin", "user_created", actor_type="system", actor_name="cli", resource_type="user",
          resource_id=username, new={"username": username, "role": role_name, "full_name": full_name})
    db.session.commit()
    return user


def register_cli(app: Flask) -> None:
    @app.cli.command("seed")
    def seed_command():
        """Create default roles and permissions."""
        seed_roles()
        click.echo("roles and permissions are up to date")

    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--full-name", default="")
    @click.option("--role", default="Administrator", show_default=True)
    @click.option("--password", default=None, help="omit to be prompted (recommended)")
    def create_admin_command(username, full_name, role, password):
        """Create a user (Administrator by default)."""
        seed_roles()
        if password is None:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                raise click.ClickException("passwords do not match")
        create_user(username, password, role, full_name)
        click.echo(f"user {username} created with role {role}")
