"""Tariff provider that reads rates from existing Home Assistant entities.

This is the path for users who already run a supplier integration -- most
commonly BottlecapDave's Octopus Energy integration, but the extraction is
deliberately tolerant so Nord Pool, Tibber, Amber and hand-built template
sensors work too. Rather than demand one exact schema, we look for any of the
attribute names those integrations use and any of the value keys they publish.

The parsing is kept free of Home Assistant imports so it can be tested against
captured real-world attribute payloads.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from ..const import PRICE_SCALE_AUTO, SLOT_MINUTES
from ..models import PriceSlot
from .base import PriceSeries, TariffProvider, detect_scale

_LOGGER = logging.getLogger(__name__)

_SLOT = timedelta(minutes=SLOT_MINUTES)

# Attributes that may hold a list of forward rates, best first. BottlecapDave
# publishes "rates" on its rate events and "all_rates" on the current-rate
# sensor; Nord Pool uses raw_today/raw_tomorrow; others vary.
RATE_ATTRIBUTES: tuple[str, ...] = (
    "rates",
    "all_rates",
    "raw_today",
    "raw_tomorrow",
    "prices",
    "forecast",
    "rates_today",
    "rates_tomorrow",
    "today",
    "tomorrow",
)

# Attributes checked in addition to the primary one, so a single entity that
# splits today and tomorrow still yields a full horizon.
COMPANION_ATTRIBUTES: tuple[tuple[str, ...], ...] = (
    ("raw_today", "raw_tomorrow"),
    ("rates_today", "rates_tomorrow"),
    ("today", "tomorrow"),
)

START_KEYS: tuple[str, ...] = (
    "start",
    "valid_from",
    "from",
    "start_time",
    "hour",
    "time",
)
END_KEYS: tuple[str, ...] = ("end", "valid_to", "to", "end_time")
VALUE_KEYS: tuple[str, ...] = (
    "value_inc_vat",
    "value",
    "price",
    "rate",
    "cost",
    "per_kwh",
    "value_exc_vat",
)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pick(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def extract_rate_entries(attributes: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Find the list of rate dictionaries inside an entity's attributes."""
    for group in COMPANION_ATTRIBUTES:
        combined: list[Mapping[str, Any]] = []
        for name in group:
            value = attributes.get(name)
            if isinstance(value, list):
                combined.extend(item for item in value if isinstance(item, Mapping))
        if combined:
            return combined

    for name in RATE_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, list) and value:
            entries = [item for item in value if isinstance(item, Mapping)]
            if entries:
                return entries
    return []


def parse_rate_entries(
    entries: Iterable[Mapping[str, Any]],
    scale_mode: str = PRICE_SCALE_AUTO,
) -> list[PriceSlot]:
    """Convert loosely-shaped rate dictionaries into price slots."""
    staged: list[tuple[datetime, datetime | None, float]] = []
    values: list[float] = []

    for entry in entries:
        raw_value = _pick(entry, VALUE_KEYS)
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        start = _parse_dt(_pick(entry, START_KEYS))
        if start is None:
            continue
        end = _parse_dt(_pick(entry, END_KEYS))
        staged.append((start, end, value))
        values.append(value)

    if not staged:
        return []

    scale = detect_scale(values, scale_mode)
    staged.sort(key=lambda item: item[0])

    slots: list[PriceSlot] = []
    for index, (start, end, value) in enumerate(staged):
        if end is None or end <= start:
            # Infer the window from the next entry, defaulting to a half hour.
            end = staged[index + 1][0] if index + 1 < len(staged) else start + _SLOT
        # Split anything longer than a slot so the grid stays uniform.
        if end - start > _SLOT * 1.5:
            cursor = start
            while cursor < end:
                slots.append(
                    PriceSlot(
                        start=cursor,
                        end=min(cursor + _SLOT, end),
                        price=value * scale,
                    )
                )
                cursor += _SLOT
        else:
            slots.append(PriceSlot(start=start, end=end, price=value * scale))
    return slots


def parse_entity_rates(
    attributes: Mapping[str, Any], scale_mode: str = PRICE_SCALE_AUTO
) -> list[PriceSlot]:
    """Full pipeline: entity attributes in, price slots out."""
    return parse_rate_entries(extract_rate_entries(attributes), scale_mode)


class EntityTariffProvider(TariffProvider):
    """Reads forward rates from one or more Home Assistant entities."""

    name = "entity"

    def __init__(
        self,
        hass: Any,
        entity_ids: list[str],
        scale_mode: str = PRICE_SCALE_AUTO,
        fallback_rate: float | None = None,
    ) -> None:
        self._hass = hass
        self._entity_ids = [e for e in entity_ids if e]
        self._scale_mode = scale_mode
        self._fallback_rate = fallback_rate
        self._available = False
        self._last_error: str | None = None
        self._slot_count = 0

    @property
    def available(self) -> bool:
        return self._available

    async def async_fetch(self, now: datetime, horizon_end: datetime) -> PriceSeries:
        series = PriceSeries()
        missing: list[str] = []

        for entity_id in self._entity_ids:
            state = self._hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                missing.append(entity_id)
                continue
            slots = parse_entity_rates(state.attributes, self._scale_mode)
            if slots:
                series.merge(slots)
            else:
                missing.append(entity_id)

        self._slot_count = len(series)

        if not series:
            self._available = False
            self._last_error = (
                f"no rate data found on {', '.join(self._entity_ids) or '(none)'}"
            )
            if self._fallback_rate is not None:
                _LOGGER.warning(
                    "%s; falling back to a flat %.2f",
                    self._last_error,
                    self._fallback_rate,
                )
                cursor = now.replace(
                    minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES,
                    second=0,
                    microsecond=0,
                )
                flat: list[PriceSlot] = []
                while cursor < horizon_end:
                    flat.append(
                        PriceSlot(
                            start=cursor,
                            end=cursor + _SLOT,
                            price=self._fallback_rate,
                            is_forecast=True,
                        )
                    )
                    cursor += _SLOT
                series.merge(flat)
            return series

        self._available = True
        self._last_error = f"no data from {', '.join(missing)}" if missing else None
        return series

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "entities": self._entity_ids,
            "available": self._available,
            "last_error": self._last_error,
            "slots": self._slot_count,
        }
