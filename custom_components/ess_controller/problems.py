"""Faults worth interrupting someone for, and the wording to interrupt them with.

Everything this integration gets wrong is visible somewhere -- a skipped write, an
unfilled role, a sensor reading nothing, a battery filling from the grid in a
half-hour the plan costed as self-use. None of it was ever *said*. A whole day
went on discovering, one screenshot at a time, faults the controller already had
the facts to name: a grid-charge control it could not find, an inverter quietly
charging outside the plan, forecast entities that had stopped existing.

So this module decides what is wrong, and Home Assistant's Repairs panel says it.
Two rules shape what gets in:

* **Only what a person can act on.** "The forecast is uncertain" is not a fault,
  it is Tuesday. "The entity you configured no longer exists" is.
* **Only what persists.** A Modbus write that fails once and succeeds on the
  retry is noise; the same write failing for half an hour is a fault. Persistence
  is applied *once, to everything*, by the caller: a rule says only what is wrong
  right now, and a fault has to be wrong for ``PERSIST_CYCLES`` cycles running
  before anybody is told. That used to be a counter threaded through a single
  rule, which left every other rule firing on its first look -- so a restart
  raised "the state of charge cannot be read" and "your forecast entity is
  missing" in the half-minute before those sensors had published anything, and
  cleared them again unprompted. Transient by construction, alarming by
  accident.

Home Assistant-free on purpose: this takes a plain snapshot of facts and returns
plain descriptions, so every rule can be tested against a hand-written case
rather than a running instance. The thin layer that turns these into repairs
lives in the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# How many consecutive cycles a symptom must persist before it is a fault.
# The coordinator plans every five minutes, so three is a quarter of an hour --
# long enough to ride out a Modbus timeout, a restarting integration, or a whole
# startup's worth of entities arriving one by one, and short enough that a real
# fault is named while the person who caused it is still near the inverter.
#
# Applied by the caller to every rule alike, so a rule never has to count for
# itself and cannot be forgotten when the next one is written.
PERSIST_CYCLES = 3

# How long a load sensor may report nothing before the silence is the fault
# rather than the weather. Solar is excluded: a genuinely dark half-hour reads
# zero and is not a problem, whereas a house that draws nothing for hours is a
# sensor that has stopped.
QUIET_LOAD_SLOTS = 6


@dataclass(frozen=True, slots=True)
class Problem:
    """One fault, in the words the Repairs panel will show."""

    key: str
    """Stable issue id. Changing it orphans anyone's dismissed issue, so do not."""

    severity: str
    """``warning`` or ``error``. Error is for "it has stopped controlling"."""

    placeholders: dict[str, str] = field(default_factory=dict)
    """Values interpolated into the translated text."""


@dataclass(slots=True)
class Snapshot:
    """The facts the rules below are allowed to look at.

    Deliberately narrow. A detector that could reach into the whole coordinator
    would grow conditions nobody can test; one that takes eleven plain fields
    can be handed a dataclass in a test and asked what it thinks.
    """

    controlling: bool = False
    """Whether the user has asked it to write at all. Almost nothing is a fault
    on an install that was never given control."""

    on_backup_power: bool = False
    """The inverter is islanded, carrying the house on its EPS output."""

    inverter_available: bool = True
    soc_readable: bool = True
    rejected_roles: dict[str, str] = field(default_factory=dict)
    unverified_writes: list[str] = field(default_factory=list)
    unfilled_control_roles: list[str] = field(default_factory=list)
    disabled_candidates: list[str] = field(default_factory=list)
    missing_forecast_entities: list[str] = field(default_factory=list)
    unexplained_charge_kwh: float = 0.0
    unexplained_charge_cost: float = 0.0
    quiet_load_slots: int = 0


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def detect(state: Snapshot) -> list[Problem]:
    """Everything currently wrong, worst first."""
    found: list[Problem] = []

    # Raised whether or not the controller was given write access: a house on
    # battery power wants to know either way, and wants to know why the
    # dashboard's plan has stopped meaning anything.
    if state.on_backup_power:
        found.append(Problem("running_on_backup_power", "warning"))

    # -- it has stopped controlling -------------------------------------
    if state.controlling and not state.inverter_available:
        found.append(Problem("inverter_unreachable", "error"))

    if state.controlling and not state.soc_readable:
        # The controller refuses to write on a guessed state of charge, which is
        # right and completely silent: from outside it looks like a system that
        # has decided to do nothing all day for no reason.
        found.append(Problem("soc_unreadable", "error"))

    if state.controlling and state.rejected_roles:
        found.append(
            Problem(
                "writes_rejected",
                "error",
                {
                    "controls": ", ".join(sorted(state.rejected_roles)),
                    "reason": next(iter(sorted(state.rejected_roles.values()))),
                },
            )
        )

    if state.controlling and state.unverified_writes:
        # A dongle that accepts a write and drops it. The plan and the hardware
        # disagree from here on, and nothing else will say so.
        found.append(
            Problem(
                "writes_not_verified",
                "warning",
                {"controls": ", ".join(sorted(set(state.unverified_writes)))},
            )
        )

    # -- something else is driving the inverter --------------------------
    if state.unexplained_charge_kwh > 0:
        found.append(
            Problem(
                "unexplained_grid_charging",
                "warning",
                {
                    "kwh": f"{state.unexplained_charge_kwh:.1f}",
                    "cost": f"{state.unexplained_charge_cost:.0f}",
                },
            )
        )

    # -- it cannot see or reach something it was told about ---------------
    if state.missing_forecast_entities:
        found.append(
            Problem(
                "forecast_entity_missing",
                "warning",
                {
                    "entities": ", ".join(sorted(state.missing_forecast_entities)),
                    "count": str(len(state.missing_forecast_entities)),
                },
            )
        )

    if state.quiet_load_slots >= QUIET_LOAD_SLOTS:
        found.append(
            Problem(
                "load_not_measured",
                "warning",
                {"slots": str(state.quiet_load_slots)},
            )
        )

    # A control the inverter has and the controller could not find is the fault
    # that hid longest: it reads as a hardware limitation, so nobody looks again.
    # Only raised when a matching disabled entity exists, because that is the
    # case with an answer -- enable it and the role binds.
    if state.controlling and state.unfilled_control_roles and state.disabled_candidates:
        found.append(
            Problem(
                "control_disabled_in_home_assistant",
                "warning",
                {
                    "roles": ", ".join(sorted(state.unfilled_control_roles)),
                    "entities": ", ".join(sorted(state.disabled_candidates)[:3]),
                    "noun": _plural(
                        len(state.unfilled_control_roles), "control", "controls"
                    ),
                },
            )
        )

    return found
