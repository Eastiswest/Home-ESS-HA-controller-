"""Supplier-agnostic tariff providers.

Only Home Assistant-free symbols are exported here so the parsing logic stays
unit testable. The factory that builds providers from a config entry lives in
:mod:`.factory`.
"""

from .base import (
    PriceSeries,
    TariffProvider,
    ZeroTariffProvider,
    detect_scale,
)
from .entity import parse_entity_rates, parse_rate_entries
from .fixed import FixedTariffProvider, TimeOfUseTariffProvider, parse_tou_schedule
from .octopus import (
    REGION_NAMES,
    REGIONS,
    OctopusApiError,
    build_tariff_code,
    parse_account_tariffs,
    parse_unit_rates,
    product_code_from_tariff,
)

__all__ = [
    "REGIONS",
    "REGION_NAMES",
    "FixedTariffProvider",
    "OctopusApiError",
    "PriceSeries",
    "TariffProvider",
    "TimeOfUseTariffProvider",
    "ZeroTariffProvider",
    "build_tariff_code",
    "detect_scale",
    "parse_account_tariffs",
    "parse_entity_rates",
    "parse_rate_entries",
    "parse_tou_schedule",
    "parse_unit_rates",
    "product_code_from_tariff",
]
