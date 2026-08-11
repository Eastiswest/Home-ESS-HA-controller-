"""Predicted Agile prices for the slots Octopus has not announced yet.

Octopus publishes Agile rates for tomorrow at about 16:00, so for most of the
day the planning horizon runs off the end of real data. Until now the tail was
filled by day-ahead persistence -- yesterday's price at the same time of day --
which is honest but weak: it keeps the overnight trough and evening peak in
roughly the right place and knows nothing about tomorrow's wind.

AgilePredict (https://agilepredict.com, by fboundy) runs an ensemble model over
weather and demand data and publishes Agile forecasts per region up to fourteen
days out. This module consumes that API. It is a strictly better source than
persistence for the same job, and it degrades to persistence whenever it is
unavailable, so nothing depends on a third-party free service staying up.

The response shape, confirmed against the project's own API tests rather than
guessed:

.. code-block:: json

    [
      {
        "name": "forecast-2026-08-11T09:00:00",
        "created_at": "2026-08-11T09:04:11Z",
        "prices": [
          {"date_time": "2026-08-12T00:00:00+01:00", "agile_pred": 10.59,
           "agile_low": 8.1, "agile_high": 13.0, "region": "C"}
        ]
      }
    ]

A list of forecasts, newest first, each carrying its own price series.
``agile_pred`` is the predicted Agile unit rate in pence per kWh including VAT,
which is the same unit and basis as Octopus's own ``value_inc_vat`` -- so the two
series are directly comparable and no conversion is applied. That unit is taken
as given rather than sniffed from the values, unlike the supplier providers:
see ``parse_forecast``.

Home Assistant-free apart from the session object, which is duck-typed, so the
parsing is unit testable without a running instance.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..const import PRICE_SCALE_MINOR, SLOT_MINUTES
from ..models import PriceSlot
from .base import detect_scale

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://agilepredict.com/api"

REQUEST_TIMEOUT = 30

# Fourteen days are offered; a fortnight of half-hours is 672 slots to parse on
# every poll for a horizon that is at most a couple of days long. Asked for in
# whole days because that is the parameter the API takes.
MAX_DAYS = 14
DEFAULT_DAYS = 3

_SLOT = timedelta(minutes=SLOT_MINUTES)

KIND_UNREACHABLE = "unreachable"
KIND_NOT_FOUND = "not_found"
KIND_BAD_RESPONSE = "bad_response"
KIND_HTTP = "http"


class AgilePredictError(Exception):
    """Raised when AgilePredict cannot be reached or answers unusably."""

    def __init__(
        self, message: str, kind: str = KIND_UNREACHABLE, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def forecast_url(region: str) -> str:
    """The endpoint for one region.

    Region letters are the same ones Octopus uses as the suffix of a tariff code,
    so whatever the user already told us about their Octopus tariff answers this
    too -- there is nothing extra to ask them.
    """
    return f"{API_BASE}/{region.strip().upper()}"


def _is_number(value: Any) -> bool:
    """A real number, and not a bool.

    ``isinstance(True, int)`` is True in Python, so a JSON ``true`` in the price
    field would otherwise be planned against as 1p/kWh.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """The forecasts in a payload, whichever shape it arrived in.

    Documented as a list, newest first. A bare object is accepted too: a single
    forecast is a perfectly reasonable thing for the API to grow into returning,
    and treating it as an error would break for no good reason.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _parse_moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Python's parser wants an offset, not a Z.
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def newest_forecast(payload: Any) -> dict[str, Any] | None:
    """The most recently created forecast in a payload.

    The API returns newest first, but ``created_at`` is present and sorting on it
    costs nothing -- and a forecast_count > 1 response whose order ever changed
    would otherwise silently start using a stale prediction.
    """
    forecasts = [f for f in _as_list(payload) if f.get("prices")]
    if not forecasts:
        return None
    dated = [(_parse_moment(f.get("created_at")), f) for f in forecasts]
    if all(moment is not None for moment, _ in dated):
        return max(dated, key=lambda pair: pair[0])[1]
    return forecasts[0]


def parse_forecast(
    payload: Any,
    *,
    scale_mode: str = PRICE_SCALE_MINOR,
    after: datetime | None = None,
    until: datetime | None = None,
) -> list[PriceSlot]:
    """Turn an AgilePredict payload into forecast price slots.

    Every slot is flagged ``is_forecast``, which is what keeps predicted prices
    out of the learned persistence samples and lets the UI show where certainty
    ends. ``after`` and ``until`` trim the series to the part actually needed,
    since the API happily returns a fortnight.
    """
    forecast = newest_forecast(payload)
    if forecast is None:
        return []

    entries = [e for e in forecast.get("prices", []) if isinstance(e, dict)]
    values = [float(e["agile_pred"]) for e in entries if _is_number(e.get("agile_pred"))]
    if not values:
        return []
    # Minor units by default, and deliberately *not* the magnitude sniff the
    # supplier providers use. AgilePredict documents pence per kWh, and on a windy
    # night every value in the window can legitimately sit below the sniff's
    # major-unit threshold -- which would multiply the whole forecast by a hundred
    # and have the optimiser refuse to charge at 400p/kWh. A documented unit does
    # not need guessing at.
    scale = detect_scale(values, scale_mode)

    slots: list[PriceSlot] = []
    for entry in entries:
        price = entry.get("agile_pred")
        if not _is_number(price):
            continue
        start = _parse_moment(entry.get("date_time"))
        # A naive timestamp cannot be compared with the horizon's aware ones, and
        # assuming a zone would silently shift every price by an hour through
        # British Summer Time. Dropped instead, which shows up as a shorter
        # forecast -- persistence covers the rest -- rather than a wrong one.
        if start is None or start.tzinfo is None:
            continue
        end = start + _SLOT
        if after is not None and end <= after:
            continue
        if until is not None and start >= until:
            continue
        slots.append(
            PriceSlot(
                start=start,
                end=end,
                price=float(price) * scale,
                is_forecast=True,
            )
        )
    slots.sort(key=lambda slot: slot.start)
    return slots


def days_needed(now: datetime, horizon_end: datetime) -> int:
    """Whole days of forecast to ask for, clamped to what the API offers."""
    span = horizon_end - now
    days = int(span.total_seconds() // 86400) + 1
    return max(1, min(days, MAX_DAYS))


async def async_fetch_forecast(
    session: Any,
    region: str,
    *,
    days: int = DEFAULT_DAYS,
    export: bool = False,
    scale_mode: str = PRICE_SCALE_MINOR,
    after: datetime | None = None,
    until: datetime | None = None,
) -> list[PriceSlot]:
    """Fetch and parse one region's forecast.

    ``high_low=false`` because the low and high estimates are not used: planning
    against an optimistic bound would systematically over-charge, and against a
    pessimistic one under-charge. The central estimate is the honest input.
    """
    params: dict[str, str] = {
        "days": str(max(1, min(days, MAX_DAYS))),
        "high_low": "false",
    }
    if export:
        params["export"] = "true"

    url = forecast_url(region)
    try:
        response = await session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as err:
        raise AgilePredictError(
            f"request to {url} failed: {err}", KIND_UNREACHABLE
        ) from err

    async with response:
        if response.status == 404:
            raise AgilePredictError(
                f"no forecast for region {region}", KIND_NOT_FOUND, 404
            )
        if response.status >= 400:
            raise AgilePredictError(
                f"HTTP {response.status} from {url}", KIND_HTTP, response.status
            )
        try:
            payload = await response.json()
        except Exception as err:
            raise AgilePredictError(
                f"invalid JSON from {url}: {err}", KIND_BAD_RESPONSE
            ) from err

    return parse_forecast(payload, scale_mode=scale_mode, after=after, until=until)


def compare_with_actual(
    forecast: list[PriceSlot], actual: list[PriceSlot]
) -> dict[str, Any] | None:
    """How the prediction did, over the slots where both exist.

    AgilePredict returns a fortnight, so it always overlaps the announced window.
    That overlap is free scoring data: it says whether the forecast is any good
    for this region right now, and -- more usefully -- catches a units or VAT
    mismatch, which shows up as a large signed bias rather than as noise.
    """
    by_start = {slot.start: slot.price for slot in actual if not slot.is_forecast}
    pairs = [
        (slot.price, by_start[slot.start]) for slot in forecast if slot.start in by_start
    ]
    if not pairs:
        return None
    errors = [predicted - real for predicted, real in pairs]
    return {
        "slots": len(pairs),
        "mae": round(sum(abs(e) for e in errors) / len(errors), 3),
        "bias": round(sum(errors) / len(errors), 3),
    }
