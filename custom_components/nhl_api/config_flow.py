"""Configuration and options flows for NHL API."""

from __future__ import annotations

from typing import Any

import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector

from .api import NHLApiClient, RateLimit
from .const import (
    CONF_ABBREV,
    CONF_POSTGAME_MINUTES,
    DEFAULT_NAME,
    DEFAULT_POSTGAME_MINUTES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    TEAM_ABBREVS,
)
from .helpers import get_season_id, normalize_team_abbrev

TEAM_ABBREV_SELECTOR = selector(
    {
        "select": {
            "options": [team.lower() for team in TEAM_ABBREVS],
            "translation_key": "team_abbrev",
            "mode": "dropdown",
        }
    }
)


def _settings_schema(settings: dict[str, Any]) -> dict:
    """Use the same constraints in initial setup and options."""
    return {
        vol.Optional(CONF_NAME, default=settings.get(CONF_NAME, DEFAULT_NAME)): str,
        vol.Optional(
            CONF_SCAN_INTERVAL,
            default=settings.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
        ): vol.All(vol.Coerce(int), vol.Range(min=DEFAULT_SCAN_INTERVAL_SECONDS)),
        vol.Optional(
            CONF_POSTGAME_MINUTES,
            default=settings.get(CONF_POSTGAME_MINUTES, DEFAULT_POSTGAME_MINUTES),
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
    }


async def _async_validate_team(hass: HomeAssistant, team_abbrev: str) -> None:
    """Separate unknown teams from unavailable or malformed upstream data."""
    if team_abbrev not in TEAM_ABBREVS:
        raise ValueError("invalid_team")
    client = NHLApiClient(
        async_get_clientsession(hass),
        hass.data.setdefault(f"{DOMAIN}_rate_limit", RateLimit()),
    )
    data = await client.fetch(
        f"/club-schedule-season/{team_abbrev}/{get_season_id(dt_util.utcnow())}"
    )
    if data is None:
        raise ConnectionError("Unable to fetch a valid NHL schedule")


class NHLAPIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create one stable config entry per NHL team."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> NHLAPIOptionsFlow:
        return NHLAPIOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            team_abbrev = normalize_team_abbrev(user_input[CONF_ABBREV])
            await self.async_set_unique_id(team_abbrev)
            self._abort_if_unique_id_configured()
            try:
                await _async_validate_team(self.hass, team_abbrev)
            except ValueError:
                errors["base"] = "invalid_team"
            except ConnectionError:
                errors["base"] = "cannot_connect"
            else:
                name = (
                    str(user_input.get(CONF_NAME) or DEFAULT_NAME).strip()
                    or DEFAULT_NAME
                )
                return self.async_create_entry(
                    title=name if name != DEFAULT_NAME else team_abbrev,
                    data={
                        CONF_ABBREV: team_abbrev,
                        CONF_NAME: name,
                        CONF_SCAN_INTERVAL: user_input.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
                        ),
                        CONF_POSTGAME_MINUTES: user_input.get(
                            CONF_POSTGAME_MINUTES, DEFAULT_POSTGAME_MINUTES
                        ),
                    },
                )
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ABBREV): TEAM_ABBREV_SELECTOR,
                    **_settings_schema(user_input or {}),
                }
            ),
        )


class NHLAPIOptionsFlow(config_entries.OptionsFlowWithReload):
    """Change polling/name/grace without recreating entities or adding listeners."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            settings = {**self.config_entry.options, **user_input}
            settings[CONF_NAME] = (
                str(settings.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            )
            return self.async_create_entry(title="", data=settings)
        settings = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_settings_schema(settings))
        )
