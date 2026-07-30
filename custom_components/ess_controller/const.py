"""Constants for the AI ESS Controller integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ess_controller"
NAME: Final = "AI ESS Controller"
VERSION: Final = "0.1.0"
MANUFACTURER: Final = "AI ESS Controller"

PLATFORMS: Final = [
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "select",
    "button",
]

# How often the coordinator wakes up. The optimiser itself is cheap, but we
# re-plan on every cycle so that a change in SoC, load or price is reflected
# quickly. Half-hour slot boundaries are handled separately.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)

STORAGE_VERSION: Final = 1
STORAGE_KEY_LEARNING: Final = f"{DOMAIN}.learning"
STORAGE_KEY_RUNTIME: Final = f"{DOMAIN}.runtime"

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------

# Battery
CONF_BATTERY_CAPACITY: Final = "battery_capacity_kwh"
CONF_BATTERY_SOC_ENTITY: Final = "battery_soc_entity"
CONF_BATTERY_MIN_SOC: Final = "battery_min_soc"
CONF_BATTERY_MAX_SOC: Final = "battery_max_soc"
CONF_BATTERY_RESERVE_SOC: Final = "battery_reserve_soc"
CONF_MAX_CHARGE_POWER: Final = "max_charge_power_kw"
CONF_MAX_DISCHARGE_POWER: Final = "max_discharge_power_kw"
CONF_CHARGE_EFFICIENCY: Final = "charge_efficiency"
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"
CONF_CYCLE_COST: Final = "cycle_cost_per_kwh"
CONF_BATTERY_HEALTH_ENTITY: Final = "battery_health_entity"
CONF_BATTERY_CAPACITY_ENTITY: Final = "battery_capacity_entity"
CONF_BATTERY_TEMPERATURE_ENTITY: Final = "battery_temperature_entity"

# Inverter / control
CONF_INVERTER_ADAPTER: Final = "inverter_adapter"
CONF_INVERTER_PREFIX: Final = "inverter_entity_prefix"
CONF_ENTITY_MAP: Final = "entity_map"
CONF_GRID_EXPORT_LIMIT: Final = "grid_export_limit_kw"
CONF_GRID_IMPORT_LIMIT: Final = "grid_import_limit_kw"

# Site measurement entities
CONF_PV_POWER_ENTITY: Final = "pv_power_entity"
CONF_PV_ENERGY_ENTITY: Final = "pv_energy_entity"
CONF_LOAD_POWER_ENTITY: Final = "load_power_entity"
CONF_LOAD_ENERGY_ENTITY: Final = "load_energy_entity"
CONF_GRID_POWER_ENTITY: Final = "grid_power_entity"

# Forecast sources
CONF_SOLAR_FORECAST_ENTITIES: Final = "solar_forecast_entities"
CONF_SOLAR_FORECAST_TODAY: Final = "solar_forecast_today_entity"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_OUTDOOR_TEMP_ENTITY: Final = "outdoor_temp_entity"
CONF_SOLAR_PEAK_POWER: Final = "solar_peak_power_kw"
# Used only until the load model has learned enough to speak for itself.
CONF_DEFAULT_DAILY_LOAD: Final = "default_daily_load_kwh"

# Tariff
CONF_IMPORT_PROVIDER: Final = "import_provider"
CONF_EXPORT_PROVIDER: Final = "export_provider"
CONF_IMPORT_RATE_ENTITY: Final = "import_rate_entity"
CONF_EXPORT_RATE_ENTITY: Final = "export_rate_entity"
CONF_IMPORT_FIXED_RATE: Final = "import_fixed_rate"
CONF_EXPORT_FIXED_RATE: Final = "export_fixed_rate"
CONF_IMPORT_TOU_SCHEDULE: Final = "import_tou_schedule"
CONF_EXPORT_TOU_SCHEDULE: Final = "export_tou_schedule"
CONF_STANDING_CHARGE: Final = "standing_charge"
CONF_CURRENCY: Final = "currency"
# Suppliers publish rates in either major units (£0.2345/kWh) or minor units
# (23.45p/kWh). "auto" sniffs the magnitude; set explicitly if it guesses wrong.
CONF_IMPORT_PRICE_SCALE: Final = "import_price_scale"
CONF_EXPORT_PRICE_SCALE: Final = "export_price_scale"

PRICE_SCALE_AUTO: Final = "auto"
PRICE_SCALE_MINOR: Final = "minor"
PRICE_SCALE_MAJOR: Final = "major"
PRICE_SCALES: Final = [PRICE_SCALE_AUTO, PRICE_SCALE_MINOR, PRICE_SCALE_MAJOR]

# Octopus API
CONF_OCTOPUS_IMPORT_PRODUCT: Final = "octopus_import_product"
CONF_OCTOPUS_IMPORT_TARIFF: Final = "octopus_import_tariff"
CONF_OCTOPUS_EXPORT_PRODUCT: Final = "octopus_export_product"
CONF_OCTOPUS_EXPORT_TARIFF: Final = "octopus_export_tariff"
CONF_OCTOPUS_REGION: Final = "octopus_region"
CONF_OCTOPUS_API_KEY: Final = "octopus_api_key"
CONF_OCTOPUS_ACCOUNT: Final = "octopus_account"

# Optimiser tuning
CONF_HORIZON_HOURS: Final = "horizon_hours"
CONF_SOC_LEVELS: Final = "soc_levels"
CONF_TERMINAL_VALUE_MODE: Final = "terminal_value_mode"
CONF_TERMINAL_VALUE_RATE: Final = "terminal_value_rate"
CONF_ALLOW_GRID_CHARGE: Final = "allow_grid_charge"
CONF_ALLOW_BATTERY_EXPORT: Final = "allow_battery_export"
CONF_ALLOW_EXPORT: Final = "allow_export"
CONF_DRY_RUN: Final = "dry_run"

# ---------------------------------------------------------------------------
# Provider identifiers
# ---------------------------------------------------------------------------

PROVIDER_NONE: Final = "none"
PROVIDER_OCTOPUS_ENTITY: Final = "octopus_entity"
PROVIDER_OCTOPUS_API: Final = "octopus_api"
PROVIDER_ENTITY: Final = "entity"
PROVIDER_FIXED: Final = "fixed"
PROVIDER_TOU: Final = "tou"

IMPORT_PROVIDERS: Final = [
    PROVIDER_OCTOPUS_ENTITY,
    PROVIDER_OCTOPUS_API,
    PROVIDER_ENTITY,
    PROVIDER_FIXED,
    PROVIDER_TOU,
]

EXPORT_PROVIDERS: Final = [
    PROVIDER_NONE,
    PROVIDER_OCTOPUS_ENTITY,
    PROVIDER_OCTOPUS_API,
    PROVIDER_ENTITY,
    PROVIDER_FIXED,
    PROVIDER_TOU,
]

ADAPTER_SOLAX_MODBUS: Final = "solax_modbus"
ADAPTER_GENERIC: Final = "generic"
ADAPTER_NONE: Final = "none"

ADAPTERS: Final = [ADAPTER_SOLAX_MODBUS, ADAPTER_GENERIC, ADAPTER_NONE]

TERMINAL_MODE_HORIZON_MEAN: Final = "horizon_mean"
TERMINAL_MODE_FIXED: Final = "fixed"
TERMINAL_MODE_ZERO: Final = "zero"

# ---------------------------------------------------------------------------
# Runtime strategy (user-selectable behaviour)
# ---------------------------------------------------------------------------

STRATEGY_AUTO: Final = "auto"
STRATEGY_SELF_USE: Final = "self_use"
STRATEGY_FORCE_CHARGE: Final = "force_charge"
STRATEGY_FORCE_DISCHARGE: Final = "force_discharge"
STRATEGY_IDLE: Final = "idle"
STRATEGY_OFF: Final = "off"

STRATEGIES: Final = [
    STRATEGY_AUTO,
    STRATEGY_SELF_USE,
    STRATEGY_FORCE_CHARGE,
    STRATEGY_FORCE_DISCHARGE,
    STRATEGY_IDLE,
    STRATEGY_OFF,
]

# ---------------------------------------------------------------------------
# Defaults tuned for the reference system (Solax X1 Hybrid G4 + ~22 kWh EV
# pack via Battery Emulator + ~2 kW PV) but every one of these is user
# configurable so the integration suits any size of system.
# ---------------------------------------------------------------------------

DEFAULT_BATTERY_CAPACITY: Final = 22.0
DEFAULT_MIN_SOC: Final = 15.0
DEFAULT_MAX_SOC: Final = 97.0
DEFAULT_RESERVE_SOC: Final = 10.0
DEFAULT_MAX_CHARGE_POWER: Final = 3.6
DEFAULT_MAX_DISCHARGE_POWER: Final = 3.6
DEFAULT_CHARGE_EFFICIENCY: Final = 0.95
DEFAULT_DISCHARGE_EFFICIENCY: Final = 0.95
DEFAULT_CYCLE_COST: Final = 2.0
DEFAULT_SOLAR_PEAK_POWER: Final = 2.0
DEFAULT_DAILY_LOAD: Final = 10.0
DEFAULT_GRID_EXPORT_LIMIT: Final = 3.68
DEFAULT_GRID_IMPORT_LIMIT: Final = 15.0
DEFAULT_HORIZON_HOURS: Final = 36
DEFAULT_SOC_LEVELS: Final = 60
DEFAULT_STANDING_CHARGE: Final = 0.0
DEFAULT_CURRENCY: Final = "GBP"
DEFAULT_IMPORT_FIXED_RATE: Final = 25.0
DEFAULT_EXPORT_FIXED_RATE: Final = 15.0

SLOT_MINUTES: Final = 30

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

SERVICE_REPLAN: Final = "replan"
SERVICE_SET_OVERRIDE: Final = "set_override"
SERVICE_CLEAR_OVERRIDE: Final = "clear_override"
SERVICE_RESET_LEARNING: Final = "reset_learning"
SERVICE_LEARN_FROM_HISTORY: Final = "learn_from_history"

ATTR_ACTION: Final = "action"
ATTR_DURATION: Final = "duration"
ATTR_POWER: Final = "power"
ATTR_TARGET_SOC: Final = "target_soc"
ATTR_DAYS: Final = "days"
