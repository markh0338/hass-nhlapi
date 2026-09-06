"""Transport contracts, retry behavior and cooldown regression tests."""

import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.nhl_api.api import (
    NHLApiClient,
    RateLimit,
    parse_retry_after,
    validate_payload,
)

FIXTURES = Path(__file__).parent / "fixtures"
LANDING = json.loads((FIXTURES / "landing.json").read_text())
PBP = json.loads((FIXTURES / "pbp.json").read_text())
NOW = datetime(2026, 9, 6, tzinfo=UTC)
PATH = f"/gamecenter/{LANDING['id']}/landing"
PBP_PATH = f"/gamecenter/{LANDING['id']}/play-by-play"
SCHEDULE_PATH = "/club-schedule-season/MTL/20262027"


def response(status=200, data=None, headers=None, error=None):
    result = MagicMock(status=status, headers=headers or {})
    result.json = AsyncMock(return_value=data, side_effect=error)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=result)
    manager.__aexit__ = AsyncMock(return_value=False)
    return manager


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = MagicMock()
        self.cooldown = RateLimit()
        self.client = NHLApiClient(self.session, self.cooldown)
        self.sleep = patch(
            "custom_components.nhl_api.api.asyncio.sleep", new_callable=AsyncMock
        ).start()
        patch("custom_components.nhl_api.api.dt_util.utcnow", return_value=NOW).start()
        self.addCleanup(patch.stopall)

    async def test_valid_response_and_optional_empty_schedule(self):
        self.session.get.side_effect = [
            response(data=LANDING),
            response(data={"games": []}),
        ]
        self.assertEqual(await self.client.fetch(PATH), LANDING)
        self.assertEqual(await self.client.fetch(SCHEDULE_PATH), {"games": []})
        self.assertEqual(self.client.error_count, 0)

    async def test_invalid_json_is_retried_and_classified_as_upstream_error(self):
        self.session.get.return_value = response(error=ValueError("maintenance HTML"))
        self.assertIsNone(await self.client.fetch(PATH))
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(self.client.error_count, 1)
        self.assertIn("ValueError", self.client.last_error)

    async def test_timeout_and_5xx_retry_then_recover(self):
        self.session.get.side_effect = [
            TimeoutError(),
            response(503),
            response(data=LANDING),
        ]
        self.assertEqual(await self.client.fetch(PATH), LANDING)
        self.assertEqual(self.client.timeout_count, 1)
        self.assertEqual(self.client.error_count, 0)
        self.assertEqual(self.sleep.await_count, 2)

    async def test_429_on_last_attempt_keeps_cooldown_without_sleep(self):
        self.session.get.side_effect = [
            response(500),
            response(503),
            response(429, headers={"Retry-After": "7200"}),
        ]
        self.assertIsNone(await self.client.fetch(PATH))
        self.assertEqual(self.cooldown.until, NOW + timedelta(hours=2))
        self.assertEqual(self.sleep.await_count, 2)
        self.assertTrue(all(call.args[0] < 2 for call in self.sleep.call_args_list))
        self.assertIsNone(await self.client.fetch(PATH))
        self.assertEqual(self.session.get.call_count, 3)

    async def test_http_date_cooldown_shared_across_team_clients(self):
        self.session.get.return_value = response(
            429, headers={"Retry-After": format_datetime(NOW + timedelta(minutes=5))}
        )
        await self.client.fetch(PATH)
        other_session = MagicMock()
        other = NHLApiClient(other_session, self.cooldown)
        self.assertIsNone(await other.fetch(SCHEDULE_PATH))
        other_session.get.assert_not_called()
        with patch(
            "custom_components.nhl_api.api.dt_util.utcnow",
            return_value=NOW + timedelta(minutes=6),
        ):
            other_session.get.return_value = response(data={"games": []})
            self.assertEqual(await other.fetch(SCHEDULE_PATH), {"games": []})

    async def test_optional_missing_season_is_quiet_and_not_retried(self):
        self.session.get.return_value = response(500)
        self.assertIsNone(await self.client.fetch(SCHEDULE_PATH, optional=True))
        self.session.get.assert_called_once()
        self.assertEqual(self.client.last_error, "")
        self.assertEqual(self.client.error_count, 0)
        self.sleep.assert_not_called()

    async def test_pbp_error_survives_landing_success_until_pbp_recovery(self):
        self.session.get.side_effect = [
            response(404),
            response(data=LANDING),
            response(data=PBP),
        ]
        await self.client.fetch(PBP_PATH)
        await self.client.fetch(PATH)
        self.assertIn("404", self.client.last_error)
        await self.client.fetch(PBP_PATH)
        self.assertEqual(self.client.last_error, "")

    async def test_repeated_identical_errors_log_once(self):
        self.session.get.return_value = response(404)
        with patch("custom_components.nhl_api.api._LOGGER.warning") as warning:
            for _ in range(3):
                await self.client.fetch(PATH)
        warning.assert_called_once()
        self.assertEqual(self.client.error_count, 3)


@pytest.mark.parametrize(
    "header", [None, "", "invalid", "-1", "NaN", "Infinity", "1e100"]
)
def test_malformed_retry_after_falls_back_without_overflow(header):
    assert parse_retry_after(header, NOW) == NOW + timedelta(minutes=1)


@pytest.mark.parametrize(
    "path,payload",
    [
        (SCHEDULE_PATH, {}),
        (SCHEDULE_PATH, {"games": None}),
        (SCHEDULE_PATH, {"games": [None]}),
        (
            SCHEDULE_PATH,
            {"games": [{"id": 1, "gameState": "FUT", "startTimeUTC": None}]},
        ),
        (
            SCHEDULE_PATH,
            {
                "games": [
                    {"id": 1, "gameState": "FUT", "startTimeUTC": "2026-10-10T10:00:00"}
                ]
            },
        ),
        (PATH, []),
        (PATH, {"id": 123}),
        (PATH, {**LANDING, "homeTeam": None}),
        (PATH, {**LANDING, "homeTeam": {**LANDING["homeTeam"], "score": "6"}}),
        (PBP_PATH, {**PBP, "homeTeam": {"score": -1}}),
        (PATH, {**LANDING, "clock": None}),
        (PBP_PATH, {}),
        (PBP_PATH, {**PBP, "plays": None}),
        (PBP_PATH, {**PBP, "id": 123}),
        (PBP_PATH, {**PBP, "rosterSpots": None}),
    ],
)
def test_schema_errors_are_rejected(path, payload):
    with pytest.raises(ValueError):
        validate_payload(path, payload)


def test_postponed_game_can_have_no_known_start():
    data = {"games": [{"id": 1, "gameState": "FUT", "gameScheduleState": "PPD"}]}
    assert validate_payload(SCHEDULE_PATH, data) == data


def test_bad_goal_details_cannot_change_baseline():
    data = copy.deepcopy(PBP)
    data["plays"][0]["details"] = None
    with pytest.raises(ValueError):
        validate_payload(PBP_PATH, data)
