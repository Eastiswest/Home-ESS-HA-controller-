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
    terminal_mode: str = TERMINAL_MODE_HORIZON_MEAN
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
    export_capacity = max(grid.export_limit_kw * hours, 0.0)
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


def _terminal_rate(slots: list[HorizonSlot], settings: OptimiserSettings) -> float:
    """Value per kWh assigned to energy still in the battery at horizon end.

    Without this the optimiser would empty the battery into the final slot,
    because energy has no value once the horizon stops. Valuing the remainder
    at the horizon's mean import price is a neutral choice: it neither hoards
    nor dumps.

    Clamped at zero. On a heavily negative day the horizon mean can itself go
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
    mean = sum(s.import_price for s in slots) / len(slots)
    return max(mean, 0.0)


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
    n = len(slots)

    start_energy = battery.soc_to_energy(start_soc)
    start_level = round(start_energy / step)
    start_level = min(max(start_level, 0), levels)

    # --- terminal valuation ------------------------------------------------
    rate = _terminal_rate(slots, settings) * settings.terminal_weight
    # Only the energy that survives the inverter on the way out has value.
    value_per_kwh = rate * battery.discharge_efficiency
    future = [-(j * step) * value_per_kwh for j in range(levels + 1)]

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
    plan.terminal_value = (level * step) * value_per_kwh
    plan.baseline_cost = simulate_idle(
        slots, start_soc, battery, grid, created
    ).total_cost
    plan.self_use_cost = simulate_self_use(
        slots, start_soc, battery, grid, created
    ).total_cost
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
