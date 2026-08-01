"""Install the generated dashboard into the sidebar.

There is no public API for a custom integration to add a Lovelace dashboard, so
this does it the way every integration that manages one does: through Lovelace's
own storage collection. That is internal API, so every step is feature-detected
and the whole thing is wrapped — a Home Assistant release that moves the
furniture must cost the user a sidebar entry, never a working controller.

The fallback is deliberate rather than a consolation prize: the same
configuration is written to ``config/ess_controller/dashboard.yaml`` and
returned by the ``generate_dashboard`` action, so adding it by hand is one paste
into the raw configuration editor.

Rules this follows:

* **Created once.** A flag is persisted the first time, so a user who deletes
  the dashboard does not find it back after every restart.
* **Never overwritten.** If a dashboard already claims the URL path, the config
  is left exactly as it is. After creation it is the user's, edits included.
* **Never fatal.** Any failure logs and moves on.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .dashboard import (
    DASHBOARD_ICON,
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    build_dashboard,
)

_LOGGER = logging.getLogger(__name__)

LOVELACE_DOMAIN = "lovelace"


def resolve_entities(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """Map each entity key to the entity id it actually got.

    Read from the entity registry rather than assembled from the device name:
    the user may have renamed entities, and disabled ones must be left out so
    the dashboard never points at something that will not resolve.
    """
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    resolved: dict[str, str] = {}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not item.unique_id.startswith(prefix) or item.disabled_by is not None:
            continue
        resolved[item.unique_id[len(prefix) :]] = item.entity_id
    return resolved


def build_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    title = entry.title or DASHBOARD_TITLE
    return build_dashboard(resolve_entities(hass, entry), title=title)


def to_yaml(config: dict[str, Any]) -> str:
    """Serialise for the raw configuration editor.

    JSON is a subset of YAML, so the fallback is still valid input to the editor
    if the YAML dumper is not importable for any reason.
    """
    try:
        from homeassistant.util.yaml import dump as yaml_dump

        return yaml_dump(config)
    except Exception:  # pragma: no cover - depends on the HA version
        _LOGGER.debug("YAML dumper unavailable; falling back to JSON")
        return json.dumps(config, indent=2)


async def async_write_yaml(
    hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any] | None = None
) -> str:
    """Write the dashboard YAML next to the performance exports."""
    directory = hass.config.path(DOMAIN)
    path = f"{directory}/dashboard.yaml"
    payload = to_yaml(config if config is not None else build_for_entry(hass, entry))

    def _write() -> None:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)

    await hass.async_add_executor_job(_write)
    return path


def _lovelace_data(hass: HomeAssistant) -> Any | None:
    return hass.data.get(LOVELACE_DOMAIN)


def _dashboards(data: Any) -> dict[str, Any] | None:
    """Lovelace's url_path -> dashboard mapping, whichever shape it is in.

    It was a plain dict under ``hass.data["lovelace"]["dashboards"]`` for years
    and is an attribute of a dataclass in newer releases.
    """
    if data is None:
        return None
    if isinstance(data, dict):
        found = data.get("dashboards")
        return found if isinstance(found, dict) else None
    found = getattr(data, "dashboards", None)
    return found if isinstance(found, dict) else None


def _collection(data: Any) -> Any | None:
    """The live dashboards collection, if it can be reached.

    Only the *live* one will do. Instantiating a second collection over the same
    store would let the live one overwrite our item on its next save -- and
    worse, could drop dashboards the user created in the meantime.
    """
    if data is None:
        return None
    candidates = ("dashboards_collection", "dashboard_collection", "collection")
    if isinstance(data, dict):
        for name in candidates:
            found = data.get(name)
            if found is not None and hasattr(found, "async_create_item"):
                return found
        return None
    for name in candidates:
        found = getattr(data, name, None)
        if found is not None and hasattr(found, "async_create_item"):
            return found
    return None


async def async_install(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Create the sidebar dashboard. Returns what happened, for logging.

    One of: ``created``, ``exists``, ``unsupported`` (Lovelace internals did not
    look as expected, YAML written instead), or ``failed``.
    """
    config = build_for_entry(hass, entry)
    if not config["views"]:
        _LOGGER.debug("No entities resolved yet; not creating a dashboard")
        return "failed"

    data = _lovelace_data(hass)
    dashboards = _dashboards(data)
    if dashboards is not None and DASHBOARD_URL_PATH in dashboards:
        _LOGGER.debug("Dashboard %s already exists; leaving it alone", DASHBOARD_URL_PATH)
        return "exists"

    collection = _collection(data)
    if collection is None:
        path = await async_write_yaml(hass, entry, config)
        _LOGGER.warning(
            "Could not add the dashboard to the sidebar automatically on this "
            "Home Assistant version. The configuration has been written to %s -- "
            "create a dashboard under Settings > Dashboards, open its raw "
            "configuration editor and paste the file in",
            path,
        )
        return "unsupported"

    try:
        await collection.async_create_item(
            {
                "url_path": DASHBOARD_URL_PATH,
                "title": entry.title or DASHBOARD_TITLE,
                "icon": DASHBOARD_ICON,
                "show_in_sidebar": True,
                "require_admin": False,
            }
        )
        # Creating the item registers the panel; the config is stored separately,
        # against the dashboard object Lovelace just built for it.
        dashboards = _dashboards(_lovelace_data(hass)) or {}
        target = dashboards.get(DASHBOARD_URL_PATH)
        if target is None or not hasattr(target, "async_save"):
            raise RuntimeError("dashboard created but its config store is unreachable")
        await target.async_save(config)
    except Exception as err:
        # Includes the case where the collection rejects the item because the URL
        # path is taken by a dashboard Lovelace had not listed for us.
        path = await async_write_yaml(hass, entry, config)
        _LOGGER.warning(
            "Could not create the ESS Controller dashboard (%s). Its configuration "
            "is in %s if you want to add it by hand",
            err,
            path,
        )
        return "failed"

    _LOGGER.info(
        "Added the ESS Controller dashboard to the sidebar with %d views. "
        "It is yours now -- edit or delete it freely; it will not be recreated",
        len(config["views"]),
    )
    return "created"
