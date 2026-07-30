"""Build tariff providers from a config entry's options."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_EXPORT_FIXED_RATE,
    CONF_EXPORT_PRICE_SCALE,
    CONF_EXPORT_PROVIDER,
    CONF_EXPORT_RATE_ENTITY,
    CONF_EXPORT_TOU_SCHEDULE,
    CONF_IMPORT_FIXED_RATE,
    CONF_IMPORT_PRICE_SCALE,
    CONF_IMPORT_PROVIDER,
    CONF_IMPORT_RATE_ENTITY,
    CONF_IMPORT_TOU_SCHEDULE,
    CONF_OCTOPUS_API_KEY,
    CONF_OCTOPUS_EXPORT_PRODUCT,
    CONF_OCTOPUS_EXPORT_TARIFF,
    CONF_OCTOPUS_IMPORT_PRODUCT,
    CONF_OCTOPUS_IMPORT_TARIFF,
    CONF_OCTOPUS_REGION,
    DEFAULT_EXPORT_FIXED_RATE,
    DEFAULT_IMPORT_FIXED_RATE,
    PRICE_SCALE_AUTO,
    PROVIDER_ENTITY,
    PROVIDER_FIXED,
    PROVIDER_NONE,
    PROVIDER_OCTOPUS_API,
    PROVIDER_OCTOPUS_ENTITY,
    PROVIDER_TOU,
)
from .base import TariffProvider, ZeroTariffProvider
from .entity import EntityTariffProvider
from .fixed import FixedTariffProvider, TimeOfUseTariffProvider
from .octopus import OctopusApiTariffProvider, build_tariff_code

_LOGGER = logging.getLogger(__name__)


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


def build_provider(
    hass: HomeAssistant,
    options: dict[str, Any],
    direction: str,
) -> TariffProvider:
    """Create the import or export provider described by ``options``.

    Any misconfiguration degrades to a fixed rate rather than leaving the
    controller with no prices at all -- a stale plan is worse than a simple one.
    """
    is_import = direction == "import"
    provider = options.get(
        CONF_IMPORT_PROVIDER if is_import else CONF_EXPORT_PROVIDER,
        PROVIDER_FIXED if is_import else PROVIDER_NONE,
    )
    scale = options.get(
        CONF_IMPORT_PRICE_SCALE if is_import else CONF_EXPORT_PRICE_SCALE,
        PRICE_SCALE_AUTO,
    )
    fixed_rate = float(
        options.get(
            CONF_IMPORT_FIXED_RATE if is_import else CONF_EXPORT_FIXED_RATE,
            DEFAULT_IMPORT_FIXED_RATE if is_import else DEFAULT_EXPORT_FIXED_RATE,
        )
    )

    if provider == PROVIDER_NONE:
        if is_import:
            # Import prices are mandatory; there is nothing to optimise without
            # them, so fall back rather than accept "none".
            _LOGGER.warning("Import tariff cannot be 'none'; using a fixed rate")
            return FixedTariffProvider(fixed_rate)
        return ZeroTariffProvider()

    if provider in (PROVIDER_ENTITY, PROVIDER_OCTOPUS_ENTITY):
        entities = _as_list(
            options.get(CONF_IMPORT_RATE_ENTITY if is_import else CONF_EXPORT_RATE_ENTITY)
        )
        if not entities:
            _LOGGER.warning(
                "No %s rate entity configured; using a fixed rate", direction
            )
            return FixedTariffProvider(fixed_rate)
        return EntityTariffProvider(
            hass, entities, scale, fallback_rate=fixed_rate if is_import else 0.0
        )

    if provider == PROVIDER_OCTOPUS_API:
        tariff_code = options.get(
            CONF_OCTOPUS_IMPORT_TARIFF if is_import else CONF_OCTOPUS_EXPORT_TARIFF
        )
        product = options.get(
            CONF_OCTOPUS_IMPORT_PRODUCT if is_import else CONF_OCTOPUS_EXPORT_PRODUCT
        )
        region = options.get(CONF_OCTOPUS_REGION)
        if not tariff_code and product and region:
            try:
                tariff_code = build_tariff_code(product, region, direction)
            except ValueError as err:
                _LOGGER.error("Bad Octopus configuration: %s", err)
                tariff_code = None
        if not tariff_code:
            _LOGGER.warning(
                "Octopus %s tariff code missing; using a fixed rate", direction
            )
            return (
                FixedTariffProvider(fixed_rate) if is_import else ZeroTariffProvider()
            )
        return OctopusApiTariffProvider(
            async_get_clientsession(hass),
            tariff_code=tariff_code,
            product_code=product,
            api_key=options.get(CONF_OCTOPUS_API_KEY),
            scale_mode=scale,
        )

    if provider == PROVIDER_TOU:
        schedule = options.get(
            CONF_IMPORT_TOU_SCHEDULE if is_import else CONF_EXPORT_TOU_SCHEDULE
        )
        return TimeOfUseTariffProvider(
            schedule, fixed_rate, tz=dt_timezone(hass)
        )

    return FixedTariffProvider(fixed_rate)


def dt_timezone(hass: HomeAssistant):
    """The user's configured local timezone, for wall-clock tariff windows."""
    # ``get_default_time_zone()`` replaced the DEFAULT_TIME_ZONE constant in
    # HA 2024.6; support both so the integration spans a wider HA range.
    getter = getattr(dt_util, "get_default_time_zone", None)
    if callable(getter):
        return getter()
    return getattr(dt_util, "DEFAULT_TIME_ZONE", None)
