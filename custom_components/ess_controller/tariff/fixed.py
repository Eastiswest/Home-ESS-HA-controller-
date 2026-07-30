"""Fixed-rate and time-of-use tariff providers.

These need no network access and no other integration, so they are the fallback
that guarantees the controller can always plan something. They also cover the
majority of non-UK setups (a flat rate, or a simple day/night split).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, tzinfo
from typing import Any

from ..const import SLOT_MINUTES
from ..models import PriceSlot
from .base import PriceSeries, TariffProvider

_LOGGER = logging.getLogger(__name__)

_SLOT = timedelta(minutes=SLOT_MINUTES)


def _floor_slot(moment: datetime) -> datetime:
    return moment.replace(
        minute=(moment.minute // SLOT_MINUTES) * SLOT_MINUTES, second=0, microsecond=0
    )


class FixedTariffProvider(TariffProvider):
    """A single flat rate for the whole horizon."""

    name = "fixed"

    def __init__(self, rate: float) -> None:
        self._rate = float(rate)

    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        slots: list[PriceSlot] = []
        cursor = _floor_slot(now)
        while cursor < horizon_end:
            slots.append(PriceSlot(start=cursor, end=cursor + _SLOT, price=self._rate))
            cursor += _SLOT
        return PriceSeries(slots)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "rate": self._rate}


def parse_tou_schedule(raw: Any) -> list[tuple[time, time, float]]:
    """Parse a time-of-use schedule into ordered (start, end, rate) windows.

    Accepts either a list of mappings::

        [{"start": "00:30", "end": "04:30", "rate": 9.5}, ...]

    or the compact string form ``"00:30-04:30=9.5,04:30-00:30=28.0"`` so the
    schedule can be typed into a single config-flow text field.

    Windows that wrap past midnight are supported. Any part of the day left
    uncovered falls back to the caller's default rate.
    """
    windows: list[tuple[time, time, float]] = []
    if not raw:
        return windows

    entries: list[Any]
    if isinstance(raw, str):
        entries = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    elif isinstance(raw, list):
        entries = raw
    else:
        _LOGGER.warning("Unrecognised time-of-use schedule: %r", raw)
        return windows

    for entry in entries:
        try:
            if isinstance(entry, str):
                window, _, rate_text = entry.partition("=")
                start_text, _, end_text = window.partition("-")
                rate = float(rate_text)
            elif isinstance(entry, dict):
                start_text = str(entry["start"])
                end_text = str(entry["end"])
                rate = float(entry["rate"])
            else:
                continue
            windows.append((_parse_time(start_text), _parse_time(end_text), rate))
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning("Skipping bad time-of-use entry %r: %s", entry, err)
    return windows


def _parse_time(text: str) -> time:
    text = text.strip()
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time out of range: {text!r}")
    return time(hour=hour, minute=minute)


def _in_window(moment: time, start: time, end: time) -> bool:
    if start == end:
        return True  # a zero-length window is treated as all day
    if start < end:
        return start <= moment < end
    # Wraps midnight.
    return moment >= start or moment < end


class TimeOfUseTariffProvider(TariffProvider):
    """A repeating daily schedule of rates, e.g. Economy 7 or a day/night split.

    ``tz`` must be the user's local timezone. Tariff windows are quoted in local
    wall-clock time, so evaluating them against UTC would silently shift every
    window by an hour for half the year.
    """

    name = "tou"

    def __init__(self, schedule: Any, default_rate: float, tz: tzinfo | None = None) -> None:
        self._windows = parse_tou_schedule(schedule)
        self._default = float(default_rate)
        self._tz = tz
        if not self._windows:
            _LOGGER.warning(
                "Time-of-use schedule is empty; falling back to flat %.2f", self._default
            )

    def rate_at(self, moment: datetime) -> float:
        local = (
            moment.astimezone(self._tz)
            if self._tz is not None and moment.tzinfo is not None
            else moment
        )
        clock = local.time()
        for start, end, rate in self._windows:
            if _in_window(clock, start, end):
                return rate
        return self._default

    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        slots: list[PriceSlot] = []
        cursor = _floor_slot(now)
        while cursor < horizon_end:
            slots.append(
                PriceSlot(start=cursor, end=cursor + _SLOT, price=self.rate_at(cursor))
            )
            cursor += _SLOT
        return PriceSeries(slots)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "default_rate": self._default,
            "windows": [
                {"start": s.isoformat(), "end": e.isoformat(), "rate": r}
                for s, e, r in self._windows
            ],
        }
