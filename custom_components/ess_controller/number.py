"""Numbers for tuning battery limits and forecast assumptions live."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_category import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EssCoordinator
from .entity import EssEntity
from .runtime import RuntimeSettings


@dataclass(frozen=True, kw_only=True)
class EssNumberDescription(NumberEntityDescription):
    value: Callable[[RuntimeSettings], float]
    field: str
    attributes: Callable[[EssCoordinator], dict[str, Any]] | None = None


NUMBERS: tuple[EssNumberDescription, ...] = (
    EssNumberDescription(
        key="min_soc",
        translation_key="min_soc",
        name="Minimum state of charge",
        icon="mdi:battery-low",
        native_unit_of_measurement="%",
        native_min_value=0,
        native_max_value=99,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.min_soc,
        field="min_soc",
        attributes=lambda c: {
            "description": (
                "The floor the optimiser plans down to. Raise it to keep more in "
                "reserve; lower it to unlock more of the pack for arbitrage."
            ),
            "usable_kwh": round(c.battery_spec().usable_kwh, 2),
        },
    ),
    EssNumberDescription(
        key="max_soc",
        translation_key="max_soc",
        name="Maximum state of charge",
        icon="mdi:battery-high",
        native_unit_of_measurement="%",
        native_min_value=1,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.max_soc,
        field="max_soc",
        attributes=lambda c: {
            "description": (
                "Keeping a lithium pack below 100% for most of its life reduces "
                "calendar ageing, which matters for a second-life EV battery."
            )
        },
    ),
    EssNumberDescription(
        key="reserve_soc",
        translation_key="reserve_soc",
        name="Emergency reserve",
        icon="mdi:battery-alert-variant-outline",
        native_unit_of_measurement="%",
        native_min_value=0,
        native_max_value=99,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.reserve_soc,
        field="reserve_soc",
        attributes=lambda c: {
            "description": (
                "Written to the inverter as its own minimum SoC, below the "
                "planning floor. This is the hardware backstop if the controller "
                "stops running, so it is deliberately not something the optimiser "
                "can plan into."
            )
        },
    ),
    EssNumberDescription(
        key="max_charge_power",
        translation_key="max_charge_power",
        name="Maximum charge power",
        icon="mdi:battery-charging-high",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        native_min_value=0,
        native_max_value=30,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.max_charge_kw,
        field="max_charge_kw",
    ),
    EssNumberDescription(
        key="max_discharge_power",
        translation_key="max_discharge_power",
        name="Maximum discharge power",
        icon="mdi:battery-arrow-down",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        native_min_value=0,
        native_max_value=30,
        native_step=0.1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.max_discharge_kw,
        field="max_discharge_kw",
    ),
    EssNumberDescription(
        key="cycle_cost",
        translation_key="cycle_cost",
        name="Battery wear allowance",
        icon="mdi:currency-usd",
        native_unit_of_measurement="p/kWh",
        native_min_value=0,
        native_max_value=50,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.cycle_cost,
        field="cycle_cost",
        attributes=lambda c: {
            "description": (
                "Cost charged against each kWh cycled through the battery. This is "
                "the single most useful dial: raise it and the optimiser only takes "
                "big price spreads, lower it and it cycles more aggressively. "
                "Ignored while 'Derive wear from battery cost' is on."
            ),
            "in_use": not c.settings.derive_wear_from_cost,
            "effective_cycle_cost": round(c.wear_estimate().cycle_cost, 3),
            "spread_needed_to_cycle": round(c.battery_spec().spread_needed_to_cycle(), 2),
        },
    ),
    EssNumberDescription(
        key="battery_cost",
        translation_key="battery_cost",
        name="Battery cost",
        icon="mdi:cash-multiple",
        native_min_value=0,
        native_max_value=100000,
        native_step=10,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.battery_cost,
        field="battery_cost",
        attributes=lambda c: {
            "description": (
                "What the pack cost you. With 'Derive wear from battery cost' on, "
                "the wear allowance is worked out from this, the usable capacity "
                "and the expected cycle life."
            ),
            **c.wear_estimate().as_dict(),
        },
    ),
    EssNumberDescription(
        key="battery_expected_cycles",
        translation_key="battery_expected_cycles",
        name="Expected cycle life",
        icon="mdi:battery-sync-outline",
        native_min_value=0,
        native_max_value=20000,
        native_step=50,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.battery_expected_cycles,
        field="battery_expected_cycles",
        attributes=lambda c: {
            "description": (
                "Equivalent full cycles the pack is expected to deliver over the "
                "SoC window you have configured -- not the headline number quoted "
                "at a shallower depth of discharge."
            ),
            "usable_kwh_per_cycle": round(c.usable_kwh(), 2),
            "lifetime_throughput_kwh": round(
                c.wear_estimate().lifetime_throughput_kwh, 0
            ),
        },
    ),
    EssNumberDescription(
        key="battery_residual_value",
        translation_key="battery_residual_value",
        name="Battery residual value",
        icon="mdi:cash-refund",
        native_min_value=0,
        native_max_value=100000,
        native_step=10,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.battery_residual_value,
        field="battery_residual_value",
        attributes=lambda c: {
            "description": (
                "What the pack will still be worth at end of life. Subtracted "
                "from the cost before the wear allowance is worked out. Leave at "
                "zero for a salvage pack."
            )
        },
    ),
    EssNumberDescription(
        key="default_daily_load",
        translation_key="default_daily_load",
        name="Typical daily consumption",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        native_min_value=0,
        native_max_value=200,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.default_daily_load,
        field="default_daily_load",
        attributes=lambda c: {
            "description": (
                "Used only for slots the load model has not learned yet. Its "
                "influence fades as history builds."
            ),
            "load_observations": c.learning_store.model.load_observations,
        },
    ),
    EssNumberDescription(
        key="cooling_rate",
        translation_key="cooling_rate",
        name="Cooling load per degree",
        icon="mdi:snowflake",
        native_unit_of_measurement="kWh/°C/h",
        native_min_value=0,
        native_max_value=5,
        native_step=0.05,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.cooling_rate,
        field="cooling_rate",
        attributes=lambda c: {
            "description": (
                "Extra load assumed per degree above the cooling threshold, so a "
                "forecast hot day plans a bigger overnight charge before any hot "
                "weather has been observed. Leave at 0 to rely purely on learned "
                "history, which supersedes this once it exists."
            ),
            "cooling_threshold_c": c.settings.cooling_threshold,
        },
    ),
    EssNumberDescription(
        key="cooling_threshold",
        translation_key="cooling_threshold",
        name="Cooling threshold",
        icon="mdi:thermometer-high",
        native_unit_of_measurement="°C",
        native_min_value=10,
        native_max_value=40,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.cooling_threshold,
        field="cooling_threshold",
    ),
    EssNumberDescription(
        key="heating_rate",
        translation_key="heating_rate",
        name="Heating load per degree",
        icon="mdi:radiator",
        native_unit_of_measurement="kWh/°C/h",
        native_min_value=0,
        native_max_value=5,
        native_step=0.05,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.heating_rate,
        field="heating_rate",
    ),
    EssNumberDescription(
        key="heating_threshold",
        translation_key="heating_threshold",
        name="Heating threshold",
        icon="mdi:thermometer-low",
        native_unit_of_measurement="°C",
        native_min_value=-10,
        native_max_value=25,
        native_step=0.5,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.heating_threshold,
        field="heating_threshold",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(EssNumber(coordinator, description) for description in NUMBERS)


class EssNumber(EssEntity, NumberEntity):
    entity_description: EssNumberDescription

    def __init__(
        self, coordinator: EssCoordinator, description: EssNumberDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        return self.entity_description.value(self.coordinator.settings)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_update_settings(
            **{self.entity_description.field: value}
        )
