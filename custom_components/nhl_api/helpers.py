"""Small shared helpers for the NHL API integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util


def normalize_team_abbrev(value: Any) -> str:
    """Normalize a team abbreviation from config/user input."""
    return str(value).strip().upper()


def get_season_id(now_utc: datetime) -> str:
    """Return season id string (for example, 20252026)."""
    start_year = now_utc.year if now_utc.month >= 9 else now_utc.year - 1
    return f"{start_year}{start_year + 1}"


def get_next_season_id(now_utc: datetime) -> str:
    """Return the next season id string."""
    start_year = now_utc.year + 1 if now_utc.month >= 9 else now_utc.year
    return f"{start_year}{start_year + 1}"


def is_same_local_day(first: datetime | None, second: datetime | None) -> bool:
    """Return True when both datetimes fall on the same local calendar day."""
    if first is None or second is None:
        return False
    return dt_util.as_local(first).date() == dt_util.as_local(second).date()
