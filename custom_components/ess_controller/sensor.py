"""Sensors exposing the plan, prices, forecasts and control status."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EssCoordinator
from .entity import EssEntity
from .models import SlotAction

_LOGGER = logging.getLogger(__name__)

# Plan costs are in minor currency units (pence for GBP). There is no HA unit
# for "pence", so the unit is left as a plain string and the currency is exposed
# as an attribute rather than pretending to be a monetary device class.
PRICE_UNIT = "p/kWh"
COST_UNIT = "p"


@dataclass(frozen=True, kw_only=True)
class EssSensorDescription(SensorEntityDescription):
    """A sensor described by a function of the coordinator."""

    value: Callable[[EssCoordinator], Any]
    attributes: Callable[[EssCoordinator], dict[str, Any]] | None = None


def _plan_action(coordinator: EssCoordinator) -> str | None:
    command = coordinator.last_command
    return command.action.value if command else None


def _plan_action_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    command = coordinator.last_command
    slot = coordinator.data.get("current_slot") if coordinator.data else None
    apply_result = coordinator.last_apply
    attributes: dict[str, Any] = {}
    if command is not None:
        attributes.update(
            {
                "power_kw": round(command.power_kw, 3),
                "target_soc": command.target_soc,
                "reason": command.reason,
                "slot_end": (
                    dt_util.as_local(command.slot_end).isoformat()
                    if command.slot_end
                    else None
                ),
            }
        )
    if slot is not None:
        attributes.update(
            {
                "slot_start": dt_util.as_local(slot.start).isoformat(),
                "import_price": round(slot.import_price, 3),
                "export_price": round(slot.export_price, 3),
                "forecast_solar_kwh": round(slot.pv_kwh, 3),
                "forecast_load_kwh": round(slot.load_kwh, 3),
            }
        )
    if apply_result is not None:
        attributes["applied"] = apply_result.summary()
        attributes["dry_run"] = apply_result.dry_run
        attributes["verified"] = apply_result.verified
    if coordinator.override is not None:
        attributes["override"] = coordinator.override.as_dict()
    return attributes


def _next_change(coordinator: EssCoordinator) -> str | None:
    slot = coordinator.data.get("next_change") if coordinator.data else None
    return slot.action.value if slot else None


def _next_change_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    slot = coordinator.data.get("next_change") if coordinator.data else None
    if slot is None:
        return {}
    return {
        "starts": dt_util.as_local(slot.start).isoformat(),
        "import_price": round(slot.import_price, 3),
        "power_kw": round(
            slot.charge_power_kw if slot.charge_ac_kwh > 0 else slot.discharge_power_kw,
            3,
        ),
        "target_soc": round(slot.soc_end, 1),
    }


def _plan_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    plan = coordinator.plan
    if plan is None:
        return {"error": coordinator.data.get("error") if coordinator.data else None}
    # The full slot list is what makes the plan auditable, and it drives
    # ApexCharts-style dashboard cards directly.
    return {
        "reason": plan.reason,
        "slots": [s.as_dict() for s in plan.slots],
        "baseline_cost": round(plan.baseline_cost, 2),
        "self_use_cost": round(plan.self_use_cost, 2),
        "saving_vs_self_use": round(plan.saving_vs_self_use, 2),
        "saving_vs_idle": round(plan.saving_vs_baseline, 2),
        # In *hours* as well as slots. "horizon_slots: 48" was read as a 48-hour
        # horizon when it is 48 half-hours -- and the setting that controls it is in
        # hours, so the same number meant two different things in two places.
        "horizon_slots": len(plan.slots),
        "horizon_hours": (
            round((plan.slots[-1].end - plan.slots[0].start).total_seconds() / 3600, 1)
            if plan.slots
            else 0.0
        ),
        # Whether the plan is looking as far ahead as its prices allow. A horizon
        # shorter than the prices in hand is a silent loss: a real install planned
        # 24 hours with 48 available, so it could not see that tomorrow evening was
        # dearer than today's cheap window and had no reason to buy into it.
        "horizon_reach": coordinator.horizon_reach,
        "created": dt_util.as_local(plan.created).isoformat(),
    }


def _control_status(coordinator: EssCoordinator) -> str:
    settings = coordinator.settings
    if not settings.enabled:
        return "disabled"
    if coordinator.override is not None:
        return "override"
    if settings.strategy != "auto":
        return f"locked: {settings.strategy}"
    if settings.dry_run:
        return "advisory (dry run)"
    apply_result = coordinator.last_apply
    if apply_result is not None and apply_result.errors:
        return "error"
    if apply_result is not None and not apply_result.verified:
        return "unverified"
    return "controlling"


def _control_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    apply_result = coordinator.last_apply
    attributes: dict[str, Any] = {
        "enabled": coordinator.settings.enabled,
        "dry_run": coordinator.settings.dry_run,
        "strategy": coordinator.settings.strategy,
        "adapter": coordinator.adapter.name,
        "capabilities": coordinator.adapter.capabilities.as_dict(),
    }
    if apply_result is not None:
        attributes["last_result"] = apply_result.summary()
        attributes["writes"] = [w.as_dict() for w in apply_result.writes]
        if apply_result.errors:
            attributes["errors"] = apply_result.errors
        if apply_result.unverified:
            attributes["unverified"] = apply_result.unverified
        if apply_result.skipped:
            attributes["skipped"] = apply_result.skipped
    inverter = coordinator.inverter_state
    attributes["inverter_mode"] = inverter.mode
    attributes["inverter_locked"] = inverter.locked
    attributes["inverter_reserve"] = inverter.min_soc
    # The floor that actually governs, when it disagrees with the plan's. A real
    # install had the inverter stuck at 90% while the plan worked to 20%, and nothing
    # compared the two.
    conflict = coordinator.reserve_conflict()
    if conflict:
        attributes["reserve_conflict"] = conflict
    rejected = coordinator.adapter.rejected_roles()
    if rejected:
        attributes["rejected_writes"] = rejected
    return attributes


def _day_total(coordinator: EssCoordinator, day: str, field: str) -> float | None:
    totals = coordinator.forecast_totals()
    entry = totals.get(day)
    if not entry:
        return None
    return round(entry[field], 2)


def _learning_progress(coordinator: EssCoordinator) -> float:
    confidence = coordinator.learning_store.model.confidence()
    solar = confidence["solar_maturity"]
    load = confidence["load_maturity"]
    return round((solar + load) / 2 * 100, 1)


SENSORS: tuple[EssSensorDescription, ...] = (
    EssSensorDescription(
        key="plan_action",
        translation_key="plan_action",
        name="Planned action",
        icon="mdi:battery-clock",
        device_class=SensorDeviceClass.ENUM,
        options=[action.value for action in SlotAction],
        value=_plan_action,
        attributes=_plan_action_attributes,
    ),
    EssSensorDescription(
        key="next_action",
        translation_key="next_action",
        name="Next action",
        icon="mdi:skip-next-outline",
        device_class=SensorDeviceClass.ENUM,
        options=[action.value for action in SlotAction],
        value=_next_change,
        attributes=_next_change_attributes,
    ),
    EssSensorDescription(
        key="control_status",
        translation_key="control_status",
        name="Control status",
        icon="mdi:shield-check-outline",
        value=_control_status,
        attributes=_control_attributes,
    ),
    EssSensorDescription(
        key="plan_cost",
        translation_key="plan_cost",
        name="Planned horizon cost",
        icon="mdi:cash",
        native_unit_of_measurement=COST_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda c: round(c.plan.total_cost, 2) if c.plan else None,
        attributes=_plan_attributes,
    ),
    EssSensorDescription(
        key="plan_saving",
        translation_key="plan_saving",
        name="Planned saving vs self-use",
        icon="mdi:piggy-bank-outline",
        native_unit_of_measurement=COST_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda c: round(c.plan.saving_vs_self_use, 2) if c.plan else None,
    ),
    EssSensorDescription(
        key="import_price",
        translation_key="import_price",
        name="Import price now",
        icon="mdi:transmission-tower-import",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda c: c.price_now("import"),
        attributes=lambda c: {
            **c.price_stats("import"),
            "cheapest_upcoming": c.cheapest_slots(8),
        },
    ),
    EssSensorDescription(
        key="export_price",
        translation_key="export_price",
        name="Export price now",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda c: c.price_now("export"),
        attributes=lambda c: c.price_stats("export"),
    ),
    EssSensorDescription(
        key="target_soc",
        translation_key="target_soc",
        name="Planned target SoC",
        icon="mdi:battery-charging-70",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=lambda c: (
            round(c.last_command.target_soc, 1)
            if c.last_command and c.last_command.target_soc is not None
            else None
        ),
    ),
    EssSensorDescription(
        key="solar_forecast_today",
        translation_key="solar_forecast_today",
        name="Solar forecast remaining today",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: _day_total(c, "today", "solar_kwh"),
        attributes=lambda c: {
            **c.forecast_totals(),
            # So the learned correction can be watched converging rather than
            # taken on trust.
            "learned_correction": c.solar_learning(),
        },
    ),
    EssSensorDescription(
        key="solar_forecast_tomorrow",
        translation_key="solar_forecast_tomorrow",
        name="Solar forecast tomorrow",
        icon="mdi:weather-sunny",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: _day_total(c, "tomorrow", "solar_kwh"),
    ),
    EssSensorDescription(
        key="load_forecast_today",
        translation_key="load_forecast_today",
        name="Load forecast remaining today",
        icon="mdi:home-lightning-bolt-outline",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: _day_total(c, "today", "load_kwh"),
    ),
    EssSensorDescription(
        key="load_forecast_tomorrow",
        translation_key="load_forecast_tomorrow",
        name="Load forecast tomorrow",
        icon="mdi:home-lightning-bolt-outline",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: _day_total(c, "tomorrow", "load_kwh"),
    ),
    EssSensorDescription(
        key="planned_grid_import",
        translation_key="planned_grid_import",
        name="Planned grid import",
        icon="mdi:transmission-tower",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: (
            round(sum(s.grid_import_kwh for s in c.plan.slots), 2) if c.plan else None
        ),
    ),
    EssSensorDescription(
        key="planned_grid_export",
        translation_key="planned_grid_export",
        name="Planned grid export",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        suggested_display_precision=2,
        value=lambda c: (
            round(sum(s.grid_export_kwh for s in c.plan.slots), 2) if c.plan else None
        ),
    ),
    EssSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        name="Battery state of charge",
        icon="mdi:battery",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda c: c.battery.soc,
        attributes=lambda c: c.battery.as_dict(),
    ),
    EssSensorDescription(
        key="usable_capacity",
        translation_key="usable_capacity",
        name="Usable battery capacity",
        icon="mdi:battery-heart-variant",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda c: round(c.battery_spec().usable_kwh, 2),
        attributes=lambda c: {
            "nameplate_kwh": c.battery_spec().capacity_kwh,
            "capacity_source": c.battery.capacity_source,
            "min_soc": c.settings.min_soc,
            "max_soc": c.settings.max_soc,
            "round_trip_efficiency": round(c.battery_spec().round_trip_efficiency, 3),
            "spread_needed_to_cycle": round(c.battery_spec().spread_needed_to_cycle(), 2),
        },
    ),
    EssSensorDescription(
        key="learning_progress",
        translation_key="learning_progress",
        name="Learning progress",
        icon="mdi:school-outline",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=_learning_progress,
        attributes=lambda c: {
            **c.learning_store.model.confidence(),
            # What the immaturity is actually costing in caution, in words. Without
            # this, a plan built against a marked-up load looks like the optimiser
            # being needlessly timid rather than the forecast being young.
            "planning_allowance": c.confidence_note(),
        },
    ),
    EssSensorDescription(
        key="planned_charge_power",
        translation_key="planned_charge_power",
        name="Planned charge power",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda c: (
            round(c.last_command.power_kw, 3)
            if c.last_command
            and c.last_command.action in (SlotAction.CHARGE, SlotAction.CHARGE_SOLAR_ONLY)
            else 0.0
        ),
    ),
    EssSensorDescription(
        key="planned_discharge_power",
        translation_key="planned_discharge_power",
        name="Planned discharge power",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda c: (
            round(c.last_command.power_kw, 3)
            if c.last_command and c.last_command.action is SlotAction.DISCHARGE
            else 0.0
        ),
    ),
)


def _wear_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    """Show the workings, and what the allowance means in practice."""
    from .wear import negative_price_threshold

    estimate = coordinator.wear_estimate()
    spec = coordinator.battery_spec()
    return {
        **estimate.as_dict(),
        "derived": coordinator.settings.derive_wear_from_cost,
        "manual_cycle_cost": coordinator.settings.cycle_cost,
        # What the number actually means, in terms a user can check.
        "spread_needed_to_cycle": round(spec.spread_needed_to_cycle(), 2),
        "negative_price_to_dump_and_reimport": round(
            negative_price_threshold(estimate.cycle_cost, spec.charge_efficiency), 2
        ),
    }


def _session_state(coordinator: EssCoordinator) -> str:
    active = coordinator.active_session()
    if active is not None:
        return active.kind
    upcoming = coordinator.next_session()
    return "upcoming" if upcoming is not None else "none"


def _session_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    active = coordinator.active_session()
    upcoming = coordinator.next_session()
    adjustments = coordinator.adjustment_result
    return {
        "active": active.as_dict() if active else None,
        "next": upcoming.as_dict() if upcoming else None,
        "known": [s.as_dict() for s in coordinator.sessions],
        "applied_to_plan": (
            [a.as_dict() for a in adjustments.applied] if adjustments else []
        ),
        "slots_repriced": adjustments.slots_changed if adjustments else 0,
        "enabled": coordinator.settings.sessions_enabled,
    }


def _shifted_loads_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    from .shifting import next_placement, schedule_advice

    now = dt_util.utcnow()
    upcoming = next_placement(coordinator.placements, now)
    return {
        "placements": [p.as_dict() for p in coordinator.placements],
        "defined": [load.as_dict() for load in coordinator.shiftable_loads()],
        "enabled": coordinator.settings.shifting_enabled,
        "total_saving": round(
            sum(p.saving_vs_worst or 0.0 for p in coordinator.placements), 2
        ),
        # The advice line is what makes this useful without smart appliances:
        # a plain instruction you can put on a dashboard and act on yourself.
        "advice": schedule_advice(coordinator.placements, now, dt_util.as_local),
        "next_load": upcoming.name if upcoming else None,
        "next_start": upcoming.start.isoformat() if upcoming else None,
        **coordinator.appliance_status(now),
    }


def _performance_attributes(coordinator: EssCoordinator) -> dict[str, Any]:
    """The last week's report, on the entity that summarises it.

    Attached to a sensor as well as a service so the numbers are visible on a
    dashboard without anyone having to know the action exists.
    """
    return {
        **coordinator.performance_summary(7.0),
        "records_held": len(coordinator.performance_store.log),
        "export_action": "ess_controller.export_performance",
    }


def _performance_state(coordinator: EssCoordinator) -> float | None:
    """Net saving against a self-use battery over the last week, after wear.

    Net rather than gross: the optimiser cycles the pack harder than self-use
    does, and a headline saving that disappears once the wear allowance is paid
    is not one worth reporting.
    """
    net = coordinator.performance_report(7.0).net_saving_vs_self_use
    return None if net is None else round(net, 1)


def _recommendation_state(coordinator: EssCoordinator) -> str | None:
    """Never ``None``. "Unknown" is what Home Assistant shows for a sensor with
    no state, and it reads as a broken feature rather than as one that has not
    been asked a question yet -- which is what it is, because the comparison is
    only run on demand and does not survive a restart."""
    from . import recommend as recommend_mod

    if coordinator.recommendation is None:
        return "not compared yet"
    return recommend_mod.summarise(coordinator.recommendation)


SESSION_SENSORS: tuple[EssSensorDescription, ...] = (
    EssSensorDescription(
        key="wear_allowance",
        translation_key="wear_allowance",
        name="Wear allowance in use",
        icon="mdi:battery-heart-outline",
        native_unit_of_measurement=PRICE_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda c: round(c.wear_estimate().cycle_cost, 3),
        attributes=lambda c: _wear_attributes(c),
    ),
    EssSensorDescription(
        key="grid_session",
        translation_key="grid_session",
        name="Grid incentive session",
        icon="mdi:hand-coin-outline",
        value=_session_state,
        attributes=_session_attributes,
    ),
    EssSensorDescription(
        key="outage_risk",
        translation_key="outage_risk",
        name="Outage risk",
        icon="mdi:transmission-tower-off",
        value=lambda c: c.outage.level,
        attributes=lambda c: {
            **c.outage.as_dict(),
            "effective_min_soc": round(c.effective_min_soc, 1),
            "enabled": c.settings.outage_protection,
        },
    ),
    EssSensorDescription(
        key="shifted_loads",
        translation_key="shifted_loads",
        name="Scheduled flexible loads",
        icon="mdi:calendar-clock",
        value=lambda c: len(c.placements),
        attributes=_shifted_loads_attributes,
    ),
    EssSensorDescription(
        key="weekly_saving",
        translation_key="weekly_saving",
        name="Saving vs self use this week",
        icon="mdi:chart-line",
        # A total, not a rate. Labelled p/kWh it read as "-476.6 p/kWh", which is
        # not a quantity anybody can act on -- and on a graph of it, the y-axis
        # made a week's money look like a tariff gone mad.
        native_unit_of_measurement=COST_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=_performance_state,
        attributes=_performance_attributes,
    ),
    EssSensorDescription(
        key="tariff_recommendation",
        translation_key="tariff_recommendation",
        name="Tariff recommendation",
        icon="mdi:swap-horizontal",
        value=_recommendation_state,
        attributes=lambda c: (
            c.recommendation.as_dict() if c.recommendation else {"note": "not yet run"}
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EssSensor(coordinator, description)
        for description in (*SENSORS, *SESSION_SENSORS)
    )


class EssSensor(EssEntity, SensorEntity):
    """A sensor computed from the coordinator."""

    entity_description: EssSensorDescription

    def __init__(
        self, coordinator: EssCoordinator, description: EssSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        try:
            return self.entity_description.value(self.coordinator)
        except Exception:
            _LOGGER.exception("Error computing sensor %s", self.entity_description.key)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        try:
            return self.entity_description.attributes(self.coordinator)
        except Exception:
            _LOGGER.exception(
                "Error computing attributes for %s", self.entity_description.key
            )
            return None
