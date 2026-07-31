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
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_RESERVE_SOC,
    CONF_CYCLE_COST,
    CONF_DEFAULT_DAILY_LOAD,
    CONF_DRY_RUN,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_OUTAGE_ENABLED,
    CONF_SESSIONS_ENABLED,
    CONF_SHIFTING_ENABLED,
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
        self.default_daily_load = _clamp(self.default_daily_load, 0.0, 500.0)
        self.cooling_rate = _clamp(self.cooling_rate, 0.0, 10.0)
        self.heating_rate = _clamp(self.heating_rate, 0.0, 10.0)
        if self.strategy not in STRATEGIES:
            self.strategy = STRATEGY_AUTO
        return self

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
        entities, so a later options edit does not silently undo a tuning change
        the user made from their dashboard.
        """
        if self.seeded:
            return
        self.min_soc = float(options.get(CONF_BATTERY_MIN_SOC, self.min_soc))
        self.max_soc = float(options.get(CONF_BATTERY_MAX_SOC, self.max_soc))
        self.reserve_soc = float(options.get(CONF_BATTERY_RESERVE_SOC, self.reserve_soc))
        self.max_charge_kw = float(options.get(CONF_MAX_CHARGE_POWER, self.max_charge_kw))
        self.max_discharge_kw = float(
            options.get(CONF_MAX_DISCHARGE_POWER, self.max_discharge_kw)
        )
        self.cycle_cost = float(options.get(CONF_CYCLE_COST, self.cycle_cost))
        self.default_daily_load = float(
            options.get(CONF_DEFAULT_DAILY_LOAD, self.default_daily_load)
        )
        self.allow_grid_charge = bool(
            options.get(CONF_ALLOW_GRID_CHARGE, self.allow_grid_charge)
        )
        self.allow_export = bool(options.get(CONF_ALLOW_EXPORT, self.allow_export))
        self.allow_battery_export = bool(
            options.get(CONF_ALLOW_BATTERY_EXPORT, self.allow_battery_export)
        )
        self.dry_run = bool(options.get(CONF_DRY_RUN, self.dry_run))
        self.sessions_enabled = bool(
            options.get(CONF_SESSIONS_ENABLED, self.sessions_enabled)
        )
        self.shifting_enabled = bool(
            options.get(CONF_SHIFTING_ENABLED, self.shifting_enabled)
        )
        self.outage_protection = bool(
            options.get(CONF_OUTAGE_ENABLED, self.outage_protection)
        )
        self.seeded = True
        self.sanitised()


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return min(max(number, low), high)
