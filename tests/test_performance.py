"""Tests for the performance history and the metrics derived from it."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.ess_controller.performance import (
    CSV_COLUMNS,
    DEFAULT_RETENTION_DAYS,
    MAX_RECORDS,
    LifetimeTally,
    PerformanceLog,
    SelfUseShadow,
    SlotRecord,
    summarise,
)
from custom_components.ess_controller.sampling import SlotAccumulator


def dt(hour: int, minute: int = 0, day: int = 15) -> datetime:
    return datetime(2026, 2, day, hour, minute, tzinfo=UTC)


def record(hour: int, minute: int = 0, day: int = 15, **kwargs) -> SlotRecord:
    return SlotRecord(start=dt(hour, minute, day), **kwargs)


class TestSlotRecordArithmetic:
    def test_cost_is_import_minus_export(self):
        r = record(
            3,
            import_price=10.0,
            export_price=4.0,
            grid_import_kwh=2.0,
            grid_export_kwh=0.5,
        )
        assert r.cost == pytest.approx(2.0 * 10.0 - 0.5 * 4.0)

    def test_import_and_export_in_one_slot_both_count(self):
        """Netting them to zero would hide real money changing hands."""
        r = record(
            3,
            import_price=30.0,
            export_price=5.0,
            grid_import_kwh=1.0,
            grid_export_kwh=1.0,
        )
        assert r.cost == pytest.approx(25.0)

    def test_no_battery_cost_imports_the_shortfall(self):
        r = record(3, import_price=20.0, pv_kwh=0.2, load_kwh=0.7)
        assert r.no_battery_cost == pytest.approx(0.5 * 20.0)

    def test_no_battery_cost_exports_the_surplus(self):
        r = record(12, import_price=20.0, export_price=15.0, pv_kwh=1.0, load_kwh=0.25)
        assert r.no_battery_cost == pytest.approx(-0.75 * 15.0)

    def test_no_export_tariff_makes_surplus_worthless_not_free_money(self):
        r = record(12, import_price=20.0, export_price=0.0, pv_kwh=1.0, load_kwh=0.25)
        assert r.no_battery_cost == pytest.approx(0.0)

    def test_forecast_error_is_signed_forecast_minus_actual(self):
        r = record(
            12, pv_kwh=0.8, pv_forecast_kwh=1.0, load_kwh=0.5, load_forecast_kwh=0.3
        )
        assert r.pv_error == pytest.approx(0.2)
        assert r.load_error == pytest.approx(-0.2)

    def test_errors_are_none_without_a_forecast(self):
        r = record(12, pv_kwh=0.8)
        assert r.pv_error is None
        assert r.load_error is None

    def test_plan_fidelity_needs_both_sides(self):
        assert record(3, planned_action="charge").followed_plan is None
        assert (
            record(3, planned_action="charge", applied_action="charge").followed_plan
            is True
        )
        assert (
            record(3, planned_action="charge", applied_action="self_use").followed_plan
            is False
        )

    def test_round_trips_through_a_dict(self):
        original = record(
            3,
            import_price=12.5,
            pv_kwh=0.4,
            soc_start=50.0,
            planned_action="charge",
            controlling=True,
        )
        restored = SlotRecord.from_dict(original.as_dict())
        assert restored is not None
        assert restored.start == original.start
        assert restored.import_price == pytest.approx(12.5)
        assert restored.planned_action == "charge"
        assert restored.controlling is True

    def test_a_solar_outage_is_recorded_as_unmeasured_not_dark(self):
        data = record(12, pv_kwh=0.0, pv_forecast_kwh=0.8, pv_measured=False).as_dict()
        loaded = SlotRecord.from_dict(data)
        assert loaded is not None
        assert loaded.pv_measured is False
        # Records from before the field existed were measured as far as anyone knows.
        del data["pv_measured"]
        assert SlotRecord.from_dict(data).pv_measured is True

    def test_the_untrimmed_solar_forecast_travels_with_the_record(self):
        data = record(12, pv_forecast_kwh=0.6, pv_forecast_raw_kwh=0.8).as_dict()
        loaded = SlotRecord.from_dict(data)
        assert loaded.pv_forecast_raw_kwh == pytest.approx(0.8)
        assert loaded.pv_error == pytest.approx(0.6)  # judged on what the plan used

    def test_a_record_written_before_the_field_existed_still_loads(self):
        """History outlives the schema, and the upgrade has to survive it.

        ``load_measured`` arrived after months of records had been written
        without it. Restored as "not measured" every one of them would be
        reported as a house nobody can see, so an absent answer reads as
        measured rather than as a fault.
        """
        payload = record(3, import_price=10.0).as_dict()
        del payload["load_measured"]
        restored = SlotRecord.from_dict(payload)
        assert restored is not None
        assert restored.load_measured is True

    def test_unreadable_record_is_dropped_not_raised(self):
        assert SlotRecord.from_dict({"start": "not a timestamp"}) is None
        assert SlotRecord.from_dict({}) is None

    def test_derived_keys_in_the_dict_are_ignored_on_the_way_back(self):
        """`cost` is exported for readers but recomputed, never stored."""
        payload = record(3, import_price=10.0, grid_import_kwh=1.0).as_dict()
        assert payload["cost"] == pytest.approx(10.0)
        restored = SlotRecord.from_dict(payload)
        assert restored is not None and restored.cost == pytest.approx(10.0)


class TestPerformanceLog:
    def test_same_slot_recorded_twice_replaces_rather_than_duplicates(self):
        """A restart mid-slot must not double-count the money."""
        log = PerformanceLog()
        log.add(record(3, import_price=10.0, grid_import_kwh=1.0))
        log.add(record(3, import_price=10.0, grid_import_kwh=2.0))
        assert len(log) == 1
        assert log.records[0].grid_import_kwh == pytest.approx(2.0)

    def test_records_are_kept_in_time_order(self):
        log = PerformanceLog()
        for hour in (5, 1, 3):
            log.add(record(hour))
        assert [r.start.hour for r in log.records] == [1, 3, 5]

    def test_retention_prunes_from_the_newest_record(self):
        log = PerformanceLog(retention_days=1)
        log.add(record(12, day=10))
        log.add(record(12, day=15))
        assert [r.start.day for r in log.records] == [15]

    def test_zero_retention_keeps_everything_it_is_given(self):
        """Retention of zero disables pruning by date; the cap still applies."""
        log = PerformanceLog(retention_days=0)
        log.add(record(12, day=1))
        log.add(record(12, day=28))
        assert len(log) == 2

    def test_hard_cap_bounds_the_store(self):
        log = PerformanceLog(retention_days=0)
        base = dt(0)
        for index in range(MAX_RECORDS + 10):
            log.add(SlotRecord(start=base + timedelta(minutes=30 * index)))
        assert len(log) == MAX_RECORDS

    def test_window_measures_back_from_the_newest_record(self):
        """Not from the wall clock: an export after downtime must still return data."""
        log = PerformanceLog()
        log.add(record(12, day=1))
        log.add(record(12, day=14))
        log.add(record(12, day=15))
        assert [r.start.day for r in log.window(2)] == [14, 15]

    def test_window_of_an_empty_log_is_empty(self):
        assert PerformanceLog().window(7) == []

    def test_persistence_round_trip(self):
        log = PerformanceLog(retention_days=9)
        log.add(record(3, import_price=8.0, grid_import_kwh=1.5))
        log.add(record(4))
        restored = PerformanceLog.from_dict(log.as_dict())
        assert len(restored) == 2
        assert restored.retention_days == 9
        assert restored.records[0].import_price == pytest.approx(8.0)

    def test_loading_junk_keeps_the_readable_rows(self):
        log = PerformanceLog.from_dict(
            {
                "retention_days": "nonsense",
                "records": [
                    record(3).as_dict(),
                    {"start": "broken"},
                    "not even a mapping",
                ],
            }
        )
        assert len(log) == 1
        assert log.retention_days == DEFAULT_RETENTION_DAYS

    def test_from_dict_tolerates_nothing_stored(self):
        assert len(PerformanceLog.from_dict(None)) == 0

    def test_clear_empties_the_log(self):
        log = PerformanceLog()
        log.add(record(3))
        log.clear()
        assert len(log) == 0


class TestCsvExport:
    def test_header_and_one_row_per_record(self):
        log = PerformanceLog()
        log.add(
            record(3, import_price=10.0, grid_import_kwh=1.0, planned_action="charge")
        )
        log.add(record(3, 30, import_price=9.0))
        rows = list(csv.reader(io.StringIO(log.to_csv())))
        assert rows[0] == list(CSV_COLUMNS)
        assert len(rows) == 3
        assert rows[1][CSV_COLUMNS.index("planned_action")] == "charge"
        assert rows[1][CSV_COLUMNS.index("cost")] == "10.0"

    def test_missing_values_are_blank_not_the_word_none(self):
        """A literal "None" would be read as a string by every spreadsheet."""
        log = PerformanceLog()
        log.add(record(3))
        rows = list(csv.reader(io.StringIO(log.to_csv())))
        assert rows[1][CSV_COLUMNS.index("pv_forecast_kwh")] == ""

    def test_can_export_a_subset(self):
        log = PerformanceLog()
        log.add(record(12, day=1))
        log.add(record(12, day=15))
        rows = list(csv.reader(io.StringIO(log.to_csv(log.window(1)))))
        assert len(rows) == 2


class TestSelfUseShadow:
    def _shadow(self, soc: float = 50.0, **kwargs) -> SelfUseShadow:
        defaults = {
            "capacity_kwh": 10.0,
            "min_soc": 10.0,
            "max_soc": 90.0,
            "max_charge_kw": 3.0,
            "max_discharge_kw": 3.0,
            "charge_efficiency": 1.0,
            "discharge_efficiency": 1.0,
        }
        return SelfUseShadow(soc=soc, **{**defaults, **kwargs})

    def test_shortfall_is_covered_from_the_battery_for_nothing(self):
        shadow = self._shadow()
        cost = shadow.step(record(3, import_price=30.0, load_kwh=1.0, pv_kwh=0.0))
        assert cost == pytest.approx(0.0)
        assert shadow.soc == pytest.approx(40.0)

    def test_imports_what_the_battery_cannot_cover(self):
        shadow = self._shadow(soc=11.0)
        # 1% above the floor of a 10 kWh pack is 0.1 kWh available.
        cost = shadow.step(record(3, import_price=30.0, load_kwh=1.0, pv_kwh=0.0))
        assert cost == pytest.approx(0.9 * 30.0)
        assert shadow.soc == pytest.approx(10.0)

    def test_never_discharges_past_its_floor(self):
        shadow = self._shadow(soc=10.0)
        shadow.step(record(3, import_price=30.0, load_kwh=2.0))
        assert shadow.soc == pytest.approx(10.0)

    def test_power_limit_caps_what_one_slot_can_deliver(self):
        shadow = self._shadow(max_discharge_kw=1.0)
        cost = shadow.step(record(3, import_price=20.0, load_kwh=1.0, pv_kwh=0.0))
        # 1 kW for half an hour is 0.5 kWh; the rest is imported.
        assert cost == pytest.approx(0.5 * 20.0)

    def test_surplus_charges_the_battery_before_exporting(self):
        shadow = self._shadow()
        cost = shadow.step(
            record(12, import_price=20.0, export_price=15.0, pv_kwh=1.0, load_kwh=0.0)
        )
        assert cost == pytest.approx(0.0)
        assert shadow.soc == pytest.approx(60.0)

    def test_exports_what_will_not_fit(self):
        shadow = self._shadow(soc=89.0)
        cost = shadow.step(
            record(12, import_price=20.0, export_price=15.0, pv_kwh=1.0, load_kwh=0.0)
        )
        # 0.1 kWh of headroom absorbs a tenth; the rest is exported.
        assert cost == pytest.approx(-0.9 * 15.0)
        assert shadow.soc == pytest.approx(90.0)

    def test_efficiency_losses_are_charged_to_the_battery(self):
        shadow = self._shadow(charge_efficiency=0.9)
        shadow.step(record(12, pv_kwh=1.0, load_kwh=0.0))
        assert shadow.soc == pytest.approx(50.0 + 0.9 * 10.0)

    def test_without_a_battery_it_is_just_the_no_battery_cost(self):
        shadow = self._shadow(capacity_kwh=0.0)
        slot = record(3, import_price=30.0, load_kwh=1.0)
        assert shadow.step(slot) == pytest.approx(slot.no_battery_cost)


class TestSummary:
    def _armed_day(self) -> list[SlotRecord]:
        """Four days of a simple pattern: cheap overnight, dear evening."""
        records: list[SlotRecord] = []
        for day in range(15, 19):
            for slot in range(48):
                hour, minute = divmod(slot * 30, 60)
                cheap = hour < 5
                records.append(
                    SlotRecord(
                        start=datetime(2026, 2, day, hour, minute, tzinfo=UTC),
                        import_price=5.0 if cheap else 30.0,
                        export_price=0.0,
                        pv_kwh=0.0,
                        load_kwh=0.3,
                        grid_import_kwh=1.5 if cheap else 0.0,
                        grid_measured=True,
                        soc_start=50.0,
                        soc_end=50.0,
                        planned_action="charge" if cheap else "self_use",
                        applied_action="charge" if cheap else "self_use",
                        controlling=True,
                        pv_forecast_kwh=0.0,
                        load_forecast_kwh=0.35,
                    )
                )
        return records

    def test_empty_window_says_so_rather_than_reporting_zeros_as_fact(self):
        summary = summarise([])
        assert summary.slots == 0
        assert "no records yet" in summary.notes

    def test_totals_and_span(self):
        summary = summarise(self._armed_day())
        assert summary.slots == 192
        assert summary.days == pytest.approx(4.0)
        assert summary.load_kwh == pytest.approx(192 * 0.3)
        assert summary.grid_import_kwh == pytest.approx(4 * 10 * 1.5)

    def test_a_single_slot_counts_as_half_an_hour_not_zero_days(self):
        summary = summarise([record(3, grid_measured=True)])
        assert summary.days == pytest.approx(0.5 / 24)

    def test_cost_and_the_no_battery_counterfactual(self):
        summary = summarise(self._armed_day())
        # Charging overnight at 5p: 60 kWh over four days.
        assert summary.cost == pytest.approx(4 * 10 * 1.5 * 5.0)
        # Without a battery the same load is bought when it is needed.
        assert summary.no_battery_cost > summary.cost
        assert summary.saving_vs_no_battery == pytest.approx(
            summary.no_battery_cost - summary.cost
        )

    def test_forecast_error_separates_bias_from_magnitude(self):
        summary = summarise(self._armed_day())
        # Load forecast is 0.35 against an actual 0.3 in every slot.
        assert summary.load_mae == pytest.approx(0.05)
        assert summary.load_bias == pytest.approx(0.05)
        assert summary.load_forecast_slots == 192

    def test_offsetting_errors_show_a_high_mae_and_no_bias(self):
        records = [
            record(1, load_kwh=0.5, load_forecast_kwh=1.0),
            record(2, load_kwh=0.5, load_forecast_kwh=0.0),
        ]
        summary = summarise(records)
        assert summary.load_mae == pytest.approx(0.5)
        assert summary.load_bias == pytest.approx(0.0)

    def test_plan_fidelity_counts_only_comparable_slots(self):
        records = [
            record(1, planned_action="charge", applied_action="charge"),
            record(2, planned_action="charge", applied_action="self_use"),
            record(3, planned_action="charge"),
        ]
        summary = summarise(records)
        assert summary.compared_slots == 2
        assert summary.plan_fidelity == pytest.approx(0.5)

    def test_fidelity_is_none_when_nothing_can_be_compared(self):
        assert summarise([record(1)]).plan_fidelity is None

    def test_round_trip_efficiency_from_measured_flow(self):
        records = [
            record(1, battery_charge_kwh=2.0),
            record(2, battery_discharge_kwh=1.7),
        ]
        summary = summarise(records)
        assert summary.round_trip_efficiency == pytest.approx(0.85)

    def test_round_trip_efficiency_is_none_without_charging(self):
        assert summarise([record(1)]).round_trip_efficiency is None

    def test_wear_is_charged_against_the_saving(self):
        records = [
            record(
                1,
                import_price=10.0,
                grid_import_kwh=1.0,
                load_kwh=1.0,
                battery_discharge_kwh=10.0,
            )
        ]
        shadow = SelfUseShadow(
            soc=50.0,
            capacity_kwh=10.0,
            min_soc=10.0,
            max_soc=90.0,
            max_charge_kw=3.0,
            max_discharge_kw=3.0,
        )
        summary = summarise(records, cycle_cost=2.0, usable_kwh=8.0, shadow=shadow)
        assert summary.wear_cost == pytest.approx(20.0)
        assert summary.equivalent_full_cycles == pytest.approx(1.25)
        gross = summary.saving_vs_self_use
        assert gross is not None
        assert summary.net_saving_vs_self_use == pytest.approx(gross - 20.0)

    def test_self_use_comparison_is_none_without_a_shadow(self):
        summary = summarise(self._armed_day())
        assert summary.self_use_cost is None
        assert summary.saving_vs_self_use is None
        assert summary.net_saving_vs_self_use is None

    def test_self_consumption_excludes_export(self):
        summary = summarise([record(12, pv_kwh=2.0, grid_export_kwh=0.5)])
        assert summary.self_consumption == pytest.approx(0.75)

    def test_self_consumption_is_none_without_generation(self):
        assert summarise([record(2)]).self_consumption is None

    def test_optimiser_beats_self_use_on_a_cheap_overnight_tariff(self):
        """The comparison that decides whether any of this was worth it."""
        records = self._armed_day()
        shadow = SelfUseShadow(
            soc=50.0,
            capacity_kwh=15.0,
            min_soc=10.0,
            max_soc=90.0,
            max_charge_kw=3.0,
            max_discharge_kw=3.0,
        )
        summary = summarise(records, shadow=shadow)
        assert summary.self_use_cost is not None
        # Self-use never grid-charges, so it buys the evening load at 30p.
        assert summary.saving_vs_self_use is not None
        assert summary.saving_vs_self_use > 0

    def test_as_dict_is_shaped_for_a_reader(self):
        summary = summarise(self._armed_day())
        data = summary.as_dict()
        assert set(data) == {
            "window",
            "energy_kwh",
            "money",
            "forecast_error_kwh_per_slot",
            "control",
            "notes",
        }
        assert data["window"]["slots"] == 192
        assert data["money"]["actual"] == pytest.approx(300.0)


class TestTheComparisonIsLikeForLike:
    """A week's saving read -476.6 while the battery sat full.

    The self-use counterfactual spends its charge covering the house every
    evening and arrives at its floor. A plan holding for tomorrow arrives full.
    Charging the plan for every kWh it bought while crediting the shadow for
    having burnt its own measures nothing except how much charge each happened
    to be sitting on when the window closed -- and it reads as the optimiser
    losing money, which is the one conclusion the number must not invite when it
    is not true.
    """

    PRICES = [4.0, 6.0, 20.0, 30.0, 40.0]

    def _records(self, end_soc: float) -> list[SlotRecord]:
        records = []
        for index, price in enumerate(self.PRICES):
            records.append(
                record(
                    index,
                    import_price=price,
                    load_kwh=0.3,
                    grid_import_kwh=0.3,
                    grid_measured=True,
                    soc_start=50.0,
                    soc_end=end_soc if index == len(self.PRICES) - 1 else 50.0,
                )
            )
        return records

    def _summary(self, end_soc: float, shadow_soc: float = 20.0):
        shadow = SelfUseShadow(
            soc=shadow_soc,
            capacity_kwh=10.0,
            min_soc=shadow_soc,
            max_soc=90.0,
            max_charge_kw=3.0,
            max_discharge_kw=0.0,
            discharge_efficiency=0.95,
        )
        return summarise(self._records(end_soc), shadow=shadow)

    def test_a_fuller_battery_is_credited_not_charged(self):
        summary = self._summary(end_soc=70.0)
        # 50 points of a 10 kWh pack, delivered at 95%.
        assert summary.stored_energy_kwh == pytest.approx(5.0 * 0.95)
        assert summary.stored_energy_value > 0
        gross = summary.saving_vs_self_use
        assert gross is not None
        assert summary.net_saving_vs_self_use == pytest.approx(
            gross - summary.wear_cost + summary.stored_energy_value
        )
        assert summary.net_saving_vs_self_use > gross

    def test_an_emptier_battery_is_penalised_by_the_same_rule(self):
        """The correction has to cut both ways or it is just flattery."""
        summary = self._summary(end_soc=10.0)
        assert summary.stored_energy_kwh < 0
        gross = summary.saving_vs_self_use
        assert gross is not None
        assert summary.net_saving_vs_self_use < gross

    def test_it_is_valued_at_what_it_costs_to_put_back(self):
        """The cheap end of the window, not the average.

        Valuing leftover charge at a typical price lets a report flatter itself
        by ending full: it books the energy at more than the half-hours that
        would actually refill it.
        """
        summary = self._summary(end_soc=70.0)
        mean = sum(self.PRICES) / len(self.PRICES)
        assert summary.stored_energy_rate < mean
        assert summary.stored_energy_rate == pytest.approx(4.8)

    def test_the_adjustment_is_shown_not_buried(self):
        summary = self._summary(end_soc=70.0)
        money = summary.as_dict()["money"]
        assert money["stored_energy_kwh"] == pytest.approx(4.75)
        assert money["stored_energy_value"] == pytest.approx(
            summary.stored_energy_value, abs=0.01
        )
        assert any("against the self-use counterfactual" in n for n in summary.notes)

    def test_a_matched_ending_needs_no_correction(self):
        summary = self._summary(end_soc=20.0)
        assert summary.stored_energy_kwh == pytest.approx(0.0)
        gross = summary.saving_vs_self_use
        assert summary.net_saving_vs_self_use == pytest.approx(gross - summary.wear_cost)

    def test_without_a_recorded_charge_nothing_is_invented(self):
        """No SoC sensor means no correction, not a guessed one."""
        shadow = SelfUseShadow(
            soc=20.0,
            capacity_kwh=10.0,
            min_soc=20.0,
            max_soc=90.0,
            max_charge_kw=3.0,
            max_discharge_kw=0.0,
        )
        summary = summarise([record(1, import_price=10.0, load_kwh=0.3)], shadow=shadow)
        assert summary.stored_energy_kwh == pytest.approx(0.0)
        assert summary.stored_energy_value == pytest.approx(0.0)


class TestSummaryCaveats:
    def test_short_window_is_flagged(self):
        summary = summarise([record(1), record(2)])
        assert any("too short" in note for note in summary.notes)

    def test_advisory_only_history_says_the_saving_is_not_its_doing(self):
        records = [
            SlotRecord(start=dt(0) + timedelta(days=d), grid_measured=True)
            for d in range(5)
        ]
        summary = summarise(records)
        assert any("advisory mode throughout" in note for note in summary.notes)

    def test_partial_arming_is_reported(self):
        records = [
            SlotRecord(
                start=dt(0) + timedelta(days=d), grid_measured=True, controlling=d < 2
            )
            for d in range(5)
        ]
        summary = summarise(records)
        assert any("armed for 2 of 5" in note for note in summary.notes)

    def test_unmetered_slots_are_declared(self):
        records = [SlotRecord(start=dt(0) + timedelta(days=d)) for d in range(5)]
        summary = summarise(records)
        assert any("no grid power sensor" in note for note in summary.notes)

    def test_soc_drift_is_declared_because_it_flatters_the_cost(self):
        records = [
            SlotRecord(
                start=dt(0) + timedelta(days=d),
                grid_measured=True,
                controlling=True,
                soc_start=20.0 if d == 0 else 50.0,
                soc_end=90.0 if d == 4 else 50.0,
            )
            for d in range(5)
        ]
        summary = summarise(records)
        assert any("from where it started" in note for note in summary.notes)

    def test_stable_soc_is_not_flagged(self):
        records = [
            SlotRecord(
                start=dt(0) + timedelta(days=d),
                grid_measured=True,
                controlling=True,
                soc_start=50.0,
                soc_end=52.0,
            )
            for d in range(5)
        ]
        summary = summarise(records)
        assert not any("from where it started" in note for note in summary.notes)


class TestGridIntegration:
    """The accumulator has to meter grid flow for any of the money to be real."""

    def _samples(self, accumulator: SlotAccumulator, grid_kw: float) -> list:
        completed: list = []
        for minute in range(0, 35, 5):
            completed += accumulator.add_sample(
                dt(3, 0) + timedelta(minutes=minute),
                pv_power_kw=0.0,
                load_power_kw=1.0,
                grid_power_kw=grid_kw,
            )
        return completed

    def test_import_is_integrated_over_the_slot(self):
        completed = self._samples(SlotAccumulator(), 2.0)
        assert len(completed) == 1
        assert completed[0].grid_import_kwh == pytest.approx(1.0)
        assert completed[0].grid_export_kwh == pytest.approx(0.0)
        assert completed[0].grid_measured is True

    def test_export_is_a_negative_reading(self):
        completed = self._samples(SlotAccumulator(), -2.0)
        assert completed[0].grid_export_kwh == pytest.approx(1.0)
        assert completed[0].grid_import_kwh == pytest.approx(0.0)

    def test_import_and_export_are_not_netted_within_a_slot(self):
        accumulator = SlotAccumulator()
        completed: list = []
        for minute in range(0, 35, 5):
            grid = 2.0 if minute < 15 else -2.0
            completed += accumulator.add_sample(
                dt(3, 0) + timedelta(minutes=minute),
                pv_power_kw=0.0,
                load_power_kw=1.0,
                grid_power_kw=grid,
            )
        assert completed[0].grid_import_kwh > 0
        assert completed[0].grid_export_kwh > 0

    def test_no_grid_sensor_is_recorded_as_unmetered(self):
        """Zero must not be mistaken for "imported nothing"."""
        accumulator = SlotAccumulator()
        completed: list = []
        for minute in range(0, 35, 5):
            completed += accumulator.add_sample(
                dt(3, 0) + timedelta(minutes=minute),
                pv_power_kw=0.0,
                load_power_kw=1.0,
            )
        assert completed[0].grid_measured is False
        assert completed[0].grid_import_kwh == pytest.approx(0.0)


class TestLifetimeTally:
    """The log keeps two months. The saving since the start has to outlive it.

    Every money figure was computed over a window of surviving records, so
    nothing could say what the controller had saved since it was installed:
    the answer pruned itself a slot at a time.
    """

    @staticmethod
    def _shadow(soc: float = 50.0) -> SelfUseShadow:
        return SelfUseShadow(
            soc=soc,
            capacity_kwh=10.0,
            min_soc=10.0,
            max_soc=90.0,
            max_charge_kw=3.0,
            max_discharge_kw=3.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
        )

    @staticmethod
    def _day(day: int, soc: float = 40.0) -> list[SlotRecord]:
        """Cheap overnight, dear evening; the pack sits at ``soc`` throughout."""
        records = []
        for slot in range(48):
            hour, minute = divmod(slot * 30, 60)
            cheap = hour < 5
            records.append(
                SlotRecord(
                    start=datetime(2026, 2, day, hour, minute, tzinfo=UTC),
                    import_price=5.0 if cheap else 30.0,
                    pv_kwh=0.4 if 10 <= hour < 15 else 0.0,
                    load_kwh=0.3,
                    grid_import_kwh=1.5 if cheap else 0.0,
                    grid_measured=True,
                    soc_start=soc,
                    soc_end=soc,
                    battery_charge_kwh=1.2 if cheap else 0.0,
                    battery_discharge_kwh=0.0 if cheap else 0.3,
                    planned_action="charge" if cheap else "self_use",
                    applied_action="charge" if cheap or hour > 20 else "hold",
                    controlling=hour != 12,
                    pv_forecast_kwh=0.5 if 10 <= hour < 15 else 0.0,
                    load_forecast_kwh=0.35,
                )
            )
        return records

    def test_the_total_outlives_the_logs_retention(self):
        log = PerformanceLog(retention_days=1)
        tally = LifetimeTally()
        shadow = self._shadow()
        for day in range(15, 20):
            for item in self._day(day):
                log.add(item)
                tally.add(item, shadow)
        assert len(log) < 5 * 48
        assert tally.slots == 5 * 48
        assert tally.since == dt(0, 0, 15)
        assert tally.summary().days == pytest.approx(5.0)

    def test_seeding_from_the_log_matches_the_windowed_summary(self):
        """Adding slots one at a time must give the same answer as summarising
        them all at once, field for field -- otherwise the two tables on the
        dashboard would disagree about the same week."""
        records = [item for day in range(15, 19) for item in self._day(day)]
        # The real battery sat at 40%; the counterfactual starts there too.
        windowed = summarise(
            records, cycle_cost=2.0, usable_kwh=8.0, shadow=self._shadow(soc=40.0)
        )
        tally = LifetimeTally()
        # The template's own charge is irrelevant: the tally starts the
        # counterfactual where the first record says the real battery was.
        assert tally.seed(records, self._shadow(soc=85.0)) == len(records)
        lifetime = tally.summary(
            cycle_cost=2.0,
            usable_kwh=8.0,
            capacity_kwh=10.0,
            discharge_efficiency=0.95,
            stored_energy_rate=windowed.stored_energy_rate,
        )
        for name in (
            "slots",
            "days",
            "controlled_slots",
            "grid_measured_slots",
            "compared_slots",
            "followed_slots",
            "pv_kwh",
            "load_kwh",
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_charge_kwh",
            "battery_discharge_kwh",
            "cost",
            "no_battery_cost",
            "self_use_cost",
            "wear_cost",
            "stored_energy_kwh",
            "stored_energy_value",
            "saving_vs_self_use",
            "net_saving_vs_self_use",
            "pv_mae",
            "pv_bias",
            "load_mae",
            "load_bias",
            "plan_fidelity",
            "round_trip_efficiency",
            "equivalent_full_cycles",
        ):
            assert getattr(lifetime, name) == pytest.approx(getattr(windowed, name)), name
        assert lifetime.first == windowed.first
        assert lifetime.last == windowed.last
        assert set(lifetime.as_dict()) == set(windowed.as_dict())

    def test_the_same_half_hour_recorded_twice_is_swapped_not_doubled(self):
        """A restart on the slot boundary can close the same half-hour twice.
        The log replaces the row; the running total has to do the same --
        including winding the shadow battery back before re-stepping it."""
        first = record(
            1, import_price=10.0, load_kwh=1.0, grid_import_kwh=1.0, soc_start=50.0
        )
        provisional = record(
            2, import_price=10.0, load_kwh=1.5, grid_import_kwh=1.5, soc_end=40.0
        )
        final = record(
            2, import_price=10.0, load_kwh=0.5, grid_import_kwh=0.5, soc_end=45.0
        )
        tally = LifetimeTally()
        shadow = self._shadow()
        tally.add(first, shadow)
        tally.add(provisional, shadow)
        drained = tally.shadow_soc
        assert tally.add(final, shadow) is True

        assert tally.slots == 2
        assert tally.cost == pytest.approx(10.0 + 5.0)
        assert tally.last_soc_end == 45.0
        clean = LifetimeTally()
        clean.seed([first, final], self._shadow())
        # The shadow covered 1.5 kWh provisionally and only 0.5 kWh finally,
        # so it must end fuller than it did after the provisional slot.
        assert tally.shadow_soc > drained
        assert tally.shadow_soc == pytest.approx(clean.shadow_soc)
        assert tally.self_use_cost == pytest.approx(clean.self_use_cost)
        assert tally.load_kwh == pytest.approx(clean.load_kwh)

    def test_a_half_hour_arriving_late_is_not_counted(self):
        """The counterfactual is sequential; a slot from the past cannot be
        stepped into the middle of it."""
        tally = LifetimeTally()
        shadow = self._shadow()
        tally.add(record(2, load_kwh=0.3), shadow)
        assert tally.add(record(1, load_kwh=0.3), shadow) is False
        assert tally.slots == 1
        assert tally.since == dt(2)

    def test_the_swap_survives_a_restart(self):
        """The case the swap exists for *is* a restart, so the undo state has
        to be in the file, not in memory: the totals, the shadow's charge and
        the real battery's last reading all wind back."""
        import json

        first = record(
            1,
            import_price=10.0,
            load_kwh=1.0,
            grid_import_kwh=1.0,
            soc_start=50.0,
            soc_end=50.0,
        )
        tally = LifetimeTally()
        shadow = self._shadow()
        tally.add(first, shadow)
        tally.add(
            record(2, import_price=10.0, load_kwh=2.0, grid_import_kwh=2.0, soc_end=48.0),
            shadow,
        )
        restored = LifetimeTally.from_dict(json.loads(json.dumps(tally.as_dict())))
        # After a restart the slot closes with no marks at all: no soc_end.
        again = record(2, import_price=10.0, load_kwh=0.5, grid_import_kwh=0.5)
        assert restored.add(again, self._shadow()) is True
        assert restored.slots == 2
        assert isinstance(restored.slots, int)
        assert restored.cost == pytest.approx(15.0)
        assert restored.last_soc_end == 50.0
        clean = LifetimeTally()
        clean.seed([first, again], self._shadow())
        assert restored.shadow_soc == pytest.approx(clean.shadow_soc)
        assert restored.self_use_cost == pytest.approx(clean.self_use_cost)

    def test_a_same_half_hour_with_no_undo_state_is_left_alone(self):
        """Counting it again would be worse than keeping the old row."""
        import json

        tally = LifetimeTally()
        tally.add(
            record(1, import_price=10.0, load_kwh=1.0, grid_import_kwh=1.0),
            self._shadow(),
        )
        data = json.loads(json.dumps(tally.as_dict()))
        del data["last_delta"]
        restored = LifetimeTally.from_dict(data)
        again = record(1, import_price=10.0, load_kwh=2.0, grid_import_kwh=2.0)
        assert restored.add(again, self._shadow()) is False
        assert restored.slots == 1
        assert restored.cost == pytest.approx(10.0)

    def test_persistence_round_trip(self):
        import json

        tally = LifetimeTally()
        tally.seed(self._day(15), self._shadow())
        restored = LifetimeTally.from_dict(json.loads(json.dumps(tally.as_dict())))
        assert restored.as_dict() == tally.as_dict()
        assert restored.since == tally.since
        # And it carries on from where it was, not from zero.
        shadow = self._shadow()
        for item in self._day(16):
            tally.add(item, shadow)
            restored.add(item, shadow)
        assert restored.summary().as_dict() == tally.summary().as_dict()

    def test_swapping_the_only_half_hour_restarts_cleanly(self):
        tally = LifetimeTally()
        shadow = self._shadow()
        tally.add(record(1, soc_start=30.0, soc_end=30.0, load_kwh=0.3), shadow)
        tally.add(record(1, soc_start=60.0, soc_end=60.0, load_kwh=0.3), shadow)
        assert tally.slots == 1
        assert tally.first_soc_start == 60.0
        # The counterfactual started where the *kept* record says the battery was.
        clean = LifetimeTally()
        clean.add(record(1, soc_start=60.0, soc_end=60.0, load_kwh=0.3), self._shadow())
        assert tally.shadow_soc == pytest.approx(clean.shadow_soc)

    def test_wear_is_charged_at_todays_allowance(self):
        """Not banked at the allowance of the day: correcting the setting
        corrects the whole history, as it does for the weekly figure."""
        tally = LifetimeTally()
        tally.seed([record(1, battery_discharge_kwh=10.0)], self._shadow())
        assert tally.summary(cycle_cost=2.0).wear_cost == pytest.approx(20.0)
        assert tally.summary(cycle_cost=3.0).wear_cost == pytest.approx(30.0)

    def test_leftover_charge_is_credited_against_the_shadow(self):
        """The same like-for-like rule the weekly report applies: what the real
        battery holds beyond the counterfactual is bought and still there."""
        tally = LifetimeTally()
        # Nothing for the house to draw, so the shadow stays put at 50%.
        tally.add(record(1, soc_start=50.0, soc_end=90.0), self._shadow())
        summary = tally.summary(
            capacity_kwh=10.0, discharge_efficiency=0.95, stored_energy_rate=10.0
        )
        assert summary.stored_energy_kwh == pytest.approx(4.0 * 0.95)
        assert summary.stored_energy_value == pytest.approx(38.0)
        assert summary.net_saving_vs_self_use == pytest.approx(
            summary.saving_vs_self_use + 38.0
        )
        assert any("holds 3.8 kWh more" in note for note in summary.notes)

    def test_without_a_recorded_charge_nothing_is_invented(self):
        tally = LifetimeTally()
        tally.add(record(1, load_kwh=0.3, import_price=10.0), self._shadow())
        summary = tally.summary(capacity_kwh=10.0, stored_energy_rate=10.0)
        assert summary.stored_energy_kwh == pytest.approx(0.0)

    def test_an_empty_tally_says_so(self):
        tally = LifetimeTally()
        assert tally.is_empty
        summary = tally.summary()
        assert summary.slots == 0
        assert "no records yet" in summary.notes
        assert summary.net_saving_vs_self_use is None

    def test_unreadable_totals_start_fresh_rather_than_raise(self):
        assert LifetimeTally.from_dict(None).is_empty
        assert LifetimeTally.from_dict("junk").is_empty
        assert LifetimeTally.from_dict({"since": "not a date", "slots": 3}).is_empty
        assert LifetimeTally.from_dict({"since": dt(1).isoformat()}).is_empty
        assert LifetimeTally.from_dict({"cost": "a lot"}).is_empty

    def test_the_summary_reports_per_day(self):
        tally = LifetimeTally()
        tally.seed(
            [item for day in range(15, 19) for item in self._day(day)], self._shadow()
        )
        summary = tally.summary(cycle_cost=1.0)
        assert summary.days == pytest.approx(4.0)
        assert summary.net_saving_per_day == pytest.approx(
            summary.net_saving_vs_self_use / 4.0
        )
        assert summary.as_dict()["money"]["net_saving_per_day"] == pytest.approx(
            summary.net_saving_per_day, abs=0.01
        )

    def test_a_first_slot_without_a_reading_does_not_pin_the_shadow_to_the_floor(
        self,
    ):
        """The first slot after a boot often closes before the inverter has
        reported, so it carries no opening charge. Starting the counterfactual
        at the floor from that one slot banked the real battery's whole
        pre-existing charge as a saving, for ever."""
        unread = record(0, load_kwh=1.4, import_price=30.0, soc_end=80.0)
        rest = [
            record(h, load_kwh=1.4, import_price=30.0, soc_start=80.0, soc_end=80.0)
            for h in range(1, 8)
        ]
        records = [unread, *rest]
        windowed = summarise(records, shadow=self._shadow(soc=80.0))

        seeded = LifetimeTally()
        seeded.seed(records, self._shadow(soc=10.0))
        assert seeded.first_soc_start == 80.0
        assert seeded.self_use_cost == pytest.approx(windowed.self_use_cost)

        live = LifetimeTally()
        shadow = self._shadow(soc=10.0)
        for item in records:
            live.add(item, shadow)
        assert live.first_soc_start == 80.0
        assert live.self_use_cost == pytest.approx(windowed.self_use_cost)
        assert live.shadow_soc == pytest.approx(seeded.shadow_soc)

    def test_a_swapped_baseline_slot_re_bases_the_shadow(self):
        def reading(soc: float) -> SlotRecord:
            return record(1, load_kwh=0.3, import_price=20.0, soc_start=soc, soc_end=soc)

        tally = LifetimeTally()
        shadow = self._shadow(soc=10.0)
        tally.add(record(0, load_kwh=0.3, import_price=20.0), shadow)
        tally.add(reading(60.0), shadow)
        assert tally.first_soc_start == 60.0
        tally.add(reading(70.0), shadow)
        assert tally.first_soc_start == 70.0
        # Live, slot by slot, as the controller itself would have counted it.
        clean = LifetimeTally()
        fresh = self._shadow(soc=10.0)
        clean.add(record(0, load_kwh=0.3, import_price=20.0), fresh)
        clean.add(reading(70.0), fresh)
        assert tally.shadow_soc == pytest.approx(clean.shadow_soc)
        assert tally.self_use_cost == pytest.approx(clean.self_use_cost)
