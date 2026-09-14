"""Tests for spotting a self-use inverter that has stopped discharging."""

from __future__ import annotations

from custom_components.ess_controller.stall import (
    STALL_BATTERY_KW,
    STALL_CYCLES,
    STALL_GRID_KW,
    STALL_SOC_MARGIN,
    StallReading,
    stalled,
)


def reading(**overrides) -> StallReading:
    """The 14 September evening: self-use, 31% over a 15% floor, 2.2 kW bought."""
    fields = dict(
        self_use=True, soc=31.0, floor=15.0, grid_kw=2.2, battery_kw=0.0, islanded=False
    )
    fields.update(overrides)
    return StallReading(**fields)


class TestTheRealEveningIsCaught:
    def test_idle_above_the_floor_with_the_house_importing_is_a_stall(self):
        why = stalled(reading())
        assert why is not None
        assert "31%" in why
        assert "15%" in why
        assert "2.2 kW" in why

    def test_the_august_trickle_is_caught_too(self):
        """330 W bought at 21% over a 15% floor, battery at nothing."""
        assert stalled(reading(soc=21.0, grid_kw=0.33)) is not None

    def test_it_takes_more_than_one_cycle(self):
        """One cycle can be the inverter settling after a mode write."""
        assert STALL_CYCLES >= 2


class TestOrdinaryBehaviourIsNotAStall:
    def test_a_discharging_battery_is_fine(self):
        assert stalled(reading(battery_kw=-2.1)) is None

    def test_a_charging_battery_is_fine(self):
        """Grid charging under the inverter's own logic is a different fault."""
        assert stalled(reading(battery_kw=1.5)) is None

    def test_sitting_on_the_floor_is_the_reserve_doing_its_job(self):
        assert stalled(reading(soc=15.0)) is None
        assert stalled(reading(soc=15.0 + STALL_SOC_MARGIN - 0.5)) is None

    def test_one_point_above_the_floor_is_rounding(self):
        assert stalled(reading(soc=16.0)) is None

    def test_standby_draw_is_not_the_house_on_the_grid(self):
        assert stalled(reading(grid_kw=STALL_GRID_KW)) is None
        assert stalled(reading(grid_kw=0.05)) is None

    def test_exporting_is_fine(self):
        assert stalled(reading(grid_kw=-1.2)) is None

    def test_a_trickle_still_counts_as_moving(self):
        assert stalled(reading(battery_kw=STALL_BATTERY_KW)) is None


class TestWithoutTheFactsThereIsNoVerdict:
    def test_no_grid_sensor_says_nothing(self):
        assert stalled(reading(grid_kw=None)) is None

    def test_no_battery_power_sensor_says_nothing(self):
        assert stalled(reading(battery_kw=None)) is None

    def test_no_state_of_charge_says_nothing(self):
        assert stalled(reading(soc=None)) is None

    def test_no_floor_says_nothing(self):
        assert stalled(reading(floor=None)) is None


class TestOnlySelfUseCanStall:
    def test_a_hold_is_meant_to_sit_idle(self):
        assert stalled(reading(self_use=False)) is None

    def test_an_islanded_inverter_is_left_alone(self):
        assert stalled(reading(islanded=True)) is None
