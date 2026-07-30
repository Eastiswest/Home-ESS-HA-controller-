"""Data models shared across the AI ESS Controller.

Everything in here is plain Python with no Home Assistant imports so that the
optimiser and forecasting logic can be unit tested standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class SlotAction(str, Enum):
    """What the controller intends to do during a single planning slot."""

    CHARGE = "charge"
    """Actively charge the battery, drawing from the grid if needed."""

    DISCHARGE = "discharge"
    """Actively discharge the battery, exporting if the plan calls for it."""

    SELF_USE = "self_use"
    """Normal behaviour: PV first, battery covers the shortfall."""

    IDLE = "idle"
    """Hold the battery: neither charge nor discharge. Load runs off grid/PV."""

    CHARGE_SOLAR_ONLY = "charge_solar_only"
    """Charge from surplus PV only; never pull charge energy from the grid."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


@dataclass(slots=True)
class BatterySpec:
    """Physical and economic description of the storage system.

    ``capacity_kwh`` is the *nameplate* capacity. The usable window is derived
    from ``min_soc``/``max_soc`` so that a degraded EV pack (very common with a
    Battery Emulator setup) can be described accurately without lying about
    nameplate size.
    """

    capacity_kwh: float
    min_soc: float
    max_soc: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    cycle_cost_per_kwh: float = 0.0
    reserve_soc: float | None = None

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be positive")
        if not 0.0 <= self.min_soc < self.max_soc <= 100.0:
            raise ValueError("require 0 <= min_soc < max_soc <= 100")
        for name in ("charge_efficiency", "discharge_efficiency"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(f"{name} must be in (0, 1]")

    @property
    def usable_kwh(self) -> float:
        """Energy available between the configured SoC limits."""
        return self.capacity_kwh * (self.max_soc - self.min_soc) / 100.0

    @property
    def round_trip_efficiency(self) -> float:
        return self.charge_efficiency * self.discharge_efficiency

    def soc_to_energy(self, soc: float) -> float:
        """Convert an absolute SoC percentage to usable-window energy in kWh.

        The result is clamped to the usable window, so an SoC below ``min_soc``
        reads as an empty (but not negative) battery.
        """
        clamped = min(max(soc, self.min_soc), self.max_soc)
        return self.capacity_kwh * (clamped - self.min_soc) / 100.0

    def energy_to_soc(self, energy_kwh: float) -> float:
        """Inverse of :meth:`soc_to_energy`."""
        energy = min(max(energy_kwh, 0.0), self.usable_kwh)
        return self.min_soc + (energy / self.capacity_kwh) * 100.0

    def spread_needed_to_cycle(self) -> float:
        """Minimum import/export price spread that makes a cycle worthwhile.

        Charging 1 kWh of *delivered* energy costs ``price / round_trip`` plus
        the wear allowance, so arbitrage only pays when the spread clears this.
        """
        return self.cycle_cost_per_kwh / max(self.round_trip_efficiency, 1e-6)


@dataclass(slots=True)
class GridSpec:
    """Grid connection limits."""

    import_limit_kw: float
    export_limit_kw: float
    allow_export: bool = True
    allow_grid_charge: bool = True
    allow_battery_export: bool = True


@dataclass(slots=True)
class HorizonSlot:
    """One slot of the planning horizon with everything the optimiser needs.

    Prices are in minor currency units per kWh (pence/kWh for GBP) so that the
    numbers users see match what Octopus publishes. Energies are kWh.
    """

    start: datetime
    end: datetime
    import_price: float
    export_price: float = 0.0
    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    pv_is_forecast: bool = True
    load_is_forecast: bool = True
    price_is_forecast: bool = False

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def net_kwh(self) -> float:
        """Site energy balance before any battery action (positive = import)."""
        return self.load_kwh - self.pv_kwh


@dataclass(slots=True)
class PlanSlot:
    """The optimiser's decision for one slot."""

    start: datetime
    end: datetime
    action: SlotAction
    import_price: float
    export_price: float
    pv_kwh: float
    load_kwh: float
    # Battery energy change at the cells, positive = charging.
    battery_delta_kwh: float
    # AC-side flows, always non-negative.
    charge_ac_kwh: float
    discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    curtailed_kwh: float
    soc_start: float
    soc_end: float
    cost: float
    """Net cost for this slot in minor units (negative = earning)."""

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def charge_power_kw(self) -> float:
        hours = self.duration_hours
        return self.charge_ac_kwh / hours if hours else 0.0

    @property
    def discharge_power_kw(self) -> float:
        hours = self.duration_hours
        return self.discharge_ac_kwh / hours if hours else 0.0

    @property
    def target_soc(self) -> float:
        return self.soc_end

    def as_dict(self) -> dict[str, Any]:
        """Serialise for entity attributes and diagnostics."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "action": self.action.value,
            "import_price": round(self.import_price, 3),
            "export_price": round(self.export_price, 3),
            "pv_kwh": round(self.pv_kwh, 3),
            "load_kwh": round(self.load_kwh, 3),
            "battery_delta_kwh": round(self.battery_delta_kwh, 3),
            "charge_power_kw": round(self.charge_power_kw, 3),
            "discharge_power_kw": round(self.discharge_power_kw, 3),
            "grid_import_kwh": round(self.grid_import_kwh, 3),
            "grid_export_kwh": round(self.grid_export_kwh, 3),
            "curtailed_kwh": round(self.curtailed_kwh, 3),
            "soc_start": round(self.soc_start, 1),
            "soc_end": round(self.soc_end, 1),
            "cost": round(self.cost, 2),
        }


@dataclass(slots=True)
class Plan:
    """A complete optimisation result."""

    created: datetime
    slots: list[PlanSlot] = field(default_factory=list)
    total_cost: float = 0.0
    baseline_cost: float = 0.0
    """Cost of the same horizon with the battery held idle (no charge/discharge)."""
    self_use_cost: float = 0.0
    """Cost of the same horizon under plain self-consumption, for comparison."""
    terminal_value: float = 0.0
    reason: str = ""
    infeasible: bool = False

    @property
    def saving_vs_baseline(self) -> float:
        return self.baseline_cost - self.total_cost

    @property
    def saving_vs_self_use(self) -> float:
        return self.self_use_cost - self.total_cost

    def slot_at(self, moment: datetime) -> PlanSlot | None:
        """Return the slot containing ``moment``, if any."""
        for slot in self.slots:
            if slot.start <= moment < slot.end:
                return slot
        return None

    def next_slot_after(self, moment: datetime) -> PlanSlot | None:
        for slot in self.slots:
            if slot.start > moment:
                return slot
        return None

    def next_change_after(self, moment: datetime) -> PlanSlot | None:
        """Return the next slot whose action differs from the current one."""
        current = self.slot_at(moment)
        current_action = current.action if current else None
        for slot in self.slots:
            if slot.start <= moment:
                continue
            if slot.action != current_action:
                return slot
        return None

    def window_energy(self, action: SlotAction) -> float:
        """Total AC energy assigned to a given action across the horizon."""
        if action is SlotAction.CHARGE:
            return sum(s.charge_ac_kwh for s in self.slots)
        if action is SlotAction.DISCHARGE:
            return sum(s.discharge_ac_kwh for s in self.slots)
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created.isoformat(),
            "total_cost": round(self.total_cost, 2),
            "baseline_cost": round(self.baseline_cost, 2),
            "self_use_cost": round(self.self_use_cost, 2),
            "saving_vs_baseline": round(self.saving_vs_baseline, 2),
            "saving_vs_self_use": round(self.saving_vs_self_use, 2),
            "reason": self.reason,
            "infeasible": self.infeasible,
            "slots": [s.as_dict() for s in self.slots],
        }


@dataclass(slots=True)
class SiteState:
    """A snapshot of live measurements, taken at the top of each cycle."""

    soc: float
    """Battery state of charge, percent."""
    timestamp: datetime
    pv_power_kw: float = 0.0
    load_power_kw: float = 0.0
    grid_power_kw: float = 0.0
    """Positive = importing, negative = exporting."""
    battery_power_kw: float = 0.0
    """Positive = charging."""
    battery_capacity_kwh: float | None = None
    """Live capacity reported by the BMS, when available."""
    battery_soh: float | None = None
    battery_temperature: float | None = None
    outdoor_temperature: float | None = None
    soc_valid: bool = True


@dataclass(slots=True)
class Override:
    """A manual instruction that takes precedence over the plan."""

    action: SlotAction
    until: datetime
    power_kw: float | None = None
    target_soc: float | None = None

    def active(self, moment: datetime) -> bool:
        return moment < self.until

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "until": self.until.isoformat(),
            "power_kw": self.power_kw,
            "target_soc": self.target_soc,
        }


@dataclass(slots=True)
class ControlCommand:
    """A resolved, hardware-agnostic instruction for the inverter adapter.

    Adapters translate this into whatever their inverter understands. Keeping
    it abstract is what lets the same planner drive a Solax today and any
    other Modbus inverter later.
    """

    action: SlotAction
    power_kw: float = 0.0
    target_soc: float | None = None
    min_soc: float = 10.0
    max_soc: float = 100.0
    export_limit_kw: float | None = None
    allow_grid_charge: bool = True
    reason: str = ""
    slot_end: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "power_kw": round(self.power_kw, 3),
            "target_soc": self.target_soc,
            "min_soc": self.min_soc,
            "max_soc": self.max_soc,
            "export_limit_kw": self.export_limit_kw,
            "allow_grid_charge": self.allow_grid_charge,
            "reason": self.reason,
            "slot_end": self.slot_end.isoformat() if self.slot_end else None,
        }

    def differs_from(self, other: ControlCommand | None) -> bool:
        """Whether applying this command would change anything.

        Used to avoid hammering the inverter with identical writes every cycle,
        which matters a lot when the transport is a flaky WiFi dongle.
        """
        if other is None:
            return True
        if self.action is not other.action:
            return True
        if abs(self.power_kw - other.power_kw) > 0.05:
            return True
        if (self.target_soc is None) != (other.target_soc is None):
            return True
        if (
            self.target_soc is not None
            and other.target_soc is not None
            and abs(self.target_soc - other.target_soc) > 0.5
        ):
            return True
        if abs(self.min_soc - other.min_soc) > 0.5:
            return True
        if self.allow_grid_charge is not other.allow_grid_charge:
            return True
        return False


@dataclass(slots=True)
class PriceSlot:
    """A tariff price for a half-hour window, as published by the supplier."""

    start: datetime
    end: datetime
    price: float
    is_forecast: bool = False

    @property
    def duration(self) -> timedelta:
        return self.end - self.start
