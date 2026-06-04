"""Tests for the Ampio sensor platform."""

from datetime import UTC, datetime
import json

import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.ampio.const import DOMAIN
from homeassistant.const import CONF_MAC, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from . import device_prefix
from .conftest import DEFAULT_DETAILS, DEFAULT_DEVICES, USER_INPUT, FakeMqttBroker

from tests.common import MockConfigEntry, snapshot_platform

# Module override macs as seeded in DEFAULT_DEVICES.
MAC_MSERV = 1
MAC_MREL = 48770
MAC_MSENS = 52111


def _prefix(entry: MockConfigEntry) -> str:
    return device_prefix(entry)


def _last_seen_entity_id(
    entity_registry: er.EntityRegistry, entry: MockConfigEntry, module_mac: int
) -> str | None:
    return entity_registry.async_get_entity_id(
        Platform.SENSOR,
        "ampio",
        f"{_prefix(entry)}_module_{module_mac}_last_seen",
    )


def _sensor_entity_id(
    entity_registry: er.EntityRegistry,
    entry: MockConfigEntry,
    module_mac: int,
    typ: str,
    funkcja: int,
) -> str | None:
    return entity_registry.async_get_entity_id(
        Platform.SENSOR,
        "ampio",
        f"{_prefix(entry)}_obj_{module_mac}_{typ}_{funkcja}",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_all_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every entity's registry entry and state."""
    await _setup(hass, mock_config_entry)
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_devices(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Snapshot every device registry entry the integration creates."""
    await _setup(hass, mock_config_entry)
    devices = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert devices
    for device in devices:
        assert device == snapshot(name=f"device-{device.name}")


async def test_module_with_mixed_rooms_has_no_suggested_area(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A module whose objects span multiple rooms is left without an area hint."""
    mock_aiomqtt.groups = [
        {"id": 1, "opis_menu": "Salon"},
        {"id": 2, "opis_menu": "Kuchnia"},
    ]
    mock_aiomqtt.group_devices = [
        {"id_grupy": 1, "id_obiektu": 36},
        {"id_grupy": 2, "id_obiektu": 37},
    ]
    await _setup(hass, mock_config_entry)

    device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{_prefix(mock_config_entry)}:{MAC_MSENS}")}
    )
    assert device is not None
    assert device.area_id is None


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_device_serial_number_uses_override_can_mac(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """serial_number is the Designer override CAN mac (module.mac) in hex.

    The M-SERV's override is 1, so its serial reads 0x1 - honest about the
    bus address rather than fabricating a factory id no one debugs against.
    """
    await _setup(hass, mock_config_entry)

    mserv = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{_prefix(mock_config_entry)}:{MAC_MSERV}")}
    )
    assert mserv is not None
    assert mserv.serial_number == "0x1"

    sens = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{_prefix(mock_config_entry)}:{MAC_MSENS}")}
    )
    assert sens is not None
    assert sens.serial_number == "0xCB8F"


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_entity_unique_ids_use_replacement_stable_composite(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Sensor and last-seen uids encode {module.mac, typ_komponentu, funkcja}."""
    await _setup(hass, mock_config_entry)

    # Temperature object id=36 lives on module 17 (mac 52111), typ temp, funkcja 1.
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "temp", 1)
        is not None
    )
    # Last seen for the M-SENS uses the module's override mac.
    assert (
        _last_seen_entity_id(entity_registry, mock_config_entry, MAC_MSENS) is not None
    )


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_mserv_network_mac_connection(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A DHCP-known Ethernet MAC is wired into the M-SERV DeviceInfo connections."""
    ethernet_mac = "b8:27:eb:b2:83:df"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ampio (ampio.test)",
        data={**USER_INPUT, CONF_MAC: ethernet_mac},
        unique_id="47846",
    )
    await _setup(hass, entry)

    mserv = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{_prefix(entry)}:{MAC_MSERV}")}
    )
    assert mserv is not None
    assert (CONNECTION_NETWORK_MAC, ethernet_mac) in mserv.connections


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_only_visible_sensors_created(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Only objects the M-SERV considers visible produce sensor entities."""
    await _setup(hass, mock_config_entry)

    # Three real sensors on module 17 have a non-empty leafId -> exposed.
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "temp", 1)
        is not None
    )
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "lin_wej", 2)
        is not None
    )
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "lin_wej", 3)
        is not None
    )
    # Ghost object 99 (empty leafId, no name) -> filtered.
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "lin_wej", 4)
        is None
    )


async def test_named_ghost_is_filtered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A removed-but-still-returned object (name + value, empty leafId) is dropped.

    The old value/name heuristic would have surfaced this row; the canonical
    `visible` predicate (empty `leafId` AND not a system type) drops it.
    """
    mock_aiomqtt.details.append(
        {
            "id": 200,
            "id_urzadzenia": 17,
            "typ_komponentu": "lin_wej",
            "interpretacja": 1,
            "funkcja": 5,
            "leafId": "",  # ghost
            "opis_menu": "Ghost humidity",
            "stan_json": json.dumps({"state": "55.0"}),
        }
    )
    await _setup(hass, mock_config_entry)

    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "lin_wej", 5)
        is None
    )


async def test_ghost_object_promoted_when_leaf_id_arrives(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """An initially-filtered ghost becomes an entity once it reports a leafId.

    Guards against the regression where rejected objects were cached and never
    re-evaluated on subsequent discovery ticks.
    """
    ghost = {
        "id": 201,
        "id_urzadzenia": 17,
        "typ_komponentu": "temp",
        "interpretacja": 1,
        "funkcja": 6,
        "leafId": "",
        "opis_menu": "Pending temp",
        "stan_json": json.dumps({"state": "21.0"}),
    }
    mock_aiomqtt.details.append(ghost)
    await _setup(hass, mock_config_entry)
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "temp", 6)
        is None
    )

    promoted = dict(ghost, leafId="0_cb8f_temp_0_6")
    mock_aiomqtt.push_details([*DEFAULT_DETAILS, promoted])
    await hass.async_block_till_done()

    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "temp", 6)
        is not None
    )


async def test_push_update_changes_state(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A pushed object update is reflected in the entity state."""
    await _setup(hass, mock_config_entry)
    entity_id = _sensor_entity_id(
        entity_registry, mock_config_entry, MAC_MSENS, "temp", 1
    )
    assert entity_id is not None

    mock_aiomqtt.push_state(36, "25.5")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "25.5"


async def test_non_numeric_value_surfaces_as_unknown(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A non-numeric push on a numeric sensor maps to unknown, not the raw string.

    ``state_class=MEASUREMENT`` would otherwise reject the string and degrade
    silently; the platform logs at debug and returns ``None`` instead.
    """
    await _setup(hass, mock_config_entry)
    entity_id = _sensor_entity_id(
        entity_registry, mock_config_entry, MAC_MSENS, "temp", 1
    )
    assert entity_id is not None

    mock_aiomqtt.push_state(36, "INVALID")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"


async def test_push_only_refreshes_subscribed_entity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A push for one object refreshes only that entity, not its siblings.

    The per-object dispatcher should isolate the fan-out so a humidity push
    does not bump the temperature sensor's ``last_updated``.
    """
    await _setup(hass, mock_config_entry)
    temp_id = _sensor_entity_id(
        entity_registry, mock_config_entry, MAC_MSENS, "temp", 1
    )
    humidity_id = _sensor_entity_id(
        entity_registry, mock_config_entry, MAC_MSENS, "lin_wej", 2
    )
    assert temp_id is not None
    assert humidity_id is not None
    temp_before = hass.states.get(temp_id).last_updated
    humidity_before = hass.states.get(humidity_id).last_updated

    # Push only humidity (object id 37).
    mock_aiomqtt.push_state(37, "45.5")
    await hass.async_block_till_done()

    assert hass.states.get(humidity_id).last_updated > humidity_before
    assert hass.states.get(temp_id).last_updated == temp_before


async def test_unknown_kind_is_skipped(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """Objects classified into a kind without a static description are skipped."""
    mock_aiomqtt.details.append(
        {
            "id": 400,
            "id_urzadzenia": 17,
            "funkcja": 9,
            "opis_menu": "Status",
            "stan_json": json.dumps({"state": "armed"}),
        }
    )
    await _setup(hass, mock_config_entry)

    # No description for an unclassified kind -> no entity in any flavour.
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "", 9) is None
    )


async def test_orphan_object_is_filtered(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An object with no id_urzadzenia is dropped; no synthetic hub device exists."""
    mock_aiomqtt.details.append(
        {
            "id": 300,
            "typ_komponentu": "temp",
            "interpretacja": 1,
            "funkcja": 1,
            "opis_menu": "Orphan",
            "stan_json": json.dumps({"state": "1"}),
        }
    )
    await _setup(hass, mock_config_entry)

    # Orphan does not create an entity under any module's namespace.
    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, MAC_MSENS, "temp", 1)
        is not None
    )  # the real one on module 17 is still there
    # And no generic "hub" device is created.
    assert (
        device_registry.async_get_device(
            identifiers={(DOMAIN, f"{_prefix(mock_config_entry)}:hub")}
        )
        is None
    )


@pytest.mark.usefixtures("mock_aiomqtt")
async def test_module_last_seen_seeded_value(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The Last seen state reflects the module's seeded last_seen timestamp."""
    await _setup(hass, mock_config_entry)

    entity_id = _last_seen_entity_id(entity_registry, mock_config_entry, MAC_MSENS)
    assert entity_id is not None
    expected = datetime.fromtimestamp(1779565263.0, tz=UTC).isoformat()
    assert hass.states.get(entity_id).state == expected

    other_id = _last_seen_entity_id(entity_registry, mock_config_entry, MAC_MREL)
    assert other_id is not None
    assert hass.states.get(other_id).state == "unknown"


async def test_module_last_seen_updates_on_state_push(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A state push with an ``on`` timestamp advances the module's Last seen."""
    await _setup(hass, mock_config_entry)
    entity_id = _last_seen_entity_id(entity_registry, mock_config_entry, MAC_MSENS)
    assert entity_id is not None

    new_ts = 1779565999.0
    mock_aiomqtt.push_state(36, "24.4", on_ms=int(new_ts * 1000))
    await hass.async_block_till_done()

    expected = datetime.fromtimestamp(new_ts, tz=UTC).isoformat()
    assert hass.states.get(entity_id).state == expected


async def test_dynamic_discovery_adds_entities_post_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    entity_registry: er.EntityRegistry,
) -> None:
    """A module and object that appear after setup are discovered and added."""
    await _setup(hass, mock_config_entry)

    extra_mac = 999
    mock_aiomqtt.push_devices(
        [
            *DEFAULT_DEVICES,
            {
                "id": 42,
                "mac": extra_mac,
                "typ_urzadzenia": 44,
                "nazwa_urzadzenia": "m-extra",
            },
        ]
    )
    mock_aiomqtt.push_details(
        [
            *DEFAULT_DETAILS,
            {
                "id": 500,
                "id_urzadzenia": 42,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "funkcja": 1,
                "leafId": "0_late_temp_0_1",
                "opis_menu": "Late temp",
                "stan_json": json.dumps({"state": "20.5"}),
            },
        ]
    )
    await hass.async_block_till_done()

    assert (
        _sensor_entity_id(entity_registry, mock_config_entry, extra_mac, "temp", 1)
        is not None
    )
    assert (
        _last_seen_entity_id(entity_registry, mock_config_entry, extra_mac) is not None
    )


async def test_module_name_fallback_uses_override_mac(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_aiomqtt: FakeMqttBroker,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A module with no nazwa_urzadzenia falls back to 'Ampio module <mac>'."""
    mock_aiomqtt.devices = [
        # Replace module 17's entry with a minimal one that has only id + mac.
        d
        for d in mock_aiomqtt.devices
        if d["id"] != 17
    ]
    mock_aiomqtt.devices.append({"id": 17, "mac": MAC_MSENS})
    await _setup(hass, mock_config_entry)

    device = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{_prefix(mock_config_entry)}:{MAC_MSENS}")}
    )
    assert device is not None
    assert device.name == f"Ampio module {MAC_MSENS}"
    assert device.sw_version is None
