"""Persistence for the live-tunable runtime settings.

The settings themselves live in :mod:`.settings`, which is Home Assistant-free
so its clamping rules can be tested directly.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .settings import RuntimeSettings

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY_SECONDS = 10

__all__ = ["RuntimeSettings", "RuntimeStore"]


class RuntimeStore:
    """Persists :class:`RuntimeSettings` for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.runtime"
        )
        self.settings = RuntimeSettings()

    async def async_load(self, options: dict[str, Any]) -> RuntimeSettings:
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.exception("Failed to load runtime settings; using defaults")
            data = None
        self.settings = RuntimeSettings.from_dict(data)
        self.settings.seed_from_options(options)
        if not data:
            await self.async_save()
        return self.settings

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self.settings.as_dict, SAVE_DELAY_SECONDS)

    async def async_save(self) -> None:
        await self._store.async_save(self.settings.as_dict())
