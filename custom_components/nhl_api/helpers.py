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


def goal_order(play: dict[str, Any], *, use_sort_order: bool = True) -> tuple[int, int]:
    """Use NHL chronology; event IDs identify plays, not their order."""
    sort_order = play.get("sortOrder")
    if use_sort_order and isinstance(sort_order, int):
        return sort_order, 0
    elapsed = str(play.get("timeInPeriod") or "00:00").split(":")
    seconds = int(elapsed[0]) * 60 + int(elapsed[1])
    return int(play["periodDescriptor"]["number"]), seconds


def goal_strength(play: dict[str, Any], landing: dict[str, Any]) -> str:
    """Normalize summary strength, falling back only to unambiguous manpower."""
    if play.get("periodDescriptor", {}).get("periodType") == "SO":
        return "SO"
    mapping = {
        "ev": "EVEN",
        "even": "EVEN",
        "pp": "PPG",
        "ppg": "PPG",
        "sh": "SHG",
        "shg": "SHG",
    }
    for period in landing.get("summary", {}).get("scoring", []):
        for goal in period.get("goals", []):
            if goal.get("eventId") == play.get("eventId"):
                if strength := mapping.get(str(goal.get("strength", "")).lower()):
                    return strength
    if strength := mapping.get(
        str(play.get("details", {}).get("strength", "")).lower()
    ):
        return strength
    code = str(play.get("situationCode", ""))
    # With a goalie pulled, skater counts alone cannot establish penalty strength.
    if len(code) != 4 or not code.isdigit() or code[0] != "1" or code[3] != "1":
        return ""
    away, home = int(code[1]), int(code[2])
    owner = play["details"].get("eventOwnerTeamId")
    if owner == landing["awayTeam"]["id"]:
        advantage = away - home
    elif owner == landing["homeTeam"]["id"]:
        advantage = home - away
    else:
        return ""
    return "PPG" if advantage > 0 else "SHG" if advantage < 0 else "EVEN"
