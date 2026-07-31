"""Buttons for manual actions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_category import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EssCoordinator
from .entity import EssEntity


@dataclass(frozen=True, kw_only=True)
class EssButtonDescription(ButtonEntityDescription):
    action: Callable[[EssCoordinator], Awaitable[None]]


BUTTONS: tuple[EssButtonDescription, ...] = (
    EssButtonDescription(
        key="replan",
        translation_key="replan",
        name="Re-plan now",
        icon="mdi:refresh",
        action=lambda c: c.async_request_refresh(),
    ),
    EssButtonDescription(
        key="clear_override",
        translation_key="clear_override",
        name="Clear override",
        icon="mdi:cancel",
        action=lambda c: c.async_clear_override(),
    ),
    EssButtonDescription(
        key="recommend_tariffs",
        translation_key="recommend_tariffs",
        name="Compare tariffs",
        icon="mdi:swap-horizontal",
        action=lambda c: c.async_recommend_tariffs(),
    ),
    EssButtonDescription(
        key="reset_learning",
        translation_key="reset_learning",
        name="Reset learned history",
        icon="mdi:database-remove-outline",
        entity_category=EntityCategory.CONFIG,
        action=lambda c: c.async_reset_learning(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(EssButton(coordinator, description) for description in BUTTONS)


class EssButton(EssEntity, ButtonEntity):
    entity_description: EssButtonDescription

    def __init__(
        self, coordinator: EssCoordinator, description: EssButtonDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.action(self.coordinator)
