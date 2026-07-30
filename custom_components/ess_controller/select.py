"""Strategy selector: let the optimiser decide, or lock it to one behaviour."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_category import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STRATEGIES, STRATEGY_AUTO
from .coordinator import EssCoordinator
from .entity import EssEntity

STRATEGY_DESCRIPTION = SelectEntityDescription(
    key="strategy",
    translation_key="strategy",
    name="Strategy",
    icon="mdi:strategy",
    options=list(STRATEGIES),
    entity_category=EntityCategory.CONFIG,
)

STRATEGY_HELP: dict[str, str] = {
    "auto": "Follow the cost-optimised plan.",
    "self_use": "Plain self-consumption; no arbitrage.",
    "force_charge": "Charge continuously at the configured power.",
    "force_discharge": "Discharge continuously at the configured power.",
    "idle": "Hold the battery; run the house from PV and grid.",
    "off": "Stop controlling and hand the inverter back to its own logic.",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EssCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EssStrategySelect(coordinator)])


class EssStrategySelect(EssEntity, SelectEntity):
    """Locks the controller to one behaviour, or hands it back to the optimiser."""

    entity_description = STRATEGY_DESCRIPTION

    def __init__(self, coordinator: EssCoordinator) -> None:
        super().__init__(coordinator, STRATEGY_DESCRIPTION.key)
        self.entity_description = STRATEGY_DESCRIPTION

    @property
    def current_option(self) -> str:
        return self.coordinator.settings.strategy or STRATEGY_AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "help": STRATEGY_HELP,
            # A strategy lock outranks the plan but is itself outranked by a
            # manual override from the set_override service.
            "override_active": self.coordinator.override is not None,
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_update_settings(strategy=option)
