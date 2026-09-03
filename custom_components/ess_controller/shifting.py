"""Flexible load shifting.

Moving a dishwasher, immersion heater or EV charge into a cheap slot is often
worth more than any battery arbitrage, because the energy is bought cheaply *and*
never suffers round-trip losses. This module decides when to run those loads.

Approach: two-stage, rather than folding the loads into the dynamic program.
Adding "has this load run yet" to the DP state would multiply the state space by
two per load, and the coupling between loads and the battery is weak enough that
it is not worth it.

1. Price each candidate window using the **marginal** cost of extra consumption
   in each slot of the *current* battery plan. That is not simply the import
   price: in a slot where the plan already exports, extra load costs you the
   export revenue you forgo; in a slot where the plan curtails surplus PV, extra
   load is nearly free.
2. Place the loads, add them to the load forecast, and re-run the battery
   optimiser so the battery re-plans around them.

Repeating that twice converges in practice: the first pass places loads against
the battery-only plan, the second re-places them against a plan that already
knows about them.

Home Assistant-free, so the placement logic is directly testable.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .models import HorizonSlot, Plan, SlotAction

_LOGGER = logging.getLogger(__name__)

EPS = 1e-9


@dataclass(slots=True)
class ShiftableLoad:
    """A load whose timing is flexible but whose energy is not."""

    name: str
    energy_kwh: float
    power_kw: float
    earliest: time | None = None
    """Earliest local start time. ``None`` means no restriction."""
    latest: time | None = None
    """Latest local time by which the load must have *finished*."""
    enabled: bool = True
    must_run_daily: bool = True
    """Whether it needs to run every day, or only when explicitly requested."""
    days: frozenset[int] | None = None
    """Local weekdays the load may be placed on (0=Monday), or None for any.

    A sauna is a Tuesday-and-Saturday sort of load, and provisioning it daily
    buys energy for sessions that are not happening. The run must *start* on an
    allowed day; one that begins Saturday 23:30 and finishes Sunday morning is a
    Saturday sauna, whatever the clock says when it ends."""
    switch_entity: str | None = None
    """Optional entity to switch on for the scheduled window.

    Most people's dishwasher has a dial and a door, not an API, so this is
    deliberately optional: with it empty the load is still scheduled and
    published, and you read the time off the sensor and press the button
    yourself. Fill it in only if the appliance (or the plug it is on) actually
    appears in Home Assistant as something switchable.
    """

    def __post_init__(self) -> None:
        if self.energy_kwh <= 0:
            raise ValueError(f"{self.name}: energy_kwh must be positive")
        if self.power_kw <= 0:
            raise ValueError(f"{self.name}: power_kw must be positive")
        self.switch_entity = _clean_entity_id(self.switch_entity, self.name)

    @property
    def controllable(self) -> bool:
        return self.switch_entity is not None

    @property
    def duration_hours(self) -> float:
        return self.energy_kwh / self.power_kw

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy_kwh": self.energy_kwh,
            "power_kw": self.power_kw,
            "duration_hours": round(self.duration_hours, 3),
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
            "enabled": self.enabled,
            "days": format_days(self.days),
            "switch": self.switch_entity,
        }


@dataclass(slots=True)
class LoadPlacement:
    """Where a shiftable load has been scheduled."""

    name: str
    start: datetime
    end: datetime
    energy_kwh: float
    power_kw: float
    cost: float
    """Estimated marginal cost of running here, in minor units."""
    best_alternative_cost: float | None = None
    """Cost of the most expensive feasible window, for showing the saving."""
    switch_entity: str | None = None
    """Carried through from the load, so the caller can drive it if it has one."""

    @property
    def saving_vs_worst(self) -> float | None:
        if self.best_alternative_cost is None:
            return None
        return self.best_alternative_cost - self.cost

    def running_at(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "energy_kwh": round(self.energy_kwh, 3),
            "power_kw": round(self.power_kw, 3),
            "cost": round(self.cost, 2),
            "worst_case_cost": (
                round(self.best_alternative_cost, 2)
                if self.best_alternative_cost is not None
                else None
            ),
            "saving": (
                round(self.saving_vs_worst, 2)
                if self.saving_vs_worst is not None
                else None
            ),
            "switch": self.switch_entity,
        }


def marginal_prices(slots: list[HorizonSlot], plan: Plan | None) -> list[float]:
    """The cost of one extra kWh consumed in each slot.

    Without a plan this is just the import price. With one it is more subtle, and
    getting it right is what stops loads being scheduled into slots that look
    cheap but are not:

    * The plan **spills** surplus PV -> that energy is being thrown away, so
      extra load is free.
    * The plan **exports** in this slot -> extra load forgoes export revenue, so
      the marginal cost is the *export* price.
    * Otherwise -> the import price.

    The true marginal cost is piecewise: in a slot that both exports and spills,
    the first kWh of extra load is free (it soaks up the spill) and the rest
    costs the export price. Collapsing that to one number per slot is what the
    placement search needs, so the tie is broken by which flow dominates -- a
    slot spilling most of its surplus reads as free, one exporting most of it
    reads as the export price. Treating any spill at all as free would badly
    under-price a slot that exports 1.8 kWh while spilling 0.06.
    """
    if plan is None or not plan.slots:
        return [slot.import_price for slot in slots]

    by_start = {p.start: p for p in plan.slots}
    prices: list[float] = []
    for slot in slots:
        planned = by_start.get(slot.start)
        if planned is None:
            prices.append(slot.import_price)
            continue
        spilling = planned.curtailed_kwh > 0.01
        exporting = planned.grid_export_kwh > 0.01
        if spilling and planned.curtailed_kwh >= planned.grid_export_kwh:
            prices.append(0.0)
        elif exporting:
            prices.append(planned.export_price)
        elif spilling:
            prices.append(0.0)
        else:
            prices.append(planned.import_price)
    return prices


def _within_window(
    local_start: datetime,
    local_end: datetime,
    earliest: time | None,
    latest: time | None,
) -> bool:
    """Whether a run fits the load's allowed local time window.

    A window whose ``latest`` is earlier than its ``earliest`` wraps midnight,
    which is the normal case for an overnight load such as an immersion heater.
    """
    if earliest is None and latest is None:
        return True

    start_clock = local_start.time()
    end_clock = local_end.time()

    if earliest is not None and latest is not None and latest <= earliest:
        # Wrapping window: the run must sit entirely in one of the two arms.
        in_evening = start_clock >= earliest and (
            end_clock >= earliest or end_clock <= latest
        )
        in_morning = start_clock <= latest and end_clock <= latest
        # Reject a run that spans more than the window can hold.
        if local_end - local_start > timedelta(hours=24):
            return False
        return in_evening or in_morning

    if earliest is not None and start_clock < earliest:
        return False
    if latest is not None:
        # A run finishing exactly at the deadline is acceptable.
        if local_end.date() != local_start.date():
            return False
        if end_clock > latest:
            return False
    return True


def place_load(
    load: ShiftableLoad,
    slots: list[HorizonSlot],
    prices: list[float],
    to_local,
    occupied: list[float] | None = None,
    max_slot_power_kw: float | None = None,
) -> LoadPlacement | None:
    """Find the cheapest feasible window for one load.

    ``occupied`` accumulates power already committed to other shiftable loads in
    each slot, so two of them are not stacked beyond the connection limit.
    """
    if not slots or not load.enabled:
        return None

    duration = timedelta(hours=load.duration_hours)
    costs: list[tuple[float, int, datetime, datetime]] = []

    for index, slot in enumerate(slots):
        start = slot.start
        end = start + duration
        if end > slots[-1].end:
            break
        if not _within_window(to_local(start), to_local(end), load.earliest, load.latest):
            continue
        if load.days is not None and to_local(start).weekday() not in load.days:
            continue

        # Spread the load's energy across the slots it overlaps and price it.
        cost = 0.0
        feasible = True
        for other_index in range(index, len(slots)):
            other = slots[other_index]
            if other.start >= end:
                break
            overlap_start = max(other.start, start)
            overlap_end = min(other.end, end)
            hours = (overlap_end - overlap_start).total_seconds() / 3600.0
            if hours <= 0:
                continue
            if max_slot_power_kw is not None and occupied is not None:
                committed = occupied[other_index] + load.power_kw
                if committed > max_slot_power_kw + EPS:
                    feasible = False
                    break
            cost += load.power_kw * hours * prices[other_index]
        if not feasible:
            continue
        costs.append((cost, index, start, end))

    if not costs:
        _LOGGER.debug("No feasible window found for %s", load.name)
        return None

    cheapest = min(costs, key=lambda item: item[0])
    dearest = max(costs, key=lambda item: item[0])
    cost, index, start, end = cheapest

    if occupied is not None:
        for other_index in range(index, len(slots)):
            if slots[other_index].start >= end:
                break
            occupied[other_index] += load.power_kw

    return LoadPlacement(
        name=load.name,
        start=start,
        end=end,
        energy_kwh=load.energy_kwh,
        power_kw=load.power_kw,
        cost=cost,
        best_alternative_cost=dearest[0],
        switch_entity=load.switch_entity,
    )


def place_loads(
    loads: list[ShiftableLoad],
    slots: list[HorizonSlot],
    plan: Plan | None,
    to_local,
    max_slot_power_kw: float | None = None,
) -> list[LoadPlacement]:
    """Place every enabled load, largest first.

    Largest first matters: a big load has fewer feasible windows once the
    connection limit is considered, so it should get first choice of the cheap
    slots rather than being squeezed out by a kettle.
    """
    active = [load for load in loads if load.enabled]
    if not active or not slots:
        return []

    prices = marginal_prices(slots, plan)
    occupied = [0.0] * len(slots)
    placements: list[LoadPlacement] = []

    for load in sorted(active, key=lambda item: item.energy_kwh, reverse=True):
        placement = place_load(load, slots, prices, to_local, occupied, max_slot_power_kw)
        if placement is not None:
            placements.append(placement)

    return sorted(placements, key=lambda p: p.start)


def add_placements_to_slots(
    slots: list[HorizonSlot], placements: list[LoadPlacement]
) -> list[HorizonSlot]:
    """Return a copy of the horizon with the placed loads folded into demand.

    The battery optimiser then re-plans knowing the loads will run, which is what
    lets it pre-charge for an immersion heater rather than being surprised by it.
    """
    if not placements:
        return slots

    extra = [0.0] * len(slots)
    for placement in placements:
        for index, slot in enumerate(slots):
            overlap_start = max(slot.start, placement.start)
            overlap_end = min(slot.end, placement.end)
            hours = (overlap_end - overlap_start).total_seconds() / 3600.0
            if hours > 0:
                extra[index] += placement.power_kw * hours

    return [
        HorizonSlot(
            start=slot.start,
            end=slot.end,
            import_price=slot.import_price,
            export_price=slot.export_price,
            pv_kwh=slot.pv_kwh,
            load_kwh=slot.load_kwh + extra[index],
            pv_is_forecast=slot.pv_is_forecast,
            load_is_forecast=slot.load_is_forecast,
            price_is_forecast=slot.price_is_forecast,
        )
        for index, slot in enumerate(slots)
    ]


def parse_shiftable_loads(raw: Any) -> list[ShiftableLoad]:
    """Parse shiftable load definitions from config.

    Accepts a list of mappings, or the compact string form
    ``"Dishwasher=1.2kWh@2kW,22:00-06:00"`` so loads can be typed into a single
    config-flow text field.
    """
    loads: list[ShiftableLoad] = []
    if not raw:
        return loads

    entries: list[Any]
    if isinstance(raw, str):
        # The config flow offers a multiline box, so a newline is the separator
        # people actually reach for; semicolons stay supported for one-liners.
        entries = [
            chunk.strip()
            for line in raw.splitlines()
            for chunk in line.split(";")
            if chunk.strip()
        ]
    elif isinstance(raw, list):
        entries = raw
    else:
        _LOGGER.warning("Unrecognised shiftable load definition: %r", raw)
        return loads

    for entry in entries:
        try:
            loads.append(
                _parse_compact(entry) if isinstance(entry, str) else _parse_mapping(entry)
            )
        except (KeyError, ValueError, TypeError, AttributeError) as err:
            _LOGGER.warning("Skipping bad shiftable load %r: %s", entry, err)
    return loads


# Three-letter prefixes, because "Thurs", "thu" and "Thursday" are all the same
# intent, plus the two groupings people actually mean.
_DAY_TOKENS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_DAY_GROUPS: dict[str, tuple[int, ...]] = {
    "weekdays": (0, 1, 2, 3, 4),
    "weekends": (5, 6),
    "weekend": (5, 6),
    "daily": (0, 1, 2, 3, 4, 5, 6),
}
_DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_days(value: Any) -> frozenset[int] | None:
    """Parse a day-of-week restriction; ``None`` means every day.

    Accepts ``"tue/thu/sat"``, a list of names, or ``"weekends"``. Raises on a
    token that is not a day, so a typo costs the load definition a warning
    rather than silently planning the sauna for the wrong day of the week.
    """
    if value is None:
        return None
    if isinstance(value, str):
        tokens = [t for t in re.split(r"[/\s]+", value.strip().lower()) if t]
    else:
        tokens = [str(t).strip().lower() for t in value]
    if not tokens:
        return None
    days: set[int] = set()
    for token in tokens:
        if token in _DAY_GROUPS:
            days.update(_DAY_GROUPS[token])
            continue
        prefix = token[:3]
        if prefix not in _DAY_TOKENS:
            raise ValueError(f"not a day of the week: {token!r}")
        days.add(_DAY_TOKENS[prefix])
    return frozenset(days) if len(days) < 7 else None


def format_days(days: frozenset[int] | None) -> str | None:
    if days is None:
        return None
    return "/".join(_DAY_NAMES[d] for d in sorted(days))


def _day_intended(text: str) -> bool:
    """Whether a field is *trying* to be a day list, however badly.

    "tue/tomorow" must fail the load rather than be shrugged off as an unknown
    field: half-parsed days would quietly plan the sauna for every day of the
    week, which is the exact mistake the restriction exists to prevent. A field
    with no day in it at all is somebody's typo'd switch entity, and that costs
    them the automation, not the definition.
    """
    tokens = [t for t in re.split(r"[/\s]+", text.strip().lower()) if t]
    return any(t in _DAY_GROUPS or t[:3] in _DAY_TOKENS for t in tokens)


def loads_to_text(raw: Any) -> str:
    """Render stored definitions in the compact one-line-each form.

    The options flow edits loads as structured forms and stores them as
    mappings, but the bulk-edit textarea and the initial setup still speak the
    compact form -- so the two views have to round-trip through each other.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    lines: list[str] = []
    for load in parse_shiftable_loads(raw):
        parts = [f"{load.name}={load.energy_kwh:g}kWh@{load.power_kw:g}kW"]
        if load.earliest is not None or load.latest is not None:
            begin = load.earliest.strftime("%H:%M") if load.earliest else ""
            finish = load.latest.strftime("%H:%M") if load.latest else ""
            parts.append(f"{begin}-{finish}")
        if load.days is not None:
            parts.append(format_days(load.days) or "")
        if load.switch_entity:
            parts.append(load.switch_entity)
        lines.append(",".join(parts))
    return "\n".join(lines)


def _parse_mapping(entry: Any) -> ShiftableLoad:
    return ShiftableLoad(
        name=str(entry["name"]),
        energy_kwh=float(entry["energy_kwh"]),
        power_kw=float(entry["power_kw"]),
        earliest=_parse_time(entry.get("earliest")),
        latest=_parse_time(entry.get("latest")),
        enabled=bool(entry.get("enabled", True)),
        must_run_daily=bool(entry.get("must_run_daily", True)),
        days=parse_days(entry.get("days")),
        switch_entity=entry.get("switch") or entry.get("switch_entity"),
    )


def _parse_compact(text: str) -> ShiftableLoad:
    """Parse ``Sauna=4.5kWh@3.6kW,16:00-21:00,tue/thu/sat,switch.sauna``.

    Everything after the energy@power spec is recognised by shape rather than
    position -- a time window has a colon, an entity id has a dot, a day list is
    made of day names -- so the trailing fields may appear in any order and any
    of them may be left out. The old positional form (window, then switch, with
    an empty field held open: ``Dishwasher=1.2kWh@2kW,,switch.dishwasher``)
    parses identically under these rules.
    """
    name, _, rest = text.partition("=")
    fields = [part.strip() for part in rest.split(",")]
    spec = fields[0]
    energy_text, _, power_text = spec.partition("@")
    energy = float(energy_text.strip().lower().removesuffix("kwh"))
    power = float(power_text.strip().lower().removesuffix("kw"))
    earliest = latest = None
    switch: str | None = None
    days: frozenset[int] | None = None
    for part in fields[1:]:
        if not part:
            continue
        if _day_intended(part):
            days = parse_days(part)
        elif ":" in part:
            start_text, _, end_text = part.partition("-")
            earliest = _parse_time(start_text)
            latest = _parse_time(end_text)
        elif "." in part:
            switch = part
        else:
            # A typo costs whatever the field was for, never the whole load --
            # the same contract _clean_entity_id applies to a bad switch.
            _LOGGER.warning("%s: unrecognised field %r ignored", name.strip(), part)
    return ShiftableLoad(
        name=name.strip() or "load",
        energy_kwh=energy,
        power_kw=power,
        earliest=earliest,
        latest=latest,
        days=days,
        switch_entity=switch,
    )


def _clean_entity_id(value: Any, load_name: str) -> str | None:
    """Validate an optional entity id, warning rather than failing.

    A typo here should cost the user their appliance automation, not their whole
    load definition -- the placement is still worth publishing so they can run
    the thing by hand.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    domain, _, object_id = text.partition(".")
    if not domain or not object_id or "." in object_id:
        _LOGGER.warning(
            "%s: %r is not an entity id, so the load will be advisory only",
            load_name,
            value,
        )
        return None
    return text


def _parse_time(value: Any) -> time | None:
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {text!r}")
    return time(hour=hour, minute=minute)


def describe_placements(placements: list[LoadPlacement], to_local=None) -> str:
    """A one-line summary for the plan reason.

    ``to_local`` converts to wall-clock time before formatting. Without it the
    line printed UTC, and for a whole British summer "run Sauna at 18:30" meant
    19:30 -- an hour early, on the one line that exists to be acted on.
    """
    if not placements:
        return ""
    convert = to_local or (lambda moment: moment)
    parts = [
        f"{p.name} at {convert(p.start).strftime('%H:%M')}"
        for p in sorted(placements, key=lambda p: p.start)
    ]
    return "run " + ", ".join(parts)


def total_shifted_energy(placements: list[LoadPlacement]) -> float:
    return math.fsum(p.energy_kwh for p in placements)


def next_placement(
    placements: list[LoadPlacement], moment: datetime
) -> LoadPlacement | None:
    """The next placement due to start, ignoring any already under way."""
    upcoming = [p for p in placements if p.start > moment]
    return min(upcoming, key=lambda p: p.start) if upcoming else None


def schedule_advice(placements: list[LoadPlacement], moment: datetime, to_local) -> str:
    """Plain-language instruction for a load you have to start yourself.

    This is the whole feature for anyone without smart appliances: the schedule
    is worth just as much read off a dashboard and acted on by hand.
    """
    if not placements:
        return "nothing scheduled"

    running = sorted((p for p in placements if p.running_at(moment)), key=lambda p: p.end)
    if running:
        names = ", ".join(p.name for p in running)
        until = to_local(max(p.end for p in running)).strftime("%H:%M")
        return f"run {names} now, until {until}"

    upcoming = next_placement(placements, moment)
    if upcoming is None:
        return "all scheduled loads have finished"
    start = to_local(upcoming.start)
    return f"start {upcoming.name} at {start.strftime('%H:%M')}"


def appliance_targets(
    placements: list[LoadPlacement], moment: datetime
) -> dict[str, bool]:
    """Desired on/off state at ``moment`` for each placement that has a switch.

    Two loads may legitimately share one switch -- a single smart plug feeding a
    machine that runs two cycles -- so the states are OR'ed rather than the last
    one winning.
    """
    targets: dict[str, bool] = {}
    for placement in placements:
        entity_id = placement.switch_entity
        if not entity_id:
            continue
        targets[entity_id] = targets.get(entity_id, False) or placement.running_at(moment)
    return targets


def cheapest_action_for(plan: Plan | None, moment: datetime) -> SlotAction | None:
    """The plan's action at ``moment``, for entities that want to show it."""
    if plan is None:
        return None
    slot = plan.slot_at(moment)
    return slot.action if slot else None
