"""Hardware-agnostic inverter control.

The planner emits a :class:`~..models.ControlCommand`; an adapter turns that into
whatever its inverter actually understands. Nothing above this layer knows about
Solax, Modbus, registers, or what kind of battery is attached -- which is what
lets the same planner drive an OEM pack today and a Battery Emulator tomorrow.

Adapters drive the inverter through **Home Assistant entities** rather than
speaking Modbus themselves. That is a deliberate choice: the existing inverter
integrations already solve transport, reconnection and register maps, and it
means any inverter with an HA integration that exposes its mode and current
limits can be supported by describing its entities, with no new protocol code.

These modules touch only ``hass.states`` and ``hass.services``, so they carry no
Home Assistant imports and can be tested against a stub.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import ControlCommand, SlotAction

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE_STATES = ("unknown", "unavailable", "none", "", None)

# How long to wait before reading back a write. Modbus writes over a WiFi
# dongle are not instantaneous, and the HA entity only updates on its next poll.
VERIFY_DELAY_SECONDS = 2.0


@dataclass(slots=True)
class Capabilities:
    """What an inverter can actually be told to do.

    The planner is not restricted by these -- it plans against the physics -- but
    the adapter uses them to pick the closest achievable behaviour, and the
    coordinator surfaces them so a user can see why something was not applied.
    """

    force_charge: bool = False
    force_discharge: bool = False
    idle: bool = False
    self_use: bool = True
    charge_power_limit: bool = False
    discharge_power_limit: bool = False
    target_soc: bool = False
    min_soc: bool = False
    export_limit: bool = False
    grid_charge_toggle: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "force_charge": self.force_charge,
            "force_discharge": self.force_discharge,
            "idle": self.idle,
            "self_use": self.self_use,
            "charge_power_limit": self.charge_power_limit,
            "discharge_power_limit": self.discharge_power_limit,
            "target_soc": self.target_soc,
            "min_soc": self.min_soc,
            "export_limit": self.export_limit,
            "grid_charge_toggle": self.grid_charge_toggle,
        }

    def supports(self, action: SlotAction) -> bool:
        if action is SlotAction.CHARGE:
            return self.force_charge
        if action is SlotAction.DISCHARGE:
            return self.force_discharge
        if action is SlotAction.IDLE:
            return self.idle
        return self.self_use


@dataclass(slots=True)
class Write:
    """A single pending or completed entity write."""

    domain: str
    service: str
    entity_id: str
    value: Any
    role: str = ""
    field: str = ""
    """Service data key carrying ``value``. Empty for services that take none
    (``switch.turn_on`` and friends), where the value is only used for
    read-back verification."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": f"{self.domain}.{self.service}",
            "entity_id": self.entity_id,
            "value": self.value,
            "role": self.role,
        }

    def describe(self) -> str:
        return f"{self.entity_id} -> {self.value}"


@dataclass(slots=True)
class ApplyResult:
    """Outcome of applying one command."""

    action: SlotAction
    dry_run: bool
    writes: list[Write] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    changed: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def verified(self) -> bool:
        return not self.unverified and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "writes": [w.as_dict() for w in self.writes],
            "skipped": self.skipped,
            "errors": self.errors,
            "unverified": self.unverified,
            "verified": self.verified,
        }

    def summary(self) -> str:
        if self.errors:
            return f"failed: {'; '.join(self.errors)}"
        if not self.writes:
            return "no change needed"
        prefix = "would set" if self.dry_run else "set"
        body = ", ".join(w.describe() for w in self.writes)
        if self.unverified:
            return f"{prefix} {body} (unverified: {', '.join(self.unverified)})"
        return f"{prefix} {body}"


@dataclass(slots=True)
class InverterState:
    """What the inverter currently reports."""

    available: bool = False
    soc: float | None = None
    mode: str | None = None
    manual_mode: str | None = None
    battery_voltage: float | None = None
    battery_power_kw: float | None = None
    pv_power_kw: float | None = None
    grid_power_kw: float | None = None
    load_power_kw: float | None = None
    min_soc: float | None = None
    locked: bool | None = None
    run_mode: str | None = None
    """The inverter's operating state, e.g. "Normal Mode" or "EPS Mode"."""
    eps_voltage: float | None = None
    """Backup output voltage: ~230 V while islanded, 0 on grid."""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def islanded(self) -> bool:
        """Whether the inverter is running its backup output off the battery.

        The grid is gone, which changes everything the controller believes:
        prices stop meaning anything, "import" is impossible, Manual-mode writes
        steer nothing, and the load sensor may honestly report a house whose
        non-essential circuits have been shed. Two independent signals, either
        sufficient: the run-mode text (any EPS state counts, including EPS
        Check -- transitional is still off-grid), and the backup output being
        energised, which no grid-tied state produces.
        """
        if self.run_mode and "eps" in self.run_mode.lower():
            return True
        return self.eps_voltage is not None and self.eps_voltage > 100.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "soc": self.soc,
            "mode": self.mode,
            "manual_mode": self.manual_mode,
            "battery_voltage": self.battery_voltage,
            "min_soc": self.min_soc,
            "locked": self.locked,
            "run_mode": self.run_mode,
            "eps_voltage": self.eps_voltage,
            "islanded": self.islanded,
        }

    def power_summary(self) -> dict[str, Any]:
        """The four live flows, which ``as_dict`` has never carried.

        They are read on every cycle and then thrown away before anything can
        look at them, so a diagnostics download could say what mode the inverter
        was in but not what it was *doing*. Every argument about whether the
        battery was charging has therefore been settled by differencing a
        whole-percent state of charge across a half-hour -- which is how a
        forty-three per cent under-count of throughput went unnoticed for weeks.

        ``None`` where no entity is mapped for that role, which is itself the
        answer to why the figure is missing.
        """
        return {
            "battery_kw": self.battery_power_kw,
            "pv_kw": self.pv_power_kw,
            "grid_kw": self.grid_power_kw,
            "load_kw": self.load_power_kw,
        }


def pick_option(options: Sequence[str], candidates: Iterable[str]) -> str | None:
    """Choose the option best matching one of ``candidates``.

    Inverter integrations word their mode options inconsistently -- "Back Up
    Mode" vs "Backup", "Manual Mode" vs "Manual" -- and the wording changes
    between firmware versions. Matching loosely is far more durable than
    demanding an exact string, and stops a harmless relabel upstream from
    silently disabling control.

    Matching is restricted to a **prefix** relation rather than free substring
    containment, which would be actively dangerous: "Locked" contains "locked",
    a substring of "Unlocked", so a containment match would report a locked
    inverter as unlocked. Every subsequent setting write would then be rejected
    by the inverter while the controller believed it had succeeded.
    """
    if not options:
        return None
    normalised = {opt: _normalise(opt) for opt in options}
    for candidate in candidates:
        target = _normalise(candidate)
        if not target:
            continue
        for option, norm in normalised.items():
            if norm == target:
                return option
        # A prefix relation in either direction covers the real-world variations
        # ("Backup" vs "Back Up Mode", "Manual" vs "Manual Mode") without
        # matching a word that merely ends with the candidate.
        for option, norm in normalised.items():
            if norm.startswith(target) or target.startswith(norm):
                return option
    return None


def _normalise(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def current_from_power(
    power_kw: float, voltage: float | None, fallback_voltage: float
) -> float:
    """Convert an AC power target into a battery current limit in amps.

    Solax (and several others) limit battery charge/discharge by **current**,
    not power, so a power plan has to be divided by the pack voltage. Using the
    live voltage matters on a high-voltage EV pack, whose terminal voltage swings
    widely between empty and full -- a fixed assumption would under- or
    over-shoot the intended power by tens of percent.
    """
    volts = voltage if voltage and voltage > 20.0 else fallback_voltage
    if volts <= 0:
        return 0.0
    return max(power_kw, 0.0) * 1000.0 / volts


def entity_unit(hass: Any, entity_id: str | None) -> str:
    """The ``unit_of_measurement`` of an entity, lowercased."""
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    if state is None:
        return ""
    return str(state.attributes.get("unit_of_measurement") or "").strip().lower()


def power_limit_value(
    hass: Any,
    entity_id: str,
    power_kw: float,
    voltage: float | None = None,
    fallback_voltage: float = 360.0,
) -> float:
    """Convert a power target into whatever unit the limit entity expects.

    Inverters disagree about how a charge limit is expressed: Solax uses battery
    **current** in amps, others use **power** in watts or kilowatts. Reading the
    entity's own declared unit means one code path handles all of them, instead
    of hard-coding an assumption that silently misconfigures the wrong inverter
    by a factor of a few hundred.
    """
    unit = entity_unit(hass, entity_id)
    if unit in ("a", "amp", "amps"):
        return current_from_power(power_kw, voltage, fallback_voltage)
    if unit == "kw":
        return max(power_kw, 0.0)
    if unit == "w":
        return max(power_kw, 0.0) * 1000.0
    # No unit declared. Infer from the entity's range: a limit that tops out in
    # the hundreds or thousands is watts, a small one is amps or kilowatts.
    _, maximum, _ = number_bounds(hass, entity_id)
    if maximum > 1000:
        return max(power_kw, 0.0) * 1000.0
    if maximum > 100:
        return current_from_power(power_kw, voltage, fallback_voltage)
    return max(power_kw, 0.0)


def power_kw(hass: Any, entity_id: str | None) -> float | None:
    """Read a power sensor and normalise it to kilowatts.

    Integrations report power in W, kW or occasionally MW. Trusting the declared
    unit rather than assuming watts avoids a 1000x error that would look like a
    plausible-but-wrong plan.
    """
    value = state_float(hass, entity_id)
    if value is None:
        return None
    unit = entity_unit(hass, entity_id)
    if unit == "kw":
        return value
    if unit == "mw":
        return value * 1000.0
    if unit == "w":
        return value / 1000.0
    # Undeclared: assume watts, the overwhelmingly common case for inverter
    # integrations, unless the magnitude makes that implausible for a house.
    return value if abs(value) < 100.0 else value / 1000.0


def state_value(hass: Any, entity_id: str | None) -> str | None:
    """Current state string of an entity, or ``None`` if unusable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in UNAVAILABLE_STATES:
        return None
    return state.state


def state_float(hass: Any, entity_id: str | None) -> float | None:
    raw = state_value(hass, entity_id)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def state_options(hass: Any, entity_id: str | None) -> list[str]:
    """The ``options`` attribute of a select entity."""
    if not entity_id:
        return []
    state = hass.states.get(entity_id)
    if state is None:
        return []
    options = state.attributes.get("options")
    if isinstance(options, list):
        return [str(o) for o in options]
    return []


def number_bounds(hass: Any, entity_id: str | None) -> tuple[float, float, float]:
    """``(min, max, step)`` for a number entity, with sane defaults."""
    state = hass.states.get(entity_id) if entity_id else None
    attributes = state.attributes if state else {}
    minimum = _to_float(attributes.get("min"), 0.0)
    maximum = _to_float(attributes.get("max"), 100.0)
    step = _to_float(attributes.get("step"), 1.0)
    return minimum, maximum, step


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp_to_number(hass: Any, entity_id: str, value: float) -> float:
    """Clamp and quantise a value to a number entity's advertised bounds.

    Writing outside the bounds makes HA reject the whole service call, so a
    plan asking for 4 kW on a 3.6 kW inverter must be trimmed, not refused.
    """
    minimum, maximum, step = number_bounds(hass, entity_id)
    clamped = min(max(value, minimum), maximum)
    if step > 0:
        steps = round((clamped - minimum) / step)
        clamped = minimum + steps * step
        clamped = min(max(clamped, minimum), maximum)
    return round(clamped, 3)


class InverterAdapter(ABC):
    """Translates control commands into inverter-specific entity writes."""

    name: str = "adapter"

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self._last_applied: ControlCommand | None = None
        # Roles the inverter has refused to write, and what it said, so we ask once
        # rather than every cycle for ever.
        self._rejected: dict[str, str] = {}

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """What this inverter can be asked to do."""

    @abstractmethod
    async def async_read_state(self) -> InverterState:
        """Read the inverter's current mode and measurements."""

    @abstractmethod
    def plan_writes(self, command: ControlCommand) -> tuple[list[Write], list[str]]:
        """Return the writes needed for ``command`` plus any skipped reasons.

        Kept separate from applying so that dry-run mode can show exactly what
        would happen without touching the hardware, using the identical code
        path that real control uses.
        """

    @property
    def last_applied(self) -> ControlCommand | None:
        return self._last_applied

    def reset_last_applied(self) -> None:
        """Force the next apply to write, e.g. after a manual override."""
        self._last_applied = None
        # A deliberate action is also the moment to stop sulking: whatever the
        # inverter refused before, try it once more rather than carrying the refusal
        # forward for ever.
        self._rejected.clear()

    def _note_rejection(self, write: Write, err: Exception) -> None:
        """Remember a role the inverter refused, so we stop asking every cycle.

        A real inverter rejected its own reserve register outright -- Modbus exception
        4, device failure -- and the controller asked again every five minutes for
        hours. Each attempt failed the whole apply, so ``write_verified`` stayed false
        and every genuine problem was buried underneath a known one. Asking once and
        saying so is more useful than asking three hundred times.
        """
        if write.role:
            self._rejected[write.role] = str(err)

    def rejected_roles(self) -> dict[str, str]:
        """Roles the inverter has refused to write, and what it said."""
        return dict(self._rejected)

    def drop_rejected_writes(
        self, writes: list[Write], skipped: list[str]
    ) -> list[Write]:
        """Filter out writes for roles the inverter has already refused."""
        if not self._rejected:
            return writes
        kept: list[Write] = []
        for write in writes:
            reason = self._rejected.get(write.role) if write.role else None
            if reason is None:
                kept.append(write)
                continue
            skipped.append(
                f"not retrying {write.entity_id}: the inverter refused it "
                f"({reason.split(':')[-1].strip()[:80]})"
            )
        return kept

    async def async_apply(
        self, command: ControlCommand, dry_run: bool = True, verify: bool = True
    ) -> ApplyResult:
        """Apply a command, optionally verifying the writes landed."""
        result = ApplyResult(action=command.action, dry_run=dry_run)

        writes, skipped = self.plan_writes(command)
        writes = self.drop_rejected_writes(writes, skipped)
        result.skipped = skipped
        result.writes = writes
        result.changed = bool(writes)

        if not writes:
            self._last_applied = command
            return result

        if dry_run:
            _LOGGER.debug("Dry run: %s", result.summary())
            return result

        for write in writes:
            data: dict[str, Any] = {"entity_id": write.entity_id}
            if write.field:
                data[write.field] = write.value
            try:
                await self.hass.services.async_call(
                    write.domain, write.service, data, blocking=True
                )
            except Exception as err:
                _LOGGER.warning(
                    "Failed writing %s to %s: %s", write.value, write.entity_id, err
                )
                result.errors.append(f"{write.entity_id}: {err}")
                self._note_rejection(write, err)

        if verify and not result.errors:
            result.unverified = await self._async_verify(writes)

        if result.ok:
            self._last_applied = command
        else:
            # Do not remember a failed command, so the next cycle retries it.
            self._last_applied = None
        return result

    async def _async_verify(self, writes: list[Write]) -> list[str]:
        """Read back each write and report any that did not stick.

        This matters more than it might seem: a Solax Pocket WiFi dongle will
        happily accept a Modbus write and drop it. Without read-back the
        controller would believe it was charging while the inverter sat idle.
        """
        await asyncio.sleep(VERIFY_DELAY_SECONDS)
        failed: list[str] = []
        for write in writes:
            actual = state_value(self.hass, write.entity_id)
            if actual is None:
                failed.append(f"{write.entity_id} unavailable")
                continue
            if isinstance(write.value, (int, float)):
                try:
                    if abs(float(actual) - float(write.value)) > max(
                        abs(float(write.value)) * 0.05, 0.5
                    ):
                        failed.append(
                            f"{write.entity_id}={actual} (wanted {write.value})"
                        )
                except (TypeError, ValueError):
                    failed.append(f"{write.entity_id}={actual} (not numeric)")
            elif _normalise(actual) != _normalise(str(write.value)):
                failed.append(f"{write.entity_id}={actual} (wanted {write.value})")
        return failed

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "capabilities": self.capabilities.as_dict(),
        }


class NullAdapter(InverterAdapter):
    """Plans nothing and writes nothing.

    Used when a user wants the analysis, forecasting and plan display without
    giving the integration any control at all -- a legitimate end state, not
    just a placeholder.
    """

    name = "none"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(self_use=False)

    async def async_read_state(self) -> InverterState:
        return InverterState(available=False)

    def plan_writes(self, command: ControlCommand) -> tuple[list[Write], list[str]]:
        return [], ["no inverter adapter configured; advisory only"]
