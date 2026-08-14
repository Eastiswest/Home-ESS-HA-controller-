"""Tests for the fault detection behind the Repairs entries."""

from __future__ import annotations

from custom_components.ess_controller.problems import (
    PERSIST_CYCLES,
    QUIET_LOAD_SLOTS,
    Snapshot,
    detect,
)


def keys(state: Snapshot) -> set[str]:
    return {problem.key for problem in detect(state)}


class TestNothingIsWrong:
    def test_a_healthy_install_says_nothing(self):
        assert detect(Snapshot(controlling=True)) == []

    def test_an_advisory_install_is_not_nagged_about_control(self):
        """Nothing about writing is a fault on an install never given control."""
        state = Snapshot(
            controlling=False,
            inverter_available=False,
            soc_readable=False,
            rejected_roles={"min_soc": "rejected"},
            unverified_writes=["use_mode"],
            failing_cycles=99,
        )
        assert detect(state) == []


class TestItHasStoppedControlling:
    def test_a_lost_inverter_is_an_error_once_it_persists(self):
        state = Snapshot(
            controlling=True, inverter_available=False, failing_cycles=PERSIST_CYCLES
        )
        assert "inverter_unreachable" in keys(state)
        assert detect(state)[0].severity == "error"

    def test_a_blip_is_not_a_fault(self):
        """A Modbus timeout that clears on the retry is Tuesday, not a repair."""
        state = Snapshot(
            controlling=True, inverter_available=False, failing_cycles=PERSIST_CYCLES - 1
        )
        assert "inverter_unreachable" not in keys(state)

    def test_an_unreadable_battery_is_named(self):
        """The controller refuses to write on a guess, and says nothing about it.

        From outside that is indistinguishable from a system that has decided to
        do nothing all day for no reason.
        """
        assert "soc_unreadable" in keys(Snapshot(controlling=True, soc_readable=False))

    def test_a_refused_write_names_the_control_and_the_reason(self):
        state = Snapshot(
            controlling=True,
            rejected_roles={"min_soc": "Modbus exception 4 at 0xc5"},
        )
        problem = next(p for p in detect(state) if p.key == "writes_rejected")
        assert problem.placeholders["controls"] == "min_soc"
        assert "0xc5" in problem.placeholders["reason"]

    def test_a_dropped_write_is_a_warning_not_an_error(self):
        """It still controls; the plan and the hardware have just diverged."""
        state = Snapshot(controlling=True, unverified_writes=["use_mode", "use_mode"])
        problem = next(p for p in detect(state) if p.key == "writes_not_verified")
        assert problem.severity == "warning"
        # Deduplicated: the same control failing twice is one fault.
        assert problem.placeholders["controls"] == "use_mode"


class TestSomethingElseIsDrivingTheInverter:
    def test_unplanned_grid_charging_is_reported_with_what_it_cost(self):
        state = Snapshot(unexplained_charge_kwh=2.29, unexplained_charge_cost=51.0)
        problem = next(p for p in detect(state) if p.key == "unexplained_grid_charging")
        assert problem.placeholders == {"kwh": "2.3", "cost": "51"}

    def test_it_is_raised_even_without_control(self):
        """This is the inverter acting on its own settings, so handing back
        control does not make it stop -- it makes it invisible."""
        assert "unexplained_grid_charging" in keys(
            Snapshot(controlling=False, unexplained_charge_kwh=1.0)
        )


class TestItCannotSeeSomethingItWasTold:
    def test_a_vanished_forecast_entity_is_named(self):
        state = Snapshot(missing_forecast_entities=["sensor.energy_production_today"])
        problem = next(p for p in detect(state) if p.key == "forecast_entity_missing")
        assert "energy_production_today" in problem.placeholders["entities"]

    def test_a_silent_load_sensor_is_named_after_a_few_hours(self):
        assert "load_not_measured" in keys(Snapshot(quiet_load_slots=QUIET_LOAD_SLOTS))

    def test_a_quiet_half_hour_is_not_a_fault(self):
        assert "load_not_measured" not in keys(
            Snapshot(quiet_load_slots=QUIET_LOAD_SLOTS - 1)
        )

    def test_a_control_disabled_in_home_assistant_is_named(self):
        """The fault that hid longest, because it reads as a hardware limit.

        A grid-charge control the inverter plainly had went unmapped for weeks
        and was reported as a control the inverter did not have.
        """
        state = Snapshot(
            controlling=True,
            unfilled_control_roles=["grid_charge"],
            disabled_candidates=["select.solax1_inverter_selfuse_night_charge"],
        )
        problem = next(
            p for p in detect(state) if p.key == "control_disabled_in_home_assistant"
        )
        assert problem.placeholders["roles"] == "grid_charge"
        assert "selfuse_night_charge" in problem.placeholders["entities"]
        assert problem.placeholders["noun"] == "control"

    def test_an_unfilled_role_with_no_candidate_is_not_raised(self):
        """Nothing to enable means nothing to do, and a repair with no action is
        a notification that trains people to ignore notifications."""
        state = Snapshot(controlling=True, unfilled_control_roles=["target_soc"])
        assert "control_disabled_in_home_assistant" not in keys(state)


class TestTheKeysAreStable:
    def test_every_problem_has_a_key_and_a_severity(self):
        state = Snapshot(
            controlling=True,
            inverter_available=False,
            soc_readable=False,
            rejected_roles={"min_soc": "no"},
            unverified_writes=["use_mode"],
            unfilled_control_roles=["grid_charge"],
            disabled_candidates=["select.x"],
            missing_forecast_entities=["sensor.gone"],
            unexplained_charge_kwh=1.0,
            quiet_load_slots=QUIET_LOAD_SLOTS,
            failing_cycles=PERSIST_CYCLES,
        )
        problems = detect(state)
        assert len(problems) == 8
        assert len({p.key for p in problems}) == len(problems)
        for problem in problems:
            assert problem.severity in ("warning", "error")
            assert problem.key.islower()
