"""Tests for runtime settings sanitisation and horizon slot arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.ess_controller.sampling import slot_boundaries
from custom_components.ess_controller.settings import RuntimeSettings
from custom_components.ess_controller.wear import (
    MAX_SENSIBLE_WEAR,
    manual_wear,
    negative_price_threshold,
    spread_needed,
    wear_from_cost,
)


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

    def test_appliance_switching_is_off_by_default(self):
        """Scheduling a load and energising it are separate levels of trust."""
        settings = RuntimeSettings(enabled=True, dry_run=False, shifting_enabled=True)
        assert settings.controlling is True
        assert settings.appliance_control is False
        assert settings.may_switch_appliances is False

    def test_appliance_switching_needs_all_four_gates(self):
        armed = {
            "enabled": True,
            "dry_run": False,
            "shifting_enabled": True,
            "appliance_control": True,
        }
        assert RuntimeSettings(**armed).may_switch_appliances is True
        for gate, disarmed in (
            ("enabled", False),
            ("dry_run", True),
            ("shifting_enabled", False),
            ("appliance_control", False),
        ):
            settings = RuntimeSettings(**{**armed, gate: disarmed})
            assert settings.may_switch_appliances is False, gate


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


class TestOptionsEditsAfterSeeding:
    """The options flow shows the seeded values, accepts edits, and used to
    discard every one of them.

    A real install retyped the wear allowance as 2.0 through Configure; the
    entry stored it, the plan carried on pricing wear off the old 1.5, and
    nothing said so. Only a key that *changed* since the last snapshot is
    applied, so a stale option can never clobber a dashboard tune."""

    def test_a_changed_option_takes_effect(self):
        settings = RuntimeSettings()
        settings.seed_from_options({"cycle_cost_per_kwh": 1.5})
        changed = settings.apply_option_changes(
            {"cycle_cost_per_kwh": 1.5}, {"cycle_cost_per_kwh": 2.0}
        )
        assert settings.cycle_cost == 2.0
        assert changed == ["cycle_cost"]

    def test_a_stale_option_cannot_undo_a_dashboard_tune(self):
        settings = RuntimeSettings()
        settings.seed_from_options({"battery_min_soc": 20.0, "cycle_cost_per_kwh": 1.5})
        settings.min_soc = 35.0  # adjusted from the dashboard since
        # An unrelated Configure save re-submits every field unchanged.
        changed = settings.apply_option_changes(
            {"battery_min_soc": 20.0, "cycle_cost_per_kwh": 1.5},
            {"battery_min_soc": 20.0, "cycle_cost_per_kwh": 1.5},
        )
        assert changed == []
        assert settings.min_soc == 35.0

    def test_applied_changes_are_sanitised(self):
        settings = RuntimeSettings()
        settings.seed_from_options({"battery_min_soc": 20.0})
        settings.apply_option_changes({}, {"battery_reserve_soc": 50.0})
        assert settings.reserve_soc <= settings.min_soc

    def test_tracked_options_keeps_only_seedable_keys(self):
        tracked = RuntimeSettings.tracked_options(
            {"cycle_cost_per_kwh": 2.0, "battery_soc_entity": "sensor.soc"}
        )
        assert tracked == {"cycle_cost_per_kwh": 2.0}


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


class TestWearFromCost:
    """Deriving the wear allowance from what the pack actually cost."""

    def test_reference_pack(self):
        """A 500 pack with a 17.6 kWh usable window over 1500 cycles has
        26,400 kWh of throughput in it, so about 1.9p per kWh cycled."""
        estimate = wear_from_cost(pack_cost=500.0, usable_kwh=17.6, cycles=1500.0)
        assert estimate.lifetime_throughput_kwh == pytest.approx(26400.0)
        assert estimate.cycle_cost == pytest.approx(1.894, abs=0.01)
        assert estimate.warning is None

    def test_cheaper_pack_cycles_more_freely(self):
        dear = wear_from_cost(5000.0, 17.6, 1500.0).cycle_cost
        cheap = wear_from_cost(500.0, 17.6, 1500.0).cycle_cost
        assert cheap == pytest.approx(dear / 10, abs=0.01)

    def test_longer_life_lowers_the_allowance(self):
        short = wear_from_cost(500.0, 17.6, 1000.0).cycle_cost
        long = wear_from_cost(500.0, 17.6, 3000.0).cycle_cost
        assert long == pytest.approx(short / 3, abs=0.01)

    def test_bigger_usable_window_lowers_the_allowance(self):
        """A wider SoC window puts more kWh through the same pack."""
        narrow = wear_from_cost(500.0, 8.8, 1500.0).cycle_cost
        wide = wear_from_cost(500.0, 17.6, 1500.0).cycle_cost
        assert wide == pytest.approx(narrow / 2, abs=0.01)

    def test_residual_value_reduces_the_net_cost(self):
        full = wear_from_cost(500.0, 17.6, 1500.0).cycle_cost
        with_residual = wear_from_cost(
            500.0, 17.6, 1500.0, residual_value=250.0
        ).cycle_cost
        assert with_residual == pytest.approx(full / 2, abs=0.01)

    def test_residual_above_cost_does_not_go_negative(self):
        estimate = wear_from_cost(500.0, 17.6, 1500.0, residual_value=900.0)
        assert estimate.cycle_cost == 0.0
        assert estimate.net_cost == 0.0

    def test_zero_cycles_is_reported_not_divided_by(self):
        estimate = wear_from_cost(500.0, 17.6, 0.0)
        assert estimate.cycle_cost == 0.0
        assert estimate.source == "unavailable"
        assert estimate.warning is not None

    def test_zero_usable_capacity_is_reported(self):
        estimate = wear_from_cost(500.0, 0.0, 1500.0)
        assert estimate.source == "unavailable"
        assert estimate.warning is not None

    def test_implausible_result_clamped_and_warned(self):
        # A tiny pack with a huge price would otherwise give a silly allowance.
        estimate = wear_from_cost(pack_cost=50000.0, usable_kwh=0.5, cycles=10.0)
        assert estimate.cycle_cost == MAX_SENSIBLE_WEAR
        assert estimate.warning is not None

    def test_manual_wear_passthrough(self):
        assert manual_wear(3.5).cycle_cost == pytest.approx(3.5)
        assert manual_wear(3.5).source == "entered manually"

    def test_manual_wear_clamped(self):
        assert manual_wear(-5.0).cycle_cost == 0.0
        assert manual_wear(1e6).cycle_cost == MAX_SENSIBLE_WEAR


class TestWearThresholds:
    def test_spread_needed_accounts_for_losses(self):
        # 2p of wear over a 90% round trip needs a 2.22p spread to clear.
        assert spread_needed(2.0, 0.9) == pytest.approx(2.222, abs=0.01)

    def test_negative_price_threshold(self):
        """Dumping to re-import pays once import goes below this."""
        assert negative_price_threshold(2.0, 0.95) == pytest.approx(-1.9)
        assert negative_price_threshold(8.0, 0.95) == pytest.approx(-7.6)

    def test_zero_wear_means_any_negative_price_pays(self):
        assert negative_price_threshold(0.0, 0.95) == pytest.approx(0.0)


class TestSettingsWearResolution:
    def test_manual_by_default(self):
        settings = RuntimeSettings(cycle_cost=3.0)
        assert settings.effective_cycle_cost(17.6) == pytest.approx(3.0)
        assert settings.wear_estimate(17.6).source == "entered manually"

    def test_derived_when_enabled(self):
        settings = RuntimeSettings(
            derive_wear_from_cost=True,
            battery_cost=500.0,
            battery_expected_cycles=1500.0,
        )
        assert settings.effective_cycle_cost(17.6) == pytest.approx(1.894, abs=0.01)
        assert "derived" in settings.wear_estimate(17.6).source

    def test_derivation_ignored_without_a_cost(self):
        """Switching derivation on with no cost entered must not silently drop the
        allowance to zero and let the optimiser cycle freely."""
        settings = RuntimeSettings(
            derive_wear_from_cost=True, battery_cost=0.0, cycle_cost=4.0
        )
        assert settings.effective_cycle_cost(17.6) == pytest.approx(4.0)

    def test_manual_value_preserved_while_derived(self):
        """Turning derivation off again must restore what was typed."""
        settings = RuntimeSettings(
            derive_wear_from_cost=True, battery_cost=500.0, cycle_cost=7.0
        )
        assert settings.effective_cycle_cost(17.6) != pytest.approx(7.0)
        settings.derive_wear_from_cost = False
        assert settings.effective_cycle_cost(17.6) == pytest.approx(7.0)

    def test_sanitiser_clamps_residual_to_cost(self):
        settings = RuntimeSettings(
            battery_cost=500.0, battery_residual_value=900.0
        ).sanitised()
        assert settings.battery_residual_value == 500.0

    def test_seeds_wear_settings_from_options(self):
        settings = RuntimeSettings()
        settings.seed_from_options(
            {
                "derive_wear_from_cost": True,
                "battery_cost": 500.0,
                "battery_expected_cycles": 1200.0,
                "battery_residual_value": 50.0,
            }
        )
        assert settings.derive_wear_from_cost is True
        assert settings.battery_cost == 500.0
        assert settings.battery_expected_cycles == 1200.0
        assert settings.battery_residual_value == 50.0

    def test_roundtrip_persists_wear_settings(self):
        settings = RuntimeSettings(
            derive_wear_from_cost=True, battery_cost=500.0, battery_expected_cycles=1800.0
        )
        restored = RuntimeSettings.from_dict(settings.as_dict())
        assert restored.derive_wear_from_cost is True
        assert restored.battery_cost == 500.0
        assert restored.battery_expected_cycles == 1800.0
