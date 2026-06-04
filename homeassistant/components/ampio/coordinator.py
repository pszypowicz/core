"""Coordinator for the Ampio (local MQTT) integration."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

from ampio_mqtt import AmpioAuthError, AmpioClient, AmpioConnectionError, AmpioObject

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type AmpioConfigEntry = ConfigEntry[AmpioLocalCoordinator]


async def _fetch_optional(
    coro: Awaitable[dict[int, str]], label: str
) -> dict[int, str]:
    """Return the awaited dict, or {} on a connection error (debug-logged)."""
    try:
        return await coro
    except AmpioConnectionError:
        _LOGGER.debug("Failed to fetch Ampio %s map", label, exc_info=True)
        return {}


def _add_listener(
    registry: dict[int, list[CALLBACK_TYPE]],
    key: int,
    update_callback: CALLBACK_TYPE,
) -> Callable[[], None]:
    """Append ``update_callback`` under ``key`` and return its unsubscribe."""
    registry.setdefault(key, []).append(update_callback)

    @callback
    def _remove() -> None:
        listeners = registry.get(key)
        if listeners is None:
            return
        listeners.remove(update_callback)
        if not listeners:
            del registry[key]

    return _remove


class AmpioLocalCoordinator(DataUpdateCoordinator[dict[int, AmpioObject]]):
    """Maintains the Ampio connection and pushes object updates to entities."""

    config_entry: AmpioConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: AmpioConfigEntry, client: AmpioClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN)
        self.client = client
        self.room_map: dict[int, str] = {}
        self.location_map: dict[int, str] = {}
        self.module_room_hints: dict[int, str | None] = {}
        self._object_listeners: dict[int, list[CALLBACK_TYPE]] = {}
        self._module_listeners: dict[int, list[CALLBACK_TYPE]] = {}

    @callback
    def async_add_object_listener(
        self, object_id: int, update_callback: CALLBACK_TYPE
    ) -> Callable[[], None]:
        """Subscribe ``update_callback`` to pushes for a single object.

        Returns an unsubscribe closure. Push fan-out is per-object so an
        installation with hundreds of entities does not pay N work on every
        state push. Availability transitions still ride the catchall
        ``async_add_listener`` channel so every entity flips together.
        """
        return _add_listener(self._object_listeners, object_id, update_callback)

    @callback
    def async_add_module_listener(
        self, module_id: int, update_callback: CALLBACK_TYPE
    ) -> Callable[[], None]:
        """Subscribe ``update_callback`` to pushes for any object on ``module_id``.

        Used by per-module diagnostic entities (e.g. ``Last seen``) that
        reflect aggregate state across all of a module's objects.
        """
        return _add_listener(self._module_listeners, module_id, update_callback)

    @property
    def identifier_prefix(self) -> str:
        """Stable identifier prefix sourced from the M-SERV MAC.

        The config flow refuses to create an entry without a server identity,
        so ``unique_id`` is always set by the time the coordinator runs.
        """
        unique_id = self.config_entry.unique_id
        if unique_id is None:
            raise RuntimeError(
                "Ampio coordinator started without a unique_id; "
                "this is a programmer error"
            )
        return unique_id

    @property
    def mserv_id(self) -> int:
        """Module id of the M-SERV broker.

        ``_async_setup`` raises ``ConfigEntryAuthFailed`` when the library
        does not report one, so by the time platforms see the coordinator
        this is guaranteed to be set.
        """
        mserv_id = self.client.mserv_id
        if mserv_id is None:
            raise RuntimeError(
                "Ampio coordinator queried for mserv_id before setup completed"
            )
        return mserv_id

    def _build_module_room_hints(self) -> None:
        """Populate ``module_room_hints`` once per setup.

        The hint feeds ``DeviceInfo.suggested_area`` which only takes effect
        on first device creation, so a single pass at end of ``_async_setup``
        replaces the previous per-module lazy cache.
        """
        hints: dict[int, str | None] = dict.fromkeys(self.client.modules)
        if not self.room_map:
            self.module_room_hints = hints
            return
        per_module: dict[int, set[str]] = {}
        for obj in self.client.objects.values():
            if obj.device_id is None or obj.kind is None:
                continue
            room = self.room_map.get(obj.id)
            if room:
                per_module.setdefault(obj.device_id, set()).add(room)
        for module_id, rooms in per_module.items():
            if len(rooms) == 1:
                hints[module_id] = rooms.pop()
        self.module_room_hints = hints

    async def _async_setup(self) -> None:
        """Connect to the broker and start discovery (push-based)."""
        self.client.add_object_listener(self._handle_object)
        self.client.add_availability_listener(self._handle_availability)
        try:
            await self.client.start()
        except AmpioAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except AmpioConnectionError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err
        # Explicit opt-in to the discovery-completion contract. `start()`
        # blocks on the initial cycle today, but `wait_for_initial_discovery`
        # latches per-signal and is the documented hook to depend on.
        await self.client.wait_for_initial_discovery()
        if self.client.mserv_id is None:
            # The broker accepted the connection but never reported a server
            # identity. Only realistic cause is an account without permission
            # to read the Ampio config; surface as reauth so the user is told
            # what to fix (permissions), not that the credentials are wrong.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="no_server_info"
            )
        # Rooms feed `DeviceInfo.suggested_area`; the location-name table is
        # diagnostics-only (per-output pointers into it live on the modules'
        # CAN-resident description and are not published over MQTT). Both are
        # nice-to-have, so failure leaves the corresponding map empty.
        self.room_map, self.location_map = await asyncio.gather(
            _fetch_optional(self.client.fetch_rooms(), "room"),
            _fetch_optional(self.client.fetch_locations(), "location"),
        )
        self._build_module_room_hints()
        _LOGGER.debug(
            "Ampio discovery complete: modules=%d objects=%d mserv_id=%s "
            "room_map_entries=%d location_map_entries=%d",
            len(self.client.modules),
            len(self.client.objects),
            self.client.mserv_id,
            len(self.room_map),
            len(self.location_map),
        )

    async def _async_update_data(self) -> dict[int, AmpioObject]:
        """Return the current object snapshot (data arrives via push)."""
        return self.client.objects

    @callback
    def _handle_object(self, obj: AmpioObject) -> None:
        """Dispatch a per-object push to per-object and per-module listeners.

        Per-object listeners refresh exactly the entity backing ``obj.id``;
        per-module listeners refresh the module's diagnostic entities (e.g.
        ``Last seen``). Falls back to the catchall only when no listener is
        registered for the id - that path runs the platform's ``_discover``
        so freshly arriving objects are picked up.
        """
        for listener in self._object_listeners.get(obj.id, ()):
            listener()
        if obj.device_id is not None:
            for listener in self._module_listeners.get(obj.device_id, ()):
                listener()
        if obj.id not in self._object_listeners:
            self.async_update_listeners()

    @callback
    def _handle_availability(self, available: bool) -> None:
        """Log connection availability transitions and refresh entities."""
        if available:
            _LOGGER.info("Reconnected to Ampio broker")
        else:
            _LOGGER.warning("Lost connection to Ampio broker; retrying")
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Stop the client on unload."""
        await super().async_shutdown()
        await self.client.stop()
