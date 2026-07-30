"""Battery state source, independent of what kind of battery it is.

This is the abstraction that makes the controller work for a DIY pack behind a
Battery Emulator, an OEM stack, or anything else: the planner only ever needs
state of charge, usable capacity and (optionally) health, and it does not care
which integration reports them.

Resolution order for SoC:

1. An explicitly configured SoC entity -- for a Battery Emulator setup this is
   the BMS's own figure, which is more trustworthy than the inverter's estimate
   because it comes from the real pack.
2. The inverter's reported SoC, for an OEM battery that has no separate BMS
   integration.

Capacity works the same way, which matters for a used EV pack: a Renault Zoe
22 kWh pack that has aged to 17 kWh should be planned against 17, and if the BMS
reports that figure the planner should follow it rather than the nameplate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .base import InverterState, power_kw, state_float

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BatteryReading:
    """A resolved view of the battery, with provenance for each figure."""

    soc: float | None = None
    soc_source: str = "none"
    capacity_kwh: float | None = None
    capacity_source: str = "nameplate"
    soh: float | None = None
    temperature: float | None = None
    voltage: float | None = None
    power_kw: float | None = None

    @property
    def valid(self) -> bool:
        return self.soc is not None and 0.0 <= self.soc <= 100.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "soc": self.soc,
            "soc_source": self.soc_source,
            "capacity_kwh": self.capacity_kwh,
            "capacity_source": self.capacity_source,
            "soh": self.soh,
            "temperature": self.temperature,
            "voltage": self.voltage,
            "power_kw": self.power_kw,
        }


class BatterySource:
    """Reads battery state from configured entities, or falls back to the inverter."""

    def __init__(
        self,
        hass: Any,
        nameplate_capacity_kwh: float,
        soc_entity: str | None = None,
        capacity_entity: str | None = None,
        health_entity: str | None = None,
        temperature_entity: str | None = None,
        voltage_entity: str | None = None,
        power_entity: str | None = None,
        derate_capacity_by_soh: bool = False,
    ) -> None:
        self._hass = hass
        self._nameplate = max(nameplate_capacity_kwh, 0.0)
        self._soc_entity = soc_entity or None
        self._capacity_entity = capacity_entity or None
        self._health_entity = health_entity or None
        self._temperature_entity = temperature_entity or None
        self._voltage_entity = voltage_entity or None
        self._power_entity = power_entity or None
        self._derate_by_soh = derate_capacity_by_soh

    def read(self, inverter_state: InverterState | None = None) -> BatteryReading:
        reading = BatteryReading()

        soc = state_float(self._hass, self._soc_entity)
        if soc is not None:
            reading.soc = soc
            reading.soc_source = self._soc_entity or "entity"
        elif inverter_state is not None and inverter_state.soc is not None:
            reading.soc = inverter_state.soc
            reading.soc_source = "inverter"

        if reading.soc is not None and not 0.0 <= reading.soc <= 100.0:
            _LOGGER.warning(
                "Ignoring implausible battery SoC of %.1f%% from %s",
                reading.soc,
                reading.soc_source,
            )
            reading.soc = None
            reading.soc_source = "invalid"

        reading.soh = state_float(self._hass, self._health_entity)
        reading.temperature = state_float(self._hass, self._temperature_entity)
        reading.voltage = state_float(self._hass, self._voltage_entity)
        if reading.voltage is None and inverter_state is not None:
            reading.voltage = inverter_state.battery_voltage

        reading.power_kw = power_kw(self._hass, self._power_entity)
        if reading.power_kw is None and inverter_state is not None:
            reading.power_kw = inverter_state.battery_power_kw

        reading.capacity_kwh, reading.capacity_source = self._resolve_capacity(
            reading.soh
        )
        return reading

    def _resolve_capacity(self, soh: float | None) -> tuple[float, str]:
        """Prefer a measured capacity, then a health-derated nameplate."""
        measured = state_float(self._hass, self._capacity_entity)
        if measured is not None and measured > 0.0:
            # Some BMS integrations report capacity in Wh rather than kWh.
            if measured > 1000.0:
                measured = measured / 1000.0
            return measured, self._capacity_entity or "entity"

        if self._derate_by_soh and soh is not None and 0.0 < soh <= 100.0:
            return self._nameplate * soh / 100.0, "nameplate x SoH"

        return self._nameplate, "nameplate"

    def describe(self) -> dict[str, Any]:
        return {
            "nameplate_capacity_kwh": self._nameplate,
            "soc_entity": self._soc_entity,
            "capacity_entity": self._capacity_entity,
            "health_entity": self._health_entity,
            "derate_capacity_by_soh": self._derate_by_soh,
        }
