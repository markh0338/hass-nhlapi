"""NHL team sensor, goal events, and diagnostic entities."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from aiohttp import ClientTimeout, ContentTypeError
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import CONF_ABBREV, DEFAULT_NAME, DOMAIN, TEAM_ABBREV_RE

_LOGGER = logging.getLogger(__name__)

__version__ = "1.0.0"

PREGAME_SCAN_INTERVAL = timedelta(seconds=10)
LIVE_SCAN_INTERVAL = timedelta(seconds=2)
POSTGAME_SCAN_INTERVAL = timedelta(seconds=600)
SCHEDULE_REFRESH_SAME_DAY = timedelta(minutes=10)
SCHEDULE_REFRESH_PREGAME = timedelta(minutes=5)
SCHEDULE_REFRESH_LIVE = timedelta(minutes=15)
SCHEDULE_REFRESH_POSTGAME = timedelta(minutes=15)
SCHEDULE_REFRESH_IDLE = timedelta(minutes=60)
FUTURE_GAME_SCAN_INTERVAL_SAME_DAY = timedelta(minutes=10)
FUTURE_GAME_SCAN_INTERVAL_IDLE = timedelta(minutes=60)
ERROR_RETRY_INTERVAL = timedelta(minutes=1)

API_BASE = "https://api-web.nhle.com/v1"
API_TIMEOUT_SECONDS = 8
API_MAX_RETRIES = 2
API_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
API_RETRY_BACKOFF_SECONDS = 0.4
API_RETRY_JITTER_SECONDS = 0.2
MAX_SEEN_GOAL_IDS = 32


@dataclass(slots=True)
class NHLSensorData:
    """In-memory data exposed by the coordinator."""

    state: str
    attrs: dict[str, Any]
    tracked_game_state: str | None


@dataclass(frozen=True, slots=True)
class NHLDiagnosticSensorDescription(SensorEntityDescription):
    """Description for NHL diagnostic sensors."""

    value_fn: Any = None


DIAGNOSTIC_SENSORS: tuple[NHLDiagnosticSensorDescription, ...] = (
    NHLDiagnosticSensorDescription(
        key="configured_live_scan_interval_seconds",
        name="Configured Live Scan Interval",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: int(
            coordinator.live_scan_interval.total_seconds()
        ),
    ),
    NHLDiagnosticSensorDescription(
        key="effective_scan_interval_seconds",
        name="Effective Scan Interval",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: int(
            coordinator._get_polling_delta().total_seconds()
        ),
    ),
    NHLDiagnosticSensorDescription(
        key="next_schedule_lookup",
        name="Next Schedule Lookup",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._next_schedule_lookup,
    ),
    NHLDiagnosticSensorDescription(
        key="next_update",
        name="Next Update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._next_update_utc,
    ),
    NHLDiagnosticSensorDescription(
        key="last_refresh_started",
        name="Last Refresh Started",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._last_refresh_started_utc,
    ),
    NHLDiagnosticSensorDescription(
        key="last_refresh_duration",
        name="Last Refresh Duration",
        native_unit_of_measurement="ms",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._last_refresh_duration_ms,
    ),
    NHLDiagnosticSensorDescription(
        key="observed_refresh_interval_seconds",
        name="Observed Refresh Interval",
        native_unit_of_measurement="s",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._observed_refresh_interval_seconds,
    ),
    NHLDiagnosticSensorDescription(
        key="refresh_count",
        name="Refresh Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._refresh_count,
    ),
    NHLDiagnosticSensorDescription(
        key="goals_seen_count",
        name="Goals Seen Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: len(coordinator._seen_goal_event_ids),
    ),
    NHLDiagnosticSensorDescription(
        key="api_last_success",
        name="API Last Success",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._api_last_success_utc,
    ),
    NHLDiagnosticSensorDescription(
        key="api_last_error",
        name="API Last Error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._api_last_error,
    ),
    NHLDiagnosticSensorDescription(
        key="api_error_count",
        name="API Error Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._api_error_count,
    ),
    NHLDiagnosticSensorDescription(
        key="api_timeout_count",
        name="API Timeout Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._api_timeout_count,
    ),
    NHLDiagnosticSensorDescription(
        key="last_attempt",
        name="Last Attempt",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._last_attempt_utc,
    ),
    NHLDiagnosticSensorDescription(
        key="last_good_game_refresh",
        name="Last Good Game Refresh",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._last_good_game_refresh_utc,
    ),
)


def _device_info(team_abbrev: str) -> dict[str, Any]:
    """Build device metadata shared by a team's entities."""
    return {
        "identifiers": {(DOMAIN, team_abbrev)},
        "name": f"NHL {team_abbrev}",
        "manufacturer": "NHL",
        "model": "Team Sensor",
        "sw_version": __version__,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NHL API entities from a config entry."""
    coordinator: NHLDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    team_abbrev = str(entry.data[CONF_ABBREV]).strip().upper()
    name = str(entry.data.get(CONF_NAME) or DEFAULT_NAME)

    if not TEAM_ABBREV_RE.match(team_abbrev):
        _LOGGER.error(
            "Invalid team_abbrev %r in config entry %s; setup aborted",
            team_abbrev,
            entry.entry_id,
        )
        return

    if coordinator.last_update_success:
        _LOGGER.debug("Initial NHL refresh succeeded for team=%s", team_abbrev)
    else:
        _LOGGER.warning(
            "Initial NHL refresh failed for team=%s; entity will be added and background retries will continue",
            team_abbrev,
        )
    async_add_entities(
        [NHLSensor(coordinator, entry, name, team_abbrev)]
        + [
            NHLDiagnosticSensor(coordinator, entry, name, team_abbrev, description)
            for description in DIAGNOSTIC_SENSORS
        ]
    )
    await coordinator.async_start()


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
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"nhl_sensor_{team_abbrev.upper()}",
            update_interval=None,
            always_update=True,
        )
        self._session = async_get_clientsession(hass)
        self.team_abbrev = team_abbrev.upper()
        self.sensor_name = name
        self.live_scan_interval = max(scan_interval, LIVE_SCAN_INTERVAL)

        self._game_id: int | None = None
        self._tracked_game_state: str | None = None
        self._goal_tracking_initialized = False
        self._seen_goal_event_ids: set[str] = set()
        self._last_goal_event_id: str | None = None
        self._next_schedule_lookup = dt_util.utcnow()
        self._schedule_unsub: Any | None = None

        self._last_refresh_started_utc: datetime | None = None
        self._last_refresh_duration_ms = 0
        self._observed_refresh_interval_seconds: float | None = None
        self._refresh_count = 0
        self._api_last_success_utc: datetime | None = None
        self._api_last_error = ""
        self._api_error_count = 0
        self._api_timeout_count = 0
        self._last_attempt_utc: datetime | None = None
        self._last_good_game_refresh_utc: datetime | None = None
        self._next_update_utc: datetime | None = None
        self._tracked_game_start: datetime | None = None
        self._consecutive_refresh_failures = 0
        self._last_scheduled_interval: timedelta | None = None
        self._logged_unknown_game_states: set[str | None] = set()
        self._started = False

    async def async_start(self) -> None:
        """Start dynamic scheduling after entity setup."""
        if self._started:
            _LOGGER.debug(
                "NHL coordinator already started for team=%s", self.team_abbrev
            )
            return
        self._started = True
        _LOGGER.debug(
            "Starting NHL coordinator for team=%s live_scan_interval=%ss",
            self.team_abbrev,
            int(self.live_scan_interval.total_seconds()),
        )
        self._schedule_next_update(dt_util.utcnow())

    async def async_shutdown(self) -> None:
        """Cancel scheduled callbacks on unload/remove."""
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub: Any | None = None
        _LOGGER.debug("Stopped NHL coordinator for team=%s", self.team_abbrev)

    async def async_manual_refresh(self) -> None:
        """Run an on-demand refresh without delaying the next automatic update."""
        scheduled_run = self._next_update_utc
        _LOGGER.debug(
            "Manual refresh requested for team=%s game_id=%s scheduled_run=%s",
            self.team_abbrev,
            self._game_id,
            self._format_log_datetime(scheduled_run),
        )
        await self.async_refresh()

        if (
            scheduled_run is not None
            and self._consecutive_refresh_failures == 0
            and scheduled_run > dt_util.utcnow()
            and (
                self._next_update_utc is None or scheduled_run < self._next_update_utc
            )
        ):
            self._schedule_next_update(scheduled_run - self._get_polling_delta())
            _LOGGER.debug(
                "Manual refresh restored existing cadence for team=%s next_update=%s",
                self.team_abbrev,
                self._format_log_datetime(self._next_update_utc),
            )

    @callback
    def _schedule_next_update(self, now: datetime, *, failure: bool = False) -> None:
        """Schedule the next refresh using the current effective cadence."""
        if self._schedule_unsub is not None:
            self._schedule_unsub()
            self._schedule_unsub: Any | None = None

        interval = ERROR_RETRY_INTERVAL if failure else self._get_polling_delta()
        next_run = now + interval
        self._next_update_utc = next_run
        if failure or interval != self._last_scheduled_interval:
            _LOGGER.debug(
                "Updated NHL refresh cadence for team=%s game_id=%s state=%s interval=%ss failure=%s next_update=%s",
                self.team_abbrev,
                self._game_id,
                self._tracked_game_state,
                int(interval.total_seconds()),
                failure,
                self._format_log_datetime(next_run),
            )
        self._last_scheduled_interval = interval
        self._schedule_unsub = async_track_point_in_utc_time(
            self.hass,
            self._handle_scheduled_refresh,
            next_run,
        )

    @callback
    def _handle_scheduled_refresh(self, _now: datetime) -> None:
        """Trigger an async coordinator refresh from the scheduler."""
        self.hass.async_create_task(self._async_scheduled_refresh())

    async def _async_scheduled_refresh(self) -> None:
        """Refresh and reschedule from the custom scheduler.

        Exceptions are caught here because this method runs inside a
        fire-and-forget task created by async_create_task.  Any uncaught
        exception would silently terminate the task and halt all future
        scheduled updates.  _async_update_data already calls
        _schedule_next_update(failure=True) before re-raising, so the
        reschedule is guaranteed even on failure.
        """
        try:
            await self.async_refresh()
        except Exception as err:  # noqa: BLE001
            # Logging is handled inside _async_update_data / async_refresh.
            # We swallow here only to keep the scheduler alive.
            if self._consecutive_refresh_failures >= 3:
                _LOGGER.warning(
                    "Scheduled refresh repeatedly failing for team=%s game_id=%s failures=%s next_retry_utc=%s last_error=%s",
                    self.team_abbrev,
                    self._game_id,
                    self._consecutive_refresh_failures,
                    self._next_update_utc.isoformat() if self._next_update_utc else "",
                    err,
                )
            else:
                _LOGGER.debug(
                    "Scheduled refresh raised for team=%s game_id=%s failure_count=%s; next retry already queued",
                    self.team_abbrev,
                    self._game_id,
                    self._consecutive_refresh_failures,
                )

    async def _async_update_data(self) -> NHLSensorData:
        """Fetch data from the NHL API."""
        started_monotonic = time.monotonic()
        started_utc = dt_util.utcnow()
        if self._last_refresh_started_utc is not None:
            self._observed_refresh_interval_seconds = round(
                (started_utc - self._last_refresh_started_utc).total_seconds(),
                3,
            )
        self._last_refresh_started_utc = started_utc

        try:
            data = await self._async_build_sensor_data(started_utc)
        except Exception as err:  # noqa: BLE001
            self._refresh_count += 1
            self._consecutive_refresh_failures += 1
            self._last_refresh_duration_ms = int(
                (time.monotonic() - started_monotonic) * 1000
            )
            self._schedule_next_update(dt_util.utcnow(), failure=True)
            if isinstance(err, UpdateFailed):
                log_fn = (
                    _LOGGER.warning
                    if self._consecutive_refresh_failures >= 3
                    else _LOGGER.debug
                )
                log_fn(
                    "NHL refresh failed for team=%s game_id=%s state=%s failures=%s next_retry=%s error=%s",
                    self.team_abbrev,
                    self._game_id,
                    self._tracked_game_state,
                    self._consecutive_refresh_failures,
                    self._format_log_datetime(self._next_update_utc),
                    err,
                )
            if not isinstance(err, UpdateFailed):
                _LOGGER.exception(
                    "Failed to refresh NHL sensor data for team=%s game_id=%s",
                    self.team_abbrev,
                    self._game_id,
                )
                raise UpdateFailed(
                    f"Unexpected error refreshing NHL API data: {err}"
                ) from err
            raise

        self._refresh_count += 1
        self._consecutive_refresh_failures = 0
        self._last_refresh_duration_ms = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        self._schedule_next_update(dt_util.utcnow())
        return data

    async def _async_build_sensor_data(self, now_utc: datetime) -> NHLSensorData:
        """Build the sensor state and attributes for the current refresh."""
        previous_game_id = self._game_id
        previous_game_state = self._tracked_game_state
        if self._should_refresh_schedule(now_utc):
            schedule_game, fetched_any_schedule = await self._async_get_relevant_game(
                now_utc
            )
            if schedule_game is not None:
                new_game_id = schedule_game.get("id")
                if new_game_id != self._game_id:
                    self._reset_goal_tracking()
                    _LOGGER.info(
                        "Tracking NHL game changed for team=%s previous_game_id=%s new_game_id=%s scheduled_state=%s start=%s",
                        self.team_abbrev,
                        self._game_id,
                        new_game_id,
                        str(schedule_game.get("gameState") or "").upper() or None,
                        schedule_game.get("startTimeUTC"),
                    )
                self._game_id = new_game_id
                self._tracked_game_start = dt_util.parse_datetime(
                    schedule_game.get("startTimeUTC")
                )
            elif self._game_id is None and not fetched_any_schedule:
                raise UpdateFailed("Unable to fetch NHL schedule data")
            elif schedule_game is None:
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

            self._next_schedule_lookup = now_utc + self._get_schedule_refresh_delta(
                now_utc
            )

        if self._game_id is None:
            self._tracked_game_state = None
            attrs = {
                "next_game_date": "",
                "next_game_time": "",
                "next_game_datetime": "",
                "goal_tracked_team": False,
            }
            return NHLSensorData(
                state="No Game Scheduled",
                attrs=attrs,
                tracked_game_state=None,
            )

        game_landing = await self._async_fetch_json(
            f"/gamecenter/{self._game_id}/landing"
        )
        if game_landing is None:
            raise UpdateFailed(
                f"Unable to fetch game landing data for game_id={self._game_id}"
            )

        self._last_good_game_refresh_utc = now_utc
        game_state = str(game_landing.get("gameState") or "").upper() or None
        self._tracked_game_state = game_state
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

        attrs = self._build_base_attributes(game_landing)
        await self._async_add_goal_attributes(game_landing, attrs)

        if game_state == "FUT":
            state = self._format_next_game_display(attrs.get("next_game_datetime"))
        else:
            state = game_state or "No Game Scheduled"

        return NHLSensorData(state=state, attrs=attrs, tracked_game_state=game_state)

    def _reset_goal_tracking(self) -> None:
        """Reset goal tracking when moving to a new game.

        Goal events are deduplicated in memory. When the tracked game changes we
        intentionally drop that cache so only goals from the new game are
        considered.
        """
        self._goal_tracking_initialized = False
        self._seen_goal_event_ids.clear()
        self._last_goal_event_id = None

    def _get_polling_delta(self) -> timedelta:
        """Return dynamic polling interval based on tracked game state."""
        if self._tracked_game_state is None:
            return SCHEDULE_REFRESH_IDLE
        if self._tracked_game_state == "PRE":
            return PREGAME_SCAN_INTERVAL
        if self._tracked_game_state in {"LIVE", "CRIT"}:
            return self.live_scan_interval
        if self._tracked_game_state == "FUT":
            tracked_start = self._tracked_game_start
            if tracked_start is not None:
                local_now = dt_util.as_local(dt_util.utcnow())
                local_start = dt_util.as_local(tracked_start)
                if local_start.date() == local_now.date():
                    return FUTURE_GAME_SCAN_INTERVAL_SAME_DAY
            return FUTURE_GAME_SCAN_INTERVAL_IDLE
        if self._tracked_game_state in {"FINAL", "OFF"}:
            return SCHEDULE_REFRESH_POSTGAME
        self._log_unknown_game_state(self._tracked_game_state)
        return SCHEDULE_REFRESH_IDLE

    def _should_refresh_schedule(self, now_utc: datetime) -> bool:
        """Return True when the schedule should be refreshed."""
        return (
            self._game_id is None
            or now_utc >= self._next_schedule_lookup
            or self._tracked_game_state in {"FINAL", "OFF", None}
        )

    def _get_schedule_refresh_delta(self, now_utc: datetime) -> timedelta:
        """Return schedule refresh cadence based on current context."""
        if self._game_id is None or self._tracked_game_state is None:
            return SCHEDULE_REFRESH_IDLE
        if self._tracked_game_state in {"LIVE", "CRIT"}:
            return SCHEDULE_REFRESH_LIVE
        if self._tracked_game_state == "PRE":
            return SCHEDULE_REFRESH_PREGAME
        if self._tracked_game_state in {"FINAL", "OFF"}:
            return SCHEDULE_REFRESH_POSTGAME

        # FUT state: check the currently tracked start time, not stale prior coordinator data.
        if self._tracked_game_state == "FUT":
            if self._tracked_game_start is not None:
                local_now = dt_util.as_local(now_utc)
                local_start = dt_util.as_local(self._tracked_game_start)
                if local_start.date() == local_now.date():
                    return SCHEDULE_REFRESH_SAME_DAY
            return SCHEDULE_REFRESH_IDLE

        self._log_unknown_game_state(self._tracked_game_state)
        return SCHEDULE_REFRESH_IDLE

    async def _async_get_relevant_game(
        self, now_utc: datetime
    ) -> tuple[dict[str, Any] | None, bool]:
        """Find the current or next relevant game.

        Selection priority is:
        1. A pregame/live/critical game.
        2. The next scheduled game in the future.
        3. A recently completed game, so late postgame data can still surface.

        Returns ``(game, fetched_any_schedule)``.
        """
        seasons = [self._get_season_id(now_utc)]
        next_season = self._get_next_season_id(now_utc)
        if next_season not in seasons:
            seasons.append(next_season)

        all_games: list[dict[str, Any]] = []
        fetched_any_schedule = False
        for season in seasons:
            data = await self._async_fetch_json(
                f"/club-schedule-season/{self.team_abbrev}/{season}"
            )
            if data is None:
                continue
            fetched_any_schedule = True
            all_games.extend(data.get("games", []))

        if not fetched_any_schedule:
            return None, False
        if not all_games:
            return None, True

        games = sorted(
            all_games,
            key=lambda game: game.get("startTimeUTC") or "9999-12-31T00:00:00Z",
        )

        live_states = {"LIVE", "CRIT", "PRE"}
        for game in games:
            if str(game.get("gameState", "")).upper() in live_states:
                return game, True

        upcoming_games: list[tuple[datetime, dict[str, Any]]] = []
        recently_completed: list[tuple[datetime, dict[str, Any]]] = []

        for game in games:
            start = dt_util.parse_datetime(game.get("startTimeUTC"))
            if start is None:
                continue
            if start >= now_utc:
                upcoming_games.append((start, game))
            elif start >= now_utc - timedelta(hours=8):
                recently_completed.append((start, game))

        if upcoming_games:
            return min(upcoming_games, key=lambda item: item[0])[1], True
        if recently_completed:
            return max(recently_completed, key=lambda item: item[0])[1], True

        return None, True

    async def _async_add_goal_attributes(
        self, game_landing: dict[str, Any], attrs: dict[str, Any]
    ) -> None:
        """Populate goal data and fire events for newly seen goals."""
        game_state = str(game_landing.get("gameState") or "").upper()
        if game_state not in {"LIVE", "CRIT", "FINAL", "OFF"}:
            attrs["goal_tracked_team"] = False
            return

        game_id = game_landing.get("id")
        if game_id is None:
            attrs["goal_tracked_team"] = False
            return

        pbp = await self._async_fetch_json(f"/gamecenter/{game_id}/play-by-play")
        if pbp is None:
            # A transient PBP failure should not discard the score / period
            # data that was already fetched from the landing endpoint.  Log a
            # warning, mark goal tracking as unavailable, and return so the
            # caller can still publish a useful sensor state.
            _LOGGER.warning(
                "Unable to fetch play-by-play for team=%s game_id=%s; "
                "goal attributes will be skipped this cycle",
                self.team_abbrev,
                game_id,
            )
            attrs["goal_tracked_team"] = False
            return

        roster_map = {
            player.get("playerId"): player
            for player in pbp.get("rosterSpots", [])
            if player.get("playerId") is not None
        }

        goal_plays = sorted(
            (
                play
                for play in pbp.get("plays", [])
                if str(play.get("typeDescKey", "")).lower() == "goal"
            ),
            key=lambda play: (
                int(play.get("eventId", 0))
                if str(play.get("eventId", "")).isdigit()
                else 0
            ),
        )
        if not goal_plays:
            self._goal_tracking_initialized = True
            attrs["goal_tracked_team"] = False
            return

        home_team = game_landing.get("homeTeam", {})
        away_team = game_landing.get("awayTeam", {})

        if not self._goal_tracking_initialized:
            # On first load we seed the seen-goal cache from existing PBP so the
            # integration does not replay historical goals as new events.
            self._seen_goal_event_ids = {
                str(play.get("eventId"))
                for play in goal_plays
                if play.get("eventId") is not None
            }
            self._goal_tracking_initialized = True
            _LOGGER.debug(
                "Initialized goal tracking baseline for team=%s game_id=%s existing_goals=%s",
                self.team_abbrev,
                game_id,
                len(self._seen_goal_event_ids),
            )
        else:
            unseen_goals = []
            for play in goal_plays:
                event_id = play.get("eventId")
                event_id_str = str(event_id) if event_id is not None else None
                if event_id_str is None or event_id_str in self._seen_goal_event_ids:
                    continue
                unseen_goals.append(play)

            unseen_goals.sort(
                key=lambda play: (
                    int(play.get("eventId", 0))
                    if str(play.get("eventId", "")).isdigit()
                    else 0
                )
            )

            for play in unseen_goals:
                event_id = play.get("eventId")
                event_id_str = str(event_id) if event_id is not None else None
                if event_id_str is None:
                    continue
                event_payload = self._build_goal_payload(
                    play, roster_map, home_team, away_team, game_landing
                )
                self._seen_goal_event_ids.add(event_id_str)
                self.hass.bus.async_fire("nhl_goal", event_payload)
                _LOGGER.info(
                    "New NHL goal observed for team=%s game_id=%s scoring_team=%s scorer=%s event_id=%s period=%s time_remaining=%s tracked_team_goal=%s",
                    self.team_abbrev,
                    game_id,
                    event_payload.get("goal_team_abbrev"),
                    event_payload.get("scoring_player_name"),
                    event_payload.get("event_id"),
                    event_payload.get("period_number"),
                    event_payload.get("time_remaining"),
                    event_payload.get("goal_tracked_team"),
                )
                self._last_goal_event_id = event_id_str

        if len(self._seen_goal_event_ids) > MAX_SEEN_GOAL_IDS:
            # Sort numerically where possible; non-numeric IDs sort to the
            # front and are pruned first, which is the safest behaviour.
            self._seen_goal_event_ids = set(
                sorted(
                    self._seen_goal_event_ids,
                    key=lambda x: int(x) if x.isdigit() else -1,
                )[-MAX_SEEN_GOAL_IDS:]
            )

        last_goal = goal_plays[-1]
        latest_goal_payload = self._build_goal_payload(
            last_goal, roster_map, home_team, away_team, game_landing
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

        latest_event_id = latest_goal_payload.get("event_id")
        if latest_event_id is not None:
            self._last_goal_event_id = str(latest_event_id)

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
            "goal_type": details.get("strength", ""),
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
            "time_remaining": play.get("timeInPeriod")
            or play.get("timeRemaining")
            or "",
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

    async def _async_fetch_json(self, path: str) -> dict[str, Any] | None:
        """Fetch JSON from the NHL API with retries and structured logging."""
        url = f"{API_BASE}{path}"
        timeout = ClientTimeout(total=API_TIMEOUT_SECONDS)

        for attempt in range(API_MAX_RETRIES + 1):
            self._last_attempt_utc = dt_util.utcnow()
            try:
                async with self._session.get(url, timeout=timeout) as response:
                    if response.status == 200:
                        try:
                            # content_type=None skips the MIME check so a
                            # maintenance page that returns text/html with a
                            # 200 raises JSONDecodeError instead of
                            # ContentTypeError — both are caught below.
                            data = await response.json(content_type=None)
                        except (ContentTypeError, ValueError) as json_err:
                            self._api_error_count += 1
                            self._api_last_error = (
                                f"JSON decode error for {path}: {json_err}"
                            )
                            log_fn = (
                                _LOGGER.warning
                                if attempt >= API_MAX_RETRIES
                                else _LOGGER.debug
                            )
                            log_fn(
                                "NHL API returned non-JSON for team=%s endpoint=%s attempt=%s/%s error=%s",
                                self.team_abbrev,
                                path,
                                attempt + 1,
                                API_MAX_RETRIES + 1,
                                json_err,
                            )
                            if attempt >= API_MAX_RETRIES:
                                return None
                            # Fall through to backoff and retry.
                        else:
                            self._api_last_success_utc = dt_util.utcnow()
                            self._api_last_error = ""
                            return data

                    else:
                        self._api_error_count += 1
                        self._api_last_error = f"HTTP {response.status} for {path}"
                        should_retry = (
                            response.status in API_RETRYABLE_STATUSES
                            and attempt < API_MAX_RETRIES
                        )
                        _LOGGER.debug(
                            "NHL API response team=%s game_id=%s endpoint=%s status=%s attempt=%s/%s retry=%s",
                            self.team_abbrev,
                            self._game_id,
                            path,
                            response.status,
                            attempt + 1,
                            API_MAX_RETRIES + 1,
                            should_retry,
                        )
                        if not should_retry:
                            _LOGGER.warning(
                                "NHL API request failed for team=%s game_id=%s endpoint=%s status=%s attempts=%s",
                                self.team_abbrev,
                                self._game_id,
                                path,
                                response.status,
                                attempt + 1,
                            )
                            return None

                        # Respect Retry-After header on 429 responses.
                        if response.status == 429:
                            retry_after_raw = response.headers.get("Retry-After")
                            if retry_after_raw is not None:
                                try:
                                    retry_after_secs = float(retry_after_raw)
                                    _LOGGER.warning(
                                        "NHL API rate-limited team=%s; honouring Retry-After=%.1fs",
                                        self.team_abbrev,
                                        retry_after_secs,
                                    )
                                    await asyncio.sleep(retry_after_secs)
                                    continue
                                except ValueError:
                                    _LOGGER.debug(
                                        "Ignoring malformed Retry-After header for team=%s endpoint=%s value=%r",
                                        self.team_abbrev,
                                        path,
                                        retry_after_raw,
                                    )

            except asyncio.TimeoutError:
                self._api_timeout_count += 1
                self._api_error_count += 1
                self._api_last_error = f"Timeout for {path}"
                log_fn = (
                    _LOGGER.warning if attempt >= API_MAX_RETRIES else _LOGGER.debug
                )
                log_fn(
                    "Timeout calling NHL API team=%s game_id=%s endpoint=%s attempt=%s/%s",
                    self.team_abbrev,
                    self._game_id,
                    path,
                    attempt + 1,
                    API_MAX_RETRIES + 1,
                )
                if attempt >= API_MAX_RETRIES:
                    return None
            except Exception as err:  # noqa: BLE001
                self._api_error_count += 1
                self._api_last_error = f"{type(err).__name__}: {err}"
                log_fn = (
                    _LOGGER.warning if attempt >= API_MAX_RETRIES else _LOGGER.debug
                )
                log_fn(
                    "Error calling NHL API team=%s game_id=%s endpoint=%s attempt=%s/%s error=%s",
                    self.team_abbrev,
                    self._game_id,
                    path,
                    attempt + 1,
                    API_MAX_RETRIES + 1,
                    err,
                )
                if attempt >= API_MAX_RETRIES:
                    return None

            # Exponential backoff with a small random jitter to avoid
            # synchronized retries when multiple team sensors are running.
            backoff = API_RETRY_BACKOFF_SECONDS * (2**attempt) + random.uniform(
                0, API_RETRY_JITTER_SECONDS
            )
            await asyncio.sleep(backoff)

        return None

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
                result["national"].append(network)

        return result

    @staticmethod
    def _get_season_id(now_utc: datetime) -> str:
        """Return season id string (for example, 20252026)."""
        if now_utc.month >= 9:
            start_year = now_utc.year
        else:
            start_year = now_utc.year - 1
        return f"{start_year}{start_year + 1}"

    @staticmethod
    def _get_next_season_id(now_utc: datetime) -> str:
        """Return the next season id string."""
        if now_utc.month >= 9:
            start_year = now_utc.year + 1
        else:
            start_year = now_utc.year
        return f"{start_year}{start_year + 1}"


class NHLSensor(CoordinatorEntity[NHLDataUpdateCoordinator], SensorEntity):
    """Representation of the NHL API sensor backed by a coordinator."""

    _attr_icon = "mdi:hockey-sticks"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: NHLDataUpdateCoordinator,
        entry: ConfigEntry,
        name: str,
        team_abbrev: str,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._team_abbrev = team_abbrev.upper()
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_state"

    @property
    def state(self) -> str | None:
        """Return the current state."""
        return self.coordinator.data.state if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return self.coordinator.data.attrs if self.coordinator.data else {}

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return super().available and self.coordinator.data is not None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the device for this team's NHL entities."""
        return _device_info(self._team_abbrev)

    async def async_added_to_hass(self) -> None:
        """Handle entity addition."""
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """Handle entity removal."""
        await super().async_will_remove_from_hass()


class NHLDiagnosticSensor(CoordinatorEntity[NHLDataUpdateCoordinator], SensorEntity):
    """Diagnostic sensor exposing coordinator runtime details."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: NHLDataUpdateCoordinator,
        entry: ConfigEntry,
        name: str,
        team_abbrev: str,
        description: NHLDiagnosticSensorDescription,
    ) -> None:
        """Initialize the diagnostic sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry_id = entry.entry_id
        self._team_abbrev = team_abbrev.upper()
        self._attr_has_entity_name = True
        self._attr_name = description.name
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the current diagnostic value."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return True

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the device for this team's NHL entities."""
        return _device_info(self._team_abbrev)
