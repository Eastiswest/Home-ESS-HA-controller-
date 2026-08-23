"""The planning and control loop.

Each cycle does the same six things:

1. Read live state -- inverter mode, battery SoC, PV and load power.
2. Feed those readings into the learning model as completed half-hours.
3. Gather forward inputs -- tariff prices, solar forecast, weather.
4. Build the planning horizon and optimise it.
5. Resolve what to do *now*, honouring any manual override or strategy lock.
6. Apply it to the inverter -- or, in dry-run mode, describe what it would do.

The optimiser runs in an executor. It is fast (tens of milliseconds), but it is
CPU-bound, and the event loop should not be blocked by arithmetic.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import outage as outage_mod
from . import problems, recommend
from .adjustments import (
    KIND_FREE_ELECTRICITY,
    KIND_SAVING_SESSION,
    AdjustmentResult,
    SessionEvent,
    active_session,
    adjustments_from_sessions,
    apply_adjustments,
    next_session,
    parse_session_events,
)
from .const import (
    ADAPTER_GENERIC,
    ADAPTER_SOLAX_MODBUS,
    CHEAP_SLOT_FRACTION,
    CONF_AGILE_PREDICT,
    CONF_AGILE_PREDICT_EXPORT,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_BATTERY_HEALTH_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_ENTITY_MAP,
    CONF_FREE_SESSION_ENTITIES,
    CONF_GRID_EXPORT_LIMIT,
    CONF_GRID_IMPORT_LIMIT,
    CONF_GRID_POWER_ENTITY,
    CONF_HORIZON_HOURS,
    CONF_INVERTER_ADAPTER,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_POWER_ENTITY,
    CONF_LOG_RETENTION_DAYS,
    CONF_OCTOPUS_IMPORT_PRODUCT,
    CONF_OCTOPUS_REGION,
    CONF_ONLY_JOINED_SESSIONS,
    CONF_OUTAGE_CALENDAR,
    CONF_OUTAGE_CALENDAR_ALL_EVENTS,
    CONF_OUTAGE_CALENDAR_KEYWORDS,
    CONF_OUTAGE_HIGH_RESERVE_SOC,
    CONF_OUTAGE_LOOKAHEAD_HOURS,
    CONF_OUTAGE_RESERVE_SOC,
    CONF_OUTAGE_RISK_ENTITY,
    CONF_OUTAGE_WIND_HIGH_THRESHOLD,
    CONF_OUTAGE_WIND_THRESHOLD,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_RECOMMEND_IMPORT_PRODUCTS,
    CONF_RECOMMEND_WINDOW_HOURS,
    CONF_SAVING_SESSION_ENTITIES,
    CONF_SAVING_SESSION_RATE,
    CONF_SESSION_REWARD_EXPORT,
    CONF_SHIFTABLE_LOADS,
    CONF_SOC_LEVELS,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_PEAK_POWER,
    CONF_TERMINAL_VALUE_MODE,
    CONF_TERMINAL_VALUE_RATE,
    CONF_WEATHER_ENTITY,
    DEFAULT_AGILE_PREDICT,
    DEFAULT_AGILE_PREDICT_EXPORT,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_GRID_IMPORT_LIMIT,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_LOG_RETENTION_DAYS,
    DEFAULT_OUTAGE_CALENDAR_ALL_EVENTS,
    DEFAULT_OUTAGE_CALENDAR_KEYWORDS,
    DEFAULT_OUTAGE_HIGH_RESERVE_SOC,
    DEFAULT_OUTAGE_LOOKAHEAD_HOURS,
    DEFAULT_OUTAGE_RESERVE_SOC,
    DEFAULT_OUTAGE_WIND_HIGH_THRESHOLD,
    DEFAULT_OUTAGE_WIND_THRESHOLD,
    DEFAULT_RECOMMEND_WINDOW_HOURS,
    DEFAULT_SAVING_SESSION_RATE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SOC_LEVELS,
    DEFAULT_SOLAR_PEAK_POWER,
    DOMAIN,
    SLOT_MINUTES,
    STRATEGY_AUTO,
    STRATEGY_FORCE_CHARGE,
    STRATEGY_FORCE_DISCHARGE,
    STRATEGY_IDLE,
    STRATEGY_OFF,
    STRATEGY_SELF_USE,
    TERMINAL_MODE_HORIZON_MEDIAN,
)
from .dashboard import OUTAGE_HOLD_MARK
from .forecast.confidence import daytime_correction, evening_uplift, is_evening
from .forecast.confidence import describe as describe_confidence
from .forecast.energy import EnergySeries
from .forecast.load import LoadForecaster, describe_climate_uplift
from .forecast.solar import (
    SolarForecaster,
    build_forecast_series,
    daily_total_offset,
    parse_solar_forecast_attributes,
)
from .forecast.weather import WeatherSeries
from .inverter.base import (
    ApplyResult,
    InverterAdapter,
    InverterState,
    NullAdapter,
    power_kw,
)
from .inverter.battery import BatteryReading, BatterySource
from .inverter.generic import GenericEntityAdapter
from .inverter.roles import (
    ROLE_BATTERY_VOLTAGE,
    ROLE_MIN_SOC,
    ROLE_SOC,
    ROLE_USE_MODE,
    SOLAX_ROLE_SPECS,
    discover_entities,
    merge_overrides,
)
from .inverter.solax import SolaxModbusAdapter
from .learning.model import LoadObservation, SolarObservation
from .learning.store import LearningStore
from .models import (
    BatterySpec,
    ControlCommand,
    GridSpec,
    HorizonSlot,
    Override,
    Plan,
    PlanSlot,
    SiteState,
    SlotAction,
    describe_horizon_reach,
)
from .optimiser.dp import OptimiserSettings, optimise, percentile
from .performance import PerformanceSummary, SelfUseShadow, SlotRecord, summarise
from .performance_store import PerformanceStore
from .runtime import RuntimeSettings, RuntimeStore
from .sampling import SlotAccumulator, slot_boundaries, slot_start_for
from .shifting import (
    LoadPlacement,
    add_placements_to_slots,
    appliance_targets,
    describe_placements,
    parse_shiftable_loads,
    place_loads,
)
from .tariff import agile_predict
from .tariff.base import PriceSeries
from .tariff.factory import build_provider
from .tariff.octopus import OctopusApiError

_LOGGER = logging.getLogger(__name__)

_SLOT = timedelta(minutes=SLOT_MINUTES)

# How many applies to keep for the diagnostics download. Twenty half-hourly-ish
# cycles is a couple of hours of behaviour: long enough to show a setting that
# was written once and never cleared, short enough not to bloat the file.
APPLY_HISTORY = 20

# Roles whose absence stops the controller doing something it was asked to do.
# The measurement roles are optional by design -- an install with no battery
# power sensor works fine -- so their absence is not a fault.
CONTROL_ROLES = frozenset(
    {"use_mode", "manual_mode", "charge_limit", "discharge_limit", "grid_charge"}
)

# How much of a half-hour's battery gain the sun must fail to explain before the
# slot is worth reporting. A whole-percent state of charge on a 22 kWh pack
# quantises to 0.22 kWh, so this is comfortably clear of the rounding.
UNEXPLAINED_CHARGE_KWH = 0.15

# Solar reaching the cells rather than the meter. The real figure is a setting;
# this only has to be close enough not to invent an unexplained kilowatt-hour out
# of an efficiency rounding, and erring high makes the check quieter, not louder.
CHARGE_EFFICIENCY_HINT = 1.0

# Weather and solar forecasts change slowly; refetching every cycle would spam
# the upstream integration for no benefit.
FORECAST_REFRESH = timedelta(minutes=20)


def _slot_is_forecast(series: PriceSeries, start: datetime) -> bool:
    """Whether the price covering ``start`` is predicted rather than announced."""
    for slot in series:
        if slot.start <= start < slot.end:
            return slot.is_forecast
    return False


class EssCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Plans and applies battery dispatch."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
            # Explicit rather than left to the ContextVar the base class would
            # otherwise infer it from: that inference is deprecated and stops
            # working in Home Assistant 2026.8, at which point setup raises and
            # the integration produces no entities at all.
            config_entry=entry,
        )
        self.entry = entry
        self.learning_store = LearningStore(hass, entry.entry_id)
        self.runtime_store = RuntimeStore(hass, entry.entry_id)
        self.performance_store = PerformanceStore(hass, entry.entry_id)
        self.settings: RuntimeSettings = RuntimeSettings()
        # (slot start, action) for the half-hour currently being acted on.
        self._committed: tuple[datetime, SlotAction] | None = None
        self.accumulator = SlotAccumulator()

        self.plan: Plan | None = None
        self.override: Override | None = None
        self.last_apply: ApplyResult | None = None
        # One apply is a snapshot; a fault is usually a sequence. A leftover
        # Force Charge, a write that stopped landing, a mode flapping every
        # cycle -- none of them are visible in the single most recent result,
        # and each of them has cost a real install money while the file that was
        # meant to explain it showed one clean line. Cheap to keep, and the
        # first thing worth reading when the behaviour and the plan disagree.
        self._apply_history: deque[dict[str, Any]] = deque(maxlen=APPLY_HISTORY)
        self._raised_problems: set[str] = set()
        self._last_site: SiteState | None = None
        # Consecutive cycles each fault has been true for. A single Modbus
        # timeout is not a fault; the same one for a quarter of an hour is, and
        # so is anything else that outlasts the entities arriving at boot.
        self._problem_runs: dict[str, int] = {}
        self.last_command: ControlCommand | None = None
        self.inverter_state = InverterState()
        self.battery: BatteryReading = BatteryReading()

        self._adapter: InverterAdapter = NullAdapter(hass)
        self._battery_source: BatterySource | None = None
        self._import_provider = None
        self._export_provider = None
        self._weather: WeatherSeries | None = None
        self._solar_forecast: EnergySeries | None = None
        self._solar_daily_totals: dict[Any, float] = {}
        self._solar_forecast_note: str = ""
        self._forecast_fetched: datetime | None = None
        self._import_prices = PriceSeries()
        self._export_prices = PriceSeries()
        self._diagnostics: dict[str, Any] = {}
        # Per-slot climate uplift, so the temperature dials' contribution can be
        # read off rather than inferred from the size of the bill.
        self._climate_uplift: dict[datetime, float] = {}
        self._climate_note: str = ""
        self._plan_error: str | None = None
        self.sessions: list[SessionEvent] = []
        self.adjustment_result: AdjustmentResult | None = None
        # What AgilePredict contributed this cycle, and how it is doing. None
        # when the feature is off, so diagnostics can tell "disabled" from
        # "enabled and failing".
        self.price_forecast: dict[str, Any] | None = None
        self.outage: outage_mod.OutageAssessment = outage_mod.OutageAssessment()
        self.placements: list[LoadPlacement] = []
        self._slot_marks: dict[datetime, dict[str, Any]] = {}
        self._report_cache: dict[float, PerformanceSummary] = {}
        self._appliance_until: dict[str, datetime] = {}
        self._appliance_notes: list[str] = []
        self.recommendation = None
        self._raw_import_prices = PriceSeries()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    @property
    def options(self) -> dict[str, Any]:
        merged = dict(self.entry.data)
        merged.update(self.entry.options)
        return merged

    async def async_setup(self) -> None:
        """Load persisted state and build the adapters."""
        await self.learning_store.async_load()
        self.settings = await self.runtime_store.async_load(self.options)
        await self.performance_store.async_load(
            int(self.options.get(CONF_LOG_RETENTION_DAYS, DEFAULT_LOG_RETENTION_DAYS))
        )
        self._build_adapters()
        self._build_tariffs()

    @callback
    def async_start_live_polling(self) -> CALLBACK_TYPE:
        """Re-read the live state every ``DEFAULT_LIVE_INTERVAL``.

        Separate from the planning cycle on purpose. Re-planning pulls forecasts
        and can call the tariff API, so it belongs on a five-minute clock; reading
        the inverter is pure state-machine lookups against entities another
        integration is already polling, so it costs nothing and there is no excuse
        for the link status to be up to five minutes behind reality. That gap is
        exactly what made "inverter link disconnected" sit there while values were
        visibly still arriving.

        Returns the unsubscribe callable, for ``entry.async_on_unload``.
        """
        return async_track_time_interval(
            self.hass,
            self._async_live_refresh,
            DEFAULT_LIVE_INTERVAL,
            name=f"{DOMAIN} live state",
        )

    async def _async_live_refresh(self, _now: datetime) -> None:
        """Refresh the live readings without re-planning.

        Deliberately not ``async_set_updated_data``: that reschedules the main
        interval, so a fast poll would postpone re-planning for ever and the plan
        would never be rebuilt. This updates the data and notifies listeners
        directly, leaving the planning clock alone.
        """
        if self.data is None:
            # Nothing to patch yet; the first full refresh has not finished.
            return
        now = dt_util.utcnow()
        try:
            self.inverter_state = await self._adapter.async_read_state()
            site = self._read_site_state(now)
            self.data = self._build_data(now, site)
        except Exception:  # pragma: no cover - a poll must never kill the timer
            _LOGGER.exception("Live state refresh failed")
            return
        self.async_update_listeners()

    def _build_adapters(self) -> None:
        options = self.options
        adapter_kind = options.get(CONF_INVERTER_ADAPTER, ADAPTER_SOLAX_MODBUS)
        prefix = options.get(CONF_INVERTER_PREFIX) or None
        overrides = options.get(CONF_ENTITY_MAP) or {}

        discovered = discover_entities(self.hass, SOLAX_ROLE_SPECS, prefix)
        entities = merge_overrides(discovered, overrides)

        nominal_voltage = float(options.get("nominal_voltage", 360.0) or 360.0)

        if adapter_kind == ADAPTER_SOLAX_MODBUS:
            self._adapter = SolaxModbusAdapter(
                self.hass,
                entities,
                nominal_voltage=nominal_voltage,
                manage_export_limit=bool(options.get("manage_export_limit", False)),
            )
        elif adapter_kind == ADAPTER_GENERIC:
            self._adapter = GenericEntityAdapter(
                self.hass,
                entities,
                nominal_voltage=nominal_voltage,
                manage_export_limit=bool(options.get("manage_export_limit", False)),
                options=options.get("adapter_options") or {},
            )
        else:
            self._adapter = NullAdapter(self.hass)

        self._battery_source = BatterySource(
            self.hass,
            nameplate_capacity_kwh=float(
                options.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
            ),
            soc_entity=options.get(CONF_BATTERY_SOC_ENTITY),
            capacity_entity=options.get(CONF_BATTERY_CAPACITY_ENTITY),
            health_entity=options.get(CONF_BATTERY_HEALTH_ENTITY),
            temperature_entity=options.get(CONF_BATTERY_TEMPERATURE_ENTITY),
            voltage_entity=entities.get(ROLE_BATTERY_VOLTAGE),
            derate_capacity_by_soh=bool(options.get("derate_capacity_by_soh", False)),
        )

    def _rediscover_if_blind(self) -> None:
        """Re-scan for inverter entities while we cannot see the inverter at all.

        Discovery ran once, at setup. That is wrong for the commonest install
        order: people add this integration first and get the inverter talking
        afterwards, so the scan finds nothing, the entity map is frozen empty, and
        "Inverter link: Disconnected" persists for ever even though Home Assistant
        is now full of the inverter's sensors. Reloading fixed it, which is a thing
        nobody should have to know.

        Cheap because it is conditional: once either the state-of-charge or the
        use-mode entity is mapped we never scan again, so a working install pays
        nothing. Overrides the user set by hand always win, as before.
        """
        if self._adapter is None:
            return
        entities = self._adapter.entities
        if entities.get(ROLE_SOC) or entities.get(ROLE_USE_MODE):
            return

        options = self.options
        discovered = discover_entities(
            self.hass, SOLAX_ROLE_SPECS, options.get(CONF_INVERTER_PREFIX) or None
        )
        if not discovered:
            return
        merged = merge_overrides(discovered, options.get(CONF_ENTITY_MAP) or {})
        if merged == entities:
            return
        _LOGGER.info(
            "Found %d inverter entities that did not exist at setup; rebuilding "
            "the adapter (roles: %s)",
            len(discovered),
            ", ".join(sorted(discovered)),
        )
        self._build_adapters()

    def _build_tariffs(self) -> None:
        options = self.options
        self._import_provider = build_provider(self.hass, options, "import")
        self._export_provider = build_provider(self.hass, options, "export")

    @property
    def adapter(self) -> InverterAdapter:
        return self._adapter

    # ------------------------------------------------------------------
    # Specs derived from settings
    # ------------------------------------------------------------------

    @property
    def effective_min_soc(self) -> float:
        """The floor the optimiser plans to, after outage protection.

        Outage protection can only raise this, never lower it, so a user's
        deliberate setting is always respected as a minimum.
        """
        floor = self.settings.min_soc
        if self.settings.outage_protection and self.outage.at_risk:
            floor = max(floor, self.outage.reserve_soc)
        # Never invert the window: a boost above max_soc would make the battery
        # unusable rather than merely conservative.
        return min(floor, self.settings.max_soc - 1.0)

    def nominal_capacity_kwh(self) -> float:
        """Capacity in force: measured if a BMS reports it, else nameplate."""
        return self.battery.capacity_kwh or float(
            self.options.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )

    def usable_kwh(self) -> float:
        """The configured usable window, used to derive the wear allowance.

        Deliberately the *configured* floor rather than the outage-boosted one:
        holding extra charge back for a storm should not change what a cycle is
        reckoned to cost.
        """
        capacity = max(self.nominal_capacity_kwh(), 0.1)
        return capacity * (self.settings.max_soc - self.settings.min_soc) / 100.0

    def wear_estimate(self):
        return self.settings.wear_estimate(self.usable_kwh())

    def battery_spec(self) -> BatterySpec:
        options = self.options
        capacity = self.nominal_capacity_kwh()
        return BatterySpec(
            capacity_kwh=max(capacity, 0.1),
            min_soc=self.effective_min_soc,
            max_soc=self.settings.max_soc,
            max_charge_kw=self.settings.max_charge_kw,
            max_discharge_kw=self.settings.max_discharge_kw,
            charge_efficiency=float(
                options.get(CONF_CHARGE_EFFICIENCY, DEFAULT_CHARGE_EFFICIENCY)
            ),
            discharge_efficiency=float(
                options.get(CONF_DISCHARGE_EFFICIENCY, DEFAULT_DISCHARGE_EFFICIENCY)
            ),
            cycle_cost_per_kwh=self.wear_estimate().cycle_cost,
            reserve_soc=self.settings.reserve_soc,
        )

    def grid_spec(self) -> GridSpec:
        options = self.options
        return GridSpec(
            import_limit_kw=float(
                options.get(CONF_GRID_IMPORT_LIMIT, DEFAULT_GRID_IMPORT_LIMIT)
            ),
            export_limit_kw=float(
                options.get(CONF_GRID_EXPORT_LIMIT, DEFAULT_GRID_EXPORT_LIMIT)
            ),
            allow_export=self.settings.allow_export,
            allow_grid_charge=self.settings.allow_grid_charge,
            allow_battery_export=self.settings.allow_battery_export
            and self.settings.allow_export,
        )

    def optimiser_settings(self) -> OptimiserSettings:
        options = self.options
        return OptimiserSettings(
            soc_levels=int(options.get(CONF_SOC_LEVELS, DEFAULT_SOC_LEVELS)),
            terminal_mode=options.get(
                CONF_TERMINAL_VALUE_MODE, TERMINAL_MODE_HORIZON_MEDIAN
            ),
            terminal_rate=float(options.get(CONF_TERMINAL_VALUE_RATE, 0.0) or 0.0),
        )

    def forecast_confidence(self) -> float:
        """How far the load and solar models have earned the right to be believed.

        The *lower* of the two maturities, because the plan is only as safe as its
        weakest input: a perfect solar forecast is no help if the evening load is
        under-called, and that is exactly the failure that leaves a floored battery
        buying the back half of the evening at the peak rate.
        """
        progress = self.learning_store.model.confidence()
        return min(
            float(progress.get("load_maturity", 0.0) or 0.0),
            float(progress.get("solar_maturity", 0.0) or 0.0),
        )

    def daytime_load_bias_kwh(self, days: float = 7.0) -> tuple[float, int]:
        """Mean signed load-forecast error per daytime slot, and the count.

        Positive means the forecast has been running high. Daytime only, because
        the evening has its own allowance and correcting it here as well would
        apply the same adjustment twice.
        """
        errors: list[float] = []
        for record in self.performance_store.log.window(days):
            local = dt_util.as_local(record.start)
            if is_evening(local.hour) or not record.load_measured:
                continue
            error = record.load_error
            if error is not None:
                errors.append(error)
        if not errors:
            return 0.0, 0
        return sum(errors) / len(errors), len(errors)

    def evening_forecast_error_kwh(self, days: float = 7.0) -> float:
        """How wrong the evening load forecast has been, per evening, in kWh.

        Positive means the forecast has been running *high*. Measured against
        what the plan actually used, so the young-model allowance is included in
        it -- which is the point: what wants answering is "did the evening turn
        out heavier than we planned for", and the allowance is part of what we
        planned for.

        Maturity says how much the model has seen, never whether it was right.
        A house whose evenings it had already learned went on being provisioned
        for three kilowatt-hours it did not use, because nothing fed the outcome
        back into the guess.
        """
        records = self.performance_store.log.window(days)
        errors: list[float] = []
        evenings: set[Any] = set()
        for record in records:
            local = dt_util.as_local(record.start)
            if not is_evening(local.hour) or not record.load_measured:
                continue
            error = record.load_error
            if error is None:
                continue
            errors.append(error)
            evenings.add(local.date())
        if not errors or not evenings:
            return 0.0
        # Per evening rather than per slot: the allowance is a nightly figure.
        return sum(errors) / len(evenings)

    @property
    def horizon_hours(self) -> int:
        return int(self.options.get(CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS))

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        now = dt_util.utcnow()

        self._rediscover_if_blind()
        self.inverter_state = await self._adapter.async_read_state()
        site = self._read_site_state(now)
        self._train_from_samples(now, site)

        if self.settings.strategy == STRATEGY_OFF or not self.settings.enabled:
            # Hand the inverter back to its own logic and stop planning.
            await self._async_release(now)
            self._note_slot_state(now, site)
            return self._build_data(now, site)

        await self._async_refresh_forecasts(now)
        self._read_sessions(now)
        self._assess_outage(now)

        slots, price_note = await self._async_build_horizon(now)

        if not slots:
            self._plan_error = price_note or "no tariff data"
            self.plan = None
        else:
            self._plan_error = None
            self.plan = await self.hass.async_add_executor_job(
                optimise,
                slots,
                site.soc,
                self.battery_spec(),
                self.grid_spec(),
                self.optimiser_settings(),
                now,
            )
            self._note_raised_floor(self.plan)
            await self._async_shift_loads(now, slots, site.soc)

        await self._async_drive_appliances(now)

        command = self._resolve_command(now)
        if command is not None:
            self.last_command = command
            self.last_apply = await self._adapter.async_apply(
                command,
                dry_run=not self.settings.controlling,
                verify=self.settings.controlling,
            )
            self._remember_apply(now, self.last_apply)
            if self.last_apply.writes:
                _LOGGER.info("%s: %s", command.action.value, self.last_apply.summary())

        self._sync_problems()

        self._last_site = site
        self._note_slot_state(now, site)
        return self._build_data(now, site)

    def _hand_back_command(self, reason: str) -> ControlCommand:
        """The instruction that returns the inverter to running itself.

        Self-use, at the user's own emergency reserve rather than the plan's floor,
        with the configured rate ceilings restored. Every field earns its place: a
        hold leaves the reserve raised to the charge it was protecting, an outage
        boost leaves it raised further still, and a forced charge leaves the
        charge-current limit at whatever that slot was throttled to. Each of those
        silently outlives the controller unless it is written back.
        """
        return ControlCommand(
            action=SlotAction.SELF_USE,
            min_soc=self.settings.reserve_soc,
            max_soc=self.settings.max_soc,
            allow_grid_charge=self.settings.allow_grid_charge,
            max_charge_kw=self.settings.max_charge_kw,
            max_discharge_kw=self.settings.max_discharge_kw,
            reason=reason,
        )

    async def _async_hand_back(self, now: datetime) -> None:
        """Return the inverter to self-use, once, while writing is still permitted."""
        try:
            await self._async_release(now)
        except Exception:
            _LOGGER.warning("Could not hand the inverter back to self-use")

    async def _async_release(self, now: datetime) -> None:
        """Return the inverter to self-use and stop controlling it.

        This write is gated on ``may_write`` rather than ``controlling``, which
        is false whenever the optimiser is disabled. Gating on ``controlling``
        would mean switching the optimiser off could never hand the inverter
        back, leaving it stuck in whatever mode was last set -- quite possibly a
        forced charge.
        """
        self.plan = None
        command = self._hand_back_command("controller disabled")
        self.last_command = command
        self.last_apply = await self._adapter.async_apply(
            command,
            dry_run=not self.settings.may_write,
            verify=False,
        )
        self._remember_apply(now, self.last_apply)
        await self._async_drive_appliances(now)

    # ------------------------------------------------------------------
    # Live readings
    # ------------------------------------------------------------------

    def _read_site_state(self, now: datetime) -> SiteState:
        options = self.options
        assert self._battery_source is not None
        self.battery = self._battery_source.read(self.inverter_state)

        pv = power_kw(self.hass, options.get(CONF_PV_POWER_ENTITY))
        if pv is None:
            pv = self.inverter_state.pv_power_kw
        load = power_kw(self.hass, options.get(CONF_LOAD_POWER_ENTITY))
        if load is None:
            load = self.inverter_state.load_power_kw
        grid = power_kw(self.hass, options.get(CONF_GRID_POWER_ENTITY))
        if grid is None:
            grid = self.inverter_state.grid_power_kw

        temperature = _state_float(self.hass, options.get(CONF_OUTDOOR_TEMP_ENTITY))
        if temperature is None and self._weather is not None:
            temperature = self._weather.temperature_at(now)

        soc = self.battery.soc
        return SiteState(
            soc=soc if soc is not None else 50.0,
            timestamp=now,
            pv_power_kw=pv or 0.0,
            load_power_kw=load or 0.0,
            grid_power_kw=grid or 0.0,
            grid_valid=grid is not None,
            battery_power_kw=self.battery.power_kw or 0.0,
            battery_capacity_kwh=self.battery.capacity_kwh,
            battery_soh=self.battery.soh,
            battery_temperature=self.battery.temperature,
            outdoor_temperature=temperature,
            soc_valid=self.battery.valid,
        )

    def _train_from_samples(self, now: datetime, site: SiteState) -> None:
        """Integrate live power into half-hour observations and learn from them.

        The sky signal recorded here must match the one used for prediction, or
        the learned buckets would be keyed one way and looked up another.
        """
        cloud = self._weather.measured_cloud_at(now) if self._weather else None
        uv_index = self._weather.uv_index_at(now) if self._weather else None
        if cloud is None and uv_index is None and self._weather is not None:
            cloud = self._weather.cloud_at(now)
        forecast_kwh = None
        if self._solar_forecast:
            slot_start = now.replace(
                minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES,
                second=0,
                microsecond=0,
            )
            forecast_kwh = self._solar_forecast.energy_between(
                slot_start, slot_start + _SLOT
            )

        completed = self.accumulator.add_sample(
            now,
            site.pv_power_kw,
            site.load_power_kw,
            cloud_cover=cloud,
            temperature=site.outdoor_temperature,
            forecast_kwh=forecast_kwh,
            uv_index=uv_index,
            grid_power_kw=site.grid_power_kw if site.grid_valid else None,
        )
        self._record_completed(completed)

        model = self.learning_store.model
        for slot in completed:
            local = dt_util.as_local(slot.start)
            # Never learn from a signal nobody measured. A slot whose sensor was
            # unavailable throughout reads zero, and zero is indistinguishable
            # from a genuinely idle half-hour -- so an inverter switched off for
            # an afternoon would teach the model that the house uses nothing and
            # the sun does not shine, at whatever time of day the work happened.
            if not slot.load_measured and not slot.pv_measured:
                continue
            if slot.pv_measured:
                model.observe_solar(
                    SolarObservation(
                        month=local.month,
                        hour=local.hour,
                        minute=local.minute,
                        kwh=slot.pv_kwh,
                        cloud_cover=slot.cloud_cover,
                        uv_index=slot.uv_index,
                        forecast_kwh=slot.forecast_kwh,
                    )
                )
            if not slot.load_measured:
                continue
            model.observe_load(
                LoadObservation(
                    hour=local.hour,
                    minute=local.minute,
                    kwh=slot.load_kwh,
                    weekday=local.weekday(),
                    temperature=slot.temperature,
                    month=local.month,
                )
            )
            _LOGGER.debug(
                "Learned slot %s: pv=%.3f kWh load=%.3f kWh cloud=%s uv=%s temp=%s",
                local.isoformat(),
                slot.pv_kwh,
                slot.load_kwh,
                slot.cloud_cover,
                slot.uv_index,
                slot.temperature,
            )
        if completed:
            self.learning_store.async_schedule_save()

    # ------------------------------------------------------------------
    # Performance history
    # ------------------------------------------------------------------

    def _note_slot_state(self, now: datetime, site: SiteState) -> None:
        """Stamp what is true right now against the slot it belongs to.

        Called at the end of the cycle, once the plan has been resolved and
        applied, so the marks describe a decision that was actually made rather
        than one still being computed. The measured energy arrives later, when
        the accumulator closes the slot.
        """
        slot_start = slot_start_for(now)
        mark = self._slot_marks.setdefault(slot_start, {})
        mark.setdefault("soc_start", site.soc if site.soc_valid else None)
        if site.soc_valid:
            mark["soc_end"] = site.soc
        mark["import_price"] = self._import_prices.price_at(slot_start)
        mark["export_price"] = self._export_prices.price_at(slot_start)
        mark["controlling"] = self.settings.controlling
        planned = self.plan.slot_at(now) if self.plan else None
        if planned is not None:
            # First value wins, like soc_start. Overwriting each cycle recorded
            # the plan's *last* view of a half-hour against the action committed
            # at its *start* -- and the commitment exists precisely to ignore the
            # plan changing its mind inside a slot. So the two disagreed by
            # construction, and "plan followed 76%" was measuring churn the
            # controller had deliberately declined to act on.
            mark.setdefault("planned_action", planned.action.value)
        if self.last_command is not None:
            mark["applied_action"] = self.last_command.action.value

        # Capture forecasts for slots that have not started yet, so the error
        # measured later is against a genuine prediction rather than hindsight.
        # Overwriting each cycle leaves the freshest forecast made before the
        # slot began, which is the one the decision was actually taken on.
        if self.plan is not None:
            for slot in self.plan.slots[:6]:
                if slot.start <= slot_start or slot.start != slot_start_for(slot.start):
                    continue
                future = self._slot_marks.setdefault(slot.start, {})
                future["pv_forecast_kwh"] = slot.pv_kwh
                future["load_forecast_kwh"] = slot.load_kwh

        # Marks are only needed until their slot closes; a few hours is ample.
        cutoff = slot_start - timedelta(hours=4)
        for start in [s for s in self._slot_marks if s < cutoff]:
            self._slot_marks.pop(start, None)

    # How far back a slot may reach for the charge it is measuring from.
    #
    # Bridging a gap attributes everything that happened in it to the slot that
    # closes it, which is honest about the energy and vague about the timing --
    # the right way round, since the totals are what the wear allowance and the
    # cycle count are built on. But only for a gap short enough that the timing
    # still means something: after an afternoon offline, dropping half a pack
    # into one half-hour would invent a discharge that never happened there.
    MAX_SOC_BRIDGE = timedelta(hours=1)

    def _last_recorded_soc(self, start: datetime) -> float | None:
        """The charge at the close of the last record, if it is recent enough."""
        records = self.performance_store.log.records
        if not records:
            return None
        last = records[-1]
        if last.soc_end is None or last.start >= start:
            return None
        if start - (last.start + _SLOT) > self.MAX_SOC_BRIDGE:
            return None
        return last.soc_end

    def _live_power(self) -> dict[str, Any]:
        """The four live flows, from wherever they are actually configured.

        Wired to the inverter adapter's own roles first time round, which on a
        real install reported four nulls: those roles were unfilled because the
        site's power sensors are configured separately, in the integration's own
        options. The figures existed, were read every cycle, and the file that
        was meant to show them said nothing on all four counts.
        """
        site = self._last_site
        state = self.inverter_state
        if site is None:
            return state.power_summary()
        # The site reading has already fallen back to the adapter's roles for
        # anything the options do not configure, so it is the better of the two
        # by construction -- except on the grid, where a missing sensor is
        # recorded as zero and flagged, and zero must not be published as fact.
        return {
            "battery_kw": site.battery_power_kw,
            "pv_kw": site.pv_power_kw,
            "grid_kw": site.grid_power_kw if site.grid_valid else state.grid_power_kw,
            "load_kw": site.load_power_kw,
        }

    def why_no_state(self, entity_id: str) -> str:
        """Why a configured entity has no value, in words that suggest a fix.

        "Not found" is true and useless. It reads as "you typed it wrong" or
        "your integration is broken", and a real install spent two days assuming
        the first while the entity sat in the registry, disabled -- with a
        forecast visibly working on the Energy dashboard, because that comes from
        the provider's config entry and not from these sensors at all.

        The registry knows the difference between an entity that was never
        created, one that exists and is switched off, and one whose integration
        has not loaded. Each has a different fix, so each gets said.
        """
        try:
            from homeassistant.helpers import entity_registry as er

            entry = er.async_get(self.hass).async_get(entity_id)
        except Exception:  # pragma: no cover - diagnostics must not fail
            return "not found"
        if entry is None:
            return "no such entity; check the name"
        if entry.disabled_by:
            return f"disabled in Home Assistant (by {entry.disabled_by}); enable it"
        return "registered but reporting nothing; is its integration loaded?"

    def problem_snapshot(self) -> problems.Snapshot:
        """The facts the fault rules are allowed to look at.

        Assembled here rather than reached for by the rules, so every rule can be
        handed a hand-written case in a test instead of a running instance.
        """
        unexplained = self.unexplained_charge()
        missing = [
            f"{entity_id} — {self.why_no_state(entity_id)}"
            for entity_id in _as_list(self.options.get(CONF_SOLAR_FORECAST_ENTITIES))
            if self.hass.states.get(entity_id) is None
        ]
        recent = self.performance_store.log.window(0.25)
        return problems.Snapshot(
            controlling=self.settings.controlling,
            inverter_available=self.inverter_state.available,
            soc_readable=self.battery.valid,
            rejected_roles=dict(self._adapter.rejected_roles()),
            unverified_writes=list(self.last_apply.unverified if self.last_apply else []),
            unfilled_control_roles=[
                role
                for role in self._adapter.describe().get("unfilled_roles", [])
                if role in CONTROL_ROLES
            ],
            disabled_candidates=self.disabled_inverter_controls(),
            missing_forecast_entities=missing,
            unexplained_charge_kwh=sum(row["unexplained_kwh"] for row in unexplained),
            unexplained_charge_cost=sum(row["cost_estimate"] for row in unexplained),
            quiet_load_slots=sum(1 for r in recent if not r.load_measured),
        )

    @callback
    def _sync_problems(self) -> None:
        """Raise what is wrong in the Repairs panel, and clear what is not.

        Every fault the controller found today was already visible in its own
        state and said out loud nowhere: a control it could not find, an inverter
        charging outside the plan, a forecast entity that had stopped existing.
        Repairs is where Home Assistant users already look for "something needs
        attention", so that is where these go.

        Cleared as readily as raised. A fault that lingers after it is fixed
        teaches people to ignore the panel, which costs more than the warning
        was ever worth.

        Nothing in here may take the cycle down with it. This code exists to
        *report* faults, and it is called from the middle of the update that
        plans and drives the battery -- so a bug in a warning stopped the
        controller dead, failed the whole entry, and took the dashboard off the
        sidebar. A house on a 45p tariff needs its battery run correctly far
        more than it needs to be told about a missing sensor, and the one must
        never be able to cost the other.
        """
        from homeassistant.helpers import issue_registry as ir

        try:
            found = problems.detect(self.problem_snapshot())
        except Exception:  # a warning must never stop the control loop
            _LOGGER.exception("Fault detection failed; continuing without it")
            return

        # Nothing is reported until it has been true for several cycles running.
        # A restart brings the integration up before the entities it reads, so
        # the first look round finds no state of charge and no forecast sensor
        # and is right about both, for about half a minute. Raising on that puts
        # errors in front of someone who has done nothing wrong and clears them
        # again unprompted, which teaches people to ignore the panel -- the one
        # thing it cannot afford.
        self._problem_runs = {
            problem.key: self._problem_runs.get(problem.key, 0) + 1 for problem in found
        }
        found = [
            problem
            for problem in found
            if self._problem_runs[problem.key] >= problems.PERSIST_CYCLES
        ]

        wanted = {problem.key for problem in found}
        for problem in found:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                problem.key,
                is_fixable=False,
                severity=(
                    ir.IssueSeverity.ERROR
                    if problem.severity == "error"
                    else ir.IssueSeverity.WARNING
                ),
                translation_key=problem.key,
                translation_placeholders=problem.placeholders or None,
            )
        for issue_id in [key for key in self._raised_problems if key not in wanted]:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._raised_problems = wanted

    def _remember_apply(self, now: datetime, result: ApplyResult) -> None:
        """Keep an apply in the rolling history, timestamped."""
        entry = result.as_dict()
        entry["at"] = now.isoformat()
        self._apply_history.append(entry)

    def unexplained_charge(self, days: float = 2.0) -> list[dict[str, Any]]:
        """Half-hours where the battery gained more than the sun could have given.

        The one check that says "something other than the plan is driving this
        inverter", and it needs nothing the log does not already hold. A real
        install had its self-use grid charging enabled at the inverter, so the
        pack filled from the grid through half-hours the plan had costed as
        self-use; every figure in the file was individually plausible and the
        arithmetic across two of them was not. Working it out by hand settled an
        argument about whether the behaviour was even real, so it should not need
        working out by hand.

        Conservative on purpose. Solar is credited at the full surplus and the
        threshold is well above the quantisation in a whole-percent state of
        charge, so a slot listed here is one where the sun genuinely cannot
        account for what arrived.
        """
        capacity = self.nominal_capacity_kwh()
        if capacity <= 0:
            return []
        found: list[dict[str, Any]] = []
        for record in self.performance_store.log.window(days):
            if record.applied_action in (None, "charge", "discharge"):
                continue
            if record.soc_start is None or record.soc_end is None:
                continue
            gained = (record.soc_end - record.soc_start) / 100.0 * capacity
            from_sun = max(record.pv_kwh - record.load_kwh, 0.0) * CHARGE_EFFICIENCY_HINT
            unexplained = gained - from_sun
            if unexplained <= UNEXPLAINED_CHARGE_KWH or record.grid_import_kwh <= 0.05:
                continue
            found.append(
                {
                    "start": record.start.isoformat(),
                    "planned_action": record.planned_action,
                    "applied_action": record.applied_action,
                    "import_price": round(record.import_price, 3),
                    "sun_could_supply_kwh": round(from_sun, 3),
                    "battery_gained_kwh": round(gained, 3),
                    "unexplained_kwh": round(unexplained, 3),
                    "grid_import_kwh": round(record.grid_import_kwh, 3),
                    "cost_estimate": round(
                        unexplained / CHARGE_EFFICIENCY_HINT * record.import_price, 1
                    ),
                }
            )
        return found

    def slot_coverage(self, days: float = 2.0) -> dict[str, Any]:
        """How much of the window was actually recorded.

        A slot with too few samples is dropped, and a dropped slot teaches the
        model nothing, costs nothing and appears nowhere. Eleven of ninety-eight
        went missing on a real install without a word, which is both a sensor
        problem worth knowing about and an explanation for a model maturing more
        slowly than the calendar suggests.
        """
        records = self.performance_store.log.window(days)
        if not records:
            return {"recorded": 0, "expected": 0, "missing": 0}
        ordered = sorted(records, key=lambda r: r.start)
        span = (ordered[-1].start - ordered[0].start).total_seconds() / 60.0
        expected = int(span // SLOT_MINUTES) + 1
        thin = [r.start.isoformat() for r in ordered if r.coverage < 0.95]
        return {
            "recorded": len(ordered),
            "expected": expected,
            "missing": max(expected - len(ordered), 0),
            "thin_slots": thin[:20],
            "learning_observations": self.learning_store.model.solar_observations,
        }

    def _record_completed(self, completed: list[Any]) -> None:
        """Turn closed half-hours into performance records."""
        if not completed:
            return
        capacity = self.nominal_capacity_kwh()
        for slot in completed:
            mark = self._slot_marks.pop(slot.start, {})
            # A slot that closed without ever being marked -- the first slot
            # after a restart -- would otherwise be recorded at a price of zero
            # and quietly report having cost nothing. The series still covers a
            # half-hour just gone, so ask it.
            import_price = mark.get("import_price")
            if import_price is None:
                import_price = self._import_prices.price_at(slot.start)
            export_price = mark.get("export_price")
            if export_price is None:
                export_price = self._export_prices.price_at(slot.start)
            record = SlotRecord(
                start=slot.start,
                import_price=float(import_price or 0.0),
                export_price=float(export_price or 0.0),
                pv_kwh=slot.pv_kwh,
                load_kwh=slot.load_kwh,
                grid_import_kwh=slot.grid_import_kwh,
                grid_export_kwh=slot.grid_export_kwh,
                grid_measured=slot.grid_measured,
                load_measured=slot.load_measured,
                coverage=slot.coverage,
                pv_forecast_kwh=mark.get("pv_forecast_kwh"),
                load_forecast_kwh=mark.get("load_forecast_kwh"),
                soc_start=mark.get("soc_start"),
                soc_end=mark.get("soc_end"),
                planned_action=mark.get("planned_action"),
                applied_action=mark.get("applied_action"),
                controlling=bool(mark.get("controlling", False)),
            )
            # Battery flow from the SoC change rather than a power sensor: it is
            # the one figure every inverter reports, and over a half-hour the
            # quantisation matters far less than the missing sensor would.
            #
            # Measured from where the battery was when we last looked, not from
            # this slot's own opening mark. The two are usually the same and the
            # difference is not small when they are not: on a real day the marks
            # ran 63 -> 93%, thirty points, but the within-slot deltas summed to
            # seventeen. A slot dropped for poor coverage takes its movement with
            # it, a missing opening mark discards the whole slot, and the marks
            # are sampled a little either side of the boundary so most slots also
            # lost a point at the join. Forty-three per cent of the day's
            # throughput went unrecorded, which is charged to nothing, wears
            # nothing, and makes the round-trip figure meaningless.
            start_soc, end_soc = record.soc_start, record.soc_end
            bridged = self._last_recorded_soc(slot.start)
            if bridged is not None:
                start_soc = bridged
            if start_soc is not None and end_soc is not None and capacity > 0:
                delta = (end_soc - start_soc) / 100.0 * capacity
                if delta >= 0:
                    record.battery_charge_kwh = delta
                else:
                    record.battery_discharge_kwh = -delta
            self.performance_store.log.add(record)
        self._report_cache = {}
        self.performance_store.async_schedule_save()

    def _self_use_shadow(self, records: list[SlotRecord]) -> SelfUseShadow:
        """A self-use battery starting where the window does.

        ``records`` is the window being summarised, not the whole log. Reading
        the starting charge from the full history meant a seven-day report began
        its counterfactual at whatever the battery held two months ago, then
        stepped it through this week's slots -- so the two batteries were never
        even started from the same place, and the difference between them was
        partly just that.
        """
        battery = self.battery_spec()
        start_soc = next(
            (r.soc_start for r in records if r.soc_start is not None), battery.min_soc
        )
        return SelfUseShadow(
            soc=start_soc,
            capacity_kwh=battery.capacity_kwh,
            min_soc=battery.min_soc,
            max_soc=battery.max_soc,
            max_charge_kw=self.settings.max_charge_kw,
            max_discharge_kw=self.settings.max_discharge_kw,
            charge_efficiency=battery.charge_efficiency,
            discharge_efficiency=battery.discharge_efficiency,
        )

    def performance_report(self, days: float = 7.0) -> PerformanceSummary:
        """The metrics for a window, as an object entities can read fields off.

        Memoised until the next slot is recorded: the self-use shadow replays
        every record in the window, and several entities and the diagnostics
        download all ask for the same window on the same refresh.
        """
        if (cached := self._report_cache.get(days)) is not None:
            return cached
        records = self.performance_store.log.window(days)
        report = summarise(
            records,
            cycle_cost=self.wear_estimate().cycle_cost,
            usable_kwh=self.usable_kwh(),
            shadow=self._self_use_shadow(records) if records else None,
        )
        self._report_cache[days] = report
        return report

    def performance_summary(self, days: float = 7.0) -> dict[str, Any]:
        return self.performance_report(days).as_dict()

    def performance_rows(self, days: float = 7.0) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.performance_store.log.window(days)]

    def performance_csv(self, days: float = 7.0) -> str:
        log = self.performance_store.log
        return log.to_csv(log.window(days))

    async def async_write_performance_csv(self, days: float = 7.0) -> str:
        """Write the CSV under the config directory and return its path."""
        directory = self.hass.config.path(DOMAIN)
        filename = f"performance_{self.entry.entry_id[:8]}.csv"
        path = f"{directory}/{filename}"
        payload = self.performance_csv(days)

        def _write() -> None:
            os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(payload)

        await self.hass.async_add_executor_job(_write)
        _LOGGER.info("Wrote %d days of performance history to %s", days, path)
        return path

    async def async_create_dashboard(self) -> str:
        """Rebuild the sidebar dashboard from the current entities, on demand.

        Discards edits deliberately -- this is the button for when the dashboard
        has been broken or emptied and the generated one is wanted back. It also
        writes the YAML out if the sidebar cannot be touched on this Home
        Assistant version.
        """
        from .panel import async_install

        # reseed: the button exists for someone who has broken or emptied the
        # dashboard and wants the generated one back, so it overwrites the stored
        # copy rather than preserving it.
        outcome = await async_install(self.hass, self.entry, reseed=True)
        self.settings.dashboard_created = True
        self.runtime_store.async_schedule_save()
        return outcome

    async def async_clear_performance(self) -> None:
        await self.performance_store.async_clear()
        self._slot_marks = {}
        self._report_cache = {}

    # ------------------------------------------------------------------
    # Forward inputs
    # ------------------------------------------------------------------

    async def _async_refresh_forecasts(self, now: datetime) -> None:
        if (
            self._forecast_fetched is not None
            and now - self._forecast_fetched < FORECAST_REFRESH
        ):
            return
        self._forecast_fetched = now
        self._weather = await self._async_fetch_weather()
        self._solar_forecast = self._fetch_solar_forecast()

    async def _async_fetch_weather(self) -> WeatherSeries | None:
        entity_id = self.options.get(CONF_WEATHER_ENTITY)
        if not entity_id:
            return None

        # weather.get_forecasts is the supported route on modern HA. Older
        # installs only expose a "forecast" attribute, so fall back to that
        # rather than losing the temperature and cloud inputs entirely.
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly", "entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            _LOGGER.debug("weather.get_forecasts unavailable (%s); using attribute", err)
            response = None

        entries: Any = None
        if isinstance(response, dict):
            payload = response.get(entity_id) or {}
            entries = payload.get("forecast")

        if not entries:
            state = self.hass.states.get(entity_id)
            if state is not None:
                entries = state.attributes.get("forecast")

        if not entries:
            _LOGGER.debug("No hourly forecast available from %s", entity_id)
            return None
        return WeatherSeries.from_forecast(entries)

    def _fetch_solar_forecast(self) -> EnergySeries | None:
        """The hourly forecast, plus daily totals for the sensors that publish only
        those, plus a note when a configured sensor gave us nothing.

        Forecast.Solar's "Estimated energy production - today/tomorrow" are plain
        daily totals with no hourly breakdown. They are the sensors a person naturally
        picks, they parsed to nothing, and the estimate fell back to bare geometry
        without a word -- which on a real install put a whole afternoon at 2 kWh while
        the array was busy filling the battery. Both halves of that are fixed here: the
        totals are kept and used, and anything unusable is named.
        """
        raw = self.options.get(CONF_SOLAR_FORECAST_ENTITIES)
        entity_ids = _as_list(raw)
        self._solar_daily_totals = {}
        self._solar_forecast_note = ""
        if not entity_ids:
            return None

        today = dt_util.as_local(dt_util.utcnow()).date()
        attribute_sets = []
        hourly: list[str] = []
        totals: list[str] = []
        unusable: list[str] = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                unusable.append(f"{entity_id} ({self.why_no_state(entity_id)})")
                continue
            if parse_solar_forecast_attributes(state.attributes):
                attribute_sets.append(state.attributes)
                hourly.append(entity_id)
                continue
            # No hourly detail. A plain daily total is still worth having.
            offset = daily_total_offset(entity_id.partition(".")[2])
            value = _as_float(state.state)
            if offset is not None and value is not None and value >= 0:
                self._solar_daily_totals[today + timedelta(days=offset)] = value
                totals.append(entity_id)
            else:
                unusable.append(entity_id)

        if unusable:
            self._solar_forecast_note = (
                f"no usable forecast from {', '.join(unusable)}; "
                f"estimating from the sun's position instead"
            )
        elif totals and not hourly:
            self._solar_forecast_note = (
                f"using daily totals from {', '.join(totals)} to scale an estimate "
                f"from the sun's position; an hourly forecast would be better"
            )

        if not attribute_sets:
            return None
        series = build_forecast_series(attribute_sets)
        return series if series else None

    async def _async_apply_price_forecast(
        self,
        import_series: PriceSeries,
        export_series: PriceSeries,
        now: datetime,
        horizon_end: datetime,
    ) -> int:
        """Fill the unannounced tail from AgilePredict, if the user enabled it.

        Merged *after* the real prices and never over them: ``PriceSeries.merge``
        lets later sources win, so predicted slots are trimmed to start where the
        announced run ends. Getting that wrong would replace a known price with a
        guess, which is the one outcome worse than no forecast at all.

        Returns the number of slots the prediction supplied, and never raises: an
        unreachable third-party service must degrade to persistence, not stop the
        plan.
        """
        if not self.options.get(CONF_AGILE_PREDICT, DEFAULT_AGILE_PREDICT):
            self.price_forecast = None
            return 0

        region = self.options.get(CONF_OCTOPUS_REGION)
        if not region:
            self.price_forecast = {"available": False, "error": "no region configured"}
            return 0

        known_end = import_series.known_until(now) or import_series.end or now
        if known_end >= horizon_end:
            # Everything is announced already. Still worth scoring the forecast
            # against reality, but not worth a request to do it.
            self.price_forecast = {
                "available": True,
                "used": 0,
                "reason": "all announced",
            }
            return 0

        # Even acquiring the session can fail, and an optional price forecast must
        # never be the reason a plan does not happen.
        try:
            session = async_get_clientsession(self.hass)
        except Exception as err:  # degrade, never propagate
            _LOGGER.warning("No HTTP session for AgilePredict (%s)", err)
            self.price_forecast = {
                "available": False,
                "region": region,
                "error": f"no http session: {err}",
                "error_kind": agile_predict.KIND_UNREACHABLE,
            }
            return 0

        state: dict[str, Any] = {"available": True, "region": region}
        added = 0
        try:
            slots = await agile_predict.async_fetch_forecast(
                session,
                region,
                days=agile_predict.days_needed(now, horizon_end),
                until=horizon_end,
            )
        except agile_predict.AgilePredictError as err:
            _LOGGER.warning(
                "AgilePredict unavailable (%s); falling back to persistence", err
            )
            self.price_forecast = {
                "available": False,
                "region": region,
                "error": str(err),
                "error_kind": err.kind,
            }
            return 0

        # Score against the announced window before trimming: that overlap is
        # what says whether the model is any good here, and a large signed bias
        # is how a units or VAT mismatch announces itself.
        state["accuracy"] = agile_predict.compare_with_actual(slots, list(import_series))

        future = [slot for slot in slots if slot.start >= known_end]
        if future:
            import_series.merge(future)
            added = len(future)
        state["used"] = added
        state["until"] = dt_util.as_local(future[-1].end).isoformat() if future else None

        if self.options.get(CONF_AGILE_PREDICT_EXPORT, DEFAULT_AGILE_PREDICT_EXPORT):
            state["export"] = await self._async_forecast_export(
                session, export_series, region, now, horizon_end
            )

        self.price_forecast = state
        return added

    async def _async_forecast_export(
        self,
        session: Any,
        export_series: PriceSeries,
        region: str,
        now: datetime,
        horizon_end: datetime,
    ) -> dict[str, Any]:
        """Same again for Agile Outgoing, which the API serves via ``export=true``.

        Separate and separately switched: plenty of people (including this
        project's first user) are on Agile import with no export tariff at all,
        and predicting a price for energy nobody buys would be noise.
        """
        known_end = export_series.known_until(now) or export_series.end or now
        if known_end >= horizon_end:
            return {"used": 0, "reason": "all announced"}
        try:
            slots = await agile_predict.async_fetch_forecast(
                session,
                region,
                days=agile_predict.days_needed(now, horizon_end),
                export=True,
                until=horizon_end,
            )
        except agile_predict.AgilePredictError as err:
            return {"used": 0, "error": str(err), "error_kind": err.kind}
        future = [slot for slot in slots if slot.start >= known_end]
        if future:
            export_series.merge(future)
        return {"used": len(future)}

    async def _async_build_horizon(
        self, now: datetime
    ) -> tuple[list[HorizonSlot], str | None]:
        """Assemble the priced, forecast horizon the optimiser will plan over."""
        horizon_end = now + timedelta(hours=self.horizon_hours)

        import_series = PriceSeries()
        export_series = PriceSeries()
        note: str | None = None

        if self._import_provider is not None:
            import_series = await self._import_provider.async_fetch(now, horizon_end)
        if self._export_provider is not None:
            export_series = await self._export_provider.async_fetch(now, horizon_end)

        if not import_series:
            return [], "import tariff returned no prices"

        # Agile publishes tomorrow's prices around 16:00, so for most of the day
        # the horizon runs past known data. Two ways to fill the tail, best
        # first: AgilePredict's model, then persistence for whatever it does not
        # reach. Both are flagged so the UI can show where certainty ends.
        predicted = await self._async_apply_price_forecast(
            import_series, export_series, now, horizon_end
        )
        extended = import_series.extend_by_persistence(horizon_end, now)
        parts = []
        if predicted:
            parts.append(f"{predicted} slots predicted")
        if extended:
            parts.append(f"{extended} slots extrapolated")
        if parts:
            note = ", ".join(parts)
        export_series.extend_by_persistence(horizon_end, now)

        # Keep the real tariff before incentives so the UI can show both.
        self._raw_import_prices = import_series

        adjustments = self._session_adjustments()
        self.adjustment_result = apply_adjustments(
            import_series, export_series, adjustments
        )
        import_series = self.adjustment_result.import_series
        export_series = self.adjustment_result.export_series
        if self.adjustment_result.applied:
            reasons = ", ".join(a.reason for a in self.adjustment_result.applied)
            note = f"{note + '; ' if note else ''}{reasons}"

        self._import_prices = import_series
        self._export_prices = export_series

        boundaries = slot_boundaries(now, horizon_end)
        if not boundaries:
            return [], "empty horizon"

        solar = SolarForecaster(
            self.learning_store.model,
            peak_power_kw=float(
                self.options.get(CONF_SOLAR_PEAK_POWER, DEFAULT_SOLAR_PEAK_POWER)
            ),
            # Home Assistant already knows where the house is, so the day-one
            # clear-sky estimate costs the user no extra configuration.
            latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude,
        )
        load = LoadForecaster(
            self.learning_store.model,
            daily_kwh=self.settings.default_daily_load,
            cooling_threshold_c=self.settings.cooling_threshold,
            cooling_kwh_per_degree_hour=self.settings.cooling_rate,
            heating_threshold_c=self.settings.heating_threshold,
            heating_kwh_per_degree_hour=self.settings.heating_rate,
        )

        solar_predictions = solar.predict_series(
            boundaries,
            self._solar_forecast,
            self._weather,
            dt_util.as_local,
            self._solar_daily_totals,
        )
        load_predictions = load.predict_series(
            boundaries, self._weather, dt_util.as_local
        )

        # A young load model under-calls the evening, and the evening is where the
        # dear half-hours are. Provision for more of it until the house has taught
        # the model otherwise -- only the evening, because that is where the error
        # measured on a real install actually was, and only until the evenings
        # themselves say it is no longer needed.
        hours = [dt_util.as_local(start).hour for start, _ in boundaries]
        uplift = evening_uplift(
            hours,
            [demand.kwh for demand in load_predictions],
            self.forecast_confidence(),
            self.evening_forecast_error_kwh(),
        )
        # ...and the other direction, outside the evening. A load forecast that
        # runs high under-states the solar surplus kWh for kWh, so the plan buys
        # from the grid to fill headroom the afternoon's own sun was going to
        # fill -- paying for the electricity and spilling the generation, which
        # is why this one is worth correcting rather than riding out.
        bias, measured = self.daytime_load_bias_kwh()
        trim = daytime_correction(
            hours, [demand.kwh for demand in load_predictions], bias, measured
        )

        slots: list[HorizonSlot] = []
        for index, ((start, end), sun, demand) in enumerate(
            zip(boundaries, solar_predictions, load_predictions, strict=True)
        ):
            import_price = import_series.price_at(start)
            if import_price is None:
                # Never plan a slot we cannot price; truncate instead.
                break
            export_price = export_series.price_at(start) or 0.0
            slots.append(
                HorizonSlot(
                    start=start,
                    end=end,
                    import_price=import_price,
                    export_price=export_price,
                    pv_kwh=sun.kwh,
                    load_kwh=max(demand.kwh + uplift[index] - trim[index], 0.0),
                    # Declared on HorizonSlot from the start and never populated,
                    # which only mattered once there was a forecast worth telling
                    # apart from an announced price.
                    price_is_forecast=_slot_is_forecast(import_series, start),
                )
            )

        # A horizon shorter than the prices available is a silent loss of money, and
        # exactly the kind nobody notices. A real install was planning 24 hours with
        # 48 hours of prices in hand, so it could not see that tomorrow evening was
        # dearer than today's cheap window -- and therefore had no reason to buy into
        # the cheap window. Worth stating plainly wherever anyone might look.
        priced = [s for s in import_series.slots if s.end > now]
        self._horizon_note = describe_horizon_reach(
            slots[-1].end if slots else None,
            priced[-1].end if priced else None,
            now,
        )

        # What the temperature dials contributed, per slot, so a wrong one is
        # visible rather than something to be inferred from an expensive plan.
        # The dials are quoted per degree per hour and a pair set to 2.0 tripled a
        # real forecast; the only clue on any screen was that the numbers were
        # large, which is not a clue.
        self._climate_uplift = {
            start: demand.climate_uplift_kwh
            for (start, _), demand in zip(boundaries, load_predictions, strict=True)
        }
        climate_kwh = sum(self._climate_uplift.values())
        self._climate_note = describe_climate_uplift(
            sum(slot.load_kwh for slot in slots), climate_kwh
        )
        if self._climate_note:
            _LOGGER.warning("Load forecast: %s", self._climate_note)

        self._diagnostics = {
            "solar_sources": [p.source for p in solar_predictions[:8]],
            "load_sources": [p.source for p in load_predictions[:8]],
            "climate_uplift_kwh": round(climate_kwh, 3),
            "evening_allowance_kwh": round(sum(uplift), 3),
            "climate_note": self._climate_note,
        }
        return slots, note

    # ------------------------------------------------------------------
    # Grid incentives, outage risk and flexible loads
    # ------------------------------------------------------------------

    def _read_sessions(self, now: datetime) -> None:
        """Collect supplier incentive windows from the configured entities."""
        self.sessions = []
        if not self.settings.sessions_enabled:
            return

        options = self.options
        default_rate = float(
            options.get(CONF_SAVING_SESSION_RATE, DEFAULT_SAVING_SESSION_RATE) or 0.0
        )
        for entity_ids, kind, rate in (
            (
                _as_list(options.get(CONF_SAVING_SESSION_ENTITIES)),
                KIND_SAVING_SESSION,
                default_rate,
            ),
            (
                _as_list(options.get(CONF_FREE_SESSION_ENTITIES)),
                KIND_FREE_ELECTRICITY,
                None,
            ),
        ):
            for entity_id in entity_ids:
                state = self.hass.states.get(entity_id)
                if state is None:
                    continue
                found = parse_session_events(state.attributes, kind, rate)
                if found:
                    self.sessions.extend(found)

        # Drop windows already behind us so the sensors show what is coming.
        self.sessions = [s for s in self.sessions if s.end > now - timedelta(hours=6)]
        if self.sessions:
            _LOGGER.debug("Known incentive windows: %s", len(self.sessions))

    def _session_adjustments(self) -> list:
        options = self.options
        return adjustments_from_sessions(
            self.sessions,
            only_joined=bool(options.get(CONF_ONLY_JOINED_SESSIONS, True)),
            saving_session_rate=float(
                options.get(CONF_SAVING_SESSION_RATE, DEFAULT_SAVING_SESSION_RATE) or 0.0
            ),
            reward_export=bool(options.get(CONF_SESSION_REWARD_EXPORT, True)),
        )

    def _assess_outage(self, now: datetime) -> None:
        """Work out whether to hold extra charge back for a likely power cut."""
        if not self.settings.outage_protection:
            self.outage = outage_mod.OutageAssessment(
                reserve_soc=self.settings.min_soc, reason="outage protection disabled"
            )
            return

        options = self.options
        risk_entity = options.get(CONF_OUTAGE_RISK_ENTITY)
        risk_on = False
        if risk_entity:
            state = self.hass.states.get(risk_entity)
            risk_on = state is not None and state.state == "on"

        self.outage = outage_mod.assess(
            now,
            base_reserve_soc=self.settings.min_soc,
            wind_series=outage_mod.wind_series_from_weather(self._weather),
            wind_threshold=float(
                options.get(CONF_OUTAGE_WIND_THRESHOLD, DEFAULT_OUTAGE_WIND_THRESHOLD)
                or 0.0
            ),
            wind_high_threshold=float(
                options.get(
                    CONF_OUTAGE_WIND_HIGH_THRESHOLD,
                    DEFAULT_OUTAGE_WIND_HIGH_THRESHOLD,
                )
                or 0.0
            ),
            risk_entity_on=risk_on,
            planned_outages=self._planned_outages(),
            boost_soc=float(
                options.get(CONF_OUTAGE_RESERVE_SOC, DEFAULT_OUTAGE_RESERVE_SOC)
            ),
            high_boost_soc=float(
                options.get(CONF_OUTAGE_HIGH_RESERVE_SOC, DEFAULT_OUTAGE_HIGH_RESERVE_SOC)
            ),
            lookahead=timedelta(
                hours=float(
                    options.get(
                        CONF_OUTAGE_LOOKAHEAD_HOURS, DEFAULT_OUTAGE_LOOKAHEAD_HOURS
                    )
                )
            ),
        )

    def reserve_conflict(self) -> str:
        """When the inverter's own reserve overrides the plan's floor, say so.

        The inverter's reserve is the floor that actually governs: whatever the plan
        intends, the battery will not discharge below it. A real install had it stuck
        at 90% -- written there by an earlier hold, and then refused when the
        controller tried to lower it again -- so the pack sat at 89% all afternoon
        buying 45p electricity while the plan believed it was free to spend down to
        20%. Nothing anywhere compared the two numbers, and they are the two numbers
        that matter.
        """
        actual = self.inverter_state.min_soc
        if actual is None:
            return ""
        floor = self.effective_min_soc
        if actual <= floor + 1.0:
            return ""
        rejected = self._adapter.rejected_roles().get(ROLE_MIN_SOC)
        tail = (
            " The inverter refused to change it, so it has to be set on the inverter "
            "itself."
            if rejected
            else ""
        )
        return (
            f"The inverter will not discharge below {actual:.0f}% -- its own reserve "
            f"setting -- while the plan is working to {floor:.0f}%, so "
            f"{max(actual - floor, 0.0):.0f} points of the pack are unavailable.{tail}"
        )

    @property
    def horizon_reach(self) -> str:
        """Whether the plan is looking as far ahead as its prices allow."""
        return getattr(self, "_horizon_note", "")

    def _planned_outages(self) -> list[tuple[datetime, datetime, str]]:
        """Planned interruptions from a calendar entity's current event.

        Only the calendar's active/next event is available from its state, which
        is enough: a planned outage further out than that does not need action
        yet, and the next refresh will pick it up.
        """
        entity_id = self.options.get(CONF_OUTAGE_CALENDAR)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        start = _parse_attr_dt(state.attributes.get("start_time"))
        end = _parse_attr_dt(state.attributes.get("end_time"))
        if start is None or end is None:
            return []
        summary = str(state.attributes.get("message") or "planned outage")
        # Not every calendar event is a power cut. Without this filter a bin
        # collection became a high-risk planned interruption and held 80% of the
        # pack back all day.
        keywords = outage_mod.parse_keywords(
            self.options.get(
                CONF_OUTAGE_CALENDAR_KEYWORDS, DEFAULT_OUTAGE_CALENDAR_KEYWORDS
            )
        )
        every = bool(
            self.options.get(
                CONF_OUTAGE_CALENDAR_ALL_EVENTS, DEFAULT_OUTAGE_CALENDAR_ALL_EVENTS
            )
        )
        matched = outage_mod.matched_keyword(summary, keywords)
        if not every and matched is None:
            _LOGGER.debug("Ignoring calendar event %r: not about the supply", summary)
            return []
        # Naming the event and the phrase it matched turns "why is this high?"
        # into a one-line answer, which is the question this feature kept raising.
        why = (
            f'{summary} ({entity_id}, matched "{matched}")'
            if matched
            else f"{summary} ({entity_id}, every event treated as an outage)"
        )
        return [(start, end, why)]

    def shiftable_loads(self) -> list:
        return parse_shiftable_loads(self.options.get(CONF_SHIFTABLE_LOADS))

    async def _async_shift_loads(
        self, now: datetime, slots: list[HorizonSlot], start_soc: float
    ) -> None:
        """Place flexible loads, then re-plan the battery around them.

        Two passes: the first places loads against the battery-only plan, the
        second re-places them against a plan that already knows about them. That
        is enough to converge in practice, and each pass is one optimiser run.
        """
        self.placements = []
        if not self.settings.shifting_enabled:
            return
        loads = self.shiftable_loads()
        if not loads:
            return

        grid = self.grid_spec()
        battery = self.battery_spec()
        settings = self.optimiser_settings()

        placements = place_loads(
            loads, slots, self.plan, dt_util.as_local, grid.import_limit_kw
        )
        if not placements:
            return

        for _ in range(2):
            combined = add_placements_to_slots(slots, placements)
            plan = await self.hass.async_add_executor_job(
                optimise, combined, start_soc, battery, grid, settings, now
            )
            revised = place_loads(
                loads, slots, plan, dt_util.as_local, grid.import_limit_kw
            )
            self.plan = plan
            self.placements = placements
            if [p.start for p in revised] == [p.start for p in placements]:
                break
            placements = revised

        if self.plan is not None and self.placements:
            self.plan.reason = (
                f"{self.plan.reason} {describe_placements(self.placements)}."
            )

    # ------------------------------------------------------------------
    # Appliance switching
    # ------------------------------------------------------------------

    async def _async_drive_appliances(self, now: datetime) -> None:
        """Switch scheduled appliances that have a switch to drive.

        Optional by design. Most flexible loads are a dishwasher with a dial and
        an immersion on a timer, and for those the schedule is the whole product:
        it is published, you read it, you press the button. A load only gets
        switched if you gave it an entity *and* armed appliance control.

        Two rules keep this from fighting either the appliance or the plan:

        * Only ever switch off what this integration switched on. A machine you
          started yourself is none of its business.
        * Once a load has been energised, its finish time is committed and a
          later re-plan cannot move it. Without that, a plan that keeps finding
          a slightly cheaper window later would switch a running dishwasher off
          and on again every cycle.
        """
        self._appliance_notes = []
        settings = self.settings

        # Anything whose committed window has ended, or that can no longer be
        # managed, goes back off -- gated on may_write like the inverter release,
        # so dry run really means no writes at all.
        expired = [
            entity_id
            for entity_id, until in self._appliance_until.items()
            if now >= until or not settings.may_switch_appliances
        ]
        for entity_id in sorted(expired):
            if not settings.may_write:
                _LOGGER.warning(
                    "Cannot switch %s off while in advisory mode; leaving it on",
                    entity_id,
                )
                self._appliance_notes.append(f"{entity_id}: left on (advisory mode)")
                continue
            if await self._async_switch_appliance(entity_id, turn_on=False):
                self._appliance_until.pop(entity_id, None)

        if not settings.may_switch_appliances:
            return

        for entity_id, should_run in sorted(
            appliance_targets(self.placements, now).items()
        ):
            if not should_run or entity_id in self._appliance_until:
                continue
            end = max(
                (p.end for p in self.placements if p.switch_entity == entity_id),
                default=None,
            )
            if end is None:
                continue
            if await self._async_switch_appliance(entity_id, turn_on=True):
                self._appliance_until[entity_id] = end

    async def _async_switch_appliance(self, entity_id: str, *, turn_on: bool) -> bool:
        """Call turn_on/turn_off, returning whether it was actually sent."""
        if self.hass.states.get(entity_id) is None:
            _LOGGER.warning("Flexible load points at %s, which does not exist", entity_id)
            self._appliance_notes.append(f"{entity_id}: no such entity")
            return False
        service = "turn_on" if turn_on else "turn_off"
        try:
            # The homeassistant.* services rather than switch.*, so a load can
            # point at an input_boolean, a script or a climate entity just as
            # happily as a smart plug.
            await self.hass.services.async_call(
                "homeassistant",
                service,
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Could not %s %s: %s", service, entity_id, err)
            self._appliance_notes.append(f"{entity_id}: {err}")
            return False
        _LOGGER.info("Flexible load: %s %s", service, entity_id)
        return True

    def appliance_status(self, now: datetime) -> dict[str, Any]:
        """What appliance switching is doing, for entity attributes."""
        with_switch = [p for p in self.placements if p.switch_entity]
        return {
            "armed": self.settings.may_switch_appliances,
            "switchable": [p.name for p in with_switch],
            "advisory_only": [p.name for p in self.placements if not p.switch_entity],
            "switched_on": sorted(self._appliance_until),
            "until": {
                entity_id: until.isoformat()
                for entity_id, until in sorted(self._appliance_until.items())
            },
            "problems": list(self._appliance_notes),
        }

    async def async_recommend_tariffs(self) -> Any:
        """Score candidate tariffs against this system's learned profile.

        Deliberately on demand rather than every cycle: it makes a request per
        candidate, and the answer changes on the timescale of weeks.
        """
        options = self.options
        region = options.get(CONF_OCTOPUS_REGION)
        if not region:
            _LOGGER.warning("Tariff comparison needs an Octopus region")
            return None

        now = dt_util.utcnow()
        await self._async_refresh_forecasts(now)
        template_slots, _ = await self._async_build_horizon(now)
        window = float(
            options.get(CONF_RECOMMEND_WINDOW_HOURS, DEFAULT_RECOMMEND_WINDOW_HOURS)
        )
        template = recommend.build_comparison_template(template_slots, window)
        if not template:
            _LOGGER.warning("No forecast horizon available for tariff comparison")
            return None

        import_codes = _as_list(options.get(CONF_RECOMMEND_IMPORT_PRODUCTS)) or list(
            recommend.DEFAULT_IMPORT_PRODUCTS
        )
        current_code = options.get(CONF_OCTOPUS_IMPORT_PRODUCT)
        if current_code and current_code not in import_codes:
            import_codes.insert(0, current_code)

        candidates = recommend.candidates_from_codes(import_codes, region, "import")
        session = async_get_clientsession(self.hass)
        export_series = self._export_prices
        battery = self.battery_spec()
        grid = self.grid_spec()
        settings = self.optimiser_settings()
        start_soc = self.battery.soc if self.battery.valid else 50.0

        # Fetched first, scored second, because the charge left at the end of the
        # window has to be priced identically for every candidate. Priced from
        # each tariff's own prices -- the default -- a deep-trough tariff credits
        # its own leftover kWh more generously than a flat one, which is a larger
        # effect than the difference being measured: it put Go and Agile 0.22p
        # apart across a day whose troughs differ by 10.6p a kWh.
        scores: list[recommend.TariffScore] = []
        fetched: list[tuple[Any, Any, float]] = []
        for candidate in candidates:
            try:
                prices, standing = await self._async_fetch_candidate(
                    session, candidate, now, window
                )
            except OctopusApiError as err:
                scores.append(
                    recommend.TariffScore(
                        candidate=candidate,
                        optimised_cost=0.0,
                        self_use_cost=0.0,
                        standing_charge=0.0,
                        hours=0.0,
                        slots=0,
                        mean_price=0.0,
                        min_price=0.0,
                        max_price=0.0,
                        error=str(err),
                    )
                )
                continue
            fetched.append((candidate, prices, standing))

        # The rate comes from the tariff the house is on: a stored kWh is worth
        # what it costs to put back, and until a switch happens it goes back at
        # today's prices. Falls back to whatever was fetched if the current
        # tariff is not among the candidates.
        reference = next(
            (p for c, p, _ in fetched if c.product_code == current_code),
            next((p for _, p, _ in fetched), None),
        )
        window_prices = [
            slot.price
            for slot in (reference.slots if reference is not None else [])
            if slot.price is not None
        ]
        settings = recommend.common_terminal_settings(settings, window_prices)

        for candidate, prices, standing in fetched:
            score = await self.hass.async_add_executor_job(
                recommend.score_tariff,
                candidate,
                prices,
                export_series,
                template,
                start_soc,
                battery,
                grid,
                settings,
                standing,
                candidate.product_code == current_code,
            )
            scores.append(score)

        self.recommendation = recommend.Recommendation(
            created=now,
            scores=scores,
            window_hours=sum(s.duration_hours for s in template),
            note=(
                "Scored by running the optimiser on your learned load and solar "
                "profile over the window below, including standing charges. A "
                "short window is a snapshot, not an annual projection."
            ),
        )
        self.async_update_listeners()
        return self.recommendation

    async def _async_fetch_candidate(
        self,
        session: Any,
        candidate: Any,
        now: datetime,
        window_hours: float,
    ) -> tuple[PriceSeries, float]:
        """Fetch one candidate's unit rates and standing charge."""
        from .tariff.octopus import (
            API_BASE,
            async_get_json,
            parse_unit_rates,
        )

        horizon_end = now + timedelta(hours=window_hours)
        base = (
            f"{API_BASE}/products/{candidate.product_code}"
            f"/electricity-tariffs/{candidate.tariff_code}"
        )
        rates = await async_get_json(
            session,
            f"{base}/standard-unit-rates/",
            {
                "period_from": now.isoformat(),
                "period_to": horizon_end.isoformat(),
                "page_size": "1500",
            },
        )
        slots = parse_unit_rates(rates, fallback_end=horizon_end)
        standing = 0.0
        try:
            charges = await async_get_json(
                session, f"{base}/standing-charges/", {"page_size": "10"}
            )
            standing = recommend.parse_standing_charge(charges)
        except OctopusApiError as err:
            # A missing standing charge is a comparison inaccuracy, not a failure.
            _LOGGER.debug("No standing charge for %s: %s", candidate.tariff_code, err)
        return PriceSeries(slots), standing

    def active_session(self, now: datetime | None = None) -> SessionEvent | None:
        return active_session(self.sessions, now or dt_util.utcnow())

    def next_session(self, now: datetime | None = None) -> SessionEvent | None:
        return next_session(self.sessions, now or dt_util.utcnow())

    # ------------------------------------------------------------------
    # Deciding what to do now
    # ------------------------------------------------------------------

    def _resolve_command(self, now: datetime) -> ControlCommand | None:
        """Turn the plan (or an override) into a single instruction for now.

        Precedence: a manual override beats a strategy lock, which beats the
        plan. That ordering matters -- a user forcing a charge should not be
        undone two minutes later by the optimiser.
        """
        base = {
            # The floor the *plan* was built on, not the deeper emergency reserve.
            # The two were different, and the difference was invisible: the
            # optimiser planned never to go below the configured minimum (raised
            # further when an outage looks likely), while the inverter was told it
            # could discharge down to the emergency reserve -- so in every
            # self-use slot the hardware was free to spend energy the plan had
            # already promised to something else, and an outage boost never
            # reached the inverter at all.
            #
            # During a genuine power cut an islanded inverter follows its own
            # backup floor, which is not this setting and not ours to manage.
            "min_soc": self.effective_min_soc,
            "max_soc": self.settings.max_soc,
            "allow_grid_charge": self.settings.allow_grid_charge,
            "max_charge_kw": self.settings.max_charge_kw,
            "max_discharge_kw": self.settings.max_discharge_kw,
            # Zero when export is not permitted, so the switch does something
            # physical. It used to be written as the connection limit regardless,
            # which left "Export: off" as a note to the optimiser and nothing more.
            "export_limit_kw": self._effective_export_limit(),
        }

        if self.override is not None:
            if self.override.active(now):
                return ControlCommand(
                    action=self.override.action,
                    power_kw=self.override.power_kw
                    or self._default_power(self.override.action),
                    target_soc=self.override.target_soc,
                    reason=f"manual override until {self.override.until.isoformat()}",
                    slot_end=self.override.until,
                    **base,
                )
            self.override = None
            # Force a write next cycle so the plan reasserts itself immediately.
            self._adapter.reset_last_applied()

        strategy = self.settings.strategy
        if strategy != STRATEGY_AUTO:
            forced = {
                STRATEGY_SELF_USE: SlotAction.SELF_USE,
                STRATEGY_FORCE_CHARGE: SlotAction.CHARGE,
                STRATEGY_FORCE_DISCHARGE: SlotAction.DISCHARGE,
                STRATEGY_IDLE: SlotAction.IDLE,
            }.get(strategy)
            if forced is not None:
                return ControlCommand(
                    action=forced,
                    power_kw=self._default_power(forced),
                    reason=f"strategy locked to {strategy}",
                    **base,
                )

        if not self.battery.valid:
            # The plan above was built on the 50% ``_read_site_state`` substitutes
            # when the battery cannot be read. That is fine to display -- an
            # advisory install with no state-of-charge sensor still wants its
            # prices and forecasts -- but it must not reach the hardware. Every
            # action the optimiser chooses turns on where the charge actually is:
            # a pack really at 90% would be told to charge, one at 15% told to
            # discharge, and neither could be checked.
            #
            # The tariff comparison already refused to score on the placeholder.
            # The path that writes to the inverter did not, which was the wrong
            # way round. Self-use is the honest answer to not being able to see
            # the battery: the inverter runs its own logic until the reading is
            # back. A deliberate override or strategy lock still wins, above --
            # those are instructions, not inferences.
            return ControlCommand(
                action=SlotAction.SELF_USE,
                reason=(
                    f"battery state of charge unavailable from "
                    f"{self.battery.soc_source}; not acting on a guess"
                ),
                **base,
            )

        if self.plan is None:
            # No plan (no prices, or an error). Self-use is the safe default: it
            # keeps the house running off PV and battery without gambling.
            return ControlCommand(
                action=SlotAction.SELF_USE,
                reason=self._plan_error or "no plan available",
                **base,
            )

        slot = self.plan.slot_at(now)
        if slot is None:
            # The last sliver of a half-hour has no slot of its own: anything
            # under two minutes is folded into the next one rather than planned
            # separately, so for those two minutes the plan starts in the future
            # and nothing covers now. Falling through to the default handed the
            # inverter a fresh self-use instruction at the tail of every slot,
            # undoing whatever the slot had decided seconds before its end. The
            # commitment already holds the answer for this half-hour; a sliver is
            # no reason to change our mind.
            held = self._committed_for(slot_start_for(now))
            if held is not None and self.last_command is not None:
                return replace(self.last_command, action=held)
            return ControlCommand(
                action=SlotAction.SELF_USE, reason="outside plan horizon", **base
            )

        action = self._committed_action(slot)

        # Last line of defence before a hold reaches the hardware.
        #
        # A hold raises the inverter's reserve to the current charge, so for the
        # rest of the half-hour every kilowatt-hour the house draws is bought,
        # whatever it costs. That is worth doing only while the charge is worth
        # more than the grid, and the plan carries the optimiser's own answer to
        # that question. Three separate routes have now published a hold that
        # could not justify itself -- a labelling artefact, the self-use fallback,
        # and a shortfall too small for the level grid to express -- and each time
        # the damage was done here, at the write. So the test is applied here as
        # well as where the plan is built: whatever produced it, a hold that
        # cannot say the charge is worth more than this half-hour does not go out.
        if (
            action is SlotAction.IDLE
            and slot.hold_value is not None
            and slot.hold_value <= slot.import_price
        ):
            action = SlotAction.SELF_USE

        power = (
            slot.charge_power_kw if slot.charge_ac_kwh > 0 else slot.discharge_power_kw
        )
        if power <= 0:
            power = self._default_power(action)

        grid = self.grid_spec()
        return ControlCommand(
            action=action,
            power_kw=power,
            target_soc=slot.soc_end,
            # Only give surplus PV to the grid during a hold if the grid pays for
            # it. On an import-only tariff it is worth nothing exported and
            # something stored, so the hold must let the array keep charging.
            hold_absorbs_solar=not (grid.allow_export and slot.export_price > 0),
            reason=self.plan.reason,
            slot_end=slot.end,
            **base,
        )

    def _note_raised_floor(self, plan: Plan) -> None:
        """Say in the plan's own words when something is holding the pack shut.

        The plan is where people look, and it was the one place that did not know. A
        real install had its floor lifted from 20% to 90% by an outage hold on a
        forecast 42 mph wind, leaving 1.1 kWh of a 22 kWh pack: the evening ran off
        the grid at 45p, the state of charge sat flat, and the reason read
        "grid-charge 0.0 kWh ... Saves 1p vs self-use". The explanation existed, on a
        different entity, which is no use to anyone reading this one.
        """
        boosted = self.effective_min_soc
        configured = self.settings.min_soc
        if boosted <= configured + 0.5:
            return
        why = self.outage.reason or "outage protection"
        # Marked, not merely mentioned. This clause explains an entire day of
        # apparently baffling behaviour -- a flat state of charge and an evening
        # bought at 45p -- and it has to survive being skim-read at the top of a card
        # that is otherwise full of prices.
        plan.reason = (
            f"{OUTAGE_HOLD_MARK} Holding {boosted:.0f}% back for a possible power cut "
            f"({why}), so only {self.battery_spec().usable_kwh:.1f} kWh is available "
            f"to plan with. {plan.reason}"
        )

    def _effective_export_limit(self) -> float:
        """The export ceiling to write: the connection limit, or nothing at all."""
        grid = self.grid_spec()
        return grid.export_limit_kw if grid.allow_export else 0.0

    def _committed_action(self, slot: PlanSlot) -> SlotAction:
        """The action for this half-hour, held steady once it has been applied.

        The plan is rebuilt from live readings every cycle, so the *current*
        slot's action could change several times inside one half-hour -- charge at
        19:31, self-use at 19:46 -- as the load forecast and SoC moved under it.
        That is churn, not intelligence: it puts the inverter through mode changes
        that achieve nothing, and it makes the dashboard contradict what the
        battery is doing.

        A tariff slot is the natural unit of a decision, so the first action
        actually applied inside a slot stands for the rest of it. Later slots are
        free to move as the plan improves, and anything the user does -- an
        override, a strategy lock, a settings change, "Re-plan now" -- clears the
        commitment immediately, because holding a stale decision against an
        explicit instruction would be the same bug in the other direction.

        Keyed on the half-hour, not on ``slot.start``. The horizon's first slot
        deliberately begins at *now* so the remainder of the current half-hour is
        planned rather than ignored, which means its start moves forward every
        cycle -- 13:32, 13:38, 13:43. Comparing literal starts therefore never
        matched, and this guard re-committed from scratch every five minutes: on
        a real install the inverter went charge, solar-only, charge, charge,
        solar-only inside one half-hour, and "charge" means Force Charge, which
        buys from the grid. Hours of apparently unexplained grid charging in
        slots the plan wanted on solar alone were this, doing exactly what it was
        written to prevent.
        """
        key = slot_start_for(slot.start)
        if self._committed is not None:
            start, action = self._committed
            if start == key:
                if action is not slot.action:
                    _LOGGER.debug(
                        "Holding %s for the slot from %s; the plan now prefers %s",
                        action.value,
                        key.isoformat(),
                        slot.action.value,
                    )
                return action
        self._committed = (key, slot.action)
        return slot.action

    def _committed_for(self, half_hour: datetime) -> SlotAction | None:
        """The action already committed to this half-hour, if there is one."""
        if self._committed is None:
            return None
        start, action = self._committed
        return action if start == half_hour else None

    def clear_commitment(self) -> None:
        """Let the next cycle change the current slot's action again."""
        self._committed = None

    def _default_power(self, action: SlotAction) -> float:
        if action is SlotAction.DISCHARGE:
            return self.settings.max_discharge_kw
        if action in (SlotAction.CHARGE, SlotAction.CHARGE_SOLAR_ONLY):
            return self.settings.max_charge_kw
        return 0.0

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def async_set_override(
        self,
        action: SlotAction,
        duration: timedelta,
        power_kw: float | None = None,
        target_soc: float | None = None,
    ) -> None:
        self.override = Override(
            action=action,
            until=dt_util.utcnow() + duration,
            power_kw=power_kw,
            target_soc=target_soc,
        )
        self._adapter.reset_last_applied()
        self.clear_commitment()
        await self.async_request_refresh()

    async def async_clear_override(self) -> None:
        self.override = None
        self._adapter.reset_last_applied()
        self.clear_commitment()
        await self.async_request_refresh()

    async def async_reset_learning(self) -> None:
        await self.learning_store.async_reset()
        self.accumulator.reset()
        await self.async_request_refresh()

    async def async_update_settings(self, **changes: Any) -> None:
        """Apply a runtime settings change and re-plan immediately."""
        # Turning writing off must not leave the inverter wherever the last command
        # put it. "Advisory only" reasonably means "stop touching it", and the
        # previous reading of that was to stop mid-instruction: switch off during a
        # forced charge and the inverter stayed in Manual mode buying electricity,
        # with the controller no longer even claiming responsibility for it. One
        # final write hands it back to self-use, which is the state a person expects
        # an unmanaged inverter to be in.
        was_writing = self.settings.may_write
        if changes.get("dry_run") is True and was_writing:
            await self._async_hand_back(dt_util.utcnow())

        for key, value in changes.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.settings.sanitised()
        self.runtime_store.async_schedule_save()
        # The wear allowance feeds the performance report, so a change to it
        # must not be masked by a cached summary until the next slot closes.
        self._report_cache = {}
        # A permission or limit change can alter the correct action right now.
        self._adapter.reset_last_applied()
        self.clear_commitment()
        await self.async_request_refresh()

    async def async_release_on_unload(self) -> bool:
        """Hand the inverter back to its own logic on the way out.

        Whatever was last written outlives this integration, and every mode the
        controller uses is a mode the inverter will happily sit in for ever:

        * A hold raises the inverter's own reserve to the charge it is protecting,
          so unloading mid-hold leaves a pack refusing to discharge below 94% and
          a house running off the grid with nothing on screen to explain it.
        * A forced charge or discharge leaves it in Manual mode -- and a forced
          charge left there simply carries on buying.
        * Even an ordinary self-use slot writes the *plan's* floor, which an
          outage boost can put far above the reserve the user actually set.

        Disabling the optimiser already hands the inverter back; removing or
        reloading the entry has to as well, from any of those states.

        This used to fire only for a solar-absorbing hold, on the reasoning that an
        ordinary reload should not add a write for the sake of it. The reasoning
        was right; the guard was the wrong way to get it, and it left the three
        cases above unhandled. The adapter already compares every target against
        the entity's current state and plans no write where they match, so an
        install genuinely sitting in self-use at its own reserve still costs
        nothing. Asking the hardware beats inferring from the last command.

        Returns whether the hand-back was sent.
        """
        if self.last_command is None or not self.settings.may_write:
            return False
        try:
            await self._adapter.async_apply(
                self._hand_back_command("releasing control on the way out"),
                dry_run=False,
                verify=False,
            )
        except Exception:
            # Home Assistant may already be tearing the service registry down.
            _LOGGER.warning("Could not hand the inverter back on unload")
            return False
        return True

    async def async_shutdown_store(self) -> None:
        await self.learning_store.async_save()
        await self.runtime_store.async_save()
        await self.performance_store.async_save()

    # ------------------------------------------------------------------
    # Data for entities
    # ------------------------------------------------------------------

    def _build_data(self, now: datetime, site: SiteState) -> dict[str, Any]:
        plan = self.plan
        current = plan.slot_at(now) if plan else None
        upcoming = plan.next_change_after(now) if plan else None

        return {
            "updated": now,
            "site": site,
            "plan": plan,
            "current_slot": current,
            "next_change": upcoming,
            "command": self.last_command,
            "apply": self.last_apply,
            "inverter": self.inverter_state,
            "battery": self.battery,
            "settings": self.settings,
            "error": self._plan_error,
            "learning": self.learning_store.model.confidence(),
            "sessions": self.sessions,
            "outage": self.outage,
            "placements": self.placements,
        }

    def confidence_note(self) -> str:
        """One line on how much the forecasts are being trusted right now."""
        return describe_confidence(
            self.forecast_confidence(), self.evening_forecast_error_kwh()
        )

    def solar_learning(self) -> dict[str, Any]:
        """How the learned correction is treating the solar forecast right now.

        Reported for the next daylight slot rather than for "now", because at
        22:00 the correction for a midnight slot says nothing useful and the
        question a user is asking is always about the next generating period.
        """
        model = self.learning_store.model
        now = dt_util.utcnow()
        candidates = [
            slot
            for slot in (self.plan.slots if self.plan else [])
            if slot.start >= now and slot.pv_kwh > 0.01
        ]
        target = candidates[0].start if candidates else now
        local = dt_util.as_local(target)
        described = model.describe_solar_correction(
            month=local.month, hour=local.hour, minute=local.minute
        )
        described["for_slot"] = local.isoformat()
        described["forecast_mapped"] = bool(self._solar_forecast)
        return described

    def forecast_totals(self) -> dict[str, Any]:
        """Solar and load totals per local day across the plan horizon.

        "Today" is necessarily the *remainder* of today, since the horizon starts
        now -- reporting it as a whole-day figure would be misleading. Coverage
        hours are included so a user can see when tomorrow is only partly in view.
        """
        totals: dict[str, dict[str, float]] = {}
        if self.plan is None:
            return {"days": totals}

        for slot in self.plan.slots:
            key = dt_util.as_local(slot.start).date().isoformat()
            day = totals.setdefault(
                key,
                {
                    "solar_kwh": 0.0,
                    "load_kwh": 0.0,
                    "hours": 0.0,
                    "climate_uplift_kwh": 0.0,
                },
            )
            day["solar_kwh"] += slot.pv_kwh
            day["load_kwh"] += slot.load_kwh
            day["hours"] += slot.duration_hours
            # How much of that load is the temperature dials rather than the
            # house. A figure that rivals load_kwh means the dials are wrong.
            day["climate_uplift_kwh"] += self._climate_uplift.get(slot.start, 0.0)

        today = dt_util.as_local(dt_util.utcnow()).date()
        tomorrow = today + timedelta(days=1)
        return {
            "days": {
                key: {k: round(v, 3) for k, v in value.items()}
                for key, value in sorted(totals.items())
            },
            "today": totals.get(today.isoformat()),
            "tomorrow": totals.get(tomorrow.isoformat()),
        }

    def cheapest_slots(self, count: int = 6) -> list[dict[str, Any]]:
        """The cheapest upcoming import slots, for dashboards and automations."""
        now = dt_util.utcnow()
        upcoming = [s for s in self._import_prices.slots if s.end > now]
        cheapest = sorted(upcoming, key=lambda s: s.price)[:count]
        return [
            {
                "start": dt_util.as_local(s.start).isoformat(),
                "price": round(s.price, 3),
                "forecast": s.is_forecast,
            }
            for s in sorted(cheapest, key=lambda s: s.start)
        ]

    def price_now(self, direction: str = "import") -> float | None:
        series = self._import_prices if direction == "import" else self._export_prices
        return series.price_at(dt_util.utcnow())

    def price_stats(self, direction: str = "import") -> dict[str, Any]:
        """Min/max/mean of the priced horizon, plus where real data ends."""
        now = dt_util.utcnow()
        series = self._import_prices if direction == "import" else self._export_prices
        upcoming = [s for s in series.slots if s.end > now]
        if not upcoming:
            return {}
        prices = [s.price for s in upcoming]
        known_until = series.known_until(now)
        return {
            "min": round(min(prices), 3),
            "max": round(max(prices), 3),
            "mean": round(sum(prices) / len(prices), 3),
            "slots": len(upcoming),
            "known_until": (
                dt_util.as_local(known_until).isoformat() if known_until else None
            ),
            "extrapolated_slots": sum(1 for s in upcoming if s.is_forecast),
            # Only on import, because that is the only direction the plan pays
            # for and the only one the forecast is scored against.
            "price_forecast": self.price_forecast if direction == "import" else None,
        }

    def price_percentile(
        self, direction: str = "import", fraction: float = CHEAP_SLOT_FRACTION
    ) -> float | None:
        """The price at ``fraction`` of the way up the remaining horizon.

        A *rank* on the slots, not a position in the price range. Those are very
        different on a peaky tariff: one 58p evening spike stretches the range so
        far that a third of the way up it lands at 31p, and two thirds of an Agile
        day come out "cheap" -- which is no use at all for deciding when to run a
        dishwasher. A third of the *slots* is the cheapest eight hours of a
        day, whatever shape the prices are.
        """
        series = self._import_prices if direction == "import" else self._export_prices
        now = dt_util.utcnow()
        prices = [s.price for s in series.slots if s.end > now]
        if not prices:
            return None
        return percentile(prices, fraction)

    def disabled_inverter_controls(self) -> list[str]:
        """Inverter controls that exist but are switched off in Home Assistant.

        The SolaX Modbus integration ships a great many of its ``number`` and
        ``switch`` entities disabled by default, to keep a Modbus device from adding
        two hundred entities to a house. A disabled entity has no state, and
        discovery reads states -- so a control the inverter plainly has, and whose
        value is visible in SolaX's own app, is simply invisible here.

        That is what happened to "charge from grid" and the minimum-SoC reserve: the
        plan believed it had no way to set them, when in truth they were one tick
        box away. Worth naming precisely, because the fix is on the user's side and
        takes seconds once you know which entities to enable.

        Entities that fail the configured prefix are *marked* rather than dropped,
        for the same reason the candidate list marks them: a wrong prefix is the
        other reason a control goes unbound, and filtering this list by that same
        prefix guarantees it cannot say so.
        """
        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self.hass)
        except Exception:  # pragma: no cover - diagnostics must not fail
            return []

        from .inverter.roles import CANDIDATE_WORDS, _is_our_own

        raw = str(self.options.get(CONF_INVERTER_PREFIX) or "")
        prefix = raw.strip().lower().replace(" ", "_").replace("-", "_")
        found: list[str] = []
        for entry in registry.entities.values():
            if entry.disabled_by is None or entry.domain not in (
                "number",
                "switch",
                "select",
            ):
                continue
            object_id = entry.entity_id.partition(".")[2].lower()
            if _is_our_own(object_id):
                continue
            if not any(word in object_id for word in CANDIDATE_WORDS):
                continue
            if prefix and not object_id.startswith(prefix):
                found.append(f"{entry.entity_id} (does not match the prefix '{raw}')")
                continue
            found.append(entry.entity_id)
        return sorted(found)[:60]

    def diagnostics(self) -> dict[str, Any]:
        """Everything needed to debug a plan, for the diagnostics download."""
        return {
            "disabled_inverter_controls": self.disabled_inverter_controls(),
            "horizon_reach": self.horizon_reach,
            "solar_forecast_note": self._solar_forecast_note,
            "climate_note": self._climate_note,
            "reserve_conflict": self.reserve_conflict(),
            "rejected_writes": self._adapter.rejected_roles(),
            "settings": self.settings.as_dict(),
            "battery_spec": {
                "capacity_kwh": self.battery_spec().capacity_kwh,
                "usable_kwh": round(self.battery_spec().usable_kwh, 3),
                "min_soc": self.battery_spec().min_soc,
                "max_soc": self.battery_spec().max_soc,
                "round_trip_efficiency": round(
                    self.battery_spec().round_trip_efficiency, 4
                ),
                "spread_needed_to_cycle": round(
                    self.battery_spec().spread_needed_to_cycle(), 3
                ),
                "wear": self.wear_estimate().as_dict(),
            },
            "grid": {
                "import_limit_kw": self.grid_spec().import_limit_kw,
                "export_limit_kw": self.grid_spec().export_limit_kw,
                "allow_export": self.grid_spec().allow_export,
                "allow_grid_charge": self.grid_spec().allow_grid_charge,
            },
            "adapter": self._adapter.describe(),
            "battery_source": (
                self._battery_source.describe() if self._battery_source else {}
            ),
            "battery_reading": self.battery.as_dict(),
            "inverter_state": self.inverter_state.as_dict(),
            "import_tariff": (
                self._import_provider.describe() if self._import_provider else {}
            ),
            "export_tariff": (
                self._export_provider.describe() if self._export_provider else {}
            ),
            "import_prices": self._import_prices.as_dict_list()[:96],
            "export_prices": self._export_prices.as_dict_list()[:96],
            "weather": self._weather.describe() if self._weather else {},
            "solar_forecast": (
                self._solar_forecast.describe() if self._solar_forecast else {}
            ),
            "learning": self.learning_store.model.confidence(),
            "forecast_sources": self._diagnostics,
            "plan": self.plan.as_dict() if self.plan else None,
            "last_command": self.last_command.as_dict() if self.last_command else None,
            "last_apply": self.last_apply.as_dict() if self.last_apply else None,
            "override": self.override.as_dict() if self.override else None,
            "error": self._plan_error,
            "effective_min_soc": self.effective_min_soc,
            "sessions": [s.as_dict() for s in self.sessions],
            "adjustments": (
                self.adjustment_result.as_dict() if self.adjustment_result else {}
            ),
            "outage": self.outage.as_dict(),
            "shiftable_loads": [load.as_dict() for load in self.shiftable_loads()],
            "placements": [p.as_dict() for p in self.placements],
            "raw_import_prices": self._raw_import_prices.as_dict_list()[:96],
            "recommendation": (
                self.recommendation.as_dict() if self.recommendation else None
            ),
            "apply_history": list(self._apply_history),
            "live_power": self._live_power(),
            "unexplained_charge": self.unexplained_charge(),
            "slot_coverage": self.slot_coverage(),
        }


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def _as_float(value: Any) -> float | None:
    """A number from an entity state, or None for "unknown" and friends."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_attr_dt(value: Any) -> datetime | None:
    """Parse a datetime from an entity attribute, which may already be one."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    for text in (value, value.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Calendar attributes are local wall-clock without an offset.
        return parsed if parsed.tzinfo else dt_util.as_utc(parsed)
    return None


def _state_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None
