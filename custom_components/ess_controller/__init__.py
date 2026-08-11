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
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ACTION,
    ATTR_DURATION,
    ATTR_POWER,
    ATTR_TARGET_SOC,
    CONF_CREATE_DASHBOARD,
    DOMAIN,
    NAME,
    PLATFORMS,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_EXPORT_PERFORMANCE,
    SERVICE_GENERATE_DASHBOARD,
    SERVICE_REBUILD_DASHBOARD,
    SERVICE_RECOMMEND_TARIFFS,
    SERVICE_REPLAN,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_OVERRIDE,
    VERSION,
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

# The entity registry can lag a fresh install by a few seconds, so a single look
# is not enough to conclude there is nothing to build a dashboard from.
DASHBOARD_ATTEMPTS = 4
DASHBOARD_RETRY_SECONDS = 15

GENERATE_DASHBOARD_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("write_file", default=False): cv.boolean,
    }
)

EXPORT_FORMATS = ["summary", "slots", "csv"]

EXPORT_PERFORMANCE_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("days", default=7): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=400)
        ),
        vol.Optional("format", default="summary"): vol.In(EXPORT_FORMATS),
        vol.Optional("write_file", default=False): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, regardless of how many entries exist."""
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a configured ESS controller."""
    # Logged so "is the version I think I installed actually running?" is
    # answerable from the log, which a stale HACS download makes a real question.
    _LOGGER.info("Setting up %s version %s", NAME, VERSION)
    coordinator = EssCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not set up ESS controller: {err}") from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    # Link status and live readings on their own fast clock, so they are not
    # hostage to the five-minute planning cycle.
    entry.async_on_unload(coordinator.async_start_live_polling())
    _async_register_services(hass)
    _async_schedule_dashboard(hass, entry, coordinator)
    return True


def _async_schedule_dashboard(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: EssCoordinator
) -> None:
    """Register the sidebar dashboard, after startup has settled.

    After startup because Lovelace has to be loaded before a panel can be
    attached to it, and because the entities must be in the registry before their
    ids can be read.

    On *every* setup, not once: the sidebar panel is in-memory and disappears with
    a restart or a reload, so registering it a single time would leave the
    dashboard working until the next reboot and then silently vanish. What
    happens only once is seeding the configuration -- after that the stored copy,
    including any edits, is what gets served.
    """
    if not entry.options.get(
        CONF_CREATE_DASHBOARD, entry.data.get(CONF_CREATE_DASHBOARD, True)
    ):
        return

    attempts = 0

    async def _install(_now: Any) -> None:
        nonlocal attempts
        from .panel import async_install

        attempts += 1
        try:
            outcome = await async_install(hass, entry)
        except Exception:
            _LOGGER.exception("Unexpected failure registering the dashboard")
            return

        if outcome == "waiting" and attempts < DASHBOARD_ATTEMPTS:
            # The entity registry can lag a fresh install, and giving up on the
            # first look is how this ends up doing nothing at all with nothing
            # said. Try again shortly.
            entry.async_on_unload(
                async_call_later(hass, DASHBOARD_RETRY_SECONDS, _install)
            )
            return
        if outcome == "waiting":
            _LOGGER.warning(
                "No ESS Controller entities were found after %d attempts, so no "
                "dashboard was created. Check the integration set up cleanly",
                attempts,
            )
            return
        if outcome != "failed" and not coordinator.settings.dashboard_created:
            coordinator.settings.dashboard_created = True
            coordinator.runtime_store.async_schedule_save()

    entry.async_on_unload(async_at_started(hass, _install))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down an entry, persisting anything learned first."""
    # Before the platforms go, so a reload does not leave a panel pointing at
    # entities that are about to be removed and then re-registered.
    from .panel import async_remove

    async_remove(hass, entry)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: EssCoordinator | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            # A hold raises the inverter's own reserve, and that setting outlives
            # us: going away mid-hold would leave the battery pinned shut.
            await coordinator.async_restore_reserve()
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
            # An explicit re-plan means "reconsider now", including the half-hour
            # already under way, which is otherwise held steady to stop churn.
            coordinator.clear_commitment()
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

    async def async_export_performance(call: ServiceCall) -> ServiceResponse:
        """Return the performance history, and optionally write it to a file.

        Response-only: the whole point is to get the numbers back out, whether
        into a template, the Developer Tools output box, or a CSV to hand to
        something that can read it.
        """
        days: float = call.data["days"]
        fmt: str = call.data["format"]
        write_file: bool = call.data["write_file"]
        results: dict[str, Any] = {}
        for coordinator in _coordinators(hass, call):
            payload: dict[str, Any] = {
                "days": days,
                "summary": coordinator.performance_summary(days),
            }
            if fmt == "slots":
                payload["slots"] = coordinator.performance_rows(days)
            elif fmt == "csv":
                payload["csv"] = coordinator.performance_csv(days)
            if write_file:
                payload["file"] = await coordinator.async_write_performance_csv(days)
            results[coordinator.entry.entry_id] = payload
        return {"controllers": results}

    async def async_generate_dashboard(call: ServiceCall) -> ServiceResponse:
        """Return the prebuilt dashboard configuration, for hand-installing.

        The same configuration the sidebar dashboard is built from, so it can be
        pasted into any dashboard's raw configuration editor, or used as a
        starting point for a custom one.
        """
        from .panel import build_for_entry, to_yaml

        write_file: bool = call.data["write_file"]
        results: dict[str, Any] = {}
        for coordinator in _coordinators(hass, call):
            config = build_for_entry(hass, coordinator.entry)
            payload: dict[str, Any] = {
                "views": len(config["views"]),
                "yaml": to_yaml(config),
                "config": config,
            }
            if write_file:
                from .panel import async_write_yaml

                payload["file"] = await async_write_yaml(hass, coordinator.entry, config)
            results[coordinator.entry.entry_id] = payload
        return {"dashboards": results}

    async def async_rebuild_dashboard(call: ServiceCall) -> ServiceResponse:
        """Replace the sidebar dashboard with a freshly generated one.

        The same thing the Rebuild dashboard button does, reachable without having
        to find it: the button carries ``EntityCategory.CONFIG``, so Home Assistant
        files it away in a collapsed Configuration block on the device page where
        nobody looks. Discards edits, deliberately -- this is the "give me the
        current layout back" action.
        """
        results: dict[str, Any] = {}
        for coordinator in _coordinators(hass, call):
            results[
                coordinator.entry.entry_id
            ] = await coordinator.async_create_dashboard()
        return {"rebuilt": results}

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_DASHBOARD,
        async_rebuild_dashboard,
        schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_DASHBOARD,
        async_generate_dashboard,
        schema=GENERATE_DASHBOARD_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    # Exists only to return data, so a response is mandatory rather than optional.
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_PERFORMANCE,
        async_export_performance,
        schema=EXPORT_PERFORMANCE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    # Returns the ranked comparison, so it is a response-aware service.
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECOMMEND_TARIFFS,
        async_recommend_tariffs,
        schema=ENTRY_ONLY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
