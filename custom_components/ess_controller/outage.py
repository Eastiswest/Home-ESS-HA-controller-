"""Power cut anticipation.

The controller cannot predict an outage, but it can notice when one is more
likely and stop planning the battery down to its usual floor. The mechanism is
deliberately the one the optimiser already respects: raise the planning floor.
Nothing else in the planner needs to know why.

Three signals, any of which can raise the floor:

* **Forecast wind.** Storms are the dominant cause of unplanned distribution
  faults, and wind is already in the weather forecast being fetched. Gusts are
  used in preference to mean wind, since gusts bring lines down.
* **A risk entity.** Any binary sensor the user wires up -- a weather warning
  integration, a DNO scraper, a manual toggle -- so the feature is not limited
  to the signals shipped here.
* **A planned-outage calendar.** DNO planned interruption notices, entered or
  imported into a Home Assistant calendar.

Home Assistant-free: the coordinator resolves the entity and calendar states and
passes plain values in.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

RISK_NONE = "none"
RISK_ELEVATED = "elevated"
RISK_HIGH = "high"

# How far ahead a signal is acted on. Charging to a higher floor takes hours on
# a domestic inverter, so a storm tomorrow evening needs notice today.
DEFAULT_LOOKAHEAD = timedelta(hours=12)


@dataclass(slots=True)
class OutageWindow:
    """A period during which supply is expected to be at risk."""

    start: datetime
    end: datetime
    reason: str
    level: str = RISK_ELEVATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "reason": self.reason,
            "level": self.level,
        }


@dataclass(slots=True)
class OutageAssessment:
    """The resolved risk and the planning floor it implies."""

    level: str = RISK_NONE
    reserve_soc: float = 0.0
    """The SoC floor the optimiser should respect, in percent."""
    reason: str = ""
    windows: list[OutageWindow] = field(default_factory=list)
    max_wind: float | None = None

    @property
    def at_risk(self) -> bool:
        return self.level != RISK_NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reserve_soc": self.reserve_soc,
            "reason": self.reason,
            "max_wind": self.max_wind,
            "windows": [w.as_dict() for w in self.windows],
        }


DEFAULT_KEYWORDS: tuple[str, ...] = (
    "power",
    "outage",
    "interruption",
    "supply",
    "electricity",
    "planned",
    "shutdown",
)


def parse_keywords(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated keyword list, falling back to the defaults."""
    if raw is None:
        return DEFAULT_KEYWORDS
    words = tuple(word.strip().lower() for word in str(raw).split(",") if word.strip())
    return words or DEFAULT_KEYWORDS


def is_outage_event(summary: str, keywords: Sequence[str], all_events: bool) -> bool:
    """Whether a calendar event is plausibly about the electricity supply.

    The calendar field invites pointing at a calendar, and until this existed
    *every* event on it became a high-risk planned power cut -- a bin collection
    held 80% of the pack back for a day. Matching the summary against keywords
    means a general-purpose calendar is merely useless rather than harmful.

    ``all_events`` is the escape hatch for a DNO calendar whose entries are too
    terse to match anything, where the whole calendar really is outages.
    """
    if all_events:
        return True
    text = (summary or "").lower()
    return any(word in text for word in keywords)


def assess(
    now: datetime,
    base_reserve_soc: float,
    *,
    wind_series: list[tuple[datetime, float]] | None = None,
    wind_threshold: float = 0.0,
    wind_high_threshold: float = 0.0,
    risk_entity_on: bool = False,
    planned_outages: list[tuple[datetime, datetime, str]] | None = None,
    boost_soc: float = 50.0,
    high_boost_soc: float = 80.0,
    lookahead: timedelta = DEFAULT_LOOKAHEAD,
) -> OutageAssessment:
    """Decide whether to hold more charge back than usual.

    Returns the *higher* of the configured reserve and any boost the signals
    justify, so this can only ever make the battery more conservative -- it can
    never lower the floor a user set deliberately.
    """
    assessment = OutageAssessment(reserve_soc=base_reserve_soc)
    windows: list[OutageWindow] = []
    horizon_end = now + lookahead

    # -- planned outages: the strongest signal, because they are scheduled ----
    for start, end, summary in planned_outages or []:
        if end <= now or start > horizon_end:
            continue
        windows.append(
            OutageWindow(
                start=start,
                end=end,
                reason=summary or "planned supply interruption",
                level=RISK_HIGH,
            )
        )

    # -- forecast wind -------------------------------------------------------
    max_wind: float | None = None
    if wind_series and wind_threshold > 0:
        relevant = [
            (moment, speed)
            for moment, speed in wind_series
            if now <= moment <= horizon_end
        ]
        if relevant:
            max_wind = max(speed for _, speed in relevant)
            gusty = [(m, s) for m, s in relevant if s >= wind_threshold]
            if gusty:
                first = min(m for m, _ in gusty)
                last = max(m for m, _ in gusty)
                severe = wind_high_threshold > 0 and max_wind >= wind_high_threshold
                windows.append(
                    OutageWindow(
                        start=first,
                        end=last + timedelta(hours=1),
                        reason=f"forecast wind peaking at {max_wind:.0f}",
                        level=RISK_HIGH if severe else RISK_ELEVATED,
                    )
                )
    assessment.max_wind = max_wind

    # -- user-supplied risk signal ------------------------------------------
    if risk_entity_on:
        windows.append(
            OutageWindow(
                start=now,
                end=horizon_end,
                reason="outage risk signal active",
                level=RISK_ELEVATED,
            )
        )

    if not windows:
        assessment.reason = "no elevated risk"
        return assessment

    assessment.windows = sorted(windows, key=lambda w: w.start)
    assessment.level = (
        RISK_HIGH if any(w.level == RISK_HIGH for w in windows) else RISK_ELEVATED
    )
    boost = high_boost_soc if assessment.level == RISK_HIGH else boost_soc
    # Only ever raise the floor.
    assessment.reserve_soc = max(base_reserve_soc, boost)
    assessment.reason = "; ".join(dict.fromkeys(w.reason for w in assessment.windows))
    _LOGGER.debug(
        "Outage risk %s: holding %.0f%% (%s)",
        assessment.level,
        assessment.reserve_soc,
        assessment.reason,
    )
    return assessment


def wind_series_from_weather(
    weather: Any, limit_hours: int = 48
) -> list[tuple[datetime, float]]:
    """Build a ``(time, gust)`` series from a weather forecast.

    Gusts are preferred over mean wind speed because gusts are what bring
    conductors and trees down; mean wind is the fallback.
    """
    if weather is None:
        return []
    series: list[tuple[datetime, float]] = []
    for point in getattr(weather, "points", []):
        speed = getattr(point, "wind_gust_speed", None)
        if speed is None:
            speed = getattr(point, "wind_speed", None)
        if speed is None:
            continue
        series.append((point.time, float(speed)))
    return series[: limit_hours * 2]
