"""Real Home Assistant flows, platforms, registries, reload and diagnostics."""

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, translation

from custom_components.nhl_api import config_flow, diagnostics, sensor
from custom_components.nhl_api.const import DOMAIN, VERSION

ROOT = Path(__file__).resolve().parents[1]
FUTURE = json.loads((ROOT / "tests/fixtures/future_landing.json").read_text())


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.future = copy.deepcopy(FUTURE)
        self.future["startTimeUTC"] = (
            datetime.now(UTC) + timedelta(days=2)
        ).isoformat()
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        frame.async_setup(self.hass)
        loader.async_setup(self.hass)
        translation.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        if hasattr(dr, "async_setup"):
            dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        await self.hass.config_entries.async_initialize()
        patch(
            "custom_components.nhl_api.coordinator.async_get_clientsession",
            return_value=MagicMock(),
        ).start()
        self.fetch = patch(
            "custom_components.nhl_api.api.NHLApiClient.fetch",
            side_effect=self.valid_fetch,
        ).start()
        self.addCleanup(patch.stopall)

    async def asyncTearDown(self):
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            await self.hass.config_entries.async_unload(entry.entry_id)
        await self.hass.async_stop(force=True)
        self.tmp.cleanup()

    async def valid_fetch(self, path, **kwargs):
        return {"games": [self.future]} if "schedule" in path else self.future

    async def create_entry(self):
        with patch.object(config_flow, "_async_validate_team", new_callable=AsyncMock):
            form = await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}
            )
            result = await self.hass.config_entries.flow.async_configure(
                form["flow_id"],
                {"team_abbrev": "mtl", "name": "Canadiens", "scan_interval": 2},
            )
            await self.hass.async_block_till_done()
        return result["result"]

    async def test_full_setup_entities_button_and_unload(self):
        entry = await self.create_entry()
        self.assertEqual(entry.state.value, "loaded")
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{entry.entry_id}_state"
        )
        state = self.hass.states.get(entity_id)
        self.assertEqual(state.attributes["game_id"], FUTURE["id"])
        self.assertEqual(state.attributes["away_record"], FUTURE["awayTeam"]["record"])
        button_id = registry.async_get_entity_id(
            "button", DOMAIN, f"{DOMAIN}_{entry.entry_id}_refresh"
        )
        await self.hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
        self.assertEqual(entry.runtime_data._manual_refresh_count, 1)
        coordinator = entry.runtime_data
        self.assertIsNotNone(coordinator._schedule_unsub)
        self.assertTrue(await self.hass.config_entries.async_unload(entry.entry_id))
        self.assertIsNone(coordinator._schedule_unsub)
        self.assertTrue(coordinator._shutdown_requested)
        self.assertEqual(len(coordinator._refresh_tasks), 0)

    async def test_options_reload_preserves_entity_ids_and_legacy_entry_data(self):
        entry = await self.create_entry()
        registry = er.async_get(self.hass)
        before = {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        old = entry.runtime_data
        # Simulate v1.0.1 data: no postgame setting and no options.
        self.hass.config_entries.async_update_entry(
            entry, data={"team_abbrev": "MTL", "name": "Canadiens", "scan_interval": 2}
        )
        form = await self.hass.config_entries.options.async_init(entry.entry_id)
        self.assertEqual(form["type"], "form")
        result = await self.hass.config_entries.options.async_configure(
            form["flow_id"], {"name": "Habs", "scan_interval": 7, "postgame_minutes": 5}
        )
        await self.hass.async_block_till_done()
        self.assertEqual(result["type"], "create_entry")
        self.assertTrue(old._shutdown_requested)
        self.assertEqual(entry.runtime_data.live_scan_interval, timedelta(seconds=7))
        self.assertEqual(entry.runtime_data.postgame_grace, timedelta(minutes=5))
        after = {
            e.unique_id: e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        }
        self.assertEqual(before, after)

    async def test_duplicate_team_aborts(self):
        await self.create_entry()
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={"team_abbrev": "mtl", "scan_interval": 2},
        )
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "already_configured")

    async def test_connection_failure_keeps_form_and_known_team(self):
        with patch.object(
            config_flow, "_async_validate_team", side_effect=ConnectionError
        ):
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "user"},
                data={"team_abbrev": "mtl", "scan_interval": 2},
            )
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertEqual(result["type"], "form")

    async def test_validator_separates_invalid_team_and_invalid_upstream(self):
        with self.assertRaises(ValueError):
            await config_flow._async_validate_team(self.hass, "XYZ")
        self.fetch.side_effect = None
        self.fetch.return_value = None
        with patch.object(
            config_flow, "async_get_clientsession", return_value=MagicMock()
        ):
            with self.assertRaises(ConnectionError):
                await config_flow._async_validate_team(self.hass, "MTL")

    async def test_english_config_and_team_translations_load(self):
        config = await translation.async_get_translations(
            self.hass, "en", "config", {DOMAIN}
        )
        teams = await translation.async_get_translations(
            self.hass, "en", "selector", {DOMAIN}
        )
        options = await translation.async_get_translations(
            self.hass, "en", "options", {DOMAIN}
        )
        self.assertEqual(
            config["component.nhl_api.config.step.user.data.team_abbrev"], "Team"
        )
        self.assertEqual(
            teams["component.nhl_api.selector.team_abbrev.options.uta"], "Utah Mammoth"
        )
        self.assertIn(
            "component.nhl_api.options.step.init.data.postgame_minutes", options
        )

    async def test_polling_disabled_and_download_diagnostics(self):
        entry = await self.create_entry()
        self.hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
        await self.hass.config_entries.async_reload(entry.entry_id)
        await self.hass.async_block_till_done()
        self.assertIsNone(entry.runtime_data._schedule_unsub)
        data = await diagnostics.async_get_config_entry_diagnostics(self.hass, entry)
        self.assertEqual(data["version"], VERSION)
        self.assertFalse(data["polling_enabled"])
        self.assertNotIn("name", data)
        self.assertNotIn("entry_id", data)

    async def test_diagnostic_defaults_and_bounded_publication(self):
        entry = await self.create_entry()
        description = next(
            d for d in sensor.DIAGNOSTIC_SENSORS if d.key == "refresh_count"
        )
        entity = sensor.NHLDiagnosticSensor(
            entry.runtime_data, entry, "MTL", description
        )
        self.assertFalse(entity.entity_registry_enabled_default)
        entity._last_published_main_state = entry.runtime_data.data.state
        entity._last_published_value = entry.runtime_data._refresh_count
        entity._last_publish_monotonic = 100
        entry.runtime_data._refresh_count += 1
        with (
            patch.object(entity, "async_write_ha_state") as write,
            patch.object(sensor.time, "monotonic", return_value=120),
        ):
            entity._handle_coordinator_update()
            write.assert_not_called()
        with (
            patch.object(entity, "async_write_ha_state") as write,
            patch.object(sensor.time, "monotonic", return_value=161),
        ):
            entity._handle_coordinator_update()
            write.assert_called_once()

    async def test_platform_setup_failure_shuts_down_coordinator(self):
        # Create through the flow but defer automatic platform setup.
        with patch(
            "homeassistant.config_entries.ConfigEntries.async_setup", return_value=True
        ):
            entry = await self.create_entry()
        from custom_components.nhl_api import async_setup_entry

        with patch.object(
            self.hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform failed"),
        ):
            with self.assertRaises(RuntimeError):
                await async_setup_entry(self.hass, entry)
        self.assertTrue(entry.runtime_data._shutdown_requested)
        self.assertIsNone(entry.runtime_data._schedule_unsub)
