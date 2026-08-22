"""Performance history: what actually happened, half-hour by half-hour.

The plan sensors say what the controller *intends*. This module records what it
then *got*, so the two can be compared over weeks rather than argued about from
memory. It is the file you export and hand to someone -- or something -- when
the question is "is this thing actually working?".

Three groups of question it is built to answer:

* **Money.** What did the site actually spend, against two counterfactuals it
  could have had instead: no battery at all, and a battery running plain
  self-use. Beating the first is easy; beating the second is the only number
  that justifies the optimiser existing.
* **Forecasting.** Solar and load prediction error per slot, signed as well as
  absolute -- a model that is 2 kWh out in both directions is healthier than one
  quietly 2 kWh low every single day, and only the bias term distinguishes them.
* **Control.** Whether the inverter did what the plan said, and what round-trip
  efficiency the battery actually returned.

Home Assistant-free: it takes plain numbers, so every metric can be tested
against a hand-worked example.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 60
"""Two months of half-hours is ~2,900 rows: enough to see a season change in
the learned model, small enough not to bother an SD card."""

MAX_RECORDS = 5000

EPS = 1e-9

# What a kWh still in the battery is worth when the window closes.
#
# The cheap end of the prices actually seen, not their average: what leftover
# charge is worth is what it costs to put back, and valuing it at a typical price
# would let a report flatter itself simply by ending full. A low percentile
# rather than the bare minimum, so one free half-hour cannot value a whole pack
# at nothing.
STORED_ENERGY_FRACTION = 0.10


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile, so a short window still gives an answer."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(fraction, 0.0), 1.0) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(slots=True)
class SlotRecord:
    """One completed half-hour, as measured rather than as planned."""

    start: datetime

    # -- prices actually in force (minor units per kWh) -------------------
    import_price: float = 0.0
    export_price: float = 0.0

    # -- measured energy ---------------------------------------------------
    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    grid_measured: bool = False
    load_measured: bool = True
    """Whether a load sensor was actually reporting for this half-hour.

    Carried through from the completed slot, because the fault rule that asks
    "is the house being measured at all?" reads records, not live samples, and
    reading it off the wrong object is what broke setup for anyone who had
    history to read.

    Defaults *true*, unlike its counterpart on the sample: records written
    before this field existed have no answer, and treating "unknown" as "not
    measured" would report every historic half-hour as a fault.
    """

    coverage: float = 1.0

    # -- forecasts made for this slot, before it happened -----------------
    pv_forecast_kwh: float | None = None
    load_forecast_kwh: float | None = None

    # -- battery ------------------------------------------------------------
    soc_start: float | None = None
    soc_end: float | None = None
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0

    # -- what the controller was doing ------------------------------------
    planned_action: str | None = None
    applied_action: str | None = None
    controlling: bool = False
    """False while in advisory mode, which is why plan and outcome may diverge
    without anything being wrong."""

    @property
    def cost(self) -> float:
        """What this half-hour actually cost, in minor units."""
        return (
            self.grid_import_kwh * self.import_price
            - self.grid_export_kwh * self.export_price
        )

    @property
    def no_battery_cost(self) -> float:
        """The same half-hour with the battery removed entirely.

        Exact, not modelled: with no store, every kWh of shortfall is imported
        and every kWh of surplus is exported.
        """
        net = self.load_kwh - self.pv_kwh
        if net >= 0:
            return net * self.import_price
        return net * self.export_price

    @property
    def followed_plan(self) -> bool | None:
        if self.planned_action is None or self.applied_action is None:
            return None
        return self.planned_action == self.applied_action

    @property
    def pv_error(self) -> float | None:
        """Signed forecast error: positive means the forecast was too high."""
        if self.pv_forecast_kwh is None:
            return None
        return self.pv_forecast_kwh - self.pv_kwh

    @property
    def load_error(self) -> float | None:
        if self.load_forecast_kwh is None:
            return None
        return self.load_forecast_kwh - self.load_kwh

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["cost"] = round(self.cost, 3)
        data["no_battery_cost"] = round(self.no_battery_cost, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlotRecord | None:
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        try:
            payload["start"] = datetime.fromisoformat(str(payload["start"]))
        except (KeyError, TypeError, ValueError):
            return None
        try:
            return cls(**payload)
        except TypeError:
            return None


CSV_COLUMNS: tuple[str, ...] = (
    "start",
    "import_price",
    "export_price",
    "pv_kwh",
    "load_kwh",
    "pv_forecast_kwh",
    "load_forecast_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "battery_charge_kwh",
    "battery_discharge_kwh",
    "soc_start",
    "soc_end",
    "planned_action",
    "applied_action",
    "controlling",
    "coverage",
    "cost",
    "no_battery_cost",
)


@dataclass(slots=True)
class SelfUseShadow:
    """A battery that only ever follows the house, simulated alongside the real one.

    This is the counterfactual worth measuring against. "Versus no battery" only
    proves that a battery is better than no battery, which nobody doubts; the
    open question is whether planning beats the dumb behaviour the inverter
    would fall back to on its own.

    It is charged and discharged with the *measured* PV and load of each slot,
    so it faces exactly the weather and demand the real system faced.
    """

    soc: float
    capacity_kwh: float
    min_soc: float
    max_soc: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    cost: float = 0.0
    """Running total in minor units."""

    def step(self, record: SlotRecord, hours: float = 0.5) -> float:
        """Advance one slot with real weather and demand; return the slot cost."""
        if self.capacity_kwh <= 0:
            return record.no_battery_cost

        available = max(self.soc - self.min_soc, 0.0) / 100.0 * self.capacity_kwh
        headroom = max(self.max_soc - self.soc, 0.0) / 100.0 * self.capacity_kwh
        net = record.load_kwh - record.pv_kwh

        if net > 0:
            # Shortfall: cover as much as the pack can, then import the rest.
            from_battery = min(
                net, available * self.discharge_efficiency, self.max_discharge_kw * hours
            )
            drawn = from_battery / max(self.discharge_efficiency, EPS)
            self.soc -= drawn / self.capacity_kwh * 100.0
            slot_cost = (net - from_battery) * record.import_price
        else:
            surplus = -net
            to_battery = min(
                surplus,
                headroom / max(self.charge_efficiency, EPS),
                self.max_charge_kw * hours,
            )
            self.soc += to_battery * self.charge_efficiency / self.capacity_kwh * 100.0
            slot_cost = -(surplus - to_battery) * record.export_price

        self.soc = min(max(self.soc, 0.0), 100.0)
        self.cost += slot_cost
        return slot_cost

    def as_dict(self) -> dict[str, Any]:
        return {"soc": round(self.soc, 2), "cost": round(self.cost, 2)}


@dataclass(slots=True)
class PerformanceSummary:
    """Aggregated answers, computed from a window of records."""

    slots: int = 0
    days: float = 0.0
    first: datetime | None = None
    last: datetime | None = None

    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0

    cost: float = 0.0
    no_battery_cost: float = 0.0
    self_use_cost: float | None = None

    stored_energy_kwh: float = 0.0
    """Energy the real battery ended holding beyond what the shadow ended with.

    Measured at the meter -- what the difference would actually deliver to the
    house -- and signed, so a plan that ends emptier than the counterfactual is
    penalised by exactly the same rule that credits one ending fuller.

    Without this the comparison is not like-for-like and cannot be read at all.
    The shadow spends its charge covering the house every evening and arrives at
    its floor; a plan that is holding for tomorrow arrives full. Charging the
    plan for every kWh it bought while crediting the shadow for having burnt its
    own is not a measurement of anything -- it is a measurement of how much
    charge each one happened to be sitting on when the window closed.
    """

    stored_energy_rate: float = 0.0
    """Price per kWh that energy is valued at -- see ``STORED_ENERGY_FRACTION``."""

    pv_forecast_slots: int = 0
    pv_mae: float | None = None
    pv_bias: float | None = None
    load_forecast_slots: int = 0
    load_mae: float | None = None
    load_bias: float | None = None

    controlled_slots: int = 0
    compared_slots: int = 0
    followed_slots: int = 0

    cycle_cost: float = 0.0
    usable_kwh: float = 0.0
    grid_measured_slots: int = 0
    notes: list[str] = field(default_factory=list)

    # -- derived ----------------------------------------------------------

    @property
    def saving_vs_no_battery(self) -> float:
        return self.no_battery_cost - self.cost

    @property
    def saving_vs_self_use(self) -> float | None:
        if self.self_use_cost is None:
            return None
        return self.self_use_cost - self.cost

    @property
    def round_trip_efficiency(self) -> float | None:
        """Measured discharge over measured charge.

        Only meaningful over a window that starts and ends at a similar state of
        charge, so it is reported alongside the SoC drift rather than alone.
        """
        if self.battery_charge_kwh <= EPS:
            return None
        return self.battery_discharge_kwh / self.battery_charge_kwh

    @property
    def equivalent_full_cycles(self) -> float | None:
        if self.usable_kwh <= EPS:
            return None
        return self.battery_discharge_kwh / self.usable_kwh

    @property
    def wear_cost(self) -> float:
        return self.battery_discharge_kwh * self.cycle_cost

    @property
    def stored_energy_value(self) -> float:
        """What the leftover charge is worth, in the same units as the costs."""
        return self.stored_energy_kwh * self.stored_energy_rate

    @property
    def net_saving_vs_self_use(self) -> float | None:
        """Saving after wear, and after the charge each side ended holding.

        The optimiser cycles the pack harder than self-use does, and a saving
        that vanishes once that is paid for is not a saving. Equally, a plan is
        not losing money because it ends the week with a fuller battery than the
        counterfactual -- that energy is bought and still there, and the headline
        has to say so or a patient plan reads as an expensive one.
        """
        gross = self.saving_vs_self_use
        if gross is None:
            return None
        return gross - self.wear_cost + self.stored_energy_value

    @property
    def self_consumption(self) -> float | None:
        """Share of generation used on site rather than exported."""
        if self.pv_kwh <= EPS:
            return None
        return max(self.pv_kwh - self.grid_export_kwh, 0.0) / self.pv_kwh

    @property
    def plan_fidelity(self) -> float | None:
        if self.compared_slots == 0:
            return None
        return self.followed_slots / self.compared_slots

    @property
    def cost_per_day(self) -> float | None:
        return self.cost / self.days if self.days > EPS else None

    def as_dict(self) -> dict[str, Any]:
        def r(value: float | None, places: int = 2) -> float | None:
            return None if value is None else round(value, places)

        return {
            "window": {
                "slots": self.slots,
                "days": r(self.days),
                "first": self.first.isoformat() if self.first else None,
                "last": self.last.isoformat() if self.last else None,
                "grid_metered_slots": self.grid_measured_slots,
            },
            "energy_kwh": {
                "solar": r(self.pv_kwh, 3),
                "house_load": r(self.load_kwh, 3),
                "grid_import": r(self.grid_import_kwh, 3),
                "grid_export": r(self.grid_export_kwh, 3),
                "battery_charge": r(self.battery_charge_kwh, 3),
                "battery_discharge": r(self.battery_discharge_kwh, 3),
                "self_consumption": r(self.self_consumption, 4),
            },
            "money": {
                "actual": r(self.cost),
                "per_day": r(self.cost_per_day),
                "if_no_battery": r(self.no_battery_cost),
                "if_self_use_only": r(self.self_use_cost),
                "saving_vs_no_battery": r(self.saving_vs_no_battery),
                "saving_vs_self_use": r(self.saving_vs_self_use),
                "wear_cost": r(self.wear_cost),
                "stored_energy_kwh": r(self.stored_energy_kwh, 3),
                "stored_energy_rate": r(self.stored_energy_rate),
                "stored_energy_value": r(self.stored_energy_value),
                "net_saving_vs_self_use": r(self.net_saving_vs_self_use),
            },
            "forecast_error_kwh_per_slot": {
                "solar_slots": self.pv_forecast_slots,
                "solar_mae": r(self.pv_mae, 4),
                "solar_bias": r(self.pv_bias, 4),
                "load_slots": self.load_forecast_slots,
                "load_mae": r(self.load_mae, 4),
                "load_bias": r(self.load_bias, 4),
            },
            "control": {
                "armed_slots": self.controlled_slots,
                "compared_slots": self.compared_slots,
                "plan_fidelity": r(self.plan_fidelity, 4),
                "round_trip_efficiency": r(self.round_trip_efficiency, 4),
                "equivalent_full_cycles": r(self.equivalent_full_cycles, 3),
                "cycle_cost": r(self.cycle_cost, 4),
            },
            "notes": list(self.notes),
        }


class PerformanceLog:
    """A bounded, persistable history of completed slots."""

    def __init__(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.retention_days = retention_days
        self._records: list[SlotRecord] = []

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> list[SlotRecord]:
        return list(self._records)

    def add(self, record: SlotRecord) -> None:
        """Append a slot, replacing any existing record for the same start.

        Slots are keyed by their start rather than merely appended: a restart
        mid-slot can produce the same half-hour twice, and two rows for one
        half-hour would double-count the money.
        """
        for index, existing in enumerate(self._records):
            if existing.start == record.start:
                self._records[index] = record
                break
        else:
            self._records.append(record)
        self._records.sort(key=lambda item: item.start)
        self._prune()

    def _prune(self) -> None:
        if self._records and self.retention_days > 0:
            cutoff = self._records[-1].start - timedelta(days=self.retention_days)
            self._records = [r for r in self._records if r.start >= cutoff]
        if len(self._records) > MAX_RECORDS:
            self._records = self._records[-MAX_RECORDS:]

    def since(self, moment: datetime) -> list[SlotRecord]:
        return [r for r in self._records if r.start >= moment]

    def window(self, days: float, now: datetime | None = None) -> list[SlotRecord]:
        """The last ``days`` of records, measured back from the newest one.

        From the newest record rather than from the wall clock, so exporting
        after a few hours offline still returns data instead of an empty file.
        """
        if not self._records:
            return []
        end = now or self._records[-1].start
        return self.since(end - timedelta(days=days))

    def clear(self) -> None:
        self._records = []

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "records": [r.as_dict() for r in self._records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PerformanceLog:
        log = cls()
        if not data:
            return log
        try:
            log.retention_days = int(data.get("retention_days", DEFAULT_RETENTION_DAYS))
        except (TypeError, ValueError):
            log.retention_days = DEFAULT_RETENTION_DAYS
        raw = data.get("records") or []
        if isinstance(raw, list):
            parsed = [
                SlotRecord.from_dict(item) for item in raw if isinstance(item, dict)
            ]
            log._records = sorted(
                (r for r in parsed if r is not None), key=lambda item: item.start
            )
            dropped = len(raw) - len(log._records)
            if dropped:
                _LOGGER.warning("Discarded %d unreadable performance records", dropped)
        log._prune()
        return log

    # -- export ------------------------------------------------------------

    def to_csv(self, records: list[SlotRecord] | None = None) -> str:
        """The whole point of the module: a file you can hand to anything.

        CSV rather than the store's JSON because a spreadsheet, a notebook and a
        language model all read it without being told how.
        """
        rows = self._records if records is None else records
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for record in rows:
            data = record.as_dict()
            writer.writerow(
                [
                    "" if data.get(column) is None else data.get(column)
                    for column in CSV_COLUMNS
                ]
            )
        return buffer.getvalue()


def summarise(
    records: list[SlotRecord],
    *,
    cycle_cost: float = 0.0,
    usable_kwh: float = 0.0,
    shadow: SelfUseShadow | None = None,
) -> PerformanceSummary:
    """Aggregate a window of records into the numbers worth reading."""
    summary = PerformanceSummary(cycle_cost=cycle_cost, usable_kwh=usable_kwh)
    if not records:
        summary.notes.append("no records yet")
        return summary

    ordered = sorted(records, key=lambda item: item.start)
    summary.slots = len(ordered)
    summary.first = ordered[0].start
    summary.last = ordered[-1].start
    # Each record covers a half-hour, so the span is one slot longer than the
    # gap between the first and last start.
    summary.days = (
        (ordered[-1].start - ordered[0].start).total_seconds() + 1800
    ) / 86400.0

    pv_errors: list[float] = []
    load_errors: list[float] = []

    for record in ordered:
        summary.pv_kwh += record.pv_kwh
        summary.load_kwh += record.load_kwh
        summary.grid_import_kwh += record.grid_import_kwh
        summary.grid_export_kwh += record.grid_export_kwh
        summary.battery_charge_kwh += record.battery_charge_kwh
        summary.battery_discharge_kwh += record.battery_discharge_kwh
        summary.cost += record.cost
        summary.no_battery_cost += record.no_battery_cost
        if record.grid_measured:
            summary.grid_measured_slots += 1
        if record.controlling:
            summary.controlled_slots += 1
        followed = record.followed_plan
        if followed is not None:
            summary.compared_slots += 1
            summary.followed_slots += int(followed)
        if (error := record.pv_error) is not None:
            pv_errors.append(error)
        if (error := record.load_error) is not None:
            load_errors.append(error)
        if shadow is not None:
            shadow.step(record)

    if pv_errors:
        summary.pv_forecast_slots = len(pv_errors)
        summary.pv_mae = math.fsum(abs(e) for e in pv_errors) / len(pv_errors)
        summary.pv_bias = math.fsum(pv_errors) / len(pv_errors)
    if load_errors:
        summary.load_forecast_slots = len(load_errors)
        summary.load_mae = math.fsum(abs(e) for e in load_errors) / len(load_errors)
        summary.load_bias = math.fsum(load_errors) / len(load_errors)
    if shadow is not None:
        summary.self_use_cost = shadow.cost
        summary.stored_energy_rate = _percentile(
            [record.import_price for record in ordered], STORED_ENERGY_FRACTION
        )
        real_end = next(
            (r.soc_end for r in reversed(ordered) if r.soc_end is not None), None
        )
        if real_end is not None and shadow.capacity_kwh > 0:
            at_cells = (real_end - shadow.soc) / 100.0 * shadow.capacity_kwh
            summary.stored_energy_kwh = at_cells * shadow.discharge_efficiency

    _add_caveats(summary, ordered)
    return summary


def _add_caveats(summary: PerformanceSummary, records: list[SlotRecord]) -> None:
    """Say out loud what would otherwise be misread.

    A summary that quietly reports a saving from three hours of advisory-mode
    data invites exactly the wrong conclusion, so the caveats travel with the
    numbers rather than living in documentation nobody reads.
    """
    if summary.days < 3:
        summary.notes.append(
            f"only {summary.days:.1f} days of data -- too short to judge savings"
        )
    if summary.grid_measured_slots < summary.slots:
        missing = summary.slots - summary.grid_measured_slots
        summary.notes.append(
            f"{missing} of {summary.slots} slots had no grid power sensor, so their "
            "cost is derived from solar and load rather than metered"
        )
    if summary.controlled_slots == 0:
        summary.notes.append(
            "advisory mode throughout: the plan was never applied, so any "
            "difference from the counterfactuals is not the optimiser's doing"
        )
    elif summary.controlled_slots < summary.slots:
        summary.notes.append(
            f"armed for {summary.controlled_slots} of {summary.slots} slots"
        )
    if abs(summary.stored_energy_kwh) > 0.2:
        # Says where the credit comes from and, when it is doing the heavy
        # lifting, says that too. A week that spent more than self-use and still
        # showed a positive net saving is not wrong, but the reader deserves to
        # be told which of the two facts is carrying it rather than left to
        # reconcile a minus and a plus on their own.
        held = (
            "more" if summary.stored_energy_kwh > 0 else "less"
        )
        note = (
            f"ended the week with {abs(summary.stored_energy_kwh):.1f} kWh {held} "
            "in the battery than plain self-use would have -- measured against "
            "the self-use counterfactual, and valued at "
            f"{summary.stored_energy_rate:.1f}p/kWh -- the cheap end of this "
            "week's prices, which is what it would cost to put back"
        )
        if (
            summary.saving_vs_self_use is not None
            and summary.saving_vs_self_use < 0
            and summary.net_saving_vs_self_use is not None
            and summary.net_saving_vs_self_use > 0
        ):
            note += (
                ". That credit is the whole of this week's net saving: the "
                "electricity itself cost more than self-use would have"
            )
        summary.notes.append(note)
    soc_first = next((r.soc_start for r in records if r.soc_start is not None), None)
    soc_last = next((r.soc_end for r in reversed(records) if r.soc_end is not None), None)
    if soc_first is not None and soc_last is not None:
        drift = soc_last - soc_first
        if abs(drift) > 5:
            # A different quantity from the credit above, and both are worth
            # saying. That one asks whether the *comparison* is fair; this one
            # asks whether "Spent" is, because a week that ends fuller than it
            # started has bought electricity it has not used yet.
            summary.notes.append(
                f"the battery ended {drift:+.0f}% from where it started, so "
                "\"Spent\" includes electricity bought and not yet used"
            )
