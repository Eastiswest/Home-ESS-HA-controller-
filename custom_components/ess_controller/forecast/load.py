"""Household load forecasting.

Load is predicted from learned history keyed by day-type, half-hour slot and
outdoor temperature bucket. Temperature binning is what makes A/C visible to the
planner: once a few hot afternoons have been observed, the 25-30 C bucket for
14:00 carries a much higher mean than the 15-20 C bucket, so a forecast hot day
automatically produces a bigger overnight charge.

Until enough history exists, two fallbacks fill the gap:

1. A typical domestic diurnal shape scaled to the user's declared daily
   consumption -- overnight trough, morning rise, evening peak. Far better than
   assuming flat load, which would badly misprice the evening peak.
2. An optional explicit temperature uplift, for users who want sensible
   behaviour on day one of a heatwave rather than waiting to learn it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..learning.model import LearningModel

_LOGGER = logging.getLogger(__name__)

# Relative weights for each half-hour of the day, starting at 00:00. Shaped on
# a typical domestic profile: quiet overnight, morning peak, evening maximum.
DIURNAL_WEIGHTS: tuple[float, ...] = (
    # 00:00-06:00
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    0.5,
    # 06:00-08:00
    1.0,
    1.0,
    1.0,
    1.0,
    # 08:00-09:00
    1.2,
    1.2,
    # 09:00-12:00
    0.9,
    0.9,
    0.9,
    0.9,
    0.9,
    0.9,
    # 12:00-14:00
    1.0,
    1.0,
    1.0,
    1.0,
    # 14:00-17:00
    0.9,
    0.9,
    0.9,
    0.9,
    0.9,
    0.9,
    # 17:00-19:00
    1.8,
    1.8,
    1.8,
    1.8,
    # 19:00-21:00
    1.7,
    1.7,
    1.7,
    1.7,
    # 21:00-23:00
    1.2,
    1.2,
    1.2,
    1.2,
    # 23:00-24:00
    0.8,
    0.8,
)

_WEIGHT_SUM = sum(DIURNAL_WEIGHTS)


def default_slot_load(
    hour: int, minute: int, daily_kwh: float, duration_hours: float = 0.5
) -> float:
    """Typical-profile load for one slot, scaled to a daily total."""
    index = (hour * 2 + (1 if minute >= 30 else 0)) % len(DIURNAL_WEIGHTS)
    share = DIURNAL_WEIGHTS[index] / _WEIGHT_SUM
    # The profile is defined per half-hour; scale if the slot is a different length.
    return max(daily_kwh, 0.0) * share * (duration_hours / 0.5)


@dataclass(slots=True)
class LoadPrediction:
    """A per-slot load prediction with provenance."""

    kwh: float
    source: str
    temperature: float | None = None
    climate_uplift_kwh: float = 0.0


class LoadForecaster:
    """Predicts household consumption per planning slot."""

    def __init__(
        self,
        model: LearningModel,
        daily_kwh: float,
        cooling_threshold_c: float = 22.0,
        cooling_kwh_per_degree_hour: float = 0.0,
        heating_threshold_c: float = 12.0,
        heating_kwh_per_degree_hour: float = 0.0,
        use_learning: bool = True,
    ) -> None:
        self._model = model
        self._daily_kwh = max(daily_kwh, 0.0)
        self._cooling_threshold = cooling_threshold_c
        self._cooling_rate = max(cooling_kwh_per_degree_hour, 0.0)
        self._heating_threshold = heating_threshold_c
        self._heating_rate = max(heating_kwh_per_degree_hour, 0.0)
        self._use_learning = use_learning

    def climate_uplift(self, temperature: float | None, duration_hours: float) -> float:
        """Extra load implied by outdoor temperature, in kWh for the slot.

        Zero by default: the learned temperature buckets are the better answer
        once they exist. This exists so a new install behaves sensibly during a
        heatwave before it has learned anything.
        """
        if temperature is None:
            return 0.0
        uplift = 0.0
        if self._cooling_rate > 0.0 and temperature > self._cooling_threshold:
            uplift += (temperature - self._cooling_threshold) * self._cooling_rate
        if self._heating_rate > 0.0 and temperature < self._heating_threshold:
            uplift += (self._heating_threshold - temperature) * self._heating_rate
        return uplift * duration_hours

    def predict_slot(
        self,
        local_start: datetime,
        duration_hours: float,
        temperature: float | None = None,
        is_holiday: bool = False,
    ) -> LoadPrediction:
        """Predict one slot. ``local_start`` must be in the user's timezone."""
        fallback = default_slot_load(
            local_start.hour, local_start.minute, self._daily_kwh, duration_hours
        )

        if not self._use_learning:
            uplift = self.climate_uplift(temperature, duration_hours)
            return LoadPrediction(
                kwh=fallback + uplift,
                source="profile",
                temperature=temperature,
                climate_uplift_kwh=uplift,
            )

        # The learned model works on half-hour slots; rescale for a short first
        # slot so a partial slot is not over-counted.
        learned, source = self._model.predict_load(
            hour=local_start.hour,
            minute=local_start.minute,
            weekday=local_start.weekday(),
            is_holiday=is_holiday,
            temperature=temperature,
            default_kwh=-1.0,
        )

        if source == "default" or learned < 0.0:
            uplift = self.climate_uplift(temperature, duration_hours)
            return LoadPrediction(
                kwh=fallback + uplift,
                source="profile",
                temperature=temperature,
                climate_uplift_kwh=uplift,
            )

        scaled = learned * (duration_hours / 0.5)
        # Once history exists it already contains whatever the A/C actually did,
        # so the explicit uplift is not applied on top -- that would double-count.
        return LoadPrediction(
            kwh=max(scaled, 0.0),
            source=source,
            temperature=temperature,
        )

    def predict_series(
        self,
        slots: list[tuple[datetime, datetime]],
        weather,
        to_local,
        is_holiday=None,
    ) -> list[LoadPrediction]:
        """Predict every slot of a horizon."""
        predictions: list[LoadPrediction] = []
        for start, end in slots:
            duration = (end - start).total_seconds() / 3600.0
            temperature = weather.temperature_at(start) if weather else None
            local = to_local(start)
            holiday = bool(is_holiday(local)) if callable(is_holiday) else False
            predictions.append(self.predict_slot(local, duration, temperature, holiday))
        return predictions

    def describe(self) -> dict[str, Any]:
        return {
            "default_daily_kwh": self._daily_kwh,
            "learning_enabled": self._use_learning,
            "load_buckets": len(self._model.load),
            "cooling_kwh_per_degree_hour": self._cooling_rate,
            "heating_kwh_per_degree_hour": self._heating_rate,
        }
