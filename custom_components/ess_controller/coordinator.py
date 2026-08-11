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
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import outage as outage_mod
from . import recommend
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
from .forecast.energy import EnergySeries
from .forecast.load import LoadForecaster
from .forecast.solar import SolarForecaster, build_forecast_series
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
        self.last_command: ControlCommand | None = None
        self.inverter_state = InverterState()
        self.battery: BatteryReading = BatteryReading()

        self._adapter: InverterAdapter = NullAdapter(hass)
        self._battery_source: BatterySource | None = None
        self._import_provider = None
        self._export_provider = None
        self._weather: WeatherSeries | None = None
        self._solar_forecast: EnergySeries | None = None
        self._forecast_fetched: datetime | None = None
        self._import_prices = PriceSeries()
        self._export_prices = PriceSeries()
        self._diagnostics: dict[str, Any] = {}
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
            if self.last_apply.writes:
                _LOGGER.info("%s: %s", command.action.value, self.last_apply.summary())

        self._note_slot_state(now, site)
        return self._build_data(now, site)

    async def _async_release(self, now: datetime) -> None:
        """Return the inverter to self-use and stop controlling it.

        This write is gated on ``may_write`` rather than ``controlling``, which
        is false whenever the optimiser is disabled. Gating on ``controlling``
        would mean switching the optimiser off could never hand the inverter
        back, leaving it stuck in whatever mode was last set -- quite possibly a
        forced charge.
        """
        self.plan = None
        command = ControlCommand(
            action=SlotAction.SELF_USE,
            min_soc=self.settings.reserve_soc,
            max_soc=self.settings.max_soc,
            allow_grid_charge=self.settings.allow_grid_charge,
            reason="controller disabled",
        )
        self.last_command = command
        self.last_apply = await self._adapter.async_apply(
            command,
            dry_run=not self.settings.may_write,
            verify=False,
        )
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
            mark["planned_action"] = planned.action.value
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
            start_soc, end_soc = record.soc_start, record.soc_end
            if start_soc is not None and end_soc is not None and capacity > 0:
                delta = (end_soc - start_soc) / 100.0 * capacity
                if delta >= 0:
                    record.battery_charge_kwh = delta
                else:
                    record.battery_discharge_kwh = -delta
            self.performance_store.log.add(record)
        self._report_cache = {}
        self.performance_store.async_schedule_save()

    def _self_use_shadow(self) -> SelfUseShadow:
        """A self-use battery starting where the history does."""
        battery = self.battery_spec()
        records = self.performance_store.log.records
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
            shadow=self._self_use_shadow() if records else None,
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
        raw = self.options.get(CONF_SOLAR_FORECAST_ENTITIES)
        entity_ids = _as_list(raw)
        if not entity_ids:
            return None
        attribute_sets = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            attribute_sets.append(state.attributes)
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
            boundaries, self._solar_forecast, self._weather, dt_util.as_local
        )
        load_predictions = load.predict_series(
            boundaries, self._weather, dt_util.as_local
        )

        slots: list[HorizonSlot] = []
        for (start, end), sun, demand in zip(
            boundaries, solar_predictions, load_predictions, strict=True
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
                    load_kwh=demand.kwh,
                    # Declared on HorizonSlot from the start and never populated,
                    # which only mattered once there was a forecast worth telling
                    # apart from an announced price.
                    price_is_forecast=_slot_is_forecast(import_series, start),
                )
            )

        self._diagnostics = {
            "solar_sources": [p.source for p in solar_predictions[:8]],
            "load_sources": [p.source for p in load_predictions[:8]],
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

        scores: list[recommend.TariffScore] = []
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
            return ControlCommand(
                action=SlotAction.SELF_USE, reason="outside plan horizon", **base
            )

        action = self._committed_action(slot)

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
        """
        if self._committed is not None:
            start, action = self._committed
            if start == slot.start:
                if action is not slot.action:
                    _LOGGER.debug(
                        "Holding %s for the slot from %s; the plan now prefers %s",
                        action.value,
                        slot.start.isoformat(),
                        slot.action.value,
                    )
                return action
        self._committed = (slot.start, slot.action)
        return slot.action

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

    async def async_restore_reserve(self) -> bool:
        """Put the inverter's reserve back where the user set it, on the way out.

        A hold is expressed by raising the inverter's own reserve to the state of
        charge it is holding, which is what lets the array keep charging. That
        setting outlives this integration: unload the entry mid-hold and the
        inverter is left refusing to discharge below 94%, running the house off
        the grid for ever with nothing on screen to explain it. Disabling the
        optimiser already hands the inverter back; removing or reloading the entry
        has to as well.

        Narrow on purpose: it writes only when a hold actually raised the floor, so
        an ordinary reload does not add a write for the sake of it. Returns whether
        anything was written.
        """
        command = self.last_command
        if command is None or command.action is not SlotAction.IDLE:
            return False
        if not command.hold_absorbs_solar or not self.settings.may_write:
            return False
        try:
            await self._adapter.async_apply(
                ControlCommand(
                    action=SlotAction.SELF_USE,
                    min_soc=self.settings.reserve_soc,
                    max_soc=self.settings.max_soc,
                    allow_grid_charge=self.settings.allow_grid_charge,
                    reason="releasing the hold on the way out",
                ),
                dry_run=False,
                verify=False,
            )
        except Exception:
            # Home Assistant may already be tearing the service registry down.
            _LOGGER.warning("Could not restore the inverter reserve on unload")
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
                key, {"solar_kwh": 0.0, "load_kwh": 0.0, "hours": 0.0}
            )
            day["solar_kwh"] += slot.pv_kwh
            day["load_kwh"] += slot.load_kwh
            day["hours"] += slot.duration_hours

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

    def diagnostics(self) -> dict[str, Any]:
        """Everything needed to debug a plan, for the diagnostics download."""
        return {
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
        }


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


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
