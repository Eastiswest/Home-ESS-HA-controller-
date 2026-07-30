"""Persistence for the learning model."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN, STORAGE_VERSION
from .model import LearningModel

_LOGGER = logging.getLogger(__name__)

# The model changes slowly, so batching writes keeps flash wear down on the
# SD-card / eMMC installs that most HA boxes run on.
SAVE_DELAY_SECONDS = 300


class LearningStore:
    """Loads and saves a :class:`LearningModel` for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.learning"
        )
        self.model = LearningModel()
        self._loaded = False

    async def async_load(self) -> LearningModel:
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 - never block setup on a bad store
            _LOGGER.exception("Failed to load learning data; starting fresh")
            data = None
        if data:
            self.model.load_dict(data)
            _LOGGER.debug(
                "Loaded learning model: %s solar buckets, %s load buckets",
                len(self.model.solar_absolute),
                len(self.model.load),
            )
        self._loaded = True
        return self.model

    @property
    def loaded(self) -> bool:
        return self._loaded

    def async_schedule_save(self) -> None:
        """Queue a debounced save of the current model."""
        self._store.async_delay_save(self._data_func, SAVE_DELAY_SECONDS)

    async def async_save(self) -> None:
        """Write immediately (used on shutdown and after a reset)."""
        await self._store.async_save(self._data_func())

    async def async_reset(self) -> None:
        self.model.reset()
        await self.async_save()

    def _data_func(self) -> dict[str, Any]:
        return self.model.as_dict()
