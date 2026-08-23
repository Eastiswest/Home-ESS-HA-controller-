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


class TestTheReportedSavingMatchesTheDecision:
    """The plan card said "Saves -150p vs self-use" about a plan the optimiser
    had already judged the better of the two.

    Taken from a real diagnostics file: grid-charge 12.1 kWh at avg 19.4p,
    total_cost 299.82, terminal_value 165.91, self_use_cost 149.77. The plan
    banked cheap overnight energy and was charged the whole 299p for it while
    being credited none of the 166p still in the pack; self-use ended near empty
    and so looked cheaper. ``optimise`` only ever returns a plan whose net cost
    beats self-use, so a negative saving is self-contradictory by construction.
    """

    @staticmethod
    def _cheap_at_the_end() -> list[HorizonSlot]:
        # Dear all day, cheap overnight at the end, so the plan fills the pack on
        # the way out and ends far fuller than self-use does.
        return build_slots([30.0] * 36 + [5.0] * 12, pv=0.0, load=0.3)

    def test_a_plan_that_ends_full_does_not_report_a_loss(self):
        plan = optimise(self._cheap_at_the_end(), 50.0, make_battery(), make_grid())
        assert plan.saving_vs_self_use >= -1e-6, plan.reason
        assert "Saves -" not in plan.reason

    def test_the_saving_is_net_on_both_sides(self):
        plan = optimise(self._cheap_at_the_end(), 50.0, make_battery(), make_grid())
        assert plan.saving_vs_self_use == pytest.approx(
            plan.self_use_net_cost - plan.net_cost
        )
        assert plan.saving_vs_baseline == pytest.approx(
            plan.baseline_net_cost - plan.net_cost
        )

    @pytest.mark.parametrize(
        "prices",
        [
            [30.0] * 36 + [5.0] * 12,
            [5.0] * 12 + [45.0] * 36,
            [8.0] * 12 + [35.0] * 12 + [15.0] * 12 + [45.0] * 12,
            [22.0] * 48,
            [-3.0] * 6 + [28.0] * 42,
        ],
    )
    def test_a_returned_plan_never_loses_to_self_use(self, prices):
        """The invariant behind the guard, stated where a reader can see it."""
        plan = optimise(
            build_slots(prices, pv=0.1, load=0.3), 50.0, make_battery(), make_grid()
        )
        assert plan.saving_vs_self_use >= -1e-6, prices


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

    def test_a_horizon_with_nothing_to_move_keeps_the_configured_resolution(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        # Sun and house exactly matched: no deficit to cover, no surplus to store.
        slots = build_slots([20.0] * 6, load=1.0, pv=1.0)
        grid = make_grid(allow_export=False, allow_battery_export=False)
        assert _smallest_useful_move(slots, make_battery(), grid) is None

    def test_a_tiny_load_does_not_make_the_sweep_enormous(self):
        """A milliwatt-hour deficit must not be chased to arbitrary resolution."""
        plan = optimise(
            build_slots([20.0] * 8, load=0.001),
            50.0,
            make_battery(),
            make_grid(allow_export=False, allow_battery_export=False),
            OptimiserSettings(),
        )
        assert plan.slots

    def test_the_refinement_is_bounded_by_work_not_by_a_level_count(self):
        """Levels alone do not bound the cost, and pretending they did was wrong.

        Every level is priced against every transition reachable inside a slot,
        and how many that is scales with the resolution too -- so the sweep is
        quadratic. A small pack behind a large inverter crosses most of its own
        window in a half-hour, and there the same level count costs several times
        as much. The budget is the work, and the level count follows from it.
        """
        from custom_components.ess_controller.optimiser.dp import (
            MAX_SWEEP_TRANSITIONS,
            _refined_levels,
        )

        slots = build_slots([20.0] * 96, load=0.25)
        roomy = make_battery()
        cramped = make_battery(capacity_kwh=5.0, max_charge_kw=6.0, max_discharge_kw=6.0)
        # Ask for an impossible resolution, and let the budget answer.
        for battery in (roomy, cramped):
            levels = _refined_levels(slots, battery, 60, finest=0.0001)
            hours = slots[0].duration_hours
            reach = max(
                battery.max_charge_kw * hours * battery.charge_efficiency,
                battery.max_discharge_kw * hours / battery.discharge_efficiency,
            )
            span = min(int(reach / (battery.usable_kwh / levels)), levels) * 2 + 1
            assert len(slots) * (levels + 1) * span <= MAX_SWEEP_TRANSITIONS
            assert levels >= 60
        # The cramped pack has to give up resolution the roomy one can afford.
        assert _refined_levels(slots, cramped, 60, 0.0001) < _refined_levels(
            slots, roomy, 60, 0.0001
        )

    def test_a_grid_no_finer_than_asked_for_is_not_refined(self):
        """The budget is a ceiling, not a target."""
        from custom_components.ess_controller.optimiser.dp import _refined_levels

        slots = build_slots([20.0] * 8, load=0.25)
        battery = make_battery()
        # One level of the configured grid already covers this move.
        assert _refined_levels(slots, battery, 60, battery.usable_kwh) == 60


class TestASmallSurplusIsStoredRatherThanSpilled:
    """A solar surplus smaller than one level had nowhere to go.

    Storing exactly the surplus is the only free move available, and a level grid
    coarser than the surplus cannot express it. The next move up is a *grid*
    charge, which is either refused outright or -- once ``MIN_GRID_CHARGE_KWH``
    landed -- rejected as a sliver not worth a mode change. So the sun was given
    away: on a real plan, two consecutive half-hours with 0.06 and 0.07 kWh of
    surplus against a 0.069 kWh step, both curtailed.
    """

    # Flat, so nothing is being arbitraged: the only reason to move energy is
    # that free sun is worth keeping.
    FLAT = [20.0] * 8

    # Sun comfortably ahead of the house, but by less than one configured level.
    PV = 0.35
    LOAD = 0.25

    def slots(self):
        return build_slots(self.FLAT, pv=self.PV, load=self.LOAD)

    def grid(self):
        # Import-only, and grid charging refused: a charge larger than the
        # surplus is not merely uneconomic here, it is impossible. What the plan
        # cannot store, it curtails.
        return make_grid(
            allow_export=False, allow_battery_export=False, allow_grid_charge=False
        )

    def test_the_configured_grid_really_was_coarser_than_the_surplus(self):
        battery = make_battery()
        step = battery.usable_kwh / OptimiserSettings().clamped_levels()
        # One level, taken back to the AC side, is more sun than the half-hour
        # has -- which is exactly the condition that spilled it.
        assert step / battery.charge_efficiency > self.PV - self.LOAD

    def test_the_surplus_now_constrains_the_resolution(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        battery = make_battery()
        finest = _smallest_useful_move(self.slots(), battery, self.grid())
        assert finest is not None
        assert finest <= (self.PV - self.LOAD) * battery.charge_efficiency

    def _finest_step(self) -> float:
        """The step ``optimise`` actually sweeps with, refinement included."""
        import math

        from custom_components.ess_controller.optimiser.dp import (
            MAX_REFINED_LEVELS,
            _smallest_useful_move,
        )

        battery = make_battery()
        levels = OptimiserSettings().clamped_levels()
        step = battery.usable_kwh / levels
        finest = _smallest_useful_move(self.slots(), battery, self.grid())
        if finest is not None and step > finest:
            levels = min(math.ceil(battery.usable_kwh / finest), MAX_REFINED_LEVELS)
            step = battery.usable_kwh / levels
        return step

    def test_storing_the_sun_becomes_a_move_the_grid_can_express(self):
        """The mechanism, stated directly.

        A charge is feasible without buying only when the AC energy it needs is
        no more than the surplus. On the configured grid the smallest non-zero
        charge overshoots that, and the overshoot is a sliver too small to be
        worth a mode change -- so the transition is rejected and the only
        remaining move is to do nothing and lose the sun.
        """
        from custom_components.ess_controller.optimiser.dp import _price_delta

        battery = make_battery()
        slot = self.slots()[0]
        coarse = battery.usable_kwh / OptimiserSettings().clamped_levels()
        assert _price_delta(slot, coarse, battery, self.grid()) is None

        flow = _price_delta(slot, self._finest_step(), battery, self.grid())
        assert flow is not None
        assert flow.grid_import_kwh == pytest.approx(0.0, abs=1e-9)
        assert flow.curtailed_kwh < self.PV - self.LOAD

    def test_the_sun_goes_into_the_battery(self):
        plan = optimise(
            self.slots(), 50.0, make_battery(), self.grid(), OptimiserSettings()
        )
        assert sum(s.charge_ac_kwh for s in plan.slots) > 0.5
        # Not exactly zero: the level grid still cannot land on the surplus to
        # the milliwatt-hour. What matters is that the bulk of it is kept.
        assert sum(s.curtailed_kwh for s in plan.slots) < 0.1
        assert sum(s.grid_import_kwh for s in plan.slots) == pytest.approx(0.0, abs=1e-6)

    def test_it_waits_for_the_sun_instead_of_buying_the_last_two_percent(self):
        """Taken from a real horizon, where this cost money and looked absurd.

        A 22 kWh pack at 92.8% with a 68.7p evening four hours off. Three of the
        afternoon's surpluses were 54-68 Wh at the cells against a 69 Wh level,
        so none of them could be stored: the plan could not climb the last of the
        way on sunshine however much of it arrived. Rather than enter the peak
        short it bought the difference from the grid at 21.3p and spilled the sun
        it could not hold -- with the array still producing and the battery all
        but full, which is precisely when a user asks what on earth it is doing.
        """
        rows = [
            # price, pv, load -- the afternoon, then the evening it is filling for
            (21.3, 0.60, 0.35),
            (38.1, 0.54, 0.40),
            (38.2, 0.42, 0.36),
            (45.4, 0.46, 0.39),
            (49.2, 0.26, 0.38),
            (68.7, 0.06, 0.31),
            (69.7, 0.02, 0.30),
            (56.4, 0.01, 1.13),
            (57.4, 0.02, 1.13),
            (44.7, 0.01, 0.47),
        ]
        slots = []
        for index, (price, pv, load) in enumerate(rows):
            start = START + timedelta(minutes=30 * index)
            slots.append(
                HorizonSlot(
                    start=start,
                    end=start + timedelta(minutes=30),
                    import_price=price,
                    export_price=0.0,
                    pv_kwh=pv,
                    load_kwh=load,
                )
            )
        from custom_components.ess_controller.optimiser.dp import (
            _refined_levels,
            _smallest_useful_move,
        )

        battery = make_battery(min_soc=20.0, max_soc=95.0, cycle_cost_per_kwh=1.147)
        grid = make_grid(allow_export=False, allow_battery_export=False)

        # The resolution is the fix, so the resolution is what is asserted: at 240
        # levels the step is 69 Wh and the three smallest surpluses are 54, 57 and
        # 66 Wh, so none of them is a move the plan can make.
        smallest = min(
            (s.pv_kwh - s.load_kwh) * battery.charge_efficiency
            for s in slots
            if s.pv_kwh > s.load_kwh
        )
        finest = _smallest_useful_move(slots, battery, grid)
        assert finest is not None
        step = battery.usable_kwh / _refined_levels(slots, battery, 60, finest)
        assert step <= smallest, f"step {step:.4f} cannot store {smallest:.4f}"
        assert battery.usable_kwh / 240 > smallest  # ...as the old ceiling could not

        plan = optimise(slots, 92.8, battery, grid, OptimiserSettings())
        afternoon = plan.slots[:5]
        # The sun fills the last of the pack. Not a penny of it is bought.
        assert sum(s.grid_import_kwh for s in afternoon) == pytest.approx(0.0, abs=0.01)
        assert sum(s.charge_ac_kwh for s in afternoon) > 0.3
        # ...and it is full before the dear half-hours arrive.
        assert plan.slots[4].soc_end > 94.0

    def test_the_ceiling_can_still_bind(self):
        """Refinement is capped, and the cap is not a bug -- but it is a limit.

        A household whose smallest half-hour deficit is tiny already pins the
        level grid to ``MAX_REFINED_LEVELS``, and adding the surplus to the
        calculation cannot make the grid finer than that ceiling. Where the cap
        binds, a surplus below one level is still spilled; it is worth about a
        penny, and refining past the ceiling would cost the sweep far more.
        """
        import math

        from custom_components.ess_controller.optimiser.dp import (
            MAX_REFINED_LEVELS,
            _smallest_useful_move,
        )

        battery = make_battery()
        # A half-hour where sun and house nearly cancel: a 5 Wh deficit.
        slots = build_slots(self.FLAT, pv=0.35, load=0.25)
        slots[0].pv_kwh, slots[0].load_kwh = 0.25, 0.255
        finest = _smallest_useful_move(slots, battery, self.grid())
        assert finest is not None
        assert math.ceil(battery.usable_kwh / finest) > MAX_REFINED_LEVELS

    def test_a_surplus_too_small_to_matter_does_not_drive_the_resolution(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        # Ten watt-hours over a half-hour. Chasing it would pin the level grid to
        # its ceiling on every sunny horizon and buy a fraction of a penny.
        slots = build_slots(self.FLAT, pv=0.26, load=0.25)
        assert _smallest_useful_move(slots, make_battery(), self.grid()) is None

    def test_a_horizon_with_no_sun_is_unaffected(self):
        from custom_components.ess_controller.optimiser.dp import _smallest_useful_move

        slots = build_slots(self.FLAT, pv=0.0, load=0.0)
        assert _smallest_useful_move(slots, make_battery(), make_grid()) is None


class TestAHoldThatProtectsNothingIsNotAHold:
    """The plan is a forecast, and the forecast is wrong all day.

    Where the sun already covers the house, ``idle`` and ``self_use`` are the
    same plan -- nothing moves either way -- so the optimiser had no reason to
    prefer one and the label it happened to emit decided real behaviour. As a
    hold the inverter's reserve is raised to the current charge, and every load
    the forecast missed is bought at whatever the half-hour costs: switch the
    oven on during a sunny 45.4p slot and the grid pays for all of it while a
    charged battery watches.

    So the shadow price decides it. Above this slot's price the charge is worth
    more later and the hold stands; at or below it the battery is the cheaper
    source and stays available.
    """

    def _slots(self, rows):
        out = []
        for index, (price, pv, load) in enumerate(rows):
            start = START + timedelta(minutes=30 * index)
            out.append(
                HorizonSlot(
                    start=start,
                    end=start + timedelta(minutes=30),
                    import_price=price,
                    export_price=0.0,
                    pv_kwh=pv,
                    load_kwh=load,
                )
            )
        return out

    def _plan(self, rows, start_soc):
        return optimise(
            self._slots(rows),
            start_soc,
            make_battery(cycle_cost_per_kwh=2.0),
            make_grid(allow_export=False, allow_battery_export=False),
            OptimiserSettings(),
        )

    # Cheap overnight, a sunny afternoon where the house needs nothing at a dear
    # price, then a dearer evening the plan is saving charge for.
    DEAR_SUNNY = (
        [(8.0, 0.0, 0.35)] * 6
        + [(38.0, 0.45, 0.35)] * 4
        + [(45.0, 0.42, 0.36)] * 2
        + [(70.0, 0.0, 0.9)] * 6
    )

    # A full pack, sun covering the house, and an evening dear enough that every
    # kWh in the battery is spoken for.
    CHEAP_SUNNY = [(5.0, 0.45, 0.35)] * 4 + [(95.0, 0.0, 1.2)] * 8

    def test_a_dear_sunny_slot_leaves_the_battery_available(self):
        """What matters is that the battery is not shut, not which state says so.

        A finer level grid can now store the small surplus these slots carry, so
        they come out as a solar charge rather than a bare self-use. Both leave
        the inverter in self-use with its ordinary reserve -- only a hold raises
        the floor to the current charge and puts an unforecast oven on the grid.
        """
        plan = self._plan(self.DEAR_SUNNY, 55.0)
        sunny = [s for s in plan.slots if s.import_price == 45.0]
        assert sunny
        for slot in sunny:
            assert slot.action is not SlotAction.IDLE, slot
            assert slot.action in (
                SlotAction.SELF_USE,
                SlotAction.CHARGE_SOLAR_ONLY,
            ), slot
            # Whatever it does, it does not buy: the sun covers the house here.
            assert slot.grid_import_kwh == pytest.approx(0.0, abs=1e-9)

    def test_a_cheap_sunny_slot_before_a_brutal_evening_still_holds(self):
        """The other half of the rule, and the reason it is not just self-use."""
        plan = self._plan(self.CHEAP_SUNNY, 95.0)
        sunny = [s for s in plan.slots if s.import_price == 5.0]
        assert sunny
        assert all(s.action is SlotAction.IDLE for s in sunny), sunny
        # Sun ahead of house and nothing bought: this is the hold the dashboard
        # labels "sun covers the house", and here it is protecting something.
        assert all(s.pv_kwh > s.load_kwh for s in sunny)

    def test_the_hold_value_is_what_decides_it(self):
        for rows, soc, price in (
            (self.DEAR_SUNNY, 55.0, 45.0),
            (self.CHEAP_SUNNY, 95.0, 5.0),
        ):
            for slot in self._plan(rows, soc).slots:
                if slot.import_price != price:
                    continue
                assert slot.hold_value is not None
                held = slot.action is SlotAction.IDLE
                assert held == (slot.hold_value > slot.import_price), slot

    def test_the_hold_value_is_a_price_not_a_marginal_sliver(self):
        """Measured across a half-hour's discharge, not one level.

        At the top of a pack that free sun is about to refill, the true marginal
        value of one more level is near zero -- swapping it for grid energy costs
        almost nothing, because the sun puts it straight back. An oven is not a
        marginal quantity, and reading the slope one level at a time released a
        hold in front of a 95p evening for want of asking about a real kWh.
        """
        plan = self._plan(self.CHEAP_SUNNY, 95.0)
        first = plan.slots[0]
        assert first.hold_value is not None
        assert first.hold_value > first.import_price

    # A dear half-hour whose forecast shortfall is far below one level of the
    # grid, so the sweep cannot discharge into it however much it would like to.
    # ...followed by the evening it is holding for and a cheap night after it, so
    # the sweep has something to schedule and the plan is its own rather than the
    # self-use fallback.
    TINY_SHORTFALL = (
        [(49.2, 0.26, 0.29), (68.7, 0.06, 0.31), (69.7, 0.02, 0.30)]
        + [(56.4, 0.01, 1.13)] * 4
        + [(24.0, 0.0, 0.4)] * 12
        + [(60.0, 0.0, 0.9)] * 6
    )

    def test_no_hold_anywhere_costs_more_than_it_protects(self):
        """The whole of the test, on every slot of every shape.

        This was once restricted to slots where the sun covered the house, on the
        reasoning that a hold making the house buy *forecast* load must have been
        chosen deliberately. It was not always chosen at all: a shortfall smaller
        than one level of the grid leaves holding as the only move that can be
        represented, and the slot goes out as a hold regardless of what the charge
        is worth. A real plan did exactly that at 49.2p, against its own valuation
        of 23.9p, with the pack at 93%.
        """
        for rows, soc in (
            (self.DEAR_SUNNY, 55.0),
            (self.CHEAP_SUNNY, 95.0),
            (self.TINY_SHORTFALL, 93.0),
        ):
            for slot in self._plan(rows, soc).slots:
                if slot.action is not SlotAction.IDLE:
                    continue
                assert slot.hold_value is not None, slot
                assert slot.hold_value > slot.import_price, slot

    def test_a_shortfall_too_small_to_cover_is_not_a_refusal_to_cover_it(self):
        plan = self._plan(self.TINY_SHORTFALL, 93.0)
        dear = plan.slots[0]
        assert dear.import_price == 49.2
        assert dear.hold_value is not None
        assert dear.hold_value < dear.import_price
        # Whatever it does, it must not shut the battery and put the house on the
        # grid at 49.2p while holding charge it values at half that.
        assert dear.action is not SlotAction.IDLE, dear

    def test_the_baselines_express_no_opinion(self):
        """Neither counterfactual runs a sweep, so neither has a slope to read."""
        slots = self._slots(self.DEAR_SUNNY)
        battery, grid = make_battery(), make_grid(allow_export=False)
        for plan in (
            simulate_idle(slots, 55.0, battery, grid, START),
            simulate_self_use(slots, 55.0, battery, grid, START),
        ):
            assert all(s.hold_value is None for s in plan.slots)

    def test_the_figure_is_published(self):
        """Diagnostics have to be able to show why a hold was or was not kept."""
        slot = self._plan(self.DEAR_SUNNY, 55.0).slots[0]
        assert slot.as_dict()["hold_value"] == pytest.approx(slot.hold_value, abs=0.005)


class TestTheSelfUseFallbackNeverShutsTheBattery:
    """The fallback plan means "leave the inverter alone". It was doing the opposite.

    Whenever the sweep cannot beat plain self-consumption, that simulation is
    handed back as the plan -- and the control path reads a slot's action
    literally, so ``idle`` raises the inverter's reserve to the current charge
    and holds the battery shut for the half-hour.

    A real install hit it with a full pack on a sunny afternoon: nowhere to put
    the surplus, so the policy moved nothing, so four consecutive half-hours came
    back ``idle`` and were printed as "Hold (sun covers the house)" at 38.2p and
    45.4p with the battery shut behind them. Any load the forecast had not
    predicted would have been bought at those prices while a full battery
    watched, on a plan whose whole premise was that nothing clever was worth
    doing. Not moving is not the same as refusing to move.
    """

    def _slots(self):
        # A full pack, a sunny afternoon with nowhere to put the surplus, then an
        # evening the battery covers on its own.
        rows = [(38.2, 0.42, 0.36), (45.4, 0.46, 0.39)] * 2 + [
            (49.2, 0.06, 0.38),
            (68.7, 0.0, 0.31),
        ]
        slots = []
        for index, (price, pv, load) in enumerate(rows):
            start = START + timedelta(minutes=30 * index)
            slots.append(
                HorizonSlot(
                    start=start,
                    end=start + timedelta(minutes=30),
                    import_price=price,
                    export_price=0.0,
                    pv_kwh=pv,
                    load_kwh=load,
                )
            )
        return slots

    def _plan(self):
        return simulate_self_use(
            self._slots(),
            95.0,
            make_battery(min_soc=20.0, max_soc=95.0),
            make_grid(allow_export=False, allow_battery_export=False),
        )

    def test_a_full_pack_under_sun_is_not_expressed_as_a_hold(self):
        plan = self._plan()
        assert plan.slots
        assert not [s for s in plan.slots if s.action is SlotAction.IDLE], [
            (f"{s.start:%H:%M}", s.import_price, s.action.value)
            for s in plan.slots
            if s.action is SlotAction.IDLE
        ]

    def test_the_slots_that_moved_nothing_say_self_use(self):
        plan = self._plan()
        stuck = [s for s in plan.slots if s.battery_delta_kwh == pytest.approx(0.0)]
        assert stuck, "the case under test did not arise"
        assert all(s.action is SlotAction.SELF_USE for s in stuck)

    def test_relabelling_changes_no_money(self):
        """It is a statement about the inverter's mode, not about the energy."""
        plan = self._plan()
        assert plan.total_cost == pytest.approx(plan.self_use_cost)
        assert all(s.grid_import_kwh == pytest.approx(0.0, abs=1e-9) for s in plan.slots)

    def test_the_do_nothing_baseline_still_holds_because_that_is_what_it_is(self):
        """``simulate_idle`` is the counterfactual, never handed back as a plan."""
        plan = simulate_idle(
            self._slots(),
            95.0,
            make_battery(min_soc=20.0, max_soc=95.0),
            make_grid(allow_export=False, allow_battery_export=False),
        )
        assert all(s.action is SlotAction.IDLE for s in plan.slots)

    def test_no_plan_ever_holds_without_having_priced_the_hold(self):
        """The invariant that would have caught this, whichever branch returns.

        A hold shuts the battery for a half-hour, so something must have decided
        it was worth more shut than open. ``hold_value`` is that decision. A slot
        carrying ``idle`` without one is a hold nobody priced, and the sweep is
        the only thing entitled to price it -- so any plan that leaves this
        module holding something must be able to say what it is protecting.
        """
        battery = make_battery(min_soc=20.0, max_soc=95.0, cycle_cost_per_kwh=1.147)
        grids = (
            make_grid(allow_export=False, allow_battery_export=False),
            make_grid(allow_export=False, allow_grid_charge=False),
            make_grid(),
        )
        shapes = (
            ([25.0] * 12, 0.5, 0.3),  # flat, sunny, nothing to arbitrage
            ([25.0] * 12, 0.0, 0.3),  # flat, dark
            ([5.0] * 6 + [70.0] * 6, 0.0, 0.4),  # a real spread
            ([70.0] * 6 + [5.0] * 6, 0.45, 0.35),  # dear first, sunny
            ([25.0] * 12, 0.42, 0.36),  # the surplus that will not fit
        )
        for prices, pv, load in shapes:
            for grid in grids:
                for soc in (20.0, 55.0, 95.0):
                    plan = optimise(
                        build_slots(prices, pv=pv, load=load),
                        soc,
                        battery,
                        grid,
                        OptimiserSettings(),
                    )
                    unpriced = [
                        s
                        for s in plan.slots
                        if s.action is SlotAction.IDLE and s.hold_value is None
                    ]
                    assert not unpriced, (prices[0], pv, load, soc, plan.reason)


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


class TestACrampedWindowSaysSo:
    """SoC limits can make the battery a spectator, and the plan still looks valid.

    A real install ran with Minimum charge at 90% against a maximum of 95%: 1.1 kWh
    of a 22 kWh pack. The evening ran off the grid at 45p with the SoC line pinned
    flat, and the plan reported "Saves 1p vs self-use" with a straight face. The
    setting was the cause; the plan reason was the only place anyone was looking.
    """

    @staticmethod
    def plan_with(min_soc: float, max_soc: float):
        battery = make_battery(min_soc=min_soc, max_soc=max_soc)
        slots = build_slots([20.0] * 8 + [50.0] * 8, load=0.3)
        return optimise(slots, (min_soc + max_soc) / 2, battery, make_grid())

    def test_a_ninety_percent_floor_is_called_out(self):
        reason = self.plan_with(90.0, 95.0).reason
        assert "usable" in reason
        assert "very little to schedule" in reason

    def test_it_does_not_blame_a_setting_it_cannot_see(self):
        """The floor handed to the optimiser is not necessarily the user's
        minimum-charge setting: an outage hold can raise it, and on a real install
        one did -- to 90% while the setting sat correctly at 20. Blaming the setting
        sent the reader away from the storm that was holding the pack shut."""
        reason = self.plan_with(90.0, 95.0).reason
        assert "setting" not in reason.lower()

    def test_it_names_the_numbers(self):
        reason = self.plan_with(90.0, 95.0).reason
        assert "1.1 kWh" in reason
        assert "90%" in reason and "95%" in reason

    def test_it_is_said_first(self):
        """Nothing else in the sentence matters if the battery cannot move."""
        assert self.plan_with(90.0, 95.0).reason.startswith("Only ")

    def test_a_normal_window_says_nothing_about_it(self):
        assert "usable" not in self.plan_with(20.0, 95.0).reason

    def test_the_threshold_is_a_share_not_an_absolute(self):
        """A 5 kWh pack with a sensible window is not cramped; a 100 kWh pack with
        5 kWh of window is."""
        from custom_components.ess_controller.optimiser.dp import CRAMPED_WINDOW_SHARE

        assert 0.0 < CRAMPED_WINDOW_SHARE < 0.5

    def test_the_plan_is_still_produced(self):
        """A warning, not a refusal: it is the user's battery and their setting."""
        plan = self.plan_with(90.0, 95.0)
        assert plan.slots
        assert not plan.infeasible


class TestASliverFromTheGridIsNotWorthAModeChange:
    """Any grid contribution makes a slot a forced charge, however small.

    A real plan bought 0.05 kWh at 36.6p -- under two pence -- and paid for it by
    putting the inverter into Manual mode for the whole half-hour, near the top of the
    day, while the array was still producing. The money is trivial; the mode change is
    not, and neither is the risk of a forced charge taking more than was planned.
    """

    @staticmethod
    def plan(surplus_per_slot: float):
        slots = build_slots([36.6] * 12, pv=surplus_per_slot + 0.3, load=0.3)
        return optimise(slots, 90.0, make_battery(), make_grid(allow_export=False))

    def test_it_does_not_buy_a_sliver(self):
        from custom_components.ess_controller.optimiser.dp import MIN_GRID_CHARGE_KWH

        for slot in self.plan(0.5).slots:
            surplus = max(slot.pv_kwh - slot.load_kwh, 0.0)
            bought = slot.charge_power_kw * slot.duration_hours - surplus
            assert not (1e-6 < bought < MIN_GRID_CHARGE_KWH), bought

    def test_a_purely_solar_charge_is_still_allowed(self):
        plan = self.plan(0.5)
        assert sum(s.charge_ac_kwh for s in plan.slots) > 0.0

    def test_the_threshold_is_small_enough_not_to_matter_in_money(self):
        """A few pence at the dearest price on an Agile day."""
        from custom_components.ess_controller.optimiser.dp import MIN_GRID_CHARGE_KWH

        assert MIN_GRID_CHARGE_KWH * 0.60 < 0.15

    def test_a_real_grid_charge_still_happens(self):
        """The rule must not stop a genuine cheap-window purchase."""
        slots = build_slots([8.0] * 12 + [45.0] * 12, load=0.4)
        plan = optimise(slots, 40.0, make_battery(), make_grid(allow_export=False))
        assert sum(s.grid_import_kwh for s in plan.slots) > 1.0

    def test_nothing_deadlocks_when_every_charge_is_a_sliver(self):
        plan = self.plan(0.001)
        assert plan.slots
        assert not plan.infeasible


class TestItBuysCheapBeforeDear:
    """Ordering, not merely quantity: the two are decided by different things.

    *Whether* to buy is a price-versus-value question the sweep answers well at
    almost any resolution. *Which* half-hour to buy in turns on differences of a
    fraction of a penny between neighbouring levels -- so the level grid, not the
    economics, governs it, and too coarse a grid gets it wrong in a way that is
    plainly visible on the dashboard.

    A real horizon: the plan bought 1.18 kWh in half-hours dearer than 22p while
    leaving room unused in half-hours at 20.9p and 21.3p an hour later, and
    published "grid charge at 23.9p" above a whole morning of cheaper slots. On
    that horizon 400 levels cost 225.4p; a finer grid cost 214.3p and bought
    nothing dear. The prices and the small, uneven solar and load below are that
    morning, which is what makes this reproduce it -- a tidy horizon of flat load
    and two clean price bands does not.
    """

    PRICES = [
        23.9,
        23.7,
        22.3,
        21.2,
        20.7,
        21.3,
        21.6,
        20.9,
        20.8,
        20.5,
        21.3,
        20.9,
        21.7,
        22.4,
    ] + [45.0] * 22
    PV = [
        0.31,
        0.31,
        0.38,
        0.42,
        0.45,
        0.44,
        0.43,
        0.37,
        0.47,
        0.58,
        0.52,
        0.52,
        0.56,
        0.46,
    ] + [0.2] * 22
    LOAD = [
        0.27,
        0.26,
        0.57,
        0.38,
        0.24,
        0.65,
        0.28,
        0.26,
        0.32,
        0.31,
        0.37,
        0.48,
        0.31,
        0.30,
    ] + [0.5] * 22

    def plan(self):
        slots = []
        for index, price in enumerate(self.PRICES):
            start = START + timedelta(minutes=30 * index)
            slots.append(
                HorizonSlot(
                    start=start,
                    end=start + timedelta(minutes=30),
                    import_price=price,
                    export_price=0.0,
                    pv_kwh=self.PV[index],
                    load_kwh=self.LOAD[index],
                )
            )
        battery = make_battery(
            min_soc=20.0,
            max_charge_kw=6.0,
            max_discharge_kw=6.0,
            cycle_cost_per_kwh=1.147,
        )
        return optimise(slots, 32.6, battery, make_grid(allow_export=False))

    def test_the_battery_is_not_filled_dear_while_cheap_slots_have_room(self):
        plan = self.plan()
        dear = sum(
            s.grid_import_kwh
            for s in plan.slots
            if s.action is SlotAction.CHARGE and s.import_price > 22.0
        )
        cheap = sum(
            s.grid_import_kwh
            for s in plan.slots
            if s.action is SlotAction.CHARGE and s.import_price < 22.0
        )
        assert cheap > 1.0, "nothing was bought in the cheap window at all"
        # Room to spare in the cheap window, so buying dear was never forced.
        assert max(s.soc_end for s in plan.slots) < 90.0
        assert dear == 0.0, f"bought {dear:.3f} kWh above 22p"

    def test_the_grid_is_fine_enough_to_order_a_half_hour_of_load(self):
        """The step has to be smaller than the decision it is asked to express."""
        from custom_components.ess_controller.optimiser.dp import MAX_REFINED_LEVELS

        assert make_battery().usable_kwh / MAX_REFINED_LEVELS < 0.02
