"""Server configuration, read from environment variables (and server/.env)."""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

SERVER_ROOT = Path(__file__).resolve().parents[1]
# Must run before the Config class body below reads os.environ.  Real
# environment variables take precedence over values in server/.env.
load_dotenv(SERVER_ROOT / ".env", override=False)
INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", SERVER_ROOT / "instance"))


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _secret_key() -> str:
    """SECRET_KEY from the environment, else a random key persisted in the
    instance folder (so sessions survive restarts without a key in git)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    key_file = INSTANCE_DIR / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    return key_file.read_text(encoding="utf-8").strip()


class Config:
    APP_NAME = os.environ.get("APP_NAME", "Temperature Monitoring")
    SECRET_KEY = None  # resolved lazily in create_app
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'temperature.db').as_posix()}")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Sessions / security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)  # set true when served over HTTPS
    SESSION_COOKIE_NAME = "tm_session"
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=_int("SESSION_TIMEOUT_MINUTES", 30))
    SESSION_REFRESH_EACH_REQUEST = True
    WTF_CSRF_TIME_LIMIT = None  # token valid for the session lifetime
    MAX_CONTENT_LENGTH = _int("MAX_REQUEST_BYTES", 4 * 1024 * 1024)
    LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
    LOGIN_LOCKOUT_MINUTES = _int("LOGIN_LOCKOUT_MINUTES", 15)
    PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 10)
    TRUST_PROXY = _bool("TRUST_PROXY", False)  # true only behind a reverse proxy (Caddy/IIS)

    # Node API
    API_MAX_BATCH = _int("API_MAX_BATCH", 1000)
    BACKFILL_THRESHOLD_S = _int("BACKFILL_THRESHOLD_S", 120)

    # Exports (documented limits - exports beyond them are refused, never truncated)
    EXPORT_MAX_ROWS_XLSX = _int("EXPORT_MAX_ROWS_XLSX", 500_000)
    EXPORT_MAX_ROWS_PDF = _int("EXPORT_MAX_ROWS_PDF", 20_000)
    EXPORT_DIR = os.environ.get("EXPORT_DIR", str(INSTANCE_DIR / "exports"))

    # Defaults for settings editable in the UI (Admin > Settings)
    DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "UTC")
    ENABLE_REMOTE_CONTROL = _bool("ENABLE_REMOTE_CONTROL", False)

    # Logging
    LOG_DIR = os.environ.get("LOG_DIR", str(SERVER_ROOT / "logs"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    LOG_FORMAT = os.environ.get("LOG_FORMAT", "text")
    LOG_MAX_BYTES = _int("LOG_MAX_BYTES", 10_000_000)
    LOG_BACKUP_COUNT = _int("LOG_BACKUP_COUNT", 10)

    # Background monitor
    START_MONITOR = False  # run.py starts it for the real server
    AUTO_MIGRATE = _bool("AUTO_MIGRATE", True)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    WTF_CSRF_ENABLED = False
    SECRET_KEY = "test-secret"
    LOG_DIR = None
    AUTO_MIGRATE = False
    EXPORT_MAX_ROWS_PDF = 5000
