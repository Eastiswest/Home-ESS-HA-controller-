"""Binary sensors for automating against the plan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EssCoordinator
from .entity import EssEntity
from .models import SlotAction


@dataclass(frozen=True, kw_only=True)
class EssBinarySensorDescription(BinarySensorEntityDescription):
    value: Callable[[EssCoordinator], bool | None]
    attributes: Callable[[EssCoordinator], dict[str, Any]] | None = None


def _action_is(coordinator: EssCoordinator, *actions: SlotAction) -> bool:
    command = coordinator.last_command
    return command is not None and command.action in actions


BINARY_SENSORS: tuple[EssBinarySensorDescription, ...] = (
    EssBinarySensorDescription(
        key="charging_planned",
        translation_key="charging_planned",
        name="Charging planned",
        icon="mdi:battery-charging",
        value=lambda c: _action_is(c, SlotAction.CHARGE, SlotAction.CHARGE_SOLAR_ONLY),
        attributes=lambda c: {
            "from_grid": _action_is(c, SlotAction.CHARGE),
            "solar_only": _action_is(c, SlotAction.CHARGE_SOLAR_ONLY),
        },
    ),
    EssBinarySensorDescription(
        key="discharging_planned",
        translation_key="discharging_planned",
        name="Discharging planned",
        icon="mdi:battery-arrow-down",
        value=lambda c: _action_is(c, SlotAction.DISCHARGE),
    ),
    EssBinarySensorDescription(
        key="exporting_planned",
        translation_key="exporting_planned",
        name="Exporting planned",
        icon="mdi:transmission-tower-export",
        value=lambda c: bool(
            c.data
            and c.data.get("current_slot") is not None
            and c.data["current_slot"].grid_export_kwh > 0.01
        ),
    ),
    EssBinarySensorDescription(
        key="cheap_slot",
        translation_key="cheap_slot",
        name="Cheap import slot",
        icon="mdi:tag-check-outline",
        value=lambda c: _cheap_now(c),
        attributes=lambda c: {
            "description": (
                "True when the current import price is in the cheapest third of "
                "the planning horizon. Useful for scheduling flexible loads such "
                "as a dishwasher or EV charge."
            ),
            **c.price_stats("import"),
        },
    ),
    EssBinarySensorDescription(
        key="control_active",
        translation_key="control_active",
        name="Control active",
        icon="mdi:robot-outline",
        value=lambda c: c.settings.controlling,
        attributes=lambda c: {
            "enabled": c.settings.enabled,
            "dry_run": c.settings.dry_run,
        },
    ),
    EssBinarySensorDescription(
        key="inverter_available",
        translation_key="inverter_available",
        name="Inverter available",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value=lambda c: c.inverter_state.available,
        attributes=lambda c: c.inverter_state.as_dict(),
    ),
    EssBinarySensorDescription(
        key="plan_valid",
        translation_key="plan_valid",
        name="Plan valid",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # A problem device class is inverted: on means something is wrong.
        value=lambda c: c.plan is None or c.plan.infeasible,
        attributes=lambda c: {
            "error": c.data.get("error") if c.data else None,
            "reason": c.plan.reason if c.plan else None,
        },
    ),
    EssBinarySensorDescription(
        key="write_verified",
        translation_key="write_verified",
        name="Inverter write problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        icon="mdi:alert-circle-outline",
        # Surfaces the Pocket WiFi failure mode: a write accepted then dropped.
        value=lambda c: bool(
            c.last_apply is not None
            and not c.last_apply.dry_run
            and not c.last_apply.verified
        ),
        attributes=lambda c: {
            "errors": c.last_apply.errors if c.last_apply else [],
            "unverified": c.last_apply.unverified if c.last_apply else [],
        },
    ),
)


def _cheap_now(coordinator: EssCoordinator) -> bool | None:
    stats = coordinator.price_stats("import")
    price = coordinator.price_now("import")
    if not stats or price is None:
        return None
    low = stats["min"]
    high = stats["max"]
    if high <= low:
        return False
    return (price - low) / (high - low) <= 0.34


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EssBinarySensor(coordinator, description) for description in BINARY_SENSORS
    )


class EssBinarySensor(EssEntity, BinarySensorEntity):
    entity_description: EssBinarySensorDescription

    def __init__(
        self, coordinator: EssCoordinator, description: EssBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self.coordinator)
