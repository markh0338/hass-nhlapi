"""The NHL API integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ABBREV,
    CONF_POSTGAME_MINUTES,
    DEFAULT_NAME,
    DEFAULT_POSTGAME_MINUTES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    PLATFORMS,
)
from .coordinator import NHLConfigEntry, NHLDataUpdateCoordinator
from .helpers import normalize_team_abbrev


async def async_setup_entry(hass: HomeAssistant, entry: NHLConfigEntry) -> bool:
    """Set up entities even when initially offline, with prompt recovery."""
    settings = {**entry.data, **entry.options}
    coordinator = NHLDataUpdateCoordinator(
        hass,
        normalize_team_abbrev(entry.data[CONF_ABBREV]),
        str(settings.get(CONF_NAME) or DEFAULT_NAME),
        timedelta(
            seconds=int(settings.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS))
        ),
        config_entry=entry,
        postgame_minutes=int(
            settings.get(CONF_POSTGAME_MINUTES, DEFAULT_POSTGAME_MINUTES)
        ),
    )
    entry.runtime_data = coordinator
    try:
        await coordinator.async_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await coordinator.async_start()
    except BaseException:
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NHLConfigEntry) -> bool:
    """Unload entities and stop every request owned by this entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_shutdown()
    return unload_ok
