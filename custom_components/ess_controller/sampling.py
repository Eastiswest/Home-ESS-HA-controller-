"""Turn periodic power samples into completed half-hour energy observations.

The coordinator polls every few minutes and hands the instantaneous PV and load
power to :class:`SlotAccumulator`. The accumulator integrates power over time
into the current half-hour slot and, when the slot rolls over, emits a
completed observation for the learning model.

Slots with poor coverage (a Home Assistant restart, an unavailable sensor) are
discarded rather than committed, because a slot that only saw two minutes of
data would otherwise teach the model that the house used almost nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .const import SLOT_MINUTES

_LOGGER = logging.getLogger(__name__)

# Fraction of a slot that must be covered by samples before we trust it.
MIN_COVERAGE = 0.6

# A gap longer than this means the integration was asleep; don't bridge it.
MAX_SAMPLE_GAP = timedelta(minutes=10)

_SLOT_LENGTH = timedelta(minutes=SLOT_MINUTES)


def slot_start_for(moment: datetime) -> datetime:
    """Floor a timestamp to its half-hour slot boundary."""
    minute = (moment.minute // SLOT_MINUTES) * SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)


@dataclass(slots=True)
class CompletedSlot:
    """An integrated half-hour of site measurements."""

    start: datetime
    end: datetime
    pv_kwh: float
    load_kwh: float
    coverage: float
    cloud_cover: float | None = None
    uv_index: float | None = None
    temperature: float | None = None
    forecast_kwh: float | None = None
    grid_import_kwh: float = 0.0
    """Metered import. Integrated separately from export rather than netted:
    a slot that imports 1 kWh and exports 1 kWh costs real money, and netting
    it to zero would hide that."""
    grid_export_kwh: float = 0.0
    grid_measured: bool = False
    """Whether a grid power sensor was actually contributing. Without one the
    two figures above are zero and must not be read as "imported nothing"."""


@dataclass(slots=True)
class _Bucket:
    start: datetime
    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    grid_seconds: float = 0.0
    covered_seconds: float = 0.0
    cloud_samples: list[float] = field(default_factory=list)
    uv_samples: list[float] = field(default_factory=list)
    temp_samples: list[float] = field(default_factory=list)
    forecast_kwh: float | None = None


class SlotAccumulator:
    """Integrates power samples into half-hour energy totals."""

    def __init__(self) -> None:
        self._bucket: _Bucket | None = None
        self._last_sample: datetime | None = None

    def reset(self) -> None:
        self._bucket = None
        self._last_sample = None

    @property
    def current_coverage(self) -> float:
        if self._bucket is None:
            return 0.0
        return self._bucket.covered_seconds / _SLOT_LENGTH.total_seconds()

    def add_sample(
        self,
        moment: datetime,
        pv_power_kw: float | None,
        load_power_kw: float | None,
        cloud_cover: float | None = None,
        temperature: float | None = None,
        forecast_kwh: float | None = None,
        uv_index: float | None = None,
        grid_power_kw: float | None = None,
    ) -> list[CompletedSlot]:
        """Record a sample, returning any slots completed by it.

        Power is treated as constant since the previous sample (left Riemann
        sum). At a 5-minute cadence against household load this is accurate
        enough for planning; we are learning a typical shape, not billing.

        An interval that straddles a slot boundary is split at the boundary so
        each half-hour is credited only with the energy that belongs to it.
        """
        completed: list[CompletedSlot] = []
        previous = self._last_sample
        self._last_sample = moment

        if self._bucket is None:
            self._bucket = _Bucket(start=slot_start_for(moment))

        if (
            previous is not None
            and moment > previous
            and moment - previous <= MAX_SAMPLE_GAP
        ):
            cursor = previous
            while cursor < moment:
                cursor_slot = slot_start_for(cursor)
                segment_end = min(moment, cursor_slot + _SLOT_LENGTH)

                if self._bucket.start != cursor_slot:
                    finished = self._finalise(self._bucket)
                    if finished is not None:
                        completed.append(finished)
                    self._bucket = _Bucket(start=cursor_slot)

                seconds = (segment_end - cursor).total_seconds()
                hours = seconds / 3600.0
                if pv_power_kw is not None:
                    self._bucket.pv_kwh += max(float(pv_power_kw), 0.0) * hours
                if load_power_kw is not None:
                    self._bucket.load_kwh += max(float(load_power_kw), 0.0) * hours
                if grid_power_kw is not None:
                    # Sign convention: positive is import, negative is export.
                    grid = float(grid_power_kw)
                    self._bucket.grid_import_kwh += max(grid, 0.0) * hours
                    self._bucket.grid_export_kwh += max(-grid, 0.0) * hours
                    self._bucket.grid_seconds += seconds
                self._bucket.covered_seconds += seconds
                cursor = segment_end

        # Make the live bucket match the slot the sample actually landed in.
        target_slot = slot_start_for(moment)
        if self._bucket.start != target_slot:
            finished = self._finalise(self._bucket)
            if finished is not None:
                completed.append(finished)
            self._bucket = _Bucket(start=target_slot)

        if cloud_cover is not None:
            self._bucket.cloud_samples.append(float(cloud_cover))
        if uv_index is not None:
            self._bucket.uv_samples.append(float(uv_index))
        if temperature is not None:
            self._bucket.temp_samples.append(float(temperature))
        if forecast_kwh is not None:
            self._bucket.forecast_kwh = forecast_kwh

        return completed

    def _finalise(self, bucket: _Bucket) -> CompletedSlot | None:
        coverage = bucket.covered_seconds / _SLOT_LENGTH.total_seconds()
        if coverage < MIN_COVERAGE:
            _LOGGER.debug(
                "Discarding slot %s: coverage %.0f%% below threshold",
                bucket.start.isoformat(),
                coverage * 100,
            )
            return None

        # Scale a partially covered slot up to a full half-hour equivalent so
        # a slot with 80% coverage still teaches the right magnitude.
        scale = 1.0 / coverage
        cloud = (
            sum(bucket.cloud_samples) / len(bucket.cloud_samples)
            if bucket.cloud_samples
            else None
        )
        uv = (
            sum(bucket.uv_samples) / len(bucket.uv_samples) if bucket.uv_samples else None
        )
        temp = (
            sum(bucket.temp_samples) / len(bucket.temp_samples)
            if bucket.temp_samples
            else None
        )
        return CompletedSlot(
            start=bucket.start,
            end=bucket.start + _SLOT_LENGTH,
            pv_kwh=bucket.pv_kwh * scale,
            load_kwh=bucket.load_kwh * scale,
            coverage=min(coverage, 1.0),
            cloud_cover=cloud,
            uv_index=uv,
            temperature=temp,
            forecast_kwh=bucket.forecast_kwh,
            grid_import_kwh=bucket.grid_import_kwh * scale,
            grid_export_kwh=bucket.grid_export_kwh * scale,
            grid_measured=bucket.grid_seconds > 0,
        )


def slot_boundaries(
    now: datetime, horizon_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Half-hour planning boundaries from ``now``, with a shortened first slot.

    Planning from the next half-hour boundary would ignore the remainder of the
    current one, which is exactly the period a decision applies to. A sliver of
    a slot is not worth planning separately, so anything under two minutes is
    folded into the following slot.

    The final slot is clamped to ``horizon_end`` so the plan never prices energy
    beyond the horizon it was asked for.
    """
    if horizon_end <= now:
        return []

    boundaries: list[tuple[datetime, datetime]] = []
    first_end = min(slot_start_for(now) + _SLOT_LENGTH, horizon_end)

    if (first_end - now).total_seconds() >= 120:
        boundaries.append((now, first_end))
    cursor = first_end

    while cursor < horizon_end:
        boundaries.append((cursor, min(cursor + _SLOT_LENGTH, horizon_end)))
        cursor += _SLOT_LENGTH
    return boundaries
