from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Integer

# BIGINT primary keys do not auto-increment on SQLite; use INTEGER there.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    """Naive UTC 'now' - the only form stored in the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_uid() -> str:
    return str(uuid.uuid4())


def dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default
