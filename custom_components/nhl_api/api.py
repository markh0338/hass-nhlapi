"""Validated NHL HTTP requests and shared rate-limit cooldowns."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import homeassistant.util.dt as dt_util
from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import API_BASE, API_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUSES = {500, 502, 503, 504}
MAX_RETRIES = 2
BLOCKED_SCHEDULE_STATES = {"PPD", "CNCL", "CANCELLED", "POSTPONED", "SUSP", "SUSPENDED"}


@dataclass
class RateLimit:
    """An NHL cooldown shared by all entries in this Home Assistant instance."""

    until: datetime | None = None

    def active(self) -> bool:
        """Return whether requests must wait."""
        return self.until is not None and self.until > dt_util.utcnow()


def parse_retry_after(value: str | None, now: datetime) -> datetime:
    """Parse either HTTP Retry-After format, with a safe default for bad headers."""
    try:
        seconds = float(value or "")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        return now + timedelta(seconds=max(1, seconds))
    except ValueError, OverflowError:
        try:
            result = parsedate_to_datetime(value or "")
            if result.tzinfo is None:
                raise ValueError
            return max(now + timedelta(seconds=1), result)
        except ValueError, TypeError, OverflowError:
            return now + timedelta(minutes=1)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Expected a list of objects")
    return value


def _identifier(value: Any) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("Missing or invalid identifier")


def _team_score(team: dict[str, Any]) -> None:
    if "score" in team and (type(team["score"]) is not int or team["score"] < 0):
        raise ValueError("Invalid team score")


def _game(game: dict[str, Any]) -> None:
    _identifier(game.get("id"))
    if not isinstance(game.get("gameState"), str) or not game["gameState"]:
        raise ValueError("Missing game state")
    if game.get("gameScheduleState") in BLOCKED_SCHEDULE_STATES:
        return
    start = game.get("startTimeUTC")
    parsed = dt_util.parse_datetime(start) if isinstance(start, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("Missing or invalid game start")


def validate_payload(path: str, value: Any) -> dict[str, Any]:
    """Validate required data before it can change game or goal tracking."""
    data = _object(value)
    if path.startswith("/club-schedule-season/"):
        for game in _objects(data.get("games")):
            _game(game)
        return data

    game_id = int(path.split("/")[2])
    if data.get("id") != game_id:
        raise ValueError("Response belongs to a different game")
    if not isinstance(data.get("gameState"), str) or not data["gameState"]:
        raise ValueError("Missing game state")
    if path.endswith("/landing"):
        _game(data)
        for key in ("homeTeam", "awayTeam"):
            team = _object(data.get(key))
            _identifier(team.get("id"))
            _team_score(team)
            if not isinstance(team.get("abbrev"), str) or not team["abbrev"]:
                raise ValueError("Missing team abbreviation")
            _object(team.get("commonName", {}))
        for key in ("clock", "periodDescriptor", "summary"):
            _object(data.get(key, {}))
        _objects(data.get("tvBroadcasts", []))
        for period in _objects(data.get("summary", {}).get("scoring", [])):
            _objects(period.get("goals", []))
    elif path.endswith("/play-by-play"):
        for player in _objects(data.get("rosterSpots")):
            _identifier(player.get("playerId"))
            _object(player.get("firstName", {}))
            _object(player.get("lastName", {}))
        for play in _objects(data.get("plays")):
            if play.get("typeDescKey") != "goal":
                continue
            _identifier(play.get("eventId"))
            _identifier(_object(play.get("details")).get("eventOwnerTeamId"))
            period = _object(play.get("periodDescriptor"))
            _identifier(period.get("number"))
            if not isinstance(period.get("periodType"), str):
                raise ValueError("Missing period type")
            for key in ("timeInPeriod", "timeRemaining"):
                if key in play and not re.fullmatch(r"\d{2,3}:[0-5]\d", str(play[key])):
                    raise ValueError("Invalid play time")
        for key in ("homeTeam", "awayTeam"):
            if key in data:
                _team_score(_object(data[key]))
    else:
        raise ValueError("Unsupported NHL endpoint")
    return data


class NHLApiClient:
    """Fetch NHL data without retaining connections or sleeping through cooldowns."""

    def __init__(self, session: ClientSession, cooldown: RateLimit) -> None:
        self.session = session
        self.cooldown = cooldown
        self.last_attempt: datetime | None = None
        self.last_success: datetime | None = None
        self.error_count = 0
        self.timeout_count = 0
        self.errors: dict[str, str] = {}

    @property
    def last_error(self) -> str:
        """Keep a failed PBP request visible even when landing requests succeed."""
        return next(reversed(self.errors.values()), "")

    def record_error(self, path: str, message: str, *, optional: bool = False) -> None:
        """Count failures and log only a changed endpoint error."""
        if optional:
            _LOGGER.debug("Optional NHL request %s: %s", path, message)
            return
        self.error_count += 1
        message = f"{message} for {path}"[:255]
        if self.errors.get(path) != message:
            _LOGGER.warning("NHL API %s", message)
        self.errors[path] = message

    async def fetch(
        self, path: str, *, optional: bool = False
    ) -> dict[str, Any] | None:
        """Return validated data, or None while preserving a rate-limit deadline."""
        if self.cooldown.active():
            return None
        message = "Request failed"
        for attempt in range(MAX_RETRIES + 1):
            # Another team's request may have just imposed a cooldown.
            if self.cooldown.active():
                return None
            self.last_attempt = dt_util.utcnow()
            try:
                async with self.session.get(
                    f"{API_BASE}{path}",
                    timeout=ClientTimeout(total=API_TIMEOUT_SECONDS),
                ) as response:
                    if response.status == 429:
                        until = parse_retry_after(
                            response.headers.get("Retry-After"), dt_util.utcnow()
                        )
                        self.cooldown.until = max(self.cooldown.until or until, until)
                        self.record_error(path, "HTTP 429", optional=optional)
                        return None
                    if response.status == 200:
                        data = validate_payload(
                            path, await response.json(content_type=None)
                        )
                        self.last_success = dt_util.utcnow()
                        self.errors.pop(path, None)
                        return data
                    message = f"HTTP {response.status}"
                    if response.status not in RETRYABLE_STATUSES or optional:
                        break
            except TimeoutError:
                if not optional:
                    self.timeout_count += 1
                message = "Timeout"
            except (ClientError, ValueError, TypeError, OverflowError) as err:
                message = f"{type(err).__name__}: {err}"
            if optional or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(0.4 * (2**attempt) + random.uniform(0, 0.2))
        self.record_error(path, message, optional=optional)
        return None
