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

from .const import CONF_CURRENCY, DEFAULT_CURRENCY, DOMAIN
from .dashboard import (
    APEX_RESOURCE,
    DASHBOARD_ICON,
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    build_dashboard,
    dashboards_mapping,
    fingerprint,
    is_placeholder,
    is_untouched,
    placeholder_dashboard,
    stamp,
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


def has_apexcharts(hass: HomeAssistant) -> bool:
    """Whether ApexCharts Card is registered as a Lovelace resource.

    Feature detection rather than a config option: the answer is knowable, and a
    setting the user has to find and tick to get charts they already installed is
    a setting that will not get ticked. Because the dashboard is fingerprinted,
    installing the card and reloading makes the charts appear on their own.

    Anything unexpected in Lovelace's internals means "no": drawing block
    characters is always safe, whereas emitting a custom card that is not there
    renders a red error box.
    """
    data = hass.data.get(LOVELACE_DOMAIN)
    resources = getattr(data, "resources", None)
    if resources is None:
        return False
    try:
        items = list(resources.async_items())
    except Exception:  # pragma: no cover - defensive about a private API
        _LOGGER.debug("Could not read Lovelace resources", exc_info=True)
        return False
    return any(APEX_RESOURCE in str(item.get("url", "")) for item in items)


def build_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    title = entry.title or DASHBOARD_TITLE
    currency = {**entry.data, **entry.options}.get(CONF_CURRENCY, DEFAULT_CURRENCY)
    return build_dashboard(
        resolve_entities(hass, entry),
        title=title,
        charts=has_apexcharts(hass),
        currency=str(currency),
    )


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


def _register_panel(hass: HomeAssistant, item: dict[str, Any], mode: str) -> None:
    """Put the panel in the sidebar.

    Prefers Lovelace's own ``_register_panel``, which is what its dashboards
    collection calls and therefore the exact call a real dashboard gets --
    including the keys it reads out of the item dict. Falls back to the frontend
    helper directly if that private name ever moves.
    """
    try:
        from homeassistant.components.lovelace import _register_panel as lovelace_panel

        lovelace_panel(hass, item["url_path"], mode, item, True)
        return
    except Exception:
        _LOGGER.debug("Lovelace's panel helper unavailable; registering directly")

    from homeassistant.components import frontend

    frontend.async_register_built_in_panel(
        hass,
        LOVELACE_DOMAIN,
        sidebar_title=item["title"],
        sidebar_icon=item["icon"],
        frontend_url_path=item["url_path"],
        config={"mode": mode},
        require_admin=item["require_admin"],
        update=True,
        show_in_sidebar=item["show_in_sidebar"],
    )


async def _async_install_storage(
    hass: HomeAssistant,
    entry: ConfigEntry,
    dashboards: dict[str, Any],
    config: dict[str, Any],
    reseed: bool,
) -> None:
    """Serve an editable, storage-backed dashboard.

    Preferred because the user can rearrange it in the UI and the edits persist:
    they are saved through the same object Lovelace would use for a dashboard
    created by hand.
    """
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    item = _dashboard_item(entry)
    store = LovelaceStorage(hass, item)

    stored: Any = None
    if not reseed:
        try:
            stored = await store.async_load(False)
        except Exception:
            # ConfigNotFound on a first run, which is the case we are here to
            # fix. Anything else is equally a reason to seed.
            stored = None
    # Three replaceable cases and one that never is.
    #
    # Nothing stored, or the placeholder: seed it, obviously. A dashboard that is
    # byte-for-byte what we last generated: also safe, and this is the case that
    # matters -- without it every improvement to the dashboard needed the user to
    # delete theirs by hand, which they had to be told three separate times.
    # Anything else carries edits and is left alone; the doubtful case has to be
    # "leave it", because a wrong answer destroys somebody's work.
    stamped = stamp(config)
    if not stored or is_placeholder(stored):
        await store.async_save(stamped)
    elif is_untouched(stored):
        if fingerprint(stored) != fingerprint(stamped):
            _LOGGER.info(
                "Refreshing the ESS Controller dashboard: the stored one is "
                "unmodified and this version generates a different layout"
            )
            await store.async_save(stamped)
    else:
        _LOGGER.debug("Leaving the ESS Controller dashboard alone: it has been edited")

    dashboards[DASHBOARD_URL_PATH] = store
    _register_panel(hass, item, "storage")


async def _async_install_yaml(
    hass: HomeAssistant,
    entry: ConfigEntry,
    dashboards: dict[str, Any],
    config: dict[str, Any],
) -> str:
    """Serve the dashboard straight from the YAML file, as a fallback.

    Read-only in the UI, which is why it is second choice, but it depends on far
    less: a file we wrote and Lovelace's YAML dashboard class. Returns the path.
    """
    from homeassistant.components.lovelace.dashboard import LovelaceYAML

    path = await async_write_yaml(hass, entry, config)
    item = _dashboard_item(entry)
    item["mode"] = "yaml"
    # Relative to the config directory, which is what Lovelace resolves against.
    item["filename"] = f"{DOMAIN}/dashboard.yaml"
    dashboards[DASHBOARD_URL_PATH] = LovelaceYAML(hass, DASHBOARD_URL_PATH, item)
    _register_panel(hass, item, "yaml")
    return path


async def async_install(
    hass: HomeAssistant, entry: ConfigEntry, *, reseed: bool = False
) -> str:
    """Register the dashboard, seeding its configuration if it has none.

    Returns ``installed``, ``installed_yaml``, ``adopted`` (something else owns
    the URL path), ``unsupported`` (file written, nothing registered), ``waiting``
    (no entities yet) or ``failed``.

    Safe to call on every setup: the panel registration is in-memory and has to
    be repeated, which is what ``update=True`` is for.
    """
    config = build_for_entry(hass, entry)
    lovelace = hass.data.get(LOVELACE_DOMAIN)
    dashboards = dashboards_mapping(lovelace)

    # One line saying what was actually found, because every failure below is
    # otherwise indistinguishable from the outside.
    _LOGGER.debug(
        "Dashboard install: views=%d lovelace=%s dashboards=%s",
        len(config["views"]),
        type(lovelace).__name__ if lovelace is not None else "missing",
        "yes" if dashboards is not None else "no",
    )

    waiting = not config["views"]
    if waiting:
        # Register the placeholder rather than nothing at all: an absent sidebar
        # entry is indistinguishable from the integration having failed, and that
        # ambiguity is exactly what made this hard to diagnose. The caller retries
        # and the real dashboard replaces it.
        _LOGGER.debug("No entities resolved yet; registering a placeholder")
        config = placeholder_dashboard(entry.title or DASHBOARD_TITLE)

    if dashboards is None:
        path = await async_write_yaml(hass, entry, config)
        _LOGGER.warning(
            "Lovelace is not available in the expected shape (found %s), so the "
            "dashboard could not be added to the sidebar. Its configuration is "
            "in %s",
            type(lovelace).__name__ if lovelace is not None else "nothing",
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
    current = dashboards.get(DASHBOARD_URL_PATH)
    if current is not None and not _is_ours(current, entry):
        _LOGGER.info("A dashboard already uses /%s; leaving it alone", DASHBOARD_URL_PATH)
        return "adopted"

    try:
        await _async_install_storage(hass, entry, dashboards, config, reseed)
    except Exception as storage_err:
        _LOGGER.warning(
            "Could not register an editable dashboard (%s); falling back to "
            "serving it from a file",
            storage_err,
        )
        try:
            path = await _async_install_yaml(hass, entry, dashboards, config)
        except Exception as yaml_err:
            path = await async_write_yaml(hass, entry, config)
            _LOGGER.warning(
                "Could not register the dashboard at all (%s). Its configuration "
                "is in %s if you want to add it by hand",
                yaml_err,
                path,
            )
            _notify(
                hass,
                "ESS Controller dashboard",
                f"Adding the dashboard to the sidebar failed: `{yaml_err}`.\n\n"
                f"Its configuration has been written to `{path}`. Add it by hand "
                "with **Settings > Dashboards > Add dashboard**, then the "
                "three-dot menu > **Raw configuration editor**, and paste the "
                "file in.",
            )
            return "failed"

        _notify(
            hass,
            "ESS Controller dashboard",
            "**ESS Controller** is in your sidebar, served from "
            f"`{path}`.\n\nEditing it in the UI is not possible in this mode -- "
            "edit that file instead, or use the "
            "`ess_controller.generate_dashboard` action to get the configuration "
            "for a dashboard of your own.",
        )
        return "installed_yaml"

    if waiting:
        _LOGGER.info(
            "ESS Controller dashboard registered at /%s; waiting for entities",
            DASHBOARD_URL_PATH,
        )
        return "waiting"

    _LOGGER.info(
        "ESS Controller dashboard registered at /%s with %d views",
        DASHBOARD_URL_PATH,
        len(config["views"]),
    )
    _notify(
        hass,
        "ESS Controller dashboard",
        f"**ESS Controller** is in your sidebar with {len(config['views'])} "
        "views. It is an ordinary dashboard now -- rearrange it however you "
        "like and the changes are kept.\n\nIf you cannot see it, reload the "
        "page. To remove it, turn off *Add a dashboard to the sidebar* in the "
        "integration's options.",
    )
    return "installed"


def _url_paths(dashboards: dict[Any, Any]) -> list[str]:
    """The url paths Lovelace is serving, in a form that can be sorted.

    Home Assistant keys its *default* dashboard ``None`` and every other one by
    url path, so sorting the raw keys compares ``None`` with a string and raises.
    That happens on every real install, because the default dashboard always
    exists -- so the diagnostic written to explain a missing dashboard was itself
    the only thing failing, and reported nothing but a TypeError. The test
    harness built its mapping empty, which is the one shape that never occurs.
    """
    return sorted("(default)" if key is None else str(key) for key in dashboards)


def describe(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """What the installer can see, for the diagnostics download.

    Every failure mode above looks identical from the outside -- no sidebar
    entry -- so the state it depends on is reported explicitly.
    """
    lovelace = hass.data.get(LOVELACE_DOMAIN)
    dashboards = dashboards_mapping(lovelace)
    resolved = resolve_entities(hass, entry)
    current = (dashboards or {}).get(DASHBOARD_URL_PATH)
    return {
        "url_path": DASHBOARD_URL_PATH,
        "lovelace_data": type(lovelace).__name__ if lovelace is not None else None,
        "dashboards_mapping": None if dashboards is None else _url_paths(dashboards),
        "entities_resolved": len(resolved),
        "views_built": len(build_dashboard(resolved)["views"]),
        "registered": current is not None,
        "registered_is_ours": current is not None and _is_ours(current, entry),
        "registered_type": type(current).__name__ if current is not None else None,
    }


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
