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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .const import TERMINAL_MODE_FIXED, TERMINAL_MODE_ZERO
from .models import BatterySpec, GridSpec, HorizonSlot
from .optimiser.dp import (
    TERMINAL_REPLACEMENT_FRACTION,
    OptimiserSettings,
    optimise,
    percentile,
    simulate_self_use,
    terminal_value,
)
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
    """Cost with the battery optimised, in minor units, *net* of the charge left
    in the pack at the end of the window -- the same basis the optimiser uses to
    judge its own plan. See :func:`score_tariff` for why the gross figure cannot
    be compared across tariffs."""
    self_use_cost: float
    """The same for plain self-consumption, valued identically so the two
    subtract cleanly."""
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

    Both sides are scored on :attr:`Plan.net_cost`: what the window cost, less
    the value of the charge it ends holding. The gross figure cannot be compared
    across tariffs, because different tariffs deliberately end at different
    states of charge. A tariff whose cheap window falls late in the horizon fills
    the pack on the way out, so it books the whole purchase and none of the
    value -- and a comparison run on realised cost alone then recommends against
    the deep-trough tariffs a battery owner is looking for, which is the one
    answer this feature must not give. The same reasoning is why
    ``Plan.net_cost`` exists at all, and why ``optimise`` judges its own plan
    against self-use that way.

    ``settings`` must therefore price that leftover charge at a *fixed* rate, the
    same one for every candidate. Left on the default it is derived from each
    candidate's own prices, so a tariff with a deep trough credits its own
    leftover kWh more generously than a flat one does -- every tariff marking its
    own homework, and the credit is larger than the difference being measured. On
    a real comparison that put Go and Agile 0.22p apart across a day whose
    troughs differ by 10.6p a kWh, and made every total come out negative.
    :func:`common_terminal_settings` builds the right object.
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
    # The simulators do not value what they end holding, because inside the
    # optimiser they are only ever used for a gross total. Credited here, from
    # the optimiser's own function, so the baseline is comparable with the plan.
    self_use.terminal_value = (
        terminal_value(slots, battery, settings, self_use.slots[-1].soc_end)
        if self_use.slots
        else 0.0
    )

    hours = sum(s.duration_hours for s in slots)
    prices = [s.import_price for s in slots]
    return TariffScore(
        candidate=candidate,
        optimised_cost=plan.net_cost,
        self_use_cost=self_use.net_cost,
        standing_charge=standing_charge_per_day * hours / 24.0,
        hours=hours,
        slots=len(slots),
        mean_price=sum(prices) / len(prices),
        min_price=min(prices),
        max_price=max(prices),
        current=current,
    )


def common_terminal_settings(
    settings: OptimiserSettings, prices: list[float]
) -> OptimiserSettings:
    """``settings`` with leftover charge priced once, for every candidate alike.

    The rate is the cheap end of the tariff the house is actually on, because
    what a stored kWh is worth is what it costs to put back -- and until a switch
    happens, it goes back at today's prices. Any single rate would remove the
    bias; this one also keeps the figure meaning something.
    """
    if not prices:
        return replace(settings, terminal_mode=TERMINAL_MODE_ZERO)
    return replace(
        settings,
        terminal_mode=TERMINAL_MODE_FIXED,
        terminal_rate=percentile(sorted(prices), TERMINAL_REPLACEMENT_FRACTION),
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
# Product codes are what the API speaks and nobody else does. A recommendation
# that reads "GO-VAR-22-10-14 saves ~0.01/day" asks the reader to know the
# catalogue, do the currency in their head, and decide whether a hundredth of
# something is worth a switch.
PRODUCT_NAMES: dict[str, str] = {
    "AGILE-24-10-01": "Octopus Agile",
    "GO-VAR-22-10-14": "Octopus Go",
    "COSY-22-12-08": "Octopus Cosy",
    "FLUX-IMPORT-23-02-14": "Octopus Flux",
    "VAR-22-11-01": "Octopus Flexible",
    "OUTGOING-AGILE-24-10-01": "Outgoing Agile",
    "OUTGOING-FIX-12M-19-05-13": "Outgoing Fixed",
    "FLUX-EXPORT-23-02-14": "Flux Export",
}


def friendly_name(product_code: str, fallback: str = "") -> str:
    """The name on the tariff's own web page, where we know it."""
    return PRODUCT_NAMES.get(product_code, fallback or product_code)


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
                display_name=(names or {}).get(code) or friendly_name(code),
                tariff_code=tariff_code,
                direction=direction,
            )
        )
    return candidates


# Below this, a day's difference is not a reason to change supplier.
#
# The window is a day of forecast load against a day of forecast solar, and the
# forecast is worth a few pence either way on its own. A ranking separated by
# less than this has ordered the noise, and saying "switch to X, it saves 0.01"
# invites somebody to spend an afternoon on a tariff change worth nothing.
WORTH_SWITCHING_PENCE_PER_DAY = 5.0


def _money(pence_per_day: float) -> str:
    """A day's difference, in the units a person actually thinks in."""
    yearly = pence_per_day * 365 / 100.0
    if pence_per_day < 100:
        return f"about {pence_per_day:.0f}p a day (~\u00a3{yearly:.0f} a year)"
    return f"about \u00a3{pence_per_day / 100:.2f} a day (~\u00a3{yearly:.0f} a year)"


def summarise(recommendation: Recommendation) -> str:
    """A one-line summary for the sensor state.

    Written to be read by someone who does not know the product catalogue and is
    not going to convert pence-per-day into anything. It used to say
    "GO-VAR-22-10-14 saves ~0.01/day", which names a code rather than a tariff,
    gives no currency, and dresses a hundredth of a penny as a recommendation.
    """
    best = recommendation.best
    if best is None:
        return "no comparison available"
    current = recommendation.current
    if current is not None and best is current:
        # Already on the winner: say so, rather than merely naming the tariff.
        return f"stay on {current.candidate.display_name} — nothing cheaper found"

    saving = recommendation.saving_vs_current
    if saving is None:
        # Nothing identified as the current tariff, so there is no comparison to
        # draw -- name the cheapest and leave it there.
        return f"cheapest looks like {best.candidate.display_name}"
    if saving < WORTH_SWITCHING_PENCE_PER_DAY:
        # Includes the negative case. Naming the near-winner anyway would read as
        # a suggestion, which is the opposite of what the number says.
        return (
            f"stay on {current.candidate.display_name} — nothing else is "
            "meaningfully cheaper"
        )
    return f"{best.candidate.display_name} would save {_money(saving)}"
