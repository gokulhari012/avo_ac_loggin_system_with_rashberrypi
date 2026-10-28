"""Temperature monitoring central server (Flask application factory)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import uuid
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request, session
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import HTTPException

from .config import INSTANCE_DIR, SERVER_ROOT, Config, _secret_key  # also loads server/.env
from .extensions import csrf, db, login_manager, migrate

__version__ = "1.0.0"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_SECRET_RE = (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I),
              re.compile(r"((?:password|token|secret)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}&]+", re.I))


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=FULL")   # an acknowledged reading survives power loss
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()


class _RedactFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        red = msg
        for pattern in _SECRET_RE:
            red = pattern.sub(r"\1***", red)
        if red != msg:
            record.msg, record.args = red, ()
        record.request_id = getattr(g, "request_id", "-") if _has_request() else "-"
        return True


def _has_request() -> bool:
    from flask import has_request_context
    return has_request_context()


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                 "logger": record.name, "request_id": getattr(record, "request_id", "-"), "msg": record.getMessage()}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _configure_logging(app: Flask) -> None:
    root = logging.getLogger()
    if getattr(root, "_tm_configured", False):
        return
    root.setLevel(app.config["LOG_LEVEL"])
    fmt = _JsonFormatter() if app.config["LOG_FORMAT"] == "json" else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if app.config.get("LOG_DIR"):
        Path(app.config["LOG_DIR"]).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            Path(app.config["LOG_DIR"]) / "server.log", maxBytes=app.config["LOG_MAX_BYTES"],
            backupCount=app.config["LOG_BACKUP_COUNT"], encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(_RedactFilter())
        root.addHandler(handler)
    root._tm_configured = True


def create_app(config_object: type[Config] | None = None, **overrides) -> Flask:
    app = Flask(__name__, instance_path=str(INSTANCE_DIR), instance_relative_config=False)
    app.config.from_object(config_object or Config)
    app.config.update(overrides)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _secret_key()
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if not app.config.get("TESTING"):
        _configure_logging(app)
    if app.config.get("TRUST_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, directory=str(SERVER_ROOT / "migrations"), render_as_batch=True)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.session_protection = "strong"

    from . import models  # noqa: F401  (register models)
    from .models import User

    @login_manager.user_loader
    def load_user(user_key: str):
        try:
            user_id, version = (int(x) for x in user_key.split(":", 1))
        except ValueError:
            return None
        user = db.session.get(User, user_id)
        if user is None or user.session_version != version or not user.is_active:
            return None
        return user

    from .routes import register_blueprints
    register_blueprints(app)
    _register_hooks(app)
    _register_template_helpers(app)
    from .cli import register_cli
    register_cli(app)
    return app


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def _request_context():
        incoming = request.headers.get("X-Request-ID", "")
        g.request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        if not request.path.startswith("/api/"):
            session.permanent = True  # idle timeout = PERMANENT_SESSION_LIFETIME

    @app.after_request
    def _headers(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'")
        if request.path.startswith("/ui-api/") or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.teardown_request
    def _rollback_on_error(exc):
        if exc is not None:
            db.session.rollback()

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        if request.path.startswith("/api/") or request.path.startswith("/ui-api/"):
            return jsonify({"error": {"code": exc.name.lower().replace(" ", "_"), "message": exc.description,
                                      "request_id": g.get("request_id")}}), exc.code
        return render_template("errors/error.html", code=exc.code, title=exc.name,
                               message=exc.description), exc.code

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        db.session.rollback()
        logging.getLogger("app").exception("unhandled error on %s %s", request.method, request.path)
        if request.path.startswith("/api/") or request.path.startswith("/ui-api/"):
            return jsonify({"error": {"code": "internal_error", "message": "internal server error",
                                      "request_id": g.get("request_id")}}), 500
        return render_template("errors/error.html", code=500, title="Internal error",
                               message="The server could not complete the request. The error was logged with "
                                       f"reference {g.get('request_id')}."), 500


def _register_template_helpers(app: Flask) -> None:
    from .services.security import PERMISSIONS
    from .services.settings import get_setting
    from .services.timeutil import duration_text, fmt_local, iso_utc

    @app.context_processor
    def _inject():
        def can(code: str) -> bool:
            return bool(getattr(current_user, "is_authenticated", False) and current_user.can(code))
        return {"app_name": app.config["APP_NAME"], "app_version": __version__, "can": can,
                "display_tz": get_setting("display_timezone") if _has_request() else "UTC",
                "all_permissions": PERMISSIONS}

    app.jinja_env.filters["localtime"] = lambda dt, fmt="%Y-%m-%d %H:%M:%S": fmt_local(dt, fmt) if dt else "—"
    app.jinja_env.filters["isoutc"] = iso_utc
    app.jinja_env.filters["duration"] = duration_text

    def temp(value, digits=1):
        return "—" if value is None else f"{value:.{digits}f} °C"

    def onoff(value, on="ON", off="OFF"):
        return "—" if value is None else (on if value else off)

    app.jinja_env.filters["temp"] = temp
    app.jinja_env.filters["onoff"] = onoff
    app.jinja_env.filters["fromjson"] = lambda s: json.loads(s) if s else None
    app.jinja_env.filters["pretty"] = lambda v: json.dumps(v, indent=2, sort_keys=True, default=str) \
        if v not in (None, "") else ""
