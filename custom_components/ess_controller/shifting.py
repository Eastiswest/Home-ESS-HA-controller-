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

    def __post_init__(self) -> None:
        if self.energy_kwh <= 0:
            raise ValueError(f"{self.name}: energy_kwh must be positive")
        if self.power_kw <= 0:
            raise ValueError(f"{self.name}: power_kw must be positive")

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
        entries = [chunk.strip() for chunk in raw.split(";") if chunk.strip()]
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


def _parse_mapping(entry: Any) -> ShiftableLoad:
    return ShiftableLoad(
        name=str(entry["name"]),
        energy_kwh=float(entry["energy_kwh"]),
        power_kw=float(entry["power_kw"]),
        earliest=_parse_time(entry.get("earliest")),
        latest=_parse_time(entry.get("latest")),
        enabled=bool(entry.get("enabled", True)),
        must_run_daily=bool(entry.get("must_run_daily", True)),
    )


def _parse_compact(text: str) -> ShiftableLoad:
    name, _, rest = text.partition("=")
    spec, _, window = rest.partition(",")
    energy_text, _, power_text = spec.partition("@")
    energy = float(energy_text.strip().lower().removesuffix("kwh"))
    power = float(power_text.strip().lower().removesuffix("kw"))
    earliest = latest = None
    if window.strip():
        start_text, _, end_text = window.partition("-")
        earliest = _parse_time(start_text)
        latest = _parse_time(end_text)
    return ShiftableLoad(
        name=name.strip() or "load",
        energy_kwh=energy,
        power_kw=power,
        earliest=earliest,
        latest=latest,
    )


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


def describe_placements(placements: list[LoadPlacement]) -> str:
    """A one-line summary for the plan reason."""
    if not placements:
        return ""
    parts = [
        f"{p.name} at {p.start.strftime('%H:%M')}"
        for p in sorted(placements, key=lambda p: p.start)
    ]
    return "run " + ", ".join(parts)


def total_shifted_energy(placements: list[LoadPlacement]) -> float:
    return math.fsum(p.energy_kwh for p in placements)


def cheapest_action_for(plan: Plan | None, moment: datetime) -> SlotAction | None:
    """The plan's action at ``moment``, for entities that want to show it."""
    if plan is None:
        return None
    slot = plan.slot_at(moment)
    return slot.action if slot else None
