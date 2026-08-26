"""Live-tunable settings: the values a user may change while the system runs.

This module is free of Home Assistant imports so the clamping and seeding rules
can be unit tested. Persistence lives in :mod:`.runtime`.

There is a deliberate split of responsibility:

* **Config entry options** hold things that need a reload to take effect --
  which entities to read, which tariff to use, nameplate capacity, horizon
  length. Changing those re-creates the coordinator.
* **These runtime settings** hold everything a user might reasonably want to
  change from a dashboard while the system runs -- SoC limits, power limits,
  wear allowance, the permission switches, and the master arm. They are backed
  by ``number``, ``switch`` and ``select`` entities and take effect on the next
  planning cycle with no reload.

Keeping them in one authoritative object avoids the classic failure of storing
the same value in both the config entry and an entity and then disagreeing
about which one is real.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from .const import (
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_EXPORT,
    CONF_ALLOW_GRID_CHARGE,
    CONF_APPLIANCE_CONTROL,
    CONF_BATTERY_COST,
    CONF_BATTERY_EXPECTED_CYCLES,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_RESERVE_SOC,
    CONF_BATTERY_RESIDUAL_VALUE,
    CONF_CYCLE_COST,
    CONF_DEFAULT_DAILY_LOAD,
    CONF_DERIVE_WEAR_FROM_COST,
    CONF_DRY_RUN,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_OUTAGE_ENABLED,
    CONF_SESSIONS_ENABLED,
    CONF_SHIFTING_ENABLED,
    DEFAULT_BATTERY_COST,
    DEFAULT_BATTERY_EXPECTED_CYCLES,
    DEFAULT_BATTERY_RESIDUAL_VALUE,
    DEFAULT_CYCLE_COST,
    DEFAULT_DAILY_LOAD,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_RESERVE_SOC,
    STRATEGIES,
    STRATEGY_AUTO,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeSettings:
    """Everything adjustable without reloading the integration."""

    # -- master control -------------------------------------------------
    enabled: bool = True
    """Whether the controller plans at all. Off means it stops writing and
    leaves the inverter in self-use."""

    dry_run: bool = True
    """When true, plans and publishes but never writes to the inverter.

    Defaults to true on purpose: a new install should be watched for a few days
    before being trusted with real money and a real battery.
    """

    # -- feature toggles -------------------------------------------------
    sessions_enabled: bool = True
    """Act on supplier incentive windows (Saving Sessions, free electricity)."""
    shifting_enabled: bool = False
    """Schedule flexible loads. Off by default: it only does something once
    loads have actually been defined."""
    appliance_control: bool = False
    """Switch scheduled appliances on and off, for the ones that can be.

    Separate from :attr:`dry_run` and off by default, because scheduling a load
    and actually energising it are different levels of trust -- and because most
    appliances have no switch to drive, in which case this does nothing at all.
    """
    outage_protection: bool = False
    """Hold extra charge back when an outage looks likely."""

    # -- permissions ----------------------------------------------------
    allow_grid_charge: bool = True
    allow_export: bool = True
    allow_battery_export: bool = True

    # -- strategy -------------------------------------------------------
    strategy: str = STRATEGY_AUTO

    # -- battery limits -------------------------------------------------
    min_soc: float = DEFAULT_MIN_SOC
    max_soc: float = DEFAULT_MAX_SOC
    reserve_soc: float = DEFAULT_RESERVE_SOC
    max_charge_kw: float = DEFAULT_MAX_CHARGE_POWER
    max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_POWER
    cycle_cost: float = DEFAULT_CYCLE_COST
    """Manually entered wear allowance, used unless derivation is enabled."""

    # -- wear allowance derivation ---------------------------------------
    derive_wear_from_cost: bool = False
    battery_cost: float = DEFAULT_BATTERY_COST
    """What the pack cost, in major currency units."""
    battery_expected_cycles: float = DEFAULT_BATTERY_EXPECTED_CYCLES
    battery_residual_value: float = DEFAULT_BATTERY_RESIDUAL_VALUE

    # -- forecasting ----------------------------------------------------
    default_daily_load: float = DEFAULT_DAILY_LOAD
    cooling_rate: float = 0.0
    """Extra kWh per hour per degree above the cooling threshold. Zero relies
    entirely on learned history."""
    cooling_threshold: float = 22.0
    heating_rate: float = 0.0
    heating_threshold: float = 12.0

    # -- bookkeeping ----------------------------------------------------
    seeded: bool = False
    dashboard_created: bool = False
    """Set once the sidebar dashboard has been offered. Remembered so that
    deleting the dashboard is respected rather than undone on every restart."""
    overrides: dict[str, Any] = field(default_factory=dict)

    def sanitised(self) -> RuntimeSettings:
        """Clamp values into ranges the optimiser can actually work with."""
        self.min_soc = _clamp(self.min_soc, 0.0, 99.0)
        self.max_soc = _clamp(self.max_soc, 1.0, 100.0)
        if self.max_soc <= self.min_soc:
            # An inverted window would make the battery unusable; nudge rather
            # than fail, and say so.
            _LOGGER.warning(
                "max_soc (%.0f) must exceed min_soc (%.0f); widening the window",
                self.max_soc,
                self.min_soc,
            )
            self.max_soc = min(100.0, self.min_soc + 1.0)
        # The hardware reserve sits below the planning floor: the optimiser must
        # never plan into the emergency reserve.
        self.reserve_soc = _clamp(self.reserve_soc, 0.0, self.min_soc)
        self.max_charge_kw = _clamp(self.max_charge_kw, 0.0, 100.0)
        self.max_discharge_kw = _clamp(self.max_discharge_kw, 0.0, 100.0)
        self.cycle_cost = _clamp(self.cycle_cost, 0.0, 100.0)
        self.battery_cost = _clamp(self.battery_cost, 0.0, 1_000_000.0)
        self.battery_residual_value = _clamp(
            self.battery_residual_value, 0.0, max(self.battery_cost, 0.0)
        )
        self.battery_expected_cycles = _clamp(self.battery_expected_cycles, 0.0, 20_000.0)
        self.default_daily_load = _clamp(self.default_daily_load, 0.0, 500.0)
        self.cooling_rate = _clamp(self.cooling_rate, 0.0, 10.0)
        self.heating_rate = _clamp(self.heating_rate, 0.0, 10.0)
        if self.strategy not in STRATEGIES:
            self.strategy = STRATEGY_AUTO
        return self

    def wear_estimate(self, usable_kwh: float):
        """Resolve the wear allowance actually in force.

        Derivation needs the usable window, which lives with the battery spec, so
        the caller supplies it rather than this object reaching for it.
        """
        from .wear import manual_wear, wear_from_cost

        if self.derive_wear_from_cost and self.battery_cost > 0:
            return wear_from_cost(
                pack_cost=self.battery_cost,
                usable_kwh=usable_kwh,
                cycles=self.battery_expected_cycles,
                residual_value=self.battery_residual_value,
            )
        return manual_wear(self.cycle_cost)

    def effective_cycle_cost(self, usable_kwh: float) -> float:
        return self.wear_estimate(usable_kwh).cycle_cost

    @property
    def may_write(self) -> bool:
        """Whether writing to the inverter is permitted at all.

        Distinct from :attr:`controlling`: a disabled optimiser must still be
        able to write once, to hand the inverter back to its own logic. Only
        dry-run mode forbids writing outright.
        """
        return not self.dry_run

    @property
    def controlling(self) -> bool:
        """Whether the *plan* may be applied to the inverter right now."""
        return self.enabled and not self.dry_run

    @property
    def may_switch_appliances(self) -> bool:
        """Whether scheduled appliances may actually be switched.

        Requires the optimiser to be running, advisory mode to be off, shifting
        to be on, and appliance control to be armed separately -- four gates,
        because this one turns on real heating elements.
        """
        return self.controlling and self.shifting_enabled and self.appliance_control

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RuntimeSettings:
        data = dict(data or {})
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered).sanitised()

    def seed_from_options(self, options: dict[str, Any]) -> None:
        """Take initial values from the config flow, once.

        Only applied on first load. After that these values belong to their
        entities, and only an options key that actually *changes* is applied --
        see :meth:`apply_option_changes`.
        """
        if self.seeded:
            return
        for conf, field_name, cast in OPTION_FIELDS:
            setattr(self, field_name, cast(options.get(conf, getattr(self, field_name))))
        self.seeded = True
        self.sanitised()

    def apply_option_changes(
        self, seen: dict[str, Any], options: dict[str, Any]
    ) -> list[str]:
        """Apply the options keys that changed since ``seen``. Returns the fields.

        These values belong to their dashboard entities once seeded, but the
        options flow shows them too, accepts edits, and used to discard every
        one -- a wear allowance retyped through Configure sat in the entry
        doing nothing while the plan priced wear off the old number. Diffing
        against the snapshot from the previous load keeps both surfaces honest:
        a stale option that merely sits there can never clobber a dashboard
        tune, and a deliberate edit takes effect. Last edit wins, wherever it
        was made.
        """
        changed: list[str] = []
        for conf, field_name, cast in OPTION_FIELDS:
            if conf not in options or options.get(conf) == seen.get(conf):
                continue
            setattr(self, field_name, cast(options[conf]))
            changed.append(field_name)
        if changed:
            self.sanitised()
        return changed

    @staticmethod
    def tracked_options(options: dict[str, Any]) -> dict[str, Any]:
        """The subset of options that seed runtime settings, for snapshotting."""
        return {conf: options[conf] for conf, _, _ in OPTION_FIELDS if conf in options}


#: (options key, settings field, cast) for every value the config flow seeds.
OPTION_FIELDS: tuple[tuple[str, str, Any], ...] = (
    (CONF_BATTERY_MIN_SOC, "min_soc", float),
    (CONF_BATTERY_MAX_SOC, "max_soc", float),
    (CONF_BATTERY_RESERVE_SOC, "reserve_soc", float),
    (CONF_MAX_CHARGE_POWER, "max_charge_kw", float),
    (CONF_MAX_DISCHARGE_POWER, "max_discharge_kw", float),
    (CONF_CYCLE_COST, "cycle_cost", float),
    (CONF_DERIVE_WEAR_FROM_COST, "derive_wear_from_cost", bool),
    (CONF_BATTERY_COST, "battery_cost", float),
    (CONF_BATTERY_EXPECTED_CYCLES, "battery_expected_cycles", float),
    (CONF_BATTERY_RESIDUAL_VALUE, "battery_residual_value", float),
    (CONF_DEFAULT_DAILY_LOAD, "default_daily_load", float),
    (CONF_ALLOW_GRID_CHARGE, "allow_grid_charge", bool),
    (CONF_ALLOW_EXPORT, "allow_export", bool),
    (CONF_ALLOW_BATTERY_EXPORT, "allow_battery_export", bool),
    (CONF_DRY_RUN, "dry_run", bool),
    (CONF_SESSIONS_ENABLED, "sessions_enabled", bool),
    (CONF_APPLIANCE_CONTROL, "appliance_control", bool),
    (CONF_SHIFTING_ENABLED, "shifting_enabled", bool),
    (CONF_OUTAGE_ENABLED, "outage_protection", bool),
)


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return min(max(number, low), high)
