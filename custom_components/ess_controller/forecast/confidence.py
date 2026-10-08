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


def evening_allowance_kwh(confidence: float, measured_error_kwh: float = 0.0) -> float:
    """Extra evening load to provision for, given maturity and what happened.

    ``measured_error_kwh`` is the average signed error of the evening forecast on
    this house, in kWh per evening, positive when the forecast has been running
    *high*. It is subtracted from the allowance because the two are the same
    quantity measured two ways: the allowance is a guess at how much evening
    arrives unannounced, and the measured error is the answer.

    Maturity alone was not enough. It rises with the number of observations, not
    with whether they agreed, so a house whose evenings the model had already
    learned went on being provisioned for three kilowatt-hours it did not use.
    On a real install at 41% maturity the load forecast was running 0.07 kWh a
    slot high and the hedge was still adding 1.8 kWh a night on top -- insurance
    against a risk the evidence said had gone, paid for in grid purchases.

    Symmetric, within the ceiling. A negative error means the evenings have
    been running *heavier* than forecast, and that adds to the allowance. It
    used to be ignored on the reasoning that the hedge was already sized for
    it, which holds only while the hedge exists: on a mature model it is zero,
    so a house whose evenings shifted in October was planned to the decimal
    of a forecast that had missed by 0.6 and 1.8 kWh on the two nights before,
    and the plan arrived at the floor halfway through the dear half-hours.
    Being short costs several pence a kWh at the evening rate; being long
    costs the wear on a kWh that displaces the next cheap purchase.

    Floored at zero and capped at the untrained allowance, so a single wild
    evening cannot provision more than the young-model hedge ever would.
    """
    allowance = UNTRAINED_EVENING_KWH * doubt(confidence) - measured_error_kwh
    return min(max(allowance, 0.0), UNTRAINED_EVENING_KWH)


def is_evening(hour: int) -> bool:
    return EVENING_START_HOUR <= hour <= EVENING_END_HOUR


def evening_uplift(
    hours: list[int],
    loads: list[float],
    confidence: float,
    measured_error_kwh: float = 0.0,
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

    Spread across each evening in proportion to the load already forecast there,
    so it lands where the demand is rather than smearing evenly over hours nobody
    is cooking in. If the forecast puts nothing in an evening at all, it is spread
    flat rather than discarded.

    Per evening, not per horizon. The allowance is a nightly figure, and spread
    once over everything a 48-hour horizon holds it was halved for each of the
    two evenings in it. Evenings are told apart by the hour falling back: the
    slots are chronological, so a 16 after a 23 is tomorrow's.
    """
    allowance = evening_allowance_kwh(confidence, measured_error_kwh)
    if allowance <= 0.0:
        return [0.0] * len(loads)
    evenings: list[list[int]] = []
    last_hour: int | None = None
    for i, hour in enumerate(hours):
        if not is_evening(hour):
            continue
        if not evenings or (last_hour is not None and hour < last_hour):
            evenings.append([])
        evenings[-1].append(i)
        last_hour = hour
    if not evenings:
        return [0.0] * len(loads)
    uplift = [0.0] * len(loads)
    for evening in evenings:
        total = sum(loads[i] for i in evening)
        for i in evening:
            share = (loads[i] / total) if total > 0 else (1.0 / len(evening))
            uplift[i] = allowance * share
    return uplift


# The most extra daytime load a run of heavy days may provision for. The same
# ceiling as the evening's, for the same reason: one wild day must not buy a
# week's worth of insurance.
DAYTIME_HEDGE_CAP_KWH = 3.0

# Half-hours outside the evening in a day, which is what a day's allowance is
# spread over when the whole day is ahead.
DAYTIME_SLOTS = (24 - (EVENING_END_HOUR - EVENING_START_HOUR + 1)) * 2


def daytime_allowance_kwh(measured_error_kwh: float) -> float:
    """Extra daytime load to provision for, given what recent days did.

    ``measured_error_kwh`` is the signed error of the forecast outside the
    evening, in kWh per day, positive when the forecast has been running high.
    Only a shortfall counts: an over-call is the daytime correction's business.

    No young-model term. The evening carries that, because a flat default
    shape is wrong about the evening and roughly right about the rest; what the
    daytime needs is the feedback. The first cold mornings of a heating season
    put 1.3 kWh of unforecast load before nine o'clock on a real install, and
    the evening hedge -- which only ever looked at evenings -- did not move.
    """
    return min(max(-measured_error_kwh, 0.0), DAYTIME_HEDGE_CAP_KWH)


def daytime_uplift(
    hours: list[int], loads: list[float], measured_error_kwh: float
) -> list[float]:
    """Extra kWh to add to each daytime slot's forecast, same length as the inputs.

    Per day, spread across the slots outside the evening in proportion to the
    load forecast there, so it lands at breakfast rather than at three in the
    morning. A day the horizon only holds part of gets its share of the
    allowance: the morning that has already happened cannot be provisioned for.
    """
    allowance = daytime_allowance_kwh(measured_error_kwh)
    if allowance <= 0.0:
        return [0.0] * len(loads)
    days: list[list[int]] = []
    last_hour: int | None = None
    for i, hour in enumerate(hours):
        if is_evening(hour):
            continue
        if not days or (last_hour is not None and hour < last_hour):
            days.append([])
        days[-1].append(i)
        last_hour = hour
    uplift = [0.0] * len(loads)
    for day in days:
        share_of_day = min(len(day) / DAYTIME_SLOTS, 1.0)
        total = sum(loads[i] for i in day)
        for i in day:
            share = (loads[i] / total) if total > 0 else (1.0 / len(day))
            uplift[i] = allowance * share_of_day * share
    return uplift


# How much of a shortfall the sun's recent form may take off the forecast.
#
# The floor, not the whole ratio: a forecast cannot be trusted to within a
# factor of two in either direction on three days' evidence, and planning for
# half the sun on a day that then delivers all of it costs a kWh or two bought
# cheap and carried, which is the direction to err in.
SOLAR_SHORTFALL_FLOOR = 0.5

# Less forecast sun than this over the window says nothing about the forecast.
MIN_SOLAR_SHORTFALL_FORECAST_KWH = 1.0

# Daylight half-hours needed before the sun's form is a form rather than one
# cloud: a fresh log's first midday half-hour can forecast over a kWh alone.
MIN_SOLAR_SHORTFALL_SLOTS = 12


def solar_shortfall_ratio(actual_kwh: float, forecast_kwh: float) -> float:
    """What fraction of its forecast the sun has recently delivered, at most 1.

    The learned correction fixes the forecast's systematic bias bucket by
    bucket and needs days in each to do it. This is the short-term term: three
    consecutive days delivered 50%, 73% and 78% of forecast on a real install
    while the thirty-day bias read zero, and the plan went into each evening
    sized for sun that had not come. One-sided, because the sun beating its
    forecast is already provided for by the room the solar reserve keeps.
    """
    if forecast_kwh < MIN_SOLAR_SHORTFALL_FORECAST_KWH:
        return 1.0
    ratio = max(actual_kwh, 0.0) / forecast_kwh
    return min(max(ratio, SOLAR_SHORTFALL_FLOOR), 1.0)


def describe(
    confidence: float,
    measured_error_kwh: float = 0.0,
    daytime_error_kwh: float = 0.0,
    solar_ratio: float = 1.0,
    solar_share: float | None = None,
) -> str:
    """One line for the diagnostics and the dashboard.

    ``solar_share`` is what the sun actually delivered of its forecast;
    ``solar_ratio`` is the clipped figure the plan uses. Both are said when
    they differ, so a sun at 20% is not reported as a sun at 50%.
    """
    parts: list[str] = []
    allowance = evening_allowance_kwh(confidence, measured_error_kwh)
    if allowance > 0.01:
        if measured_error_kwh < -0.01 and doubt(confidence) < 0.5:
            parts.append(
                f"recent evenings ran {-measured_error_kwh:.1f} kWh heavier than "
                f"forecast: planning for {allowance:.1f} kWh more evening load"
            )
        else:
            parts.append(
                f"still learning ({confidence * 100:.0f}% of the way): planning for "
                f"{allowance:.1f} kWh more evening load than forecast"
            )
    daytime = daytime_allowance_kwh(daytime_error_kwh)
    if daytime > 0.01:
        parts.append(
            f"recent days ran {-daytime_error_kwh:.1f} kWh heavier than forecast "
            f"outside the evening: planning for {daytime:.1f} kWh more daytime load"
        )
    if solar_ratio < 0.99:
        share = solar_ratio if solar_share is None else solar_share
        parts.append(
            f"the sun has delivered {share * 100:.0f}% of its forecast lately: "
            f"planning for {solar_ratio * 100:.0f}% of it"
        )
    if not parts:
        return "forecasts trusted as they stand"
    return "; ".join(parts)


# How many measured slots before a bias is a bias rather than a run of weather.
MIN_BIAS_SLOTS = 48

# The most of a slot's forecast the correction may remove.
#
# A bias measured across hundreds of slots is a real property of the model, but
# it is an *average*, and subtracting an average from a slot that happens to be
# small can drive it to nothing. Half is enough to fix a systematic over-call
# and not enough to invent an empty house.
MAX_BIAS_FRACTION = 0.5


def daytime_correction(
    hours: list[int], loads: list[float], bias_kwh: float, slots_measured: int
) -> list[float]:
    """kWh to take off each slot's forecast load, same length as the inputs.

    The load model over-called this house by 0.042 kWh a slot across the
    daytime -- systematic, not weather. Daytime measured on its own, not the
    0.064 blended across the whole day: two thirds of that lives in the evening,
    where it is the young-model allowance doing its job, and applying the blend
    here would have deleted about 2 kWh a day of real demand. The scoping is the
    substance of this correction rather than a detail of it.

    That matters more than it sounds, because
    an over-called load under-states the *solar surplus* kWh for kWh: on a real
    August afternoon it turned a genuine 2.3 kWh of spare sun into a forecast
    1.4 kWh, so the plan filled the last of the headroom from the grid at 19.7p
    and left the afternoon's own generation nowhere to go. Both halves of that
    are losses, which is what makes the error asymmetric and worth correcting.

    Evening slots are left alone. They have their own machinery -- an allowance
    that is added while the model is young and retired as the evenings
    themselves disprove it -- and correcting them here as well would be the same
    adjustment applied twice, in a window where under-calling costs 45p a kWh.

    One property to know before trusting the size of it. ``bias_kwh`` is measured
    against the forecast as *published*, which already carries whatever
    correction was applied last time, so the correction partly measures itself
    away: with an instantaneous window the map would flip between nothing and
    the full bias, and the rolling window damps that to roughly half. It settles
    low rather than high, which is the safe direction -- under-correcting leaves
    today's behaviour, over-correcting invents an empty house. Measuring the raw
    model instead would need the uncorrected figure recorded alongside the
    planned one, and is the fix if half ever proves too little.
    """
    if bias_kwh <= 0.0 or slots_measured < MIN_BIAS_SLOTS:
        # Under-calling is the evening allowance's business, and too little
        # measured is no business of anybody's yet.
        return [0.0] * len(loads)
    return [
        0.0 if is_evening(hour) else min(bias_kwh, load * MAX_BIAS_FRACTION)
        for hour, load in zip(hours, loads, strict=True)
    ]
