"""Weather forecast series used to condition the solar and load models.

Cloud cover drives PV output; outdoor temperature drives A/C and heating load.
Both arrive from the same hourly weather forecast, so they are modelled together
here as a simple interpolatable series.

This module is Home Assistant-free: the coordinator fetches the raw forecast
list and hands it in, which keeps the interpolation logic testable.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Keys used by HA weather forecast entries.
TIME_KEYS: tuple[str, ...] = ("datetime", "time", "period_start", "start")
CLOUD_KEYS: tuple[str, ...] = ("cloud_coverage", "cloud_cover", "clouds", "cloudiness")
TEMP_KEYS: tuple[str, ...] = ("temperature", "temp", "native_temperature")

# Weather conditions mapped to an approximate cloud cover, used when a provider
# reports a condition but no numeric cloud coverage (Met Office, some others).
CONDITION_CLOUD: dict[str, float] = {
    "sunny": 5.0,
    "clear": 5.0,
    "clear-night": 5.0,
    "windy": 20.0,
    "partlycloudy": 45.0,
    "partly-cloudy": 45.0,
    "cloudy": 85.0,
    "windy-variant": 70.0,
    "fog": 90.0,
    "hail": 95.0,
    "lightning": 90.0,
    "lightning-rainy": 95.0,
    "pouring": 98.0,
    "rainy": 92.0,
    "snowy": 95.0,
    "snowy-rainy": 95.0,
    "exceptional": 50.0,
}


def _pick(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class WeatherPoint:
    """One point of a weather forecast."""

    time: datetime
    temperature: float | None = None
    cloud_coverage: float | None = None
    condition: str | None = None

    @property
    def effective_cloud(self) -> float | None:
        """Cloud cover, falling back to an estimate from the condition."""
        if self.cloud_coverage is not None:
            return self.cloud_coverage
        if self.condition:
            return CONDITION_CLOUD.get(str(self.condition).lower())
        return None


class WeatherSeries:
    """An interpolatable weather forecast.

    Forecasts are usually hourly while planning is half-hourly, so values are
    linearly interpolated between points rather than snapped, which keeps a
    warming afternoon from stepping in 1-hour jumps.
    """

    def __init__(self, points: Iterable[WeatherPoint] | None = None) -> None:
        self._points: list[WeatherPoint] = sorted(
            (p for p in (points or []) if p.time is not None), key=lambda p: p.time
        )
        self._times = [p.time for p in self._points]

    def __len__(self) -> int:
        return len(self._points)

    def __bool__(self) -> bool:
        return bool(self._points)

    @property
    def points(self) -> list[WeatherPoint]:
        return self._points

    @property
    def start(self) -> datetime | None:
        return self._times[0] if self._times else None

    @property
    def end(self) -> datetime | None:
        return self._times[-1] if self._times else None

    @classmethod
    def from_forecast(cls, entries: Iterable[Mapping[str, Any]] | None) -> WeatherSeries:
        """Build from an HA ``weather.get_forecasts`` response list."""
        points: list[WeatherPoint] = []
        for entry in entries or []:
            if not isinstance(entry, Mapping):
                continue
            moment = _parse_dt(_pick(entry, TIME_KEYS))
            if moment is None:
                continue
            cloud = _pick(entry, CLOUD_KEYS)
            temp = _pick(entry, TEMP_KEYS)
            points.append(
                WeatherPoint(
                    time=moment,
                    temperature=_as_float(temp),
                    cloud_coverage=_as_float(cloud),
                    condition=entry.get("condition"),
                )
            )
        return cls(points)

    def _interpolate(self, moment: datetime, attribute: str) -> float | None:
        if not self._points:
            return None

        def value_of(point: WeatherPoint) -> float | None:
            if attribute == "cloud":
                return point.effective_cloud
            return point.temperature

        if moment <= self._times[0]:
            return value_of(self._points[0])
        if moment >= self._times[-1]:
            return value_of(self._points[-1])

        index = bisect_left(self._times, moment)
        after = self._points[index]
        before = self._points[index - 1]
        low = value_of(before)
        high = value_of(after)
        if low is None:
            return high
        if high is None:
            return low
        span = (after.time - before.time).total_seconds()
        if span <= 0:
            return high
        fraction = (moment - before.time).total_seconds() / span
        return low + (high - low) * fraction

    def cloud_at(self, moment: datetime) -> float | None:
        value = self._interpolate(moment, "cloud")
        if value is None:
            return None
        return min(max(value, 0.0), 100.0)

    def temperature_at(self, moment: datetime) -> float | None:
        return self._interpolate(moment, "temperature")

    def condition_at(self, moment: datetime) -> str | None:
        """Nearest reported condition (conditions cannot be interpolated)."""
        if not self._points:
            return None
        if moment <= self._times[0]:
            return self._points[0].condition
        if moment >= self._times[-1]:
            return self._points[-1].condition
        index = bisect_left(self._times, moment)
        after = self._points[index]
        before = self._points[index - 1]
        if (moment - before.time) <= (after.time - moment):
            return before.condition
        return after.condition

    def max_temperature(self) -> float | None:
        temps = [p.temperature for p in self._points if p.temperature is not None]
        return max(temps) if temps else None

    def describe(self) -> dict[str, Any]:
        return {
            "points": len(self._points),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "max_temperature": self.max_temperature(),
        }


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
