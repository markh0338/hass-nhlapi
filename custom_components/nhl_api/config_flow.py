"""Config flow for the NHL API integration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util
import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant import config_entries
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector

from .const import (
    API_BASE,
    API_TIMEOUT_SECONDS,
    CONF_ABBREV,
    DEFAULT_NAME,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    TEAM_ABBREVS,
    TEAM_ABBREV_RE,
)

_LOGGER = logging.getLogger(__name__)

TEAM_ABBREV_SELECTOR = selector(
    {
        "select": {
            "options": list(TEAM_ABBREVS),
            "translation_key": "team_abbrev",
            "mode": "dropdown",
        }
    }
)


def _get_season_id(now_utc: datetime) -> str:
    """Return season id string (for example, 20252026)."""
    if now_utc.month >= 9:
        start_year = now_utc.year
    else:
        start_year = now_utc.year - 1
    return f"{start_year}{start_year + 1}"


async def _async_validate_team(hass: HomeAssistant, team_abbrev: str) -> None:
    """Validate that the NHL API recognizes the supplied team abbreviation."""
    if not TEAM_ABBREV_RE.match(team_abbrev):
        _LOGGER.debug("Rejected invalid NHL team abbreviation format: %s", team_abbrev)
        raise ValueError("invalid_team")
    if team_abbrev not in TEAM_ABBREVS:
        _LOGGER.debug("Rejected unknown NHL team abbreviation: %s", team_abbrev)
        raise ValueError("invalid_team")

    session = async_get_clientsession(hass)
    season = _get_season_id(dt_util.utcnow())
    url = f"{API_BASE}/club-schedule-season/{team_abbrev}/{season}"

    try:
        async with session.get(
            url, timeout=ClientTimeout(total=API_TIMEOUT_SECONDS)
        ) as response:
            if response.status == 404:
                _LOGGER.debug(
                    "NHL team validation returned 404 for team=%s season=%s",
                    team_abbrev,
                    season,
                )
                raise ValueError("invalid_team")
            if response.status != 200:
                _LOGGER.warning(
                    "Unexpected status validating NHL team=%s season=%s status=%s",
                    team_abbrev,
                    season,
                    response.status,
                )
                raise ConnectionError(f"Unexpected HTTP {response.status}")
            data = await response.json(content_type=None)
    except ValueError:
        raise
    except (ClientError, TimeoutError, ConnectionError) as err:
        _LOGGER.warning(
            "Unable to validate NHL team=%s season=%s: %s",
            team_abbrev,
            season,
            err,
        )
        raise ConnectionError from err
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Unexpected error validating NHL team %s", team_abbrev)
        raise RuntimeError from err

    if "games" not in data:
        raise ValueError("invalid_team")
    _LOGGER.debug(
        "Validated NHL team abbreviation team=%s season=%s", team_abbrev, season
    )


class NHLAPIConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NHL API."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            team_abbrev = str(user_input[CONF_ABBREV]).strip().upper()
            name = (
                str(user_input.get(CONF_NAME) or DEFAULT_NAME).strip() or DEFAULT_NAME
            )
            scan_interval = int(user_input[CONF_SCAN_INTERVAL])

            await self.async_set_unique_id(team_abbrev)
            self._abort_if_unique_id_configured()

            try:
                await _async_validate_team(self.hass, team_abbrev)
            except ValueError:
                errors["base"] = "invalid_team"
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=name if name != DEFAULT_NAME else team_abbrev,
                    data={
                        CONF_ABBREV: team_abbrev,
                        CONF_NAME: name,
                        CONF_SCAN_INTERVAL: scan_interval,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ABBREV): TEAM_ABBREV_SELECTOR,
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=DEFAULT_SCAN_INTERVAL_SECONDS,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=DEFAULT_SCAN_INTERVAL_SECONDS),
                    ),
                }
            ),
            errors=errors,
        )
