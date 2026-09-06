"""Reset Home Assistant's process globals between isolated event loops."""

import gc

import pytest
from homeassistant import core
from homeassistant.helpers import frame


@pytest.fixture(autouse=True)
def reset_ha_globals():
    """Match HA's own test cleanup when each test creates a fresh instance."""
    yield
    core._hass.__dict__.clear()
    frame.async_setup(None)
    frame._REPORTED_INTEGRATIONS.clear()


@pytest.fixture(autouse=True, scope="module")
def garbage_collection():
    """Use the same module-boundary collection as Home Assistant core tests."""
    gc.collect()
    gc.freeze()
