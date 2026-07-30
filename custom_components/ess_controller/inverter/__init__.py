"""Inverter and battery abstraction.

None of these modules import Home Assistant: they interact with it only through
``hass.states`` and ``hass.services``, which keeps the control logic testable
against a stub and makes adding an inverter a matter of configuration.
"""

from .base import (
    ApplyResult,
    Capabilities,
    InverterAdapter,
    InverterState,
    NullAdapter,
    Write,
    clamp_to_number,
    current_from_power,
    pick_option,
    power_kw,
    power_limit_value,
)
from .battery import BatteryReading, BatterySource
from .generic import GenericEntityAdapter
from .roles import (
    ALL_ROLES,
    SOLAX_ROLE_SPECS,
    RoleSpec,
    discover_entities,
    merge_overrides,
)
from .solax import SolaxModbusAdapter

__all__ = [
    "ALL_ROLES",
    "ApplyResult",
    "BatteryReading",
    "BatterySource",
    "Capabilities",
    "GenericEntityAdapter",
    "InverterAdapter",
    "InverterState",
    "NullAdapter",
    "RoleSpec",
    "SOLAX_ROLE_SPECS",
    "SolaxModbusAdapter",
    "Write",
    "clamp_to_number",
    "current_from_power",
    "discover_entities",
    "merge_overrides",
    "pick_option",
    "power_kw",
    "power_limit_value",
]
