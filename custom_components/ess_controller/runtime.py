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
        self._seen_options: dict[str, Any] = {}

    async def async_load(self, options: dict[str, Any]) -> RuntimeSettings:
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.exception("Failed to load runtime settings; using defaults")
            data = None
        self.settings = RuntimeSettings.from_dict(data)
        tracked = RuntimeSettings.tracked_options(options)
        seen = (data or {}).get("seen_options")
        if not self.settings.seeded:
            self.settings.seed_from_options(options)
        elif isinstance(seen, dict):
            # Only a key that changed since the snapshot is applied, so a stale
            # option sitting in the entry cannot undo a dashboard tune, while an
            # edit actually made through Configure finally takes effect.
            changed = self.settings.apply_option_changes(seen, tracked)
            if changed:
                _LOGGER.info("Options edit applied to runtime settings: %s", changed)
        # A store from before the snapshot existed starts recording here without
        # applying anything: with no baseline, an old edit and a stale value are
        # indistinguishable, and guessing would reprice plans on an upgrade.
        self._seen_options = tracked
        if not data or seen != tracked:
            await self.async_save()
        return self.settings

    def _payload(self) -> dict[str, Any]:
        # from_dict filters unknown keys, so the snapshot rides along unharmed.
        return {**self.settings.as_dict(), "seen_options": self._seen_options}

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self._payload, SAVE_DELAY_SECONDS)

    async def async_save(self) -> None:
        await self._store.async_save(self._payload())
