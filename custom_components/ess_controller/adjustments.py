"""Time-windowed price adjustments applied before optimisation.

This is the mechanism behind grid-incentive schemes, and it deliberately needs
no changes to the optimiser at all. Anything that changes what a kWh is worth
during a window becomes an adjustment to the price series, and the existing
cost-minimising plan then does the right thing for the right reason.

Two schemes matter in the UK:

**Free electricity / Power Up sessions** — import is free for a window. Modelled
as an import price override of zero, so the optimiser fills the battery.

**Saving Sessions (National Grid Demand Flexibility Service)** — you are paid
per kWh of *reduction* against a baseline of your typical usage. Modelled as an
uplift to the import price, which is exact rather than approximate: the payment
is ``(baseline - actual) x rate``, and the baseline is fixed and exogenous, so

    maximise (baseline - actual) x rate  ==  minimise actual x rate

which is precisely what adding ``rate`` to the import price does. The constant
baseline term shifts the objective but cannot move the optimum. Exporting also
reduces net import, so the same uplift is applied to the export price.

Everything here is Home Assistant-free so the arithmetic can be tested directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import PriceSlot
from .tariff.base import PriceSeries

_LOGGER = logging.getLogger(__name__)

KIND_SAVING_SESSION = "saving_session"
KIND_FREE_ELECTRICITY = "free_electricity"
KIND_MANUAL = "manual"

# Octopus pays Octoplus rewards in OctoPoints; 8 OctoPoints is 1 penny.
OCTOPOINTS_PER_PENNY = 8.0


@dataclass(slots=True)
class PriceAdjustment:
    """A change to what energy is worth during one window."""

    start: datetime
    end: datetime
    kind: str = KIND_MANUAL
    import_delta: float = 0.0
    """Added to the import price, in minor units per kWh."""
    import_override: float | None = None
    """Replaces the import price outright. Takes precedence over the delta."""
    export_delta: float = 0.0
    export_override: float | None = None
    reason: str = ""

    def covers(self, slot_start: datetime, slot_end: datetime) -> bool:
        """Whether this adjustment applies to a slot.

        A slot counts as covered when its midpoint falls inside the window, so a
        session boundary that lands mid-slot does not half-apply an incentive to
        a slot that is mostly outside it.
        """
        midpoint = slot_start + (slot_end - slot_start) / 2
        return self.start <= midpoint < self.end

    def apply_import(self, price: float) -> float:
        if self.import_override is not None:
            return self.import_override
        return price + self.import_delta

    def apply_export(self, price: float) -> float:
        if self.export_override is not None:
            return self.export_override
        return price + self.export_delta

    def active(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "import_delta": round(self.import_delta, 3),
            "import_override": self.import_override,
            "export_delta": round(self.export_delta, 3),
            "export_override": self.export_override,
            "reason": self.reason,
        }


@dataclass(slots=True)
class AdjustmentResult:
    """Adjusted price series plus a record of what was changed."""

    import_series: PriceSeries
    export_series: PriceSeries
    applied: list[PriceAdjustment] = field(default_factory=list)
    slots_changed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": [a.as_dict() for a in self.applied],
            "slots_changed": self.slots_changed,
        }


def apply_adjustments(
    import_series: PriceSeries,
    export_series: PriceSeries,
    adjustments: list[PriceAdjustment],
) -> AdjustmentResult:
    """Return new price series with ``adjustments`` applied.

    The inputs are not mutated: the caller keeps the unadjusted series so the UI
    can show the real tariff alongside the incentive-adjusted one it planned on.
    Overlapping adjustments compose, with overrides winning over deltas.
    """
    if not adjustments:
        return AdjustmentResult(import_series, export_series)

    applied: set[int] = set()
    changed = 0

    new_import: list[PriceSlot] = []
    for slot in import_series.slots:
        price = slot.price
        touched = False
        for index, adjustment in enumerate(adjustments):
            if adjustment.covers(slot.start, slot.end):
                price = adjustment.apply_import(price)
                applied.add(index)
                touched = True
        if touched:
            changed += 1
        new_import.append(
            PriceSlot(
                start=slot.start,
                end=slot.end,
                price=price,
                is_forecast=slot.is_forecast,
            )
        )

    new_export: list[PriceSlot] = []
    for slot in export_series.slots:
        price = slot.price
        for index, adjustment in enumerate(adjustments):
            if adjustment.covers(slot.start, slot.end):
                price = adjustment.apply_export(price)
                applied.add(index)
        new_export.append(
            PriceSlot(
                start=slot.start,
                end=slot.end,
                price=price,
                is_forecast=slot.is_forecast,
            )
        )

    return AdjustmentResult(
        import_series=PriceSeries(new_import),
        export_series=PriceSeries(new_export),
        applied=[adjustments[i] for i in sorted(applied)],
        slots_changed=changed,
    )


# ---------------------------------------------------------------------------
# Session events
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SessionEvent:
    """A supplier incentive window."""

    start: datetime
    end: datetime
    kind: str
    rate: float | None = None
    """Reward in minor units per kWh, when the supplier publishes one."""
    joined: bool = False
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rate": self.rate,
            "joined": self.joined,
            "source": self.source,
        }


# Attribute names carrying event lists. The Octopus Energy integration publishes
# "events" plus separate available/joined lists depending on the entity.
EVENT_LIST_ATTRIBUTES: tuple[str, ...] = (
    "joined_events",
    "events",
    "available_events",
    "sessions",
    "upcoming_events",
)
EVENT_START_KEYS: tuple[str, ...] = ("start", "start_at", "valid_from", "from")
EVENT_END_KEYS: tuple[str, ...] = ("end", "end_at", "valid_to", "to")
# Rewards may be published in OctoPoints per kWh or already in pence.
OCTOPOINT_KEYS: tuple[str, ...] = (
    "octopoints_per_kwh",
    "octopoints",
    "rewarded_octopoints",
)
RATE_KEYS: tuple[str, ...] = ("rate", "price", "reward_per_kwh", "pence_per_kwh")


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick(mapping: Any, keys: tuple[str, ...]) -> Any:
    if not hasattr(mapping, "get"):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def parse_session_events(
    attributes: Any,
    kind: str,
    default_rate: float | None = None,
) -> list[SessionEvent]:
    """Extract incentive windows from an entity's attributes.

    Tolerant by design: the Octopus integration has changed these attribute
    names across versions, and a session that fails to parse means a missed
    payment rather than a crash.
    """
    events: list[SessionEvent] = []
    if not hasattr(attributes, "get"):
        return events

    for name in EVENT_LIST_ATTRIBUTES:
        raw = attributes.get(name)
        if not isinstance(raw, list):
            continue
        joined = name == "joined_events"
        for entry in raw:
            start = _parse_dt(_pick(entry, EVENT_START_KEYS))
            end = _parse_dt(_pick(entry, EVENT_END_KEYS))
            if start is None or end is None or end <= start:
                continue

            rate = None
            points = _pick(entry, OCTOPOINT_KEYS)
            if points is not None:
                try:
                    rate = float(points) / OCTOPOINTS_PER_PENNY
                except (TypeError, ValueError):
                    rate = None
            if rate is None:
                direct = _pick(entry, RATE_KEYS)
                if direct is not None:
                    try:
                        rate = float(direct)
                    except (TypeError, ValueError):
                        rate = None
            if rate is None:
                rate = default_rate

            # An entry that explicitly flags participation wins over the
            # attribute name it arrived under.
            explicit = entry.get("joined") if hasattr(entry, "get") else None
            events.append(
                SessionEvent(
                    start=start,
                    end=end,
                    kind=kind,
                    rate=rate,
                    joined=bool(explicit) if explicit is not None else joined,
                    source=name,
                )
            )

    return _dedupe_events(events)


def _dedupe_events(events: list[SessionEvent]) -> list[SessionEvent]:
    """Collapse duplicates, preferring the joined copy of the same window."""
    by_window: dict[tuple[datetime, datetime], SessionEvent] = {}
    for event in events:
        key = (event.start, event.end)
        existing = by_window.get(key)
        if existing is None:
            by_window[key] = event
            continue
        # Keep whichever tells us more: joined status, then a published rate.
        if (event.joined and not existing.joined) or (
            event.rate is not None and existing.rate is None
        ):
            by_window[key] = event
    return sorted(by_window.values(), key=lambda e: e.start)


def adjustments_from_sessions(
    events: list[SessionEvent],
    only_joined: bool = True,
    saving_session_rate: float = 0.0,
    reward_export: bool = True,
) -> list[PriceAdjustment]:
    """Convert incentive windows into price adjustments.

    ``only_joined`` defaults to true: acting on a Saving Session you have not
    opted into earns nothing, and would mean discharging the battery hard for no
    reward.
    """
    result: list[PriceAdjustment] = []
    for event in events:
        if event.kind == KIND_FREE_ELECTRICITY:
            # Free import. Not something you opt into, so participation is moot.
            result.append(
                PriceAdjustment(
                    start=event.start,
                    end=event.end,
                    kind=event.kind,
                    import_override=0.0,
                    reason="free electricity session",
                )
            )
            continue

        if event.kind == KIND_SAVING_SESSION:
            if only_joined and not event.joined:
                _LOGGER.debug(
                    "Ignoring saving session at %s: not joined", event.start.isoformat()
                )
                continue
            rate = event.rate if event.rate is not None else saving_session_rate
            if rate <= 0:
                _LOGGER.debug(
                    "Ignoring saving session at %s: no reward rate known",
                    event.start.isoformat(),
                )
                continue
            result.append(
                PriceAdjustment(
                    start=event.start,
                    end=event.end,
                    kind=event.kind,
                    import_delta=rate,
                    export_delta=rate if reward_export else 0.0,
                    reason=f"saving session at {rate:.0f}p/kWh reduction",
                )
            )
    return result


def active_session(
    events: list[SessionEvent], moment: datetime, kind: str | None = None
) -> SessionEvent | None:
    for event in events:
        if kind is not None and event.kind != kind:
            continue
        if event.start <= moment < event.end:
            return event
    return None


def next_session(
    events: list[SessionEvent], moment: datetime, kind: str | None = None
) -> SessionEvent | None:
    upcoming = [
        e for e in events if e.start > moment and (kind is None or e.kind == kind)
    ]
    return min(upcoming, key=lambda e: e.start) if upcoming else None
