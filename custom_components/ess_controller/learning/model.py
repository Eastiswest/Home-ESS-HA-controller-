"""Pure-Python learning model for solar yield and household load.

The approach is deliberately simple and inspectable rather than a black box:
observations are binned into buckets keyed by the conditions that actually
drive the quantity, and each bucket holds an exponentially weighted mean.

* Solar is keyed by ``month : hour : sky bucket``. Given "this weather at this
  time of year at this hour", the bucket tells you how much you generated last
  time. The sky bucket is numeric cloud cover where the weather provider offers
  it, and UV index where it does not (the UK Met Office publishes UV but no
  cloud cover). Because the key already pins season and half-hour, solar
  elevation is held roughly constant, so variation in UV within a bucket is
  mostly cloud attenuation -- which makes it a sound stand-in.
* Load is keyed by ``season : day-type : hour : temperature bucket``.
  Temperature binning is what captures A/C draw on hot afternoons (and resistive
  heating on cold mornings) without needing an explicit model of either, while
  season captures what temperature cannot -- chiefly day length, and so lighting.

Buckets degrade gracefully: a specific bucket with too few samples falls back
to a broader one, and ultimately to a caller-supplied default. This means the
system produces sane plans on day one and sharpens over weeks.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Minimum weight given to a new observation once a bucket is well populated.
# 0.12 ~ an effective window of the last ~8 comparable observations, which
# tracks seasonal drift without chasing a single freak day.
DEFAULT_MIN_ALPHA = 0.12

# A bucket needs this many samples before we trust it over a broader fallback.
MIN_SAMPLES_TRUSTED = 3

CLOUD_BUCKET_SIZE = 20.0
CLOUD_BUCKETS = 5  # 0-20, 20-40, 40-60, 60-80, 80-100

# UV index buckets, used when a weather provider publishes UV but no cloud cover
# (the UK Met Office integration being the common case). Capped at 8: UK UV
# rarely exceeds that, and higher values carry no extra information about cloud.
UV_BUCKETS = 9
# Below this, UV carries almost no information -- winter days and early mornings
# read near zero whatever the sky is doing -- so fall back to another signal.
UV_USEFUL_THRESHOLD = 0.5

TEMPERATURE_EDGES: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0)


def cloud_bucket(cloud_cover: float | None) -> int:
    """Bin cloud cover percentage into a small number of buckets.

    ``None`` maps to the mid bucket so a missing weather attribute does not
    poison the model with a false "clear sky" signal.
    """
    if cloud_cover is None:
        return CLOUD_BUCKETS // 2
    clamped = min(max(float(cloud_cover), 0.0), 100.0)
    return min(int(clamped // CLOUD_BUCKET_SIZE), CLOUD_BUCKETS - 1)


def uv_bucket(uv_index: float | None) -> int:
    """Bin a UV index. Returns ``-1`` when unknown or too low to be informative."""
    if uv_index is None:
        return -1
    value = float(uv_index)
    if value < UV_USEFUL_THRESHOLD:
        return -1
    return min(round(value), UV_BUCKETS - 1)


def sky_key(cloud: float | None, uv_index: float | None = None) -> str:
    """The key fragment describing sky conditions for a solar bucket.

    Prefers numeric cloud cover, then UV index, then a neutral marker. The kind
    is encoded in the fragment (``c3`` vs ``u5``) so buckets learned from one
    signal never mix with another after a change of weather provider -- they
    simply stop being hit and new ones build alongside.
    """
    if cloud is not None:
        return f"c{cloud_bucket(cloud)}"
    bucket = uv_bucket(uv_index)
    if bucket >= 0:
        return f"u{bucket}"
    # Nothing usable: match the behaviour of an unknown cloud reading rather
    # than pretending the sky is clear.
    return f"c{cloud_bucket(None)}"


def temperature_bucket(temperature: float | None) -> int:
    """Bin an outdoor temperature in Celsius.

    Returns ``-1`` when unknown, which the lookup treats as "no temperature
    information" and skips straight to the temperature-agnostic fallback.
    """
    if temperature is None:
        return -1
    for index, edge in enumerate(TEMPERATURE_EDGES):
        if temperature < edge:
            return index
    return len(TEMPERATURE_EDGES)


def day_type(weekday: int, is_holiday: bool = False) -> str:
    """Classify a day. ``weekday`` follows ``datetime.weekday()`` (Mon = 0)."""
    if is_holiday or weekday >= 5:
        return "weekend"
    return "weekday"


@dataclass(slots=True)
class EwmaBucket:
    """An exponentially weighted mean with a sample count."""

    mean: float = 0.0
    samples: int = 0

    def update(self, value: float, min_alpha: float = DEFAULT_MIN_ALPHA) -> None:
        """Fold a new observation in.

        Early samples get a large weight (``1/n``) so the bucket converges to a
        sensible value quickly; once populated the weight floors at
        ``min_alpha`` so the bucket keeps tracking change.
        """
        self.samples += 1
        alpha = max(1.0 / self.samples, min_alpha)
        self.mean += alpha * (value - self.mean)

    @property
    def trusted(self) -> bool:
        return self.samples >= MIN_SAMPLES_TRUSTED

    def as_dict(self) -> dict[str, Any]:
        return {"mean": round(self.mean, 5), "samples": self.samples}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EwmaBucket:
        return cls(mean=float(data.get("mean", 0.0)), samples=int(data.get("samples", 0)))


class BucketSet:
    """A keyed collection of :class:`EwmaBucket` with fallback lookup."""

    def __init__(self, min_alpha: float = DEFAULT_MIN_ALPHA) -> None:
        self._buckets: dict[str, EwmaBucket] = {}
        self._min_alpha = min_alpha

    def __len__(self) -> int:
        return len(self._buckets)

    def __contains__(self, key: str) -> bool:
        return key in self._buckets

    def keys(self) -> Iterable[str]:
        return self._buckets.keys()

    def get(self, key: str) -> EwmaBucket | None:
        return self._buckets.get(key)

    def update(self, key: str, value: float) -> EwmaBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = EwmaBucket()
            self._buckets[key] = bucket
        bucket.update(value, self._min_alpha)
        return bucket

    def lookup(self, keys: list[str], default: float = 0.0) -> tuple[float, str]:
        """Return the first trusted bucket's mean, walking from specific to broad.

        Falls back to an untrusted bucket before giving up on ``default``, since
        one real observation still beats a guess. The second element of the
        tuple names the key that supplied the answer, which the sensors surface
        so a user can see how confident the model is.
        """
        for key in keys:
            bucket = self._buckets.get(key)
            if bucket is not None and bucket.trusted:
                return bucket.mean, key
        for key in keys:
            bucket = self._buckets.get(key)
            if bucket is not None and bucket.samples > 0:
                return bucket.mean, f"{key}~"
        return default, "default"

    def total_samples(self) -> int:
        return sum(b.samples for b in self._buckets.values())

    def as_dict(self) -> dict[str, Any]:
        return {k: v.as_dict() for k, v in self._buckets.items()}

    def load_dict(self, data: dict[str, Any] | None) -> None:
        self._buckets.clear()
        for key, raw in (data or {}).items():
            if isinstance(raw, dict):
                self._buckets[key] = EwmaBucket.from_dict(raw)

    def clear(self) -> None:
        self._buckets.clear()


@dataclass(slots=True)
class SolarObservation:
    """A completed half-hour of measured PV generation."""

    month: int
    hour: int
    minute: int
    kwh: float
    cloud_cover: float | None = None
    uv_index: float | None = None
    """Sky signal fallback for providers publishing UV but not cloud cover."""
    forecast_kwh: float | None = None
    """External forecast for the same slot, when one was available."""


@dataclass(slots=True)
class LoadObservation:
    """A completed half-hour of measured household consumption."""

    hour: int
    minute: int
    kwh: float
    weekday: int = 0
    is_holiday: bool = False
    temperature: float | None = None
    month: int | None = None
    """Time of year. Separates a dark December evening from a light June one."""


class LearningModel:
    """Container for every learned relationship, with (de)serialisation."""

    def __init__(self, min_alpha: float = DEFAULT_MIN_ALPHA) -> None:
        # Absolute PV energy per half-hour slot, keyed by season/time/weather.
        self.solar_absolute = BucketSet(min_alpha)
        # Multiplicative correction applied to an external forecast. Learning a
        # ratio rather than an absolute converges far faster when a good
        # forecast source (Solcast / Forecast.Solar) is present, and it
        # automatically absorbs systematic bias such as panel soiling, shading
        # or a mis-declared array azimuth.
        self.solar_ratio = BucketSet(min_alpha)
        # Household load per half-hour slot.
        self.load = BucketSet(min_alpha)
        self._min_alpha = min_alpha
        # Counted explicitly rather than inferred from bucket sample totals:
        # an observation writes into a variable number of fallback buckets
        # (fewer when temperature is unknown), so dividing totals by a fixed
        # key depth would misreport maturity.
        self.solar_observations = 0
        self.load_observations = 0

    # -- keys ------------------------------------------------------------

    @staticmethod
    def _slot_index(hour: int, minute: int) -> int:
        return hour * 2 + (1 if minute >= 30 else 0)

    def solar_keys(
        self,
        month: int,
        hour: int,
        minute: int,
        cloud: float | None,
        uv_index: float | None = None,
    ) -> list[str]:
        slot = self._slot_index(hour, minute)
        sky = sky_key(cloud, uv_index)
        season = self.season_of(month)
        return [
            f"m{month}:s{slot}:{sky}",
            f"season{season}:s{slot}:{sky}",
            f"season{season}:s{slot}",
            f"s{slot}",
        ]

    def load_keys(
        self,
        hour: int,
        minute: int,
        weekday: int,
        is_holiday: bool,
        temperature: float | None,
        month: int | None = None,
    ) -> list[str]:
        """Keys for one load slot, most specific first.

        Season is the most specific level because temperature alone does not
        separate a mild December evening from a cool June one: at 17:00 in
        December the lights and cooking are on, in June they are not, and day
        length -- not temperature -- is what drives that. Temperature still
        carries the heating and cooling response.

        Adding season multiplies the bucket count by four, which would slow
        convergence if it were the only level. The fallback chain absorbs that:
        until a seasonal bucket has enough samples the season-agnostic one
        answers instead, so nothing is lost early on.
        """
        slot = self._slot_index(hour, minute)
        dtype = day_type(weekday, is_holiday)
        tbucket = temperature_bucket(temperature)
        season = self.season_of(month) if month is not None else None

        keys: list[str] = []
        if tbucket >= 0:
            if season is not None:
                keys.append(f"season{season}:{dtype}:s{slot}:t{tbucket}")
            keys.append(f"{dtype}:s{slot}:t{tbucket}")
            keys.append(f"any:s{slot}:t{tbucket}")
        if season is not None:
            keys.append(f"season{season}:{dtype}:s{slot}")
        keys.append(f"{dtype}:s{slot}")
        keys.append(f"any:s{slot}")
        return keys

    @staticmethod
    def season_of(month: int) -> int:
        """Group months into quarters so buckets fill ~3x faster than monthly."""
        return (month % 12) // 3

    # -- training --------------------------------------------------------

    def observe_solar(self, obs: SolarObservation) -> None:
        keys = self.solar_keys(
            obs.month, obs.hour, obs.minute, obs.cloud_cover, obs.uv_index
        )
        self.solar_observations += 1
        for key in keys:
            self.solar_absolute.update(key, obs.kwh)
        if obs.forecast_kwh is not None and obs.forecast_kwh > 0.05:
            # Clamp the ratio: a forecast of near-zero that produced a little
            # power would otherwise create an enormous multiplier.
            ratio = min(max(obs.kwh / obs.forecast_kwh, 0.0), 3.0)
            for key in keys:
                self.solar_ratio.update(key, ratio)

    def observe_load(self, obs: LoadObservation) -> None:
        self.load_observations += 1
        for key in self.load_keys(
            obs.hour,
            obs.minute,
            obs.weekday,
            obs.is_holiday,
            obs.temperature,
            obs.month,
        ):
            self.load.update(key, obs.kwh)

    # -- prediction ------------------------------------------------------

    def predict_solar(
        self,
        month: int,
        hour: int,
        minute: int,
        cloud: float | None,
        forecast_kwh: float | None = None,
        default_kwh: float = 0.0,
        uv_index: float | None = None,
    ) -> tuple[float, str]:
        """Predict PV energy for a half-hour slot.

        When an external forecast is supplied we trust its *shape* and learn a
        correction factor; otherwise we fall back to the learned absolute
        history, and finally to ``default_kwh``.
        """
        keys = self.solar_keys(month, hour, minute, cloud, uv_index)
        if forecast_kwh is not None:
            ratio, source = self.solar_ratio.lookup(keys, default=1.0)
            # Keep the correction sane even if a bucket is polluted.
            ratio = min(max(ratio, 0.25), 2.0)
            return max(forecast_kwh * ratio, 0.0), f"forecast*{ratio:.2f}@{source}"
        value, source = self.solar_absolute.lookup(keys, default=default_kwh)
        return max(value, 0.0), source

    def predict_load(
        self,
        hour: int,
        minute: int,
        weekday: int,
        is_holiday: bool = False,
        temperature: float | None = None,
        default_kwh: float = 0.25,
        month: int | None = None,
    ) -> tuple[float, str]:
        keys = self.load_keys(hour, minute, weekday, is_holiday, temperature, month)
        value, source = self.load.lookup(keys, default=default_kwh)
        return max(value, 0.0), source

    # -- introspection ---------------------------------------------------

    @property
    def solar_samples(self) -> int:
        return self.solar_absolute.total_samples()

    @property
    def load_samples(self) -> int:
        return self.load.total_samples()

    def confidence(self) -> dict[str, Any]:
        """A coarse readout of how much the model has learned so far."""
        solar_obs = self.solar_observations
        load_obs = self.load_observations
        return {
            "solar_observations": solar_obs,
            "load_observations": load_obs,
            "solar_buckets": len(self.solar_absolute),
            "load_buckets": len(self.load),
            "solar_ratio_buckets": len(self.solar_ratio),
            # 14 days of half-hourly data is roughly where the buckets settle.
            "solar_maturity": min(1.0, round(solar_obs / (48 * 14), 3)),
            "load_maturity": min(1.0, round(load_obs / (48 * 14), 3)),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "solar_absolute": self.solar_absolute.as_dict(),
            "solar_ratio": self.solar_ratio.as_dict(),
            "load": self.load.as_dict(),
            "solar_observations": self.solar_observations,
            "load_observations": self.load_observations,
        }

    def load_dict(self, data: dict[str, Any] | None) -> None:
        data = data or {}
        self.solar_absolute.load_dict(data.get("solar_absolute"))
        self.solar_ratio.load_dict(data.get("solar_ratio"))
        self.load.load_dict(data.get("load"))
        self.solar_observations = int(data.get("solar_observations") or 0)
        self.load_observations = int(data.get("load_observations") or 0)

    def reset(self) -> None:
        self.solar_absolute.clear()
        self.solar_ratio.clear()
        self.load.clear()
        self.solar_observations = 0
        self.load_observations = 0
