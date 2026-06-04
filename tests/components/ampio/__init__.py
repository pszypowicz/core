"""Tests for the Ampio integration."""

from tests.common import MockConfigEntry


def device_prefix(entry: MockConfigEntry) -> str:
    """Identifier prefix used by the integration's DeviceInfo entries."""
    return entry.unique_id or entry.entry_id
