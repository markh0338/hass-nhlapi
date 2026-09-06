"""Constants for the NHL API integration."""

from __future__ import annotations

import re

from homeassistant.const import Platform

DOMAIN = "nhl_api"
VERSION = "1.1.0"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

CONF_ABBREV = "team_abbrev"
CONF_POSTGAME_MINUTES = "postgame_minutes"
DEFAULT_POSTGAME_MINUTES = 15

TEAM_ABBREVS: tuple[str, ...] = (
    "ANA",
    "BOS",
    "BUF",
    "CGY",
    "CAR",
    "CHI",
    "COL",
    "CBJ",
    "DAL",
    "DET",
    "EDM",
    "FLA",
    "LAK",
    "MIN",
    "MTL",
    "NSH",
    "NJD",
    "NYI",
    "NYR",
    "OTT",
    "PHI",
    "PIT",
    "SJS",
    "SEA",
    "STL",
    "TBL",
    "TOR",
    "UTA",
    "VAN",
    "VGK",
    "WSH",
    "WPG",
)

DEFAULT_NAME = "NHL Sensor"
DEFAULT_SCAN_INTERVAL_SECONDS = 2

API_BASE = "https://api-web.nhle.com/v1"
API_TIMEOUT_SECONDS = 8

TEAM_ABBREV_RE = re.compile(r"^[A-Z]{2,4}$")
