"""UTC time helpers compatible with the project's existing naive DB columns."""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return current UTC without tzinfo for existing SQLAlchemy DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
