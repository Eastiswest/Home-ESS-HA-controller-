"""Shared entity base for the AI ESS Controller."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, NAME, VERSION
from .coordinator import EssCoordinator


class EssEntity(CoordinatorEntity[EssCoordinator]):
    """Base class binding every entity to the one controller device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EssCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title or NAME,
            manufacturer=MANUFACTURER,
            model="Battery dispatch optimiser",
            sw_version=VERSION,
        )

    @property
    def settings(self):
        return self.coordinator.settings
