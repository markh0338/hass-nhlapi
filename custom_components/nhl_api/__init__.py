"""The NHL API integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant

from .const import CONF_ABBREV, DEFAULT_NAME, DOMAIN, PLATFORMS
from .sensor import NHLDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the NHL API integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NHL API from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    team_abbrev = str(entry.data[CONF_ABBREV]).strip().upper()
    name = str(entry.data.get(CONF_NAME) or DEFAULT_NAME)
    scan_interval = timedelta(seconds=int(entry.data[CONF_SCAN_INTERVAL]))

    _LOGGER.debug(
        "Setting up NHL API entry_id=%s team=%s live_scan_interval=%ss",
        entry.entry_id,
        team_abbrev,
        int(scan_interval.total_seconds()),
    )
    coordinator = NHLDataUpdateCoordinator(hass, team_abbrev, name, scan_interval)
    await coordinator.async_refresh()
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an NHL API config entry."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        if coordinator is not None:
            await coordinator.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        _LOGGER.debug("Unloaded NHL API entry_id=%s", entry.entry_id)
    return unload_ok
