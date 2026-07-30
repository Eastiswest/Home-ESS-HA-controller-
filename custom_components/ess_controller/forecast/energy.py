"""A time series of energy, resampleable onto the planning grid.

Forecast sources disagree about resolution: Solcast publishes half-hourly
average power, Forecast.Solar publishes hourly energy, some template sensors
publish something else entirely. Rather than special-case each one, everything
is normalised into this series and then resampled onto the optimiser's slots by
proportional time overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(slots=True)
class EnergySlot:
    """Energy delivered over a time window, in kWh."""

    start: datetime
    end: datetime
    kwh: float

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


class EnergySeries:
    """Ordered energy windows supporting proportional resampling."""

    def __init__(self, slots: Iterable[EnergySlot] | None = None) -> None:
        self._slots: list[EnergySlot] = sorted(
            (s for s in (slots or []) if s.end > s.start), key=lambda s: s.start
        )

    def __len__(self) -> int:
        return len(self._slots)

    def __bool__(self) -> bool:
        return bool(self._slots)

    def __iter__(self):
        return iter(self._slots)

    @property
    def slots(self) -> list[EnergySlot]:
        return self._slots

    @property
    def start(self) -> datetime | None:
        return self._slots[0].start if self._slots else None

    @property
    def end(self) -> datetime | None:
        return self._slots[-1].end if self._slots else None

    def energy_between(self, start: datetime, end: datetime) -> float:
        """Energy in ``start``..``end``, splitting partially covered windows.

        Assumes energy is spread evenly within each source window, which is the
        best available assumption and exact when the source resolution already
        matches the target grid.
        """
        if end <= start:
            return 0.0
        total = 0.0
        for slot in self._slots:
            if slot.end <= start:
                continue
            if slot.start >= end:
                break
            overlap_start = max(slot.start, start)
            overlap_end = min(slot.end, end)
            overlap = (overlap_end - overlap_start).total_seconds()
            if overlap <= 0:
                continue
            span = (slot.end - slot.start).total_seconds()
            total += slot.kwh * (overlap / span)
        return total

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether the series spans the whole requested window."""
        if not self._slots:
            return False
        return self.start is not None and self.end is not None and (
            self.start <= start and self.end >= end
        )

    def total(self) -> float:
        return sum(s.kwh for s in self._slots)

    def total_between(self, start: datetime, end: datetime) -> float:
        return self.energy_between(start, end)

    def describe(self) -> dict[str, Any]:
        return {
            "windows": len(self._slots),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "total_kwh": round(self.total(), 3),
        }
