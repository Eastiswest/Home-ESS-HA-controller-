"""How much to believe the load and solar forecasts, and what to do about it.

A fresh install has no idea what the house does. The buckets start from a flat
default shape, and on a real install that shape put the 17:00-23:30 stretch at
3.9 kWh against about 6.6 kWh actually used -- an oven, a dishwasher and a kettle,
which is simply what an evening looks like. 41% low.

That error is not symmetrical in its consequences. A battery floored at 20% that
enters the evening on a forecast 41% too light runs out somewhere around 20:00 and
buys the rest of the evening at the top of the tariff, 46-58p. Carrying a few kWh
more than it turned out to need costs the wear on them, well under 2p each.

So while the model is young the plan is built against a deliberately pessimistic
forecast: load marked up, solar marked down, both by amounts that shrink to nothing
as the house teaches the model what it actually does. Applied once, here, so the
energy balance and everything derived from it agree about how heavy the evening is.

Home Assistant-free, so the arithmetic is testable on its own.
"""

from __future__ import annotations

# Set from the measured error on a real install rather than picked for feel.
UNTRAINED_LOAD_MARGIN = 0.40

# Derated harder than the load is marked up, for two reasons: an overcast day can
# take far more than a third off an array, and a day that loses its solar *and*
# turns out to have a heavy evening is one bad day rather than two independent ones.
UNTRAINED_SOLAR_DERATE = 0.35


def doubt(confidence: float) -> float:
    """Turn a maturity in 0..1 into how much of the allowance to apply."""
    return 1.0 - min(max(confidence, 0.0), 1.0)


def load_factor(confidence: float) -> float:
    """Multiplier for a forecast load. At least 1: never plan for less."""
    return 1.0 + UNTRAINED_LOAD_MARGIN * doubt(confidence)


def solar_factor(confidence: float) -> float:
    """Multiplier for a forecast generation. At most 1: never count on more."""
    return 1.0 - UNTRAINED_SOLAR_DERATE * doubt(confidence)


def describe(confidence: float) -> str:
    """One line for the diagnostics and the dashboard."""
    if doubt(confidence) <= 0.01:
        return "forecasts trusted as they stand"
    return (
        f"still learning ({confidence * 100:.0f}% of the way): planning for "
        f"{load_factor(confidence):.2f}x the forecast load and "
        f"{solar_factor(confidence):.2f}x the forecast solar"
    )
