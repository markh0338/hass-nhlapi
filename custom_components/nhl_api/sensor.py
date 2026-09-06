"""NHL team scoreboard and diagnostic sensors."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ABBREV, DEFAULT_NAME, DOMAIN
from .coordinator import NHLConfigEntry, NHLDataUpdateCoordinator
from .entity import _device_info
from .helpers import normalize_team_abbrev

ERROR_DIAGNOSTIC_SENSOR_KEYS = frozenset(
    {"api_last_error", "api_error_count", "api_timeout_count", "goal_feed_available"}
)
MANUAL_DIAGNOSTIC_SENSOR_KEYS = frozenset({"manual_refresh_count"})
DIAGNOSTIC_PUBLISH_SECONDS = 60


@dataclass(frozen=True, slots=True)
class NHLDiagnosticSensorDescription(SensorEntityDescription):
    """Description for NHL diagnostic sensors."""

    value_fn: Any = None


DIAGNOSTIC_SENSORS: tuple[NHLDiagnosticSensorDescription, ...] = (
    NHLDiagnosticSensorDescription(
        key="goal_feed_available",
        name="Goal Feed Available",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.goal_feed_available,
    ),
    NHLDiagnosticSensorDescription(
        key="last_good_pbp_refresh",
        name="Last Good Goal Refresh",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.last_good_pbp_refresh,
    ),
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
            coordinator.effective_polling_delta.total_seconds()
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
        value_fn=lambda coordinator: coordinator.api.last_success,
    ),
    NHLDiagnosticSensorDescription(
        key="api_last_error",
        name="API Last Error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.api.last_error,
    ),
    NHLDiagnosticSensorDescription(
        key="api_error_count",
        name="API Error Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.api.error_count,
    ),
    NHLDiagnosticSensorDescription(
        key="api_timeout_count",
        name="API Timeout Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.api.timeout_count,
    ),
    NHLDiagnosticSensorDescription(
        key="last_attempt",
        name="Last Attempt",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator.api.last_attempt,
    ),
    NHLDiagnosticSensorDescription(
        key="last_good_game_refresh",
        name="Last Good Game Refresh",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._last_good_game_refresh_utc,
    ),
    NHLDiagnosticSensorDescription(
        key="manual_refresh_count",
        name="Manual Refresh Count",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda coordinator: coordinator._manual_refresh_count,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NHLConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up NHL API entities from a config entry."""
    coordinator: NHLDataUpdateCoordinator = entry.runtime_data
    team_abbrev = normalize_team_abbrev(entry.data[CONF_ABBREV])
    name = str(entry.options.get(CONF_NAME, entry.data.get(CONF_NAME)) or DEFAULT_NAME)

    async_add_entities(
        [NHLSensor(coordinator, entry, name, team_abbrev)]
        + [
            NHLDiagnosticSensor(coordinator, entry, team_abbrev, description)
            for description in DIAGNOSTIC_SENSORS
        ]
    )


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
    def native_value(self) -> str | None:
        """Return the current state value."""
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


class NHLDiagnosticSensor(CoordinatorEntity[NHLDataUpdateCoordinator], SensorEntity):
    """Diagnostic sensor exposing coordinator runtime details."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: NHLDataUpdateCoordinator,
        entry: ConfigEntry,
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
        self._last_published_value: Any = None
        self._last_published_main_state: str | None = None
        self._last_publish_token = 0
        self._last_publish_monotonic = time.monotonic()
        self._attr_entity_registry_enabled_default = (
            description.key in ERROR_DIAGNOSTIC_SENSOR_KEYS
        )

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

    async def async_added_to_hass(self) -> None:
        """Handle entity addition."""
        await super().async_added_to_hass()
        self._last_published_value = self.native_value
        self._last_published_main_state = (
            self.coordinator.data.state if self.coordinator.data else None
        )
        self._last_publish_token = self.coordinator._diagnostic_publish_token

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reduce recorder/logbook churn from high-frequency diagnostic updates.

        The primary team sensor should continue to publish every meaningful
        refresh, but these diagnostic entities are mostly runtime internals.
        Publish diagnostic updates only when:
        - the main sensor state changed, or
        - one of the error-focused diagnostics changed, or
        - a manual refresh was requested, or
        - a value changed and at least one minute has elapsed.
        """
        new_value = self.native_value
        main_state = self.coordinator.data.state if self.coordinator.data else None
        publish_token = self.coordinator._diagnostic_publish_token
        should_publish = main_state != self._last_published_main_state or (
            time.monotonic() - self._last_publish_monotonic
            >= DIAGNOSTIC_PUBLISH_SECONDS
            and new_value != self._last_published_value
        )

        if self.entity_description.key in ERROR_DIAGNOSTIC_SENSOR_KEYS:
            should_publish = should_publish or new_value != self._last_published_value
        if self.entity_description.key in MANUAL_DIAGNOSTIC_SENSOR_KEYS:
            should_publish = should_publish or new_value != self._last_published_value

        should_publish = should_publish or publish_token != self._last_publish_token

        if should_publish:
            self._last_publish_monotonic = time.monotonic()
            self._last_published_value = new_value
            self._last_published_main_state = main_state
            self._last_publish_token = publish_token
            self.async_write_ha_state()
