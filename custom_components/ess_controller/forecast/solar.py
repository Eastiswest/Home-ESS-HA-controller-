"""Solar generation forecasting.

Two modes, chosen automatically:

* **Corrected forecast** -- when a forecast integration (Solcast,
  Forecast.Solar, or anything publishing a recognisable attribute) is
  configured, its shape is trusted and a learned multiplier per
  season/time/cloud bucket is applied. The multiplier absorbs everything a
  generic forecast cannot know about a specific installation: shading from a
  tree or chimney, panel soiling, a mis-declared azimuth, inverter clipping.

* **Learned absolute** -- with no forecast source at all, the learned history
  answers directly: "at this time of year, at this hour, under this much cloud,
  you generated this much".

Either way the answer is clamped to what the array can physically produce, so a
bad forecast or a polluted bucket cannot hand the optimiser an impossible number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from ..learning.model import LearningModel
from .energy import EnergySeries, EnergySlot
from .weather import WeatherSeries

_LOGGER = logging.getLogger(__name__)

# Solcast-style: list of half-hourly average power in kW.
POWER_LIST_ATTRIBUTES: tuple[str, ...] = (
    "detailedForecast",
    "detailedHourly",
    "forecasts",
    "forecast",
)
POWER_LIST_TIME_KEYS: tuple[str, ...] = (
    "period_start",
    "period_end",
    "datetime",
    "start",
    "time",
)
POWER_LIST_VALUE_KEYS: tuple[str, ...] = (
    "pv_estimate",
    "pv_power",
    "power_kw",
    "estimate",
)
ENERGY_LIST_VALUE_KEYS: tuple[str, ...] = ("pv_energy", "wh", "energy", "kwh")

# Forecast.Solar-style: mapping of ISO timestamp to a value.
WH_PERIOD_ATTRIBUTES: tuple[str, ...] = ("watt_hours_period", "wh_period")
WATTS_ATTRIBUTES: tuple[str, ...] = ("watts", "watt")

DEFAULT_PERIOD = timedelta(minutes=30)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _infer_periods(times: list[datetime]) -> list[timedelta]:
    """Work out each entry's window length from the gaps between entries."""
    if not times:
        return []
    if len(times) == 1:
        return [DEFAULT_PERIOD]
    periods: list[timedelta] = []
    for index, moment in enumerate(times):
        if index + 1 < len(times):
            gap = times[index + 1] - moment
        else:
            gap = times[-1] - times[-2]
        if gap <= timedelta(0) or gap > timedelta(hours=6):
            gap = DEFAULT_PERIOD
        periods.append(gap)
    return periods


def parse_power_list(entries: Iterable[Mapping[str, Any]]) -> list[EnergySlot]:
    """Parse a Solcast-style list of average power (kW) per period."""
    staged: list[tuple[datetime, float, bool]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        moment = _parse_dt(_pick(entry, POWER_LIST_TIME_KEYS))
        if moment is None:
            continue
        power = _as_float(_pick(entry, POWER_LIST_VALUE_KEYS))
        if power is not None:
            staged.append((moment, power, True))
            continue
        energy = _as_float(_pick(entry, ENERGY_LIST_VALUE_KEYS))
        if energy is not None:
            staged.append((moment, energy, False))

    if not staged:
        return []
    staged.sort(key=lambda item: item[0])
    times = [item[0] for item in staged]
    periods = _infer_periods(times)

    slots: list[EnergySlot] = []
    for (moment, value, is_power), period in zip(staged, periods):
        hours = period.total_seconds() / 3600.0
        kwh = max(value, 0.0) * hours if is_power else max(value, 0.0)
        slots.append(EnergySlot(start=moment, end=moment + period, kwh=kwh))
    return slots


def parse_wh_mapping(mapping: Mapping[str, Any], as_power: bool) -> list[EnergySlot]:
    """Parse a Forecast.Solar-style ``{timestamp: value}`` mapping.

    ``as_power`` distinguishes ``watts`` (instantaneous power in W) from
    ``watt_hours_period`` (energy in Wh already attributed to the period).
    """
    staged: list[tuple[datetime, float]] = []
    for key, value in mapping.items():
        moment = _parse_dt(key)
        number = _as_float(value)
        if moment is None or number is None:
            continue
        staged.append((moment, number))
    if not staged:
        return []
    staged.sort(key=lambda item: item[0])
    times = [item[0] for item in staged]
    periods = _infer_periods(times)

    slots: list[EnergySlot] = []
    for (moment, value), period in zip(staged, periods):
        hours = period.total_seconds() / 3600.0
        # Both variants are in watts / watt-hours, hence the /1000.
        kwh = (max(value, 0.0) / 1000.0) * (hours if as_power else 1.0)
        slots.append(EnergySlot(start=moment, end=moment + period, kwh=kwh))
    return slots


def parse_solar_forecast_attributes(attributes: Mapping[str, Any]) -> list[EnergySlot]:
    """Extract a forecast from one entity's attributes, whatever the format."""
    for name in POWER_LIST_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, list) and value:
            slots = parse_power_list(value)
            if slots:
                return slots

    for name in WH_PERIOD_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, Mapping) and value:
            slots = parse_wh_mapping(value, as_power=False)
            if slots:
                return slots

    for name in WATTS_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, Mapping) and value:
            slots = parse_wh_mapping(value, as_power=True)
            if slots:
                return slots
    return []


def build_forecast_series(
    attribute_sets: Iterable[Mapping[str, Any]],
) -> EnergySeries:
    """Merge forecasts from several entities (e.g. today + tomorrow sensors)."""
    by_start: dict[datetime, EnergySlot] = {}
    for attributes in attribute_sets:
        for slot in parse_solar_forecast_attributes(attributes):
            by_start[slot.start] = slot
    return EnergySeries(by_start.values())


@dataclass(slots=True)
class SolarPrediction:
    """A per-slot solar prediction with provenance."""

    kwh: float
    source: str
    external_kwh: float | None = None
    cloud: float | None = None


class SolarForecaster:
    """Predicts PV energy per planning slot."""

    def __init__(
        self,
        model: LearningModel,
        peak_power_kw: float,
        use_learning: bool = True,
    ) -> None:
        self._model = model
        self._peak_kw = max(peak_power_kw, 0.0)
        self._use_learning = use_learning

    def predict_slot(
        self,
        local_start: datetime,
        duration_hours: float,
        external_kwh: float | None = None,
        cloud: float | None = None,
    ) -> SolarPrediction:
        """Predict one slot. ``local_start`` must be in the user's timezone.

        Local time matters: solar output follows the sun, so binning by UTC hour
        would smear the learned curve by an hour every summer.
        """
        if not self._use_learning:
            value = external_kwh if external_kwh is not None else 0.0
            return SolarPrediction(
                kwh=self._clamp(value, duration_hours),
                source="forecast",
                external_kwh=external_kwh,
                cloud=cloud,
            )

        kwh, source = self._model.predict_solar(
            month=local_start.month,
            hour=local_start.hour,
            minute=local_start.minute,
            cloud=cloud,
            forecast_kwh=external_kwh,
            default_kwh=0.0,
        )
        return SolarPrediction(
            kwh=self._clamp(kwh, duration_hours),
            source=source,
            external_kwh=external_kwh,
            cloud=cloud,
        )

    def _clamp(self, kwh: float, duration_hours: float) -> float:
        """No prediction may exceed what the array can physically produce."""
        if self._peak_kw <= 0:
            return max(kwh, 0.0)
        ceiling = self._peak_kw * duration_hours
        return min(max(kwh, 0.0), ceiling)

    def predict_series(
        self,
        slots: list[tuple[datetime, datetime]],
        external: EnergySeries | None,
        weather: WeatherSeries | None,
        to_local,
    ) -> list[SolarPrediction]:
        """Predict every slot of a horizon.

        ``to_local`` converts a planning timestamp (UTC) into local time.
        """
        predictions: list[SolarPrediction] = []
        has_external = bool(external)
        for start, end in slots:
            duration = (end - start).total_seconds() / 3600.0
            external_kwh = (
                external.energy_between(start, end) if has_external else None
            )
            # Outside the forecast's coverage, fall back to the learned model
            # rather than trusting an implicit zero.
            if (
                has_external
                and external is not None
                and not external.covers(start, end)
                and external_kwh is not None
                and external_kwh <= 0.0
            ):
                external_kwh = None
            cloud = weather.cloud_at(start) if weather else None
            predictions.append(
                self.predict_slot(to_local(start), duration, external_kwh, cloud)
            )
        return predictions

    def describe(self) -> dict[str, Any]:
        return {
            "peak_power_kw": self._peak_kw,
            "learning_enabled": self._use_learning,
            "solar_buckets": len(self._model.solar_absolute),
            "ratio_buckets": len(self._model.solar_ratio),
        }
