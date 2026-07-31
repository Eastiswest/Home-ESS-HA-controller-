"""Switches for arming control and granting permissions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_category import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EssCoordinator
from .entity import EssEntity
from .runtime import RuntimeSettings


@dataclass(frozen=True, kw_only=True)
class EssSwitchDescription(SwitchEntityDescription):
    value: Callable[[RuntimeSettings], bool]
    setter: Callable[[bool], dict[str, Any]]
    attributes: Callable[[EssCoordinator], dict[str, Any]] | None = None


SWITCHES: tuple[EssSwitchDescription, ...] = (
    EssSwitchDescription(
        key="optimiser_enabled",
        translation_key="optimiser_enabled",
        name="Optimiser enabled",
        icon="mdi:brain",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.enabled,
        setter=lambda on: {"enabled": on},
        attributes=lambda c: {
            "description": (
                "Turn off to stop planning entirely and hand the inverter back "
                "to its own self-use logic."
            )
        },
    ),
    EssSwitchDescription(
        key="inverter_control",
        translation_key="inverter_control",
        name="Inverter control",
        icon="mdi:robot-industrial-outline",
        entity_category=EntityCategory.CONFIG,
        # Deliberately inverted: the stored setting is dry_run, but a switch
        # called "inverter control" that you turn ON to arm it is far clearer
        # than one called "dry run" that you turn OFF.
        value=lambda s: not s.dry_run,
        setter=lambda on: {"dry_run": not on},
        attributes=lambda c: {
            "description": (
                "Off means advisory only: the plan is published but the inverter "
                "is never written to. Turn on once you trust the plan."
            ),
            "last_result": c.last_apply.summary() if c.last_apply else None,
        },
    ),
    EssSwitchDescription(
        key="allow_grid_charge",
        translation_key="allow_grid_charge",
        name="Allow grid charging",
        icon="mdi:transmission-tower-import",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.allow_grid_charge,
        setter=lambda on: {"allow_grid_charge": on},
    ),
    EssSwitchDescription(
        key="allow_export",
        translation_key="allow_export",
        name="Allow export",
        icon="mdi:transmission-tower-export",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.allow_export,
        setter=lambda on: {"allow_export": on},
        attributes=lambda c: {
            "description": (
                "Off means exported energy is treated as worthless, so surplus is "
                "stored or spilled rather than given away."
            )
        },
    ),
    EssSwitchDescription(
        key="allow_battery_export",
        translation_key="allow_battery_export",
        name="Allow battery export",
        icon="mdi:battery-arrow-up",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.allow_battery_export,
        setter=lambda on: {"allow_battery_export": on},
        attributes=lambda c: {
            "description": (
                "Off keeps the battery for the house only: it will cover load but "
                "never discharge into the grid for arbitrage."
            )
        },
    ),
)


FEATURE_SWITCHES: tuple[EssSwitchDescription, ...] = (
    EssSwitchDescription(
        key="derive_wear_from_cost",
        translation_key="derive_wear_from_cost",
        name="Derive wear from battery cost",
        icon="mdi:calculator-variant-outline",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.derive_wear_from_cost,
        setter=lambda on: {"derive_wear_from_cost": on},
        attributes=lambda c: {
            "description": (
                "On: the wear allowance is calculated from the pack cost, usable "
                "capacity and expected cycle life. Off: the wear allowance number "
                "is used as entered."
            ),
            **c.wear_estimate().as_dict(),
        },
    ),
    EssSwitchDescription(
        key="sessions_enabled",
        translation_key="sessions_enabled",
        name="Act on grid sessions",
        icon="mdi:hand-coin-outline",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.sessions_enabled,
        setter=lambda on: {"sessions_enabled": on},
        attributes=lambda c: {
            "description": (
                "Reprice Saving Session and free-electricity windows so the plan "
                "avoids importing when reduction is paid, and fills the battery "
                "when import is free."
            ),
            "known_sessions": len(c.sessions),
        },
    ),
    EssSwitchDescription(
        key="shifting_enabled",
        translation_key="shifting_enabled",
        name="Shift flexible loads",
        icon="mdi:calendar-clock",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.shifting_enabled,
        setter=lambda on: {"shifting_enabled": on},
        attributes=lambda c: {
            "description": (
                "Schedule the flexible loads you have defined into the cheapest "
                "windows, and re-plan the battery around them."
            ),
            "defined_loads": len(c.shiftable_loads()),
        },
    ),
    EssSwitchDescription(
        key="appliance_control",
        translation_key="appliance_control",
        name="Switch appliances",
        icon="mdi:washing-machine",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.appliance_control,
        setter=lambda on: {"appliance_control": on},
        attributes=lambda c: {
            "description": (
                "Switch a scheduled load on and off at its window, for loads you "
                "gave a switch entity. Loads without one stay advisory: the "
                "schedule is published and you start them yourself."
            ),
            "switchable_loads": sum(
                1 for load in c.shiftable_loads() if load.controllable
            ),
            "advisory_loads": sum(
                1 for load in c.shiftable_loads() if not load.controllable
            ),
        },
    ),
    EssSwitchDescription(
        key="outage_protection",
        translation_key="outage_protection",
        name="Outage protection",
        icon="mdi:transmission-tower-off",
        entity_category=EntityCategory.CONFIG,
        value=lambda s: s.outage_protection,
        setter=lambda on: {"outage_protection": on},
        attributes=lambda c: {
            "description": (
                "Hold extra charge back when a power cut looks likely. Can only "
                "raise the planning floor, never lower it."
            ),
            "risk": c.outage.level,
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EssSwitch(coordinator, description)
        for description in (*SWITCHES, *FEATURE_SWITCHES)
    )


class EssSwitch(EssEntity, SwitchEntity):
    entity_description: EssSwitchDescription

    def __init__(
        self, coordinator: EssCoordinator, description: EssSwitchDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return self.entity_description.value(self.coordinator.settings)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_update_settings(
            **self.entity_description.setter(True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_update_settings(
            **self.entity_description.setter(False)
        )
