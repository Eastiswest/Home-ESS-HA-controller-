"""Entity roles and auto-discovery.

Rather than hard-code entity IDs (which embed the user's chosen integration name
and change between firmware revisions), an adapter declares the *roles* it needs
-- "the thing that selects the working mode", "the thing that limits charge" --
and discovery finds a matching entity by object-ID suffix.

Every discovered role can be overridden explicitly from the config flow, so a
non-standard setup is always recoverable without code changes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Control roles.
ROLE_USE_MODE = "use_mode"
ROLE_MANUAL_MODE = "manual_mode"
ROLE_CHARGE_LIMIT = "charge_limit"
ROLE_DISCHARGE_LIMIT = "discharge_limit"
ROLE_MIN_SOC = "min_soc"
ROLE_TARGET_SOC = "target_soc"
ROLE_EXPORT_LIMIT = "export_limit"
ROLE_LOCK = "lock"
ROLE_GRID_CHARGE = "grid_charge"

# Measurement roles.
ROLE_SOC = "soc"
ROLE_BATTERY_VOLTAGE = "battery_voltage"
ROLE_BATTERY_POWER = "battery_power"
ROLE_PV_POWER = "pv_power"
ROLE_GRID_POWER = "grid_power"
ROLE_LOAD_POWER = "load_power"

ALL_ROLES: tuple[str, ...] = (
    ROLE_USE_MODE,
    ROLE_MANUAL_MODE,
    ROLE_CHARGE_LIMIT,
    ROLE_DISCHARGE_LIMIT,
    ROLE_MIN_SOC,
    ROLE_TARGET_SOC,
    ROLE_EXPORT_LIMIT,
    ROLE_LOCK,
    ROLE_GRID_CHARGE,
    ROLE_SOC,
    ROLE_BATTERY_VOLTAGE,
    ROLE_BATTERY_POWER,
    ROLE_PV_POWER,
    ROLE_GRID_POWER,
    ROLE_LOAD_POWER,
)


@dataclass(slots=True)
class RoleSpec:
    """How to find the entity fulfilling one role."""

    role: str
    domains: tuple[str, ...]
    suffixes: tuple[str, ...]
    required: bool = False
    description: str = ""
    exclude: tuple[str, ...] = ()
    """Object-ID fragments that disqualify a match, however well the suffix fits.

    Suffix matching is loose on purpose, because inverter integrations rename
    things between firmware versions. Occasionally it is *too* loose: a real
    install bound "charge from grid" to
    ``switch.solax1_inverter_peakshaving_charge_from_grid``, which is the
    peak-shaving feature and has nothing to do with charging from the grid during
    self-use. Toggling it did nothing, silently, and the plan believed grid
    charging was permitted when it was not.
    """


# Roles for wills106's SolaX Modbus integration, which is what a Solax X1/X3
# hybrid exposes. Suffixes are listed most-specific first, because several are
# prefixes of each other ("battery_voltage_charge" vs "battery_voltage").
SOLAX_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        ROLE_USE_MODE,
        ("select",),
        ("charger_use_mode", "use_mode"),
        required=True,
        description="Self Use / Feed-in Priority / Back Up / Manual",
    ),
    RoleSpec(
        ROLE_MANUAL_MODE,
        ("select",),
        ("manual_mode_select", "manual_mode"),
        required=True,
        description="Force Charge / Force Discharge / Stop",
    ),
    RoleSpec(
        ROLE_CHARGE_LIMIT,
        ("number",),
        ("battery_charge_max_current", "charge_max_current"),
        description="Battery charge current limit (amps)",
    ),
    RoleSpec(
        ROLE_DISCHARGE_LIMIT,
        ("number",),
        ("battery_discharge_max_current", "discharge_max_current"),
        description="Battery discharge current limit (amps)",
    ),
    RoleSpec(
        ROLE_MIN_SOC,
        ("number",),
        ("battery_minimum_capacity", "selfuse_backup_soc", "battery_min_capacity"),
        description="Reserve SoC the inverter will not discharge below",
    ),
    RoleSpec(
        ROLE_TARGET_SOC,
        ("number",),
        ("forcetime_period_1_max_capacity", "charge_target_soc"),
        description="Target SoC for a forced charge",
    ),
    RoleSpec(
        ROLE_EXPORT_LIMIT,
        ("number",),
        ("export_control_user_limit", "export_limit"),
        description="Grid export power limit",
    ),
    RoleSpec(
        ROLE_LOCK,
        ("select",),
        ("lock_state",),
        description="Must be Unlocked before settings can be written",
    ),
    RoleSpec(
        ROLE_GRID_CHARGE,
        ("switch", "select"),
        ("selfuse_night_charge_enable", "charge_from_grid", "grid_charge"),
        description="Whether charging from the grid is permitted",
        # Peak shaving has its own charge-from-grid switch, for a different
        # feature entirely. Better to find nothing and say so than to write to it.
        exclude=("peakshaving", "peak_shaving"),
    ),
    RoleSpec(
        ROLE_SOC,
        ("sensor",),
        ("battery_capacity", "battery_soc", "battery_state_of_charge"),
        description="Battery state of charge (%)",
    ),
    RoleSpec(
        ROLE_BATTERY_VOLTAGE,
        ("sensor",),
        ("battery_voltage_charge", "battery_voltage"),
        description="Battery terminal voltage, used to convert power to current",
    ),
    RoleSpec(
        ROLE_BATTERY_POWER,
        ("sensor",),
        ("battery_power_charge", "battery_power"),
        description="Battery power (positive = charging)",
    ),
    RoleSpec(
        ROLE_PV_POWER,
        ("sensor",),
        ("pv_power_total", "pv_power_1", "pv_power"),
        description="Total PV generation power",
    ),
    RoleSpec(
        ROLE_GRID_POWER,
        ("sensor",),
        ("measured_power", "grid_power", "feedin_power"),
        description="Grid power (positive = importing)",
    ),
    RoleSpec(
        ROLE_LOAD_POWER,
        ("sensor",),
        ("house_load", "load_power", "inverter_load"),
        description="House load power",
    ),
)


def _iter_states(hass: Any) -> Iterable[Any]:
    """All current states, tolerating either StateMachine accessor."""
    states = hass.states
    getter = getattr(states, "async_all", None)
    if callable(getter):
        return list(getter())
    getter = getattr(states, "all", None)
    if callable(getter):
        return list(getter())
    return []


# Our own entities, which must never be discovered as the inverter's.
#
# Object IDs are built from the device name, so an install called "AI ESS
# Controller" publishes sensor.ai_ess_controller_usable_battery_capacity -- and
# that matched the state-of-charge role's suffix. Binding it would have the
# controller read its own 18 kWh capacity figure as an 18% state of charge, and
# report the inverter as connected while it was not. Found by a test, not by a
# user, but only just.
OWN_MARKERS: tuple[str, ...] = ("ess_controller", "ai_ess_controller")


def _is_our_own(object_id: str) -> bool:
    return any(marker in object_id for marker in OWN_MARKERS)


def discover_entities(
    hass: Any,
    specs: Iterable[RoleSpec],
    prefix: str | None = None,
) -> dict[str, str]:
    """Map roles to entity IDs by matching object-ID suffixes.

    When ``prefix`` is given, entities whose object ID starts with it win. That
    disambiguates a house with two inverters, where both expose an identically
    suffixed ``charger_use_mode``.

    This integration's own entities are excluded: several of them are named like
    the inverter's by design, and binding one would make the controller read its
    own output as the inverter's input.
    """
    available = _iter_states(hass)
    wanted_prefix = _clean(prefix) if prefix else None

    by_domain: dict[str, list[tuple[str, str]]] = {}
    for state in available:
        entity_id = getattr(state, "entity_id", None)
        if not entity_id or "." not in entity_id:
            continue
        domain, _, object_id = entity_id.partition(".")
        if _is_our_own(object_id):
            continue
        by_domain.setdefault(domain, []).append((entity_id, object_id))

    found: dict[str, str] = {}
    for spec in specs:
        match = _match_role(spec, by_domain, wanted_prefix)
        if match:
            found[spec.role] = match
        elif spec.required:
            _LOGGER.debug("No entity found for required role %s", spec.role)
    return found


def _match_role(
    spec: RoleSpec,
    by_domain: dict[str, list[tuple[str, str]]],
    wanted_prefix: str | None,
) -> str | None:
    # Suffixes are ordered most-specific first, and a prefixed match always
    # beats an unprefixed one at the same specificity.
    for suffix in spec.suffixes:
        fallback: str | None = None
        for domain in spec.domains:
            for entity_id, object_id in by_domain.get(domain, []):
                if not object_id.endswith(suffix):
                    continue
                if any(bad in object_id for bad in spec.exclude):
                    continue
                if wanted_prefix and object_id.startswith(wanted_prefix):
                    return entity_id
                if fallback is None:
                    fallback = entity_id
        if fallback and not wanted_prefix:
            return fallback
        if fallback:
            # A prefix was requested but nothing matched it; still better than
            # nothing, since a single-inverter install often has no prefix.
            return fallback
    return None


# Words that mark an entity as plausibly one of the inverter's controls, used only
# to narrow the "here is what I could see" list in diagnostics.
CANDIDATE_WORDS: tuple[str, ...] = (
    "soc",
    "capacity",
    "charge",
    "discharge",
    "grid",
    "selfuse",
    "self_use",
    "backup",
    "period",
    "mode",
    "export",
    "limit",
    "battery",
)


def unmatched_candidates(
    hass: Any,
    bound: dict[str, str],
    prefix: str | None = None,
    limit: int = 60,
) -> list[str]:
    """Inverter entities that look relevant but are not bound to any role.

    Guessing entity IDs is how "charge from grid" ended up bound to a peak-shaving
    switch, so this exists to stop the guessing: when a control is plainly there on
    the inverter's own screen and no role found it, the answer is in this list.
    Diagnostics only -- nothing reads it to make a decision.
    """
    taken = set(bound.values())
    wanted_prefix = _clean(prefix) if prefix else None
    found: list[str] = []
    for state in _iter_states(hass):
        entity_id = getattr(state, "entity_id", None)
        if not entity_id or "." not in entity_id:
            continue
        domain, _, object_id = entity_id.partition(".")
        if domain not in ("switch", "select", "number"):
            continue
        if entity_id in taken or _is_our_own(object_id):
            continue
        if wanted_prefix and not object_id.startswith(wanted_prefix):
            continue
        if not any(word in object_id for word in CANDIDATE_WORDS):
            continue
        found.append(entity_id)
    return sorted(found)[:limit]


def _clean(text: str) -> str:
    return str(text).strip().lower().replace(" ", "_").replace("-", "_")


def merge_overrides(
    discovered: dict[str, str], overrides: dict[str, Any] | None
) -> dict[str, str]:
    """Apply user-supplied role overrides on top of discovery.

    An explicit empty string disables a role, which is how a user tells the
    controller "do not touch my export limit".
    """
    result = dict(discovered)
    for role, entity_id in (overrides or {}).items():
        if role not in ALL_ROLES:
            continue
        if entity_id is None:
            continue
        text = str(entity_id).strip()
        if not text:
            result.pop(role, None)
        else:
            result[role] = text
    return result
