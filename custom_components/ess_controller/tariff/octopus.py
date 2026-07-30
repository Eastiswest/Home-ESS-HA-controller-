"""Octopus Energy REST API tariff provider.

Talks to the public Octopus API directly, so the integration works whether or
not a separate Octopus integration is installed. Unit-rate endpoints need no
authentication; an API key is only required for account lookup, which the
config flow uses to discover the user's tariff codes automatically.

Octopus quotes unit rates in pence per kWh, which is already the unit the
optimiser wants, so no scaling is applied by default.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta
from typing import Any

from ..const import PRICE_SCALE_AUTO, SLOT_MINUTES
from ..models import PriceSlot
from .base import PriceSeries, TariffProvider, detect_scale

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.octopus.energy/v1"

# Octopus region codes (the trailing letter of a tariff code).
REGIONS: tuple[str, ...] = (
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "J",
    "K",
    "L",
    "M",
    "N",
    "P",
)

REGION_NAMES: dict[str, str] = {
    "A": "Eastern England",
    "B": "East Midlands",
    "C": "London",
    "D": "Merseyside & North Wales",
    "E": "West Midlands",
    "F": "North East England",
    "G": "North West England",
    "H": "Southern England",
    "J": "South East England",
    "K": "South Wales",
    "L": "South West England",
    "M": "Yorkshire",
    "N": "Southern Scotland",
    "P": "Northern Scotland",
}

MAX_PAGES = 6
PAGE_SIZE = 1500
REQUEST_TIMEOUT = 30
_SLOT = timedelta(minutes=SLOT_MINUTES)


class OctopusApiError(Exception):
    """Raised when the Octopus API cannot be reached or returns an error."""


def build_tariff_code(product_code: str, region: str, direction: str = "import") -> str:
    """Compose a full tariff code from a product code and region letter.

    Octopus tariff codes look like ``E-1R-AGILE-24-10-01-C``: single-rate
    electricity, the product, then the region. Export products follow the same
    shape (``E-1R-OUTGOING-AGILE-24-10-01-C``).
    """
    product = product_code.strip().upper()
    letter = region.strip().upper()
    if letter not in REGIONS:
        raise ValueError(f"Unknown Octopus region {region!r}")
    # Tolerate a user pasting a full tariff code into the product field.
    if product.startswith("E-1R-") or product.startswith("E-2R-"):
        return product
    return f"E-1R-{product}-{letter}"


def product_code_from_tariff(tariff_code: str) -> str:
    """Recover the product code from a full tariff code."""
    parts = tariff_code.strip().upper().split("-")
    if len(parts) > 3 and parts[0] == "E":
        return "-".join(parts[2:-1])
    return tariff_code


def parse_unit_rates(
    payload: dict[str, Any],
    scale_mode: str = PRICE_SCALE_AUTO,
    *,
    fallback_end: datetime | None = None,
    include_vat: bool = True,
) -> list[PriceSlot]:
    """Convert a ``standard-unit-rates`` response into price slots.

    Handles the two awkward parts of the real payload: results arrive
    newest-first, and a non-varying tariff reports ``valid_to: null`` to mean
    "indefinitely", which we clamp to ``fallback_end``.
    """
    results = payload.get("results") or []
    key = "value_inc_vat" if include_vat else "value_exc_vat"

    raw_values = [
        r.get(key) for r in results if isinstance(r, dict) and r.get(key) is not None
    ]
    scale = detect_scale([float(v) for v in raw_values], scale_mode)

    slots: list[PriceSlot] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        value = entry.get(key)
        if value is None:
            continue
        start = _parse_dt(entry.get("valid_from"))
        end = _parse_dt(entry.get("valid_to"))
        if start is None:
            continue
        if end is None:
            if fallback_end is None or fallback_end <= start:
                continue
            end = fallback_end
        # An open-ended or multi-day band (fixed tariffs) is expanded into
        # half-hour slots so downstream code only ever sees a uniform grid.
        if end - start > _SLOT:
            cursor = start
            while cursor < end:
                slots.append(
                    PriceSlot(
                        start=cursor,
                        end=min(cursor + _SLOT, end),
                        price=float(value) * scale,
                    )
                )
                cursor += _SLOT
        else:
            slots.append(PriceSlot(start=start, end=end, price=float(value) * scale))

    slots.sort(key=lambda s: s.start)
    return slots


def _parse_dt(text: Any) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _LOGGER.debug("Could not parse Octopus timestamp %r", text)
        return None


def _auth_header(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class OctopusApiTariffProvider(TariffProvider):
    """Fetches half-hourly rates straight from the Octopus API."""

    name = "octopus_api"

    def __init__(
        self,
        session: Any,
        tariff_code: str,
        product_code: str | None = None,
        api_key: str | None = None,
        scale_mode: str = PRICE_SCALE_AUTO,
    ) -> None:
        self._session = session
        self._tariff_code = tariff_code.strip().upper()
        self._product_code = (
            product_code.strip().upper()
            if product_code
            else product_code_from_tariff(self._tariff_code)
        )
        self._api_key = api_key
        self._scale_mode = scale_mode
        self._available = True
        self._last_error: str | None = None
        # Rates change at most twice a day, so a short cache spares the API
        # while still picking tomorrow's prices up promptly.
        self._cache: PriceSeries | None = None
        self._cache_time: datetime | None = None
        self._cache_ttl = timedelta(minutes=30)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def url(self) -> str:
        return (
            f"{API_BASE}/products/{self._product_code}"
            f"/electricity-tariffs/{self._tariff_code}/standard-unit-rates/"
        )

    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        if (
            self._cache is not None
            and self._cache_time is not None
            and now - self._cache_time < self._cache_ttl
            and (self._cache.known_until(now) or now) >= horizon_end
        ):
            return self._cache

        # Reach back a day so persistence extrapolation has yesterday's shape
        # to work from, and forward past the horizon to catch a fresh release.
        params = {
            "period_from": (now - timedelta(days=1)).isoformat(),
            "period_to": (horizon_end + timedelta(days=1)).isoformat(),
            "page_size": str(PAGE_SIZE),
        }

        try:
            payloads = await self._async_get_paged(self.url, params)
        except OctopusApiError as err:
            self._available = False
            self._last_error = str(err)
            _LOGGER.warning("Octopus API fetch failed (%s); using cached rates", err)
            return self._cache or PriceSeries()

        series = PriceSeries()
        for payload in payloads:
            series.merge(
                parse_unit_rates(
                    payload,
                    self._scale_mode,
                    fallback_end=horizon_end + timedelta(days=1),
                )
            )

        self._available = bool(series)
        self._last_error = None if series else "no rates returned"
        if series:
            self._cache = series
            self._cache_time = now
        return series if series else (self._cache or PriceSeries())

    async def _async_get_paged(
        self, url: str, params: dict[str, str] | None
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params = params
        for _ in range(MAX_PAGES):
            if not next_url:
                break
            payload = await self._async_get(next_url, next_params)
            payloads.append(payload)
            next_url = payload.get("next")
            # The "next" link already carries its query string.
            next_params = None
        return payloads

    async def _async_get(self, url: str, params: dict[str, str] | None) -> dict[str, Any]:
        return await async_get_json(self._session, url, params, self._api_key)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "tariff_code": self._tariff_code,
            "product_code": self._product_code,
            "available": self._available,
            "last_error": self._last_error,
            "cached_slots": len(self._cache) if self._cache else 0,
        }


async def async_get_json(
    session: Any,
    url: str,
    params: dict[str, str] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """GET a JSON document from the Octopus API, raising on any failure."""
    try:
        response = await session.get(
            url, params=params, headers=_auth_header(api_key), timeout=REQUEST_TIMEOUT
        )
    except Exception as err:
        raise OctopusApiError(f"request to {url} failed: {err}") from err

    async with response:
        if response.status == 401:
            raise OctopusApiError("unauthorised: check the Octopus API key")
        if response.status == 404:
            raise OctopusApiError(
                "not found: check the product and tariff codes are correct"
            )
        if response.status >= 400:
            raise OctopusApiError(f"HTTP {response.status} from {url}")
        try:
            return await response.json()
        except Exception as err:
            raise OctopusApiError(f"invalid JSON from {url}: {err}") from err


def parse_account_tariffs(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract current import/export tariff codes from an account payload.

    Octopus marks an export meter point with ``is_export: true``, which is the
    only reliable way to tell an Outgoing agreement from an incoming one.
    Agreements are dated; we take the one with no end date, or the latest.
    """
    found: dict[str, Any] = {"import": None, "export": None, "mpans": []}

    for prop in payload.get("properties") or []:
        for point in prop.get("electricity_meter_points") or []:
            agreements = point.get("agreements") or []
            code = _current_agreement(agreements)
            if not code:
                continue
            direction = "export" if point.get("is_export") else "import"
            found["mpans"].append(
                {
                    "mpan": point.get("mpan"),
                    "direction": direction,
                    "tariff_code": code,
                }
            )
            if found[direction] is None:
                found[direction] = code
    return found


def _current_agreement(agreements: list[dict[str, Any]]) -> str | None:
    best: tuple[datetime | None, str] | None = None
    for agreement in agreements:
        code = agreement.get("tariff_code")
        if not code:
            continue
        valid_to = _parse_dt(agreement.get("valid_to"))
        if valid_to is None:
            return code  # open-ended agreement is the live one
        if best is None or (best[0] is not None and valid_to > best[0]):
            best = (valid_to, code)
    return best[1] if best else None


async def async_discover_tariffs(
    session: Any, api_key: str, account_id: str
) -> dict[str, Any]:
    """Look up an account's live tariff codes, for the config flow."""
    url = f"{API_BASE}/accounts/{account_id.strip().upper()}/"
    payload = await async_get_json(session, url, None, api_key)
    return parse_account_tariffs(payload)


async def async_validate_tariff(
    session: Any, tariff_code: str, product_code: str | None = None
) -> int:
    """Confirm a tariff code resolves, returning how many rates came back."""
    code = tariff_code.strip().upper()
    product = product_code or product_code_from_tariff(code)
    url = f"{API_BASE}/products/{product}/electricity-tariffs/{code}/standard-unit-rates/"
    payload = await async_get_json(session, url, {"page_size": "10"})
    return int(payload.get("count") or len(payload.get("results") or []))
