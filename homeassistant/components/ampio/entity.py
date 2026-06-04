"""Base entity for the Ampio integration."""

from ampio_mqtt import AmpioObject

from homeassistant.const import CONF_MAC
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AmpioLocalCoordinator


def discovery_skip_reason(obj: AmpioObject, sensor_kinds: frozenset[str]) -> str | None:
    """Return why the sensor platform would skip an object, or None if accepted.

    Single source of truth for the discovery filter so the platform code
    and the diagnostics report stay aligned. ``sensor_kinds`` is the set of
    ``SensorKind.key`` values the platform has entity descriptions for.
    """
    if obj.kind is None:
        return "no_kind"
    if obj.device_id is None:
        return "orphan"
    if obj.kind.key not in sensor_kinds:
        return "unknown_kind"
    if not obj.visible:
        return "not_visible"
    return None


def object_unique_key(coordinator: AmpioLocalCoordinator, obj: AmpioObject) -> str:
    """Replacement-stable composite unique_id for one object.

    The Ampio Designer override mac (``module.mac``), the object's
    ``typ_komponentu`` and its channel index (``funkcja``) all survive a
    hardware swap; the DB ``id`` and ``device_id`` (id_urzadzenia) do not.
    Crashes loudly if a future library change ever leaves ``typ_komponentu``
    or ``funkcja`` unset - we never want to fall back to a hardware-ordered
    key that defeats the replacement-stability we're after.
    """
    if obj.device_id is None:
        raise RuntimeError(
            f"Object {obj.id} has no device_id; cannot compute a stable unique_id"
        )
    if obj.typ_komponentu is None or obj.funkcja is None:
        raise RuntimeError(
            f"Object {obj.id} is missing the fields required for a stable unique_id "
            f"(typ_komponentu={obj.typ_komponentu!r}, funkcja={obj.funkcja!r})"
        )
    module = coordinator.client.modules[obj.device_id]
    return (
        f"{coordinator.identifier_prefix}_obj_"
        f"{module.mac}_{obj.typ_komponentu}_{obj.funkcja}"
    )


def module_device_info(
    coordinator: AmpioLocalCoordinator, module_id: int
) -> DeviceInfo:
    """Build the device info for a physical module.

    Identifiers are keyed on the module's override CAN mac (``module.mac``),
    the only value that survives a hardware swap; the DB ``id_urzadzenia``
    is hardware-ordered and would orphan the HA device on replacement. The
    M-SERV module gets its sw_version and configuration_url from the server
    info reply; every other module is linked back through ``via_device``.
    """
    prefix = coordinator.identifier_prefix
    module = coordinator.client.modules[module_id]
    mserv_id = coordinator.mserv_id
    server_info = coordinator.client.server_info
    is_mserv = module_id == mserv_id

    info: DeviceInfo = {
        "identifiers": {(DOMAIN, f"{prefix}:{module.mac}")},
        "name": module.name or f"Ampio module {module.mac}",
        "manufacturer": "Ampio",
    }
    if module.model:
        info["model"] = module.model
    # The override CAN mac (`module.mac`) is the device's bus address - what
    # the raw MQTT topics (`ampio/from/<mac>/...`), Ampio Designer, and CAN
    # traces all use. Surfacing it as the serial lets a maintainer correlate
    # the HA device with what they see in those tools. The factory id
    # (`mac_global`) stays in diagnostics for anyone who needs the physical-
    # unit identifier.
    info["serial_number"] = f"0x{module.mac:X}"
    if is_mserv and server_info is not None and server_info.server_version:
        info["sw_version"] = server_info.server_version
    elif module.sw_version is not None:
        info["sw_version"] = str(module.sw_version)
    if module.hw_version is not None:
        info["hw_version"] = str(module.hw_version)
    if is_mserv and server_info is not None and server_info.local_ip:
        info["configuration_url"] = f"http://{server_info.local_ip}"
    if is_mserv:
        if ethernet_mac := coordinator.config_entry.data.get(CONF_MAC):
            info["connections"] = {(CONNECTION_NETWORK_MAC, ethernet_mac)}
    else:
        mserv_module = coordinator.client.modules[mserv_id]
        info["via_device"] = (DOMAIN, f"{prefix}:{mserv_module.mac}")
        # The M-SERV is infrastructure (server cabinet, comms closet); even
        # when its module-attached objects share a room, an area there is a
        # worse hint than none.
        if (room := coordinator.module_room_hints.get(module_id)) is not None:
            info["suggested_area"] = room
    return info


class _AmpioBaseEntity(CoordinatorEntity[AmpioLocalCoordinator]):
    """Shared base for Ampio entities.

    Carries the ``has_entity_name`` flag and the connection-availability
    short-circuit. Subclasses contribute only their scope predicate
    (the backing object exists, or the module is still known).
    """

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Available when the coordinator is up and the broker is connected."""
        return super().available and self.coordinator.client.available


class AmpioModuleEntity(_AmpioBaseEntity):
    """Base class for Ampio entities scoped to a physical module.

    Subscribes to the per-module push channel so any object update on the
    module triggers a refresh - this is how diagnostic entities like
    ``Last seen`` learn about state changes without paying the cost of a
    catchall fan-out across the integration's entity list.
    """

    def __init__(self, coordinator: AmpioLocalCoordinator, module_id: int) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._module_id = module_id
        self._attr_device_info = module_device_info(coordinator, module_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-module pushes plus the catchall availability channel."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_module_listener(
                self._module_id, self._handle_coordinator_update
            )
        )

    @property
    def available(self) -> bool:
        """Available when the module is still known."""
        return super().available and self._module_id in self.coordinator.client.modules


class AmpioEntity(_AmpioBaseEntity):
    """Base class for Ampio entities backed by a DB object."""

    def __init__(self, coordinator: AmpioLocalCoordinator, object_id: int) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._object_id = object_id
        obj = coordinator.client.objects[object_id]
        if obj.device_id is None:
            raise RuntimeError(
                f"AmpioEntity({object_id}) constructed for an orphan object"
            )
        self._attr_unique_id = object_unique_key(coordinator, obj)
        self._attr_device_info = module_device_info(coordinator, obj.device_id)

    async def async_added_to_hass(self) -> None:
        """Subscribe to per-object pushes plus the catchall availability channel.

        ``CoordinatorEntity.async_added_to_hass`` subscribes to the catchall;
        keeping that gives us availability flips when the broker drops. The
        per-object subscription is what scales state pushes - the catchall
        alone would refresh every entity on every push.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_object_listener(
                self._object_id, self._handle_coordinator_update
            )
        )

    @property
    def object(self) -> AmpioObject | None:
        """The current backing object, if still present."""
        return self.coordinator.client.objects.get(self._object_id)

    @property
    def available(self) -> bool:
        """Available when the backing object still exists."""
        return super().available and self.object is not None
