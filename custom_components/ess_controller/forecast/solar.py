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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..learning.model import LearningModel
from .clearsky import clear_sky_kwh
from .energy import EnergySeries, EnergySlot
from .weather import WeatherSeries

_LOGGER = logging.getLogger(__name__)

# Below this a day's estimate is effectively zero, and scaling it would amplify
# rounding noise into a forecast.
EPS_KWH = 0.01

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
    for (moment, value, is_power), period in zip(staged, periods, strict=True):
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
    for (moment, value), period in zip(staged, periods, strict=True):
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


# Object-ID fragments naming the day a daily-total forecast sensor refers to.
#
# Forecast.Solar publishes "Estimated energy production - today/tomorrow" as plain
# daily totals with no hourly breakdown, and those are the sensors a person naturally
# reaches for. They carry no timestamps, so the day has to come from the name.
DAILY_TOTAL_DAY_OFFSETS: tuple[tuple[str, int], ...] = (
    ("today", 0),
    ("tomorrow", 1),
    ("day_after_tomorrow", 2),
    ("d3", 2),
    ("d4", 3),
)


def daily_total_offset(object_id: str) -> int | None:
    """Which day a daily-total sensor is about, or None if it does not say.

    Longest fragment first, so "day_after_tomorrow" is not read as "tomorrow".
    """
    text = str(object_id).lower()
    for fragment, offset in sorted(
        DAILY_TOTAL_DAY_OFFSETS, key=lambda pair: -len(pair[0])
    ):
        if fragment in text:
            return offset
    return None


# Once this much of a day's predicted generation comes from trusted learned
# buckets, the day is no longer rescaled to an external daily total. A majority
# rather than a single bucket, so one lucky half-hour cannot take the whole day
# off the forecast that is otherwise carrying it.
LEARNED_DAY_SHARE = 0.5


def _is_trusted_bucket(source: str) -> bool:
    """Whether a prediction came from a learned bucket with real evidence behind it.

    Everything else -- a clear-sky estimate, a bare default, or the single-sample
    fallback that ``lookup`` marks with a trailing ``~`` -- is a guess, and a
    guess must not be allowed to outrank an external forecast.
    """
    if not source or source.endswith("~"):
        return False
    return source not in ("default", "none") and not source.startswith("clearsky")


def _scale_to_daily_totals(
    predictions: list[SolarPrediction],
    local_dates: list[date],
    daily_totals: dict[date, float] | None,
) -> None:
    """Rescale a day's geometric estimate so it sums to a forecast day total.

    Applied in place, and only to days lying *wholly* within the horizon. A daily
    total describes a whole day including the hours already gone, so scaling a partial
    day by it would inflate what remains -- on a horizon starting at 16:30, today's
    total would be crammed into the last of the daylight. The first and last dates are
    therefore left alone, which on a 48-hour horizon still leaves tomorrow: the day
    that decides whether to buy into today's cheap window.

    Only estimates the model derived itself are touched. Where an hourly forecast was
    available it is already better than a total.

    ...and so, eventually, is the learned model. This ran last and unconditionally,
    which meant a house that had spent a fortnight teaching the model what its
    August afternoons produce still had every whole day in the horizon crushed back
    to whatever the daily-total sensor said. On a real install that sensor was 40%
    low, every daylight half-hour, and no amount of learning could ever show through
    -- the correction the model exists to make was overwritten immediately after
    being made. Once most of a day's generation is predicted from trusted buckets,
    the house has taught the model more about that half-hour at that cloud level
    than a whole-day number can say, and the day is left alone.
    """
    if not daily_totals or not predictions:
        return
    whole_days = set(local_dates[1:-1]) - {local_dates[0], local_dates[-1]}
    for day in whole_days:
        target = daily_totals.get(day)
        if target is None or target < 0:
            continue
        indices = [
            i
            for i, (d, p) in enumerate(zip(local_dates, predictions, strict=True))
            if d == day and p.external_kwh is None
        ]
        estimated = sum(predictions[i].kwh for i in indices)
        if not indices or estimated <= EPS_KWH:
            continue
        learned = sum(predictions[i].kwh for i in indices if predictions[i].learned)
        if learned >= estimated * LEARNED_DAY_SHARE:
            continue
        factor = target / estimated
        for i in indices:
            current = predictions[i]
            predictions[i] = SolarPrediction(
                kwh=current.kwh * factor,
                source=f"{current.source}+daily",
                external_kwh=current.external_kwh,
                cloud=current.cloud,
                uv_index=current.uv_index,
            )


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
    uv_index: float | None = None
    learned: bool = False
    """True when a *trusted* learned bucket supplied this, not a fallback.

    A single-sample bucket, a clear-sky estimate and a bare default are all
    guesses of one kind or another. This marks the case where the house has
    actually taught the model what this half-hour does at this cloud level, which
    is the only case where the learned answer should outrank an external one.
    """


class SolarForecaster:
    """Predicts PV energy per planning slot."""

    def __init__(
        self,
        model: LearningModel,
        peak_power_kw: float,
        use_learning: bool = True,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> None:
        self._model = model
        self._peak_kw = max(peak_power_kw, 0.0)
        self._use_learning = use_learning
        # Needed only for the day-one clear-sky estimate. Absent means no
        # estimate, which is the old behaviour of predicting nothing.
        self._latitude = latitude
        self._longitude = longitude

    def predict_slot(
        self,
        local_start: datetime,
        duration_hours: float,
        external_kwh: float | None = None,
        cloud: float | None = None,
        uv_index: float | None = None,
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
                uv_index=uv_index,
            )

        kwh, source = self._model.predict_solar(
            month=local_start.month,
            hour=local_start.hour,
            minute=local_start.minute,
            cloud=cloud,
            forecast_kwh=external_kwh,
            # Negative marks "the model had nothing", which is different from a
            # learned zero at midnight and needs a different answer.
            default_kwh=-1.0,
            uv_index=uv_index,
        )
        if source == "default" or kwh < 0.0:
            kwh, source = self._day_one(local_start, duration_hours, cloud)
        return SolarPrediction(
            kwh=self._clamp(kwh, duration_hours),
            source=source,
            external_kwh=external_kwh,
            cloud=cloud,
            uv_index=uv_index,
            learned=_is_trusted_bucket(source),
        )

    def _day_one(
        self, local_start: datetime, duration_hours: float, cloud: float | None
    ) -> tuple[float, str]:
        """What to predict before there is a forecast or any history.

        Zero was the old answer, and it is the one answer guaranteed to be wrong:
        the optimiser plans as though the sun will not rise, buys the whole day
        from the grid, and the user drives the battery by hand until the model
        catches up. A clear-sky estimate is crude but it is the right order of
        magnitude, and the learned correction supersedes it within days.
        """
        if self._latitude is None or self._longitude is None:
            return 0.0, "none"
        kwh = clear_sky_kwh(
            self._latitude,
            self._longitude,
            local_start,
            duration_hours,
            self._peak_kw,
            cloud,
        )
        return kwh, "clearsky" if cloud is None else "clearsky+cloud"

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
        daily_totals: dict[date, float] | None = None,
    ) -> list[SolarPrediction]:
        """Predict every slot of a horizon.

        ``to_local`` converts a planning timestamp (UTC) into local time.

        ``daily_totals`` maps a local date to a forecast day's total generation, for
        the sensors that publish only that. Forecast.Solar's "Estimated energy
        production - today/tomorrow" are exactly this, and they are the sensors a
        person naturally reaches for: before this they parsed to nothing and the
        estimate fell back silently to bare geometry, which on a real install put a
        whole afternoon at 2 kWh while the array was filling the battery.
        """
        predictions: list[SolarPrediction] = []
        has_external = bool(external)
        local_dates: list[date] = [to_local(start).date() for start, _ in slots]
        for start, end in slots:
            duration = (end - start).total_seconds() / 3600.0
            external_kwh = external.energy_between(start, end) if has_external else None
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
            # Prefer numeric cloud cover; fall back to UV index for providers
            # that publish it instead (Met Office), and only then to a
            # condition-derived cloud estimate.
            cloud = weather.measured_cloud_at(start) if weather else None
            uv_index = weather.uv_index_at(start) if weather else None
            if cloud is None and uv_index is None and weather is not None:
                cloud = weather.cloud_at(start)
            predictions.append(
                self.predict_slot(
                    to_local(start), duration, external_kwh, cloud, uv_index
                )
            )
        _scale_to_daily_totals(predictions, local_dates, daily_totals)
        return predictions

    def describe(self) -> dict[str, Any]:
        return {
            "peak_power_kw": self._peak_kw,
            "learning_enabled": self._use_learning,
            "clearsky_available": self._latitude is not None,
            "solar_buckets": len(self._model.solar_absolute),
            "ratio_buckets": len(self._model.solar_ratio),
        }
