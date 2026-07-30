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
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ADAPTER_GENERIC,
    ADAPTER_SOLAX_MODBUS,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_BATTERY_HEALTH_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_ENTITY_MAP,
    CONF_GRID_EXPORT_LIMIT,
    CONF_GRID_IMPORT_LIMIT,
    CONF_GRID_POWER_ENTITY,
    CONF_HORIZON_HOURS,
    CONF_INVERTER_ADAPTER,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_POWER_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_SOC_LEVELS,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_PEAK_POWER,
    CONF_TERMINAL_VALUE_MODE,
    CONF_TERMINAL_VALUE_RATE,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_GRID_IMPORT_LIMIT,
    DEFAULT_HORIZON_HOURS,
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
    TERMINAL_MODE_HORIZON_MEAN,
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
    SiteState,
    SlotAction,
)
from .optimiser.dp import OptimiserSettings, optimise
from .runtime import RuntimeSettings, RuntimeStore
from .sampling import SlotAccumulator, slot_boundaries
from .tariff.base import PriceSeries
from .tariff.factory import build_provider

_LOGGER = logging.getLogger(__name__)

_SLOT = timedelta(minutes=SLOT_MINUTES)

# Weather and solar forecasts change slowly; refetching every cycle would spam
# the upstream integration for no benefit.
FORECAST_REFRESH = timedelta(minutes=20)


class EssCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Plans and applies battery dispatch."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.entry = entry
        self.learning_store = LearningStore(hass, entry.entry_id)
        self.runtime_store = RuntimeStore(hass, entry.entry_id)
        self.settings: RuntimeSettings = RuntimeSettings()
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
        self._build_adapters()
        self._build_tariffs()

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

    def battery_spec(self) -> BatterySpec:
        options = self.options
        capacity = self.battery.capacity_kwh or float(
            options.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY)
        )
        return BatterySpec(
            capacity_kwh=max(capacity, 0.1),
            min_soc=self.settings.min_soc,
            max_soc=self.settings.max_soc,
            max_charge_kw=self.settings.max_charge_kw,
            max_discharge_kw=self.settings.max_discharge_kw,
            charge_efficiency=float(
                options.get(CONF_CHARGE_EFFICIENCY, DEFAULT_CHARGE_EFFICIENCY)
            ),
            discharge_efficiency=float(
                options.get(CONF_DISCHARGE_EFFICIENCY, DEFAULT_DISCHARGE_EFFICIENCY)
            ),
            cycle_cost_per_kwh=self.settings.cycle_cost,
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
                CONF_TERMINAL_VALUE_MODE, TERMINAL_MODE_HORIZON_MEAN
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

        self.inverter_state = await self._adapter.async_read_state()
        site = self._read_site_state(now)
        self._train_from_samples(now, site)

        if self.settings.strategy == STRATEGY_OFF or not self.settings.enabled:
            # Hand the inverter back to its own logic and stop planning.
            await self._async_release(now)
            return self._build_data(now, site)

        await self._async_refresh_forecasts(now)
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
        )

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
        # the horizon runs past known data. Extrapolate the remainder from the
        # same time of day, flagged so the UI can show where certainty ends.
        extended = import_series.extend_by_persistence(horizon_end, now)
        if extended:
            note = f"{extended} slots extrapolated"
        export_series.extend_by_persistence(horizon_end, now)

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
                )
            )

        self._diagnostics = {
            "solar_sources": [p.source for p in solar_predictions[:8]],
            "load_sources": [p.source for p in load_predictions[:8]],
        }
        return slots, note

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
            "min_soc": self.settings.reserve_soc,
            "max_soc": self.settings.max_soc,
            "allow_grid_charge": self.settings.allow_grid_charge,
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

        power = (
            slot.charge_power_kw if slot.charge_ac_kwh > 0 else slot.discharge_power_kw
        )
        if power <= 0:
            power = self._default_power(slot.action)

        return ControlCommand(
            action=slot.action,
            power_kw=power,
            target_soc=slot.soc_end,
            export_limit_kw=self.grid_spec().export_limit_kw,
            reason=self.plan.reason,
            slot_end=slot.end,
            **base,
        )

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
        await self.async_request_refresh()

    async def async_clear_override(self) -> None:
        self.override = None
        self._adapter.reset_last_applied()
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
        # A permission or limit change can alter the correct action right now.
        self._adapter.reset_last_applied()
        await self.async_request_refresh()

    async def async_shutdown_store(self) -> None:
        await self.learning_store.async_save()
        await self.runtime_store.async_save()

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
        }

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
        }

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
        }


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


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
