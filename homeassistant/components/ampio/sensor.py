"""Sensor platform for the Ampio integration."""

from collections import Counter
import dataclasses
from datetime import UTC, datetime
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfPressure,
    UnitOfSoundPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AmpioConfigEntry, AmpioLocalCoordinator
from .entity import AmpioEntity, AmpioModuleEntity, discovery_skip_reason

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Static descriptions for the known sensor kinds the library can report.
# Translation keys map into strings.json -> entity.sensor.<key>.name.
_SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    "temperature": SensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "humidity": SensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "pressure_abs": SensorEntityDescription(
        key="pressure_abs",
        translation_key="pressure_abs",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "pressure_rel": SensorEntityDescription(
        key="pressure_rel",
        translation_key="pressure_rel",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "loudness": SensorEntityDescription(
        key="loudness",
        translation_key="loudness",
        device_class=SensorDeviceClass.SOUND_PRESSURE,
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    "illuminance": SensorEntityDescription(
        key="illuminance",
        translation_key="illuminance",
        device_class=SensorDeviceClass.ILLUMINANCE,
        native_unit_of_measurement=LIGHT_LUX,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    "iaq": SensorEntityDescription(
        key="iaq",
        translation_key="iaq",
        device_class=SensorDeviceClass.AQI,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    "co2": SensorEntityDescription(
        key="co2",
        translation_key="co2",
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
}

# Public view of the sensor-kind keys for cross-module consumers (diagnostics
# uses this to mirror the discovery filter without importing the description
# table itself, which is sensor-platform internal).
SENSOR_KIND_KEYS: frozenset[str] = frozenset(_SENSOR_DESCRIPTIONS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AmpioConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Ampio sensors, adding new ones as they are discovered."""
    coordinator = entry.runtime_data
    # Only accepted objects are remembered as decided. Rejected objects are
    # re-evaluated on every discovery tick so a phantom whose ``leafId`` later
    # arrives can be promoted to an entity. ``last_skip_reason`` debounces the
    # DEBUG log so repeated rejections do not spam unless the reason changes.
    accepted_objects: set[int] = set()
    seen_modules: set[int] = set()
    last_skip_reason: dict[int, str] = {}

    @callback
    def _discover() -> None:
        new_modules = [
            mid for mid in coordinator.client.modules if mid not in seen_modules
        ]
        new_sensors: list[tuple[int, SensorEntityDescription]] = []
        skip_reasons: Counter[str] = Counter()
        for oid, obj in coordinator.client.sensors.items():
            if oid in accepted_objects:
                continue
            reason = discovery_skip_reason(obj, SENSOR_KIND_KEYS)
            if reason is not None:
                if last_skip_reason.get(oid) != reason:
                    last_skip_reason[oid] = reason
                    skip_reasons[reason] += 1
                continue
            # `obj.visible` (checked inside `discovery_skip_reason`) is the
            # M-SERV's own predicate (non-empty `leafId`, or group membership,
            # or a system object). Ghost rows that survived removal from the
            # Designer tree fail it; without this filter we would expose
            # objects the user no longer sees in Designer.
            kind = obj.kind
            if kind is None:
                continue
            last_skip_reason.pop(oid, None)
            accepted_objects.add(oid)
            new_sensors.append((oid, _SENSOR_DESCRIPTIONS[kind.key]))
        if skip_reasons:
            _LOGGER.debug(
                "ampio sensor discovery: kept=%d skipped=%s",
                len(new_sensors),
                dict(skip_reasons),
            )
        if not new_sensors and not new_modules:
            return
        seen_modules.update(new_modules)
        entities: list[SensorEntity] = [
            AmpioModuleLastSeenSensor(coordinator, mid) for mid in new_modules
        ]
        entities.extend(
            AmpioSensor(coordinator, oid, description)
            for oid, description in new_sensors
        )
        async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class AmpioSensor(AmpioEntity, SensorEntity):
    """A sensor backed by an Ampio DB object."""

    def __init__(
        self,
        coordinator: AmpioLocalCoordinator,
        object_id: int,
        description: SensorEntityDescription,
    ) -> None:
        """Initialize the sensor from its classified object."""
        super().__init__(coordinator, object_id)
        obj = coordinator.client.objects[object_id]
        # ``opis_menu`` is optional in the Ampio data model: visible objects
        # usually carry a Designer label, but some channels never get one
        # (e.g. an unlabelled CO2 input). When the label is present it wins
        # and the translation_key is dropped so the entity registry never
        # records both a vendor name and a translation key for the same row.
        # When the label is missing, the static description's translation_key
        # provides the kind-derived fallback.
        if obj.name:
            self.entity_description = dataclasses.replace(
                description, translation_key=None
            )
            self._attr_name = obj.name
        else:
            self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current value as a float, or None on parse failure."""
        obj = self.object
        if obj is None or obj.value is None:
            return None
        try:
            return float(obj.value)
        except ValueError:
            _LOGGER.debug(
                "Non-numeric value %r for object %d; surfacing as unknown",
                obj.value,
                self._object_id,
            )
            return None


class AmpioModuleLastSeenSensor(AmpioModuleEntity, SensorEntity):
    """Diagnostic timestamp of the last state any module object reported."""

    _attr_translation_key = "module_last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AmpioLocalCoordinator, module_id: int) -> None:
        """Initialize the diagnostic sensor for a physical module."""
        super().__init__(coordinator, module_id)
        module = coordinator.client.modules[module_id]
        self._attr_unique_id = (
            f"{coordinator.identifier_prefix}_module_{module.mac}_last_seen"
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the last-seen timestamp as a timezone-aware datetime."""
        module = self.coordinator.client.modules.get(self._module_id)
        if module is None or module.last_seen is None:
            return None
        return datetime.fromtimestamp(module.last_seen, tz=UTC)
