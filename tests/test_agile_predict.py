"""Tests for the AgilePredict price forecast.

The payload shape here is taken from AgilePredict's own API tests -- a list of
forecasts, each with a ``prices`` array of ``date_time``/``agile_pred`` entries --
rather than invented. That distinction has already cost this project once: a
fixture written from the consuming template rather than the producing service
agreed with a bug for weeks.

Everything a third-party service can do badly is exercised, because the one thing
this must never do is stop a plan. Unreachable, 404, garbage JSON, an empty list,
naive timestamps, prices in pounds: each degrades to a shorter forecast or none,
and persistence covers the rest.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.ess_controller.models import PriceSlot
from custom_components.ess_controller.tariff import agile_predict as ap

NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)


def entry(offset_slots: int, price: float, **extra) -> dict:
    start = NOW + timedelta(minutes=30 * offset_slots)
    return {
        "date_time": start.isoformat(),
        "agile_pred": price,
        "region": "C",
        **extra,
    }


def payload(prices: list[dict], created: str = "2026-08-11T13:00:00Z") -> list[dict]:
    return [{"name": f"forecast-{created}", "created_at": created, "prices": prices}]


class TestUrl:
    def test_region_letter_becomes_the_path(self):
        assert ap.forecast_url("C") == "https://agilepredict.com/api/C"

    def test_region_is_normalised(self):
        """Octopus tariff codes carry the letter uppercase; users may not."""
        assert ap.forecast_url(" c ") == "https://agilepredict.com/api/C"


class TestParse:
    def test_reads_the_documented_shape(self):
        slots = ap.parse_forecast(payload([entry(0, 10.59), entry(1, 12.0)]))
        assert [round(s.price, 2) for s in slots] == [10.59, 12.0]
        assert [s.start for s in slots] == [NOW, NOW + timedelta(minutes=30)]

    def test_every_slot_is_half_an_hour(self):
        for slot in ap.parse_forecast(payload([entry(0, 5.0), entry(1, 6.0)])):
            assert slot.end - slot.start == timedelta(minutes=30)

    def test_every_slot_is_flagged_as_a_forecast(self):
        """This is what keeps a guess out of the persistence samples."""
        for slot in ap.parse_forecast(payload([entry(0, 5.0)])):
            assert slot.is_forecast is True

    def test_accepts_a_bare_object_as_well_as_a_list(self):
        one = {"created_at": "2026-08-11T13:00:00Z", "prices": [entry(0, 9.0)]}
        assert len(ap.parse_forecast(one)) == 1

    def test_takes_the_newest_of_several_forecasts(self):
        older = {
            "created_at": "2026-08-10T13:00:00Z",
            "prices": [entry(0, 99.0)],
        }
        newer = {
            "created_at": "2026-08-11T13:00:00Z",
            "prices": [entry(0, 11.0)],
        }
        # Given in the wrong order on purpose: newest-first is documented, and
        # relying on it would silently use a stale prediction if it ever changed.
        slots = ap.parse_forecast([older, newer])
        assert [s.price for s in slots] == [11.0]

    def test_falls_back_to_the_first_when_dates_are_unusable(self):
        forecasts = [
            {"created_at": None, "prices": [entry(0, 7.0)]},
            {"created_at": None, "prices": [entry(0, 8.0)]},
        ]
        assert [s.price for s in ap.parse_forecast(forecasts)] == [7.0]

    def test_a_forecast_with_no_prices_is_skipped(self):
        forecasts = [
            {"created_at": "2026-08-11T14:00:00Z", "prices": []},
            {"created_at": "2026-08-11T13:00:00Z", "prices": [entry(0, 6.0)]},
        ]
        assert [s.price for s in ap.parse_forecast(forecasts)] == [6.0]

    def test_slots_come_back_in_order(self):
        slots = ap.parse_forecast(payload([entry(3, 1.0), entry(0, 2.0), entry(1, 3.0)]))
        assert [s.start for s in slots] == sorted(s.start for s in slots)

    def test_a_z_suffix_is_understood(self):
        one = payload([{"date_time": "2026-08-12T00:00:00Z", "agile_pred": 4.0}])
        slots = ap.parse_forecast(one)
        assert slots[0].start == datetime(2026, 8, 12, tzinfo=UTC)

    def test_a_cheap_forecast_is_not_mistaken_for_pounds(self):
        """The bug this guards: a windy night is all under 5p, and the magnitude
        sniff the supplier providers use would read that as pounds and multiply
        the whole forecast by a hundred. AgilePredict documents pence, so the unit
        is taken as given."""
        slots = ap.parse_forecast(payload([entry(0, 1.2), entry(1, 0.4)]))
        assert [round(s.price, 2) for s in slots] == [1.2, 0.4]

    def test_the_user_tariff_scale_is_not_applied(self):
        """Their supplier feed's units say nothing about this API's."""
        from custom_components.ess_controller.const import PRICE_SCALE_AUTO

        cheap = payload([entry(0, 1.2), entry(1, 0.4)])
        assert ap.parse_forecast(cheap) == ap.parse_forecast(
            cheap, scale_mode=ap.PRICE_SCALE_MINOR
        )
        # Only an explicit override rescales, and nothing passes one today.
        rescaled = ap.parse_forecast(cheap, scale_mode=PRICE_SCALE_AUTO)
        assert [round(s.price, 2) for s in rescaled] == [120.0, 40.0]


class TestParseDegrades:
    @pytest.mark.parametrize(
        "junk",
        [None, [], {}, "", 0, [1, 2, 3], {"prices": None}, {"prices": "nope"}],
    )
    def test_junk_yields_no_slots_rather_than_raising(self, junk):
        assert ap.parse_forecast(junk) == []

    def test_a_naive_timestamp_is_dropped_not_assumed(self):
        """Assuming a zone would shift every price by an hour through summer."""
        mixed = payload(
            [
                {"date_time": "2026-08-12T00:00:00", "agile_pred": 4.0},
                entry(0, 5.0),
            ]
        )
        slots = ap.parse_forecast(mixed)
        assert [s.price for s in slots] == [5.0]

    @pytest.mark.parametrize("bad", [None, "12.0", {}, [], True])
    def test_a_non_numeric_price_is_skipped(self, bad):
        mixed = payload(
            [{"date_time": NOW.isoformat(), "agile_pred": bad}, entry(1, 9.0)]
        )
        slots = ap.parse_forecast(mixed)
        assert [s.price for s in slots] == [9.0]

    def test_an_unparseable_timestamp_is_skipped(self):
        mixed = payload([{"date_time": "next tuesday", "agile_pred": 4.0}, entry(1, 9.0)])
        assert [s.price for s in ap.parse_forecast(mixed)] == [9.0]

    def test_non_dict_entries_are_ignored(self):
        mixed = payload([entry(0, 3.0)])
        mixed[0]["prices"].append("junk")
        assert len(ap.parse_forecast(mixed)) == 1


class TestTrimming:
    def test_until_drops_slots_past_the_horizon(self):
        prices = [entry(n, float(n)) for n in range(10)]
        slots = ap.parse_forecast(payload(prices), until=NOW + timedelta(hours=2))
        assert len(slots) == 4
        assert slots[-1].end <= NOW + timedelta(hours=2)

    def test_after_drops_slots_already_finished(self):
        prices = [entry(n, float(n)) for n in range(10)]
        slots = ap.parse_forecast(payload(prices), after=NOW + timedelta(hours=2))
        assert slots[0].start >= NOW + timedelta(hours=2)

    def test_a_fortnight_can_be_trimmed_to_nothing_without_error(self):
        prices = [entry(n, 1.0) for n in range(10)]
        assert ap.parse_forecast(payload(prices), after=NOW + timedelta(days=30)) == []


class TestDaysNeeded:
    def test_a_short_horizon_asks_for_one_day(self):
        assert ap.days_needed(NOW, NOW + timedelta(hours=6)) == 1

    def test_a_two_day_horizon_asks_for_three(self):
        """Rounded up, because a partial day still needs covering."""
        assert ap.days_needed(NOW, NOW + timedelta(hours=48)) == 3

    def test_never_asks_for_more_than_the_api_offers(self):
        assert ap.days_needed(NOW, NOW + timedelta(days=90)) == ap.MAX_DAYS

    def test_never_asks_for_less_than_a_day(self):
        assert ap.days_needed(NOW, NOW) == 1
        assert ap.days_needed(NOW, NOW - timedelta(hours=5)) == 1


class FakeResponse:
    def __init__(self, status=200, body=None, raises=False):
        self.status = status
        self._body = body
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append((url, params or {}))
        if self._error:
            raise self._error
        return self._response


class TestFetch:
    def test_fetches_and_parses(self):
        session = FakeSession(FakeResponse(body=payload([entry(0, 10.0)])))
        slots = asyncio.run(ap.async_fetch_forecast(session, "C"))
        assert [s.price for s in slots] == [10.0]

    def test_asks_for_the_central_estimate_only(self):
        """High and low bounds would systematically over- or under-charge."""
        session = FakeSession(FakeResponse(body=payload([])))
        asyncio.run(ap.async_fetch_forecast(session, "C"))
        _url, params = session.calls[0]
        assert params["high_low"] == "false"
        assert "export" not in params

    def test_export_is_requested_explicitly(self):
        session = FakeSession(FakeResponse(body=payload([])))
        asyncio.run(ap.async_fetch_forecast(session, "C", export=True))
        assert session.calls[0][1]["export"] == "true"

    def test_days_are_clamped_to_the_api_limit(self):
        session = FakeSession(FakeResponse(body=payload([])))
        asyncio.run(ap.async_fetch_forecast(session, "C", days=99))
        assert session.calls[0][1]["days"] == str(ap.MAX_DAYS)

    def test_a_network_failure_is_reported_as_unreachable(self):
        session = FakeSession(error=OSError("no route to host"))
        with pytest.raises(ap.AgilePredictError) as caught:
            asyncio.run(ap.async_fetch_forecast(session, "C"))
        assert caught.value.kind == ap.KIND_UNREACHABLE

    def test_a_missing_region_is_not_a_network_problem(self):
        """The same distinction the Octopus provider had to learn."""
        session = FakeSession(FakeResponse(status=404))
        with pytest.raises(ap.AgilePredictError) as caught:
            asyncio.run(ap.async_fetch_forecast(session, "Z"))
        assert caught.value.kind == ap.KIND_NOT_FOUND
        assert caught.value.status == 404

    def test_a_server_error_carries_its_status(self):
        session = FakeSession(FakeResponse(status=503))
        with pytest.raises(ap.AgilePredictError) as caught:
            asyncio.run(ap.async_fetch_forecast(session, "C"))
        assert caught.value.kind == ap.KIND_HTTP
        assert caught.value.status == 503

    def test_garbage_json_is_reported_as_a_bad_response(self):
        session = FakeSession(FakeResponse(raises=True))
        with pytest.raises(ap.AgilePredictError) as caught:
            asyncio.run(ap.async_fetch_forecast(session, "C"))
        assert caught.value.kind == ap.KIND_BAD_RESPONSE

    def test_a_valid_but_empty_payload_is_not_an_error(self):
        """Nothing to forecast is a normal answer, not a failure."""
        session = FakeSession(FakeResponse(body=[]))
        assert asyncio.run(ap.async_fetch_forecast(session, "C")) == []


class TestCompareWithActual:
    def _real(self, prices: list[float]) -> list[PriceSlot]:
        return [
            PriceSlot(
                start=NOW + timedelta(minutes=30 * n),
                end=NOW + timedelta(minutes=30 * (n + 1)),
                price=price,
            )
            for n, price in enumerate(prices)
        ]

    def test_scores_the_overlap(self):
        forecast = ap.parse_forecast(payload([entry(0, 11.0), entry(1, 9.0)]))
        result = ap.compare_with_actual(forecast, self._real([10.0, 10.0]))
        assert result == {"slots": 2, "mae": 1.0, "bias": 0.0}

    def test_bias_is_signed_so_a_units_mismatch_stands_out(self):
        """A pence/pounds or VAT error shows as a huge bias, not as noise."""
        forecast = ap.parse_forecast(payload([entry(0, 0.1), entry(1, 0.1)]))
        # Parsing scales sub-£5 values to pence, so force the mismatch instead.
        forecast = [
            PriceSlot(start=s.start, end=s.end, price=0.1, is_forecast=True)
            for s in forecast
        ]
        result = ap.compare_with_actual(forecast, self._real([10.0, 10.0]))
        assert result["bias"] == pytest.approx(-9.9)

    def test_no_overlap_scores_nothing(self):
        forecast = ap.parse_forecast(payload([entry(50, 5.0)]))
        assert ap.compare_with_actual(forecast, self._real([10.0])) is None

    def test_extrapolated_slots_are_not_treated_as_actuals(self):
        """Scoring a forecast against persistence would measure nothing useful."""
        guessed = [
            PriceSlot(
                start=NOW, end=NOW + timedelta(minutes=30), price=10.0, is_forecast=True
            )
        ]
        forecast = ap.parse_forecast(payload([entry(0, 11.0)]))
        assert ap.compare_with_actual(forecast, guessed) is None

    def test_an_empty_forecast_scores_nothing(self):
        assert ap.compare_with_actual([], self._real([10.0])) is None


class TestLayeringOverAnnouncedPrices:
    """The forecast must fill the tail and never touch a known price.

    ``PriceSeries.merge`` lets later sources win, which is right for a freshly
    published day landing over an extrapolated one and catastrophic for a
    prediction landing over an announced rate. The coordinator trims predicted
    slots to start where the announced run ends; these tests pin that contract at
    the level the merge happens.
    """

    def _announced(self, count: int, price: float = 20.0) -> list[PriceSlot]:
        return [
            PriceSlot(
                start=NOW + timedelta(minutes=30 * n),
                end=NOW + timedelta(minutes=30 * (n + 1)),
                price=price,
            )
            for n in range(count)
        ]

    def _series(self, slots: list[PriceSlot]):
        from custom_components.ess_controller.tariff.base import PriceSeries

        return PriceSeries(slots)

    def test_trimmed_forecast_leaves_announced_prices_alone(self):
        series = self._series(self._announced(4))
        known_end = series.known_until(NOW)
        forecast = ap.parse_forecast(payload([entry(n, 1.0) for n in range(8)]))
        series.merge([s for s in forecast if s.start >= known_end])
        assert [s.price for s in series][:4] == [20.0] * 4
        assert [s.price for s in series][4:] == [1.0] * 4

    def test_an_untrimmed_merge_would_have_clobbered_them(self):
        """Stated as a test so the trim is never quietly dropped as redundant."""
        series = self._series(self._announced(4))
        series.merge(ap.parse_forecast(payload([entry(n, 1.0) for n in range(8)])))
        assert [s.price for s in series][:4] == [1.0] * 4

    def test_the_join_has_no_gap_and_no_overlap(self):
        series = self._series(self._announced(4))
        known_end = series.known_until(NOW)
        forecast = ap.parse_forecast(payload([entry(n, 1.0) for n in range(8)]))
        series.merge([s for s in forecast if s.start >= known_end])
        slots = list(series)
        for earlier, later in pairwise(slots):
            assert earlier.end == later.start

    def test_announced_slots_stay_unflagged_across_the_join(self):
        """``is_forecast`` is what the dashboard marks and the learner skips."""
        series = self._series(self._announced(4))
        known_end = series.known_until(NOW)
        forecast = ap.parse_forecast(payload([entry(n, 1.0) for n in range(8)]))
        series.merge([s for s in forecast if s.start >= known_end])
        assert [s.is_forecast for s in series] == [False] * 4 + [True] * 4

    def test_persistence_still_covers_what_the_forecast_does_not_reach(self):
        """A short forecast must not leave the horizon unpriced."""
        series = self._series(self._announced(4))
        known_end = series.known_until(NOW)
        forecast = ap.parse_forecast(payload([entry(4, 1.0), entry(5, 1.0)]))
        series.merge([s for s in forecast if s.start >= known_end])
        until = NOW + timedelta(hours=8)
        added = series.extend_by_persistence(until=until, now=NOW)
        assert added > 0
        assert series.end >= until

    def test_persistence_does_not_learn_from_predicted_slots(self):
        """Extrapolating from a guess would compound one model's error."""
        series = self._series(self._announced(4, price=20.0))
        known_end = series.known_until(NOW)
        series.merge(
            [
                s
                for s in ap.parse_forecast(payload([entry(n, 1.0) for n in range(4, 8)]))
                if s.start >= known_end
            ]
        )
        series.extend_by_persistence(until=NOW + timedelta(hours=8), now=NOW)
        # Nothing beyond the forecast may have inherited the predicted 1.0: the
        # only real samples are the announced 20.0 slots.
        tail = [s.price for s in series if s.start >= NOW + timedelta(hours=4)]
        assert 1.0 not in tail
