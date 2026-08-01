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
