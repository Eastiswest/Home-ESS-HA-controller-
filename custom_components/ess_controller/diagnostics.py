"""Diagnostics download.

Everything needed to explain why the controller did what it did: the resolved
specs, the prices it planned against, the forecast provenance for each slot, the
full plan, and the exact writes attempted. This is the first thing to ask for
when a plan looks wrong.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_OCTOPUS_ACCOUNT, CONF_OCTOPUS_API_KEY, DOMAIN
from .coordinator import EssCoordinator

# An Octopus API key grants access to consumption data and account details, and
# the account number identifies the user, so neither belongs in a file people
# paste into a public issue.
REDACT = {CONF_OCTOPUS_API_KEY, CONF_OCTOPUS_ACCOUNT}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: EssCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    data: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": async_redact_data(dict(entry.options), REDACT),
        }
    }

    if coordinator is None:
        data["error"] = "coordinator not loaded"
        return data

    data["controller"] = async_redact_data(coordinator.diagnostics(), REDACT)
    data["forecast_totals"] = coordinator.forecast_totals()
    data["cheapest_slots"] = coordinator.cheapest_slots(12)
    return data
