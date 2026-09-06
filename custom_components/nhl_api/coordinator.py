"""NHL polling, game selection, and goal tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BLOCKED_SCHEDULE_STATES, NHLApiClient, RateLimit, validate_payload
from .const import DEFAULT_POSTGAME_MINUTES, DOMAIN
from .helpers import (
    get_next_season_id,
    get_season_id,
    goal_order,
    goal_strength,
    is_same_local_day,
)

_LOGGER = logging.getLogger(__name__)
type NHLConfigEntry = ConfigEntry[NHLDataUpdateCoordinator]

PREGAME_SCAN_INTERVAL = timedelta(seconds=10)
LIVE_SCAN_INTERVAL = timedelta(seconds=2)
SCHEDULE_REFRESH_SAME_DAY = timedelta(minutes=10)
SCHEDULE_REFRESH_PREGAME = timedelta(minutes=5)
SCHEDULE_REFRESH_LIVE = timedelta(minutes=15)
SCHEDULE_REFRESH_POSTGAME = timedelta(minutes=15)
SCHEDULE_REFRESH_IDLE = timedelta(minutes=60)
FUTURE_GAME_SCAN_INTERVAL_SAME_DAY = timedelta(minutes=10)
FUTURE_GAME_SCAN_INTERVAL_IDLE = timedelta(minutes=60)
ERROR_RETRY_INTERVAL = timedelta(minutes=1)

POSTGAME_SCAN_INTERVAL = timedelta(seconds=30)
POSTGAME_MAX_WAIT = timedelta(hours=1)
NEXT_SEASON_CACHE_INTERVAL = timedelta(hours=6)

LIVE_GAME_STATES = frozenset({"LIVE", "CRIT"})
POSTGAME_STATES = frozenset({"FINAL", "OFF"})
GOAL_TRACKING_GAME_STATES = frozenset({*LIVE_GAME_STATES, *POSTGAME_STATES})


@dataclass(slots=True)
class NHLSensorData:
    """In-memory data exposed by the coordinator."""

    state: str
    attrs: dict[str, Any]
    tracked_game_state: str | None


class NHLDataUpdateCoordinator(DataUpdateCoordinator[NHLSensorData]):
    """Coordinate NHL API data fetching and dynamic scheduling.

    The coordinator manages two related cadences:

    - Game polling, which controls how often the tracked gamecenter payload is
      refreshed.
    - Schedule lookups, which control when the team schedule is searched for a
      better game to track.

    These move independently so a live game can be refreshed quickly without
    repeatedly walking the club schedule endpoint.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        team_abbrev: str,
        name: str,
        scan_interval: timedelta,
        *,
        config_entry: ConfigEntry | None = None,
        postgame_minutes: int = DEFAULT_POSTGAME_MINUTES,
    ) -> None:
        """Initialize per-team state; share only the API cooldown across teams."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"nhl_sensor_{team_abbrev.upper()}",
            update_interval=None,
            always_update=True,
        )
        cooldown = hass.data.setdefault(f"{DOMAIN}_rate_limit", RateLimit())
        self.api = NHLApiClient(async_get_clientsession(hass), cooldown)
        self.team_abbrev = team_abbrev.upper()
        self.live_scan_interval = max(scan_interval, LIVE_SCAN_INTERVAL)
        self.postgame_grace = timedelta(minutes=postgame_minutes)
        self._game_id: int | None = None
        self._tracked_game_state: str | None = None
        self._tracked_game_start: datetime | None = None
        self._goal_tracking_initialized = False
        self._seen_goal_event_ids: set[str] = set()
        self._last_goal_event_id: str | None = None
        self._last_goal_attributes: dict[str, Any] = {}
        self.goal_feed_available: bool | None = None
        self.last_good_pbp_refresh: datetime | None = None
        self._next_pbp_lookup = dt_util.utcnow()
        self._postgame_started: datetime | None = None
        self._final_pbp_received = False
        self._retired_game_ids: set[int] = set()
        self._next_schedule_lookup = dt_util.utcnow()
        self._schedule_retry_pending = False
        self._next_season_id: str | None = None
        self._next_season_cache: dict[str, Any] | None = None
        self._next_season_failed = False
        self._next_season_lookup = dt_util.utcnow()
        self._schedule_unsub: Callable[[], None] | None = None
        self._refresh_tasks: set[asyncio.Task] = set()
        self._last_refresh_started_utc: datetime | None = None
        self._last_refresh_duration_ms = 0
        self._observed_refresh_interval_seconds: float | None = None
        self._refresh_count = 0
        self._last_good_game_refresh_utc: datetime | None = None
        self._next_update_utc: datetime | None = None
        self._diagnostic_publish_token = 0
        self._manual_refresh_count = 0
        self._consecutive_refresh_failures = 0
        self._logged_unknown_game_states: set[str | None] = set()
        self._started = False

    async def async_start(self) -> None:
        """Start only after platform setup, preserving failure retry cadence."""
        if self._started or self._shutdown_requested:
            return
        self._started = True
        self._schedule_next_update(dt_util.utcnow())

    async def async_shutdown(self) -> None:
        """Stop timers, queued requests and in-flight work before unloading."""
        await super().async_shutdown()
        self._started = False
        self._cancel_schedule()
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._refresh_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @callback
    def _cancel_schedule(self) -> None:
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub = None
        self._next_update_utc = None

    @callback
    def async_add_listener(
        self, update_callback: Callable[[], None], context: Any = None
    ) -> Callable[[], None]:
        """Stop custom polling when the last enabled entity unsubscribes."""
        remove_listener = super().async_add_listener(update_callback, context)
        if self._schedule_unsub is None:
            self._schedule_next_update(dt_util.utcnow())

        @callback
        def remove() -> None:
            remove_listener()
            if not self._listeners:
                self._cancel_schedule()

        return remove

    async def async_refresh(self) -> None:
        """Own queued manual/scheduled refreshes as well as their HTTP work."""
        task = asyncio.current_task()
        self._refresh_tasks.add(task)
        try:
            if not self._shutdown_requested and not self.hass.is_stopping:
                await super().async_refresh()
        finally:
            self._refresh_tasks.discard(task)

    async def async_manual_refresh(self) -> None:
        """Refresh schedules and scores now without shortening an NHL cooldown."""
        if self._shutdown_requested:
            return
        self._diagnostic_publish_token += 1
        self._manual_refresh_count += 1
        scheduled_run = self._next_update_utc
        self._next_schedule_lookup = dt_util.utcnow()
        self._next_pbp_lookup = dt_util.utcnow()
        await self.async_refresh()
        if (
            scheduled_run is not None
            and self.last_update_success
            and scheduled_run > dt_util.utcnow()
            and self._next_update_utc is not None
            and scheduled_run < self._next_update_utc
        ):
            self._schedule_next_update(dt_util.utcnow(), deadline=scheduled_run)

    @callback
    def _schedule_next_update(
        self, now: datetime, *, deadline: datetime | None = None
    ) -> None:
        """Schedule only while enabled and never before the NHL cooldown ends."""
        self._cancel_schedule()
        if (
            not self._started
            or self._shutdown_requested
            or self.hass.is_stopping
            or not self._listeners
            or (self.config_entry and self.config_entry.pref_disable_polling)
        ):
            return
        next_run = deadline or now + self.effective_polling_delta
        if self.api.cooldown.active():
            next_run = max(next_run, self.api.cooldown.until)
        self._next_update_utc = next_run
        self._schedule_unsub = async_track_point_in_utc_time(
            self.hass, self._handle_scheduled_refresh, next_run
        )

    @callback
    def _handle_scheduled_refresh(self, _now: datetime) -> None:
        """Keep the scheduled task owned by this entry and coordinator."""
        self._schedule_unsub = None
        self._next_update_utc = None
        if (
            self._shutdown_requested
            or self.hass.is_stopping
            or not self._started
            or not self._listeners
        ):
            return
        if self.config_entry and self.config_entry.pref_disable_polling:
            return
        coro = self.async_refresh()
        if self.config_entry:
            task = self.config_entry.async_create_background_task(
                self.hass, coro, name=f"NHL {self.team_abbrev} refresh"
            )
        else:
            task = self.hass.async_create_background_task(
                coro, name=f"NHL {self.team_abbrev} refresh"
            )
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def _async_update_data(self) -> NHLSensorData:
        """Fetch data; cancellation cannot re-arm a timer or emit late events."""
        task = asyncio.current_task()
        self._refresh_tasks.add(task)
        started = time.monotonic()
        now = dt_util.utcnow()
        if self._last_refresh_started_utc is not None:
            self._observed_refresh_interval_seconds = round(
                (now - self._last_refresh_started_utc).total_seconds(), 3
            )
        self._last_refresh_started_utc = now
        try:
            if self._shutdown_requested:
                raise asyncio.CancelledError
            if self.api.cooldown.active():
                raise UpdateFailed(
                    f"NHL API cooldown until {self.api.cooldown.until.isoformat()}"
                )
            data = await self._async_build_sensor_data(now)
            if self._shutdown_requested:
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._consecutive_refresh_failures += 1
            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(f"Invalid NHL response: {err}") from err
        else:
            self._consecutive_refresh_failures = 0
            return data
        finally:
            self._refresh_count += 1
            self._last_refresh_duration_ms = int((time.monotonic() - started) * 1000)
            self._schedule_next_update(dt_util.utcnow())
            self._refresh_tasks.discard(task)

    @property
    def effective_polling_delta(self) -> timedelta:
        """Expose the actual failure-aware cadence, including cooldown time."""
        interval = (
            ERROR_RETRY_INTERVAL
            if self._consecutive_refresh_failures
            else self._get_polling_delta()
        )
        if self._schedule_retry_pending:
            interval = min(
                interval,
                max(
                    timedelta(seconds=1), self._next_schedule_lookup - dt_util.utcnow()
                ),
            )
        if self.api.cooldown.active():
            interval = max(interval, self.api.cooldown.until - dt_util.utcnow())
        return interval

    async def _async_build_sensor_data(self, now_utc: datetime) -> NHLSensorData:
        """Build the sensor state and attributes for the current refresh."""
        previous_game_id = self._game_id
        previous_game_state = self._tracked_game_state
        if self._should_refresh_schedule(now_utc):
            await self._async_refresh_tracked_game(now_utc)

        if self._game_id is None:
            return self._build_no_game_sensor_data()

        game_landing = await self._async_fetch_json(
            f"/gamecenter/{self._game_id}/landing"
        )
        if game_landing is None:
            raise UpdateFailed(
                f"Unable to fetch game landing data for game_id={self._game_id}"
            )

        validate_payload(f"/gamecenter/{self._game_id}/landing", game_landing)
        if game_landing.get("gameScheduleState", "OK") in BLOCKED_SCHEDULE_STATES:
            self._clear_tracked_game()
            self._next_schedule_lookup = now_utc
            raise UpdateFailed(
                "Tracked game was postponed or cancelled; refreshing schedule"
            )
        game_state = self._update_tracked_game_state(
            game_landing,
            now_utc,
            previous_game_id=previous_game_id,
            previous_game_state=previous_game_state,
        )

        attrs = self._build_base_attributes(game_landing)
        await self._async_add_goal_attributes(game_landing, attrs)

        if game_state == "FUT":
            state = self._format_next_game_display(attrs.get("next_game_datetime"))
        else:
            state = game_state or "No Game Scheduled"

        return NHLSensorData(state=state, attrs=attrs, tracked_game_state=game_state)

    async def _async_refresh_tracked_game(self, now_utc: datetime) -> None:
        """Only authoritative current-season data can clear the tracked game."""
        schedule_game, current_schedule_ok = await self._async_get_relevant_game(
            now_utc
        )
        if schedule_game is not None:
            self._set_tracked_game(schedule_game)
        elif not current_schedule_ok:
            self._schedule_retry_pending = True
            self._next_schedule_lookup = now_utc + ERROR_RETRY_INTERVAL
            if self._game_id is None:
                raise UpdateFailed("Unable to fetch NHL schedule data")
            return
        else:
            self._clear_tracked_game()
        self._schedule_retry_pending = False
        self._next_schedule_lookup = now_utc + self._get_schedule_refresh_delta(now_utc)

    def _set_tracked_game(self, schedule_game: dict[str, Any]) -> None:
        """Update tracked game details from schedule data."""
        new_game_id = schedule_game.get("id")
        if new_game_id != self._game_id:
            self._reset_goal_tracking()
            self.api.errors = {
                path: message
                for path, message in self.api.errors.items()
                if not path.startswith("/gamecenter/")
            }
            self._tracked_game_state = str(schedule_game["gameState"]).upper()
            _LOGGER.info(
                "Tracking NHL game changed for team=%s previous_game_id=%s new_game_id=%s scheduled_state=%s start=%s",
                self.team_abbrev,
                self._game_id,
                new_game_id,
                str(schedule_game.get("gameState") or "").upper() or None,
                schedule_game.get("startTimeUTC"),
            )

        self._game_id = new_game_id
        if (
            schedule_game.get("gameState") in {"FUT", "PRE"}
            and dt_util.parse_datetime(schedule_game["startTimeUTC"]) > dt_util.utcnow()
        ):
            self._goal_tracking_initialized = True
        self._tracked_game_start = dt_util.parse_datetime(
            schedule_game.get("startTimeUTC")
        )

    def _clear_tracked_game(self) -> None:
        """Clear tracked game details when no relevant game is found."""
        if self._game_id is not None:
            _LOGGER.info(
                "No relevant NHL game found for team=%s; clearing tracked game previous_game_id=%s previous_state=%s",
                self.team_abbrev,
                self._game_id,
                self._tracked_game_state,
            )
            self._reset_goal_tracking()

        self._game_id = None
        self._tracked_game_state = None
        self._tracked_game_start = None

    def _build_no_game_sensor_data(self) -> NHLSensorData:
        """Build the default state when no game is being tracked."""
        self._tracked_game_state = None
        return NHLSensorData(
            state="No Game Scheduled",
            attrs={
                "next_game_date": "",
                "next_game_time": "",
                "next_game_datetime": None,
                "goal_tracked_team": False,
                "goal_feed_available": None,
            },
            tracked_game_state=None,
        )

    def _update_tracked_game_state(
        self,
        game_landing: dict[str, Any],
        now_utc: datetime,
        *,
        previous_game_id: int | None,
        previous_game_state: str | None,
    ) -> str | None:
        """Update tracked game state from landing data and log meaningful changes."""
        self._last_good_game_refresh_utc = now_utc
        game_state = str(game_landing.get("gameState") or "").upper() or None
        self._tracked_game_state = game_state
        self._tracked_game_start = dt_util.parse_datetime(game_landing["startTimeUTC"])
        if game_state in POSTGAME_STATES and self._postgame_started is None:
            self._postgame_started = now_utc

        if game_state != previous_game_state or self._game_id != previous_game_id:
            _LOGGER.info(
                "NHL game state updated for team=%s game_id=%s previous_state=%s new_state=%s away_score=%s home_score=%s period=%s time_remaining=%s",
                self.team_abbrev,
                self._game_id,
                previous_game_state,
                game_state,
                game_landing.get("awayTeam", {}).get("score"),
                game_landing.get("homeTeam", {}).get("score"),
                game_landing.get("periodDescriptor", {}).get("number"),
                game_landing.get("clock", {}).get("timeRemaining"),
            )

        return game_state

    def _reset_goal_tracking(self) -> None:
        """Reset goal tracking when moving to a new game.

        Goal events are deduplicated in memory. When the tracked game changes we
        intentionally drop that cache so only goals from the new game are
        considered.
        """
        self._goal_tracking_initialized = False
        self._seen_goal_event_ids.clear()
        self._last_goal_event_id = None
        self._last_goal_attributes = {}
        self.goal_feed_available = None
        self.last_good_pbp_refresh = None
        self._next_pbp_lookup = dt_util.utcnow()
        self._postgame_started = None
        self._final_pbp_received = False

    def _get_polling_delta(self) -> timedelta:
        """Use fast pregame/closing cadence and quiet distant-game polling."""
        state = self._tracked_game_state
        now = dt_util.utcnow()
        if state == "PRE":
            return PREGAME_SCAN_INTERVAL
        if state in LIVE_GAME_STATES:
            return self.live_scan_interval
        if state == "FUT" and self._tracked_game_start is not None:
            until_pregame = self._tracked_game_start - now - timedelta(minutes=30)
            if until_pregame <= timedelta(0):
                return PREGAME_SCAN_INTERVAL
            interval = (
                FUTURE_GAME_SCAN_INTERVAL_SAME_DAY
                if is_same_local_day(self._tracked_game_start, now)
                else FUTURE_GAME_SCAN_INTERVAL_IDLE
            )
            return min(interval, until_pregame)
        if state in POSTGAME_STATES:
            return (
                POSTGAME_SCAN_INTERVAL
                if self._retain_postgame(now)
                else ERROR_RETRY_INTERVAL
                if self._schedule_retry_pending
                else timedelta(seconds=1)
            )
        if state is not None:
            self._log_unknown_game_state(state)
        return SCHEDULE_REFRESH_IDLE

    def _retain_postgame(self, now: datetime) -> bool:
        """Allow final PBP to catch up; never pin an inaccessible game forever."""
        if self._postgame_started is None:
            return True
        elapsed = now - self._postgame_started
        return elapsed < self.postgame_grace or (
            not self._final_pbp_received and elapsed < POSTGAME_MAX_WAIT
        )

    def _should_refresh_schedule(self, now_utc: datetime) -> bool:
        """Respect schedule deadlines and release games after final reconciliation."""
        if self._schedule_retry_pending:
            return now_utc >= self._next_schedule_lookup
        return now_utc >= self._next_schedule_lookup or (
            self._tracked_game_state in POSTGAME_STATES
            and not self._retain_postgame(now_utc)
        )

    def _get_schedule_refresh_delta(self, now_utc: datetime) -> timedelta:
        """Return schedule refresh cadence based on current context."""
        if self._game_id is None or self._tracked_game_state is None:
            return SCHEDULE_REFRESH_IDLE
        if self._tracked_game_state in LIVE_GAME_STATES:
            return SCHEDULE_REFRESH_LIVE
        if self._tracked_game_state == "PRE":
            return SCHEDULE_REFRESH_PREGAME
        if self._tracked_game_state in POSTGAME_STATES:
            return SCHEDULE_REFRESH_POSTGAME

        if self._tracked_game_state == "FUT":
            if is_same_local_day(self._tracked_game_start, now_utc):
                return SCHEDULE_REFRESH_SAME_DAY
            return SCHEDULE_REFRESH_IDLE

        self._log_unknown_game_state(self._tracked_game_state)
        return SCHEDULE_REFRESH_IDLE

    async def _async_get_relevant_game(
        self, now_utc: datetime
    ) -> tuple[dict[str, Any] | None, bool]:
        """Look ahead lazily, without confusing partial success with an empty season."""
        season = get_season_id(now_utc)
        data = await self._async_fetch_json(
            f"/club-schedule-season/{self.team_abbrev}/{season}"
        )
        if data is None:
            return None, False
        if selected := self._select_game(data["games"], now_utc):
            return selected, True
        next_season = get_next_season_id(now_utc)
        if next_season != self._next_season_id:
            self._next_season_id = next_season
            self._next_season_cache = None
            self._next_season_failed = False
            self._next_season_lookup = now_utc
        if now_utc >= self._next_season_lookup:
            next_data = await self._async_fetch_json(
                f"/club-schedule-season/{self.team_abbrev}/{next_season}", optional=True
            )
            self._next_season_failed = next_data is None
            if next_data is not None:
                self._next_season_cache = next_data
            self._next_season_lookup = now_utc + NEXT_SEASON_CACHE_INTERVAL
        games = self._next_season_cache["games"] if self._next_season_cache else []
        return self._select_game(games, now_utc), not (
            self._next_season_failed and self._game_id is not None
        )

    def _select_game(
        self, games: list[dict[str, Any]], now: datetime
    ) -> dict[str, Any] | None:
        """Keep a closing game, then prefer LIVE over PRE and the next fixture."""
        games = sorted(
            (
                game
                for game in games
                if game.get("gameScheduleState", "OK") not in BLOCKED_SCHEDULE_STATES
                and game.get("gameState") not in BLOCKED_SCHEDULE_STATES
            ),
            key=lambda game: (game["startTimeUTC"], game["id"]),
        )
        current = next((game for game in games if game["id"] == self._game_id), None)
        if current is not None:
            current_state = current["gameState"]
            if current_state in POSTGAME_STATES:
                if (
                    self._tracked_game_state in LIVE_GAME_STATES
                    or self._retain_postgame(now)
                ):
                    return current
                self._retired_game_ids.add(current["id"])
            elif self._tracked_game_state in POSTGAME_STATES:
                # Do not regress to a cached PRE/LIVE schedule after a final landing.
                if self._retain_postgame(now):
                    return current
                self._retired_game_ids.add(current["id"])
        eligible = [game for game in games if game["id"] not in self._retired_game_ids]
        for states in (LIVE_GAME_STATES, {"PRE"}):
            active = [game for game in eligible if game["gameState"] in states]
            if active:
                # Keep the chosen split-squad game stable if it has equal priority.
                return next(
                    (game for game in active if game["id"] == self._game_id), active[0]
                )
        upcoming = [
            game
            for game in eligible
            if game["gameState"] == "FUT"
            and dt_util.parse_datetime(game["startTimeUTC"]) >= now - timedelta(hours=6)
        ]
        if upcoming:
            return upcoming[0]
        recent = [
            game
            for game in eligible
            if game["gameState"] in POSTGAME_STATES
            and dt_util.parse_datetime(game["startTimeUTC"]) >= now - timedelta(hours=8)
        ]
        return recent[-1] if recent else None

    async def _async_add_goal_attributes(
        self, game_landing: dict[str, Any], attrs: dict[str, Any]
    ) -> None:
        """Emit new regulation/OT goals only from a valid, matching feed."""
        game_state = game_landing["gameState"].upper()
        if game_state in {"FUT", "PRE"}:
            self._goal_tracking_initialized = True
        if game_state not in GOAL_TRACKING_GAME_STATES:
            self.goal_feed_available = None
            attrs.update(goal_tracked_team=False, goal_feed_available=None)
            return
        now = dt_util.utcnow()
        path = f"/gamecenter/{game_landing['id']}/play-by-play"
        pbp = None
        if now >= self._next_pbp_lookup:
            pbp = await self._async_fetch_json(path)
            if pbp is not None:
                try:
                    validate_payload(path, pbp)
                    if pbp["gameState"] not in GOAL_TRACKING_GAME_STATES:
                        raise ValueError("Play-by-play state is behind the scoreboard")
                except (ValueError, TypeError) as err:
                    self.api.record_error(path, str(err))
                    pbp = None
            if pbp is None:
                self._next_pbp_lookup = now + ERROR_RETRY_INTERVAL
        if pbp is None:
            self.goal_feed_available = False
            attrs.update(self._last_goal_attributes)
            attrs["goal_feed_available"] = False
            return

        home_team, away_team = game_landing["homeTeam"], game_landing["awayTeam"]
        roster_map = {player["playerId"]: player for player in pbp["rosterSpots"]}
        goal_plays = [
            play
            for play in pbp["plays"]
            if play.get("typeDescKey") == "goal"
            and play["periodDescriptor"]["periodType"] != "SO"
        ]
        complete_order = all(
            isinstance(play.get("sortOrder"), int) for play in goal_plays
        )
        goal_plays.sort(
            key=lambda play: goal_order(play, use_sort_order=complete_order)
        )
        # An empty/incomplete feed must not establish a false zero-goal baseline.
        # When joining midgame, wait until PBP has caught up to the landing score.
        last_details = goal_plays[-1]["details"] if goal_plays else {}
        pbp_scores = tuple(
            pbp.get(team, {}).get("score", last_details.get(score, 0))
            for team, score in (("homeTeam", "homeScore"), ("awayTeam", "awayScore"))
        )
        landing_scores = (home_team.get("score", 0), away_team.get("score", 0))
        # The deciding shootout point is not a regulation/OT goal play.
        shootout_point = (
            game_state in POSTGAME_STATES
            and game_landing.get("periodDescriptor", {}).get("periodType") == "SO"
        )
        missing_plays = not goal_plays and sum(landing_scores) > int(shootout_point)
        if missing_plays or (
            not self._goal_tracking_initialized and pbp_scores != landing_scores
        ):
            self.goal_feed_available = False
            attrs.update(self._last_goal_attributes)
            attrs["goal_feed_available"] = False
            self._next_pbp_lookup = now + self.live_scan_interval
            return
        self.goal_feed_available = True
        self.last_good_pbp_refresh = now
        attrs["goal_feed_available"] = True
        if game_state in POSTGAME_STATES and pbp["gameState"] in POSTGAME_STATES:
            self._final_pbp_received = pbp_scores == landing_scores
        if not self._goal_tracking_initialized:
            self._seen_goal_event_ids.update(
                str(play["eventId"]) for play in goal_plays
            )
            self._goal_tracking_initialized = True
        for play in goal_plays:
            event_id = str(play["eventId"])
            if event_id in self._seen_goal_event_ids:
                continue
            if self._shutdown_requested or self.hass.is_stopping:
                return
            payload = self._build_goal_payload(
                play, roster_map, home_team, away_team, game_landing
            )
            self._seen_goal_event_ids.add(event_id)
            self.hass.bus.async_fire("nhl_goal", payload)
            _LOGGER.info(
                "NHL goal team=%s game=%s event=%s scorer=%s",
                self.team_abbrev,
                game_landing["id"],
                event_id,
                payload["scoring_player_name"],
            )
        if not goal_plays:
            self._last_goal_attributes = {"goal_tracked_team": False}
            self._last_goal_event_id = None
            attrs.update(self._last_goal_attributes)
            return
        latest_goal_payload = self._build_goal_payload(
            goal_plays[-1], roster_map, home_team, away_team, game_landing
        )
        attrs.update(
            {
                "goal_type": latest_goal_payload.get("goal_type", ""),
                "goal_team_id": latest_goal_payload.get("goal_team_id"),
                "goal_event_id": latest_goal_payload.get("event_id"),
                "goal_team_abbrev": latest_goal_payload.get("goal_team_abbrev", ""),
                "goal_team_name": latest_goal_payload.get("goal_team_name", ""),
                "scoring_player_name": latest_goal_payload.get(
                    "scoring_player_name", ""
                ),
                "scoring_player_total": latest_goal_payload.get("scoring_player_total"),
                "scoring_player_number": latest_goal_payload.get(
                    "scoring_player_number"
                ),
                "assist1_player_name": latest_goal_payload.get(
                    "assist1_player_name", ""
                ),
                "assist1_player_total": latest_goal_payload.get("assist1_player_total"),
                "assist1_player_number": latest_goal_payload.get(
                    "assist1_player_number"
                ),
                "assist2_player_name": latest_goal_payload.get(
                    "assist2_player_name", ""
                ),
                "assist2_player_total": latest_goal_payload.get("assist2_player_total"),
                "assist2_player_number": latest_goal_payload.get(
                    "assist2_player_number"
                ),
                "goal_period": latest_goal_payload.get("period_number"),
                "goal_period_type": latest_goal_payload.get("period_type"),
                "goal_time_remaining": latest_goal_payload.get("time_remaining", ""),
            }
        )
        attrs["goal_tracked_team"] = bool(
            latest_goal_payload.get("goal_tracked_team", False)
        )

        self._last_goal_event_id = str(latest_goal_payload["event_id"])
        self._last_goal_attributes = {
            key: value
            for key, value in attrs.items()
            if key.startswith(("goal_", "scoring_", "assist1_", "assist2_"))
            and key != "goal_feed_available"
        }

    def _build_goal_payload(
        self,
        play: dict[str, Any],
        roster_map: dict[int, dict[str, Any]],
        home_team: dict[str, Any],
        away_team: dict[str, Any],
        game_landing: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a consistent goal payload for entity attributes and HA events."""
        details = play.get("details", {})
        goal_team_id = details.get("eventOwnerTeamId")

        goal_team_abbrev = ""
        goal_team_name = ""
        if goal_team_id == home_team.get("id"):
            goal_team_abbrev = home_team.get("abbrev", "")
            goal_team_name = home_team.get("commonName", {}).get("default", "")
        elif goal_team_id == away_team.get("id"):
            goal_team_abbrev = away_team.get("abbrev", "")
            goal_team_name = away_team.get("commonName", {}).get("default", "")

        scoring_player = roster_map.get(details.get("scoringPlayerId"), {})
        assist1_player = roster_map.get(details.get("assist1PlayerId"), {})
        assist2_player = roster_map.get(details.get("assist2PlayerId"), {})

        scoring_player_name = (
            f"{scoring_player.get('firstName', {}).get('default', '')} "
            f"{scoring_player.get('lastName', {}).get('default', '')}"
        ).strip()
        assist1_player_name = (
            f"{assist1_player.get('firstName', {}).get('default', '')} "
            f"{assist1_player.get('lastName', {}).get('default', '')}"
        ).strip()
        assist2_player_name = (
            f"{assist2_player.get('firstName', {}).get('default', '')} "
            f"{assist2_player.get('lastName', {}).get('default', '')}"
        ).strip()

        event_id = play.get("eventId")
        try:
            event_id_value = int(event_id) if event_id is not None else None
        except TypeError, ValueError:
            event_id_value = None

        home_score = details.get("homeScore")
        away_score = details.get("awayScore")

        return {
            "team_abbrev": self.team_abbrev,
            "game_id": game_landing.get("id"),
            "event_id": event_id_value,
            "goal_type": goal_strength(play, game_landing),
            "goal_team_id": goal_team_id,
            "goal_team_abbrev": goal_team_abbrev,
            "goal_team_name": goal_team_name,
            "goal_tracked_team": goal_team_abbrev.upper() == self.team_abbrev,
            "scoring_player_name": scoring_player_name,
            "scoring_player_total": details.get("scoringPlayerTotal"),
            "scoring_player_number": scoring_player.get("sweaterNumber"),
            "assist1_player_name": assist1_player_name,
            "assist1_player_total": details.get("assist1PlayerTotal"),
            "assist1_player_number": assist1_player.get("sweaterNumber"),
            "assist2_player_name": assist2_player_name,
            "assist2_player_total": details.get("assist2PlayerTotal"),
            "assist2_player_number": assist2_player.get("sweaterNumber"),
            "period_number": play.get("periodDescriptor", {}).get("number"),
            "period_type": play.get("periodDescriptor", {}).get("periodType"),
            "time_remaining": play.get("timeRemaining") or "",
            "time_in_period": play.get("timeInPeriod") or "",
            "home_score": (
                home_score
                if home_score is not None
                else game_landing.get("homeTeam", {}).get("score")
            ),
            "away_score": (
                away_score
                if away_score is not None
                else game_landing.get("awayTeam", {}).get("score")
            ),
            "home_team_abbrev": home_team.get("abbrev", ""),
            "away_team_abbrev": away_team.get("abbrev", ""),
        }

    def _build_base_attributes(self, game_data: dict[str, Any]) -> dict[str, Any]:
        """Build shared attributes from gamecenter payload."""
        away_team = game_data.get("awayTeam", {})
        home_team = game_data.get("homeTeam", {})

        game_start = dt_util.parse_datetime(game_data.get("startTimeUTC"))
        local_start = dt_util.as_local(game_start) if game_start else None

        next_game_date = ""
        next_game_time = ""
        if local_start:
            # Use portable day/hour formatting (%-d and %-I are glibc-only).
            day = str(local_start.day)  # no leading zero, all platforms
            hour = str(int(local_start.strftime("%I")))  # strip leading zero from 12-hr
            next_game_date = local_start.strftime(f"%B {day}, %Y")
            next_game_time = local_start.strftime(f"{hour}:%M %p")

        away_record = self._format_record(away_team)
        home_record = self._format_record(home_team)
        linescore = game_data.get("clock", {})
        period_desc = game_data.get("periodDescriptor", {})
        broadcasts = self._split_broadcasts(game_data.get("tvBroadcasts", []))

        return {
            "national_broadcasts": broadcasts["national"],
            "away_broadcasts": broadcasts["away"],
            "home_broadcasts": broadcasts["home"],
            "away_id": away_team.get("id"),
            "home_id": home_team.get("id"),
            "away_name": away_team.get("commonName", {}).get("default", ""),
            "home_name": home_team.get("commonName", {}).get("default", ""),
            "away_record": away_record,
            "home_record": home_record,
            "away_logo": away_team.get("logo", ""),
            "home_logo": home_team.get("logo", ""),
            "away_logo_dark": away_team.get("darkLogo", away_team.get("logo", "")),
            "home_logo_dark": home_team.get("darkLogo", home_team.get("logo", "")),
            "next_game_date": next_game_date,
            "next_game_time": next_game_time,
            "next_game_datetime": local_start,
            "game_id": game_data.get("id"),
            "game_state": str(game_data.get("gameState") or "").upper(),
            "away_score": away_team.get("score"),
            "home_score": home_team.get("score"),
            "away_sog": away_team.get("sog"),
            "home_sog": home_team.get("sog"),
            "current_period": period_desc.get("number"),
            "current_period_type": period_desc.get("periodType"),
            "is_intermission": linescore.get("inIntermission"),
            "time_remaining": linescore.get("timeRemaining"),
            "goal_tracked_team": False,
        }

    def _format_next_game_display(self, next_game_datetime: datetime | None) -> str:
        """Format FUT state display similar to the legacy integration."""
        if next_game_datetime is None:
            return "No Game Scheduled"

        local_now = dt_util.as_local(dt_util.utcnow())
        if next_game_datetime.date() == local_now.date():
            date_prefix = "Today,"
        elif next_game_datetime.date() == (local_now + timedelta(days=1)).date():
            date_prefix = "Tomorrow,"
        else:
            # Portable: avoid Linux-only %-d directive.
            day = str(next_game_datetime.day)
            date_prefix = next_game_datetime.strftime(f"%B {day}, %Y")

        hour = str(int(next_game_datetime.strftime("%I")))
        return f"{date_prefix} {next_game_datetime.strftime(f'{hour}:%M %p')}"

    async def _async_fetch_json(
        self, path: str, *, optional: bool = False
    ) -> dict[str, Any] | None:
        """Keep transport and schema errors out of coordinator state."""
        return await self.api.fetch(path, optional=optional)

    @staticmethod
    def _format_log_datetime(value: datetime | None) -> str:
        """Format a datetime for log output."""
        return value.isoformat() if value is not None else "n/a"

    def _log_unknown_game_state(self, game_state: str | None) -> None:
        """Warn once per unknown game state value."""
        if game_state in self._logged_unknown_game_states:
            return
        self._logged_unknown_game_states.add(game_state)
        _LOGGER.warning(
            "NHL sensor encountered unrecognised game state %r for team=%s; defaulting to idle polling",
            game_state,
            self.team_abbrev,
        )

    @staticmethod
    def _format_record(team_data: dict[str, Any]) -> str:
        """Format team record from wins/losses/OT losses if available."""
        if isinstance(team_data.get("record"), str):
            return team_data["record"]
        wins = team_data.get("wins")
        losses = team_data.get("losses")
        ot_losses = team_data.get("otLosses")
        if wins is None or losses is None or ot_losses is None:
            return ""
        return f"{wins}-{losses}-{ot_losses}"

    @staticmethod
    def _split_broadcasts(tv_broadcasts: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Split broadcasts into national/home/away buckets."""
        result = {"national": [], "away": [], "home": []}

        for broadcast in tv_broadcasts:
            network = broadcast.get("network")
            if not network:
                continue

            market = str(broadcast.get("market", "")).upper()
            if market in {"N", "NAT", "NATIONAL"}:
                result["national"].append(network)
            elif market in {"A", "AWAY"}:
                result["away"].append(network)
            elif market in {"H", "HOME"}:
                result["home"].append(network)
            else:
                _LOGGER.debug(
                    "NHL API returned unrecognised broadcast market %r; treating as national",
                    broadcast.get("market"),
                )
                result["national"].append(network)

        return result
