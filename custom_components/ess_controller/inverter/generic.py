"""Generic adapter for any inverter with a mode-select control model.

Most hybrid inverters with a Home Assistant integration follow the same shape as
SolaX: a working-mode select, an optional forced-charge/discharge sub-mode, and
number entities limiting charge and discharge rate. Rather than write a new
adapter per brand, this reuses the SolaX control logic and lets the user retarget
the entity roles and the option wording from the config flow.

That is what delivers the "works with any inverter" goal: supporting a new brand
is configuration, not code.
"""

from __future__ import annotations

import logging
from typing import Any

from .solax import (
    GRID_CHARGE_OFF,
    GRID_CHARGE_ON,
    LOCK_UNLOCKED,
    MANUAL_FORCE_CHARGE,
    MANUAL_FORCE_DISCHARGE,
    MANUAL_STOP,
    MODE_MANUAL,
    MODE_SELF_USE,
    SolaxModbusAdapter,
)

_LOGGER = logging.getLogger(__name__)


def _candidates(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a user-supplied option list, falling back to sensible defaults."""
    if value is None:
        return default
    if isinstance(value, str):
        parts = tuple(p.strip() for p in value.split(",") if p.strip())
        return parts or default
    if isinstance(value, (list, tuple)):
        parts = tuple(str(p).strip() for p in value if str(p).strip())
        return parts or default
    return default


class GenericEntityAdapter(SolaxModbusAdapter):
    """A mode-select inverter described entirely by configuration."""

    name = "generic"

    def __init__(
        self,
        hass: Any,
        entities: dict[str, str],
        nominal_voltage: float = 360.0,
        manage_export_limit: bool = False,
        manage_min_soc: bool = True,
        options: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            hass,
            entities,
            nominal_voltage=nominal_voltage,
            manage_export_limit=manage_export_limit,
            manage_min_soc=manage_min_soc,
        )
        opts = options or {}
        # Instance attributes shadow the class defaults inherited from the SolaX
        # adapter, so only the wording a user actually needs to change is changed.
        self.OPT_SELF_USE = _candidates(opts.get("self_use"), MODE_SELF_USE)
        self.OPT_MANUAL = _candidates(opts.get("manual"), MODE_MANUAL)
        self.OPT_FORCE_CHARGE = _candidates(
            opts.get("force_charge"), MANUAL_FORCE_CHARGE
        )
        self.OPT_FORCE_DISCHARGE = _candidates(
            opts.get("force_discharge"), MANUAL_FORCE_DISCHARGE
        )
        self.OPT_STOP = _candidates(opts.get("stop"), MANUAL_STOP)
        self.OPT_UNLOCKED = _candidates(opts.get("unlocked"), LOCK_UNLOCKED)
        self.OPT_GRID_ON = _candidates(opts.get("grid_charge_on"), GRID_CHARGE_ON)
        self.OPT_GRID_OFF = _candidates(opts.get("grid_charge_off"), GRID_CHARGE_OFF)

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["options"] = {
            "self_use": list(self.OPT_SELF_USE),
            "manual": list(self.OPT_MANUAL),
            "force_charge": list(self.OPT_FORCE_CHARGE),
            "force_discharge": list(self.OPT_FORCE_DISCHARGE),
            "stop": list(self.OPT_STOP),
        }
        return data
