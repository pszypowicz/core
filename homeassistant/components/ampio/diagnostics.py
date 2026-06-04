"""Diagnostics for the Ampio integration.

Designed so a user reporting an issue can attach a single download
(Settings -> Devices & Services -> Ampio -> overflow -> Download
diagnostics) and the maintainer has everything needed to reproduce the
misbehaviour without follow-up questions. Production diagnostics, not a
debug toggle: the same blob serves first-time-setup support, intermittent-
connection reports, "expected this entity to show up" questions, and
protocol-edge-case parser regressions.

What the file does **not** contain: the broker host / IP, the M-SERV's LAN
IP, the DHCP-derived Ethernet MAC, credentials. What it **does** contain
(intentionally, because they're what makes a report actionable): the
user-given Ampio device, room and location names, the M-SERV's CAN macs
and the serial numbers of every module, every object's metadata
including its ``leafId``, and two protocol payloads (``devices_details``
and ``info``) verbatim from the broker. The redaction comments below
spell each one out so a user can review before sharing.
"""

from collections import Counter
from dataclasses import asdict
import json
import platform as _platform
from typing import Any

import ampio_mqtt
from ampio_mqtt import AmpioModule, AmpioObject, Capability

from homeassistant import const as _ha_const
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, PLATFORMS
from .coordinator import AmpioConfigEntry, AmpioLocalCoordinator
from .entity import discovery_skip_reason
from .sensor import SENSOR_KIND_KEYS

# Strip credentials, the broker host, and the DHCP-derived Ethernet MAC
# from the config entry. Strip the M-SERV's LAN IP from server_info.
# `PROTOCOL_PAYLOAD_REDACT` walks the verbatim payloads kept under
# `protocol_payloads` and defensively redacts a small set of IP/MAC-shaped
# key names; the M-SERV does not emit any of these today on the catalogues
# we ship, but a firmware revision that does should not leak by surprise.
ENTRY_REDACT = {CONF_HOST, CONF_MAC, CONF_PASSWORD, CONF_USERNAME}
SERVER_INFO_REDACT = {"local_ip"}
PROTOCOL_PAYLOAD_REDACT = {"local_ip", "lan_ip", "wan_ip", "gateway", "hostname"}

# Maps a module Capability to the HA platforms a future revision of this
# integration would activate. Used to surface, per module, the platforms
# the hardware supports but the integration does not implement yet -
# directly answers "I have an M-REL, why are there no switches?".
# Structural flags (UI_PANEL / BRIDGE / HUB / etc.) deliberately have no
# entry: no HA platform is planned for them.
_PLATFORMS_BY_CAPABILITY: dict[Capability, frozenset[str]] = {
    Capability.DIGITAL_OUTPUT: frozenset({"switch"}),
    Capability.DIGITAL_INPUT: frozenset({"binary_sensor", "event"}),
    Capability.ANALOG_INPUT: frozenset({"sensor"}),
    Capability.TEMPERATURE_INPUT: frozenset({"sensor"}),
    Capability.ENV_SENSOR: frozenset({"sensor"}),
    Capability.ROLLER_OUTPUT: frozenset({"cover"}),
    Capability.RGBW_OUTPUT: frozenset({"light"}),
    Capability.IR_OUTPUT: frozenset({"remote"}),
    Capability.ALARM: frozenset({"alarm_control_panel"}),
    Capability.AUDIO_VIDEO: frozenset({"media_player"}),
}

_IMPLEMENTED_PLATFORMS = frozenset(p.value for p in PLATFORMS)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AmpioConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the integration's config entry."""
    coordinator = entry.runtime_data
    client = coordinator.client
    server_info = (
        async_redact_data(asdict(client.server_info), SERVER_INFO_REDACT)
        if client.server_info is not None
        else None
    )
    return {
        "versions": _versions(),
        "entry": {
            "data": async_redact_data(dict(entry.data), ENTRY_REDACT),
            "has_unique_id": entry.unique_id is not None,
            "version": entry.version,
        },
        "connection": _connection(client),
        "server_info": server_info,
        "modules": [_module_summary(m) for m in client.modules.values()],
        "objects": {
            "items": [_object_summary(o) for o in client.objects.values()],
            "overview": _objects_overview(client.objects),
        },
        "room_map": dict(coordinator.room_map),
        "location_map": dict(coordinator.location_map),
        "protocol_payloads": _protocol_payloads(client),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: AmpioConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single Ampio module device."""
    coordinator = entry.runtime_data
    client = coordinator.client
    module = _module_from_device(coordinator, device)
    return {
        "versions": _versions(),
        "connection": _connection(client),
        "module": _module_summary(module) if module else None,
        "objects": [
            _object_summary(o)
            for o in client.objects.values()
            if module is not None and o.device_id == module.id
        ],
    }


def _versions() -> dict[str, str]:
    """Stamp the running HA + library + Python triple onto every report."""
    return {
        "homeassistant": _ha_const.__version__,
        "ampio_mqtt": ampio_mqtt.__version__,
        "python": _platform.python_version(),
    }


def _connection(client: ampio_mqtt.AmpioClient) -> dict[str, Any]:
    """Liveness counters from the library plus the integration's view.

    `reconnect_count`, `last_error`, `started_at`, `last_message_at` answer
    the recurring "it works intermittently / it stopped updating" question
    without forcing the user to enable debug logging first.
    """
    stats = client.stats
    return {
        "available": client.available,
        "mserv_id": client.mserv_id,
        "reconnect_count": stats.reconnect_count,
        "last_error": stats.last_error,
        "started_at": stats.started_at,
        "last_message_at": stats.last_message_at,
    }


def _module_summary(module: AmpioModule) -> dict[str, Any]:
    """Public-safe snapshot of a module + the platforms it could but doesn't."""
    return {
        "id": module.id,
        "name": module.name,
        "type": module.type,
        "model": module.model,
        "capabilities": sorted(c.value for c in module.capabilities),
        "unimplemented_platforms": sorted(
            _unimplemented_platforms(module.capabilities)
        ),
        "sw_version": module.sw_version,
        "hw_version": module.hw_version,
        "mac": module.mac,
        "mac_global": module.mac_global,
        "last_seen": module.last_seen,
    }


def _unimplemented_platforms(capabilities: frozenset[Capability]) -> set[str]:
    """Platforms the module's hardware supports but the integration doesn't yet.

    The integration's capability vs platform coverage shifts as new
    platforms ship; this set shrinks accordingly. Today only `sensor` is
    implemented, so an M-REL surfaces `{"switch"}` and an M-COV surfaces
    `{"cover"}` until those platforms land.
    """
    supported: set[str] = set()
    for cap in capabilities:
        supported.update(_PLATFORMS_BY_CAPABILITY.get(cap, frozenset()))
    return supported - _IMPLEMENTED_PLATFORMS


def _object_summary(obj: AmpioObject) -> dict[str, Any]:
    """Per-object metadata + the precomputed entity-list decision.

    Deliberately omits live ``value``; ``has_value`` is the report-friendly
    answer. ``skip_reason`` mirrors the sensor platform's discovery filter
    and is the direct answer to "why isn't this an entity?": one of
    ``no_kind``, ``orphan``, ``unknown_kind``, ``not_visible``, or
    ``None`` when the object is on the entity list.

    ``hidden`` / ``matter_exposed`` / ``params`` expose the M-SERV's
    ``params`` bitfield so a ``not_visible`` decision is legible: an object
    with a populated ``leaf_id`` is still dropped when ``hidden`` (bit 4) is
    set - the stub half of a duplicated Designer channel, or a channel the
    user hid. ``matter_exposed`` (bit 37) is the per-object Matter opt-in.
    """
    return {
        "id": obj.id,
        "device_id": obj.device_id,
        "typ_komponentu": obj.typ_komponentu,
        "interpretacja": obj.interpretacja,
        "funkcja": obj.funkcja,
        "name": obj.name,
        "kind": obj.kind.key if obj.kind else None,
        "input_kind": obj.input_kind.key if obj.input_kind else None,
        "leaf_id": obj.leaf_id,
        "group_ids": sorted(obj.group_ids),
        "visible": obj.visible,
        "hidden": obj.hidden,
        "matter_exposed": obj.matter_exposed,
        "params": obj.params,
        "is_system": obj.is_system,
        "has_value": obj.value is not None,
        "skip_reason": discovery_skip_reason(obj, SENSOR_KIND_KEYS),
    }


def _objects_overview(objects: dict[int, AmpioObject]) -> dict[str, Any]:
    """Aggregate counts that surface coverage gaps and visibility filtering."""
    by_typ: Counter[str | None] = Counter()
    # `hidden_by_visibility` surfaces Designer-removed-but-still-returned
    # rows (ghosts) plus the rare real-but-ungrouped non-system case.
    hidden_by_visibility: Counter[str | None] = Counter()
    uncategorised: set[str] = set()
    classified_sensors = 0
    classified_inputs = 0
    system_count = 0
    ungrouped_visible_count = 0
    sensor_without_funkcja = 0
    included_in_entity_list_count = 0
    for o in objects.values():
        by_typ[o.typ_komponentu] += 1
        if o.is_sensor:
            classified_sensors += 1
            if o.funkcja is None:
                sensor_without_funkcja += 1
        if o.is_input:
            classified_inputs += 1
        if o.is_system:
            system_count += 1
        elif not o.visible:
            hidden_by_visibility[o.typ_komponentu] += 1
        if o.visible and not o.group_ids and not o.is_system:
            ungrouped_visible_count += 1
        if o.kind is None and o.input_kind is None and o.typ_komponentu is not None:
            uncategorised.add(o.typ_komponentu)
        if discovery_skip_reason(o, SENSOR_KIND_KEYS) is None:
            included_in_entity_list_count += 1
    return {
        "total": len(objects),
        "classified_sensors": classified_sensors,
        "classified_inputs": classified_inputs,
        "system_count": system_count,
        "ungrouped_visible_count": ungrouped_visible_count,
        "sensor_without_funkcja": sensor_without_funkcja,
        "included_in_entity_list_count": included_in_entity_list_count,
        "by_typ_komponentu": dict(by_typ),
        "hidden_by_visibility_by_typ_komponentu": dict(hidden_by_visibility),
        "uncategorised_typ_komponentu": sorted(uncategorised),
    }


def _protocol_payloads(client: ampio_mqtt.AmpioClient) -> dict[str, Any]:
    """Verbatim broker payloads kept for protocol-edge-case debugging.

    Most of the M-SERV's discovery surface is already covered by the
    structured `modules` / `objects` / `room_map` blocks. The two payloads
    kept here are the ones where the structured form is the *least* likely
    to capture an installation-specific quirk:

    - ``devices_details``: per-object catalogue, the field set varies
      across M-SERV firmware revisions and DB schemas. When an object's
      `leafId` (or any future field) drives a parser change, this is the
      data that reproduces it.
    - ``info``: server self-report. Small (a dozen fields) and identifies
      the broker side end-to-end.

    Both are parsed once here so the report reader does not have to unpack
    JSON-in-string escapes, then walked with `PROTOCOL_PAYLOAD_REDACT` to
    strip IP/MAC-shaped keys defensively. Object names and room names are
    intentionally kept - they're what make a report actionable.
    """
    payloads = client.last_payloads
    return {
        "devices_details": _parse_redacted(payloads.get("details")),
        "info": _parse_redacted(payloads.get("info")),
    }


def _parse_redacted(payload: str | None) -> Any:
    """Parse a stored payload to JSON and apply the IP/MAC key-name redaction."""
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return {"_unparseable": payload[:200]}
    return async_redact_data(data, PROTOCOL_PAYLOAD_REDACT)


def _module_from_device(
    coordinator: AmpioLocalCoordinator, device: dr.DeviceEntry
) -> AmpioModule | None:
    """Resolve a device entry back to the AmpioModule it represents.

    Identifiers are built by ``module_device_info`` as
    ``(DOMAIN, "{prefix}:{module.mac}")``; rebuild and look up by set
    membership instead of parsing the string back out.
    """
    prefix = coordinator.identifier_prefix
    for module in coordinator.client.modules.values():
        if (DOMAIN, f"{prefix}:{module.mac}") in device.identifiers:
            return module
    return None
