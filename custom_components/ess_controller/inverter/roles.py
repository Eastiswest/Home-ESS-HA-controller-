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
        # Order matters here, and getting it wrong is expensive.
        #
        # A SolaX exposes several floors and only one of them governs self-use
        # discharge. ``selfuse_backup_soc`` is the capacity held back for EPS
        # backup, and on a real X1 Hybrid G4 it is not the discharge floor at all:
        # the pack was reported as unable to discharge below 90% while it was
        # visibly discharging through 63%, and every attempt to write it came back
        # as Modbus exception 4 at register 0xc5. So the controller could not set
        # the reserve, and told the user it was pinned shut when it was not.
        # ``selfuse_discharge_min_soc`` is the register that actually holds the
        # self-use floor; the same install accepts and verifies a write to it.
        #
        # The suffix has to carry ``selfuse``: matching is by suffix, and the same
        # inverter also publishes backup_discharge_min_soc, feedin_discharge_min_soc
        # and eps_min_soc, none of which govern self-use either. The backup reserve
        # stays as a last resort for firmware that exposes nothing better -- where a
        # poor floor still beats no floor, since without one a hold cannot be
        # expressed as self-use and falls back to Manual mode.
        #
        # ``battery_minimum_capacity`` keeps its place at the front. It is the older
        # name for this floor and installs already bound to it are working; whether
        # it is the same register as selfuse_discharge_min_soc on firmware carrying
        # both is not something to guess at from one inverter. The bug was that
        # selfuse_discharge_min_soc was missing from this list altogether, so an
        # install without the older name fell through to the backup reserve.
        (
            "battery_minimum_capacity",
            "selfuse_discharge_min_soc",
            "battery_min_capacity",
            "selfuse_backup_soc",
        ),
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
        (
            "selfuse_night_charge_enable",
            # The name it actually has. The suffix above was one word longer than
            # the entity -- ``select.<prefix>_selfuse_night_charge`` -- so the
            # role never matched on a real SolaX, the capability reported false,
            # and the controller could not turn grid charging off. The inverter
            # went on topping the battery up from the grid whenever its own
            # self-use settings said to, through half-hours the plan had costed
            # as self-use, and the only clue was a line saying the control could
            # not be found. Both spellings appear across firmware.
            "selfuse_night_charge",
            "selfuse_nightcharge",
            "charge_from_grid",
            "grid_charge",
        ),
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


def entity_states(hass: Any, entities: dict[str, str]) -> dict[str, Any]:
    """What every bound entity reads right now, role by role.

    Diagnostics listed the entity *ids* a role had bound and the value of a
    hand-picked six. Everything else was invisible, and the thing that was
    invisible turned out to matter: a real install had its inverter quietly
    charging from the grid because a self-use setting was enabled, and the file
    that was supposed to explain the behaviour could not show the setting. The
    ids alone answer "what is it wired to"; only the values answer "and what is
    it wired to *saying*".

    Options, bounds and units come too, because the next question after "what
    does it read" is always "and what else could it read".
    """
    described: dict[str, Any] = {}
    for role, entity_id in sorted(entities.items()):
        state = hass.states.get(entity_id) if entity_id else None
        if state is None:
            described[role] = {"entity_id": entity_id, "state": None, "found": False}
            continue
        attributes = dict(getattr(state, "attributes", {}) or {})
        entry: dict[str, Any] = {
            "entity_id": entity_id,
            "state": getattr(state, "state", None),
            "found": True,
        }
        for key in ("options", "min", "max", "step", "unit_of_measurement"):
            if key in attributes:
                entry[key] = attributes[key]
        described[role] = entry
    return described


def unmatched_candidates(
    hass: Any,
    bound: dict[str, str],
    prefix: str | None = None,
    limit: int = 250,
) -> list[str]:
    """Inverter entities that look relevant but are not bound to any role.

    Guessing entity IDs is how "charge from grid" ended up bound to a peak-shaving
    switch, so this exists to stop the guessing: when a control is plainly there on
    the inverter's own screen and no role found it, the answer is in this list.
    Diagnostics only -- nothing reads it to make a decision.

    The cap was sixty, and a SolaX behind the full Modbus integration publishes
    well past that in ``number`` alone -- forty of them named ``remotecontrol_*``.
    Sorted alphabetically, the overflow fell exactly where ``selfuse_*`` sits, so
    the one control this list was written to find was in the six it did not show,
    on every file, for days. A diagnostics list that omits the answer is worse
    than no list: it reads as proof the entity is absent.
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
        if not any(word in object_id for word in CANDIDATE_WORDS):
            continue
        # Prefix mismatches are *shown*, marked, not filtered out. This list
        # exists to answer "the control is plainly there, why has nothing found
        # it?", and a wrong prefix is one of the two reasons it did not -- so
        # applying the same prefix that caused the miss guarantees the list
        # cannot explain it. A real install's grid-charge control was invisible
        # to every list the controller could produce, and was therefore reported
        # as a control the inverter did not have.
        if wanted_prefix and not object_id.startswith(wanted_prefix):
            found.append(f"{entity_id} (does not match the prefix '{prefix}')")
            continue
        found.append(entity_id)
    found.sort()
    if len(found) > limit:
        # Say so rather than stop mid-alphabet. This list exists to answer "the
        # control is plainly there, why has nothing found it?", and a silent cut
        # makes it answer "it does not exist" instead -- which is the one wrong
        # answer it can give.
        dropped = len(found) - limit
        return [*found[:limit], f"... and {dropped} more not shown"]
    return found


def _clean(text: str) -> str:
    return str(text).strip().lower().replace(" ", "_").replace("-", "_")


def merge_overrides(
    discovered: dict[str, str], overrides: dict[str, Any] | None
) -> dict[str, str]:
    """Apply user-supplied role overrides on top of discovery.

    An explicit empty string disables a role, which is how a user tells the
    controller "do not touch my export limit".

    One override is refused: our own entities. Discovery has always excluded them,
    because a role pointed at one makes the controller read or write its own
    output -- but the guard lived only in discovery, and an override walked past
    it. On a real install the grid-charge role was mapped to this integration's
    own "Allow grid charging" switch, and the two halves drove each other: a hold
    slot switches that permission off, an optimiser without grid charging can
    only plan more holds, and grid charging never comes back on. It went from
    eighteen charge slots to none, and sat flat at 63% for two days. The user did
    not turn it off and nothing said it had happened.
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
        elif _is_our_own(text.partition(".")[2] or text):
            # Left on whatever discovery found, which is the safe reading of an
            # override that cannot be honoured.
            _LOGGER.warning(
                "Ignoring the %s override: %s is one of this integration's own "
                "entities, so pointing an inverter role at it would have the "
                "controller drive itself",
                role,
                text,
            )
        else:
            result[role] = text
    return result
