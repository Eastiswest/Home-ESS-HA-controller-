"""Tariff abstraction: a supplier-agnostic view of forward prices.

Every provider boils down to the same thing -- a list of half-hourly prices --
so the optimiser never needs to know whether they came from Octopus, a fixed
rate, or a hand-written time-of-use schedule. This is what makes the
integration usable outside the UK without touching the planner.

Prices are always normalised to **minor currency units per kWh** (pence/kWh for
GBP), because that is the unit Agile is quoted in and the unit users recognise.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from ..const import (
    PRICE_SCALE_AUTO,
    PRICE_SCALE_MAJOR,
    SLOT_MINUTES,
)
from ..models import PriceSlot

_LOGGER = logging.getLogger(__name__)

_SLOT = timedelta(minutes=SLOT_MINUTES)

# Rates quoted in major units are all small numbers. A genuine minor-unit
# tariff in pence essentially never sits entirely below this, so the sniff is
# safe in practice -- and it is overridable when it is not.
MAJOR_UNIT_THRESHOLD = 5.0


def detect_scale(values: Sequence[float], mode: str = PRICE_SCALE_AUTO) -> float:
    """Return the multiplier that converts supplier values to minor units."""
    if mode == PRICE_SCALE_MAJOR:
        return 100.0
    if mode != PRICE_SCALE_AUTO:
        return 1.0
    magnitudes = [abs(v) for v in values if v is not None]
    non_zero = [v for v in magnitudes if v > 1e-9]
    if not non_zero:
        return 1.0
    if max(non_zero) < MAJOR_UNIT_THRESHOLD:
        return 100.0
    return 1.0


class PriceSeries:
    """An ordered, de-duplicated set of forward prices."""

    def __init__(self, slots: Iterable[PriceSlot] | None = None) -> None:
        self._slots: list[PriceSlot] = []
        if slots:
            self.merge(slots)

    def __len__(self) -> int:
        return len(self._slots)

    def __bool__(self) -> bool:
        return bool(self._slots)

    def __iter__(self):
        return iter(self._slots)

    @property
    def slots(self) -> list[PriceSlot]:
        return self._slots

    def merge(self, slots: Iterable[PriceSlot]) -> None:
        """Add slots, keeping the series sorted with unique start times.

        Later sources win on conflict, which lets a provider layer a freshly
        published day over a previously extrapolated one.
        """
        by_start: dict[datetime, PriceSlot] = {s.start: s for s in self._slots}
        for slot in slots:
            if slot.end <= slot.start:
                continue
            by_start[slot.start] = slot
        self._slots = sorted(by_start.values(), key=lambda s: s.start)

    @property
    def start(self) -> datetime | None:
        return self._slots[0].start if self._slots else None

    @property
    def end(self) -> datetime | None:
        return self._slots[-1].end if self._slots else None

    def price_at(self, moment: datetime) -> float | None:
        for slot in self._slots:
            if slot.start <= moment < slot.end:
                return slot.price
        return None

    def between(self, start: datetime, end: datetime) -> list[PriceSlot]:
        return [s for s in self._slots if s.end > start and s.start < end]

    def known_until(self, after: datetime) -> datetime | None:
        """End of the contiguous run of prices starting at or before ``after``.

        Agile publishes tomorrow's rates at about 16:00, so for much of the day
        the horizon runs off the end of real data. Knowing exactly where that
        happens is what lets us mark the remainder as extrapolated instead of
        silently pretending it is real.
        """
        cursor: datetime | None = None
        for slot in self._slots:
            if slot.end <= after:
                continue
            if cursor is None:
                if slot.start > after:
                    return after
                cursor = slot.end
                continue
            # Tolerate sub-minute gaps from timestamp rounding.
            if (slot.start - cursor).total_seconds() > 60:
                break
            cursor = slot.end
        return cursor

    def mean(self, start: datetime | None = None, end: datetime | None = None) -> float | None:
        subset = (
            self.between(start, end) if start and end else self._slots
        )
        if not subset:
            return None
        return sum(s.price for s in subset) / len(subset)

    def extend_by_persistence(self, until: datetime, now: datetime) -> int:
        """Fill unknown future slots using the same time of day from history.

        Day-ahead persistence is a weak forecast but an honest one, and it is
        far better than assuming a flat price: it preserves the overnight
        trough and evening peak that dominate the plan. Extrapolated slots are
        flagged ``is_forecast`` so the UI can show where certainty ends.
        """
        if not self._slots:
            return 0
        known_end = self.known_until(now) or self.end
        if known_end is None or known_end >= until:
            return 0

        by_time: dict[tuple[int, int], list[float]] = {}
        for slot in self._slots:
            if slot.is_forecast:
                continue
            by_time.setdefault((slot.start.hour, slot.start.minute), []).append(slot.price)

        overall = self.mean()
        if overall is None:
            return 0

        added: list[PriceSlot] = []
        cursor = known_end
        while cursor < until:
            key = (cursor.hour, cursor.minute)
            samples = by_time.get(key)
            price = samples[-1] if samples else overall
            added.append(
                PriceSlot(
                    start=cursor,
                    end=cursor + _SLOT,
                    price=price,
                    is_forecast=True,
                )
            )
            cursor += _SLOT

        if added:
            self.merge(added)
        return len(added)

    def as_dict_list(self) -> list[dict[str, Any]]:
        return [
            {
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "price": round(s.price, 3),
                "forecast": s.is_forecast,
            }
            for s in self._slots
        ]


class TariffProvider(ABC):
    """Source of forward prices for one direction (import or export)."""

    #: Human-readable name shown in diagnostics.
    name: str = "tariff"

    @abstractmethod
    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        """Return prices covering as much of ``now``..``horizon_end`` as known."""

    async def async_close(self) -> None:
        """Release any resources. Overridden by providers that hold sessions."""
        return None

    @property
    def available(self) -> bool:
        return True

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "available": self.available}


class ZeroTariffProvider(TariffProvider):
    """Always zero: used when the user has no export tariff at all.

    Returning explicit zeros rather than "no data" matters -- it tells the
    optimiser that exported energy is worth nothing, so it will curtail or
    store surplus instead of dumping it to the grid for free.
    """

    name = "none"

    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        slots: list[PriceSlot] = []
        cursor = now.replace(
            minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES, second=0, microsecond=0
        )
        while cursor < horizon_end:
            slots.append(PriceSlot(start=cursor, end=cursor + _SLOT, price=0.0))
            cursor += _SLOT
        return PriceSeries(slots)
