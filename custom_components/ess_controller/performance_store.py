"""Persistence for the performance history.

Split from :mod:`.performance` so the metrics stay Home Assistant-free and
directly testable, matching how the learning model and its store are separated.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .performance import PerformanceLog

_LOGGER = logging.getLogger(__name__)

# Longer than the learning store's delay: history is appended twice an hour and
# losing the last half-hour to a hard restart costs one row, not a model.
SAVE_DELAY_SECONDS = 60


class PerformanceStore:
    """Persists the slot history for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.performance"
        )
        self.log = PerformanceLog()

    async def async_load(self, retention_days: int | None = None) -> PerformanceLog:
        try:
            data = await self._store.async_load()
        except Exception:
            # A corrupt history must never stop the controller planning: the
            # log is a reporting convenience, not part of the control path.
            _LOGGER.exception("Failed to load performance history; starting fresh")
            data = None
        self.log = PerformanceLog.from_dict(data)
        if retention_days is not None:
            self.log.retention_days = retention_days
        return self.log

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self.log.as_dict, SAVE_DELAY_SECONDS)

    async def async_save(self) -> None:
        await self._store.async_save(self.log.as_dict())

    async def async_clear(self) -> None:
        self.log.clear()
        await self._store.async_save(self.log.as_dict())
