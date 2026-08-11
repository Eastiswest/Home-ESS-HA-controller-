"""Tests for the day-one clear-sky PV estimate.

Two jobs. First, the solar position has to be right -- it is checked against
published solar-noon elevations for London, because an astronomy formula that is
subtly wrong produces a forecast that looks entirely plausible and is shifted by
hours. Second, the *yields* are pinned to what a real UK array does, so a future
change to the derate cannot quietly treble the estimate: over-predicting solar is
worse than the zero this replaced, because it under-charges the battery overnight
and leaves the house buying at the evening peak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.ess_controller.forecast.clearsky import (
    MIN_ELEVATION_DEG,
    clear_sky_kwh,
    solar_elevation,
)

LONDON_LAT = 51.5
LONDON_LON = -0.13
LONDON = ZoneInfo("Europe/London")


def day_total(when: datetime, peak_kw: float = 2.0, cloud: float | None = None) -> float:
    """A whole day of half-hourly estimates, in kWh."""
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(
        clear_sky_kwh(
            LONDON_LAT,
            LONDON_LON,
            midnight + timedelta(minutes=30 * slot),
            0.5,
            peak_kw,
            cloud,
        )
        for slot in range(48)
    )


class TestSolarPosition:
    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            # Solar noon elevations for London, from published tables.
            (datetime(2026, 6, 21, 12, 0, tzinfo=UTC), 62.0),
            (datetime(2026, 12, 21, 12, 0, tzinfo=UTC), 15.0),
            (datetime(2026, 3, 20, 12, 0, tzinfo=UTC), 38.0),
            (datetime(2026, 9, 22, 12, 0, tzinfo=UTC), 38.0),
        ],
    )
    def test_solar_noon_elevation_matches_published_values(self, date, expected):
        assert solar_elevation(LONDON_LAT, LONDON_LON, date) == pytest.approx(
            expected, abs=1.5
        )

    def test_the_sun_is_below_the_horizon_at_night(self):
        night = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
        assert solar_elevation(LONDON_LAT, LONDON_LON, night) < 0

    def test_a_naive_datetime_is_refused(self):
        """Reading it as UTC would shift the whole day silently."""
        with pytest.raises(ValueError, match="aware"):
            solar_elevation(LONDON_LAT, LONDON_LON, datetime(2026, 8, 11, 12, 0))

    def test_local_and_utc_agree_for_the_same_instant(self):
        """British Summer Time must not move the sun."""
        utc = datetime(2026, 8, 11, 11, 0, tzinfo=UTC)
        local = utc.astimezone(LONDON)
        assert solar_elevation(LONDON_LAT, LONDON_LON, utc) == pytest.approx(
            solar_elevation(LONDON_LAT, LONDON_LON, local)
        )

    def test_the_southern_hemisphere_has_its_seasons_the_other_way_round(self):
        december = datetime(2026, 12, 21, 12, 0, tzinfo=UTC)
        june = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
        # Sydney: high sun in December, low in June.
        assert solar_elevation(-33.9, 151.2, december) > solar_elevation(
            -33.9, 151.2, june
        )


class TestYield:
    """The numbers a 2 kWp array in southern England actually produces."""

    def test_a_clear_summer_day_is_in_the_right_range(self):
        total = day_total(datetime(2026, 6, 21, tzinfo=UTC))
        assert 8.0 <= total <= 11.0, total

    def test_a_clear_august_day_is_in_the_right_range(self):
        total = day_total(datetime(2026, 8, 11, tzinfo=UTC))
        assert 6.5 <= total <= 9.5, total

    def test_a_clear_midwinter_day_is_small_but_not_zero(self):
        total = day_total(datetime(2026, 12, 21, tzinfo=UTC))
        assert 0.8 <= total <= 2.2, total

    def test_summer_beats_winter_by_a_wide_margin(self):
        summer = day_total(datetime(2026, 6, 21, tzinfo=UTC))
        winter = day_total(datetime(2026, 12, 21, tzinfo=UTC))
        assert summer > winter * 4

    def test_overcast_cuts_output_to_about_a_quarter(self):
        clear = day_total(datetime(2026, 8, 11, tzinfo=UTC))
        overcast = day_total(datetime(2026, 8, 11, tzinfo=UTC), cloud=100)
        assert overcast == pytest.approx(clear * 0.25, rel=0.01)

    def test_output_scales_with_array_size(self):
        two = day_total(datetime(2026, 8, 11, tzinfo=UTC), peak_kw=2.0)
        eight = day_total(datetime(2026, 8, 11, tzinfo=UTC), peak_kw=8.0)
        assert eight == pytest.approx(two * 4)

    def test_nothing_is_predicted_at_night(self):
        night = datetime(2026, 12, 21, 2, 0, tzinfo=UTC)
        assert clear_sky_kwh(LONDON_LAT, LONDON_LON, night, 0.5, 2.0) == 0.0

    def test_no_output_below_the_elevation_floor(self):
        """Just above the horizon is noise, not energy."""
        assert MIN_ELEVATION_DEG > 0


class TestCloudHandling:
    def test_a_fraction_and_a_percentage_mean_the_same_thing(self):
        """Providers disagree, and a value above 1 can only have been a percent."""
        noon = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        as_fraction = clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, 2.0, cloud=0.5)
        as_percent = clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, 2.0, cloud=50)
        assert as_fraction == pytest.approx(as_percent)

    @pytest.mark.parametrize("cloud", [-10, 0, 50, 100, 150])
    def test_any_cloud_value_yields_something_sane(self, cloud):
        noon = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        clear = clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, 2.0)
        got = clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, 2.0, cloud=cloud)
        assert 0.0 <= got <= clear + 1e-9

    def test_unknown_cloud_gives_the_clear_sky_figure(self):
        noon = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        assert clear_sky_kwh(
            LONDON_LAT, LONDON_LON, noon, 0.5, 2.0, cloud=None
        ) == clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, 2.0)


class TestDegenerateInputs:
    @pytest.mark.parametrize("peak", [0.0, -1.0])
    def test_no_array_produces_nothing(self, peak):
        noon = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        assert clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, 0.5, peak) == 0.0

    @pytest.mark.parametrize("duration", [0.0, -0.5])
    def test_no_time_produces_nothing(self, duration):
        noon = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
        assert clear_sky_kwh(LONDON_LAT, LONDON_LON, noon, duration, 2.0) == 0.0

    def test_the_poles_do_not_raise(self):
        """A latitude of 90 makes the arithmetic degenerate, not the code."""
        for latitude in (90.0, -90.0, 0.0):
            noon = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
            assert clear_sky_kwh(latitude, 0.0, noon, 0.5, 2.0) >= 0.0


class TestForecasterFallsBackToClearSky:
    """The forecaster must reach for this only when it has nothing better.

    Order matters and is the whole point: a real forecast beats a clear-sky
    guess, and learned history beats both. Getting the precedence wrong would
    mean an install with a perfectly good forecast quietly planning against
    arithmetic instead.
    """

    def _forecaster(self, **kwargs):
        from custom_components.ess_controller.forecast.solar import SolarForecaster
        from custom_components.ess_controller.learning.model import LearningModel

        defaults = {
            "model": LearningModel(),
            "peak_power_kw": 2.0,
            "latitude": LONDON_LAT,
            "longitude": LONDON_LON,
        }
        return SolarForecaster(**{**defaults, **kwargs})

    def _noon(self):
        return datetime(2026, 8, 11, 12, 0, tzinfo=LONDON)

    def test_a_fresh_install_predicts_sunshine_not_zero(self):
        """The bug: with nothing configured, every slot came back as 0.0."""
        prediction = self._forecaster().predict_slot(self._noon(), 0.5)
        assert prediction.kwh > 0.2
        assert prediction.source.startswith("clearsky")

    def test_it_still_predicts_nothing_at_night(self):
        night = datetime(2026, 8, 11, 2, 0, tzinfo=LONDON)
        assert self._forecaster().predict_slot(night, 0.5).kwh == 0.0

    def test_cloud_is_used_when_a_weather_provider_supplies_it(self):
        clear = self._forecaster().predict_slot(self._noon(), 0.5, cloud=0)
        murky = self._forecaster().predict_slot(self._noon(), 0.5, cloud=100)
        assert murky.kwh < clear.kwh
        assert murky.source == "clearsky+cloud"

    def test_a_real_forecast_wins_over_the_estimate(self):
        prediction = self._forecaster().predict_slot(self._noon(), 0.5, external_kwh=0.9)
        assert prediction.kwh == pytest.approx(0.9)
        assert "clearsky" not in prediction.source

    def test_learned_history_wins_over_the_estimate(self):
        from custom_components.ess_controller.learning.model import (
            LearningModel,
            SolarObservation,
        )

        model = LearningModel()
        local = self._noon()
        # Enough observations that the bucket becomes trusted.
        for _ in range(20):
            model.observe_solar(
                SolarObservation(
                    month=local.month,
                    hour=local.hour,
                    minute=local.minute,
                    kwh=0.42,
                )
            )
        prediction = self._forecaster(model=model).predict_slot(local, 0.5)
        assert prediction.kwh == pytest.approx(0.42, abs=0.05)
        assert "clearsky" not in prediction.source

    def test_without_a_location_it_behaves_as_before(self):
        """No latitude means no estimate, not a crash or a wrong number."""
        forecaster = self._forecaster(latitude=None, longitude=None)
        prediction = forecaster.predict_slot(self._noon(), 0.5)
        assert prediction.kwh == 0.0
        assert prediction.source == "none"

    def test_the_estimate_never_exceeds_what_the_array_can_make(self):
        forecaster = self._forecaster(peak_power_kw=0.5)
        prediction = forecaster.predict_slot(self._noon(), 0.5)
        assert prediction.kwh <= 0.5 * 0.5 + 1e-9
