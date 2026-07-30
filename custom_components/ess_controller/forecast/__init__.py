"""Solar, load and weather forecasting.

Every module here is free of Home Assistant imports: the coordinator gathers raw
entity attributes and hands them in, so the forecasting maths can be tested on
its own.
"""

from .energy import EnergySeries, EnergySlot
from .load import DIURNAL_WEIGHTS, LoadForecaster, LoadPrediction, default_slot_load
from .solar import (
    SolarForecaster,
    SolarPrediction,
    build_forecast_series,
    parse_solar_forecast_attributes,
)
from .weather import WeatherPoint, WeatherSeries

__all__ = [
    "DIURNAL_WEIGHTS",
    "EnergySeries",
    "EnergySlot",
    "LoadForecaster",
    "LoadPrediction",
    "SolarForecaster",
    "SolarPrediction",
    "WeatherPoint",
    "WeatherSeries",
    "build_forecast_series",
    "default_slot_load",
    "parse_solar_forecast_attributes",
]
