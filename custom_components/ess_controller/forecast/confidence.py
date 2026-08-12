"""How much to believe the load and solar forecasts, and what to do about it.

A fresh install has no idea what the house does. The buckets start from a flat
default shape, and on a real install that shape put the 17:00-23:30 stretch at
3.9 kWh against about 6.6 kWh actually used -- an oven, a dishwasher and a kettle,
which is simply what an evening looks like. 41% low.

That error is not symmetrical in its consequences. A battery floored at 20% that
enters the evening on a forecast 41% too light runs out somewhere around 20:00 and
buys the rest of the evening at the top of the tariff, 46-58p. Carrying a few kWh
more than it turned out to need costs the wear on them, well under 2p each.

So while the model is young the plan provisions for a heavier evening than the
forecast claims, by an amount that shrinks to nothing as the house teaches the model
what it actually does. Only the evening: the error is concentrated there, and
marking the whole day up made the plan buy for a shortfall that did not exist before
tea -- about 150p a day to hedge something worth 73p.

Home Assistant-free, so the arithmetic is testable on its own.
"""

from __future__ import annotations

# How much extra evening load to plan for, in kWh, while the model is learning.
#
# Set from the measured miss: a real install's forecast put 17:00-23:30 at 3.9 kWh
# against about 6.6 kWh used, so roughly 2.7 kWh of evening arrived unannounced.
# Three kWh covers that with a little room.
UNTRAINED_EVENING_KWH = 3.0

# The hours it is added to, local time. Ovens, dishwashers and kettles, which is
# where a flat default shape is most wrong and where the consequence is worst,
# because it coincides with the dearest half-hours of an Agile day.
EVENING_START_HOUR = 16
EVENING_END_HOUR = 23


def doubt(confidence: float) -> float:
    """Turn a maturity in 0..1 into how much of the allowance to apply."""
    return 1.0 - min(max(confidence, 0.0), 1.0)


def evening_allowance_kwh(confidence: float) -> float:
    """Extra evening load to provision for, at this level of confidence."""
    return UNTRAINED_EVENING_KWH * doubt(confidence)


def is_evening(hour: int) -> bool:
    return EVENING_START_HOUR <= hour <= EVENING_END_HOUR


def evening_uplift(
    hours: list[int], loads: list[float], confidence: float
) -> list[float]:
    """Extra kWh to add to each slot's forecast load, same length as the inputs.

    Two things this deliberately is not.

    It is not a markup on the *whole day*. That was the first attempt, and measured
    against a real horizon it cost about 150p a day to hedge a shortfall worth about
    73p: inflating midday demand made the plan buy for a shortfall that only existed
    after tea. The error is concentrated in a few hours, so the allowance is too.

    It is not a raised floor either. A floor protects charge for later, and what is
    wanted is more charge arriving *at* the evening -- raising the floor would make
    the battery stop discharging sooner, which is the opposite.

    Spread across the evening in proportion to the load already forecast there, so it
    lands where the demand is rather than smearing evenly over hours nobody is
    cooking in. If the forecast puts nothing in the evening at all, it is spread flat
    rather than discarded.
    """
    allowance = evening_allowance_kwh(confidence)
    if allowance <= 0.0:
        return [0.0] * len(loads)
    evening = [i for i, hour in enumerate(hours) if is_evening(hour)]
    if not evening:
        return [0.0] * len(loads)
    total = sum(loads[i] for i in evening)
    uplift = [0.0] * len(loads)
    for i in evening:
        share = (loads[i] / total) if total > 0 else (1.0 / len(evening))
        uplift[i] = allowance * share
    return uplift


def describe(confidence: float) -> str:
    """One line for the diagnostics and the dashboard."""
    allowance = evening_allowance_kwh(confidence)
    if allowance <= 0.01:
        return "forecasts trusted as they stand"
    return (
        f"still learning ({confidence * 100:.0f}% of the way): planning for "
        f"{allowance:.1f} kWh more evening load than forecast"
    )
