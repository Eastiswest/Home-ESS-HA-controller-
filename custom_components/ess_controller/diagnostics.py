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
from homeassistant.util import dt as dt_util

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
        # When the file was taken, which is not the same as when the plan was
        # built and is the difference between "this is what it is doing now" and
        # "this is what it decided an hour ago and has not revisited". Reading
        # the plan's own timestamp as the capture time once produced a confident
        # and completely wrong explanation of why a screenshot and a download
        # disagreed. Both are here now, so neither has to be inferred.
        "generated_at": dt_util.utcnow().isoformat(),
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": async_redact_data(dict(entry.options), REDACT),
        },
    }

    if coordinator is None:
        data["error"] = "coordinator not loaded"
        return data

    data["controller"] = async_redact_data(coordinator.diagnostics(), REDACT)
    data["forecast_totals"] = coordinator.forecast_totals()
    data["cheapest_slots"] = coordinator.cheapest_slots(12)

    # Why the sidebar dashboard is or is not there. Every failure mode looks the
    # same from the outside, so the state it depends on is reported explicitly.
    try:
        from .panel import describe as describe_dashboard

        data["dashboard"] = describe_dashboard(hass, entry)
    except Exception as err:  # pragma: no cover - diagnostics must not fail
        data["dashboard"] = {"error": str(err)}

    # Recent history, so a diagnostics file answers "has it been working?" and
    # not only "what is it doing this minute". Bounded to two days of slots:
    # enough to see a full cycle of behaviour without producing a file too large
    # to attach to an issue. Use the export_performance action for the rest.
    data["performance"] = {
        "summary_7d": coordinator.performance_summary(7.0),
        "summary_30d": coordinator.performance_summary(30.0),
        "recent_slots": coordinator.performance_rows(2.0),
        "records_held": len(coordinator.performance_store.log),
    }
    return data
