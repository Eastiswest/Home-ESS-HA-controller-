"""Cost-minimising battery dispatch by dynamic programming.

Why dynamic programming rather than linear programming: DP needs no external
solver (nothing to install, nothing to break on a Raspberry Pi), it handles the
awkward parts of a real tariff exactly -- negative Agile prices, export caps,
curtailment, round-trip losses, a wear allowance -- and it is trivially
deterministic. The cost of that is discretising the battery into levels, which
for a domestic pack is a rounding error next to forecast uncertainty.

The key structural insight that makes it fast in pure Python: the cost of a slot
depends only on *how much the battery charged or discharged*, never on the
absolute state of charge it started from. So for each slot we price every
possible energy delta once, then the DP sweep over levels is pure addition.
With a 36-hour horizon and 60 levels that is well under 100k operations.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

from ..const import (
    TERMINAL_MODE_FIXED,
    TERMINAL_MODE_HORIZON_MEAN,
    TERMINAL_MODE_HORIZON_MEDIAN,
    TERMINAL_MODE_ZERO,
)
from ..models import (
    BatterySpec,
    GridSpec,
    HorizonSlot,
    Plan,
    PlanSlot,
    SlotAction,
)

_LOGGER = logging.getLogger(__name__)

EPS = 1e-6
INF = math.inf

# Below this the usable window is too small to dispatch meaningfully, and the
# level discretisation would produce nonsense. Treat it as a misconfiguration.
MIN_USABLE_KWH = 0.05


@dataclass(slots=True)
class OptimiserSettings:
    """Knobs that shape the optimisation but are not physical limits."""

    soc_levels: int = 60
    terminal_mode: str = TERMINAL_MODE_HORIZON_MEDIAN
    terminal_rate: float = 0.0
    """Price in minor units per kWh used when ``terminal_mode`` is fixed."""
    terminal_weight: float = 1.0
    """Scales the value placed on energy left at the end of the horizon."""

    def clamped_levels(self) -> int:
        # Below ~20 levels the plan gets visibly blocky; above ~150 the runtime
        # stops being worth the extra precision.
        return int(min(max(self.soc_levels, 20), 150))


@dataclass(slots=True)
class _Flow:
    """Energy flows and cost resulting from one candidate battery delta."""

    charge_ac_kwh: float
    discharge_ac_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    curtailed_kwh: float
    cost: float
    action: SlotAction


def _price_delta(
    slot: HorizonSlot,
    delta_kwh: float,
    battery: BatterySpec,
    grid: GridSpec,
) -> _Flow | None:
    """Price a single candidate battery energy change for one slot.

    ``delta_kwh`` is measured at the cells: positive charges, negative
    discharges. Returns ``None`` when the delta violates a hard limit.
    """
    hours = slot.duration_hours
    if hours <= 0:
        return None

    if delta_kwh > EPS:
        # Losses mean the AC side must supply more than the cells receive.
        charge_ac = delta_kwh / battery.charge_efficiency
        discharge_ac = 0.0
        if charge_ac > battery.max_charge_kw * hours + EPS:
            return None
    elif delta_kwh < -EPS:
        # ...and the AC side receives less than the cells give up.
        discharge_ac = -delta_kwh * battery.discharge_efficiency
        charge_ac = 0.0
        if discharge_ac > battery.max_discharge_kw * hours + EPS:
            return None
    else:
        charge_ac = 0.0
        discharge_ac = 0.0

    pv = slot.pv_kwh
    load = slot.load_kwh
    surplus = pv - load if pv > load else 0.0
    deficit = load - pv if load > pv else 0.0

    if charge_ac > EPS and not grid.allow_grid_charge and charge_ac > surplus + EPS:
        return None

    if (
        discharge_ac > EPS
        and not grid.allow_battery_export
        and discharge_ac > deficit + EPS
    ):
        # The battery may cover the house but must not push into the grid.
        return None

    net = load + charge_ac - discharge_ac - pv

    if charge_ac > EPS and net > grid.import_limit_kw * hours + EPS:
        # Only reject when *we* caused the overshoot; a house load that exceeds
        # the connection limit on its own is not something we can plan away.
        return None

    grid_import = net if net > 0.0 else 0.0
    raw_export = -net if net < 0.0 else 0.0

    # Grid outflow is capped by the connection limit whether or not it earns
    # anything, and only *generation* can be curtailed: turning the array down is
    # possible, but battery discharge has to go somewhere. So the surplus a slot
    # cannot deliver must be absorbable by backing off PV, or the transition is
    # simply not achievable.
    # Refusing to export means the energy does not leave, not that it leaves for
    # nothing. Treating the permission as a price of zero was the wrong reading of
    # it: the plan happily pushed surplus and battery charge to the grid while the
    # switch said Export: off, and the only visible consequence was that it earned
    # nothing. With no capacity to export into, surplus has to be curtailed at the
    # array instead -- and a battery discharge bigger than the array could absorb
    # becomes infeasible, which is the physically honest answer.
    export_capacity = max(grid.export_limit_kw * hours, 0.0) if grid.allow_export else 0.0
    export_price = slot.export_price if grid.allow_export else 0.0

    if raw_export <= 0.0:
        grid_export = 0.0
        curtailed = 0.0
    else:
        if export_price < 0.0:
            # Being charged to export: shed as much as the array allows and only
            # deliver what cannot be shed.
            curtailed = min(raw_export, pv)
            grid_export = raw_export - curtailed
        else:
            # Earning, or earning nothing. Either way the energy leaves, up to
            # the connection limit; the remainder has to come off the array.
            grid_export = min(raw_export, export_capacity)
            curtailed = raw_export - grid_export
        if grid_export > export_capacity + EPS:
            return None
        if curtailed > pv + EPS:
            # More surplus than the array could account for, which means the
            # battery is being asked to discharge past the export limit.
            return None

    # A negative export price makes this a cost rather than revenue, which the
    # sign handles on its own.
    cost = grid_import * slot.import_price - grid_export * export_price
    # Giving generation away earns nothing and costs nothing, so on an
    # import-only site the objective was exactly indifferent between storing free
    # solar and losing it -- and an indifferent optimiser picks whichever branch
    # it happens to reach first. Both routes out count: curtailed at the array,
    # and exported at a price of zero, which is what an import-only tariff makes
    # of every surplus kWh. Weakly preferring to keep the energy costs nothing
    # real; the figure is far too small to outvote a genuine price or wear
    # difference, and only decides ties. A *negative* export price is already a
    # cost in its own right and needs no nudge.
    spilled = curtailed + (grid_export if export_price <= 0.0 else 0.0)
    cost += spilled * SPILL_TIE_BREAK
    # Wear allowance. Half is charged in each direction so that a full
    # round-trip of X kWh costs X * cycle_cost_per_kwh, which is how users
    # think about "cost per kWh cycled".
    cost += abs(delta_kwh) * battery.cycle_cost_per_kwh * 0.5

    if charge_ac > EPS:
        action = (
            SlotAction.CHARGE_SOLAR_ONLY
            if charge_ac <= surplus + EPS
            else SlotAction.CHARGE
        )
    elif discharge_ac > EPS:
        # Anything beyond the household's own shortfall needs a *forced*
        # discharge, whether or not the surplus earns anything. Requiring
        # grid_export here was a real bug: ahead of a negative-price window the
        # plan legitimately dumps charge to make headroom, and with no export
        # tariff that energy earns nothing -- so the slot was labelled self-use,
        # the inverter was left in self-use mode, and it discharged only enough
        # to cover the house. The headroom never materialised and the plan
        # silently failed to do the one thing it had decided to do.
        action = (
            SlotAction.DISCHARGE if discharge_ac > deficit + EPS else SlotAction.SELF_USE
        )
    else:
        action = SlotAction.IDLE

    return _Flow(
        charge_ac_kwh=charge_ac,
        discharge_ac_kwh=discharge_ac,
        grid_import_kwh=grid_import,
        grid_export_kwh=grid_export,
        curtailed_kwh=curtailed,
        cost=cost,
        action=action,
    )


# A ceiling on the refined level count, so a tiny household load cannot turn the
# sweep into something that takes seconds. Two hundred and forty levels over an
# 18 kWh window is 75 Wh of resolution, which is finer than any half-hour
# decision needs.
MAX_REFINED_LEVELS = 240


# Charged per kWh of generation the plan expects to give away for nothing --
# curtailed at the array, or exported at a price of zero -- purely to break ties
# in favour of keeping it. A hundredth of a penny is far below any real price or
# wear difference, so it can never outvote the economics.
SPILL_TIE_BREAK = 0.01


# Where on the horizon's price distribution stored energy is valued.
#
# ``replacement_cost`` uses the cheap end, because what a leftover kWh is worth is
# what it costs to put back. A low percentile rather than the bare minimum so a
# single free or negative half-hour cannot collapse the valuation to nothing.
TERMINAL_REPLACEMENT_FRACTION = 0.10

# ...and measured over the *tail* of the horizon, not all of it. The refill would
# happen after the horizon ends, so the prices that matter are the ones nearest
# that point. Reading the cheap end of the whole horizon is circular: on a day
# with a negative-price window it values stored energy at the very bargain it is
# currently exploiting, concludes energy is worthless, and declines to be paid to
# fill the battery. At least this many slots, so a short horizon still averages
# over something.
TERMINAL_TAIL_MIN_SLOTS = 6

# Half-hours in a day, for the windows that look "one day back from the end".
SLOTS_PER_DAY = 48
TERMINAL_MEDIAN_FRACTION = 0.5


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile, so a short horizon still gives an answer."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(fraction, 0.0), 1.0) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _smallest_useful_move(
    slots: list[HorizonSlot], battery: BatterySpec, grid: GridSpec
) -> float | None:
    """The finest cell-side movement the plan needs to be able to represent.

    Only discharge is considered, and only where the site may not push the
    battery into the grid: that is the case where the household's own demand is a
    hard cap on how much may leave the battery, and so the case where a coarse
    level grid silently forbids discharging at all. Charging has no such cap --
    surplus can always be topped up from the grid -- so it does not constrain the
    resolution.

    Returns ``None`` when nothing constrains it, meaning the configured
    resolution stands.
    """
    # Both permissions bind here, and checking only one of them left the bug
    # half-fixed. ``allow_battery_export`` is the direct statement that the
    # battery may not push to the grid; ``allow_export`` withdraws the export
    # capacity entirely, which caps battery discharge at the household demand just
    # as hard. A site with export refused but battery-export nominally allowed --
    # the ordinary import-only configuration -- kept the coarse grid and stayed
    # frozen, so the plan bought the house's power through the evening peak while
    # sitting on a charged battery.
    if grid.allow_battery_export and grid.allow_export:
        return None
    deficits = [
        (slot.load_kwh - slot.pv_kwh) / battery.discharge_efficiency
        for slot in slots
        if slot.load_kwh - slot.pv_kwh > EPS
    ]
    if not deficits:
        return None
    return min(deficits)


def _terminal_rate(slots: list[HorizonSlot], settings: OptimiserSettings) -> float:
    """Value per kWh assigned to energy still in the battery at horizon end.

    Without this the optimiser would empty the battery into the final slot,
    because energy has no value once the horizon stops.

    Valuing it at the horizon *mean* was a real and expensive mistake. On a peaky
    tariff the mean is dragged above almost every price on the horizon by a
    handful of evening half-hours: an Agile day averaging 25p is 16-21p for most
    of its length, so stored energy got booked at more than nearly every slot cost
    and buying looked profitable almost everywhere. The optimiser filled the pack
    and sat on it, and because an import-only site can empty a battery no faster
    than the house consumes, it bought far more than it could ever spend -- the
    house ran off the grid at 30p to protect a hoard bought at 21p. The profit was
    in the terminal credit, which is imaginary: the horizon rolls forward and that
    energy is never actually cashed in.

    The median was a large improvement and still not right. What a leftover kWh is
    worth is not what a typical half-hour costs -- it is what it costs to *put
    back*, which is the cheap end of the horizon. Replayed against a real
    twenty-four hour horizon from a working install, the median valuation bought
    7.7 kWh and spent 149p; valuing the remainder at the cheap end bought 1.4 kWh
    and spent 39p, ending the day at much the same charge. The 110p difference was
    energy bought at a premium to sit in the pack.

    ``replacement_cost`` therefore takes a low percentile of the *tail* of the
    horizon: the cheap end, but not the bare minimum, so a single free half-hour
    cannot collapse the valuation to nothing -- and the tail rather than the whole
    horizon, because the refill happens after the horizon ends and reading the
    cheap end of the whole thing is circular. Measured over everything, a day with
    a negative-price window values stored energy at the very bargain it is
    exploiting, decides energy is worthless, and declines to be paid to fill the
    battery. ``horizon_median`` and ``horizon_mean`` remain selectable.

    Clamped at zero. On a heavily negative day the horizon rate can itself go
    negative, which would make stored energy a *liability* and drive the plan to
    empty the pack at the horizon end for no reason. Energy in a battery is never
    worth less than nothing: you are never obliged to pay to keep it.
    """
    if settings.terminal_mode == TERMINAL_MODE_ZERO:
        return 0.0
    if settings.terminal_mode == TERMINAL_MODE_FIXED:
        return max(settings.terminal_rate, 0.0)
    if not slots:
        return 0.0
    prices = [s.import_price for s in slots]
    if settings.terminal_mode == TERMINAL_MODE_HORIZON_MEAN:
        return max(sum(prices) / len(prices), 0.0)
    if settings.terminal_mode == TERMINAL_MODE_HORIZON_MEDIAN:
        return max(percentile(prices, TERMINAL_MEDIAN_FRACTION), 0.0)
    tail = prices[-max(len(prices) // 2, TERMINAL_TAIL_MIN_SLOTS) :]
    return max(percentile(tail, TERMINAL_REPLACEMENT_FRACTION), 0.0)


def _terminal_energy_cap(slots: list[HorizonSlot]) -> float:
    """How much of the battery's charge is worth crediting at the horizon end.

    The price of leftover energy was never the real problem; the *quantity* was.
    Whatever rate is chosen, crediting the whole pack assumes every kWh in it will
    eventually be needed -- and on a site whose array nearly covers its load, it
    will not. Tomorrow's sun displaces it. A real horizon from a working install
    showed the plan buying 7.7 kWh for 149p against 8p for doing nothing, because
    energy it would never use was booked at a typical half-hourly price.

    So the credit is capped at the shortfall the site is actually forecast to have:
    load it cannot meet from the array over the last day of the horizon. In winter,
    with no solar, that is the whole pack and the valuation is unchanged. In summer
    it is close to nothing, and the plan stops buying what the sun will give it.

    Measured over the last day rather than the whole horizon because that is the
    part that speaks to what happens after it ends.
    """
    if not slots:
        return 0.0
    window = slots[-min(len(slots), SLOTS_PER_DAY) :]
    return max(sum(s.load_kwh - s.pv_kwh for s in window), 0.0)


def optimise(
    slots: list[HorizonSlot],
    start_soc: float,
    battery: BatterySpec,
    grid: GridSpec,
    settings: OptimiserSettings | None = None,
    created: datetime | None = None,
) -> Plan:
    """Return the least-cost dispatch plan across ``slots``."""
    settings = settings or OptimiserSettings()
    created = created or (slots[0].start if slots else datetime.now())

    plan = Plan(created=created)
    if not slots:
        plan.reason = "No tariff data available for the horizon"
        plan.infeasible = True
        return plan

    usable = battery.usable_kwh
    if usable < MIN_USABLE_KWH:
        plan.reason = f"Usable capacity is only {usable:.3f} kWh; check the SoC limits"
        plan.infeasible = True
        return plan

    levels = settings.clamped_levels()
    step = usable / levels
    # The level grid has to be fine enough to express the single most important
    # move on an import-only site: covering the house from the battery. It was
    # not, and the consequence was severe.
    #
    # A site that may not export can only discharge as much as the house is
    # using, so a transition whose discharge exceeds that half-hour's demand is
    # rejected as infeasible -- the energy would have nowhere to go. With sixty
    # levels over an 18 kWh window each step is ~0.3 kWh, and a house drawing
    # 0.5 kW uses 0.25 kWh in a half-hour. Every discharge transition was
    # therefore illegal and the battery was frozen: the plan sat on a full pack
    # and bought the load at the evening peak, because *buying* was the only move
    # the grid could represent. Refining the grid to the smallest useful movement
    # fixes it without making the sweep expensive.
    finest = _smallest_useful_move(slots, battery, grid)
    if finest is not None and step > finest:
        levels = min(math.ceil(usable / finest), MAX_REFINED_LEVELS)
        step = usable / levels
    n = len(slots)

    start_energy = battery.soc_to_energy(start_soc)
    # Floored, not rounded. Rounding to the nearest level can start the plan up
    # to half a level *above* where the battery actually is, so the plan spends
    # energy it does not have and quietly undershoots its floor. Optimism about
    # the starting charge is the one direction this must not err in.
    start_level = math.floor(start_energy / step)
    start_level = min(max(start_level, 0), levels)
    # The level grid cannot land exactly on the measured charge, and the baselines
    # have to be judged from the same starting energy as the plan or the reported
    # saving carries a quantisation artefact rather than a decision: at a flat
    # price the plan looked *worse* than plain self-consumption purely because the
    # baseline was handed the fraction of a kWh the grid had rounded away.
    quantised_start_soc = battery.energy_to_soc(start_level * step)

    # --- terminal valuation ------------------------------------------------
    rate = _terminal_rate(slots, settings) * settings.terminal_weight
    # Only the energy that survives the inverter on the way out has value.
    value_per_kwh = rate * battery.discharge_efficiency
    credit_cap = _terminal_energy_cap(slots)
    future = [-min(j * step, credit_cap) * value_per_kwh for j in range(levels + 1)]

    # --- backward sweep ----------------------------------------------------
    choices: list[list[int]] = [[-1] * (levels + 1) for _ in range(n)]

    for i in range(n - 1, -1, -1):
        slot = slots[i]
        hours = slot.duration_hours

        max_up = int((battery.max_charge_kw * hours * battery.charge_efficiency) / step)
        max_down = int(
            (battery.max_discharge_kw * hours / battery.discharge_efficiency) / step
        )
        max_up = min(max_up, levels)
        max_down = min(max_down, levels)

        # Price every reachable delta once for this slot.
        priced: list[tuple[int, float]] = []
        for offset in range(-max_down, max_up + 1):
            flow = _price_delta(slot, offset * step, battery, grid)
            if flow is not None:
                priced.append((offset, flow.cost))

        current = [INF] * (levels + 1)
        slot_choices = choices[i]
        for j in range(levels + 1):
            best = INF
            best_k = -1
            for offset, cost in priced:
                k = j + offset
                if k < 0 or k > levels:
                    continue
                candidate = cost + future[k]
                # Strictly ``<``, which breaks exact ties towards the more
                # discharged state. That is deliberate. At a flat price the
                # terminal value of holding a kWh exactly equals the import it
                # would avoid, so the two are a true tie -- and ``total_cost``
                # excludes terminal value, so preferring to hold would report a
                # cost worse than plain self-consumption while quietly banking
                # energy. Any real preference for holding charge belongs in the
                # wear allowance or the reserve, where the user can see it.
                if candidate < best:
                    best = candidate
                    best_k = k
            current[j] = best
            slot_choices[j] = best_k

        future = current

    if future[start_level] == INF or choices[0][start_level] < 0:
        # Should be unreachable: holding the battery is always priced, so at
        # minimum the idle transition survives. Fall back rather than crash.
        _LOGGER.warning("Optimiser found no feasible plan; falling back to self-use")
        fallback = simulate_self_use(slots, start_soc, battery, grid, created)
        fallback.reason = "No feasible plan found; using self-consumption"
        fallback.infeasible = True
        return fallback

    # --- forward reconstruction -------------------------------------------
    level = start_level
    total_cost = 0.0
    for i, slot in enumerate(slots):
        next_level = choices[i][level]
        if next_level < 0:
            next_level = level
        delta = (next_level - level) * step
        flow = _price_delta(slot, delta, battery, grid)
        if flow is None:  # pragma: no cover - defensive
            flow = _price_delta(slot, 0.0, battery, grid)
            next_level = level
            delta = 0.0
        assert flow is not None
        soc_start = battery.energy_to_soc(level * step)
        soc_end = battery.energy_to_soc(next_level * step)
        plan.slots.append(
            PlanSlot(
                start=slot.start,
                end=slot.end,
                action=flow.action,
                import_price=slot.import_price,
                export_price=slot.export_price,
                pv_kwh=slot.pv_kwh,
                load_kwh=slot.load_kwh,
                battery_delta_kwh=delta,
                charge_ac_kwh=flow.charge_ac_kwh,
                discharge_ac_kwh=flow.discharge_ac_kwh,
                grid_import_kwh=flow.grid_import_kwh,
                grid_export_kwh=flow.grid_export_kwh,
                curtailed_kwh=flow.curtailed_kwh,
                soc_start=soc_start,
                soc_end=soc_end,
                cost=flow.cost,
                price_is_forecast=slot.price_is_forecast,
            )
        )
        total_cost += flow.cost
        level = next_level

    plan.total_cost = total_cost
    plan.terminal_value = min(level * step, credit_cap) * value_per_kwh
    plan.baseline_cost = simulate_idle(
        slots, quantised_start_soc, battery, grid, created
    ).total_cost
    self_use = simulate_self_use(slots, quantised_start_soc, battery, grid, created)
    plan.self_use_cost = self_use.total_cost

    # Never hand back a plan that is worse than leaving the inverter alone.
    #
    # The optimiser is only optimal with respect to its own model, and when the
    # model has been wrong the result has been expensive: a real twenty-four hour
    # horizon produced a plan costing 203p against 8p for plain self-consumption,
    # because energy banked past the horizon was credited at more than it could
    # ever be worth. Both sides are scored the same way here -- realised cost
    # minus what is left in the battery, valued identically -- so a plan only
    # survives if it genuinely beats doing nothing clever. Discretising the
    # battery into levels also means the DP cannot always express the continuous
    # self-use trajectory exactly, and this catches that too.
    self_use.terminal_value = (
        min(battery.soc_to_energy(self_use.slots[-1].soc_end), credit_cap) * value_per_kwh
        if self_use.slots
        else 0.0
    )
    if self_use.net_cost < plan.net_cost - EPS:
        self_use.baseline_cost = plan.baseline_cost
        self_use.self_use_cost = plan.self_use_cost
        self_use.reason = (
            "Self-consumption is cheaper than anything worth scheduling here"
        )
        return self_use

    plan.reason = _describe(plan, battery)
    return plan


def _simulate(
    slots: list[HorizonSlot],
    start_soc: float,
    battery: BatterySpec,
    grid: GridSpec,
    created: datetime | None,
    policy,
) -> Plan:
    """Run a fixed policy over the horizon, for comparison against the plan."""
    created = created or (slots[0].start if slots else datetime.now())
    plan = Plan(created=created)
    energy = battery.soc_to_energy(start_soc)
    usable = battery.usable_kwh
    total = 0.0

    for slot in slots:
        delta = policy(slot, energy, usable, battery, grid)
        flow = _price_delta(slot, delta, battery, grid)
        if flow is None:
            delta = 0.0
            flow = _price_delta(slot, 0.0, battery, grid)
        if flow is None:  # pragma: no cover - defensive
            continue
        soc_start = battery.energy_to_soc(energy)
        energy = min(max(energy + delta, 0.0), usable)
        plan.slots.append(
            PlanSlot(
                start=slot.start,
                end=slot.end,
                action=flow.action,
                import_price=slot.import_price,
                export_price=slot.export_price,
                pv_kwh=slot.pv_kwh,
                load_kwh=slot.load_kwh,
                battery_delta_kwh=delta,
                charge_ac_kwh=flow.charge_ac_kwh,
                discharge_ac_kwh=flow.discharge_ac_kwh,
                grid_import_kwh=flow.grid_import_kwh,
                grid_export_kwh=flow.grid_export_kwh,
                curtailed_kwh=flow.curtailed_kwh,
                soc_start=soc_start,
                soc_end=battery.energy_to_soc(energy),
                cost=flow.cost,
                price_is_forecast=slot.price_is_forecast,
            )
        )
        total += flow.cost

    plan.total_cost = total
    plan.baseline_cost = total
    plan.self_use_cost = total
    return plan


def simulate_idle(
    slots: list[HorizonSlot],
    start_soc: float,
    battery: BatterySpec,
    grid: GridSpec,
    created: datetime | None = None,
) -> Plan:
    """The do-nothing counterfactual: battery held, grid covers everything."""
    return _simulate(slots, start_soc, battery, grid, created, lambda *_: 0.0)


def _self_use_policy(
    slot: HorizonSlot,
    energy: float,
    usable: float,
    battery: BatterySpec,
    grid: GridSpec,
) -> float:
    """Classic self-consumption: soak up surplus PV, cover any deficit."""
    hours = slot.duration_hours
    if slot.pv_kwh > slot.load_kwh:
        surplus = slot.pv_kwh - slot.load_kwh
        room = usable - energy
        gain = min(
            surplus * battery.charge_efficiency,
            battery.max_charge_kw * hours * battery.charge_efficiency,
            room,
        )
        return max(gain, 0.0)
    deficit = slot.load_kwh - slot.pv_kwh
    drain = min(
        deficit / battery.discharge_efficiency,
        battery.max_discharge_kw * hours / battery.discharge_efficiency,
        energy,
    )
    return -max(drain, 0.0)


def simulate_self_use(
    slots: list[HorizonSlot],
    start_soc: float,
    battery: BatterySpec,
    grid: GridSpec,
    created: datetime | None = None,
) -> Plan:
    """Plain self-consumption, the behaviour of an unmanaged hybrid inverter."""
    return _simulate(slots, start_soc, battery, grid, created, _self_use_policy)


def _describe(plan: Plan, battery: BatterySpec) -> str:
    """A short human-readable summary of what the plan does and why.

    This is what makes the optimiser auditable: the sensor carries a sentence
    a user can sanity-check against the tariff without reading the whole plan.
    """
    charge_slots = [s for s in plan.slots if s.action is SlotAction.CHARGE]
    discharge_slots = [s for s in plan.slots if s.action is SlotAction.DISCHARGE]
    parts: list[str] = []

    if charge_slots:
        energy = sum(s.charge_ac_kwh for s in charge_slots)
        avg = sum(s.import_price * s.charge_ac_kwh for s in charge_slots) / max(
            energy, EPS
        )
        parts.append(
            f"grid-charge {energy:.1f} kWh over {len(charge_slots)} slots "
            f"at avg {avg:.1f}p"
        )
    if discharge_slots:
        energy = sum(s.grid_export_kwh for s in discharge_slots)
        if energy > EPS:
            avg = sum(s.export_price * s.grid_export_kwh for s in discharge_slots) / max(
                energy, EPS
            )
            parts.append(f"export {energy:.1f} kWh at avg {avg:.1f}p")
        else:
            parts.append(f"discharge across {len(discharge_slots)} slots")

    solar_charge = sum(
        s.charge_ac_kwh for s in plan.slots if s.action is SlotAction.CHARGE_SOLAR_ONLY
    )
    if solar_charge > 0.1:
        parts.append(f"store {solar_charge:.1f} kWh of surplus PV")

    curtailed = sum(s.curtailed_kwh for s in plan.slots)
    if curtailed > 0.1:
        parts.append(f"spill {curtailed:.1f} kWh")

    if not parts:
        spread = battery.spread_needed_to_cycle()
        parts.append(
            f"hold battery: no price spread beats the {spread:.1f}p/kWh round-trip cost"
        )

    saving = plan.saving_vs_self_use
    summary = "; ".join(parts)
    return f"{summary}. Saves {saving:.0f}p vs self-use over the horizon."
