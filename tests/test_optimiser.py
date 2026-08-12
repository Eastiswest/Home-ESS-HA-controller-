"""Tests for the dynamic-programming dispatch optimiser."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.ess_controller.models import (
    BatterySpec,
    GridSpec,
    HorizonSlot,
    SlotAction,
)
from custom_components.ess_controller.optimiser.dp import (
    OptimiserSettings,
    optimise,
    simulate_idle,
    simulate_self_use,
)

START = datetime(2026, 1, 15, 0, 0)


def make_battery(**kwargs) -> BatterySpec:
    defaults = dict(
        capacity_kwh=22.0,
        min_soc=15.0,
        max_soc=95.0,
        max_charge_kw=3.6,
        max_discharge_kw=3.6,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        cycle_cost_per_kwh=0.0,
    )
    defaults.update(kwargs)
    return BatterySpec(**defaults)


def make_grid(**kwargs) -> GridSpec:
    defaults = dict(
        import_limit_kw=15.0,
        export_limit_kw=3.68,
        allow_export=True,
        allow_grid_charge=True,
        allow_battery_export=True,
    )
    defaults.update(kwargs)
    return GridSpec(**defaults)


def build_slots(
    prices: list[float],
    *,
    export: list[float] | None = None,
    pv: float = 0.0,
    load: float = 0.25,
) -> list[HorizonSlot]:
    slots = []
    for index, price in enumerate(prices):
        start = START + timedelta(minutes=30 * index)
        slots.append(
            HorizonSlot(
                start=start,
                end=start + timedelta(minutes=30),
                import_price=price,
                export_price=(export[index] if export else 0.0),
                pv_kwh=pv,
                load_kwh=load,
            )
        )
    return slots


class TestBatterySpec:
    def test_usable_window(self):
        battery = make_battery()
        assert battery.usable_kwh == pytest.approx(22.0 * 0.80)

    def test_soc_energy_roundtrip(self):
        battery = make_battery()
        for soc in (15.0, 40.0, 95.0):
            energy = battery.soc_to_energy(soc)
            assert battery.energy_to_soc(energy) == pytest.approx(soc)

    def test_soc_clamped_to_window(self):
        battery = make_battery()
        assert battery.soc_to_energy(5.0) == 0.0
        assert battery.soc_to_energy(99.0) == pytest.approx(battery.usable_kwh)

    def test_rejects_bad_limits(self):
        with pytest.raises(ValueError):
            make_battery(min_soc=90.0, max_soc=20.0)
        with pytest.raises(ValueError):
            make_battery(capacity_kwh=0.0)
        with pytest.raises(ValueError):
            make_battery(charge_efficiency=1.5)


class TestFlatTariff:
    """With a flat price there is nothing to arbitrage."""

    def test_flat_price_holds_battery(self):
        slots = build_slots([25.0] * 24, load=0.3)
        plan = optimise(slots, 50.0, make_battery(), make_grid())
        assert not plan.infeasible
        # Discharging to cover load is free of arbitrage value at a flat price,
        # but round-trip losses mean charging then discharging is never a win.
        assert plan.window_energy(SlotAction.CHARGE) == pytest.approx(0.0, abs=0.01)

    def test_flat_price_never_worse_than_self_use(self):
        slots = build_slots([25.0] * 48, load=0.4)
        plan = optimise(slots, 60.0, make_battery(), make_grid())
        assert plan.total_cost <= plan.self_use_cost + 1e-6


class TestArbitrage:
    def test_charges_cheap_discharges_expensive(self):
        # 6 hours cheap overnight, then 6 hours expensive.
        prices = [8.0] * 12 + [40.0] * 12
        slots = build_slots(prices, load=0.5)
        plan = optimise(slots, 15.0, make_battery(), make_grid())

        cheap = [s for s in plan.slots if s.import_price == 8.0]
        pricey = [s for s in plan.slots if s.import_price == 40.0]

        assert sum(s.charge_ac_kwh for s in cheap) > 5.0
        assert sum(s.charge_ac_kwh for s in pricey) == pytest.approx(0.0, abs=0.01)
        assert sum(s.discharge_ac_kwh for s in pricey) > 3.0
        assert plan.total_cost < plan.baseline_cost

    def test_respects_charge_power_limit(self):
        prices = [5.0] * 4 + [50.0] * 8
        slots = build_slots(prices, load=0.5)
        battery = make_battery(max_charge_kw=2.0)
        plan = optimise(slots, 15.0, battery, make_grid())
        for slot in plan.slots:
            # 2 kW for half an hour is 1 kWh maximum.
            assert slot.charge_ac_kwh <= 1.0 + 1e-6

    def test_respects_soc_ceiling(self):
        prices = [1.0] * 48
        slots = build_slots(prices, load=0.0)
        battery = make_battery(max_soc=80.0)
        plan = optimise(slots, 15.0, battery, make_grid())
        assert max(s.soc_end for s in plan.slots) <= 80.0 + 1e-6

    def test_never_drains_below_min_soc(self):
        prices = [50.0] * 48
        slots = build_slots(prices, load=1.0)
        battery = make_battery(min_soc=20.0)
        plan = optimise(slots, 90.0, battery, make_grid())
        assert min(s.soc_end for s in plan.slots) >= 20.0 - 1e-6

    def test_cycle_cost_suppresses_marginal_arbitrage(self):
        # A 4p spread cannot pay for a 10p/kWh wear allowance.
        prices = [20.0] * 12 + [24.0] * 12
        slots = build_slots(prices, load=0.3)
        expensive = make_battery(cycle_cost_per_kwh=10.0)
        plan = optimise(slots, 15.0, expensive, make_grid())
        assert plan.window_energy(SlotAction.CHARGE) == pytest.approx(0.0, abs=0.01)

        # ...but a 30p spread comfortably can.
        prices = [10.0] * 12 + [40.0] * 12
        slots = build_slots(prices, load=0.3)
        plan = optimise(slots, 15.0, expensive, make_grid())
        assert plan.window_energy(SlotAction.CHARGE) > 3.0


class TestNegativePrices:
    def test_paid_to_import_fills_battery(self):
        prices = [-5.0] * 6 + [30.0] * 12
        slots = build_slots(prices, load=0.3)
        plan = optimise(slots, 15.0, make_battery(), make_grid())
        negative = [s for s in plan.slots if s.import_price < 0]
        assert sum(s.charge_ac_kwh for s in negative) > 4.0

    def test_negative_export_price_curtails_rather_than_pays(self):
        slots = build_slots([20.0] * 8, export=[-10.0] * 8, pv=2.0, load=0.1)
        plan = optimise(slots, 95.0, make_battery(), make_grid())
        # Battery is full and PV exceeds load; exporting would cost money.
        assert all(s.grid_export_kwh == pytest.approx(0.0) for s in plan.slots)
        assert sum(s.curtailed_kwh for s in plan.slots) > 0.0


class TestExportBehaviour:
    def test_no_export_tariff_means_no_export_revenue(self):
        slots = build_slots([30.0] * 12, export=[0.0] * 12, pv=0.0, load=0.2)
        plan = optimise(slots, 95.0, make_battery(), make_grid(allow_export=False))
        assert all(s.grid_export_kwh == pytest.approx(0.0) for s in plan.slots)
        assert all(s.action is not SlotAction.DISCHARGE for s in plan.slots)

    def test_export_arbitrage_when_spread_is_large(self):
        # Cheap import overnight, generous export later.
        prices = [5.0] * 12 + [30.0] * 12
        export = [4.0] * 12 + [28.0] * 12
        slots = build_slots(prices, export=export, load=0.2)
        plan = optimise(slots, 15.0, make_battery(), make_grid())
        assert sum(s.grid_export_kwh for s in plan.slots) > 1.0

    def test_export_limit_caps_discharge_revenue(self):
        slots = build_slots([30.0] * 8, export=[30.0] * 8, pv=0.0, load=0.0)
        grid = make_grid(export_limit_kw=1.0)
        plan = optimise(slots, 95.0, make_battery(), grid)
        for slot in plan.slots:
            assert slot.grid_export_kwh <= 0.5 + 1e-6

    def test_battery_export_disabled_only_covers_load(self):
        slots = build_slots([30.0] * 8, export=[30.0] * 8, pv=0.0, load=0.4)
        grid = make_grid(allow_battery_export=False)
        plan = optimise(slots, 95.0, make_battery(), grid)
        for slot in plan.slots:
            assert slot.discharge_ac_kwh <= slot.load_kwh + 1e-6


class TestGridChargeDisabled:
    def test_solar_only_charging(self):
        slots = build_slots([5.0] * 12, pv=1.0, load=0.2)
        grid = make_grid(allow_grid_charge=False)
        plan = optimise(slots, 20.0, make_battery(), grid)
        for slot in plan.slots:
            surplus = max(slot.pv_kwh - slot.load_kwh, 0.0)
            assert slot.charge_ac_kwh <= surplus + 1e-6
            assert slot.action is not SlotAction.CHARGE

    def test_stores_surplus_pv(self):
        slots = build_slots([5.0] * 12, pv=1.0, load=0.2)
        grid = make_grid(allow_grid_charge=False)
        plan = optimise(slots, 20.0, make_battery(), grid)
        assert sum(s.charge_ac_kwh for s in plan.slots) > 1.0


class TestTerminalValue:
    def test_does_not_dump_battery_at_horizon_end(self):
        # Flat price with no load: there is no reason to move energy at all.
        slots = build_slots([25.0] * 24, pv=0.0, load=0.0)
        plan = optimise(slots, 80.0, make_battery(), make_grid())
        assert plan.slots[-1].soc_end == pytest.approx(80.0, abs=2.0)

    def test_zero_terminal_value_empties_battery_when_export_pays(self):
        slots = build_slots([25.0] * 24, export=[25.0] * 24, pv=0.0, load=0.0)
        settings = OptimiserSettings(
            terminal_mode="zero", terminal_rate=0.0, soc_levels=40
        )
        plan = optimise(slots, 90.0, make_battery(), make_grid(), settings)
        # With end-state energy valued at nothing, selling it all is correct.
        assert plan.slots[-1].soc_end < 30.0


class TestCounterfactuals:
    def test_idle_baseline_moves_no_energy(self):
        slots = build_slots([25.0] * 24, load=0.5)
        plan = simulate_idle(slots, 50.0, make_battery(), make_grid())
        assert all(s.battery_delta_kwh == pytest.approx(0.0) for s in plan.slots)
        assert plan.total_cost == pytest.approx(24 * 0.5 * 25.0)

    def test_self_use_covers_load_from_battery(self):
        slots = build_slots([25.0] * 12, pv=0.0, load=0.5)
        plan = simulate_self_use(slots, 90.0, make_battery(), make_grid())
        assert all(s.grid_import_kwh == pytest.approx(0.0) for s in plan.slots)
        assert plan.total_cost == pytest.approx(0.0)

    def test_optimiser_beats_or_matches_self_use(self):
        prices = [8.0] * 12 + [35.0] * 12 + [15.0] * 12 + [45.0] * 12
        slots = build_slots(prices, pv=0.1, load=0.4)
        plan = optimise(slots, 40.0, make_battery(), make_grid())
        assert plan.total_cost <= plan.self_use_cost + 1e-6
        assert plan.total_cost <= plan.baseline_cost + 1e-6


class TestEdgeCases:
    def test_empty_horizon_is_infeasible_not_a_crash(self):
        plan = optimise([], 50.0, make_battery(), make_grid())
        assert plan.infeasible
        assert plan.slots == []

    def test_zero_usable_capacity_is_reported(self):
        battery = make_battery(min_soc=50.0, max_soc=50.0001)
        plan = optimise(build_slots([25.0] * 4), 50.0, battery, make_grid())
        assert plan.infeasible

    def test_partial_first_slot(self):
        # The live horizon starts mid-slot, so slot one is short.
        slots = build_slots([10.0] * 6, load=0.4)
        slots[0].start = slots[0].end - timedelta(minutes=7)
        plan = optimise(slots, 30.0, make_battery(), make_grid())
        first = plan.slots[0]
        assert first.duration_hours == pytest.approx(7 / 60)
        # Charge energy must respect the shortened window.
        assert first.charge_ac_kwh <= 3.6 * (7 / 60) + 1e-6

    def test_plan_reason_is_populated(self):
        prices = [8.0] * 12 + [40.0] * 12
        plan = optimise(build_slots(prices, load=0.5), 15.0, make_battery(), make_grid())
        assert plan.reason
        assert "vs self-use" in plan.reason

    def test_soc_trajectory_is_continuous(self):
        prices = [8.0] * 12 + [40.0] * 12 + [12.0] * 12
        plan = optimise(build_slots(prices, load=0.4), 30.0, make_battery(), make_grid())
        for previous, following in pairwise(plan.slots):
            assert previous.soc_end == pytest.approx(following.soc_start)


class TestNegativePricePreparation:
    """Ahead of a paid-to-import window the battery must make headroom, so it can
    soak up as much as possible while being paid to take it."""

    def _horizon(self, export_price: float = 0.0):
        # A full day, because the value of a full battery rests on the load ahead
        # of it: over six hours there is nothing for a hoard to be spent on, and a
        # plan judged on six hours cannot be expected to fill one.
        prices = [20.0] * 6 + [-10.0] * 4 + [30.0] * 38
        return build_slots(prices, export=[export_price] * 48, load=0.3)

    def test_discharges_before_a_negative_window(self):
        plan = optimise(self._horizon(5.0), 90.0, make_battery(), make_grid())
        entering = plan.slots[5].soc_end
        # Started at 90%; must have made meaningful room.
        assert entering < 75.0

    def test_imports_hard_through_the_negative_window(self):
        plan = optimise(self._horizon(5.0), 90.0, make_battery(), make_grid())
        negative = [s for s in plan.slots if s.import_price < 0]
        # 3.6 kW for four half-hours is 7.2 kWh of charging headroom used.
        assert sum(s.charge_ac_kwh for s in negative) > 5.5
        # And it earns money doing so.
        assert sum(s.cost for s in negative) < 0

    def test_makes_room_even_with_no_export_tariff(self):
        """With export worth nothing, dumping charge earns nothing -- but the
        headroom it buys is still worth more than the energy given away.

        The surplus leaves as unpaid export, not curtailment: you can turn an
        array down, but battery discharge has to go somewhere.

        "No export tariff" is ``allow_export`` *on* with a price of zero -- the
        energy may physically leave, it just is not paid for. ``allow_export=False``
        is the different and stronger claim that nothing leaves at all.
        """
        grid = make_grid(allow_export=True)
        plan = optimise(self._horizon(0.0), 90.0, make_battery(), grid)
        assert plan.slots[5].soc_end < 75.0
        assert sum(s.grid_export_kwh for s in plan.slots) > 0.5
        # No PV in this horizon, so nothing is curtailable and nothing should be
        # reported as curtailed.
        assert sum(s.curtailed_kwh for s in plan.slots) == pytest.approx(0.0)
        # And it earns nothing for that export.
        assert all(s.export_price == 0.0 for s in plan.slots)

    def test_free_export_respects_the_connection_limit(self):
        """Regression: with export earning nothing the limit was not applied at
        all, so the plan could push 5.6 kW out of a 3.68 kW connection. The
        inverter would clamp it and the planned SoC would never be reached."""
        prices = [-20.0] * 8
        slots = build_slots(prices, export=[0.0] * 8, load=0.3, pv=0.0)
        grid = make_grid(export_limit_kw=1.0, allow_export=False)
        plan = optimise(slots, 90.0, make_battery(), grid)
        for slot in plan.slots:
            # 1 kW for half an hour is 0.5 kWh of deliverable export.
            assert slot.grid_export_kwh <= 0.5 + 1e-6
            # Discharge cannot exceed what the house takes plus what can leave.
            assert slot.discharge_ac_kwh <= slot.load_kwh + 0.5 + 1e-6

    def test_pre_emptive_dump_is_labelled_discharge_not_self_use(self):
        """Regression: discharging beyond the household load was labelled
        self-use whenever the surplus earned nothing, so the adapter left the
        inverter in self-use mode and it only covered the house. The headroom
        never appeared and the plan silently failed."""
        grid = make_grid(allow_export=True)
        plan = optimise(self._horizon(0.0), 90.0, make_battery(), grid)
        dumping = [
            s
            for s in plan.slots
            if s.discharge_ac_kwh > s.load_kwh + 1e-6 and s.import_price > 0
        ]
        assert dumping, "expected slots discharging beyond household load"
        for slot in dumping:
            assert slot.action is SlotAction.DISCHARGE

    def test_ends_the_negative_window_full(self):
        plan = optimise(self._horizon(5.0), 90.0, make_battery(), make_grid())
        assert plan.slots[9].soc_end > 90.0

    def test_battery_export_disabled_limits_the_headroom(self):
        """Documented consequence: if the battery may not push to the grid it can
        only empty into the house, so it captures far less."""
        free = optimise(self._horizon(5.0), 90.0, make_battery(), make_grid())
        restricted = optimise(
            self._horizon(5.0),
            90.0,
            make_battery(),
            make_grid(allow_battery_export=False),
        )
        negative_free = sum(s.charge_ac_kwh for s in free.slots if s.import_price < 0)
        negative_restricted = sum(
            s.charge_ac_kwh for s in restricted.slots if s.import_price < 0
        )
        assert negative_restricted < negative_free
        assert restricted.slots[5].soc_end > free.slots[5].soc_end


class TestTerminalValueClamp:
    def test_negative_horizon_mean_does_not_make_energy_a_liability(self):
        """Regression: an all-negative horizon gave a negative terminal rate, so
        stored energy was penalised and the plan threw the pack away at the end.

        Being paid to import should fill the battery; it should certainly not
        end at the floor.
        """
        slots = build_slots([-8.0] * 16, load=0.3)
        battery = make_battery(cycle_cost_per_kwh=2.0)
        plan = optimise(slots, 60.0, battery, make_grid(allow_export=False))
        assert plan.slots[-1].soc_end > 90.0
        assert sum(s.charge_ac_kwh for s in plan.slots) > 3.0

    def test_wear_allowance_governs_spill_to_reimport(self):
        """At a deeply negative price, dumping a kWh to re-import it is real
        arbitrage: it earns the negative rate and costs only wear. The wear
        allowance is what decides whether that is worth doing, so it is the dial
        to reach for if the plan cycles harder than you want.
        """
        slots = build_slots([-8.0] * 16, load=0.3)
        # Permitted to export, paid nothing for it: the ordinary import-only case.
        grid = make_grid(allow_export=True)

        cheap_wear = optimise(slots, 60.0, make_battery(cycle_cost_per_kwh=2.0), grid)
        dear_wear = optimise(slots, 60.0, make_battery(cycle_cost_per_kwh=8.0), grid)

        # 2p/kWh wear against an 8p/kWh reward: worth cycling for. The dumped
        # energy leaves as unpaid export.
        assert sum(s.grid_export_kwh for s in cheap_wear.slots) > 1.0
        # 8p/kWh wear: no longer worth it, so it simply fills and holds.
        assert sum(s.grid_export_kwh for s in dear_wear.slots) == pytest.approx(0.0)
        # Either way it ends full, because import is being paid for.
        assert dear_wear.slots[-1].soc_end > 90.0

    def test_negative_fixed_terminal_rate_clamped(self):
        """A negative rate would penalise holding charge, making the plan pay
        wear costs to empty the pack for no gain. Clamped, it simply holds."""
        slots = build_slots([25.0] * 12, load=0.0)
        settings = OptimiserSettings(terminal_mode="fixed", terminal_rate=-50.0)
        battery = make_battery(cycle_cost_per_kwh=2.0)
        plan = optimise(slots, 70.0, battery, make_grid(allow_export=False), settings)
        assert plan.slots[-1].soc_end >= 70.0 - 2.0
        assert sum(s.curtailed_kwh for s in plan.slots) == pytest.approx(0.0)


class TestTerminalValuationDoesNotHoard:
    """The terminal credit must not pay the plan to buy energy it cannot spend.

    Valuing the remainder at the horizon *mean* did exactly that. On a peaky
    tariff the mean sits above almost every price on the horizon, so buying
    looked profitable nearly everywhere; the pack filled and stayed full while
    the house bought its load at the dear end. The median is not moved by a few
    spikes, which is the whole point.
    """

    # Sixteen cheap half-hours and four dear ones: mean 30p, median 20p.
    PEAKY = [20.0] * 16 + [70.0] * 4

    def test_mean_is_dragged_above_the_typical_price(self):
        from custom_components.ess_controller.optimiser.dp import _terminal_rate

        slots = build_slots(self.PEAKY)
        mean = _terminal_rate(slots, OptimiserSettings(terminal_mode="horizon_mean"))
        median = _terminal_rate(slots, OptimiserSettings())
        assert mean == pytest.approx(30.0)
        assert median == pytest.approx(20.0)
        assert median < mean

    def test_median_is_the_default_rate(self):
        assert OptimiserSettings().terminal_mode == "horizon_median"

    def test_a_flat_horizon_values_energy_at_that_price(self):
        from custom_components.ess_controller.optimiser.dp import _terminal_rate

        rate = _terminal_rate(build_slots([24.0] * 12), OptimiserSettings())
        assert rate == pytest.approx(24.0)

    def test_free_slots_do_not_make_the_battery_worthless(self):
        """A percentile sitting inside a cheap block valued stored energy at zero,
        and the plan then declined free electricity. Measuring the tail, where the
        refill would actually happen, is what avoids the circularity."""
        from custom_components.ess_controller.optimiser.dp import _terminal_rate

        slots = build_slots([0.0] * 4 + [25.0] * 8)
        assert _terminal_rate(slots, OptimiserSettings()) == pytest.approx(25.0)

    def test_the_credit_is_capped_at_the_shortfall_ahead(self):
        """The price of leftover energy was never the real problem: the quantity
        was. A site whose array nearly covers its load will not use a full pack,
        so crediting one assumes a need that is not there."""
        from custom_components.ess_controller.optimiser.dp import _terminal_energy_cap

        # Solar covers all but a little of the load: almost nothing to credit.
        covered = build_slots([20.0] * 48, pv=0.45, load=0.5)
        assert _terminal_energy_cap(covered) < 3.0
        # Midwinter, no array: the whole day's load is ahead of it.
        dark = build_slots([20.0] * 48, pv=0.0, load=0.5)
        assert _terminal_energy_cap(dark) == pytest.approx(24.0)

    def test_a_surplus_day_credits_nothing_rather_than_going_negative(self):
        from custom_components.ess_controller.optimiser.dp import _terminal_energy_cap

        drenched = build_slots([20.0] * 48, pv=2.0, load=0.2)
        assert _terminal_energy_cap(drenched) == 0.0

    def test_does_not_fill_the_pack_it_cannot_empty(self):
        """No export, a small house load, and a horizon that is mostly cheap.

        Under the mean the plan bought its way to full and held there. What it
        can actually spend is bounded by the load, so it should end the horizon
        near where it started rather than gorged.
        """
        battery = make_battery(cycle_cost_per_kwh=1.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        slots = build_slots(self.PEAKY, load=0.25)
        plan = optimise(slots, 50.0, battery, grid)
        bought = sum(s.grid_import_kwh for s in plan.slots)
        # The house needs 5 kWh over ten hours; anything far above that is the
        # plan buying for a credit it will never collect.
        assert bought < 9.0

    def test_the_old_behaviour_is_still_available(self):
        settings = OptimiserSettings(terminal_mode="horizon_mean")
        plan = optimise(
            build_slots(self.PEAKY), 50.0, make_battery(), make_grid(), settings
        )
        assert plan.slots


class TestResolutionDoesNotFreezeTheBattery:
    """A level grid coarser than the household's demand forbids discharging.

    On a site that may not push the battery into the grid, a discharge may be no
    larger than what the house is using. With sixty levels over an 18 kWh window
    each level is about 0.3 kWh, and a house drawing 0.5 kW uses 0.25 kWh in a
    half-hour -- so every discharge transition was rejected as infeasible and the
    battery could not move. The plan then bought its load at the evening peak
    while sitting on a full pack, because buying was the only move it could
    express.
    """

    PEAKY = [20.0] * 16 + [70.0] * 4

    def plan(self, **kwargs):
        battery = make_battery(cycle_cost_per_kwh=1.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        slots = build_slots(self.PEAKY, load=0.25, **kwargs)
        return optimise(slots, 50.0, battery, grid, OptimiserSettings())

    def test_the_coarse_grid_really_was_coarser_than_the_demand(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        battery = make_battery()
        grid = make_grid(allow_export=False, allow_battery_export=False)
        slots = build_slots(self.PEAKY, load=0.25)
        finest = _smallest_useful_move(slots, battery, grid)
        assert finest is not None
        # One level of the configured grid is bigger than the smallest useful
        # move, which is exactly the condition that froze the battery.
        assert battery.usable_kwh / OptimiserSettings().clamped_levels() > finest

    def test_it_covers_the_house_through_the_peak(self):
        plan = self.plan()
        peak = [s for s in plan.slots if s.import_price > 50.0]
        assert peak
        # Not exactly zero: the level grid cannot land on the deficit to the
        # milliwatt-hour, so a fraction of a percent of the load still comes from
        # the grid. What matters is that the battery, not the grid, is carrying
        # the house through the dear half-hours.
        assert all(s.grid_import_kwh < 0.01 for s in peak)
        assert sum(s.discharge_ac_kwh for s in peak) > 0.9

    def test_the_saving_is_real(self):
        plan = self.plan()
        assert sum(s.grid_import_kwh for s in plan.slots) < 4.5

    def test_export_sites_keep_the_configured_resolution(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        slots = build_slots(self.PEAKY, load=0.25)
        assert _smallest_useful_move(slots, make_battery(), make_grid()) is None

    def test_a_horizon_with_no_deficit_keeps_the_configured_resolution(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        slots = build_slots([20.0] * 6, load=0.0, pv=1.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        assert _smallest_useful_move(slots, make_battery(), grid) is None

    def test_a_tiny_load_does_not_make_the_sweep_enormous(self):
        from custom_components.ess_controller.optimiser.dp import MAX_REFINED_LEVELS

        plan = optimise(
            build_slots([20.0] * 8, load=0.001),
            50.0,
            make_battery(),
            make_grid(allow_export=False, allow_battery_export=False),
            OptimiserSettings(),
        )
        assert plan.slots
        assert MAX_REFINED_LEVELS <= 240


class TestStartingChargeIsNeverOverstated:
    """The plan must not begin believing it has energy the battery lacks.

    ``round`` could start it up to half a level above the measured charge, so the
    plan spent energy that was not there and quietly undershot its own floor.
    """

    def test_the_plan_never_starts_above_the_measured_charge(self):
        battery = make_battery()
        for soc in (17.3, 33.7, 49.1, 60.4, 78.9, 94.2):
            plan = optimise(build_slots([25.0] * 8), soc, battery, make_grid())
            assert plan.slots[0].soc_start <= soc + 1e-9, soc

    def test_the_gap_is_at_most_one_level(self):
        battery = make_battery()
        step = battery.usable_kwh / OptimiserSettings().clamped_levels()
        for soc in (17.3, 60.4, 94.2):
            plan = optimise(build_slots([25.0] * 8), soc, battery, make_grid())
            lost = battery.soc_to_energy(soc) - battery.soc_to_energy(
                plan.slots[0].soc_start
            )
            assert 0.0 <= lost <= step + 1e-9

    def test_the_saving_is_not_a_quantisation_artefact(self):
        """Flooring the start handed the baseline energy the plan did not get, so
        on a flat tariff the optimised plan reported a *loss* against self-use."""
        plan = optimise(
            build_slots([25.0] * 48, load=0.4), 60.0, make_battery(), make_grid()
        )
        assert plan.total_cost <= plan.self_use_cost + 1e-6


class TestSpilledSolarLosesTies:
    """Throwing generation away earns nothing and cost nothing, so the objective
    was exactly indifferent -- and an indifferent optimiser picks whichever branch
    it reaches first rather than the one a person would want."""

    def test_the_tie_break_is_too_small_to_change_economics(self):
        from custom_components.ess_controller.optimiser.dp import SPILL_TIE_BREAK

        # Far below a penny, so it cannot outvote a real price or wear difference.
        assert 0.0 < SPILL_TIE_BREAK <= 0.05

    def test_free_solar_is_stored_rather_than_spilled(self):
        """Zero prices, no export, and more sun than the house can use: storing
        and spilling cost the same, so only the tie-break separates them."""
        battery = make_battery(cycle_cost_per_kwh=0.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        slots = build_slots([0.0] * 12, pv=1.0, load=0.25)
        plan = optimise(
            slots, 30.0, battery, grid, OptimiserSettings(terminal_mode="zero")
        )
        assert sum(s.charge_ac_kwh for s in plan.slots) > 0.0
        assert plan.slots[-1].soc_end > plan.slots[0].soc_start

    def test_it_still_spills_when_there_is_nowhere_to_put_it(self):
        """A full pack has no choice, and the tie-break must not pretend it does.

        With no export tariff the surplus still leaves the site -- it just earns
        nothing -- so what is given away shows up as unpaid export rather than as
        curtailment at the array.
        """
        battery = make_battery(cycle_cost_per_kwh=0.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        slots = build_slots([0.0] * 6, pv=1.0, load=0.25)
        plan = optimise(slots, 95.0, battery, grid)
        given_away = sum(s.grid_export_kwh + s.curtailed_kwh for s in plan.slots)
        assert given_away > 0.0


class TestExportPermissionIsPhysical:
    """ "Export: off" has to stop energy leaving, not merely stop it earning.

    Treating the permission as a price of zero was the wrong reading: the plan
    pushed surplus and battery charge to the grid while the switch said off, and
    the only visible consequence was that it made no money doing so. Note this is
    a different setting from *having no export tariff*, which is the permission on
    with a price of zero.
    """

    def test_nothing_leaves_when_export_is_refused(self):
        grid = make_grid(allow_export=False)
        slots = build_slots([25.0] * 12, pv=1.0, load=0.25)
        plan = optimise(slots, 95.0, make_battery(), grid)
        assert sum(s.grid_export_kwh for s in plan.slots) == pytest.approx(0.0)

    def test_the_surplus_is_curtailed_at_the_array_instead(self):
        grid = make_grid(allow_export=False)
        slots = build_slots([25.0] * 12, pv=1.0, load=0.25)
        plan = optimise(slots, 95.0, make_battery(), grid)
        assert sum(s.curtailed_kwh for s in plan.slots) > 0.0

    def test_the_battery_cannot_dump_to_the_grid(self):
        grid = make_grid(allow_export=False)
        slots = build_slots([-8.0] * 12, load=0.3)
        plan = optimise(slots, 80.0, make_battery(cycle_cost_per_kwh=0.0), grid)
        for slot in plan.slots:
            assert slot.discharge_ac_kwh <= slot.load_kwh + 1e-6

    def test_permitting_export_lets_it_flow_again(self):
        grid = make_grid(allow_export=True)
        slots = build_slots([25.0] * 12, pv=1.0, load=0.25)
        plan = optimise(slots, 95.0, make_battery(), grid)
        assert sum(s.grid_export_kwh for s in plan.slots) > 0.0


class TestARealSummerHorizon:
    """Replayed from a working install's diagnostics, where the plan lost money.

    A 24-hour Agile horizon on a summer day: 10.2 kWh of forecast solar against
    11.2 kWh of forecast load, a 22 kWh pack starting at 56%. The plan built for
    it bought 8.2 kWh and cost 166p, against 8p for leaving the inverter alone --
    a loss of about £58 a year, from two causes that no synthetic test had caught.

    Kept as a fixture because the shape of a real day is what exposed both: the
    prices, the solar curve and the household's own load, all together.
    """

    @staticmethod
    def load():
        import json
        from datetime import datetime
        from pathlib import Path

        raw = json.loads(
            (Path(__file__).parent / "fixtures" / "real_summer_horizon.json").read_text()
        )
        slots = [
            HorizonSlot(
                start=datetime.fromisoformat(s["start"]),
                end=datetime.fromisoformat(s["end"]),
                import_price=s["import_price"],
                export_price=s["export_price"],
                pv_kwh=s["pv_kwh"],
                load_kwh=s["load_kwh"],
            )
            for s in raw["slots"]
        ]
        return (
            slots,
            raw["start_soc"],
            BatterySpec(**raw["battery"]),
            GridSpec(**raw["grid"]),
        )

    def plan(self, **kwargs):
        slots, soc, battery, grid = self.load()
        return optimise(slots, soc, battery, grid, OptimiserSettings(**kwargs))

    def test_it_is_never_worse_than_leaving_the_inverter_alone(self):
        """The fixture's forecast is what the plan was built against, so this is the
        trained case: nothing here is worth scheduling."""
        plan = self.plan()
        assert plan.total_cost <= plan.self_use_cost + 1e-6

    def test_it_does_not_buy_what_the_sun_will_provide(self):
        plan = self.plan()
        assert sum(s.grid_import_kwh for s in plan.slots) < 1.0

    def test_the_credit_cap_reflects_the_shortfall_not_the_pack(self):
        from custom_components.ess_controller.optimiser.dp import _terminal_energy_cap

        slots, _, battery, _ = self.load()
        cap = _terminal_energy_cap(slots)
        # Barely a kilowatt-hour of unmet load, against 16.5 kWh of usable pack.
        assert cap < 2.0
        assert cap < battery.usable_kwh / 5

    def test_the_battery_is_not_frozen(self):
        """An import-only site had discharge transitions rejected as infeasible,
        because the level grid was coarser than the household's half-hourly
        demand -- and the refinement checked the wrong permission, so a site with
        export refused but battery export nominally allowed stayed frozen."""
        plan = self.plan()
        assert sum(s.discharge_ac_kwh for s in plan.slots) > 1.0

    def test_the_reason_says_why_it_is_doing_nothing_clever(self):
        plan = self.plan()
        assert "self-consumption" in plan.reason.lower()

    def test_the_old_valuation_is_what_made_it_expensive(self):
        """Kept as evidence: crediting the whole pack at a typical half-hourly
        price is what bought 8 kWh of unnecessary energy."""
        slots, soc, battery, grid = self.load()
        from custom_components.ess_controller.optimiser import dp

        original = dp._terminal_energy_cap
        try:
            dp._terminal_energy_cap = lambda _slots: 1e9
            greedy = optimise(slots, soc, battery, grid, OptimiserSettings())
        finally:
            dp._terminal_energy_cap = original
        assert greedy.total_cost > greedy.self_use_cost + 100.0
        assert sum(s.grid_import_kwh for s in greedy.slots) > 5.0

    def with_evening_allowance(self, confidence: float):
        """The same day, with the evening provisioned as a model of this maturity
        would provision it.

        Two earlier attempts at this are worth remembering. Marking the whole day's
        load up cost about 150p a day on this very horizon to hedge a shortfall worth
        about 73p, because it made the plan buy for a midday shortfall that did not
        exist. Raising the floor did nothing useful and pointed the wrong way: a floor
        protects charge for later, when what is wanted is more charge arriving at the
        evening.
        """
        from custom_components.ess_controller.forecast.confidence import evening_uplift

        slots, soc, battery, grid = self.load()
        uplift = evening_uplift(
            [s.start.hour for s in slots], [s.load_kwh for s in slots], confidence
        )
        adjusted = [
            HorizonSlot(
                start=s.start,
                end=s.end,
                import_price=s.import_price,
                export_price=s.export_price,
                pv_kwh=s.pv_kwh,
                load_kwh=s.load_kwh + extra,
            )
            for s, extra in zip(slots, uplift, strict=True)
        ]
        return optimise(adjusted, soc, battery, grid, OptimiserSettings())

    @staticmethod
    def _evening(plan):
        return [s for s in plan.slots if 16 <= s.start.hour <= 23]

    def test_it_never_arrives_at_the_evening_with_less(self):
        """The complaint that prompted this: 40% going into the evening is not
        enough when the floor is 20% and the evening takes 30% of the pack. So the
        charge must not be committed to something else earlier in the day."""
        untrained = self._evening(self.with_evening_allowance(0.0))[0]
        trained = self._evening(self.with_evening_allowance(1.0))[0]
        assert untrained.soc_start >= trained.soc_start - 1e-6

    def test_the_battery_carries_the_heavier_evening(self):
        """Provisioning for it is only worth anything if the pack then serves it."""
        untrained = self.with_evening_allowance(0.0)
        trained = self.with_evening_allowance(1.0)
        assert sum(s.discharge_ac_kwh for s in self._evening(untrained)) > sum(
            s.discharge_ac_kwh for s in self._evening(trained)
        )

    def test_the_evening_is_still_not_bought_at_peak_prices(self):
        untrained = self.with_evening_allowance(0.0)
        dear = [s for s in self._evening(untrained) if s.import_price > 40.0]
        assert not any(s.grid_import_kwh > 0.05 for s in dear)

    def test_the_allowance_lands_in_the_evening_not_the_afternoon(self):
        from custom_components.ess_controller.forecast.confidence import evening_uplift

        slots, _, _, _ = self.load()
        uplift = evening_uplift(
            [s.start.hour for s in slots], [s.load_kwh for s in slots], 0.0
        )
        for slot, extra in zip(slots, uplift, strict=True):
            if extra > 0:
                assert 16 <= slot.start.hour <= 23, slot.start.hour

    def test_the_allowance_is_the_size_of_the_error_measured(self):
        from custom_components.ess_controller.forecast.confidence import evening_uplift

        slots, _, _, _ = self.load()
        uplift = evening_uplift(
            [s.start.hour for s in slots], [s.load_kwh for s in slots], 0.0
        )
        assert sum(uplift) == pytest.approx(3.0, abs=0.01)

    def test_it_is_far_cheaper_than_marking_up_the_whole_day(self):
        """Measured, because the first attempt was not."""
        untrained = self.with_evening_allowance(0.0)
        assert untrained.total_cost - untrained.self_use_cost < 60.0

    def test_the_caution_unwinds_as_the_model_learns(self):
        """Measured on evening discharge, not on the trough. A plan serving a
        heavier evening ends *lower*, which is the whole point of provisioning for
        it -- the charge is there and it gets used."""
        served = [
            sum(s.discharge_ac_kwh for s in self._evening(self.with_evening_allowance(c)))
            for c in (0.0, 0.5, 1.0)
        ]
        assert served[0] > served[1] > served[2]

    def test_on_a_sunny_day_the_caution_is_free(self):
        """Nothing is bought for it here: it only stops the charge being spent
        elsewhere, which on a day with this much sun costs nothing at all."""
        untrained = self.with_evening_allowance(0.0)
        assert untrained.total_cost <= untrained.self_use_cost + 1e-6


class TestHorizonReach:
    """Does the plan look as far ahead as its prices allow?

    A real install carried ``horizon_hours: 24`` from its original setup while 96
    half-hours of prices sat in the cache. It therefore could not see that tomorrow
    evening was dearer than today's cheap window, and so had no reason to buy into
    the cheap window -- a silent loss, and the kind nobody notices.
    """

    @staticmethod
    def describe(planned_hours: float, priced_hours: float) -> str:
        from datetime import datetime, timedelta

        from custom_components.ess_controller.models import describe_horizon_reach

        now = datetime(2026, 8, 12, 9, 0)
        return describe_horizon_reach(
            now + timedelta(hours=planned_hours) if planned_hours else None,
            now + timedelta(hours=priced_hours) if priced_hours else None,
            now,
        )

    def test_a_short_horizon_is_called_out(self):
        note = self.describe(planned_hours=24, priced_hours=48)
        assert "24h" in note and "48h" in note
        assert "see further" in note

    def test_matching_the_prices_says_so(self):
        assert "full" in self.describe(planned_hours=36, priced_hours=36)

    def test_being_within_an_hour_counts_as_full(self):
        """Half-hour boundaries and a rolling clock make exact equality rare."""
        assert "full" in self.describe(planned_hours=35.5, priced_hours=36)

    def test_no_prices_says_nothing(self):
        assert self.describe(planned_hours=24, priced_hours=0) == ""

    def test_no_plan_says_nothing(self):
        assert self.describe(planned_hours=0, priced_hours=24) == ""
