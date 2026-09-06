"""Release metadata and shared form constraints."""

import json
import re
import tomllib
from pathlib import Path

import pytest
import voluptuous as vol

from custom_components.nhl_api.config_flow import TEAM_ABBREV_SELECTOR, _settings_schema
from custom_components.nhl_api.const import TEAM_ABBREVS, VERSION

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components/nhl_api"


def test_release_metadata_and_runtime_translations():
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == project["project"]["version"] == VERSION
    assert manifest["issue_tracker"].endswith("/issues")
    source = json.loads((INTEGRATION / "strings.json").read_text())
    runtime = json.loads((INTEGRATION / "translations/en.json").read_text())
    assert source == runtime
    labels = runtime["selector"]["team_abbrev"]["options"]
    assert {key.upper() for key in labels} == set(TEAM_ABBREVS)
    assert all(re.fullmatch("[a-z0-9]+", key) for key in labels)
    assert TEAM_ABBREV_SELECTOR("mtl") == "mtl"


@pytest.mark.parametrize(
    "settings",
    [
        {"scan_interval": 1},
        {"scan_interval": 0},
        {"postgame_minutes": -1},
        {"postgame_minutes": 61},
    ],
)
def test_config_and_options_reject_invalid_intervals(settings):
    with pytest.raises(vol.Invalid):
        vol.Schema(_settings_schema({}))(settings)
