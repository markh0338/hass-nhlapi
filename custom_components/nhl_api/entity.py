"""Shared NHL device metadata."""

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, VERSION


def _device_info(team_abbrev: str) -> DeviceInfo:
    """Build device metadata shared by a team's entities."""
    return {
        "identifiers": {(DOMAIN, team_abbrev)},
        "name": f"NHL {team_abbrev}",
        "manufacturer": "NHL",
        "model": "Team Sensor",
        "sw_version": VERSION,
    }
