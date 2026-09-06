"""Behavioral regression tests using NHL fixtures and real HA coordinators."""

import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

from custom_components.nhl_api import coordinator as module
from custom_components.nhl_api.helpers import (
    get_next_season_id,
    get_season_id,
    goal_strength,
)

FIXTURES = Path(__file__).parent / "fixtures"
LANDING = json.loads((FIXTURES / "landing.json").read_text())
PBP = json.loads((FIXTURES / "pbp.json").read_text())
FUTURE = json.loads((FIXTURES / "future_landing.json").read_text())
GOALS = PBP["plays"]
NOW = datetime(2026, 5, 30, 3, tzinfo=UTC)


def schedule(game_id=2025030315, state="OFF", start="2026-05-30T00:00:00Z", **extra):
    return {"id": game_id, "gameState": state, "startTimeUTC": start, **extra}


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/nhl-unit-tests")
        frame.async_setup(self.hass)
        self.clock = patch.object(module.dt_util, "utcnow", return_value=NOW).start()
        self.timer = patch.object(
            module, "async_track_point_in_utc_time", return_value=MagicMock()
        ).start()
        with patch.object(module, "async_get_clientsession", return_value=MagicMock()):
            self.c = module.NHLDataUpdateCoordinator(
                self.hass, "MTL", "Canadiens", timedelta(seconds=2)
            )
        self.remove = self.c.async_add_listener(lambda: None)
        self.fire = patch.object(type(self.hass.bus), "async_fire").start()
        self.addCleanup(patch.stopall)
        self.c.api.fetch = AsyncMock(return_value=copy.deepcopy(PBP))

    async def asyncTearDown(self):
        await self.c.async_shutdown()
        self.remove()

    def payload(self, play):
        return self.c._build_goal_payload(
            play,
            {p["playerId"]: p for p in PBP["rosterSpots"]},
            LANDING["homeTeam"],
            LANDING["awayTeam"],
            LANDING,
        )

    async def test_goal_time_and_record_mappings(self):
        self.assertEqual(self.payload(GOALS[0])["time_remaining"], "10:43")
        self.assertEqual(self.payload(GOALS[0])["time_in_period"], "09:17")
        self.assertEqual(
            self.c._build_base_attributes(FUTURE)["away_record"], "48-24-10"
        )
        self.assertEqual(
            self.c._format_record({"wins": 2, "losses": 1, "otLosses": 0}), "2-1-0"
        )

    async def test_strength_from_summary_and_conservative_fallback(self):
        self.assertEqual(self.payload(GOALS[4])["goal_type"], "PPG")
        no_summary = {**LANDING, "summary": {}}
        self.assertEqual(goal_strength(GOALS[4], no_summary), "PPG")
        self.assertEqual(goal_strength(GOALS[0], no_summary), "EVEN")
        sh = copy.deepcopy(GOALS[4])
        sh["details"]["eventOwnerTeamId"] = 8
        self.assertEqual(goal_strength(sh, no_summary), "SHG")
        self.assertEqual(goal_strength(GOALS[-1], no_summary), "")
        self.assertEqual(goal_strength(GOALS[-1], LANDING), "EVEN")

    async def test_per_play_score_not_latest_landing_score(self):
        self.assertEqual(
            (
                self.payload(GOALS[0])["home_score"],
                self.payload(GOALS[0])["away_score"],
            ),
            (1, 0),
        )

    async def test_goal_chronology_and_deduplication(self):
        self.c._goal_tracking_initialized = True
        await self.c._async_add_goal_attributes(LANDING, {})
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(
            [call.args[1]["event_id"] for call in self.fire.call_args_list],
            [103, 399, 472, 604, 110, 901, 1021],
        )

    async def test_latest_goal_and_missing_sort_order_fallback(self):
        pbp = copy.deepcopy(PBP)
        pbp["plays"] = pbp["plays"][:5]
        self.c._goal_tracking_initialized = True
        self.c.api.fetch.return_value = pbp
        attrs = {}
        await self.c._async_add_goal_attributes(LANDING, attrs)
        self.assertEqual(attrs["goal_event_id"], 110)
        del pbp["plays"][0]["sortOrder"]
        pbp["plays"].reverse()
        attrs = {}
        await self.c._async_add_goal_attributes(LANDING, attrs)
        self.assertEqual(attrs["goal_event_id"], 110)

    async def test_pregame_then_first_goal(self):
        await self.c._async_add_goal_attributes({**LANDING, "gameState": "PRE"}, {})
        await self.c._async_add_goal_attributes({**LANDING, "gameState": "LIVE"}, {})
        self.assertEqual(self.fire.call_count, len(GOALS))

    async def test_midgame_start_baselines_history(self):
        await self.c._async_add_goal_attributes(LANDING, {})
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 0)

    async def test_malformed_and_wrong_game_pbp_do_not_initialize(self):
        for value in ({}, {**PBP, "id": 123}):
            self.c.api.fetch.return_value = value
            attrs = {}
            await self.c._async_add_goal_attributes(LANDING, attrs)
            self.assertFalse(self.c._goal_tracking_initialized)
            self.assertFalse(attrs["goal_feed_available"])
            self.clock.return_value += timedelta(minutes=1)
        self.c.api.fetch.return_value = PBP
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 0)

    async def test_empty_lagging_pbp_does_not_baseline_history(self):
        self.c.api.fetch.return_value = {**PBP, "plays": []}
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertFalse(self.c._goal_tracking_initialized)
        self.clock.return_value += timedelta(seconds=2)
        self.c.api.fetch.return_value = PBP
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 0)

    async def test_pbp_outage_preserves_score_and_catches_up(self):
        self.c._goal_tracking_initialized = True
        self.c.api.fetch.return_value = None
        attrs = {"home_score": 6}
        await self.c._async_add_goal_attributes(LANDING, attrs)
        self.assertEqual(attrs["home_score"], 6)
        self.assertFalse(attrs["goal_feed_available"])
        self.c.api.fetch.return_value = PBP
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.c.api.fetch.call_count, 1)
        self.clock.return_value += timedelta(minutes=1)
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 7)

    async def test_empty_pbp_keeps_last_goal_even_when_header_scores_match(self):
        await self.c._async_add_goal_attributes(LANDING, {})
        self.c.api.fetch.return_value = {
            **PBP,
            "plays": [],
            "homeTeam": LANDING["homeTeam"],
            "awayTeam": LANDING["awayTeam"],
        }
        attrs = {}
        await self.c._async_add_goal_attributes(LANDING, attrs)
        self.assertFalse(attrs["goal_feed_available"])
        self.assertEqual(attrs["goal_event_id"], GOALS[-1]["eventId"])
        self.assertEqual(self.fire.call_count, 0)

    async def test_scoreless_game_decided_in_shootout_has_no_goal_events(self):
        landing = copy.deepcopy(LANDING)
        landing["periodDescriptor"] = {"number": 5, "periodType": "SO"}
        landing["homeTeam"]["score"] = 1
        landing["awayTeam"]["score"] = 0
        self.c.api.fetch.return_value = {
            **PBP,
            "plays": [],
            "homeTeam": landing["homeTeam"],
            "awayTeam": landing["awayTeam"],
        }
        attrs = {}
        await self.c._async_add_goal_attributes(landing, attrs)
        self.assertTrue(attrs["goal_feed_available"])
        self.assertTrue(self.c._final_pbp_received)
        self.assertEqual(self.fire.call_count, 0)

    async def test_postgame_schedule_failure_retries_without_spinning(self):
        self.c._game_id = LANDING["id"]
        self.c._tracked_game_state = "OFF"
        self.c._postgame_started = NOW - timedelta(hours=2)
        self.c.api.fetch.return_value = None
        await self.c._async_refresh_tracked_game(NOW)
        self.assertEqual(self.c._get_polling_delta(), timedelta(minutes=1))
        self.assertFalse(self.c._should_refresh_schedule(NOW + timedelta(seconds=30)))
        self.assertTrue(self.c._should_refresh_schedule(NOW + timedelta(minutes=1)))

    async def test_future_schedule_failure_retries_within_one_minute(self):
        self.c._game_id = FUTURE["id"]
        self.c._tracked_game_state = "FUT"
        self.c._tracked_game_start = NOW + timedelta(days=2)
        self.c.api.fetch.return_value = None
        await self.c._async_refresh_tracked_game(NOW)
        self.assertEqual(self.c.effective_polling_delta, timedelta(minutes=1))

    async def test_corrections_do_not_reannounce_and_retracted_goals_stay_seen(self):
        self.c._goal_tracking_initialized = True
        await self.c._async_add_goal_attributes(LANDING, {})
        self.fire.reset_mock()
        pbp = copy.deepcopy(PBP)
        pbp["plays"][-1]["details"]["scoringPlayerId"] = GOALS[0]["details"][
            "scoringPlayerId"
        ]
        self.c.api.fetch.return_value = pbp
        attrs = {}
        await self.c._async_add_goal_attributes(LANDING, attrs)
        self.assertEqual(attrs["scoring_player_name"], "Taylor Hall")
        self.c.api.fetch.return_value = {**PBP, "plays": PBP["plays"][:-1]}
        await self.c._async_add_goal_attributes(LANDING, {})
        self.c.api.fetch.return_value = PBP
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 0)

    async def test_more_than_32_goals_do_not_replay(self):
        self.c._goal_tracking_initialized = True
        plays = [{**GOALS[0], "eventId": i, "sortOrder": i} for i in range(1, 35)]
        self.c.api.fetch.return_value = {**PBP, "plays": plays}
        for _ in range(3):
            await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 34)

    async def test_shootout_attempts_do_not_emit_goal_events(self):
        self.c._goal_tracking_initialized = True
        play = {**GOALS[0], "periodDescriptor": {"number": 5, "periodType": "SO"}}
        self.c.api.fetch.return_value = {**PBP, "plays": [play]}
        await self.c._async_add_goal_attributes(LANDING, {})
        self.assertEqual(self.fire.call_count, 0)

    async def test_no_speculative_next_season_for_relevant_game(self):
        self.c.api.fetch.return_value = {"games": [schedule(state="LIVE")]}
        game, ok = await self.c._async_get_relevant_game(NOW)
        self.assertTrue(ok)
        self.assertEqual(game["id"], 2025030315)
        self.c.api.fetch.assert_awaited_once()

    async def test_failed_current_season_keeps_live_game(self):
        self.c._game_id = 2025030315
        self.c._tracked_game_state = "LIVE"
        self.c.api.fetch.side_effect = [None, {"games": []}]
        await self.c._async_refresh_tracked_game(NOW)
        self.assertEqual(self.c._game_id, 2025030315)
        self.assertEqual(self.c.api.fetch.call_count, 1)
        self.assertEqual(self.c._next_schedule_lookup, NOW + timedelta(minutes=1))

    async def test_next_season_failure_is_cached(self):
        self.c.api.fetch.side_effect = [{"games": []}, None, {"games": []}]
        await self.c._async_get_relevant_game(NOW)
        await self.c._async_get_relevant_game(NOW + timedelta(hours=1))
        self.assertEqual(self.c.api.fetch.call_count, 3)
        self.assertTrue(self.c.api.fetch.call_args_list[1].kwargs["optional"])

    async def test_current_terminal_game_retained_and_released(self):
        current, upcoming = (
            schedule(),
            schedule(2026030001, "FUT", "2026-06-01T00:00:00Z"),
        )
        self.c._game_id = current["id"]
        self.c._tracked_game_state = "LIVE"
        self.assertEqual(self.c._select_game([current, upcoming], NOW), current)
        self.c._tracked_game_state = "OFF"
        self.c._postgame_started = NOW
        self.c._final_pbp_received = True
        self.assertEqual(
            self.c._select_game([current, upcoming], NOW + timedelta(minutes=14)),
            current,
        )
        self.assertEqual(
            self.c._select_game([current, upcoming], NOW + timedelta(minutes=15)),
            upcoming,
        )
        self.assertEqual(
            self.c._select_game([current], NOW + timedelta(minutes=16)), None
        )

    async def test_final_pbp_failure_has_bounded_extension(self):
        self.c._game_id = LANDING["id"]
        self.c._tracked_game_state = "OFF"
        self.c._postgame_started = NOW
        self.assertTrue(self.c._retain_postgame(NOW + timedelta(minutes=30)))
        self.assertFalse(self.c._retain_postgame(NOW + timedelta(hours=1)))

    async def test_split_squad_priority_and_postponements(self):
        pre, live = schedule(1, "PRE"), schedule(2, "LIVE")
        self.assertEqual(self.c._select_game([pre, live], NOW), live)
        self.c._game_id = 2
        self.assertEqual(self.c._select_game([schedule(1, "LIVE"), live], NOW), live)
        self.assertEqual(
            self.c._select_game(
                [schedule(1, "LIVE", gameScheduleState="PPD"), live], NOW
            ),
            live,
        )
        self.assertIsNone(
            self.c._select_game([schedule(1, "FUT", gameScheduleState="CNCL")], NOW)
        )

    async def test_full_final_refresh_emits_last_goal_before_switch(self):
        self.c._game_id = LANDING["id"]
        self.c._tracked_game_state = "LIVE"
        self.c._goal_tracking_initialized = True
        self.c._seen_goal_event_ids = {str(play["eventId"]) for play in GOALS[:-1]}
        self.c.api.fetch.side_effect = [
            {"games": [schedule(), schedule(2, "FUT", "2026-06-01T00:00:00Z")]},
            LANDING,
            PBP,
        ]
        result = await self.c._async_build_sensor_data(NOW)
        self.assertEqual(result.state, "OFF")
        self.assertEqual(self.fire.call_args.args[1]["event_id"], GOALS[-1]["eventId"])
        self.assertTrue(self.c._final_pbp_received)

    async def test_startup_failure_retries_in_one_minute(self):
        self.c._async_build_sensor_data = AsyncMock(
            side_effect=module.UpdateFailed("offline")
        )
        await self.c.async_refresh()
        self.assertIsNone(self.c._schedule_unsub)
        await self.c.async_start()
        self.assertEqual(self.c._next_update_utc, NOW + timedelta(minutes=1))
        self.c._async_build_sensor_data.side_effect = None
        self.c._async_build_sensor_data.return_value = module.NHLSensorData(
            "LIVE", {}, "LIVE"
        )
        self.c._tracked_game_state = "LIVE"
        await self.c.async_refresh()
        self.assertEqual(self.c._next_update_utc, NOW + timedelta(seconds=2))

    async def test_shutdown_cancels_active_and_queued_refreshes(self):
        entered = asyncio.Event()

        async def build(now):
            entered.set()
            await asyncio.Event().wait()

        self.c._async_build_sensor_data = build
        await self.c.async_start()
        first = asyncio.create_task(self.c.async_refresh())
        await entered.wait()
        second = asyncio.create_task(self.c.async_refresh())
        await asyncio.sleep(0)
        await self.c.async_shutdown()
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertTrue(self.c._shutdown_requested)
        self.assertIsNone(self.c._schedule_unsub)
        self.assertIsNone(self.c._next_update_utc)
        await self.c.async_start()
        self.assertIsNone(self.c._schedule_unsub)

    async def test_uncooperative_fetch_cannot_emit_after_shutdown(self):
        entered = asyncio.Event()

        async def fetch(path, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return PBP

        self.c._goal_tracking_initialized = True
        self.c._async_build_sensor_data = AsyncMock(side_effect=lambda now: None)

        async def build(now):
            await self.c._async_add_goal_attributes(LANDING, {})
            return module.NHLSensorData("OFF", {}, "OFF")

        self.c._async_build_sensor_data = build
        self.c.api.fetch = fetch
        task = asyncio.create_task(self.c.async_refresh())
        await entered.wait()
        await self.c.async_shutdown()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.fire.call_count, 0)
        self.assertIsNone(self.c._schedule_unsub)

    async def test_polling_preferences_and_last_listener(self):
        self.c.config_entry = SimpleNamespace(pref_disable_polling=True)
        await self.c.async_start()
        self.assertIsNone(self.c._schedule_unsub)
        self.c.config_entry.pref_disable_polling = False
        self.c._schedule_next_update(NOW)
        self.assertIsNotNone(self.c._schedule_unsub)
        self.remove()
        self.assertIsNone(self.c._schedule_unsub)
        self.remove = self.c.async_add_listener(lambda: None)
        self.assertIsNotNone(self.c._schedule_unsub)

    async def test_manual_refresh_preserves_deadline_and_cooldown(self):
        self.c._async_build_sensor_data = AsyncMock(
            return_value=module.NHLSensorData("FUT", {}, "FUT")
        )
        await self.c.async_start()
        self.c._schedule_next_update(NOW, deadline=NOW + timedelta(seconds=5))
        await self.c.async_manual_refresh()
        self.assertEqual(self.c._next_update_utc, NOW + timedelta(seconds=5))
        self.c.api.cooldown.until = NOW + timedelta(hours=2)
        await self.c.async_manual_refresh()
        self.assertEqual(self.c._next_update_utc, NOW + timedelta(hours=2))
        self.assertEqual(self.c._async_build_sensor_data.call_count, 1)

    async def test_season_rollover_and_pregame_polling(self):
        for month, year in [(8, "20252026"), (9, "20262027"), (12, "20262027")]:
            self.assertEqual(get_season_id(datetime(2026, month, 1, tzinfo=UTC)), year)
        self.assertEqual(
            get_next_season_id(datetime(2026, 8, 1, tzinfo=UTC)), "20262027"
        )
        self.c._tracked_game_state = "FUT"
        self.c._tracked_game_start = NOW + timedelta(minutes=20)
        self.assertEqual(self.c._get_polling_delta(), timedelta(seconds=10))
