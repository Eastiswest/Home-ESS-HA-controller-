"""Tests for price adjustments, sessions, outage anticipation and load shifting."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from custom_components.ess_controller.adjustments import (
    KIND_FREE_ELECTRICITY,
    KIND_SAVING_SESSION,
    PriceAdjustment,
    SessionEvent,
    active_session,
    adjustments_from_sessions,
    apply_adjustments,
    next_session,
    parse_session_events,
)
from custom_components.ess_controller.models import (
    BatterySpec,
    GridSpec,
    HorizonSlot,
    PriceSlot,
)
from custom_components.ess_controller.optimiser.dp import optimise
from custom_components.ess_controller.outage import (
    RISK_ELEVATED,
    RISK_HIGH,
    RISK_NONE,
    assess,
)
from custom_components.ess_controller.shifting import (
    LoadPlacement,
    ShiftableLoad,
    add_placements_to_slots,
    appliance_targets,
    marginal_prices,
    next_placement,
    parse_shiftable_loads,
    place_load,
    place_loads,
    schedule_advice,
)
from custom_components.ess_controller.tariff.base import PriceSeries


def dt(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def local(moment: datetime) -> datetime:
    return moment


def price_slots(prices: list[float], start_hour: int = 0) -> list[PriceSlot]:
    slots = []
    for index, price in enumerate(prices):
        start = dt(start_hour) + timedelta(minutes=30 * index)
        slots.append(
            PriceSlot(start=start, end=start + timedelta(minutes=30), price=price)
        )
    return slots


def horizon(
    prices: list[float],
    *,
    export: list[float] | None = None,
    pv: float = 0.0,
    load: float = 0.3,
    start_hour: int = 0,
) -> list[HorizonSlot]:
    slots = []
    for index, price in enumerate(prices):
        start = dt(start_hour) + timedelta(minutes=30 * index)
        slots.append(
            HorizonSlot(
                start=start,
                end=start + timedelta(minutes=30),
                import_price=price,
                export_price=export[index] if export else 0.0,
                pv_kwh=pv,
                load_kwh=load,
            )
        )
    return slots


def battery(**kwargs) -> BatterySpec:
    defaults = dict(
        capacity_kwh=22.0,
        min_soc=15.0,
        max_soc=95.0,
        max_charge_kw=3.6,
        max_discharge_kw=3.6,
        cycle_cost_per_kwh=2.0,
    )
    defaults.update(kwargs)
    return BatterySpec(**defaults)


def grid(**kwargs) -> GridSpec:
    defaults = dict(import_limit_kw=15.0, export_limit_kw=3.68)
    defaults.update(kwargs)
    return GridSpec(**defaults)


# ---------------------------------------------------------------------------
# Price adjustments
# ---------------------------------------------------------------------------


class TestApplyAdjustments:
    def test_no_adjustments_returns_inputs(self):
        imports = PriceSeries(price_slots([20.0] * 4))
        result = apply_adjustments(imports, PriceSeries(), [])
        assert result.import_series is imports
        assert result.slots_changed == 0

    def test_override_replaces_price(self):
        imports = PriceSeries(price_slots([25.0] * 4))
        free = PriceAdjustment(
            start=dt(0), end=dt(1), kind=KIND_FREE_ELECTRICITY, import_override=0.0
        )
        result = apply_adjustments(imports, PriceSeries(), [free])
        prices = [s.price for s in result.import_series]
        assert prices == [0.0, 0.0, 25.0, 25.0]
        assert result.slots_changed == 2

    def test_delta_adds_to_price(self):
        imports = PriceSeries(price_slots([25.0] * 4))
        session = PriceAdjustment(
            start=dt(0), end=dt(1), kind=KIND_SAVING_SESSION, import_delta=200.0
        )
        result = apply_adjustments(imports, PriceSeries(), [session])
        assert [s.price for s in result.import_series] == [225.0, 225.0, 25.0, 25.0]

    def test_export_adjusted_too(self):
        exports = PriceSeries(price_slots([15.0] * 4))
        session = PriceAdjustment(
            start=dt(0), end=dt(1), import_delta=200.0, export_delta=200.0
        )
        result = apply_adjustments(PriceSeries(), exports, [session])
        assert [s.price for s in result.export_series] == [215.0, 215.0, 15.0, 15.0]

    def test_override_beats_delta_when_composed(self):
        imports = PriceSeries(price_slots([25.0] * 2))
        both = [
            PriceAdjustment(start=dt(0), end=dt(1), import_delta=100.0),
            PriceAdjustment(start=dt(0), end=dt(1), import_override=0.0),
        ]
        result = apply_adjustments(imports, PriceSeries(), both)
        assert [s.price for s in result.import_series] == [0.0, 0.0]

    def test_slot_assigned_by_midpoint(self):
        """A session boundary landing mid-slot must not half-apply."""
        imports = PriceSeries(price_slots([25.0] * 4))
        # Window covers 00:00-00:20, so only the first slot's midpoint (00:15).
        adjustment = PriceAdjustment(start=dt(0), end=dt(0, 20), import_override=0.0)
        result = apply_adjustments(imports, PriceSeries(), [adjustment])
        assert [s.price for s in result.import_series] == [0.0, 25.0, 25.0, 25.0]

    def test_inputs_not_mutated(self):
        imports = PriceSeries(price_slots([25.0] * 2))
        apply_adjustments(
            imports,
            PriceSeries(),
            [PriceAdjustment(start=dt(0), end=dt(1), import_delta=99.0)],
        )
        assert [s.price for s in imports] == [25.0, 25.0]

    def test_applied_list_records_only_used_adjustments(self):
        imports = PriceSeries(price_slots([25.0] * 2))
        used = PriceAdjustment(start=dt(0), end=dt(1), import_delta=10.0)
        unused = PriceAdjustment(start=dt(20), end=dt(21), import_delta=10.0)
        result = apply_adjustments(imports, PriceSeries(), [used, unused])
        assert result.applied == [used]


class TestSessionParsing:
    def test_parses_joined_saving_sessions(self):
        attributes = {
            "joined_events": [
                {
                    "start": "2026-01-15T17:30:00+00:00",
                    "end": "2026-01-15T18:30:00+00:00",
                    "octopoints_per_kwh": 1800,
                }
            ]
        }
        events = parse_session_events(attributes, KIND_SAVING_SESSION)
        assert len(events) == 1
        assert events[0].joined is True
        # 1800 OctoPoints per kWh is 225p per kWh.
        assert events[0].rate == pytest.approx(225.0)

    def test_available_but_unjoined_marked(self):
        attributes = {
            "available_events": [
                {
                    "start": "2026-01-15T17:30:00+00:00",
                    "end": "2026-01-15T18:30:00+00:00",
                }
            ]
        }
        events = parse_session_events(attributes, KIND_SAVING_SESSION)
        assert events[0].joined is False

    def test_joined_copy_wins_over_available_duplicate(self):
        attributes = {
            "available_events": [
                {"start": "2026-01-15T17:30:00+00:00", "end": "2026-01-15T18:30:00+00:00"}
            ],
            "joined_events": [
                {
                    "start": "2026-01-15T17:30:00+00:00",
                    "end": "2026-01-15T18:30:00+00:00",
                    "octopoints_per_kwh": 1600,
                }
            ],
        }
        events = parse_session_events(attributes, KIND_SAVING_SESSION)
        assert len(events) == 1
        assert events[0].joined is True
        assert events[0].rate == pytest.approx(200.0)

    def test_free_electricity_events(self):
        attributes = {
            "events": [
                {"start": "2026-01-15T13:00:00+00:00", "end": "2026-01-15T14:00:00+00:00"}
            ]
        }
        events = parse_session_events(attributes, KIND_FREE_ELECTRICITY)
        assert len(events) == 1
        assert events[0].kind == KIND_FREE_ELECTRICITY

    def test_explicit_joined_flag_respected(self):
        attributes = {
            "events": [
                {
                    "start": "2026-01-15T17:30:00+00:00",
                    "end": "2026-01-15T18:30:00+00:00",
                    "joined": True,
                }
            ]
        }
        assert parse_session_events(attributes, KIND_SAVING_SESSION)[0].joined is True

    def test_pence_rate_accepted_directly(self):
        attributes = {
            "joined_events": [
                {
                    "start": "2026-01-15T17:30:00+00:00",
                    "end": "2026-01-15T18:30:00+00:00",
                    "pence_per_kwh": 150.0,
                }
            ]
        }
        assert parse_session_events(attributes, KIND_SAVING_SESSION)[0].rate == 150.0

    def test_malformed_entries_skipped(self):
        attributes = {
            "events": [
                {"start": "nonsense", "end": "also nonsense"},
                {"start": "2026-01-15T17:30:00+00:00"},
                {
                    "start": "2026-01-15T18:30:00+00:00",
                    "end": "2026-01-15T17:30:00+00:00",
                },
                "not a dict",
            ]
        }
        assert parse_session_events(attributes, KIND_SAVING_SESSION) == []

    def test_no_attributes_is_safe(self):
        assert parse_session_events(None, KIND_SAVING_SESSION) == []
        assert parse_session_events({}, KIND_SAVING_SESSION) == []


class TestSessionAdjustments:
    def _session(self, **kwargs) -> SessionEvent:
        defaults = dict(
            start=dt(17, 30),
            end=dt(18, 30),
            kind=KIND_SAVING_SESSION,
            rate=225.0,
            joined=True,
        )
        defaults.update(kwargs)
        return SessionEvent(**defaults)

    def test_joined_session_becomes_import_uplift(self):
        adjustments = adjustments_from_sessions([self._session()])
        assert len(adjustments) == 1
        assert adjustments[0].import_delta == pytest.approx(225.0)
        assert adjustments[0].export_delta == pytest.approx(225.0)

    def test_unjoined_session_ignored_by_default(self):
        """Acting on a session you have not opted into earns nothing."""
        assert adjustments_from_sessions([self._session(joined=False)]) == []

    def test_unjoined_session_used_when_allowed(self):
        adjustments = adjustments_from_sessions(
            [self._session(joined=False)], only_joined=False
        )
        assert len(adjustments) == 1

    def test_session_without_rate_falls_back_to_config(self):
        adjustments = adjustments_from_sessions(
            [self._session(rate=None)], saving_session_rate=180.0
        )
        assert adjustments[0].import_delta == pytest.approx(180.0)

    def test_session_with_no_known_rate_ignored(self):
        assert adjustments_from_sessions([self._session(rate=None)]) == []

    def test_export_reward_can_be_disabled(self):
        adjustments = adjustments_from_sessions([self._session()], reward_export=False)
        assert adjustments[0].export_delta == 0.0

    def test_free_session_becomes_zero_import(self):
        event = SessionEvent(
            start=dt(13), end=dt(14), kind=KIND_FREE_ELECTRICITY, joined=False
        )
        adjustments = adjustments_from_sessions([event])
        assert adjustments[0].import_override == 0.0

    def test_active_and_next_helpers(self):
        events = [self._session(), self._session(start=dt(20), end=dt(21))]
        assert active_session(events, dt(18)) is events[0]
        assert active_session(events, dt(19)) is None
        assert next_session(events, dt(19)).start == dt(20)
        assert next_session(events, dt(22)) is None


class TestSessionsChangeThePlan:
    """End to end: the incentive must actually move the optimiser."""

    def test_saving_session_stops_import_and_pre_charges(self):
        # Flat 25p all evening, with a session at 17:30-18:30 (slots 3 and 4).
        slots = horizon([25.0] * 8, load=0.8, start_hour=16)
        without = optimise(slots, 40.0, battery(), grid())

        session = SessionEvent(
            start=dt(17, 30),
            end=dt(18, 30),
            kind=KIND_SAVING_SESSION,
            rate=225.0,
            joined=True,
        )
        adjustments = adjustments_from_sessions([session])
        imports = PriceSeries(
            [PriceSlot(start=s.start, end=s.end, price=s.import_price) for s in slots]
        )
        adjusted = apply_adjustments(imports, PriceSeries(), adjustments)
        boosted = [
            HorizonSlot(
                start=s.start,
                end=s.end,
                import_price=adjusted.import_series.price_at(s.start),
                export_price=0.0,
                pv_kwh=s.pv_kwh,
                load_kwh=s.load_kwh,
            )
            for s in slots
        ]
        with_session = optimise(boosted, 40.0, battery(), grid())

        session_slots = [
            s for s in with_session.slots if dt(17, 30) <= s.start < dt(18, 30)
        ]
        baseline_slots = [s for s in without.slots if dt(17, 30) <= s.start < dt(18, 30)]

        # Import during the session must be driven to essentially nothing.
        assert sum(s.grid_import_kwh for s in session_slots) < 0.05
        # And it must discharge to achieve that, where before it may not have.
        assert sum(s.discharge_ac_kwh for s in session_slots) >= sum(
            s.discharge_ac_kwh for s in baseline_slots
        )

    def test_free_session_fills_the_battery(self):
        # A full day: the reason to fill a pack during a free window is the load
        # that follows it, and a six-hour horizon contains none.
        slots = horizon([25.0] * 48, load=0.3, start_hour=12)
        imports = PriceSeries(
            [PriceSlot(start=s.start, end=s.end, price=s.import_price) for s in slots]
        )
        free = SessionEvent(start=dt(13), end=dt(15), kind=KIND_FREE_ELECTRICITY)
        adjusted = apply_adjustments(
            imports, PriceSeries(), adjustments_from_sessions([free])
        )
        boosted = [
            HorizonSlot(
                start=s.start,
                end=s.end,
                import_price=adjusted.import_series.price_at(s.start),
                pv_kwh=0.0,
                load_kwh=s.load_kwh,
            )
            for s in slots
        ]
        plan = optimise(boosted, 20.0, battery(), grid())
        free_slots = [s for s in plan.slots if dt(13) <= s.start < dt(15)]
        # Four half-hours at 3.6 kW is the most it can take.
        assert sum(s.charge_ac_kwh for s in free_slots) > 6.0


# ---------------------------------------------------------------------------
# Outage anticipation
# ---------------------------------------------------------------------------


class TestOutageAssessment:
    def test_quiet_conditions_leave_reserve_alone(self):
        result = assess(dt(12), base_reserve_soc=10.0)
        assert result.level == RISK_NONE
        assert result.reserve_soc == 10.0
        assert not result.at_risk

    def test_high_wind_raises_reserve(self):
        winds = [(dt(12) + timedelta(hours=h), 40.0 + h * 10) for h in range(6)]
        result = assess(
            dt(12),
            base_reserve_soc=10.0,
            wind_series=winds,
            wind_threshold=70.0,
            boost_soc=50.0,
        )
        assert result.level == RISK_ELEVATED
        assert result.reserve_soc == 50.0
        assert result.max_wind == pytest.approx(90.0)

    def test_severe_wind_raises_further(self):
        winds = [(dt(12), 110.0)]
        result = assess(
            dt(12),
            base_reserve_soc=10.0,
            wind_series=winds,
            wind_threshold=70.0,
            wind_high_threshold=100.0,
            boost_soc=50.0,
            high_boost_soc=80.0,
        )
        assert result.level == RISK_HIGH
        assert result.reserve_soc == 80.0

    def test_wind_below_threshold_ignored(self):
        winds = [(dt(12) + timedelta(hours=h), 30.0) for h in range(6)]
        result = assess(
            dt(12), base_reserve_soc=10.0, wind_series=winds, wind_threshold=70.0
        )
        assert result.level == RISK_NONE
        # Still reported, so a user can calibrate the threshold.
        assert result.max_wind == pytest.approx(30.0)

    def test_wind_beyond_lookahead_ignored(self):
        winds = [(dt(12) + timedelta(hours=40), 120.0)]
        result = assess(
            dt(12),
            base_reserve_soc=10.0,
            wind_series=winds,
            wind_threshold=70.0,
            lookahead=timedelta(hours=12),
        )
        assert result.level == RISK_NONE

    def test_risk_entity_raises_reserve(self):
        result = assess(
            dt(12), base_reserve_soc=10.0, risk_entity_on=True, boost_soc=45.0
        )
        assert result.level == RISK_ELEVATED
        assert result.reserve_soc == 45.0
        assert "risk signal" in result.reason

    def test_planned_outage_is_high_risk(self):
        result = assess(
            dt(12),
            base_reserve_soc=10.0,
            planned_outages=[(dt(14), dt(18), "planned works, Bridge Street")],
            high_boost_soc=85.0,
        )
        assert result.level == RISK_HIGH
        assert result.reserve_soc == 85.0
        assert "Bridge Street" in result.reason

    def test_past_planned_outage_ignored(self):
        result = assess(
            dt(12), base_reserve_soc=10.0, planned_outages=[(dt(6), dt(8), "done")]
        )
        assert result.level == RISK_NONE

    def test_never_lowers_a_users_reserve(self):
        """A deliberately high reserve must not be reduced by a boost."""
        result = assess(
            dt(12), base_reserve_soc=70.0, risk_entity_on=True, boost_soc=40.0
        )
        assert result.reserve_soc == 70.0

    def test_reasons_deduplicated(self):
        result = assess(
            dt(12),
            base_reserve_soc=10.0,
            planned_outages=[(dt(14), dt(15), "works"), (dt(16), dt(17), "works")],
        )
        assert result.reason == "works"


# ---------------------------------------------------------------------------
# Load shifting
# ---------------------------------------------------------------------------


class TestMarginalPrices:
    def test_without_a_plan_uses_import_price(self):
        slots = horizon([10.0, 20.0, 30.0])
        assert marginal_prices(slots, None) == [10.0, 20.0, 30.0]

    def test_exporting_slot_costs_the_forgone_export(self):
        """Extra load in an exporting slot loses export revenue, not import cost.

        PV here is well inside the export limit, so the surplus is exported
        rather than spilled.
        """
        slots = horizon([30.0] * 6, export=[25.0] * 6, pv=1.0, load=0.1)
        plan = optimise(slots, 95.0, battery(), grid())
        prices = marginal_prices(slots, plan)
        exporting = [
            index for index, s in enumerate(plan.slots) if s.grid_export_kwh > 0.01
        ]
        assert exporting, "expected the plan to export"
        for index in exporting:
            assert prices[index] == pytest.approx(25.0)

    def test_mostly_exporting_slot_ignores_a_token_spill(self):
        """A slot exporting 1.8 kWh while spilling 0.06 must not read as free."""
        # 2 kWh per half hour is 4 kW, just over the 3.68 kW export limit.
        slots = horizon([30.0] * 6, export=[25.0] * 6, pv=2.0, load=0.1)
        plan = optimise(slots, 95.0, battery(), grid())
        prices = marginal_prices(slots, plan)
        mixed = [
            index
            for index, s in enumerate(plan.slots)
            if s.grid_export_kwh > s.curtailed_kwh > 0.0
        ]
        assert mixed, "expected a slot that both exports and spills"
        for index in mixed:
            assert prices[index] == pytest.approx(25.0)

    def test_curtailed_slot_is_free(self):
        # Full battery, surplus PV, nothing paid for export -> spill.
        slots = horizon([30.0] * 6, export=[0.0] * 6, pv=2.0, load=0.1)
        plan = optimise(slots, 95.0, battery(), grid(allow_export=False))
        prices = marginal_prices(slots, plan)
        curtailed = [
            index for index, s in enumerate(plan.slots) if s.curtailed_kwh > 0.01
        ]
        assert curtailed, "expected the plan to curtail"
        for index in curtailed:
            assert prices[index] == 0.0


class TestPlaceLoad:
    def test_picks_the_cheapest_window(self):
        # Cheap in the middle: slots 4 and 5.
        prices = [30.0, 30.0, 30.0, 30.0, 5.0, 5.0, 30.0, 30.0]
        slots = horizon(prices)
        load = ShiftableLoad(name="Dishwasher", energy_kwh=2.0, power_kw=2.0)
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement is not None
        assert placement.start == dt(2)
        # 1 hour at 2 kW across two 5p slots is 2 kWh * 5p = 10p.
        assert placement.cost == pytest.approx(10.0)

    def test_reports_worst_case_for_the_saving(self):
        prices = [30.0] * 4 + [5.0] * 4
        slots = horizon(prices)
        load = ShiftableLoad(name="Immersion", energy_kwh=2.0, power_kw=2.0)
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement.saving_vs_worst == pytest.approx(50.0)

    def test_respects_an_allowed_window(self):
        # Cheapest is at 00:00 but the load may only run after 03:00.
        prices = [5.0] * 2 + [30.0] * 4 + [20.0] * 6
        slots = horizon(prices)
        load = ShiftableLoad(
            name="Washer",
            energy_kwh=1.0,
            power_kw=1.0,
            earliest=time(3, 0),
        )
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement.start >= dt(3)

    def test_respects_a_deadline(self):
        prices = [30.0] * 4 + [5.0] * 8
        slots = horizon(prices)
        load = ShiftableLoad(
            name="Washer", energy_kwh=1.0, power_kw=1.0, latest=time(2, 0)
        )
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement is not None
        assert placement.end <= dt(2)

    def test_no_feasible_window_returns_none(self):
        slots = horizon([20.0] * 2)
        # Four hours of running cannot fit in a one-hour horizon.
        load = ShiftableLoad(name="EV", energy_kwh=8.0, power_kw=2.0)
        assert place_load(load, slots, marginal_prices(slots, None), local) is None

    def test_disabled_load_not_placed(self):
        slots = horizon([20.0] * 8)
        load = ShiftableLoad(name="Off", energy_kwh=1.0, power_kw=1.0, enabled=False)
        assert place_load(load, slots, marginal_prices(slots, None), local) is None

    def test_partial_slot_overlap_priced_proportionally(self):
        # 15 minutes of a 1 kW load in a 40p slot is 0.25 kWh -> 10p.
        slots = horizon([40.0] * 4)
        load = ShiftableLoad(name="Quick", energy_kwh=0.25, power_kw=1.0)
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement.cost == pytest.approx(10.0)


class TestPlaceLoads:
    def test_largest_load_gets_first_choice(self):
        prices = [30.0] * 4 + [5.0, 5.0] + [30.0] * 4
        slots = horizon(prices)
        loads = [
            ShiftableLoad(name="Small", energy_kwh=0.5, power_kw=1.0),
            ShiftableLoad(name="Big", energy_kwh=3.0, power_kw=3.0),
        ]
        placements = place_loads(loads, slots, None, local, max_slot_power_kw=3.0)
        by_name = {p.name: p for p in placements}
        # The 3 kW load fills the cheap window, so the small one goes elsewhere.
        assert by_name["Big"].start == dt(2)
        assert by_name["Small"].start != dt(2)

    def test_connection_limit_prevents_stacking(self):
        prices = [5.0] * 8
        slots = horizon(prices)
        loads = [
            ShiftableLoad(name="A", energy_kwh=1.0, power_kw=2.0),
            ShiftableLoad(name="B", energy_kwh=1.0, power_kw=2.0),
        ]
        placements = place_loads(loads, slots, None, local, max_slot_power_kw=3.0)
        assert len(placements) == 2
        first, second = sorted(placements, key=lambda p: p.start)
        assert second.start >= first.end

    def test_empty_inputs(self):
        assert place_loads([], horizon([20.0] * 4), None, local) == []
        assert (
            place_loads(
                [ShiftableLoad(name="x", energy_kwh=1.0, power_kw=1.0)], [], None, local
            )
            == []
        )


class TestAddPlacementsToSlots:
    def test_energy_folded_into_load_forecast(self):
        slots = horizon([20.0] * 4, load=0.2)
        placement = LoadPlacement(
            name="Dishwasher",
            start=dt(0),
            end=dt(1),
            energy_kwh=2.0,
            power_kw=2.0,
            cost=10.0,
        )
        updated = add_placements_to_slots(slots, [placement])
        # 2 kW for two half-hours adds 1 kWh to each.
        assert updated[0].load_kwh == pytest.approx(1.2)
        assert updated[1].load_kwh == pytest.approx(1.2)
        assert updated[2].load_kwh == pytest.approx(0.2)

    def test_original_slots_unchanged(self):
        slots = horizon([20.0] * 2, load=0.2)
        placement = LoadPlacement(
            name="x", start=dt(0), end=dt(1), energy_kwh=2.0, power_kw=2.0, cost=0.0
        )
        add_placements_to_slots(slots, [placement])
        assert slots[0].load_kwh == pytest.approx(0.2)

    def test_no_placements_returns_input(self):
        slots = horizon([20.0] * 2)
        assert add_placements_to_slots(slots, []) is slots

    def test_battery_replans_around_the_shifted_load(self):
        """The point of folding loads in: the battery must pre-charge for them."""
        prices = [8.0] * 6 + [45.0] * 6
        slots = horizon(prices, load=0.2)
        load = ShiftableLoad(name="Immersion", energy_kwh=3.0, power_kw=3.0)
        # Force it into the expensive window with a deadline it cannot beat.
        placement = LoadPlacement(
            name=load.name,
            start=dt(4),
            end=dt(5),
            energy_kwh=3.0,
            power_kw=3.0,
            cost=0.0,
        )
        with_load = add_placements_to_slots(slots, [placement])
        plan = optimise(with_load, 20.0, battery(), grid())
        cheap = [s for s in plan.slots if s.import_price == 8.0]
        # It should buy more cheap energy up front to serve the expensive block.
        assert sum(s.charge_ac_kwh for s in cheap) > 2.0


class TestParseShiftableLoads:
    def test_compact_form(self):
        loads = parse_shiftable_loads("Dishwasher=1.2kWh@2kW,22:00-06:00")
        assert len(loads) == 1
        assert loads[0].name == "Dishwasher"
        assert loads[0].energy_kwh == pytest.approx(1.2)
        assert loads[0].power_kw == pytest.approx(2.0)
        assert loads[0].earliest == time(22, 0)
        assert loads[0].latest == time(6, 0)

    def test_several_compact_entries(self):
        loads = parse_shiftable_loads(
            "Dishwasher=1.2kWh@2kW,22:00-06:00; Immersion=3kWh@3kW"
        )
        assert [load.name for load in loads] == ["Dishwasher", "Immersion"]
        assert loads[1].earliest is None

    def test_mapping_form(self):
        loads = parse_shiftable_loads(
            [
                {
                    "name": "EV",
                    "energy_kwh": 10.0,
                    "power_kw": 7.0,
                    "earliest": "23:30",
                    "latest": "07:00",
                }
            ]
        )
        assert loads[0].power_kw == pytest.approx(7.0)
        assert loads[0].duration_hours == pytest.approx(10 / 7)

    def test_bad_entries_skipped(self):
        loads = parse_shiftable_loads("garbage; Good=1kWh@1kW; Bad=0kWh@1kW")
        assert [load.name for load in loads] == ["Good"]

    def test_empty_input(self):
        assert parse_shiftable_loads(None) == []
        assert parse_shiftable_loads("") == []

    def test_rejects_nonsense_dimensions(self):
        with pytest.raises(ValueError):
            ShiftableLoad(name="x", energy_kwh=-1.0, power_kw=1.0)
        with pytest.raises(ValueError):
            ShiftableLoad(name="x", energy_kwh=1.0, power_kw=0.0)

    def test_newlines_separate_entries(self):
        """The config flow offers a multiline box, so newlines must work."""
        loads = parse_shiftable_loads(
            "Dishwasher=1.2kWh@2kW,22:00-06:00\nImmersion=3kWh@3kW\n\n"
        )
        assert [load.name for load in loads] == ["Dishwasher", "Immersion"]

    def test_switch_entity_is_optional(self):
        load = parse_shiftable_loads("Dishwasher=1.2kWh@2kW,22:00-06:00")[0]
        assert load.switch_entity is None
        assert load.controllable is False

    def test_compact_form_accepts_a_switch(self):
        load = parse_shiftable_loads("Immersion=3kWh@3kW,00:00-07:00,switch.immersion")[0]
        assert load.switch_entity == "switch.immersion"
        assert load.controllable is True
        assert load.earliest == time(0, 0)

    def test_switch_without_a_window(self):
        load = parse_shiftable_loads("Immersion=3kWh@3kW,,switch.immersion")[0]
        assert load.earliest is None
        assert load.switch_entity == "switch.immersion"

    def test_mapping_form_accepts_a_switch(self):
        load = parse_shiftable_loads(
            [{"name": "EV", "energy_kwh": 10.0, "power_kw": 7.0, "switch": "switch.ev"}]
        )[0]
        assert load.switch_entity == "switch.ev"

    def test_a_bad_switch_leaves_the_load_advisory(self):
        """A typo should cost the automation, not the whole load definition."""
        load = parse_shiftable_loads("Immersion=3kWh@3kW,,not-an-entity")[0]
        assert load.name == "Immersion"
        assert load.switch_entity is None

    def test_switch_is_carried_into_the_placement(self):
        slots = horizon([30.0] * 4 + [5.0] * 4)
        load = ShiftableLoad(
            name="Immersion",
            energy_kwh=3.0,
            power_kw=3.0,
            switch_entity="switch.immersion",
        )
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement is not None
        assert placement.switch_entity == "switch.immersion"
        assert placement.as_dict()["switch"] == "switch.immersion"


class TestApplianceTargets:
    """The pure decision the coordinator drives switches from."""

    def _placement(self, name, hour, hours, switch):
        return LoadPlacement(
            name=name,
            start=dt(hour),
            end=dt(hour) + timedelta(hours=hours),
            energy_kwh=1.0,
            power_kw=1.0,
            cost=0.0,
            switch_entity=switch,
        )

    def test_load_without_a_switch_is_never_targeted(self):
        placements = [self._placement("Dishwasher", 3, 1, None)]
        assert appliance_targets(placements, dt(3, 30)) == {}

    def test_on_inside_the_window_off_outside(self):
        placements = [self._placement("Immersion", 3, 1, "switch.immersion")]
        assert appliance_targets(placements, dt(2, 30)) == {"switch.immersion": False}
        assert appliance_targets(placements, dt(3, 30)) == {"switch.immersion": True}
        assert appliance_targets(placements, dt(4, 0)) == {"switch.immersion": False}

    def test_two_loads_sharing_a_switch_are_ored(self):
        """A single plug feeding two cycles must stay on for either of them."""
        placements = [
            self._placement("Cycle one", 1, 1, "switch.plug"),
            self._placement("Cycle two", 5, 1, "switch.plug"),
        ]
        assert appliance_targets(placements, dt(5, 30)) == {"switch.plug": True}
        assert appliance_targets(placements, dt(3, 0)) == {"switch.plug": False}

    def test_mixed_switchable_and_manual(self):
        placements = [
            self._placement("Immersion", 3, 1, "switch.immersion"),
            self._placement("Dishwasher", 3, 1, None),
        ]
        assert appliance_targets(placements, dt(3, 30)) == {"switch.immersion": True}


class TestScheduleAdvice:
    """What a user with no smart appliances actually reads."""

    def _placement(self, name, hour, hours=1):
        return LoadPlacement(
            name=name,
            start=dt(hour),
            end=dt(hour) + timedelta(hours=hours),
            energy_kwh=1.0,
            power_kw=1.0,
            cost=0.0,
        )

    def test_nothing_scheduled(self):
        assert schedule_advice([], dt(3), local) == "nothing scheduled"

    def test_names_the_next_start(self):
        placements = [self._placement("Immersion", 3), self._placement("Dishwasher", 5)]
        assert schedule_advice(placements, dt(1), local) == "start Immersion at 03:00"

    def test_says_to_run_it_now_while_the_window_is_open(self):
        placements = [self._placement("Immersion", 3)]
        assert schedule_advice(placements, dt(3, 15), local) == (
            "run Immersion now, until 04:00"
        )

    def test_after_the_last_window(self):
        placements = [self._placement("Immersion", 3)]
        assert schedule_advice(placements, dt(9), local) == (
            "all scheduled loads have finished"
        )

    def test_next_placement_ignores_one_already_running(self):
        placements = [self._placement("Immersion", 3), self._placement("Dishwasher", 5)]
        upcoming = next_placement(placements, dt(3, 15))
        assert upcoming is not None and upcoming.name == "Dishwasher"


class TestOvernightWindow:
    def test_wrapping_window_allows_overnight_run(self):
        # Horizon spans 22:00 to 04:00 the next day.
        prices = [30.0] * 4 + [5.0] * 8
        slots = horizon(prices, start_hour=22)
        # Rebuild with the correct wrap across midnight.
        slots = []
        for index, price in enumerate(prices):
            start = dt(22) + timedelta(minutes=30 * index)
            slots.append(
                HorizonSlot(
                    start=start,
                    end=start + timedelta(minutes=30),
                    import_price=price,
                    load_kwh=0.2,
                )
            )
        load = ShiftableLoad(
            name="Immersion",
            energy_kwh=2.0,
            power_kw=2.0,
            earliest=time(22, 0),
            latest=time(6, 0),
        )
        placement = place_load(load, slots, marginal_prices(slots, None), local)
        assert placement is not None
        # Cheapest slots start at midnight.
        assert placement.start >= dt(0, 0, day=16)


# ---------------------------------------------------------------------------
# Tariff comparison
# ---------------------------------------------------------------------------


class TestTariffScoring:
    def _template(self) -> list[HorizonSlot]:
        # 24 hours of modest load, no solar, so the tariff is the only variable.
        return horizon([0.0] * 48, load=0.4)

    def _score(self, prices: list[float], standing: float = 0.0, current=False):
        from custom_components.ess_controller.optimiser.dp import OptimiserSettings
        from custom_components.ess_controller.recommend import (
            TariffCandidate,
            score_tariff,
        )

        candidate = TariffCandidate(
            product_code="TEST",
            display_name="Test tariff",
            tariff_code="E-1R-TEST-C",
        )
        return score_tariff(
            candidate,
            PriceSeries(price_slots(prices)),
            PriceSeries(),
            self._template(),
            50.0,
            battery(),
            grid(),
            OptimiserSettings(soc_levels=40),
            standing,
            current,
        )

    def test_flat_tariff_scored(self):
        score = self._score([25.0] * 48)
        assert score.slots == 48
        assert score.hours == pytest.approx(24.0)
        assert score.mean_price == pytest.approx(25.0)
        assert score.error is None

    def test_spread_tariff_beats_flat_for_a_battery(self):
        """The point of scoring with the optimiser: a tariff with a deep trough
        is better for a battery owner even at the same average price."""
        flat = self._score([25.0] * 48)
        # Same 25p mean, but a wide spread the battery can exploit.
        peaky = self._score([5.0] * 24 + [45.0] * 24)
        assert peaky.mean_price == pytest.approx(flat.mean_price)
        assert peaky.total < flat.total
        assert peaky.battery_benefit > flat.battery_benefit

    def test_standing_charge_scaled_to_the_window(self):
        without = self._score([25.0] * 48)
        with_charge = self._score([25.0] * 48, standing=50.0)
        # A 24-hour window carries one full day of standing charge.
        assert with_charge.total - without.total == pytest.approx(50.0)

    def test_daily_equivalent_normalises_a_short_window(self):
        from custom_components.ess_controller.optimiser.dp import OptimiserSettings
        from custom_components.ess_controller.recommend import (
            TariffCandidate,
            build_comparison_template,
            score_tariff,
        )

        candidate = TariffCandidate("T", "T", "E-1R-T-C")
        template = build_comparison_template(self._template(), hours=12)
        score = score_tariff(
            candidate,
            PriceSeries(price_slots([25.0] * 48)),
            PriceSeries(),
            template,
            50.0,
            battery(),
            grid(),
            OptimiserSettings(soc_levels=40),
            48.0,
        )
        assert score.hours == pytest.approx(12.0)
        # Half a day of window must double to a daily figure.
        assert score.daily_equivalent == pytest.approx(score.total * 2)

    def test_missing_prices_reported_not_raised(self):
        from custom_components.ess_controller.optimiser.dp import OptimiserSettings
        from custom_components.ess_controller.recommend import (
            TariffCandidate,
            score_tariff,
        )

        score = score_tariff(
            TariffCandidate("T", "T", "E-1R-T-C"),
            PriceSeries(),
            PriceSeries(),
            self._template(),
            50.0,
            battery(),
            grid(),
            OptimiserSettings(),
        )
        assert score.error is not None
        assert score.slots == 0


class TestRecommendationRanking:
    def _make(self, entries: list[tuple[str, float, bool]]):
        from custom_components.ess_controller.recommend import (
            Recommendation,
            TariffCandidate,
            TariffScore,
        )

        scores = []
        for name, cost, current in entries:
            scores.append(
                TariffScore(
                    candidate=TariffCandidate(name, name, f"E-1R-{name}-C"),
                    optimised_cost=cost,
                    self_use_cost=cost + 50,
                    standing_charge=0.0,
                    hours=24.0,
                    slots=48,
                    mean_price=25.0,
                    min_price=5.0,
                    max_price=45.0,
                    current=current,
                )
            )
        return Recommendation(created=dt(12), scores=scores, window_hours=24.0)

    def test_best_is_cheapest(self):
        rec = self._make([("A", 300.0, True), ("B", 200.0, False)])
        assert rec.best.candidate.product_code == "B"
        assert rec.current.candidate.product_code == "A"
        assert rec.saving_vs_current == pytest.approx(100.0)

    def test_no_saving_when_already_best(self):
        rec = self._make([("A", 200.0, True), ("B", 300.0, False)])
        assert rec.best is rec.current
        assert rec.saving_vs_current is None

    def test_failed_candidates_excluded_from_ranking(self):
        from custom_components.ess_controller.recommend import (
            TariffCandidate,
            TariffScore,
        )

        rec = self._make([("A", 300.0, True)])
        rec.scores.append(
            TariffScore(
                candidate=TariffCandidate("Broken", "Broken", "E-1R-X-C"),
                optimised_cost=0.0,
                self_use_cost=0.0,
                standing_charge=0.0,
                hours=0.0,
                slots=0,
                mean_price=0.0,
                min_price=0.0,
                max_price=0.0,
                error="unreachable",
            )
        )
        payload = rec.as_dict()
        assert [r["product_code"] for r in payload["ranked"]] == ["A"]
        assert payload["failed"][0]["product_code"] == "Broken"

    def test_summary_mentions_the_saving(self):
        from custom_components.ess_controller.recommend import summarise

        rec = self._make([("Flat", 400.0, True), ("Agile", 200.0, False)])
        assert "Agile" in summarise(rec)

    def test_summary_says_stay_when_current_is_best(self):
        from custom_components.ess_controller.recommend import summarise

        rec = self._make([("Agile", 200.0, True), ("Flat", 400.0, False)])
        assert "stay on Agile" in summarise(rec)


class TestProductCatalogue:
    def test_import_products_filtered(self):
        from custom_components.ess_controller.recommend import parse_products

        payload = {
            "results": [
                {"code": "AGILE-24-10-01", "display_name": "Agile", "is_variable": True},
                {"code": "OUTGOING-AGILE-24-10-01", "display_name": "Outgoing Agile"},
                {"code": "GO-VAR-22-10-14", "display_name": "Go"},
            ]
        }
        imports = parse_products(payload, "import")
        assert {p["code"] for p in imports} == {"AGILE-24-10-01", "GO-VAR-22-10-14"}

    def test_export_products_filtered(self):
        from custom_components.ess_controller.recommend import parse_products

        payload = {
            "results": [
                {"code": "AGILE-24-10-01"},
                {"code": "OUTGOING-AGILE-24-10-01"},
                {"code": "FLUX-EXPORT-23-02-14"},
            ]
        }
        exports = parse_products(payload, "export")
        assert {p["code"] for p in exports} == {
            "OUTGOING-AGILE-24-10-01",
            "FLUX-EXPORT-23-02-14",
        }

    def test_other_brands_excluded(self):
        from custom_components.ess_controller.recommend import parse_products

        payload = {"results": [{"code": "OTHER-1", "brand": "SOMEONE_ELSE"}]}
        assert parse_products(payload, "import") == []

    def test_standing_charge_parsed(self):
        from custom_components.ess_controller.recommend import parse_standing_charge

        assert parse_standing_charge(
            {"results": [{"value_inc_vat": 47.85, "value_exc_vat": 45.57}]}
        ) == pytest.approx(47.85)

    def test_missing_standing_charge_is_zero(self):
        from custom_components.ess_controller.recommend import parse_standing_charge

        assert parse_standing_charge({}) == 0.0
        assert parse_standing_charge({"results": []}) == 0.0

    def test_candidates_built_for_region(self):
        from custom_components.ess_controller.recommend import candidates_from_codes

        candidates = candidates_from_codes(["AGILE-24-10-01"], "C", "import")
        assert candidates[0].tariff_code == "E-1R-AGILE-24-10-01-C"

    def test_bad_region_skips_candidate(self):
        from custom_components.ess_controller.recommend import candidates_from_codes

        assert candidates_from_codes(["AGILE-24-10-01"], "Z", "import") == []


class TestCalendarEventFiltering:
    """Not every calendar event is a power cut.

    The field is labelled "calendar", so people point it at a calendar -- and
    until this filter existed, every event on it became a RISK_HIGH planned
    interruption, holding 80% of the pack back for the day. A bin collection cost
    real money.
    """

    def _filter(self, summary: str, raw: str | None = None, all_events: bool = False):
        from custom_components.ess_controller.outage import (
            is_outage_event,
            parse_keywords,
        )

        return is_outage_event(summary, parse_keywords(raw), all_events)

    @pytest.mark.parametrize(
        "summary",
        [
            "Planned power interruption",
            "PLANNED SUPPLY INTERRUPTION",
            "Electricity works",
            "Power cut - Elm Street",
            "Scheduled shutdown",
            "outage",
            "No power 09:00-15:00",
            "Planned interruption to your supply",
        ],
    )
    def test_supply_events_are_recognised(self, summary):
        assert self._filter(summary) is True

    @pytest.mark.parametrize(
        "summary",
        [
            "Bin collection",
            "Dentist 3pm",
            "Kieran's birthday",
            "Team standup",
            "Recycling",
            "",
        ],
    )
    def test_everyday_events_are_ignored(self, summary):
        assert self._filter(summary) is False

    @pytest.mark.parametrize(
        "summary",
        [
            # Every one of these matched the first version of the filter, which
            # used bare words as substrings. Each cost a day of holding 80% back.
            "Powerlifting class",
            "Power nap",
            "Planned leave",
            "Planned holiday",
            "Planned: dentist",
            "Empower workshop",
            "Horsepower meet",
            "School supplies",
            "Electricity bill due",
        ],
    )
    def test_words_that_merely_contain_a_keyword_are_ignored(self, summary):
        assert self._filter(summary) is False, summary

    def test_the_matched_phrase_is_reported(self):
        """So the sensor can say why it fired instead of leaving a mystery."""
        from custom_components.ess_controller.outage import (
            DEFAULT_KEYWORDS,
            matched_keyword,
        )

        assert (
            matched_keyword("Planned power interruption", DEFAULT_KEYWORDS)
            == "power interruption"
        )
        assert matched_keyword("Power nap", DEFAULT_KEYWORDS) is None

    def test_planned_alone_is_not_a_keyword(self):
        """It matches almost anything a person schedules."""
        from custom_components.ess_controller.outage import DEFAULT_KEYWORDS

        assert "planned" not in DEFAULT_KEYWORDS

    def test_a_custom_keyword_list_replaces_the_default(self):
        assert self._filter("Netzabschaltung", raw="netzabschaltung") is True
        assert self._filter("Planned power cut", raw="netzabschaltung") is False

    def test_an_empty_list_falls_back_to_the_defaults(self):
        """An empty box must not silently disable the whole signal."""
        for raw in ("", "   ", ",,,"):
            assert self._filter("Planned power interruption", raw=raw) is True
            assert self._filter("Bin collection", raw=raw) is False

    def test_all_events_is_the_escape_hatch(self):
        """For a DNO calendar whose titles are too terse to match anything."""
        assert self._filter("LV-4471", all_events=True) is True
        assert self._filter("", all_events=True) is True

    def test_matching_ignores_case_and_position(self):
        assert self._filter("Re: your ELECTRICITY works next week") is True
