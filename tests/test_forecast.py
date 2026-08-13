"""Tests for the learning model and the solar/load/weather forecasters."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.ess_controller.forecast.energy import EnergySeries, EnergySlot
from custom_components.ess_controller.forecast.load import (
    LoadForecaster,
    default_slot_load,
)
from custom_components.ess_controller.forecast.solar import (
    SolarForecaster,
    build_forecast_series,
    parse_solar_forecast_attributes,
)
from custom_components.ess_controller.forecast.weather import (
    WeatherPoint,
    WeatherSeries,
)
from custom_components.ess_controller.learning.model import (
    BucketSet,
    EwmaBucket,
    LearningModel,
    LoadObservation,
    SolarObservation,
    cloud_bucket,
    day_type,
    sky_key,
    temperature_bucket,
    uv_bucket,
)
from custom_components.ess_controller.sampling import SlotAccumulator, slot_start_for

UTC = UTC


def dt(hour: int, minute: int = 0, day: int = 15, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


class TestBuckets:
    def test_first_sample_becomes_the_mean(self):
        bucket = EwmaBucket()
        bucket.update(4.0)
        assert bucket.mean == pytest.approx(4.0)
        assert bucket.samples == 1

    def test_converges_towards_repeated_value(self):
        bucket = EwmaBucket()
        for _ in range(30):
            bucket.update(2.0)
        assert bucket.mean == pytest.approx(2.0, abs=1e-6)

    def test_tracks_a_step_change(self):
        bucket = EwmaBucket()
        for _ in range(20):
            bucket.update(1.0)
        for _ in range(20):
            bucket.update(5.0)
        # Should have moved most of the way to the new level.
        assert bucket.mean > 4.0

    def test_trusted_after_enough_samples(self):
        bucket = EwmaBucket()
        bucket.update(1.0)
        assert not bucket.trusted
        bucket.update(1.0)
        bucket.update(1.0)
        assert bucket.trusted

    def test_roundtrip_serialisation(self):
        bucket = EwmaBucket(mean=1.2345, samples=7)
        restored = EwmaBucket.from_dict(bucket.as_dict())
        assert restored.samples == 7
        assert restored.mean == pytest.approx(1.2345, abs=1e-4)


class TestBucketSetFallback:
    def test_prefers_specific_trusted_bucket(self):
        bucket_set = BucketSet()
        for _ in range(5):
            bucket_set.update("specific", 10.0)
            bucket_set.update("broad", 99.0)
        value, source = bucket_set.lookup(["specific", "broad"], default=0.0)
        assert value == pytest.approx(10.0)
        assert source == "specific"

    def test_falls_back_when_specific_is_untrusted(self):
        bucket_set = BucketSet()
        bucket_set.update("specific", 10.0)  # only 1 sample
        for _ in range(5):
            bucket_set.update("broad", 99.0)
        value, source = bucket_set.lookup(["specific", "broad"], default=0.0)
        assert value == pytest.approx(99.0)
        assert source == "broad"

    def test_uses_untrusted_before_giving_up(self):
        bucket_set = BucketSet()
        bucket_set.update("specific", 10.0)
        value, source = bucket_set.lookup(["specific", "broad"], default=0.0)
        assert value == pytest.approx(10.0)
        assert source.endswith("~")

    def test_default_when_nothing_known(self):
        value, source = BucketSet().lookup(["a", "b"], default=1.23)
        assert value == 1.23
        assert source == "default"


class TestBinning:
    def test_cloud_buckets(self):
        assert cloud_bucket(0) == 0
        assert cloud_bucket(19) == 0
        assert cloud_bucket(20) == 1
        assert cloud_bucket(100) == 4
        assert cloud_bucket(150) == 4

    def test_missing_cloud_maps_to_middle(self):
        # Must not read as "clear sky", which would over-forecast solar.
        assert cloud_bucket(None) == 2

    def test_temperature_buckets_ordered(self):
        assert temperature_bucket(-5) == 0
        assert temperature_bucket(2) == 1
        assert temperature_bucket(18) == 4
        assert temperature_bucket(35) == 7
        assert temperature_bucket(None) == -1

    def test_day_type(self):
        assert day_type(0) == "weekday"
        assert day_type(4) == "weekday"
        assert day_type(5) == "weekend"
        assert day_type(6) == "weekend"
        assert day_type(2, is_holiday=True) == "weekend"


class TestSolarLearning:
    def test_learns_absolute_yield_without_a_forecast(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.9, cloud_cover=10.0)
            )
        predicted, source = model.predict_solar(
            month=7, hour=13, minute=0, cloud=10.0, forecast_kwh=None
        )
        assert predicted == pytest.approx(0.9, abs=0.05)
        assert source != "default"

    def test_cloud_cover_separates_predictions(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.95, cloud_cover=5.0)
            )
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.15, cloud_cover=95.0)
            )
        clear, _ = model.predict_solar(7, 13, 0, cloud=5.0)
        overcast, _ = model.predict_solar(7, 13, 0, cloud=95.0)
        assert clear > overcast * 2

    def test_learns_forecast_correction_factor(self):
        # A forecast that consistently over-predicts by 25% (shading, soiling).
        model = LearningModel()
        for _ in range(8):
            model.observe_solar(
                SolarObservation(
                    month=7,
                    hour=12,
                    minute=0,
                    kwh=0.75,
                    cloud_cover=20.0,
                    forecast_kwh=1.0,
                )
            )
        predicted, source = model.predict_solar(
            month=7, hour=12, minute=0, cloud=20.0, forecast_kwh=1.0
        )
        assert predicted == pytest.approx(0.75, abs=0.06)
        assert "forecast*" in source

    def test_ratio_correction_is_clamped(self):
        model = LearningModel()
        # Absurd observations should not create a runaway multiplier.
        for _ in range(10):
            model.observe_solar(
                SolarObservation(
                    month=7,
                    hour=12,
                    minute=0,
                    kwh=50.0,
                    cloud_cover=20.0,
                    forecast_kwh=1.0,
                )
            )
        predicted, _ = model.predict_solar(7, 12, 0, cloud=20.0, forecast_kwh=1.0)
        assert predicted <= 2.0

    def test_near_zero_forecast_does_not_train_ratio(self):
        model = LearningModel()
        for _ in range(5):
            model.observe_solar(
                SolarObservation(
                    month=7,
                    hour=4,
                    minute=0,
                    kwh=0.02,
                    cloud_cover=50.0,
                    forecast_kwh=0.001,
                )
            )
        assert len(model.solar_ratio) == 0

    def test_unknown_slot_returns_default(self):
        model = LearningModel()
        predicted, source = model.predict_solar(
            1, 3, 0, cloud=50.0, forecast_kwh=None, default_kwh=0.0
        )
        assert predicted == 0.0
        assert source == "default"


class TestLoadLearning:
    def test_learns_hourly_shape(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(LoadObservation(hour=18, minute=0, kwh=1.4, weekday=1))
            model.observe_load(LoadObservation(hour=3, minute=0, kwh=0.15, weekday=1))
        evening, _ = model.predict_load(18, 0, weekday=1)
        night, _ = model.predict_load(3, 0, weekday=1)
        assert evening > night * 5

    def test_learns_air_conditioning_from_temperature(self):
        """The headline behaviour: hot afternoons must predict higher load."""
        model = LearningModel()
        for _ in range(8):
            # Mild afternoon: no cooling.
            model.observe_load(
                LoadObservation(hour=14, minute=0, kwh=0.35, weekday=1, temperature=17.0)
            )
            # Hot afternoon: A/C running hard.
            model.observe_load(
                LoadObservation(hour=14, minute=0, kwh=1.6, weekday=1, temperature=29.0)
            )
        mild, mild_src = model.predict_load(14, 0, weekday=1, temperature=17.0)
        hot, hot_src = model.predict_load(14, 0, weekday=1, temperature=29.0)
        assert hot > mild * 3
        assert mild_src != "default" and hot_src != "default"
        # And the temperature bucket must be what distinguished them.
        assert "t" in hot_src

    def test_weekday_and_weekend_are_separated(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(LoadObservation(hour=10, minute=0, kwh=0.2, weekday=1))
            model.observe_load(LoadObservation(hour=10, minute=0, kwh=0.9, weekday=6))
        weekday, _ = model.predict_load(10, 0, weekday=1)
        weekend, _ = model.predict_load(10, 0, weekday=6)
        assert weekend > weekday * 2

    def test_half_hour_resolution_preserved(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(LoadObservation(hour=17, minute=0, kwh=0.4, weekday=1))
            model.observe_load(LoadObservation(hour=17, minute=30, kwh=1.5, weekday=1))
        first, _ = model.predict_load(17, 0, weekday=1)
        second, _ = model.predict_load(17, 30, weekday=1)
        assert second > first * 2


class TestModelPersistence:
    def test_roundtrip(self):
        model = LearningModel()
        for _ in range(4):
            model.observe_solar(
                SolarObservation(month=6, hour=12, minute=0, kwh=0.8, cloud_cover=10.0)
            )
            model.observe_load(
                LoadObservation(hour=12, minute=0, kwh=0.5, weekday=2, temperature=20.0)
            )
        payload = model.as_dict()

        restored = LearningModel()
        restored.load_dict(payload)
        assert restored.predict_solar(6, 12, 0, cloud=10.0)[0] == pytest.approx(
            model.predict_solar(6, 12, 0, cloud=10.0)[0]
        )
        assert restored.predict_load(12, 0, weekday=2, temperature=20.0)[0] == (
            pytest.approx(model.predict_load(12, 0, weekday=2, temperature=20.0)[0])
        )

    def test_load_dict_tolerates_garbage(self):
        model = LearningModel()
        model.load_dict({"solar_absolute": {"k": "not a dict"}, "bogus": 1})
        assert len(model.solar_absolute) == 0

    def test_reset_clears_everything(self):
        model = LearningModel()
        model.observe_load(LoadObservation(hour=1, minute=0, kwh=1.0))
        model.reset()
        assert model.load_samples == 0
        assert model.solar_samples == 0

    def test_confidence_reports_maturity(self):
        model = LearningModel()
        assert model.confidence()["load_maturity"] == 0.0
        for _ in range(48 * 14):
            model.observe_load(LoadObservation(hour=1, minute=0, kwh=1.0))
        assert model.confidence()["load_maturity"] == pytest.approx(1.0)


class TestEnergySeries:
    def test_resamples_hourly_to_half_hourly(self):
        series = EnergySeries([EnergySlot(start=dt(12), end=dt(13), kwh=1.0)])
        assert series.energy_between(dt(12), dt(12, 30)) == pytest.approx(0.5)
        assert series.energy_between(dt(12, 30), dt(13)) == pytest.approx(0.5)

    def test_sums_across_windows(self):
        series = EnergySeries(
            [
                EnergySlot(start=dt(12), end=dt(12, 30), kwh=0.4),
                EnergySlot(start=dt(12, 30), end=dt(13), kwh=0.6),
            ]
        )
        assert series.energy_between(dt(12), dt(13)) == pytest.approx(1.0)

    def test_partial_overlap(self):
        series = EnergySeries([EnergySlot(start=dt(12), end=dt(13), kwh=2.0)])
        assert series.energy_between(dt(12, 45), dt(13, 30)) == pytest.approx(0.5)

    def test_outside_range_is_zero(self):
        series = EnergySeries([EnergySlot(start=dt(12), end=dt(13), kwh=2.0)])
        assert series.energy_between(dt(20), dt(21)) == 0.0

    def test_covers(self):
        series = EnergySeries([EnergySlot(start=dt(12), end=dt(14), kwh=1.0)])
        assert series.covers(dt(12, 30), dt(13))
        assert not series.covers(dt(11), dt(13))

    def test_empty_series(self):
        series = EnergySeries()
        assert not series
        assert series.energy_between(dt(1), dt(2)) == 0.0


class TestSolarForecastParsing:
    def test_solcast_detailed_forecast(self):
        attributes = {
            "detailedForecast": [
                {"period_start": "2026-07-15T11:00:00+00:00", "pv_estimate": 1.6},
                {"period_start": "2026-07-15T11:30:00+00:00", "pv_estimate": 1.8},
            ]
        }
        slots = parse_solar_forecast_attributes(attributes)
        assert len(slots) == 2
        # 1.6 kW average over half an hour is 0.8 kWh.
        assert slots[0].kwh == pytest.approx(0.8)
        assert slots[1].kwh == pytest.approx(0.9)

    def test_forecast_solar_watt_hours_period(self):
        attributes = {
            "watt_hours_period": {
                "2026-07-15T11:00:00+00:00": 800,
                "2026-07-15T12:00:00+00:00": 950,
            }
        }
        slots = parse_solar_forecast_attributes(attributes)
        assert slots[0].kwh == pytest.approx(0.8)
        assert slots[1].kwh == pytest.approx(0.95)

    def test_forecast_solar_watts_treated_as_power(self):
        attributes = {
            "watts": {
                "2026-07-15T11:00:00+00:00": 1000,
                "2026-07-15T12:00:00+00:00": 2000,
            }
        }
        slots = parse_solar_forecast_attributes(attributes)
        # 1000 W held for an hour is 1 kWh.
        assert slots[0].kwh == pytest.approx(1.0)

    def test_hourly_forecast_periods_inferred(self):
        attributes = {
            "detailedHourly": [
                {"period_start": "2026-07-15T11:00:00+00:00", "pv_estimate": 2.0},
                {"period_start": "2026-07-15T12:00:00+00:00", "pv_estimate": 2.0},
            ]
        }
        slots = parse_solar_forecast_attributes(attributes)
        assert slots[0].duration_hours == pytest.approx(1.0)
        assert slots[0].kwh == pytest.approx(2.0)

    def test_unrecognised_attributes_yield_nothing(self):
        assert parse_solar_forecast_attributes({"state": "1.0"}) == []

    def test_merging_today_and_tomorrow_sensors(self):
        today = {
            "detailedForecast": [
                {"period_start": "2026-07-15T11:00:00+00:00", "pv_estimate": 1.0}
            ]
        }
        tomorrow = {
            "detailedForecast": [
                {"period_start": "2026-07-16T11:00:00+00:00", "pv_estimate": 2.0}
            ]
        }
        series = build_forecast_series([today, tomorrow])
        assert len(series) == 2
        assert series.total() == pytest.approx(0.5 + 1.0)


class TestWeatherSeries:
    def test_interpolates_temperature(self):
        series = WeatherSeries(
            [
                WeatherPoint(time=dt(12), temperature=20.0, cloud_coverage=0.0),
                WeatherPoint(time=dt(14), temperature=30.0, cloud_coverage=100.0),
            ]
        )
        assert series.temperature_at(dt(13)) == pytest.approx(25.0)
        assert series.cloud_at(dt(13)) == pytest.approx(50.0)

    def test_clamps_outside_range(self):
        series = WeatherSeries([WeatherPoint(time=dt(12), temperature=20.0)])
        assert series.temperature_at(dt(6)) == 20.0
        assert series.temperature_at(dt(23)) == 20.0

    def test_condition_used_when_cloud_missing(self):
        series = WeatherSeries(
            [WeatherPoint(time=dt(12), temperature=20.0, condition="sunny")]
        )
        assert series.cloud_at(dt(12)) == pytest.approx(5.0)

    def test_from_ha_forecast_payload(self):
        series = WeatherSeries.from_forecast(
            [
                {
                    "datetime": "2026-07-15T12:00:00+00:00",
                    "temperature": 28.0,
                    "cloud_coverage": 10,
                    "condition": "sunny",
                },
                {
                    "datetime": "2026-07-15T13:00:00+00:00",
                    "temperature": 30.0,
                    "cloud_coverage": 20,
                },
            ]
        )
        assert len(series) == 2
        assert series.max_temperature() == 30.0
        assert series.temperature_at(dt(12, 30)) == pytest.approx(29.0)

    def test_empty_series_is_falsy(self):
        series = WeatherSeries.from_forecast([])
        assert not series
        assert series.cloud_at(dt(12)) is None

    def test_malformed_entries_skipped(self):
        series = WeatherSeries.from_forecast(
            [{"temperature": 5}, "junk", {"datetime": "nope"}]
        )
        assert len(series) == 0


class TestSolarForecaster:
    def _local(self, moment: datetime) -> datetime:
        return moment

    def test_clamps_to_array_peak(self):
        model = LearningModel()
        forecaster = SolarForecaster(model, peak_power_kw=2.0)
        prediction = forecaster.predict_slot(
            dt(13), duration_hours=0.5, external_kwh=5.0, cloud=0.0
        )
        # 2 kW for half an hour cannot exceed 1 kWh.
        assert prediction.kwh <= 1.0

    def test_uses_forecast_when_model_is_empty(self):
        forecaster = SolarForecaster(LearningModel(), peak_power_kw=2.0)
        prediction = forecaster.predict_slot(
            dt(13), duration_hours=0.5, external_kwh=0.6, cloud=30.0
        )
        assert prediction.kwh == pytest.approx(0.6)

    def test_applies_learned_correction(self):
        model = LearningModel()
        for _ in range(8):
            model.observe_solar(
                SolarObservation(
                    month=7,
                    hour=13,
                    minute=0,
                    kwh=0.4,
                    cloud_cover=30.0,
                    forecast_kwh=0.8,
                )
            )
        forecaster = SolarForecaster(model, peak_power_kw=2.0)
        prediction = forecaster.predict_slot(
            dt(13), duration_hours=0.5, external_kwh=0.8, cloud=30.0
        )
        assert prediction.kwh < 0.6

    def test_predict_series_over_horizon(self):
        model = LearningModel()
        forecaster = SolarForecaster(model, peak_power_kw=2.0)
        external = EnergySeries([EnergySlot(start=dt(12), end=dt(13), kwh=1.6)])
        weather = WeatherSeries([WeatherPoint(time=dt(12), cloud_coverage=10.0)])
        slots = [(dt(12), dt(12, 30)), (dt(12, 30), dt(13))]
        predictions = forecaster.predict_series(slots, external, weather, self._local)
        assert len(predictions) == 2
        assert predictions[0].kwh == pytest.approx(0.8)

    def test_learning_disabled_passes_forecast_through(self):
        model = LearningModel()
        for _ in range(8):
            model.observe_solar(
                SolarObservation(
                    month=7,
                    hour=13,
                    minute=0,
                    kwh=0.1,
                    cloud_cover=30.0,
                    forecast_kwh=0.8,
                )
            )
        forecaster = SolarForecaster(model, peak_power_kw=2.0, use_learning=False)
        prediction = forecaster.predict_slot(dt(13), 0.5, external_kwh=0.8, cloud=30.0)
        assert prediction.kwh == pytest.approx(0.8)


class TestLoadForecaster:
    def _local(self, moment: datetime) -> datetime:
        return moment

    def test_default_profile_sums_to_daily_total(self):
        total = sum(
            default_slot_load(index // 2, (index % 2) * 30, 10.0) for index in range(48)
        )
        assert total == pytest.approx(10.0)

    def test_default_profile_has_evening_peak(self):
        evening = default_slot_load(18, 0, 10.0)
        night = default_slot_load(3, 0, 10.0)
        assert evening > night * 2

    def test_profile_used_before_learning(self):
        forecaster = LoadForecaster(LearningModel(), daily_kwh=10.0)
        prediction = forecaster.predict_slot(dt(18), 0.5)
        assert prediction.source == "profile"
        assert prediction.kwh == pytest.approx(default_slot_load(18, 0, 10.0))

    def test_learned_value_overrides_profile(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(
                LoadObservation(hour=18, minute=0, kwh=2.5, weekday=2, temperature=20.0)
            )
        forecaster = LoadForecaster(model, daily_kwh=10.0)
        prediction = forecaster.predict_slot(dt(18), 0.5, temperature=20.0)
        assert prediction.source != "profile"
        assert prediction.kwh == pytest.approx(2.5, abs=0.15)

    def test_cooling_uplift_applies_before_learning(self):
        forecaster = LoadForecaster(
            LearningModel(),
            daily_kwh=10.0,
            cooling_threshold_c=22.0,
            cooling_kwh_per_degree_hour=0.1,
        )
        mild = forecaster.predict_slot(dt(14), 0.5, temperature=18.0)
        hot = forecaster.predict_slot(dt(14), 0.5, temperature=32.0)
        # 10 degrees over threshold * 0.1 kWh/degree/hour * 0.5 h = 0.5 kWh.
        assert hot.kwh - mild.kwh == pytest.approx(0.5)
        assert hot.climate_uplift_kwh == pytest.approx(0.5)

    def test_cooling_uplift_not_double_counted_once_learned(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(
                LoadObservation(hour=14, minute=0, kwh=1.5, weekday=2, temperature=32.0)
            )
        forecaster = LoadForecaster(
            model,
            daily_kwh=10.0,
            cooling_threshold_c=22.0,
            cooling_kwh_per_degree_hour=0.1,
        )
        prediction = forecaster.predict_slot(dt(14), 0.5, temperature=32.0)
        assert prediction.climate_uplift_kwh == 0.0
        assert prediction.kwh == pytest.approx(1.5, abs=0.15)

    def test_partial_slot_scaled_down(self):
        forecaster = LoadForecaster(LearningModel(), daily_kwh=10.0)
        full = forecaster.predict_slot(dt(18), 0.5)
        partial = forecaster.predict_slot(dt(18), 0.25)
        assert partial.kwh == pytest.approx(full.kwh / 2)

    def test_predict_series_uses_weather_temperature(self):
        model = LearningModel()
        for _ in range(8):
            model.observe_load(
                LoadObservation(hour=14, minute=0, kwh=0.3, weekday=2, temperature=16.0)
            )
            model.observe_load(
                LoadObservation(hour=14, minute=0, kwh=1.9, weekday=2, temperature=30.0)
            )
        forecaster = LoadForecaster(model, daily_kwh=10.0)
        weather = WeatherSeries([WeatherPoint(time=dt(14), temperature=30.0)])
        predictions = forecaster.predict_series(
            [(dt(14), dt(14, 30))], weather, self._local
        )
        assert predictions[0].kwh > 1.0


class TestSlotAccumulator:
    def test_floors_to_slot_boundary(self):
        assert slot_start_for(dt(12, 17)) == dt(12, 0)
        assert slot_start_for(dt(12, 47)) == dt(12, 30)

    def test_integrates_power_into_energy(self):
        acc = SlotAccumulator()
        # 1 kW held from 12:00 to 12:30 is 0.5 kWh.
        acc.add_sample(dt(12, 0), 1.0, 2.0)
        for minute in (5, 10, 15, 20, 25, 30):
            completed = acc.add_sample(dt(12, minute), 1.0, 2.0)
        assert len(completed) == 1
        slot = completed[0]
        assert slot.start == dt(12, 0)
        assert slot.pv_kwh == pytest.approx(0.5, abs=0.01)
        assert slot.load_kwh == pytest.approx(1.0, abs=0.02)

    def test_splits_interval_across_slot_boundary(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 25), 2.0, 0.0)
        # A sample at 12:35 spans the boundary: 5 minutes belong to each slot.
        completed = acc.add_sample(dt(12, 35), 2.0, 0.0)
        assert len(completed) == 0  # coverage too low to commit
        assert acc.current_coverage == pytest.approx(5 / 30, abs=0.01)

    def test_low_coverage_slot_discarded(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 25), 5.0, 5.0)
        acc.add_sample(dt(12, 29), 5.0, 5.0)
        completed = acc.add_sample(dt(12, 31), 5.0, 5.0)
        # Only ~5 of 30 minutes observed, so the slot must not be committed.
        assert completed == []

    def test_long_gap_not_bridged(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 0), 3.0, 3.0)
        # A 45-minute gap means HA was asleep; do not invent energy.
        acc.add_sample(dt(12, 45), 3.0, 3.0)
        assert acc.current_coverage == 0.0

    def test_partial_coverage_scaled_up(self):
        acc = SlotAccumulator()
        # Observe 12:00-12:18 at 1 kW (60% of the slot), then Home Assistant
        # goes away until 13:05. The gap is too long to integrate across, so the
        # 12:00 slot commits on only the coverage it actually saw.
        acc.add_sample(dt(12, 0), 1.0, 1.0)
        for minute in (6, 12, 18):
            acc.add_sample(dt(12, minute), 1.0, 1.0)
        completed = acc.add_sample(dt(13, 5), 1.0, 1.0)
        assert len(completed) == 1
        # 0.3 kWh measured over 60% coverage scales to a 0.5 kWh half hour.
        assert completed[0].coverage == pytest.approx(0.6, abs=0.02)
        assert completed[0].pv_kwh == pytest.approx(0.5, abs=0.02)

    def test_full_coverage_when_a_sample_bridges_the_boundary(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 0), 1.0, 1.0)
        for minute in (6, 12, 18, 24):
            acc.add_sample(dt(12, minute), 1.0, 1.0)
        # This sample credits 12:24-12:30 to the old slot before rolling over,
        # so the slot is fully covered.
        completed = acc.add_sample(dt(12, 31), 1.0, 1.0)
        assert len(completed) == 1
        assert completed[0].coverage == pytest.approx(1.0, abs=0.02)
        assert completed[0].pv_kwh == pytest.approx(0.5, abs=0.01)

    def test_averages_weather_over_the_slot(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 0), 1.0, 1.0, cloud_cover=0.0, temperature=20.0)
        for minute in (10, 20, 30):
            completed = acc.add_sample(
                dt(12, minute), 1.0, 1.0, cloud_cover=60.0, temperature=26.0
            )
        assert len(completed) == 1
        assert completed[0].cloud_cover == pytest.approx(40.0)
        assert completed[0].temperature == pytest.approx(24.0)


class TestEndToEndLearning:
    """Feed simulated days through the accumulator into the model."""

    def test_two_weeks_of_hot_afternoons_teaches_ac_load(self):
        model = LearningModel()
        # Simulate 14 days: mild until day 7, then a heatwave.
        for day in range(1, 15):
            temperature = 17.0 if day <= 7 else 31.0
            load = 0.3 if day <= 7 else 1.7
            model.observe_load(
                LoadObservation(
                    hour=14,
                    minute=0,
                    kwh=load,
                    weekday=(day % 7),
                    temperature=temperature,
                )
            )
        cool, _ = model.predict_load(14, 0, weekday=1, temperature=17.0)
        hot, _ = model.predict_load(14, 0, weekday=1, temperature=31.0)
        assert hot > cool * 2

    def test_summer_solar_learned_separately_from_winter(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.95, cloud_cover=10.0)
            )
            model.observe_solar(
                SolarObservation(month=1, hour=13, minute=0, kwh=0.25, cloud_cover=10.0)
            )
        summer, _ = model.predict_solar(7, 13, 0, cloud=10.0)
        winter, _ = model.predict_solar(1, 13, 0, cloud=10.0)
        assert summer > winter * 2


class TestMetOfficeSkySignal:
    """The UK Met Office integration publishes hourly condition, temperature and
    UV index, but no numeric cloud coverage. UV is the better fallback: within a
    fixed season and half-hour the sun sits at roughly the same elevation, so
    variation in UV is mostly cloud attenuation."""

    def _metoffice_forecast(self):
        # Field names as the HA metoffice weather platform emits them.
        return [
            {
                "datetime": "2026-07-15T12:00:00+00:00",
                "condition": "partlycloudy",
                "temperature": 24.0,
                "apparent_temperature": 25.0,
                "precipitation_probability": 20,
                "uv_index": 6,
                "wind_speed": 4.1,
            },
            {
                "datetime": "2026-07-15T13:00:00+00:00",
                "condition": "sunny",
                "temperature": 26.0,
                "uv_index": 7,
            },
        ]

    def test_uv_index_is_parsed(self):
        series = WeatherSeries.from_forecast(self._metoffice_forecast())
        assert series.has_uv_index()
        assert series.uv_index_at(dt(12)) == pytest.approx(6.0)
        assert series.uv_index_at(dt(12, 30)) == pytest.approx(6.5)

    def test_no_measured_cloud_reported(self):
        series = WeatherSeries.from_forecast(self._metoffice_forecast())
        assert not series.has_measured_cloud()
        assert series.measured_cloud_at(dt(12)) is None
        # The condition-derived estimate is still available as a last resort.
        assert series.cloud_at(dt(12)) == pytest.approx(45.0)

    def test_sky_signal_kind_reports_uv(self):
        series = WeatherSeries.from_forecast(self._metoffice_forecast())
        assert series.sky_signal_kind() == "uv_index"

    def test_sky_signal_prefers_measured_cloud_when_present(self):
        series = WeatherSeries.from_forecast(
            [
                {
                    "datetime": "2026-07-15T12:00:00+00:00",
                    "cloud_coverage": 30,
                    "uv_index": 6,
                }
            ]
        )
        assert series.sky_signal_kind() == "cloud_coverage"
        assert series.measured_cloud_at(dt(12)) == pytest.approx(30.0)

    def test_sky_signal_falls_back_to_condition(self):
        series = WeatherSeries.from_forecast(
            [{"datetime": "2026-07-15T12:00:00+00:00", "condition": "cloudy"}]
        )
        assert series.sky_signal_kind() == "condition"

    def test_temperature_still_available_for_load_model(self):
        """A/C learning depends on this, and Met Office does provide it."""
        series = WeatherSeries.from_forecast(self._metoffice_forecast())
        assert series.temperature_at(dt(12)) == pytest.approx(24.0)
        assert series.max_temperature() == pytest.approx(26.0)


class TestUvBucketing:
    def test_buckets_by_index(self):
        assert uv_bucket(0.0) == -1
        assert uv_bucket(None) == -1
        assert uv_bucket(1.0) == 1
        assert uv_bucket(6.4) == 6
        assert uv_bucket(20.0) == 8

    def test_low_uv_is_treated_as_uninformative(self):
        # A UK winter noon reads ~0-1 whatever the sky is doing.
        assert uv_bucket(0.2) == -1

    def test_sky_key_prefers_cloud(self):
        assert sky_key(cloud=30.0, uv_index=6.0) == "c1"

    def test_sky_key_uses_uv_when_cloud_missing(self):
        assert sky_key(cloud=None, uv_index=6.0) == "u6"

    def test_sky_key_signals_do_not_collide(self):
        """Buckets learned from cloud must never be read as UV buckets."""
        assert sky_key(cloud=60.0) != sky_key(cloud=None, uv_index=3.0)

    def test_sky_key_neutral_when_nothing_known(self):
        # Must not read as clear sky, which would over-forecast solar.
        assert sky_key(cloud=None, uv_index=None) == f"c{cloud_bucket(None)}"


class TestSolarLearningFromUv:
    def test_learns_yield_against_uv_index(self):
        """The Met Office path: no cloud cover, so UV must carry the signal."""
        model = LearningModel()
        for _ in range(6):
            # Bright afternoon.
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.92, uv_index=7.0)
            )
            # Overcast afternoon, same slot and season.
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.21, uv_index=2.0)
            )
        bright, bright_src = model.predict_solar(7, 13, 0, cloud=None, uv_index=7.0)
        dull, dull_src = model.predict_solar(7, 13, 0, cloud=None, uv_index=2.0)
        assert bright > dull * 3
        assert bright_src != "default" and dull_src != "default"
        assert "u7" in bright_src

    def test_cloud_and_uv_observations_do_not_contaminate_each_other(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_solar(
                SolarObservation(month=7, hour=13, minute=0, kwh=0.9, uv_index=7.0)
            )
        # A cloud-keyed lookup must not pick up the UV-keyed bucket at the
        # specific level; it falls back to the season/slot aggregate instead.
        _, source = model.predict_solar(7, 13, 0, cloud=10.0)
        assert "c0" not in source

    def test_forecast_correction_learned_against_uv(self):
        model = LearningModel()
        for _ in range(8):
            model.observe_solar(
                SolarObservation(
                    month=7, hour=12, minute=0, kwh=0.6, uv_index=5.0, forecast_kwh=1.0
                )
            )
        predicted, source = model.predict_solar(
            7, 12, 0, cloud=None, uv_index=5.0, forecast_kwh=1.0
        )
        assert predicted == pytest.approx(0.6, abs=0.07)
        assert "forecast*" in source


class TestSolarForecasterUvPath:
    def _local(self, moment: datetime) -> datetime:
        return moment

    def test_predict_series_uses_uv_when_cloud_absent(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_solar(
                SolarObservation(month=7, hour=12, minute=0, kwh=0.85, uv_index=7.0)
            )
            model.observe_solar(
                SolarObservation(month=7, hour=12, minute=0, kwh=0.15, uv_index=1.0)
            )
        forecaster = SolarForecaster(model, peak_power_kw=2.0)
        bright = WeatherSeries.from_forecast(
            [{"datetime": "2026-07-15T12:00:00+00:00", "uv_index": 7, "temperature": 25}]
        )
        dull = WeatherSeries.from_forecast(
            [{"datetime": "2026-07-15T12:00:00+00:00", "uv_index": 1, "temperature": 15}]
        )
        slots = [(dt(12), dt(12, 30))]
        hot = forecaster.predict_series(slots, None, bright, self._local)
        cold = forecaster.predict_series(slots, None, dull, self._local)
        assert hot[0].kwh > cold[0].kwh * 3
        assert hot[0].uv_index == pytest.approx(7.0)


class TestSlotAccumulatorUv:
    def test_averages_uv_over_the_slot(self):
        acc = SlotAccumulator()
        # Readings at 12:00, 12:10 and 12:20 belong to the 12:00 slot; the 12:30
        # sample rolls the slot over, so its reading belongs to the next one.
        for minute, uv in ((0, 4.0), (10, 6.0), (20, 8.0)):
            acc.add_sample(dt(12, minute), 1.0, 1.0, uv_index=uv)
        completed = acc.add_sample(dt(12, 30), 1.0, 1.0, uv_index=9.0)
        assert len(completed) == 1
        assert completed[0].uv_index == pytest.approx(6.0)

    def test_uv_absent_stays_none(self):
        acc = SlotAccumulator()
        acc.add_sample(dt(12, 0), 1.0, 1.0, cloud_cover=20.0)
        for minute in (10, 20, 30):
            completed = acc.add_sample(dt(12, minute), 1.0, 1.0, cloud_cover=20.0)
        assert completed[0].uv_index is None
        assert completed[0].cloud_cover == pytest.approx(20.0)


class TestSeasonalLoadLearning:
    """The stated requirement: "at this time of year, with this weather, we used
    on average this much at this time"."""

    def test_same_slot_and_temperature_split_by_season(self):
        """A 12 degree December evening is not a 12 degree June evening: the
        lights and cooking are on in one and not the other, and temperature
        cannot tell them apart."""
        model = LearningModel()
        for _ in range(6):
            model.observe_load(
                LoadObservation(
                    hour=17, minute=0, kwh=1.6, weekday=1, temperature=12.0, month=12
                )
            )
            model.observe_load(
                LoadObservation(
                    hour=17, minute=0, kwh=0.4, weekday=1, temperature=12.0, month=6
                )
            )
        winter, winter_src = model.predict_load(
            17, 0, weekday=1, temperature=12.0, month=12
        )
        summer, summer_src = model.predict_load(
            17, 0, weekday=1, temperature=12.0, month=6
        )
        assert winter > summer * 2
        assert "season" in winter_src
        assert "season" in summer_src

    def test_temperature_still_separates_within_a_season(self):
        """A/C learning must survive the extra key level."""
        model = LearningModel()
        for _ in range(8):
            model.observe_load(
                LoadObservation(
                    hour=14, minute=0, kwh=0.35, weekday=1, temperature=17.0, month=7
                )
            )
            model.observe_load(
                LoadObservation(
                    hour=14, minute=0, kwh=1.7, weekday=1, temperature=29.0, month=7
                )
            )
        mild, _ = model.predict_load(14, 0, weekday=1, temperature=17.0, month=7)
        hot, _ = model.predict_load(14, 0, weekday=1, temperature=29.0, month=7)
        assert hot > mild * 3

    def test_falls_back_to_season_agnostic_while_sparse(self):
        """Adding season quadruples the buckets. Until a seasonal bucket has
        enough samples the broader one must answer, so nothing is lost early."""
        model = LearningModel()
        # Plenty of history overall, but none in August specifically.
        for _ in range(8):
            model.observe_load(
                LoadObservation(
                    hour=18, minute=0, kwh=1.2, weekday=1, temperature=16.0, month=5
                )
            )
        value, source = model.predict_load(18, 0, weekday=1, temperature=16.0, month=8)
        assert value == pytest.approx(1.2, abs=0.15)
        assert not source.startswith("season2")

    def test_month_omitted_still_works(self):
        """Callers that do not supply a month must keep working unchanged."""
        model = LearningModel()
        for _ in range(6):
            model.observe_load(LoadObservation(hour=8, minute=0, kwh=0.9, weekday=1))
        value, source = model.predict_load(8, 0, weekday=1)
        assert value == pytest.approx(0.9, abs=0.1)
        assert source != "default"

    def test_key_order_is_most_specific_first(self):
        model = LearningModel()
        keys = model.load_keys(
            17, 0, weekday=1, is_holiday=False, temperature=12.0, month=12
        )
        assert keys[0] == "season0:weekday:s34:t3"
        # Season drops out before temperature, then day type.
        assert keys[1] == "weekday:s34:t3"
        assert keys[-1] == "any:s34"

    def test_forecaster_uses_the_slot_month(self):
        model = LearningModel()
        for _ in range(6):
            model.observe_load(
                LoadObservation(
                    hour=17, minute=0, kwh=1.9, weekday=1, temperature=8.0, month=1
                )
            )
        forecaster = LoadForecaster(model, daily_kwh=10.0)
        january = forecaster.predict_slot(
            datetime(2026, 1, 13, 17, 0, tzinfo=UTC), 0.5, temperature=8.0
        )
        assert january.kwh == pytest.approx(1.9, abs=0.2)
        assert january.source != "profile"


class TestAccurateForecastIsLeftAlone:
    """A good forecast must not be corrected on the strength of one half-hour.

    The correction factor is a quotient of two small numbers, so a single slot
    where a cloud crossed a forecast-clear sky produces a ratio near the 0.25
    floor. Applied to every later slot in that bucket, it turns an accurate
    forecast into a bad one -- which is the opposite of what learning is for, and
    worst precisely for the installs whose forecast already knows the roof's tilt
    and azimuth and has no shading to discover.
    """

    def _model(self):
        from custom_components.ess_controller.learning.model import LearningModel

        return LearningModel()

    def _observe(self, model, *, kwh, forecast_kwh, times=1):
        from custom_components.ess_controller.learning.model import SolarObservation

        for _ in range(times):
            model.observe_solar(
                SolarObservation(
                    month=8,
                    hour=12,
                    minute=0,
                    kwh=kwh,
                    forecast_kwh=forecast_kwh,
                )
            )

    def _predict(self, model, forecast_kwh=1.0):
        return model.predict_solar(
            month=8, hour=12, minute=0, cloud=None, forecast_kwh=forecast_kwh
        )

    def test_one_bad_slot_does_not_move_the_forecast(self):
        model = self._model()
        # Forecast said 1.0 kWh; a passing cloud meant 0.3 was generated.
        self._observe(model, kwh=0.3, forecast_kwh=1.0)
        kwh, source = self._predict(model)
        assert kwh == pytest.approx(1.0), "a single sample corrected the forecast"
        assert "@default" in source

    def test_two_bad_slots_still_do_not(self):
        model = self._model()
        self._observe(model, kwh=0.3, forecast_kwh=1.0, times=2)
        assert self._predict(model)[0] == pytest.approx(1.0)

    def test_a_consistent_bias_is_corrected_once_it_is_established(self):
        """The feature still has to work: real shading must still be learned."""
        model = self._model()
        self._observe(model, kwh=0.5, forecast_kwh=1.0, times=6)
        kwh, source = self._predict(model)
        assert kwh < 0.75, kwh
        assert "@default" not in source

    def test_an_accurate_forecast_stays_accurate_as_evidence_accumulates(self):
        """Your case: the ratio converges on 1.0 and does nothing, as it should."""
        model = self._model()
        self._observe(model, kwh=1.0, forecast_kwh=1.0, times=10)
        assert self._predict(model)[0] == pytest.approx(1.0, rel=0.05)

    def test_absolute_solar_history_still_uses_a_single_sample(self):
        """The relaxation is only for ratios. An absolute observation of your own
        generation is directly informative, and one beats a clear-sky guess."""
        from custom_components.ess_controller.learning.model import SolarObservation

        model = self._model()
        model.observe_solar(SolarObservation(month=8, hour=12, minute=0, kwh=0.44))
        kwh, source = model.predict_solar(
            month=8, hour=12, minute=0, cloud=None, forecast_kwh=None, default_kwh=-1.0
        )
        assert kwh == pytest.approx(0.44)
        assert source != "default"


class TestCorrectionIsVisible:
    """The correction has to be inspectable, not a matter of faith.

    "It is learning, trust me" is not good enough for something spending money,
    and the absence of any readout is why a forecast being used at face value
    looked identical to a forecast being ignored.
    """

    def _model(self, samples=0, ratio_kwh=0.5):
        from custom_components.ess_controller.learning.model import (
            LearningModel,
            SolarObservation,
        )

        model = LearningModel()
        for _ in range(samples):
            model.observe_solar(
                SolarObservation(
                    month=8, hour=12, minute=0, kwh=ratio_kwh, forecast_kwh=1.0
                )
            )
        return model

    def _describe(self, model):
        return model.describe_solar_correction(month=8, hour=12, minute=0)

    def test_a_fresh_model_says_it_is_using_the_forecast_as_published(self):
        described = self._describe(self._model())
        assert described["applied_ratio"] == 1.0
        assert described["source"] == "default"

    def test_it_reports_how_many_samples_a_bucket_needs(self):
        described = self._describe(self._model())
        assert described["samples_needed_to_trust"] >= 2

    def test_every_bucket_level_is_listed_with_its_sample_count(self):
        described = self._describe(self._model(samples=2))
        assert [level["samples"] for level in described["levels"]] == [2, 2, 2, 2]
        assert all(level["trusted"] is False for level in described["levels"])

    def test_progress_towards_trust_is_visible_before_it_applies(self):
        """Two samples in: nothing applied yet, but the evidence is on show."""
        described = self._describe(self._model(samples=2))
        assert described["applied_ratio"] == 1.0
        assert described["levels"][0]["ratio"] == pytest.approx(0.5, abs=0.05)

    def test_once_trusted_the_applied_ratio_reflects_it(self):
        described = self._describe(self._model(samples=5))
        assert described["applied_ratio"] < 0.8
        assert described["source"] != "default"
        assert described["levels"][0]["trusted"] is True

    def test_the_broadest_bucket_becomes_trusted_in_three_observations(self):
        """One observation of a given half-hour per day, so about three days."""
        described = self._describe(self._model(samples=3))
        assert described["source"] != "default", described


class TestForecastConfidence:
    """A young forecast is provisioned around, and only where it is wrong.

    On a real install the flat default shape put 17:00-23:30 at 3.9 kWh against about
    6.6 kWh actually used -- 2.7 kWh of oven, dishwasher and kettle arriving
    unannounced, in the dearest half-hours of the day. Entering that evening at 43%
    on a pack floored at 20% means running out part-way through and buying the rest
    at 46-58p.

    Two earlier shapes for the fix are recorded here because both were wrong in
    instructive ways. Marking the whole day's load up cost about 150p a day on a real
    horizon to hedge a shortfall worth about 73p. Raising the floor pointed the wrong
    way entirely: a floor protects charge for later, when what is wanted is more
    charge arriving at the evening.
    """

    @staticmethod
    def hours_and_loads(load_per_slot: float = 0.3):
        hours = [h for h in range(24) for _ in (0, 1)]
        return hours, [load_per_slot] * len(hours)

    def test_an_untrained_model_provisions_for_more_evening(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
        )

        assert evening_allowance_kwh(0.0) >= 2.7

    def test_a_mature_model_is_taken_at_its_word(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
            evening_uplift,
        )

        hours, loads = self.hours_and_loads()
        assert evening_allowance_kwh(1.0) == 0.0
        assert evening_uplift(hours, loads, 1.0) == [0.0] * len(loads)

    def test_the_allowance_shrinks_as_the_model_learns(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
        )

        for lower, higher in ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0)):
            assert evening_allowance_kwh(lower) > evening_allowance_kwh(higher)

    def test_it_only_touches_the_evening(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_uplift,
            is_evening,
        )

        hours, loads = self.hours_and_loads()
        for hour, extra in zip(hours, evening_uplift(hours, loads, 0.0), strict=True):
            assert (extra > 0) == is_evening(hour)

    def test_the_whole_allowance_is_distributed(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
            evening_uplift,
        )

        hours, loads = self.hours_and_loads()
        total = sum(evening_uplift(hours, loads, 0.0))
        assert total == pytest.approx(evening_allowance_kwh(0.0))

    def test_it_lands_where_the_demand_already_is(self):
        """Proportional to the forecast, so it goes where people cook rather than
        smearing evenly across hours nobody is using."""
        from custom_components.ess_controller.forecast.confidence import evening_uplift

        hours = [16, 17, 18, 19]
        loads = [0.1, 0.1, 1.0, 0.1]
        uplift = evening_uplift(hours, loads, 0.0)
        assert uplift[2] == max(uplift)
        assert uplift[2] > uplift[0] * 5

    def test_an_empty_evening_forecast_is_spread_flat_not_discarded(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
            evening_uplift,
        )

        hours = [16, 17, 18, 19]
        uplift = evening_uplift(hours, [0.0] * 4, 0.0)
        assert sum(uplift) == pytest.approx(evening_allowance_kwh(0.0))
        assert len({round(x, 6) for x in uplift}) == 1

    def test_a_horizon_with_no_evening_in_it_is_left_alone(self):
        from custom_components.ess_controller.forecast.confidence import evening_uplift

        hours = [2, 3, 4, 5]
        assert evening_uplift(hours, [0.3] * 4, 0.0) == [0.0] * 4

    def test_it_never_asks_for_a_negative_allowance(self):
        from custom_components.ess_controller.forecast.confidence import (
            evening_allowance_kwh,
        )

        for confidence in (-1.0, 0.0, 0.3, 1.0, 2.0):
            assert evening_allowance_kwh(confidence) >= 0.0

    def test_it_says_what_it_is_doing(self):
        from custom_components.ess_controller.forecast.confidence import describe

        assert "still learning" in describe(0.0)
        assert "trusted" in describe(1.0)


class TestDailyTotalForecasts:
    """Forecast.Solar publishes daily totals, and those are what people configure.

    "Estimated energy production - today/tomorrow" carry no hourly breakdown. They
    parsed to nothing, so the estimate fell back to bare geometry without a word: on a
    real install that put a whole afternoon at 2 kWh while the array was busy filling
    the battery, and the user had configured the integration exactly as its own UI
    invited them to.
    """

    def test_the_day_is_read_from_the_sensor_name(self):
        from custom_components.ess_controller.forecast.solar import daily_total_offset

        assert daily_total_offset("energy_production_today") == 0
        assert daily_total_offset("energy_production_tomorrow") == 1

    def test_the_day_after_tomorrow_is_not_read_as_tomorrow(self):
        """Longest fragment first, or "day_after_tomorrow" matches "tomorrow"."""
        from custom_components.ess_controller.forecast.solar import daily_total_offset

        assert daily_total_offset("energy_production_day_after_tomorrow") == 2

    def test_a_sensor_that_names_no_day_is_declined(self):
        from custom_components.ess_controller.forecast.solar import daily_total_offset

        assert daily_total_offset("solar_energy_estimate") is None

    @staticmethod
    def predict(daily_totals=None, days: int = 3):
        from datetime import UTC, datetime, timedelta

        from custom_components.ess_controller.forecast.solar import SolarForecaster
        from custom_components.ess_controller.learning.model import LearningModel

        forecaster = SolarForecaster(
            LearningModel(), peak_power_kw=2.0, latitude=54.0, longitude=-0.2
        )
        start = datetime(2026, 6, 21, 0, 0, tzinfo=UTC)
        slots = [
            (start + timedelta(minutes=30 * i), start + timedelta(minutes=30 * (i + 1)))
            for i in range(48 * days)
        ]
        return (
            forecaster.predict_series(slots, None, None, lambda dt: dt, daily_totals),
            slots,
        )

    def test_a_whole_day_is_scaled_to_its_total(self):
        from datetime import date

        target = 9.0
        predictions, slots = self.predict({date(2026, 6, 22): target})
        scaled = sum(
            p.kwh
            for p, (s, _) in zip(predictions, slots, strict=True)
            if s.date() == date(2026, 6, 22)
        )
        assert scaled == pytest.approx(target, rel=0.01)

    def test_the_scaling_is_recorded_in_the_provenance(self):
        from datetime import date

        predictions, slots = self.predict({date(2026, 6, 22): 9.0})
        sources = {
            p.source
            for p, (s, _) in zip(predictions, slots, strict=True)
            if s.date() == date(2026, 6, 22) and p.kwh > 0
        }
        assert sources
        assert all("daily" in source for source in sources)

    def test_a_partial_day_is_left_alone(self):
        """A daily total covers hours already gone, so scaling what remains by it
        would cram a whole day into the last of the daylight."""
        from datetime import date

        predictions, slots = self.predict({date(2026, 6, 21): 9.0})
        first_day = [
            p
            for p, (s, _) in zip(predictions, slots, strict=True)
            if s.date() == date(2026, 6, 21)
        ]
        assert all("daily" not in p.source for p in first_day)

    def test_no_totals_changes_nothing(self):
        plain, _ = self.predict(None)
        assert all("daily" not in p.source for p in plain)

    def test_a_zero_total_is_honoured_not_ignored(self):
        """An overcast day forecast at zero is information, not a missing value."""
        from datetime import date

        predictions, slots = self.predict({date(2026, 6, 22): 0.0})
        scaled = sum(
            p.kwh
            for p, (s, _) in zip(predictions, slots, strict=True)
            if s.date() == date(2026, 6, 22)
        )
        assert scaled == pytest.approx(0.0, abs=1e-6)

    def test_a_day_with_no_daylight_is_not_divided_by_zero(self):
        from datetime import date

        from custom_components.ess_controller.forecast.solar import (
            SolarPrediction,
            _scale_to_daily_totals,
        )

        predictions = [SolarPrediction(kwh=0.0, source="clearsky") for _ in range(4)]
        dates = [date(2026, 12, 21)] * 4
        _scale_to_daily_totals(predictions, dates, {date(2026, 12, 21): 5.0})
        assert all(p.kwh == 0.0 for p in predictions)


class TestALearnedDayStopsBeingCrushedByADailyTotal:
    """The rescale ran last and unconditionally, so learning could never show.

    A real install's solar sensors published daily totals only. Its measured
    generation ran 40% above the forecast in every daylight half-hour, and the
    correction the model exists to make was overwritten immediately after being
    made: the whole day in the horizon -- the one that decides whether to buy
    into tonight's cheap window -- was pinned to a number the house had already
    disproved. The plan therefore bought 14 kWh from the grid at an average of
    28p to cover a shortfall that was never going to arrive.
    """

    from datetime import UTC, date, datetime, timedelta

    DAY = date(2026, 6, 22)

    def _forecaster(self, trained_kwh: float | None = None):
        from custom_components.ess_controller.forecast.solar import SolarForecaster
        from custom_components.ess_controller.learning.model import (
            MIN_SAMPLES_TRUSTED,
            LearningModel,
            SolarObservation,
        )

        model = LearningModel()
        if trained_kwh is not None:
            # Enough observations for the buckets to be trusted rather than the
            # single-sample fallback, which is still a guess.
            for _ in range(MIN_SAMPLES_TRUSTED + 1):
                for slot in range(48):
                    hour, minute = divmod(slot * 30, 60)
                    model.observe_solar(
                        SolarObservation(
                            month=6,
                            hour=hour,
                            minute=minute,
                            kwh=trained_kwh if 6 <= hour < 20 else 0.0,
                            cloud_cover=None,
                            uv_index=None,
                        )
                    )
        return SolarForecaster(model, peak_power_kw=2.0, latitude=54.0, longitude=-0.2)

    def _predict(self, forecaster, total: float | None):
        start = self.datetime(2026, 6, 21, 0, 0, tzinfo=self.UTC)
        slots = [
            (
                start + self.timedelta(minutes=30 * i),
                start + self.timedelta(minutes=30 * (i + 1)),
            )
            for i in range(48 * 3)
        ]
        totals = None if total is None else {self.DAY: total}
        predictions = forecaster.predict_series(slots, None, None, lambda dt: dt, totals)
        day = [
            p
            for p, (s, _) in zip(predictions, slots, strict=True)
            if s.date() == self.DAY
        ]
        return day

    def test_an_untrained_model_still_defers_to_the_total(self):
        """Day one, a clear-sky guess is worse than a real forecast. Unchanged."""
        day = self._predict(self._forecaster(), total=3.0)
        assert sum(p.kwh for p in day) == pytest.approx(3.0, rel=0.01)
        assert any("daily" in p.source for p in day)

    def test_a_single_observation_is_not_enough_to_override_it(self):
        """One half-hour is evidence of nothing, and ``lookup`` marks it ``~``."""
        from custom_components.ess_controller.forecast.solar import _is_trusted_bucket

        assert not _is_trusted_bucket("m6:s20:u4~")
        assert not _is_trusted_bucket("clearsky+cloud")
        assert not _is_trusted_bucket("default")
        assert _is_trusted_bucket("m6:s20:u4")

    def test_a_trained_model_keeps_its_own_answer(self):
        forecaster = self._forecaster(trained_kwh=0.5)
        day = self._predict(forecaster, total=3.0)
        learned_total = sum(p.kwh for p in day)
        # 28 daylight half-hours at 0.5 kWh, clamped by a 2 kW array to 1 kWh a
        # slot, so the learned day stands well clear of the 3 kWh being imposed.
        assert learned_total > 10.0
        assert all("daily" not in p.source for p in day)
        assert any(p.learned for p in day)

    def test_the_answer_is_the_same_with_or_without_the_total(self):
        """Once trained, the external total stops changing the prediction."""
        forecaster = self._forecaster(trained_kwh=0.5)
        with_total = [p.kwh for p in self._predict(forecaster, total=3.0)]
        without = [p.kwh for p in self._predict(forecaster, total=None)]
        assert with_total == pytest.approx(without)

    def test_a_thinly_learned_day_still_defers(self):
        """A majority, not one lucky bucket, takes a day off the forecast."""
        from custom_components.ess_controller.forecast.solar import (
            LEARNED_DAY_SHARE,
            SolarPrediction,
            _scale_to_daily_totals,
        )

        # One trusted slot carrying a fifth of the day, the rest guessed. The
        # day has to sit *inside* the horizon to be a candidate at all, so it is
        # bracketed by a slot of the day either side.
        before = self.date(2026, 6, 21)
        after = self.date(2026, 6, 23)
        predictions = [
            SolarPrediction(kwh=1.0, source="clearsky"),
            SolarPrediction(kwh=1.0, source="m6:s20", learned=True),
            *(SolarPrediction(kwh=1.0, source="clearsky") for _ in range(4)),
            SolarPrediction(kwh=1.0, source="clearsky"),
        ]
        dates = [before, *([self.DAY] * 5), after]
        assert 5.0 * LEARNED_DAY_SHARE > 1.0
        _scale_to_daily_totals(predictions, dates, {self.DAY: 2.5})
        assert sum(p.kwh for p in predictions[1:6]) == pytest.approx(2.5)


class TestTheTemperatureDialsAreVisible:
    """Two settings tripled a real forecast and nothing said so.

    Cooling and heating load-per-degree are quoted in kWh per degree per hour,
    which reads like a small number and is not: at 2.0 a four-degree day adds
    8 kW of continuous load. A real install set both to 2.0 and its 48-hour
    forecast went to 96 kWh against a measured 12.7 kWh a day. The plan bought
    31 kWh it would never use and held it through 45 half-hours while the house
    ran off the grid. Every figure on every screen was merely large.

    Not clamped, deliberately -- see ``climate_uplift``. What is testable is that
    the proportion gets said out loud.
    """

    def test_a_dominating_uplift_is_described(self):
        from custom_components.ess_controller.forecast.load import (
            describe_climate_uplift,
        )

        note = describe_climate_uplift(96.3, 61.0)
        assert note
        assert "63%" in note
        assert "kW per degree" in note

    def test_a_sane_uplift_says_nothing(self):
        from custom_components.ess_controller.forecast.load import (
            describe_climate_uplift,
        )

        # 0.1 kW/degree on a small house: real air conditioning, not a typo.
        assert describe_climate_uplift(33.0, 4.0) == ""

    def test_no_uplift_says_nothing(self):
        from custom_components.ess_controller.forecast.load import (
            describe_climate_uplift,
        )

        assert describe_climate_uplift(33.0, 0.0) == ""
        assert describe_climate_uplift(0.0, 0.0) == ""

    def test_the_real_settings_reproduce_the_real_forecast(self):
        """2.0 kW/degree against a 24.3 C day, as configured on the install."""
        from custom_components.ess_controller.forecast.load import (
            describe_climate_uplift,
        )

        forecaster = LoadForecaster(
            LearningModel(),
            daily_kwh=16.0,
            cooling_threshold_c=20.0,
            cooling_kwh_per_degree_hour=2.0,
        )
        hot = forecaster.predict_slot(dt(9), 0.5, temperature=24.3)
        # (24.3 - 20) * 2.0 * 0.5 = 4.3 kWh of uplift on a 0.30 kWh profile slot.
        assert hot.climate_uplift_kwh == pytest.approx(4.3, abs=0.01)
        assert hot.kwh == pytest.approx(4.6, abs=0.05)
        assert describe_climate_uplift(hot.kwh, hot.climate_uplift_kwh)
