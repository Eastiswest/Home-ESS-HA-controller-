"""Put the generated dashboard in the sidebar.

There is no public API for a custom integration to add a Lovelace dashboard, so
this does what integrations that manage one have to do: register a panel and hand
Lovelace something that can serve a configuration for it.

Two pieces, both re-done on every setup because both are in-memory:

1. ``frontend.async_register_built_in_panel`` puts the entry in the sidebar.
2. An entry in Lovelace's ``dashboards`` mapping tells the websocket API where to
   get the configuration from when the frontend asks for it. The object stored
   there is Lovelace's *own* ``LovelaceStorage``, so the storage format is right
   by construction and any edits the user makes are saved normally.

The configuration itself is seeded once, into its own store. After that the
dashboard behaves like any other: edit it and the edits persist, because they are
saved through the same object.

Deliberately *not* added to Lovelace's dashboards collection. Creating an item
there needs the live collection, which Home Assistant keeps as a local variable
in its own setup and never publishes -- so a second collection over the same
store is the only way, and that risks the live one overwriting the user's other
dashboards on its next save. A panel we register ourselves cannot damage
anything; the cost is that it is not listed under Settings > Dashboards, so the
way to remove it is the integration's own option rather than a delete button.

Everything is feature-detected and nothing is load-bearing: if a release moves
the furniture, the configuration is written to
``config/ess_controller/dashboard.yaml``, a notification says so, and the
controller carries on.
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
    dashboards_mapping,
)

_LOGGER = logging.getLogger(__name__)

LOVELACE_DOMAIN = "lovelace"
NOTIFICATION_ID = f"{DOMAIN}_dashboard"


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


def _notify(hass: HomeAssistant, title: str, message: str) -> None:
    """Say what happened where the user will actually see it.

    A log line is invisible to most people. If the dashboard did not appear, the
    reason belongs in the notification panel.
    """
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass, message, title=title, notification_id=NOTIFICATION_ID
        )
    except Exception:  # pragma: no cover - notifications are best effort
        _LOGGER.debug("Could not raise a notification", exc_info=True)


def _dashboard_item(entry: ConfigEntry) -> dict[str, Any]:
    """The dashboard descriptor Lovelace's storage class expects.

    ``id`` becomes the storage key, so it must be stable across restarts or the
    user's edits would be orphaned every boot.
    """
    return {
        "id": f"{DOMAIN}_{entry.entry_id}",
        "url_path": DASHBOARD_URL_PATH,
        "title": entry.title or DASHBOARD_TITLE,
        "icon": DASHBOARD_ICON,
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
    }


def _is_ours(stored: Any, entry: ConfigEntry) -> bool:
    """Whether a dashboard object at our URL path is one we registered.

    Identified by the storage id we chose. Anything else -- a dashboard the user
    created at the same path, or a YAML one with no id at all -- is theirs, and
    replacing it would take their dashboard away.
    """
    config = getattr(stored, "config", None)
    if not isinstance(config, dict):
        return False
    return config.get("id") == _dashboard_item(entry)["id"]


async def async_install(
    hass: HomeAssistant, entry: ConfigEntry, *, reseed: bool = False
) -> str:
    """Register the dashboard, seeding its configuration if it has none.

    Returns one of ``installed``, ``adopted`` (something else already owns the
    URL path), ``unsupported`` (YAML written instead) or ``failed``.

    Safe to call on every setup: the panel registration is in-memory and has to
    be repeated, and re-registering is what ``update=True`` is for.
    """
    config = build_for_entry(hass, entry)
    if not config["views"]:
        _LOGGER.debug("No entities resolved yet; not registering a dashboard")
        return "failed"

    existing = dashboards_mapping(hass.data.get(LOVELACE_DOMAIN))
    if existing is None:
        path = await async_write_yaml(hass, entry, config)
        _LOGGER.warning(
            "Lovelace is not available in the expected shape, so the dashboard "
            "could not be added to the sidebar. Its configuration is in %s",
            path,
        )
        _notify(
            hass,
            "ESS Controller dashboard",
            "The dashboard could not be added to the sidebar automatically on "
            f"this Home Assistant version. Its configuration has been written to "
            f"`{path}`.\n\nTo add it by hand: **Settings > Dashboards > Add "
            "dashboard > New dashboard from scratch**, open it, then use the "
            "three-dot menu > **Raw configuration editor** and paste the file in.",
        )
        return "unsupported"

    # Something already serves this URL path -- a dashboard the user made, or
    # our own from a previous boot. Re-registering our own is the normal case;
    # taking over someone else's is not ours to do.
    current = existing.get(DASHBOARD_URL_PATH)
    if current is not None and not _is_ours(current, entry):
        _LOGGER.info("A dashboard already uses /%s; leaving it alone", DASHBOARD_URL_PATH)
        return "adopted"

    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace.dashboard import LovelaceStorage

        item = _dashboard_item(entry)
        store = LovelaceStorage(hass, item)

        stored: Any = None
        if not reseed:
            try:
                stored = await store.async_load(False)
            except Exception:
                # ConfigNotFound on a first run, which is exactly the case we
                # are here to fix. Anything else is equally a reason to seed.
                stored = None
        if not stored:
            await store.async_save(config)

        existing[DASHBOARD_URL_PATH] = store
        frontend.async_register_built_in_panel(
            hass,
            LOVELACE_DOMAIN,
            sidebar_title=item["title"],
            sidebar_icon=item["icon"],
            frontend_url_path=DASHBOARD_URL_PATH,
            config={"mode": "storage"},
            require_admin=False,
            update=True,
        )
    except Exception as err:
        path = await async_write_yaml(hass, entry, config)
        _LOGGER.warning(
            "Could not register the ESS Controller dashboard (%s). Its "
            "configuration is in %s if you want to add it by hand",
            err,
            path,
        )
        _notify(
            hass,
            "ESS Controller dashboard",
            f"Adding the dashboard to the sidebar failed: `{err}`.\n\nIts "
            f"configuration has been written to `{path}`. Add it by hand with "
            "**Settings > Dashboards > Add dashboard**, then the three-dot menu "
            "> **Raw configuration editor**, and paste the file in.",
        )
        return "failed"

    _LOGGER.info(
        "ESS Controller dashboard registered at /%s with %d views",
        DASHBOARD_URL_PATH,
        len(config["views"]),
    )
    return "installed"


def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take the panel out of the sidebar, leaving the saved config in place.

    Called on unload so a reload does not leave a stale panel, and when the
    option is turned off. The stored configuration is deliberately kept: turning
    the option back on should not discard the user's edits.
    """
    dashboards = dashboards_mapping(hass.data.get(LOVELACE_DOMAIN))
    if dashboards is not None:
        stored = dashboards.get(DASHBOARD_URL_PATH)
        if stored is not None and not _is_ours(stored, entry):
            # Somebody else's dashboard lives here; leave it and its panel alone.
            return
        dashboards.pop(DASHBOARD_URL_PATH, None)

    try:
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, DASHBOARD_URL_PATH, warn_if_unknown=False)
    except Exception:  # pragma: no cover - depends on the HA version
        _LOGGER.debug("Could not remove the dashboard panel", exc_info=True)
