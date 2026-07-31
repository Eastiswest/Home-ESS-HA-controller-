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
        prices = [20.0] * 6 + [-10.0] * 4 + [30.0] * 6
        return build_slots(prices, export=[export_price] * 16, load=0.3)

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
        headroom it buys is still worth more than the energy thrown away."""
        grid = make_grid(allow_export=False)
        plan = optimise(self._horizon(0.0), 90.0, make_battery(), grid)
        assert plan.slots[5].soc_end < 75.0
        assert sum(s.curtailed_kwh for s in plan.slots) > 0.5

    def test_pre_emptive_dump_is_labelled_discharge_not_self_use(self):
        """Regression: discharging beyond the household load was labelled
        self-use whenever the surplus earned nothing, so the adapter left the
        inverter in self-use mode and it only covered the house. The headroom
        never appeared and the plan silently failed."""
        grid = make_grid(allow_export=False)
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
        grid = make_grid(allow_export=False)

        cheap_wear = optimise(slots, 60.0, make_battery(cycle_cost_per_kwh=2.0), grid)
        dear_wear = optimise(slots, 60.0, make_battery(cycle_cost_per_kwh=8.0), grid)

        # 2p/kWh wear against an 8p/kWh reward: worth cycling for.
        assert sum(s.curtailed_kwh for s in cheap_wear.slots) > 1.0
        # 8p/kWh wear: no longer worth it, so it simply fills and holds.
        assert sum(s.curtailed_kwh for s in dear_wear.slots) == pytest.approx(0.0)
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
