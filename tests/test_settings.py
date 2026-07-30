"""Tests for runtime settings sanitisation and horizon slot arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.ess_controller.sampling import slot_boundaries
from custom_components.ess_controller.settings import RuntimeSettings


def dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 7, 15, hour, minute, second, tzinfo=UTC)


class TestDefaults:
    def test_ships_in_advisory_mode(self):
        """The headline safety property: a fresh install never writes."""
        settings = RuntimeSettings()
        assert settings.dry_run is True
        assert settings.controlling is False

    def test_enabled_but_not_controlling_by_default(self):
        settings = RuntimeSettings()
        assert settings.enabled is True
        assert settings.controlling is False

    def test_controlling_requires_both_flags(self):
        settings = RuntimeSettings(enabled=True, dry_run=False)
        assert settings.controlling is True
        assert RuntimeSettings(enabled=False, dry_run=False).controlling is False

    def test_disabled_optimiser_may_still_write(self):
        """Switching the optimiser off must be able to hand the inverter back.

        If this were gated on `controlling`, disabling the optimiser could never
        release the inverter, leaving it stuck in whatever mode was last set --
        possibly a forced charge.
        """
        settings = RuntimeSettings(enabled=False, dry_run=False)
        assert settings.controlling is False
        assert settings.may_write is True

    def test_dry_run_forbids_writing_outright(self):
        assert RuntimeSettings(enabled=True, dry_run=True).may_write is False
        assert RuntimeSettings(enabled=False, dry_run=True).may_write is False


class TestSanitisation:
    def test_clamps_soc_into_range(self):
        settings = RuntimeSettings(min_soc=-20, max_soc=150).sanitised()
        assert settings.min_soc == 0.0
        assert settings.max_soc == 100.0

    def test_inverted_window_is_widened_not_accepted(self):
        # An inverted window would make usable capacity negative.
        settings = RuntimeSettings(min_soc=80, max_soc=20).sanitised()
        assert settings.max_soc > settings.min_soc

    def test_reserve_cannot_exceed_planning_floor(self):
        """The hardware reserve must sit below the floor the optimiser plans to,
        or the optimiser would plan into the emergency reserve."""
        settings = RuntimeSettings(min_soc=15, reserve_soc=40).sanitised()
        assert settings.reserve_soc <= settings.min_soc

    def test_negative_power_clamped(self):
        settings = RuntimeSettings(max_charge_kw=-5, max_discharge_kw=-1).sanitised()
        assert settings.max_charge_kw == 0.0
        assert settings.max_discharge_kw == 0.0

    def test_unknown_strategy_falls_back_to_auto(self):
        assert RuntimeSettings(strategy="nonsense").sanitised().strategy == "auto"

    def test_non_numeric_value_does_not_raise(self):
        settings = RuntimeSettings(min_soc="abc").sanitised()  # type: ignore[arg-type]
        assert isinstance(settings.min_soc, float)


class TestPersistence:
    def test_roundtrip(self):
        settings = RuntimeSettings(min_soc=22, cycle_cost=4.5, dry_run=False)
        settings.sanitised()
        restored = RuntimeSettings.from_dict(settings.as_dict())
        assert restored.min_soc == 22
        assert restored.cycle_cost == 4.5
        assert restored.dry_run is False

    def test_unknown_keys_ignored(self):
        restored = RuntimeSettings.from_dict({"min_soc": 30, "from_the_future": 1})
        assert restored.min_soc == 30

    def test_empty_payload_gives_defaults(self):
        assert RuntimeSettings.from_dict(None).dry_run is True


class TestSeeding:
    def test_seeds_from_config_options_once(self):
        settings = RuntimeSettings()
        settings.seed_from_options(
            {
                "battery_min_soc": 20.0,
                "battery_max_soc": 90.0,
                "cycle_cost_per_kwh": 3.0,
                "dry_run": False,
            }
        )
        assert settings.min_soc == 20.0
        assert settings.max_soc == 90.0
        assert settings.cycle_cost == 3.0
        assert settings.dry_run is False
        assert settings.seeded is True

    def test_second_seed_does_not_overwrite_user_tuning(self):
        """Editing options later must not silently undo a dashboard change."""
        settings = RuntimeSettings()
        settings.seed_from_options({"battery_min_soc": 20.0})
        settings.min_soc = 35.0  # user adjusted from their dashboard
        settings.seed_from_options({"battery_min_soc": 20.0})
        assert settings.min_soc == 35.0

    def test_seeding_sanitises(self):
        settings = RuntimeSettings()
        settings.seed_from_options({"battery_min_soc": 10.0, "battery_reserve_soc": 50.0})
        assert settings.reserve_soc <= settings.min_soc


class TestSlotBoundaries:
    def test_starts_now_with_a_partial_first_slot(self):
        boundaries = slot_boundaries(dt(12, 7), dt(14, 0))
        assert boundaries[0] == (dt(12, 7), dt(12, 30))
        assert boundaries[1] == (dt(12, 30), dt(13, 0))

    def test_aligned_start_gives_full_slots(self):
        boundaries = slot_boundaries(dt(12, 0), dt(13, 0))
        assert boundaries == [(dt(12, 0), dt(12, 30)), (dt(12, 30), dt(13, 0))]

    def test_tiny_first_sliver_is_skipped(self):
        # 30 seconds of a slot is not worth planning as its own decision.
        boundaries = slot_boundaries(dt(12, 29, 30), dt(13, 30))
        assert boundaries[0] == (dt(12, 30), dt(13, 0))

    def test_covers_the_whole_horizon(self):
        boundaries = slot_boundaries(dt(12, 0), dt(12, 0) + timedelta(hours=36))
        assert len(boundaries) == 72
        assert boundaries[-1][1] == dt(12, 0) + timedelta(hours=36)

    def test_no_slots_when_horizon_has_passed(self):
        assert slot_boundaries(dt(12, 0), dt(11, 0)) == []

    def test_boundaries_are_contiguous(self):
        boundaries = slot_boundaries(dt(9, 13), dt(20, 0))
        for (_, end), (next_start, _) in pairwise(boundaries):
            assert end == next_start

    def test_first_slot_duration_matches_remaining_time(self):
        boundaries = slot_boundaries(dt(12, 10), dt(13, 0))
        start, end = boundaries[0]
        assert (end - start) == timedelta(minutes=20)
        assert pytest.approx((end - start).total_seconds() / 3600) == 1 / 3
