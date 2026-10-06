"""Select Tests."""

from unittest.mock import patch

import pytest
from homeassistant.components.select.const import (
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_OPTION
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.moonraker.const import DOMAIN

from .const import MOCK_CONFIG


@pytest.fixture(name="bypass_connect_client", autouse=True)
def bypass_connect_client_fixture():
    """Skip calls to get data from API."""
    with (
        patch("custom_components.moonraker.MoonrakerApiClient.start"),
        patch("custom_components.moonraker.MoonrakerApiClient.stop"),
    ):
        yield


async def test_file_select(hass):
    """The file select should list stored files and keep the selection."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    coordinator.data = {
        **(coordinator.data or {}),
        "file_list": {"files": [{"path": "b.gcode"}, {"path": "a.gcode"}]},
    }
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get("select.mainsail_print_file")
    assert state is not None
    assert state.attributes["options"] == ["a.gcode", "b.gcode"]

    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: "select.mainsail_print_file", ATTR_OPTION: "a.gcode"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert coordinator.selected_file == "a.gcode"
