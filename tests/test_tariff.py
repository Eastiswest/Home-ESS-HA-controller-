"""Tests for tariff parsing and the price series helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.ess_controller.models import PriceSlot
from custom_components.ess_controller.tariff.base import (
    PriceSeries,
    ZeroTariffProvider,
    detect_scale,
)
from custom_components.ess_controller.tariff.entity import (
    extract_rate_entries,
    parse_entity_rates,
)
from custom_components.ess_controller.tariff.fixed import (
    FixedTariffProvider,
    TimeOfUseTariffProvider,
    parse_tou_schedule,
)
from custom_components.ess_controller.tariff.octopus import (
    build_tariff_code,
    parse_account_tariffs,
    parse_unit_rates,
    product_code_from_tariff,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def slot(hour: int, minute: int, price: float, day: int = 15) -> PriceSlot:
    start = datetime(2026, 1, day, hour, minute, tzinfo=UTC)
    return PriceSlot(start=start, end=start + timedelta(minutes=30), price=price)


class TestScaleDetection:
    def test_pounds_are_scaled_to_pence(self):
        assert detect_scale([0.2345, 0.1876]) == 100.0

    def test_pence_are_left_alone(self):
        assert detect_scale([23.45, 18.76]) == 1.0

    def test_negative_agile_prices_still_detected(self):
        # Outgoing Agile can go negative; magnitude is what matters.
        assert detect_scale([-0.05, 0.12]) == 100.0
        assert detect_scale([-5.0, 12.0]) == 1.0

    def test_all_zero_is_not_rescaled(self):
        assert detect_scale([0.0, 0.0]) == 1.0

    def test_explicit_override_wins(self):
        assert detect_scale([23.45], "major") == 100.0
        assert detect_scale([0.23], "minor") == 1.0


class TestPriceSeries:
    def test_sorted_and_deduplicated(self):
        series = PriceSeries([slot(2, 0, 10.0), slot(1, 0, 20.0), slot(2, 0, 15.0)])
        assert len(series) == 2
        assert [s.price for s in series] == [20.0, 15.0]

    def test_price_at(self):
        series = PriceSeries([slot(1, 0, 20.0), slot(1, 30, 30.0)])
        assert series.price_at(datetime(2026, 1, 15, 1, 15, tzinfo=UTC)) == 20.0
        assert series.price_at(datetime(2026, 1, 15, 1, 45, tzinfo=UTC)) == 30.0
        assert series.price_at(datetime(2026, 1, 15, 5, 0, tzinfo=UTC)) is None

    def test_known_until_finds_end_of_contiguous_run(self):
        series = PriceSeries(
            [slot(12, 0, 10.0), slot(12, 30, 11.0), slot(13, 0, 12.0)]
        )
        assert series.known_until(NOW) == datetime(2026, 1, 15, 13, 30, tzinfo=UTC)

    def test_known_until_stops_at_a_gap(self):
        series = PriceSeries([slot(12, 0, 10.0), slot(14, 0, 12.0)])
        assert series.known_until(NOW) == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)

    def test_extend_by_persistence_reuses_same_time_of_day(self):
        # A full day of known prices with a distinctive overnight trough.
        slots = []
        for index in range(48):
            hour, minute = divmod(index * 30, 60)
            price = 8.0 if hour < 5 else 30.0
            slots.append(slot(hour, minute, price, day=15))
        series = PriceSeries(slots)
        start = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        added = series.extend_by_persistence(
            until=datetime(2026, 1, 16, 12, 0, tzinfo=UTC), now=start
        )
        assert added > 0
        # 02:00 tomorrow should inherit the cheap overnight price.
        cheap = series.price_at(datetime(2026, 1, 16, 2, 0, tzinfo=UTC))
        pricey = series.price_at(datetime(2026, 1, 16, 9, 0, tzinfo=UTC))
        assert cheap == 8.0
        assert pricey == 30.0

    def test_extended_slots_are_flagged_as_forecast(self):
        series = PriceSeries([slot(h // 2, (h % 2) * 30, 20.0) for h in range(4)])
        start = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        series.extend_by_persistence(
            until=datetime(2026, 1, 15, 6, 0, tzinfo=UTC), now=start
        )
        forecast = [s for s in series if s.is_forecast]
        assert forecast
        assert all(s.start >= datetime(2026, 1, 15, 2, 0, tzinfo=UTC) for s in forecast)

    def test_extend_is_noop_when_already_covered(self):
        series = PriceSeries([slot(12, 0, 20.0), slot(12, 30, 20.0)])
        added = series.extend_by_persistence(
            until=datetime(2026, 1, 15, 12, 30, tzinfo=UTC), now=NOW
        )
        assert added == 0


class TestOctopusParsing:
    def test_tariff_code_construction(self):
        assert build_tariff_code("AGILE-24-10-01", "C") == "E-1R-AGILE-24-10-01-C"
        assert (
            build_tariff_code("OUTGOING-AGILE-24-10-01", "H")
            == "E-1R-OUTGOING-AGILE-24-10-01-H"
        )

    def test_full_tariff_code_passed_through(self):
        assert build_tariff_code("E-1R-AGILE-24-10-01-C", "C") == "E-1R-AGILE-24-10-01-C"

    def test_bad_region_rejected(self):
        with pytest.raises(ValueError):
            build_tariff_code("AGILE-24-10-01", "Z")

    def test_product_recovered_from_tariff_code(self):
        assert product_code_from_tariff("E-1R-AGILE-24-10-01-C") == "AGILE-24-10-01"

    def test_parses_newest_first_results_into_order(self):
        payload = {
            "results": [
                {
                    "value_inc_vat": 30.0,
                    "valid_from": "2026-01-15T01:00:00Z",
                    "valid_to": "2026-01-15T01:30:00Z",
                },
                {
                    "value_inc_vat": 20.0,
                    "valid_from": "2026-01-15T00:30:00Z",
                    "valid_to": "2026-01-15T01:00:00Z",
                },
            ]
        }
        slots = parse_unit_rates(payload)
        assert [s.price for s in slots] == [20.0, 30.0]

    def test_octopus_pence_not_rescaled(self):
        payload = {
            "results": [
                {
                    "value_inc_vat": 23.45,
                    "valid_from": "2026-01-15T00:30:00Z",
                    "valid_to": "2026-01-15T01:00:00Z",
                }
            ]
        }
        assert parse_unit_rates(payload)[0].price == pytest.approx(23.45)

    def test_open_ended_band_expanded_to_half_hours(self):
        payload = {
            "results": [
                {
                    "value_inc_vat": 24.0,
                    "valid_from": "2026-01-15T00:00:00Z",
                    "valid_to": None,
                }
            ]
        }
        slots = parse_unit_rates(
            payload, fallback_end=datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
        )
        assert len(slots) == 6
        assert all(s.price == 24.0 for s in slots)
        assert all((s.end - s.start) == timedelta(minutes=30) for s in slots)

    def test_negative_agile_price_preserved(self):
        payload = {
            "results": [
                {
                    "value_inc_vat": -3.5,
                    "valid_from": "2026-01-15T13:00:00Z",
                    "valid_to": "2026-01-15T13:30:00Z",
                },
                {
                    "value_inc_vat": 42.0,
                    "valid_from": "2026-01-15T17:00:00Z",
                    "valid_to": "2026-01-15T17:30:00Z",
                },
            ]
        }
        slots = parse_unit_rates(payload)
        assert slots[0].price == pytest.approx(-3.5)

    def test_empty_payload_is_safe(self):
        assert parse_unit_rates({}) == []
        assert parse_unit_rates({"results": []}) == []

    def test_malformed_entries_skipped(self):
        payload = {
            "results": [
                {"value_inc_vat": None, "valid_from": "2026-01-15T00:00:00Z"},
                {"valid_from": "bad", "value_inc_vat": 10.0},
                "not a dict",
                {
                    "value_inc_vat": 15.0,
                    "valid_from": "2026-01-15T02:00:00Z",
                    "valid_to": "2026-01-15T02:30:00Z",
                },
            ]
        }
        slots = parse_unit_rates(payload)
        assert len(slots) == 1
        assert slots[0].price == 15.0


class TestOctopusAccount:
    def test_import_and_export_split_by_flag(self):
        payload = {
            "properties": [
                {
                    "electricity_meter_points": [
                        {
                            "mpan": "1111",
                            "is_export": False,
                            "agreements": [
                                {
                                    "tariff_code": "E-1R-AGILE-24-10-01-C",
                                    "valid_to": None,
                                }
                            ],
                        },
                        {
                            "mpan": "2222",
                            "is_export": True,
                            "agreements": [
                                {
                                    "tariff_code": "E-1R-OUTGOING-AGILE-24-10-01-C",
                                    "valid_to": None,
                                }
                            ],
                        },
                    ]
                }
            ]
        }
        found = parse_account_tariffs(payload)
        assert found["import"] == "E-1R-AGILE-24-10-01-C"
        assert found["export"] == "E-1R-OUTGOING-AGILE-24-10-01-C"
        assert len(found["mpans"]) == 2

    def test_open_ended_agreement_preferred_over_expired(self):
        payload = {
            "properties": [
                {
                    "electricity_meter_points": [
                        {
                            "mpan": "1111",
                            "agreements": [
                                {
                                    "tariff_code": "E-1R-OLD-TARIFF-C",
                                    "valid_to": "2025-01-01T00:00:00Z",
                                },
                                {
                                    "tariff_code": "E-1R-AGILE-24-10-01-C",
                                    "valid_to": None,
                                },
                            ],
                        }
                    ]
                }
            ]
        }
        assert parse_account_tariffs(payload)["import"] == "E-1R-AGILE-24-10-01-C"

    def test_empty_account_yields_nothing(self):
        found = parse_account_tariffs({"properties": []})
        assert found["import"] is None
        assert found["export"] is None


class TestEntityParsing:
    def test_bottlecapdave_rates_attribute(self):
        # BottlecapDave publishes value_inc_vat in pounds.
        attributes = {
            "rates": [
                {
                    "start": "2026-01-15T00:00:00+00:00",
                    "end": "2026-01-15T00:30:00+00:00",
                    "value_inc_vat": 0.0812,
                },
                {
                    "start": "2026-01-15T00:30:00+00:00",
                    "end": "2026-01-15T01:00:00+00:00",
                    "value_inc_vat": 0.2456,
                },
            ]
        }
        slots = parse_entity_rates(attributes)
        assert len(slots) == 2
        assert slots[0].price == pytest.approx(8.12)
        assert slots[1].price == pytest.approx(24.56)

    def test_nordpool_raw_today_and_tomorrow_combined(self):
        attributes = {
            "raw_today": [
                {
                    "start": "2026-01-15T00:00:00+00:00",
                    "end": "2026-01-15T01:00:00+00:00",
                    "value": 12.0,
                }
            ],
            "raw_tomorrow": [
                {
                    "start": "2026-01-16T00:00:00+00:00",
                    "end": "2026-01-16T01:00:00+00:00",
                    "value": 15.0,
                }
            ],
        }
        slots = parse_entity_rates(attributes)
        # Hourly bands are split into half-hour slots.
        assert len(slots) == 4
        assert {s.price for s in slots} == {12.0, 15.0}

    def test_all_rates_attribute(self):
        attributes = {
            "all_rates": [
                {
                    "valid_from": "2026-01-15T00:00:00Z",
                    "valid_to": "2026-01-15T00:30:00Z",
                    "value_inc_vat": 21.0,
                }
            ]
        }
        assert parse_entity_rates(attributes)[0].price == pytest.approx(21.0)

    def test_missing_end_inferred_from_next_entry(self):
        attributes = {
            "prices": [
                {"start": "2026-01-15T00:00:00Z", "price": 10.0},
                {"start": "2026-01-15T00:30:00Z", "price": 20.0},
            ]
        }
        slots = parse_entity_rates(attributes)
        assert slots[0].end == datetime(2026, 1, 15, 0, 30, tzinfo=UTC)
        # The final entry falls back to a half-hour window.
        assert slots[1].end == datetime(2026, 1, 15, 1, 0, tzinfo=UTC)

    def test_no_recognised_attribute_yields_nothing(self):
        assert parse_entity_rates({"friendly_name": "x", "unit": "p/kWh"}) == []
        assert extract_rate_entries({"rates": []}) == []

    def test_entries_out_of_order_are_sorted(self):
        attributes = {
            "rates": [
                {"start": "2026-01-15T01:00:00Z", "value": 30.0},
                {"start": "2026-01-15T00:00:00Z", "value": 10.0},
                {"start": "2026-01-15T00:30:00Z", "value": 20.0},
            ]
        }
        slots = parse_entity_rates(attributes)
        assert [s.price for s in slots] == [10.0, 20.0, 30.0]


class TestTimeOfUse:
    def test_compact_string_schedule(self):
        windows = parse_tou_schedule("00:30-04:30=9.5,04:30-00:30=28.0")
        assert windows == [
            (time(0, 30), time(4, 30), 9.5),
            (time(4, 30), time(0, 30), 28.0),
        ]

    def test_list_schedule(self):
        windows = parse_tou_schedule(
            [{"start": "23:30", "end": "05:30", "rate": 8.0}]
        )
        assert windows == [(time(23, 30), time(5, 30), 8.0)]

    def test_bad_entries_skipped(self):
        windows = parse_tou_schedule("garbage,00:00-06:00=7.5,99:99-1=3")
        assert windows == [(time(0, 0), time(6, 0), 7.5)]

    def test_economy7_window_wraps_midnight(self):
        provider = TimeOfUseTariffProvider("23:30-06:30=8.0", default_rate=27.0)
        assert provider.rate_at(datetime(2026, 1, 15, 2, 0)) == 8.0
        assert provider.rate_at(datetime(2026, 1, 15, 23, 45)) == 8.0
        assert provider.rate_at(datetime(2026, 1, 15, 12, 0)) == 27.0

    def test_windows_evaluated_in_local_time(self):
        # A UTC timestamp during British Summer Time is 01:30 local, which is
        # inside the cheap window even though 00:30 UTC looks like it is too.
        from zoneinfo import ZoneInfo

        london = ZoneInfo("Europe/London")
        provider = TimeOfUseTariffProvider("01:00-06:00=8.0", 27.0, tz=london)
        # 00:30 UTC in July == 01:30 BST -> cheap.
        assert provider.rate_at(datetime(2026, 7, 15, 0, 30, tzinfo=UTC)) == 8.0
        # 23:30 UTC in July == 00:30 BST -> not yet cheap.
        assert provider.rate_at(datetime(2026, 7, 15, 23, 30, tzinfo=UTC)) == 27.0


def test_fixed_provider_covers_horizon():
    provider = FixedTariffProvider(24.5)
    series = asyncio.run(provider.async_fetch(NOW, NOW + timedelta(hours=3)))
    assert len(series) == 6
    assert all(s.price == 24.5 for s in series)


def test_zero_provider_returns_explicit_zeros():
    provider = ZeroTariffProvider()
    series = asyncio.run(provider.async_fetch(NOW, NOW + timedelta(hours=2)))
    assert len(series) == 4
    assert all(s.price == 0.0 for s in series)


def test_tou_provider_covers_horizon():
    provider = TimeOfUseTariffProvider("00:00-07:00=9.0", 30.0)
    start = datetime(2026, 1, 15, 6, 0)
    series = asyncio.run(provider.async_fetch(start, start + timedelta(hours=2)))
    prices = [s.price for s in series]
    assert prices[:2] == [9.0, 9.0]
    assert prices[2:] == [30.0, 30.0]
