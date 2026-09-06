"""Diagnostic button entities for NHL API."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NHLConfigEntry, NHLDataUpdateCoordinator
from .entity import _device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NHLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NHL API buttons from a config entry."""
    coordinator: NHLDataUpdateCoordinator = entry.runtime_data
    async_add_entities([NHLRefreshButton(coordinator, entry)])


class NHLRefreshButton(CoordinatorEntity[NHLDataUpdateCoordinator], ButtonEntity):
    """Manual refresh button for the NHL API integration."""

    _attr_has_entity_name = True
    _attr_name = "Refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: NHLDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._team_abbrev = coordinator.team_abbrev
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_refresh"

    @property
    def device_info(self) -> dict:
        """Return the device for this team's NHL entities."""
        return _device_info(self._team_abbrev)

    @property
    def available(self) -> bool:
        """Keep the manual recovery button available during API failures."""
        return True

    async def async_press(self) -> None:
        """Trigger an on-demand refresh."""
        await self.coordinator.async_manual_refresh()
