"""A physical day-one estimate of PV output, for before anything is known.

The load forecaster has always had a sensible answer on day one: a diurnal
profile scaled to the household's typical daily total. Solar had nothing -- with
no forecast entity mapped and no learned history, every slot came back as exactly
zero, so the optimiser planned as though the sun would not rise. On a 2 kW array
in August that is not a small error, and it made the first days of an install
actively worse than useless: it over-buys from the grid, and the user has to
drive the battery by hand until the model catches up.

This closes that gap. It is deliberately crude -- solar position times peak power
times a fixed derate, dimmed by cloud -- because its only job is to be roughly
right before better information exists. It is superseded in order by:

1. a real solar forecast entity, if one is mapped;
2. learned history, once there are enough observations to trust.

Both of those are strictly better, so this should be the answer only briefly.

No dependencies and no Home Assistant: the solar position is computed from the
NOAA approximation, which is a couple of dozen lines of arithmetic and accurate
to well within a degree -- far better than the yield model it feeds.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

# Fraction of nameplate that survives to the meter, per unit of sin(elevation).
#
# This bundles two separate things, deliberately. The first is ordinary system
# loss -- inverter, cell temperature, wiring, soiling, glazing -- which is the
# usual 0.85. The second is geometry: sin(elevation) is the irradiance on a
# surface held square to the sun, and a roof is fixed, so a real array collects
# noticeably less than that whenever the sun is not normal to the panel. Modelling
# that properly needs tilt and azimuth, which would mean two more questions at
# setup for a number that exists only until a real forecast or a week of history
# replaces it.
#
# So it is calibrated instead: 0.55 puts a 2 kWp array in southern England at
# roughly 8 kWh on a clear August day and 1.4 kWh at midwinter, which is about
# what such an array actually does. Tests pin those totals so a future change
# cannot quietly triple them.
DERATE = 0.55

# What overcast leaves behind. Diffuse light through full cloud is worth roughly
# a quarter of clear sky, so cloud scales output by 1 - 0.75 * fraction.
CLOUD_IMPACT = 0.75

# Below this the sun is too low to bother with: refraction, horizon obstructions
# and cosine losses make anything here noise rather than energy.
MIN_ELEVATION_DEG = 3.0


def solar_elevation(latitude: float, longitude: float, moment: datetime) -> float:
    """The sun's elevation above the horizon, in degrees.

    NOAA's approximation. ``moment`` must be timezone-aware -- a naive timestamp
    would be read as UTC and put the whole day up to twelve hours out, which is
    exactly the sort of silent error that makes a forecast look plausible and be
    wrong, so it is refused instead.
    """
    if moment.tzinfo is None:
        raise ValueError("solar_elevation needs an aware datetime")
    when = moment.astimezone(UTC)

    day_of_year = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60.0 + when.second / 3600.0

    # Fractional year, radians.
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hours - 12.0) / 24.0)

    # Equation of time, minutes.
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    # Solar declination, radians.
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.001480 * math.sin(3 * gamma)
    )

    # True solar time, minutes, then the hour angle in degrees.
    true_solar_time = hours * 60.0 + eqtime + 4.0 * longitude
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = min(max(cos_zenith, -1.0), 1.0)
    return 90.0 - math.degrees(math.acos(cos_zenith))


def clear_sky_kwh(
    latitude: float,
    longitude: float,
    start: datetime,
    duration_hours: float,
    peak_kw: float,
    cloud: float | None = None,
) -> float:
    """Estimated PV energy for one slot, in kWh.

    ``cloud`` is a fraction from 0 to 1; a percentage is accepted too, since
    weather providers disagree about which they publish and a value above 1 can
    only have meant a percentage.
    """
    if peak_kw <= 0 or duration_hours <= 0:
        return 0.0

    elevation = solar_elevation(latitude, longitude, start)
    if elevation < MIN_ELEVATION_DEG:
        return 0.0

    output_kw = peak_kw * math.sin(math.radians(elevation)) * DERATE

    if cloud is not None:
        fraction = cloud / 100.0 if cloud > 1.0 else cloud
        fraction = min(max(fraction, 0.0), 1.0)
        output_kw *= 1.0 - CLOUD_IMPACT * fraction

    return max(output_kw * duration_hours, 0.0)
