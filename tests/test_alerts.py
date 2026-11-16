"""The wording of the alerts pushed to a phone."""

from __future__ import annotations

from custom_components.ess_controller.alerts import (
    problem_alert,
    resolved_alert,
    window_short_alert,
)


class TestAFaultBecomesANotification:
    def test_the_title_and_the_first_paragraph(self):
        title, message = problem_alert(
            "soc_unreadable",
            "The battery charge cannot be read",
            "The sensor is unavailable, so nothing is applied.\n\nCheck the sensor.",
            {},
        )
        assert title == "Battery: The battery charge cannot be read"
        assert message == "The sensor is unavailable, so nothing is applied."

    def test_placeholders_are_filled_in(self):
        _, message = problem_alert(
            "battery_idle_in_self_use",
            "The battery is not discharging in self-use",
            "{detail}.\n\nThe controller has cycled the mode.",
            {"detail": "house importing 0.8 kW with the pack at 40%"},
        )
        assert message == "house importing 0.8 kW with the pack at 40%."

    def test_missing_wording_falls_back_to_the_key(self):
        title, message = problem_alert("writes_rejected", "", "", {})
        assert title == "Battery: writes rejected"
        assert message == "writes rejected"

    def test_a_placeholder_the_text_does_not_have_does_not_break_it(self):
        _, message = problem_alert("x", "X", "Needs {missing} here.", {})
        assert message == "Needs {missing} here."


class TestTheOtherAlerts:
    def test_resolved(self):
        assert resolved_alert("soc_unreadable", "The battery charge cannot be read") == (
            "Battery: resolved",
            "The battery charge cannot be read",
        )

    def test_a_window_that_ended_short_names_both_figures(self):
        title, message = window_short_alert(95.0, 71.4)
        assert title == "Battery: cheap window ended short"
        assert "71%" in message and "95%" in message
