"""Downloadable, credential-free NHL diagnostics."""

from homeassistant.core import HomeAssistant

from .const import CONF_ABBREV, VERSION
from .coordinator import NHLConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NHLConfigEntry
) -> dict:
    """Expose bounded runtime metadata without user names or full API responses."""
    coordinator = entry.runtime_data
    return {
        "version": VERSION,
        "team_abbrev": entry.data[CONF_ABBREV],
        "game_id": coordinator._game_id,
        "game_state": coordinator._tracked_game_state,
        "polling_enabled": not entry.pref_disable_polling,
        "live_scan_interval_seconds": coordinator.live_scan_interval.total_seconds(),
        "postgame_minutes": coordinator.postgame_grace.total_seconds() / 60,
        "next_update": coordinator._next_update_utc,
        "next_schedule_lookup": coordinator._next_schedule_lookup,
        "cooldown_until": coordinator.api.cooldown.until,
        "refresh_count": coordinator._refresh_count,
        "refresh_failures": coordinator._consecutive_refresh_failures,
        "api_last_success": coordinator.api.last_success,
        "api_errors": dict(coordinator.api.errors),
        "api_error_count": coordinator.api.error_count,
        "api_timeout_count": coordinator.api.timeout_count,
        "goal_feed_available": coordinator.goal_feed_available,
        "last_good_goal_refresh": coordinator.last_good_pbp_refresh,
        "goals_seen": len(coordinator._seen_goal_event_ids),
        "final_pbp_received": coordinator._final_pbp_received,
        "active_requests": len(coordinator._refresh_tasks),
    }
