"""AI ESS Controller: tariff-aware battery dispatch for Home Assistant."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ACTION,
    ATTR_DURATION,
    ATTR_POWER,
    ATTR_TARGET_SOC,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_RECOMMEND_TARIFFS,
    SERVICE_REPLAN,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_OVERRIDE,
)
from .coordinator import EssCoordinator
from .models import SlotAction

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

OVERRIDE_ACTIONS = [
    SlotAction.CHARGE.value,
    SlotAction.DISCHARGE.value,
    SlotAction.IDLE.value,
    SlotAction.SELF_USE.value,
    SlotAction.CHARGE_SOLAR_ONLY.value,
]

SET_OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Required(ATTR_ACTION): vol.In(OVERRIDE_ACTIONS),
        vol.Optional(ATTR_DURATION, default={"minutes": 60}): cv.time_period,
        vol.Optional(ATTR_POWER): vol.Coerce(float),
        vol.Optional(ATTR_TARGET_SOC): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
    }
)

ENTRY_ONLY_SCHEMA = vol.Schema({vol.Optional("entry_id"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, regardless of how many entries exist."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured ESS controller."""
    coordinator = EssCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not set up ESS controller: {err}") from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down an entry, persisting anything learned first."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EssCoordinator | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            # Losing a partial day of learning on every restart would slow the
            # model down considerably, so flush before going away.
            await coordinator.async_shutdown_store()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[EssCoordinator]:
    """Resolve which coordinators a service call targets."""
    entries: dict[str, EssCoordinator] = hass.data.get(DOMAIN, {})
    entry_id = call.data.get("entry_id")
    if entry_id:
        coordinator = entries.get(entry_id)
        return [coordinator] if coordinator else []
    return list(entries.values())


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REPLAN):
        return

    async def async_replan(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_request_refresh()

    async def async_set_override(call: ServiceCall) -> None:
        action = SlotAction(call.data[ATTR_ACTION])
        duration: timedelta = call.data[ATTR_DURATION]
        power = call.data.get(ATTR_POWER)
        target_soc = call.data.get(ATTR_TARGET_SOC)
        for coordinator in _coordinators(hass, call):
            await coordinator.async_set_override(
                action, duration, power_kw=power, target_soc=target_soc
            )

    async def async_clear_override(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_clear_override()

    async def async_reset_learning(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_reset_learning()

    async def async_recommend_tariffs(call: ServiceCall) -> ServiceResponse:
        results: dict[str, Any] = {}
        for coordinator in _coordinators(hass, call):
            recommendation = await coordinator.async_recommend_tariffs()
            if recommendation is not None:
                results[coordinator.entry.entry_id] = recommendation.as_dict()
        return {"recommendations": results}

    hass.services.async_register(
        DOMAIN, SERVICE_REPLAN, async_replan, schema=ENTRY_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_OVERRIDE, async_set_override, schema=SET_OVERRIDE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_OVERRIDE, async_clear_override, schema=ENTRY_ONLY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_LEARNING, async_reset_learning, schema=ENTRY_ONLY_SCHEMA
    )
    # Returns the ranked comparison, so it is a response-aware service.
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECOMMEND_TARIFFS,
        async_recommend_tariffs,
        schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
