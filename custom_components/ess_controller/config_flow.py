"""Config and options flow.

Setup is split into steps that match how a user thinks about their system --
battery, inverter, tariff, forecasting, optimiser -- rather than presenting one
enormous form. The options flow re-uses the same step handlers through a menu so
any section can be revisited without walking the whole flow again.

Defaults suit a small domestic system, but every value is editable so the
integration fits any size of battery, array or inverter.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ADAPTER_SOLAX_MODBUS,
    ADAPTERS,
    CONF_ALLOW_BATTERY_EXPORT,
    CONF_ALLOW_EXPORT,
    CONF_ALLOW_GRID_CHARGE,
    CONF_APPLIANCE_CONTROL,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_CAPACITY_ENTITY,
    CONF_BATTERY_COST,
    CONF_BATTERY_EXPECTED_CYCLES,
    CONF_BATTERY_HEALTH_ENTITY,
    CONF_BATTERY_MAX_SOC,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_RESERVE_SOC,
    CONF_BATTERY_RESIDUAL_VALUE,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_TEMPERATURE_ENTITY,
    CONF_CHARGE_EFFICIENCY,
    CONF_CURRENCY,
    CONF_CYCLE_COST,
    CONF_DEFAULT_DAILY_LOAD,
    CONF_DERIVE_WEAR_FROM_COST,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DRY_RUN,
    CONF_ENTITY_MAP,
    CONF_EXPORT_FIXED_RATE,
    CONF_EXPORT_PRICE_SCALE,
    CONF_EXPORT_PROVIDER,
    CONF_EXPORT_RATE_ENTITY,
    CONF_EXPORT_TOU_SCHEDULE,
    CONF_FREE_SESSION_ENTITIES,
    CONF_GRID_EXPORT_LIMIT,
    CONF_GRID_IMPORT_LIMIT,
    CONF_GRID_POWER_ENTITY,
    CONF_HORIZON_HOURS,
    CONF_IMPORT_FIXED_RATE,
    CONF_IMPORT_PRICE_SCALE,
    CONF_IMPORT_PROVIDER,
    CONF_IMPORT_RATE_ENTITY,
    CONF_IMPORT_TOU_SCHEDULE,
    CONF_INVERTER_ADAPTER,
    CONF_INVERTER_PREFIX,
    CONF_LOAD_POWER_ENTITY,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_OCTOPUS_ACCOUNT,
    CONF_OCTOPUS_API_KEY,
    CONF_OCTOPUS_EXPORT_PRODUCT,
    CONF_OCTOPUS_EXPORT_TARIFF,
    CONF_OCTOPUS_IMPORT_PRODUCT,
    CONF_OCTOPUS_IMPORT_TARIFF,
    CONF_OCTOPUS_REGION,
    CONF_ONLY_JOINED_SESSIONS,
    CONF_OUTAGE_CALENDAR,
    CONF_OUTAGE_ENABLED,
    CONF_OUTAGE_HIGH_RESERVE_SOC,
    CONF_OUTAGE_LOOKAHEAD_HOURS,
    CONF_OUTAGE_RESERVE_SOC,
    CONF_OUTAGE_RISK_ENTITY,
    CONF_OUTAGE_WIND_HIGH_THRESHOLD,
    CONF_OUTAGE_WIND_THRESHOLD,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PV_POWER_ENTITY,
    CONF_RECOMMEND_ENABLED,
    CONF_RECOMMEND_EXPORT_PRODUCTS,
    CONF_RECOMMEND_IMPORT_PRODUCTS,
    CONF_RECOMMEND_WINDOW_HOURS,
    CONF_SAVING_SESSION_ENTITIES,
    CONF_SAVING_SESSION_RATE,
    CONF_SESSION_REWARD_EXPORT,
    CONF_SESSIONS_ENABLED,
    CONF_SHIFTABLE_LOADS,
    CONF_SHIFTING_ENABLED,
    CONF_SOC_LEVELS,
    CONF_SOLAR_FORECAST_ENTITIES,
    CONF_SOLAR_PEAK_POWER,
    CONF_TERMINAL_VALUE_MODE,
    CONF_TERMINAL_VALUE_RATE,
    CONF_WEATHER_ENTITY,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_BATTERY_COST,
    DEFAULT_BATTERY_EXPECTED_CYCLES,
    DEFAULT_BATTERY_RESIDUAL_VALUE,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_CURRENCY,
    DEFAULT_CYCLE_COST,
    DEFAULT_DAILY_LOAD,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_EXPORT_FIXED_RATE,
    DEFAULT_GRID_EXPORT_LIMIT,
    DEFAULT_GRID_IMPORT_LIMIT,
    DEFAULT_HORIZON_HOURS,
    DEFAULT_IMPORT_FIXED_RATE,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MAX_DISCHARGE_POWER,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    DEFAULT_OUTAGE_HIGH_RESERVE_SOC,
    DEFAULT_OUTAGE_LOOKAHEAD_HOURS,
    DEFAULT_OUTAGE_RESERVE_SOC,
    DEFAULT_OUTAGE_WIND_HIGH_THRESHOLD,
    DEFAULT_OUTAGE_WIND_THRESHOLD,
    DEFAULT_RECOMMEND_WINDOW_HOURS,
    DEFAULT_RESERVE_SOC,
    DEFAULT_SAVING_SESSION_RATE,
    DEFAULT_SOC_LEVELS,
    DEFAULT_SOLAR_PEAK_POWER,
    DOMAIN,
    EXPORT_PROVIDERS,
    IMPORT_PROVIDERS,
    NAME,
    PRICE_SCALE_AUTO,
    PRICE_SCALES,
    PROVIDER_ENTITY,
    PROVIDER_FIXED,
    PROVIDER_NONE,
    PROVIDER_OCTOPUS_API,
    PROVIDER_OCTOPUS_ENTITY,
    PROVIDER_TOU,
    TERMINAL_MODE_FIXED,
    TERMINAL_MODE_HORIZON_MEAN,
    TERMINAL_MODE_ZERO,
)
from .inverter.roles import (
    ROLE_BATTERY_VOLTAGE,
    ROLE_CHARGE_LIMIT,
    ROLE_DISCHARGE_LIMIT,
    ROLE_EXPORT_LIMIT,
    ROLE_GRID_CHARGE,
    ROLE_LOCK,
    ROLE_MANUAL_MODE,
    ROLE_MIN_SOC,
    ROLE_SOC,
    ROLE_USE_MODE,
    SOLAX_ROLE_SPECS,
    discover_entities,
)
from .tariff.octopus import (
    REGIONS,
    OctopusApiError,
    async_discover_tariffs,
    async_validate_tariff,
    build_tariff_code,
)

_LOGGER = logging.getLogger(__name__)

# Roles offered for manual override in the UI. The measurement-only roles are
# left to auto-discovery to keep the form to a reasonable size.
MAPPABLE_ROLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ROLE_USE_MODE, ("select",)),
    (ROLE_MANUAL_MODE, ("select",)),
    (ROLE_CHARGE_LIMIT, ("number",)),
    (ROLE_DISCHARGE_LIMIT, ("number",)),
    (ROLE_MIN_SOC, ("number",)),
    (ROLE_EXPORT_LIMIT, ("number",)),
    (ROLE_LOCK, ("select",)),
    (ROLE_GRID_CHARGE, ("switch", "select")),
    (ROLE_SOC, ("sensor",)),
    (ROLE_BATTERY_VOLTAGE, ("sensor",)),
)


def _number(
    minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _entity(
    domains: list[str] | str,
    device_classes: list[str] | None = None,
    multiple: bool = False,
) -> selector.EntitySelector:
    config: dict[str, Any] = {"domain": domains, "multiple": multiple}
    if device_classes:
        config["device_class"] = device_classes
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _options(values: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=values,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=key,
        )
    )


def _suggest(current: dict[str, Any], key: str, default: Any) -> Any:
    """An optional field pre-filled with the current value, or a default."""
    return vol.Optional(key, description={"suggested_value": current.get(key, default)})


class EssFlowMixin:
    """Step schemas shared between the config flow and the options flow."""

    def _battery_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(
                    current,
                    CONF_BATTERY_CAPACITY,
                    DEFAULT_BATTERY_CAPACITY,
                ): _number(0.5, 500, 0.1, "kWh"),
                _suggest(current, CONF_BATTERY_SOC_ENTITY, None): _entity(
                    ["sensor", "number", "input_number"]
                ),
                _suggest(current, CONF_BATTERY_CAPACITY_ENTITY, None): _entity(
                    ["sensor"]
                ),
                _suggest(current, CONF_BATTERY_HEALTH_ENTITY, None): _entity(["sensor"]),
                _suggest(current, CONF_BATTERY_TEMPERATURE_ENTITY, None): _entity(
                    ["sensor"]
                ),
                _suggest(
                    current, "derate_capacity_by_soh", False
                ): selector.BooleanSelector(),
                _suggest(current, CONF_BATTERY_MIN_SOC, DEFAULT_MIN_SOC): _number(
                    0, 99, 1, "%"
                ),
                _suggest(current, CONF_BATTERY_MAX_SOC, DEFAULT_MAX_SOC): _number(
                    1, 100, 1, "%"
                ),
                _suggest(
                    current,
                    CONF_BATTERY_RESERVE_SOC,
                    DEFAULT_RESERVE_SOC,
                ): _number(0, 99, 1, "%"),
                _suggest(
                    current,
                    CONF_MAX_CHARGE_POWER,
                    DEFAULT_MAX_CHARGE_POWER,
                ): _number(0.1, 50, 0.1, "kW"),
                _suggest(
                    current,
                    CONF_MAX_DISCHARGE_POWER,
                    DEFAULT_MAX_DISCHARGE_POWER,
                ): _number(0.1, 50, 0.1, "kW"),
                _suggest(
                    current,
                    CONF_CHARGE_EFFICIENCY,
                    DEFAULT_CHARGE_EFFICIENCY,
                ): _number(0.5, 1.0, 0.01),
                _suggest(
                    current,
                    CONF_DISCHARGE_EFFICIENCY,
                    DEFAULT_DISCHARGE_EFFICIENCY,
                ): _number(0.5, 1.0, 0.01),
                _suggest(
                    current, CONF_DERIVE_WEAR_FROM_COST, False
                ): selector.BooleanSelector(),
                _suggest(current, CONF_BATTERY_COST, DEFAULT_BATTERY_COST): _number(
                    0, 100000, 10
                ),
                _suggest(
                    current,
                    CONF_BATTERY_EXPECTED_CYCLES,
                    DEFAULT_BATTERY_EXPECTED_CYCLES,
                ): _number(0, 20000, 50),
                _suggest(
                    current,
                    CONF_BATTERY_RESIDUAL_VALUE,
                    DEFAULT_BATTERY_RESIDUAL_VALUE,
                ): _number(0, 100000, 10),
                _suggest(current, CONF_CYCLE_COST, DEFAULT_CYCLE_COST): _number(
                    0, 50, 0.5, "p/kWh"
                ),
            }
        )

    def _inverter_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(
                    current,
                    CONF_INVERTER_ADAPTER,
                    ADAPTER_SOLAX_MODBUS,
                ): _options(list(ADAPTERS), "adapter"),
                _suggest(current, CONF_INVERTER_PREFIX, "solax"): selector.TextSelector(),
                _suggest(current, "nominal_voltage", 360.0): _number(12, 1000, 1, "V"),
                _suggest(
                    current, "manage_export_limit", False
                ): selector.BooleanSelector(),
                _suggest(
                    current,
                    CONF_GRID_IMPORT_LIMIT,
                    DEFAULT_GRID_IMPORT_LIMIT,
                ): _number(0.5, 200, 0.1, "kW"),
                _suggest(
                    current,
                    CONF_GRID_EXPORT_LIMIT,
                    DEFAULT_GRID_EXPORT_LIMIT,
                ): _number(0, 200, 0.01, "kW"),
            }
        )

    def _tariff_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_IMPORT_PROVIDER, PROVIDER_FIXED): _options(
                    list(IMPORT_PROVIDERS), "provider"
                ),
                _suggest(current, CONF_EXPORT_PROVIDER, PROVIDER_NONE): _options(
                    list(EXPORT_PROVIDERS), "provider"
                ),
                _suggest(
                    current, CONF_CURRENCY, DEFAULT_CURRENCY
                ): selector.TextSelector(),
            }
        )

    def _octopus_schema(self, current: dict[str, Any], direction: str) -> dict[Any, Any]:
        if direction == "import":
            product_key, tariff_key = (
                CONF_OCTOPUS_IMPORT_PRODUCT,
                CONF_OCTOPUS_IMPORT_TARIFF,
            )
            product_default = "AGILE-24-10-01"
        else:
            product_key, tariff_key = (
                CONF_OCTOPUS_EXPORT_PRODUCT,
                CONF_OCTOPUS_EXPORT_TARIFF,
            )
            product_default = "OUTGOING-AGILE-24-10-01"
        return {
            _suggest(current, product_key, product_default): selector.TextSelector(),
            _suggest(current, CONF_OCTOPUS_REGION, "C"): _options(
                list(REGIONS), "region"
            ),
            _suggest(current, tariff_key, None): selector.TextSelector(),
            _suggest(current, CONF_OCTOPUS_API_KEY, None): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            _suggest(current, CONF_OCTOPUS_ACCOUNT, None): selector.TextSelector(),
        }

    def _direction_schema(self, current: dict[str, Any], direction: str) -> vol.Schema:
        is_import = direction == "import"
        provider = current.get(
            CONF_IMPORT_PROVIDER if is_import else CONF_EXPORT_PROVIDER,
            PROVIDER_FIXED if is_import else PROVIDER_NONE,
        )
        fields: dict[Any, Any] = {}

        if provider in (PROVIDER_ENTITY, PROVIDER_OCTOPUS_ENTITY):
            key = CONF_IMPORT_RATE_ENTITY if is_import else CONF_EXPORT_RATE_ENTITY
            fields[_suggest(current, key, None)] = _entity(
                ["sensor", "event"], multiple=True
            )
        elif provider == PROVIDER_OCTOPUS_API:
            fields.update(self._octopus_schema(current, direction))
        elif provider == PROVIDER_TOU:
            key = CONF_IMPORT_TOU_SCHEDULE if is_import else CONF_EXPORT_TOU_SCHEDULE
            fields[_suggest(current, key, "00:30-04:30=9.5")] = selector.TextSelector()

        rate_key = CONF_IMPORT_FIXED_RATE if is_import else CONF_EXPORT_FIXED_RATE
        rate_default = (
            DEFAULT_IMPORT_FIXED_RATE if is_import else DEFAULT_EXPORT_FIXED_RATE
        )
        fields[_suggest(current, rate_key, rate_default)] = _number(
            -100, 200, 0.1, "p/kWh"
        )
        scale_key = CONF_IMPORT_PRICE_SCALE if is_import else CONF_EXPORT_PRICE_SCALE
        fields[_suggest(current, scale_key, PRICE_SCALE_AUTO)] = _options(
            list(PRICE_SCALES), "price_scale"
        )
        return vol.Schema(fields)

    def _forecast_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_SOLAR_FORECAST_ENTITIES, None): _entity(
                    ["sensor"], multiple=True
                ),
                _suggest(
                    current,
                    CONF_SOLAR_PEAK_POWER,
                    DEFAULT_SOLAR_PEAK_POWER,
                ): _number(0.1, 100, 0.1, "kW"),
                _suggest(current, CONF_WEATHER_ENTITY, None): _entity(["weather"]),
                _suggest(current, CONF_OUTDOOR_TEMP_ENTITY, None): _entity(["sensor"]),
                _suggest(current, CONF_PV_POWER_ENTITY, None): _entity(["sensor"]),
                _suggest(current, CONF_LOAD_POWER_ENTITY, None): _entity(["sensor"]),
                _suggest(current, CONF_GRID_POWER_ENTITY, None): _entity(["sensor"]),
                _suggest(current, CONF_DEFAULT_DAILY_LOAD, DEFAULT_DAILY_LOAD): _number(
                    0.5, 200, 0.5, "kWh"
                ),
            }
        )

    def _sessions_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_SESSIONS_ENABLED, True): (
                    selector.BooleanSelector()
                ),
                _suggest(current, CONF_SAVING_SESSION_ENTITIES, None): _entity(
                    ["event", "binary_sensor", "sensor"], multiple=True
                ),
                _suggest(current, CONF_FREE_SESSION_ENTITIES, None): _entity(
                    ["event", "binary_sensor", "sensor"], multiple=True
                ),
                _suggest(
                    current, CONF_SAVING_SESSION_RATE, DEFAULT_SAVING_SESSION_RATE
                ): _number(0, 1000, 1, "p/kWh"),
                _suggest(current, CONF_ONLY_JOINED_SESSIONS, True): (
                    selector.BooleanSelector()
                ),
                _suggest(current, CONF_SESSION_REWARD_EXPORT, True): (
                    selector.BooleanSelector()
                ),
            }
        )

    def _outage_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_OUTAGE_ENABLED, False): (
                    selector.BooleanSelector()
                ),
                _suggest(current, CONF_OUTAGE_RISK_ENTITY, None): _entity(
                    ["binary_sensor", "input_boolean"]
                ),
                _suggest(current, CONF_OUTAGE_CALENDAR, None): _entity(["calendar"]),
                _suggest(
                    current, CONF_OUTAGE_WIND_THRESHOLD, DEFAULT_OUTAGE_WIND_THRESHOLD
                ): _number(0, 250, 1),
                _suggest(
                    current,
                    CONF_OUTAGE_WIND_HIGH_THRESHOLD,
                    DEFAULT_OUTAGE_WIND_HIGH_THRESHOLD,
                ): _number(0, 250, 1),
                _suggest(
                    current, CONF_OUTAGE_RESERVE_SOC, DEFAULT_OUTAGE_RESERVE_SOC
                ): _number(0, 100, 1, "%"),
                _suggest(
                    current,
                    CONF_OUTAGE_HIGH_RESERVE_SOC,
                    DEFAULT_OUTAGE_HIGH_RESERVE_SOC,
                ): _number(0, 100, 1, "%"),
                _suggest(
                    current,
                    CONF_OUTAGE_LOOKAHEAD_HOURS,
                    DEFAULT_OUTAGE_LOOKAHEAD_HOURS,
                ): _number(1, 48, 1, "h"),
            }
        )

    def _shifting_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_SHIFTING_ENABLED, False): (
                    selector.BooleanSelector()
                ),
                _suggest(current, CONF_SHIFTABLE_LOADS, None): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                _suggest(current, CONF_APPLIANCE_CONTROL, False): (
                    selector.BooleanSelector()
                ),
            }
        )

    def _recommend_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_RECOMMEND_ENABLED, True): (
                    selector.BooleanSelector()
                ),
                _suggest(
                    current, CONF_RECOMMEND_IMPORT_PRODUCTS, None
                ): selector.TextSelector(),
                _suggest(
                    current, CONF_RECOMMEND_EXPORT_PRODUCTS, None
                ): selector.TextSelector(),
                _suggest(
                    current,
                    CONF_RECOMMEND_WINDOW_HOURS,
                    DEFAULT_RECOMMEND_WINDOW_HOURS,
                ): _number(6, 48, 1, "h"),
            }
        )

    def _optimiser_schema(self, current: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                _suggest(current, CONF_HORIZON_HOURS, DEFAULT_HORIZON_HOURS): _number(
                    6, 48, 1, "h"
                ),
                _suggest(current, CONF_SOC_LEVELS, DEFAULT_SOC_LEVELS): _number(
                    20, 150, 5
                ),
                _suggest(
                    current,
                    CONF_TERMINAL_VALUE_MODE,
                    TERMINAL_MODE_HORIZON_MEAN,
                ): _options(
                    [
                        TERMINAL_MODE_HORIZON_MEAN,
                        TERMINAL_MODE_FIXED,
                        TERMINAL_MODE_ZERO,
                    ],
                    "terminal_mode",
                ),
                _suggest(current, CONF_TERMINAL_VALUE_RATE, 0.0): _number(
                    0, 200, 0.5, "p/kWh"
                ),
                _suggest(
                    current, CONF_ALLOW_GRID_CHARGE, True
                ): selector.BooleanSelector(),
                _suggest(current, CONF_ALLOW_EXPORT, True): selector.BooleanSelector(),
                _suggest(
                    current, CONF_ALLOW_BATTERY_EXPORT, True
                ): selector.BooleanSelector(),
                _suggest(current, CONF_DRY_RUN, True): selector.BooleanSelector(),
            }
        )

    def _entity_map_schema(self, current: dict[str, Any]) -> vol.Schema:
        existing = dict(current.get(CONF_ENTITY_MAP) or {})
        discovered = discover_entities(
            self.hass, SOLAX_ROLE_SPECS, current.get(CONF_INVERTER_PREFIX)
        )
        fields: dict[Any, Any] = {}
        for role, domains in MAPPABLE_ROLES:
            suggestion = existing.get(role, discovered.get(role))
            fields[vol.Optional(role, description={"suggested_value": suggestion})] = (
                _entity(list(domains))
            )
        return vol.Schema(fields)

    async def _async_validate_tariff(
        self, data: dict[str, Any], direction: str
    ) -> dict[str, str]:
        """Check an Octopus tariff resolves before accepting the configuration.

        Catching a typo here is far kinder than letting the integration set up
        and then silently fail to plan because no prices ever arrive.
        """
        is_import = direction == "import"
        provider = data.get(CONF_IMPORT_PROVIDER if is_import else CONF_EXPORT_PROVIDER)
        if provider != PROVIDER_OCTOPUS_API:
            return {}

        product_key = (
            CONF_OCTOPUS_IMPORT_PRODUCT if is_import else CONF_OCTOPUS_EXPORT_PRODUCT
        )
        tariff_key = (
            CONF_OCTOPUS_IMPORT_TARIFF if is_import else CONF_OCTOPUS_EXPORT_TARIFF
        )
        tariff_code = data.get(tariff_key)
        product = data.get(product_key)
        region = data.get(CONF_OCTOPUS_REGION)

        session = async_get_clientsession(self.hass)

        # An account number plus API key is the friendliest route: look the
        # tariff codes up rather than making the user find them.
        account = data.get(CONF_OCTOPUS_ACCOUNT)
        api_key = data.get(CONF_OCTOPUS_API_KEY)
        if not tariff_code and account and api_key:
            try:
                found = await async_discover_tariffs(session, api_key, account)
            except OctopusApiError as err:
                _LOGGER.warning("Octopus account lookup failed: %s", err)
                return {"base": "octopus_account_failed"}
            tariff_code = found.get(direction)
            if tariff_code:
                data[tariff_key] = tariff_code

        if not tariff_code and product and region:
            try:
                tariff_code = build_tariff_code(product, region, direction)
            except ValueError:
                return {CONF_OCTOPUS_REGION: "invalid_region"}
            data[tariff_key] = tariff_code

        if not tariff_code:
            return {"base": "octopus_tariff_required"}

        try:
            count = await async_validate_tariff(session, tariff_code, product)
        except OctopusApiError as err:
            _LOGGER.warning("Octopus tariff validation failed: %s", err)
            return {"base": "octopus_unreachable"}
        if count <= 0:
            return {"base": "octopus_no_rates"}
        return {}


class EssConfigFlow(EssFlowMixin, ConfigFlow, domain=DOMAIN):
    """Guided setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_inverter()
        return self.async_show_form(
            step_id="user", data_schema=self._battery_schema(self._data)
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_tariff()

        adapter = self._data.get(CONF_INVERTER_ADAPTER, ADAPTER_SOLAX_MODBUS)
        discovered = discover_entities(self.hass, SOLAX_ROLE_SPECS, "solax")
        return self.async_show_form(
            step_id="inverter",
            data_schema=self._inverter_schema(self._data),
            description_placeholders={
                "discovered": (
                    ", ".join(sorted(discovered.values())) or "none found yet"
                ),
                "adapter": adapter,
            },
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_tariff_import()
        return self.async_show_form(
            step_id="tariff", data_schema=self._tariff_schema(self._data)
        )

    async def async_step_tariff_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = dict(self._data)
            candidate.update(_clean(user_input))
            errors = await self._async_validate_tariff(candidate, "import")
            if not errors:
                self._data = candidate
                return await self.async_step_tariff_export()
        return self.async_show_form(
            step_id="tariff_import",
            data_schema=self._direction_schema(self._data, "import"),
            errors=errors,
        )

    async def async_step_tariff_export(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._data.get(CONF_EXPORT_PROVIDER, PROVIDER_NONE) == PROVIDER_NONE:
            # No export tariff configured, which is a perfectly normal setup.
            return await self.async_step_forecast()

        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = dict(self._data)
            candidate.update(_clean(user_input))
            errors = await self._async_validate_tariff(candidate, "export")
            if not errors:
                self._data = candidate
                return await self.async_step_forecast()
        return self.async_show_form(
            step_id="tariff_export",
            data_schema=self._direction_schema(self._data, "export"),
            errors=errors,
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_sessions()
        return self.async_show_form(
            step_id="forecast", data_schema=self._forecast_schema(self._data)
        )

    async def async_step_sessions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_shifting()
        return self.async_show_form(
            step_id="sessions", data_schema=self._sessions_schema(self._data)
        )

    async def async_step_shifting(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_outage()
        return self.async_show_form(
            step_id="shifting", data_schema=self._shifting_schema(self._data)
        )

    async def async_step_outage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            return await self.async_step_optimiser()
        return self.async_show_form(
            step_id="outage", data_schema=self._outage_schema(self._data)
        )

    async def async_step_optimiser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(_clean(user_input))
            await self.async_set_unique_id(
                f"{DOMAIN}_{self._data.get(CONF_INVERTER_PREFIX, 'default')}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=NAME, data=self._data)
        return self.async_show_form(
            step_id="optimiser", data_schema=self._optimiser_schema(self._data)
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> EssOptionsFlow:
        return EssOptionsFlow()


class EssOptionsFlow(EssFlowMixin, OptionsFlow):
    """Revisit any section of the configuration from a menu."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @property
    def current(self) -> dict[str, Any]:
        if not self._data:
            merged = dict(self.config_entry.data)
            merged.update(self.config_entry.options)
            self._data = merged
        return self._data

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "battery",
                "inverter",
                "entity_map",
                "tariff",
                "tariff_import",
                "tariff_export",
                "forecast",
                "sessions",
                "shifting",
                "outage",
                "recommend",
                "optimiser",
            ],
        )

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(title="", data=self.current)

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="battery", data_schema=self._battery_schema(self.current)
        )

    async def async_step_inverter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        discovered = discover_entities(
            self.hass, SOLAX_ROLE_SPECS, self.current.get(CONF_INVERTER_PREFIX)
        )
        return self.async_show_form(
            step_id="inverter",
            data_schema=self._inverter_schema(self.current),
            description_placeholders={
                "discovered": ", ".join(sorted(discovered.values())) or "none found",
                "adapter": self.current.get(CONF_INVERTER_ADAPTER, ADAPTER_SOLAX_MODBUS),
            },
        )

    async def async_step_entity_map(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Only store roles the user actually set, so auto-discovery keeps
            # working for the rest even if the upstream entity is renamed.
            mapping = {
                role: value
                for role, value in user_input.items()
                if value not in (None, "")
            }
            self.current[CONF_ENTITY_MAP] = mapping
            return self._save()
        return self.async_show_form(
            step_id="entity_map", data_schema=self._entity_map_schema(self.current)
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="tariff", data_schema=self._tariff_schema(self.current)
        )

    async def async_step_tariff_import(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = dict(self.current)
            candidate.update(_clean(user_input))
            errors = await self._async_validate_tariff(candidate, "import")
            if not errors:
                self._data = candidate
                return self._save()
        return self.async_show_form(
            step_id="tariff_import",
            data_schema=self._direction_schema(self.current, "import"),
            errors=errors,
        )

    async def async_step_tariff_export(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = dict(self.current)
            candidate.update(_clean(user_input))
            errors = await self._async_validate_tariff(candidate, "export")
            if not errors:
                self._data = candidate
                return self._save()
        return self.async_show_form(
            step_id="tariff_export",
            data_schema=self._direction_schema(self.current, "export"),
            errors=errors,
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="forecast", data_schema=self._forecast_schema(self.current)
        )

    async def async_step_sessions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="sessions", data_schema=self._sessions_schema(self.current)
        )

    async def async_step_shifting(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # A cleared load list must actually clear, so this field bypasses the
            # empty-means-unset rule that protects the other optional fields.
            self.current[CONF_SHIFTABLE_LOADS] = user_input.get(CONF_SHIFTABLE_LOADS, "")
            self.current[CONF_APPLIANCE_CONTROL] = bool(
                user_input.get(CONF_APPLIANCE_CONTROL, False)
            )
            self.current[CONF_SHIFTING_ENABLED] = bool(
                user_input.get(CONF_SHIFTING_ENABLED, False)
            )
            return self._save()
        return self.async_show_form(
            step_id="shifting", data_schema=self._shifting_schema(self.current)
        )

    async def async_step_outage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            self.current[CONF_OUTAGE_ENABLED] = bool(
                user_input.get(CONF_OUTAGE_ENABLED, False)
            )
            return self._save()
        return self.async_show_form(
            step_id="outage", data_schema=self._outage_schema(self.current)
        )

    async def async_step_recommend(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="recommend", data_schema=self._recommend_schema(self.current)
        )

    async def async_step_optimiser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.current.update(_clean(user_input))
            return self._save()
        return self.async_show_form(
            step_id="optimiser", data_schema=self._optimiser_schema(self.current)
        )


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields so they fall back to defaults.

    A blank text box should mean "not configured", not the empty string, which
    would otherwise be stored and later treated as a real entity ID.
    """
    return {
        key: value
        for key, value in user_input.items()
        if value is not None and value != ""
    }
