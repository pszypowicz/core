"""Constants for the Ampio integration."""

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ampio"

PLATFORMS: Final = [Platform.SENSOR]

DEFAULT_PORT: Final = 1883

DEFAULT_HOST: Final = "ampio.local"

DISCOVER_TIMEOUT: Final = 1.5
