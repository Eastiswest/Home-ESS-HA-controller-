"""Battery wear allowance.

The wear allowance is the single most influential number in the optimiser: it
decides how big a price spread has to be before cycling the pack is worth it, and
on a negative-price day it decides whether the battery is emptied to make room at
all. Getting it roughly right matters more than getting any other setting exact.

Two ways to set it:

* **Directly**, in minor units per kWh cycled, if you already know your figure.
* **From what the pack cost you**, which is how most people actually think about
  it. The arithmetic is just

      wear per kWh = (pack cost - residual value) / (cycles x usable kWh)

  A pack that cost 500 and will deliver 1500 cycles of 17.6 usable kWh has
  26,400 kWh of throughput in it, so each kWh cycled costs about 1.9p.

Two honest caveats, both surfaced to the user rather than buried:

* **Cycle life depends on depth of discharge.** Manufacturers quote cycles at a
  stated DoD, and a pack worked between 15% and 95% will not last as long as one
  nursed around the middle. The figure entered should be the cycle life *for the
  SoC window actually configured*, not the headline number.
* **Calendar ageing is not modelled.** A pack that dies of old age before its
  throughput is used up cost more per kWh than this says. For a cheap salvage
  pack cycled hard that is usually a second-order effect; for an expensive pack
  cycled lightly it is not.

Home Assistant-free so the arithmetic is directly testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Minor currency units per major unit. 100 for pounds, euros and dollars alike.
MINOR_PER_MAJOR = 100.0

#: A plausible default for a used EV pack worked over a wide SoC window. Well
#: short of a new LFP pack's several thousand, and deliberately conservative.
DEFAULT_EXPECTED_CYCLES = 1500.0

#: Guard rails. A wear allowance outside this range is almost certainly a typo,
#: and a wrong value here quietly distorts every plan.
MAX_SENSIBLE_WEAR = 100.0


@dataclass(slots=True)
class WearEstimate:
    """A derived wear allowance and the workings behind it."""

    cycle_cost: float
    """Wear allowance in minor units per kWh cycled."""
    lifetime_throughput_kwh: float
    net_cost: float
    """Pack cost less residual, in major units."""
    usable_kwh: float
    cycles: float
    source: str
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_cost": round(self.cycle_cost, 3),
            "source": self.source,
            "net_cost": round(self.net_cost, 2),
            "usable_kwh": round(self.usable_kwh, 2),
            "expected_cycles": round(self.cycles, 0),
            "lifetime_throughput_kwh": round(self.lifetime_throughput_kwh, 0),
            "warning": self.warning,
        }


def wear_from_cost(
    pack_cost: float,
    usable_kwh: float,
    cycles: float = DEFAULT_EXPECTED_CYCLES,
    residual_value: float = 0.0,
    minor_per_major: float = MINOR_PER_MAJOR,
) -> WearEstimate:
    """Derive a wear allowance from what the pack cost.

    ``pack_cost`` and ``residual_value`` are in major currency units (pounds);
    the result is in minor units (pence) per kWh, matching the price series.
    """
    net_cost = max(pack_cost - residual_value, 0.0)
    throughput = max(cycles, 0.0) * max(usable_kwh, 0.0)

    if throughput <= 0.0:
        return WearEstimate(
            cycle_cost=0.0,
            lifetime_throughput_kwh=0.0,
            net_cost=net_cost,
            usable_kwh=usable_kwh,
            cycles=cycles,
            source="unavailable",
            warning=(
                "Cannot derive a wear allowance without both a usable capacity "
                "and an expected cycle life."
            ),
        )

    cycle_cost = net_cost * minor_per_major / throughput
    warning: str | None = None

    if cycle_cost > MAX_SENSIBLE_WEAR:
        warning = (
            f"Derived wear allowance of {cycle_cost:.0f} per kWh looks implausible; "
            "check the pack cost, capacity and cycle life."
        )
        _LOGGER.warning("%s", warning)
        cycle_cost = MAX_SENSIBLE_WEAR
    elif pack_cost > 0 and cycle_cost <= 0.0:
        warning = "Derived wear allowance is zero, so the optimiser will cycle freely."

    return WearEstimate(
        cycle_cost=cycle_cost,
        lifetime_throughput_kwh=throughput,
        net_cost=net_cost,
        usable_kwh=usable_kwh,
        cycles=cycles,
        source="derived from pack cost",
        warning=warning,
    )


def manual_wear(cycle_cost: float) -> WearEstimate:
    """Wrap a directly entered wear allowance in the same shape."""
    clamped = min(max(cycle_cost, 0.0), MAX_SENSIBLE_WEAR)
    return WearEstimate(
        cycle_cost=clamped,
        lifetime_throughput_kwh=0.0,
        net_cost=0.0,
        usable_kwh=0.0,
        cycles=0.0,
        source="entered manually",
    )


def spread_needed(cycle_cost: float, round_trip_efficiency: float) -> float:
    """Minimum import/export spread that makes a round trip worthwhile.

    Useful to show alongside the allowance: it turns an abstract p/kWh into "the
    prices have to differ by at least this much before I will cycle".
    """
    return cycle_cost / max(round_trip_efficiency, 1e-6)


def negative_price_threshold(cycle_cost: float, charge_efficiency: float) -> float:
    """How negative import must go before dumping to re-import pays.

    On a negative-price day the battery can be emptied purely to make headroom,
    giving the energy away and being paid to take it back. That pays once the
    negative price exceeds the wear allowance scaled by charge efficiency.
    Returned as a negative number, being a price.
    """
    return -cycle_cost * max(charge_efficiency, 0.0)
