"""Structured, rotating, secret-redacting logging for the node."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

_SECRET_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (re.compile(r"(['\"]?(?:token|password|secret)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.I), r"\1***"),
)


def redact_text(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"


def setup_logging(cfg: dict) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = JsonFormatter() if cfg.get("format") == "json" else logging.Formatter(TEXT_FORMAT)
    handlers: list[logging.Handler] = [logging.StreamHandler()]  # stderr -> journald under systemd
    if cfg.get("file"):
        Path(cfg["file"]).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            cfg["file"], maxBytes=cfg.get("max_bytes", 5_000_000), backupCount=cfg.get("backup_count", 10),
            encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    root.setLevel(cfg.get("level", "INFO"))
    # pymodbus logs every failed transaction at ERROR; our own messages carry the context.
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
