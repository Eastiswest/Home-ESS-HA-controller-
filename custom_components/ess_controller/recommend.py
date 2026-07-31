"""Tariff comparison.

A generic comparison site asks "what would an average house pay on this tariff".
That is the wrong question for a house with a battery: a tariff with a deep
overnight trough and an expensive peak is *better* for you and worse for someone
without storage, because you can arbitrage the spread.

So this compares tariffs by running the actual optimiser against each candidate,
using your learned load and solar profiles. The ranking answers "what would *my
system* pay", which is the only figure that matters when deciding whether to
switch.

Honest about its limits, which the result carries explicitly:

* Agile-style tariffs only publish 24-48 hours ahead, so the comparison window is
  short. A few cheap days do not make an annual saving.
* Standing charges are included and scaled to the window. Unlike dispatch, where
  a constant has no effect on the optimum, they matter a lot for a switch.
* The battery's wear allowance is applied, so a tariff that only wins by cycling
  the pack twice as hard does not look artificially good.

The pure comparison logic lives here; the HTTP calls are injected so it can be
tested without a network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .models import BatterySpec, GridSpec, HorizonSlot
from .optimiser.dp import OptimiserSettings, optimise, simulate_self_use
from .tariff.base import PriceSeries

_LOGGER = logging.getLogger(__name__)

# Comparing more than this many products would hammer the API for little value.
MAX_CANDIDATES = 12


@dataclass(slots=True)
class TariffCandidate:
    """A tariff to evaluate."""

    product_code: str
    display_name: str
    tariff_code: str
    direction: str = "import"
    is_variable: bool = False
    is_green: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "product_code": self.product_code,
            "display_name": self.display_name,
            "tariff_code": self.tariff_code,
            "is_variable": self.is_variable,
            "is_green": self.is_green,
        }


@dataclass(slots=True)
class TariffScore:
    """What one tariff would have cost this system over the window."""

    candidate: TariffCandidate
    optimised_cost: float
    """Cost with the battery optimised, in minor units."""
    self_use_cost: float
    """Cost under plain self-consumption, for comparison."""
    standing_charge: float
    hours: float
    slots: int
    mean_price: float
    min_price: float
    max_price: float
    current: bool = False
    error: str | None = None

    @property
    def total(self) -> float:
        return self.optimised_cost + self.standing_charge

    @property
    def daily_equivalent(self) -> float:
        """Total scaled to a 24-hour day, which is the comparable figure."""
        if self.hours <= 0:
            return 0.0
        return self.total * 24.0 / self.hours

    @property
    def battery_benefit(self) -> float:
        """How much the battery is worth on this tariff."""
        return self.self_use_cost - self.optimised_cost

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.candidate.as_dict(),
            "current": self.current,
            "optimised_cost": round(self.optimised_cost, 2),
            "self_use_cost": round(self.self_use_cost, 2),
            "standing_charge": round(self.standing_charge, 2),
            "total": round(self.total, 2),
            "daily_equivalent": round(self.daily_equivalent, 2),
            "battery_benefit": round(self.battery_benefit, 2),
            "mean_price": round(self.mean_price, 2),
            "min_price": round(self.min_price, 2),
            "max_price": round(self.max_price, 2),
            "spread": round(self.max_price - self.min_price, 2),
            "hours": round(self.hours, 1),
            "error": self.error,
        }


@dataclass(slots=True)
class Recommendation:
    """The ranked comparison."""

    created: datetime
    scores: list[TariffScore] = field(default_factory=list)
    window_hours: float = 0.0
    note: str = ""

    @property
    def best(self) -> TariffScore | None:
        usable = [s for s in self.scores if s.error is None]
        return min(usable, key=lambda s: s.daily_equivalent) if usable else None

    @property
    def current(self) -> TariffScore | None:
        for score in self.scores:
            if score.current:
                return score
        return None

    @property
    def saving_vs_current(self) -> float | None:
        """Daily saving from switching to the best option, in minor units."""
        best, current = self.best, self.current
        if best is None or current is None or best is current:
            return None
        return current.daily_equivalent - best.daily_equivalent

    def as_dict(self) -> dict[str, Any]:
        best = self.best
        return {
            "created": self.created.isoformat(),
            "window_hours": round(self.window_hours, 1),
            "note": self.note,
            "best": best.as_dict() if best else None,
            "current": self.current.as_dict() if self.current else None,
            "saving_vs_current_per_day": (
                round(self.saving_vs_current, 2)
                if self.saving_vs_current is not None
                else None
            ),
            "ranked": [
                s.as_dict()
                for s in sorted(
                    (s for s in self.scores if s.error is None),
                    key=lambda s: s.daily_equivalent,
                )
            ],
            "failed": [s.as_dict() for s in self.scores if s.error is not None],
        }


def score_tariff(
    candidate: TariffCandidate,
    import_series: PriceSeries,
    export_series: PriceSeries,
    template: list[HorizonSlot],
    start_soc: float,
    battery: BatterySpec,
    grid: GridSpec,
    settings: OptimiserSettings,
    standing_charge_per_day: float = 0.0,
    current: bool = False,
) -> TariffScore:
    """Run the optimiser against one candidate tariff.

    ``template`` supplies the forecast load and solar for each slot -- the same
    profile is used for every candidate so the comparison isolates the tariff.
    """
    slots: list[HorizonSlot] = []
    for slot in template:
        price = import_series.price_at(slot.start)
        if price is None:
            continue
        slots.append(
            HorizonSlot(
                start=slot.start,
                end=slot.end,
                import_price=price,
                export_price=export_series.price_at(slot.start) or 0.0,
                pv_kwh=slot.pv_kwh,
                load_kwh=slot.load_kwh,
            )
        )

    if not slots:
        return TariffScore(
            candidate=candidate,
            optimised_cost=0.0,
            self_use_cost=0.0,
            standing_charge=0.0,
            hours=0.0,
            slots=0,
            mean_price=0.0,
            min_price=0.0,
            max_price=0.0,
            current=current,
            error="no prices available for the comparison window",
        )

    plan = optimise(slots, start_soc, battery, grid, settings)
    self_use = simulate_self_use(slots, start_soc, battery, grid)

    hours = sum(s.duration_hours for s in slots)
    prices = [s.import_price for s in slots]
    return TariffScore(
        candidate=candidate,
        optimised_cost=plan.total_cost,
        self_use_cost=self_use.total_cost,
        standing_charge=standing_charge_per_day * hours / 24.0,
        hours=hours,
        slots=len(slots),
        mean_price=sum(prices) / len(prices),
        min_price=min(prices),
        max_price=max(prices),
        current=current,
    )


def build_comparison_template(
    slots: list[HorizonSlot], hours: float | None = None
) -> list[HorizonSlot]:
    """Strip prices from a horizon, keeping only the load and solar forecast.

    The template is what makes the comparison fair: every candidate is scored
    against the identical demand and generation profile.
    """
    if hours is None:
        return list(slots)
    if not slots:
        return []
    cutoff = slots[0].start + timedelta(hours=hours)
    return [s for s in slots if s.start < cutoff]


# ---------------------------------------------------------------------------
# Octopus product catalogue
# ---------------------------------------------------------------------------

# Products worth comparing for a battery owner. Agile and Go have the deep
# troughs storage exploits; the flat trackers are the sensible baseline.
DEFAULT_IMPORT_PRODUCTS: tuple[str, ...] = (
    "AGILE-24-10-01",
    "GO-VAR-22-10-14",
    "COSY-22-12-08",
    "FLUX-IMPORT-23-02-14",
    "VAR-22-11-01",
)
DEFAULT_EXPORT_PRODUCTS: tuple[str, ...] = (
    "OUTGOING-AGILE-24-10-01",
    "OUTGOING-FIX-12M-19-05-13",
    "FLUX-EXPORT-23-02-14",
)


def parse_products(
    payload: dict[str, Any], direction: str = "import"
) -> list[dict[str, Any]]:
    """Extract usable products from a ``/v1/products/`` response."""
    results = payload.get("results") or []
    products: list[dict[str, Any]] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if not code:
            continue
        # Export products are marked by direction on their tariffs, but the
        # naming convention is the only signal available on the product list.
        is_export = "OUTGOING" in code.upper() or "EXPORT" in code.upper()
        if (direction == "export") != is_export:
            continue
        if entry.get("brand") and entry["brand"] != "OCTOPUS_ENERGY":
            continue
        products.append(
            {
                "code": code,
                "display_name": entry.get("display_name")
                or entry.get("full_name")
                or code,
                "is_variable": bool(entry.get("is_variable")),
                "is_green": bool(entry.get("is_green")),
                "direction": direction,
            }
        )
    return products


def parse_standing_charge(payload: dict[str, Any]) -> float:
    """Pull the current standing charge, in pence per day, from a response.

    Octopus quotes standing charges in pence per day inclusive of VAT, which is
    already the unit the comparison wants.
    """
    results = payload.get("results") or []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value_inc_vat")
        if value is None:
            value = entry.get("value_exc_vat")
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def candidates_from_codes(
    codes: list[str],
    region: str,
    direction: str = "import",
    names: dict[str, str] | None = None,
) -> list[TariffCandidate]:
    """Build candidates from product codes for one region."""
    from .tariff.octopus import build_tariff_code

    candidates: list[TariffCandidate] = []
    for code in codes[:MAX_CANDIDATES]:
        try:
            tariff_code = build_tariff_code(code, region, direction)
        except ValueError as err:
            _LOGGER.debug("Skipping product %s: %s", code, err)
            continue
        candidates.append(
            TariffCandidate(
                product_code=code,
                display_name=(names or {}).get(code, code),
                tariff_code=tariff_code,
                direction=direction,
            )
        )
    return candidates


def summarise(recommendation: Recommendation) -> str:
    """A one-line summary for the sensor state."""
    best = recommendation.best
    if best is None:
        return "no comparison available"
    current = recommendation.current
    if current is not None and best is current:
        # Already on the winner: say so, rather than merely naming the tariff.
        return f"stay on {current.candidate.display_name}"

    saving = recommendation.saving_vs_current
    if saving is None:
        # Nothing identified as the current tariff, so there is no comparison to
        # draw -- name the cheapest and leave it there.
        return f"best: {best.candidate.display_name}"
    if saving <= 0:
        return f"stay on {current.candidate.display_name}"
    return f"{best.candidate.display_name} saves ~{saving / 100:.2f}/day"
