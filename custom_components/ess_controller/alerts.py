"""Wording for the alerts pushed to a phone.

The Repairs panel already says what is wrong, in full, for whoever opens it.
Nobody opens it at five in the morning. These are the same faults cut down to
a notification: a title that says what, and the first paragraph of the
Repairs text for why. Home Assistant-free so the wording can be tested flat.
"""

from __future__ import annotations

import contextlib

PREFIX = "Battery"


def problem_alert(
    key: str, title: str, description: str, placeholders: dict[str, str]
) -> tuple[str, str]:
    """Title and message for a fault, from its Repairs text."""
    heading = (title or key.replace("_", " ")).strip()
    body = (description or "").split("\n\n", 1)[0]
    with contextlib.suppress(KeyError, IndexError, ValueError):
        body = body.format(**placeholders)
    return f"{PREFIX}: {heading}", body.strip() or heading


def resolved_alert(key: str, title: str) -> tuple[str, str]:
    """Title and message for a fault that has cleared."""
    heading = (title or key.replace("_", " ")).strip()
    return f"{PREFIX}: resolved", heading


def window_short_alert(aim_soc: float, reached_soc: float) -> tuple[str, str]:
    """A cheap window that ended well under what the plan meant it to reach."""
    return (
        f"{PREFIX}: cheap window ended short",
        f"The pack was at {reached_soc:.0f}% when the cheap rate ended; the plan "
        f"aimed for {aim_soc:.0f}%. The inverter may not have charged as asked.",
    )
