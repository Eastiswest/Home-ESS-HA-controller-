"""Detecting a self-use inverter that has stopped discharging.

A SolaX X1 Hybrid G4 that has just had its self-use floor raised for a hold and
then lowered again can sit in self-use with the house on the grid and the
battery idle, well above the floor, until its working mode is cycled. Nothing in
its registers says so: the mode reads Self Use, the floor reads what was written,
and every write verifies. The only evidence is the power flows.

Home Assistant-free so the rule can be tested against hand-written readings.
"""

from __future__ import annotations

from dataclasses import dataclass

# Grid import above which the house is clearly not being carried by the array
# and the pack. Standby draw and meter noise sit well below this.
STALL_GRID_KW = 0.25

# Battery flow below which the pack counts as idle rather than trickling.
STALL_BATTERY_KW = 0.1

# How far above the written floor the charge must sit before idleness is a
# fault rather than the reserve doing its job. State of charge is reported in
# whole percent, so one point is rounding.
STALL_SOC_MARGIN = 2.0

# Consecutive cycles the symptom must hold before the working mode is cycled.
# One cycle can be the inverter settling after a mode write.
STALL_CYCLES = 2


@dataclass(frozen=True, slots=True)
class StallReading:
    """What the rule is allowed to look at, all from the same cycle."""

    self_use: bool
    """The controller asked for self-use and the inverter reports it."""

    soc: float | None
    floor: float | None
    """The reserve the inverter is actually holding, as read back."""

    grid_kw: float | None
    """Positive when importing. None when no grid sensor is configured."""

    battery_kw: float | None
    """Any sign convention; only its magnitude is used. None when unmeasured."""

    islanded: bool = False


def stalled(reading: StallReading) -> str | None:
    """Why the battery ought to be discharging and is not, or None."""
    if not reading.self_use or reading.islanded:
        return None
    if reading.soc is None or reading.floor is None:
        return None
    if reading.grid_kw is None or reading.battery_kw is None:
        # Without both flows there is no evidence either way.
        return None
    if reading.grid_kw <= STALL_GRID_KW:
        return None
    if abs(reading.battery_kw) >= STALL_BATTERY_KW:
        return None
    if reading.soc < reading.floor + STALL_SOC_MARGIN:
        return None
    return (
        f"battery idle at {reading.soc:.0f}% with the floor at {reading.floor:.0f}% "
        f"while the house imports {reading.grid_kw:.1f} kW in self-use"
    )
