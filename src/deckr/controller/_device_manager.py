"""Compatibility name for the device session application boundary."""

from deckr.controller._device_session import DeviceSession

DeviceManager = DeviceSession

__all__ = ["DeviceManager"]
