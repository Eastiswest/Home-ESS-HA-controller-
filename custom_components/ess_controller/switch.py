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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(EssSwitch(coordinator, description) for description in SWITCHES)


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
