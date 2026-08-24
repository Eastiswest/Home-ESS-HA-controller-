"""Constants for the AI ESS Controller integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "ess_controller"
NAME: Final = "AI ESS Controller"
VERSION: Final = "0.26.1"
MANUFACTURER: Final = "AI ESS Controller"

PLATFORMS: Final = [
    "sensor",
    "binary_sensor",
    "switch",
    "number",
    "select",
    "button",
]

# How often the coordinator wakes up to re-plan. The optimiser itself is cheap,
# but a cycle also refreshes forecasts and may call the tariff API, so this is
# not something to run every few seconds. Half-hour slot boundaries are handled
# separately.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)

# How often the *live* state is re-read, separately from re-planning. Reading the
# inverter costs nothing -- the adapters read Home Assistant states that some
# other integration is already polling -- so there is no reason for the link
# status, SoC or power readings to be up to five minutes stale. This is what
# decides how quickly "inverter link disconnected" appears and clears.
DEFAULT_LIVE_INTERVAL: Final = timedelta(seconds=30)

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
# The wear allowance can be entered directly, or derived from what the pack cost.
CONF_DERIVE_WEAR_FROM_COST: Final = "derive_wear_from_cost"
CONF_BATTERY_COST: Final = "battery_cost"
CONF_BATTERY_EXPECTED_CYCLES: Final = "battery_expected_cycles"
CONF_BATTERY_RESIDUAL_VALUE: Final = "battery_residual_value"
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

# AgilePredict: predicted Agile prices for the slots Octopus has not announced.
#
# On by default for import. The original reasoning -- that an outbound call to a
# third-party service should be opted into -- had the balance wrong: Octopus
# announces only to 23:00 tomorrow, so without it the plan is blind past about
# lunchtime and cannot tell "the cheap window has passed" from "the cheap window
# is not published yet". A prediction is much better than no information at all,
# and the setting is right there to turn off.
#
# Predicted prices are *not* discounted before the optimiser sees them, and that is
# deliberate rather than an omission. Distorting a price to express doubt about it
# invents a fudge factor and makes the plan disagree with the figures on screen. The
# real protection is structural: only the current half-hour is ever acted on, and
# the plan is rebuilt every five minutes, so a prediction three days out shapes an
# intention that will be revised long before it becomes a command. Predicted slots
# are marked wherever they are shown, so nobody mistakes a guess for an announcement.
#
# Export stays off, because a site with no export tariff gains nothing from it.
CONF_AGILE_PREDICT: Final = "agile_predict"
CONF_AGILE_PREDICT_EXPORT: Final = "agile_predict_export"
DEFAULT_AGILE_PREDICT: Final = True
DEFAULT_AGILE_PREDICT_EXPORT: Final = False

# Grid incentive schemes (Saving Sessions, free electricity / Power Up)
CONF_SESSIONS_ENABLED: Final = "sessions_enabled"
CONF_SAVING_SESSION_ENTITIES: Final = "saving_session_entities"
CONF_FREE_SESSION_ENTITIES: Final = "free_session_entities"
CONF_SAVING_SESSION_RATE: Final = "saving_session_rate"
CONF_ONLY_JOINED_SESSIONS: Final = "only_joined_sessions"
CONF_SESSION_REWARD_EXPORT: Final = "session_reward_export"

# Power cut anticipation
CONF_OUTAGE_ENABLED: Final = "outage_enabled"
CONF_OUTAGE_RISK_ENTITY: Final = "outage_risk_entity"
CONF_OUTAGE_CALENDAR: Final = "outage_calendar"
CONF_OUTAGE_WIND_THRESHOLD: Final = "outage_wind_threshold"
CONF_OUTAGE_WIND_HIGH_THRESHOLD: Final = "outage_wind_high_threshold"
CONF_OUTAGE_RESERVE_SOC: Final = "outage_reserve_soc"
CONF_OUTAGE_HIGH_RESERVE_SOC: Final = "outage_high_reserve_soc"
CONF_OUTAGE_LOOKAHEAD_HOURS: Final = "outage_lookahead_hours"
# Which calendar events count as a planned interruption. Without this, any event
# on the chosen calendar became a high-risk power cut -- a bin collection would
# hold 80% of the pack back all day.
CONF_OUTAGE_CALENDAR_KEYWORDS: Final = "outage_calendar_keywords"
CONF_OUTAGE_CALENDAR_ALL_EVENTS: Final = "outage_calendar_all_events"
DEFAULT_OUTAGE_CALENDAR_KEYWORDS: Final = ",".join(
    (
        "outage",
        "blackout",
        "power cut",
        "powercut",
        "power off",
        "no power",
        "power interruption",
        "supply interruption",
        "electricity interruption",
        "planned interruption",
        "supply works",
        "electricity works",
        "shutdown",
    )
)
DEFAULT_OUTAGE_CALENDAR_ALL_EVENTS: Final = False

# Flexible load shifting
CONF_SHIFTING_ENABLED: Final = "shifting_enabled"
CONF_SHIFTABLE_LOADS: Final = "shiftable_loads"
CONF_APPLIANCE_CONTROL: Final = "appliance_control"

# Prebuilt dashboard
CONF_CREATE_DASHBOARD: Final = "create_dashboard"

# What counts as a "cheap" half-hour for flexible loads: the cheapest third of
# the priced horizon, as a fraction of the slots rather than of the price range.
CHEAP_SLOT_FRACTION: Final = 1.0 / 3.0

# Performance history
CONF_LOG_RETENTION_DAYS: Final = "log_retention_days"
DEFAULT_LOG_RETENTION_DAYS: Final = 60

# Tariff recommendations
CONF_RECOMMEND_ENABLED: Final = "recommend_enabled"
CONF_RECOMMEND_IMPORT_PRODUCTS: Final = "recommend_import_products"
CONF_RECOMMEND_EXPORT_PRODUCTS: Final = "recommend_export_products"
CONF_RECOMMEND_WINDOW_HOURS: Final = "recommend_window_hours"

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

TERMINAL_MODE_REPLACEMENT: Final = "replacement_cost"
TERMINAL_MODE_HORIZON_MEDIAN: Final = "horizon_median"
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
DEFAULT_BATTERY_COST: Final = 0.0
DEFAULT_BATTERY_EXPECTED_CYCLES: Final = 1500.0
DEFAULT_BATTERY_RESIDUAL_VALUE: Final = 0.0
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

DEFAULT_SAVING_SESSION_RATE: Final = 0.0
DEFAULT_OUTAGE_WIND_THRESHOLD: Final = 0.0
DEFAULT_OUTAGE_WIND_HIGH_THRESHOLD: Final = 0.0
DEFAULT_OUTAGE_RESERVE_SOC: Final = 50.0
DEFAULT_OUTAGE_HIGH_RESERVE_SOC: Final = 80.0
DEFAULT_OUTAGE_LOOKAHEAD_HOURS: Final = 12
DEFAULT_RECOMMEND_WINDOW_HOURS: Final = 24

SLOT_MINUTES: Final = 30

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

SERVICE_REPLAN: Final = "replan"
SERVICE_SET_OVERRIDE: Final = "set_override"
SERVICE_CLEAR_OVERRIDE: Final = "clear_override"
SERVICE_RESET_LEARNING: Final = "reset_learning"
SERVICE_RECOMMEND_TARIFFS: Final = "recommend_tariffs"
SERVICE_EXPORT_PERFORMANCE: Final = "export_performance"
SERVICE_GENERATE_DASHBOARD: Final = "generate_dashboard"
# Distinct from generate_dashboard, which only *returns* YAML. Rebuilding is the
# thing people actually want, and until this existed the only way to do it was to
# find a button that Home Assistant hides in a collapsed Configuration block.
SERVICE_REBUILD_DASHBOARD: Final = "rebuild_dashboard"

ATTR_ACTION: Final = "action"
ATTR_DURATION: Final = "duration"
ATTR_POWER: Final = "power"
ATTR_TARGET_SOC: Final = "target_soc"
ATTR_DAYS: Final = "days"
