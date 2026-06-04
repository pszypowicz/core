"""Tests for the Ampio integration setup and teardown."""

import logging

import aiomqtt
import pytest

from homeassistant.components.ampio.const import DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import device_prefix
from .conftest import FakeMqttBroker

from tests.common import MockConfigEntry


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """The entry loads and unloads cleanly when the broker accepts the connection."""
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_on_connection_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """A non-auth broker error during setup puts the entry in retry."""
    mock_aiomqtt.connect_error = aiomqtt.MqttError("Connection refused")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_triggers_reauth_on_auth_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """An auth failure during setup raises ConfigEntryAuthFailed -> SETUP_ERROR + reauth."""
    mock_aiomqtt.connect_error = aiomqtt.MqttError("Not authorized")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler("ampio")
    assert any(flow["context"].get("source") == "reauth" for flow in flows)


async def test_setup_triggers_reauth_when_mserv_id_missing(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """If the broker never reports the M-SERV module, prompt reauth."""
    # Strip the M-SERV (typ_urzadzenia 10) so the library has no module to
    # anchor via_device against and `mserv_id` stays None.
    mock_aiomqtt.devices = [
        d for d in mock_aiomqtt.devices if d.get("typ_urzadzenia") != 10
    ]
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler("ampio")
    assert any(flow["context"].get("source") == SOURCE_REAUTH for flow in flows)


async def test_setup_pre_registers_mserv_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The M-SERV device is in the registry before sensor platform forwards."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mserv = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{device_prefix(mock_config_entry)}:1")}
    )
    assert mserv is not None
    assert mserv.manufacturer == "Ampio"


async def test_availability_transitions_flip_entities_and_log(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disconnect flips entities unavailable and logs WARNING; reconnect restores them."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    entity_id = "sensor.salon_m_sens_salon_temperatura"
    assert hass.states.get(entity_id).state == "24.4"

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="homeassistant.components.ampio"):
        mock_aiomqtt.trigger_disconnect()
        # Let the disconnect propagate: runner consumes the poison, fires the
        # availability-False callback, then enters its reconnect sleep.
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

        # The broker fixture signals `reconnected` from inside `__aenter__`
        # whenever the runner opens a second-or-later connection. Waiting on
        # the event drives reconnect deterministically without sleeping past
        # the library's backoff interval.
        await mock_aiomqtt.reconnected.wait()
        await hass.async_block_till_done()

    levels = [(rec.levelno, rec.message) for rec in caplog.records]
    assert (logging.WARNING, "Lost connection to Ampio broker; retrying") in levels
    assert (logging.INFO, "Reconnected to Ampio broker") in levels
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_populates_room_map(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """The coordinator fetches the room map at setup."""
    del mock_aiomqtt
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.room_map == {
        36: "Salon",
        37: "Salon",
        43: "Salon",
    }


async def test_setup_tolerates_missing_room_map(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """Setup loads cleanly even when the broker never answers the room request."""
    mock_aiomqtt.disable_room_response = True
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.room_map == {}


async def test_setup_populates_location_map(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """The coordinator fetches the Designer location name table at setup."""
    del mock_aiomqtt
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.location_map == {
        1: "Salon",
        2: "Kuchnia",
    }


async def test_setup_tolerates_missing_location_map(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
) -> None:
    """Setup loads cleanly even when the broker never answers the locations request."""
    mock_aiomqtt.disable_location_response = True
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data.location_map == {}
