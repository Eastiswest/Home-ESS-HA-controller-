"""A ready-made dashboard, generated from the entities that actually exist.

Nobody should have to assemble a dashboard card by card before the integration
tells them anything. This module builds a complete Lovelace configuration —
five views, stock cards only — which is installed into the sidebar on setup and
is a perfectly ordinary dashboard afterwards: editable, deletable, and never
rewritten once created.

Four constraints shape it:

* **Stock cards only.** ApexCharts and mini-graph-card make far prettier plots,
  but they are separate HACS installs, and a dashboard that renders as a column
  of red "Custom element doesn't exist" boxes is worse than no dashboard. The
  forward plan is therefore drawn as a Markdown table from the plan sensor's
  attributes, which needs nothing installed.
* **Every card is optional.** Entities can be disabled, and a card referencing
  a missing entity shows an error row. Cards are built from the keys that
  resolved and dropped entirely when nothing in them did, so a partial install
  degrades to a smaller dashboard rather than a broken one.
* **Every row is renamed.** Entity friendly names carry the device name, so
  left alone every single row reads "AI ESS Controller Planned action" and a
  glance card truncates all four of its columns to "AI ESS Controll...". The
  device name is redundant on a dashboard that is entirely about that device, so
  ``LABELS`` gives each key a short name and every reference uses it. This is the
  difference between a wall of identical grey text and something readable.
* **Sections, not masonry.** The default masonry view packs cards into ragged
  columns by height, which looks like a filing error. ``type: sections`` lays
  them out on a grid with headings, and puts the four numbers you actually watch
  into badges along the top.

Home Assistant-free: it takes a mapping of entity key to entity id and returns
plain dictionaries, so the whole structure is testable without a running
instance.
"""

from __future__ import annotations

from typing import Any

DASHBOARD_URL_PATH = "ess-controller"
DASHBOARD_TITLE = "ESS Controller"
DASHBOARD_ICON = "mdi:home-battery-outline"

# The Plan view shows the *whole* horizon, however far that reaches.
#
# It used to show twelve slots -- six hours -- for no better reason than that a
# short table reads well on a phone. But Octopus publishes to 23:00 tomorrow,
# AgilePredict forecasts a fortnight, and the optimiser plans across the entire
# horizon: capping the display at six hours threw away most of what the system
# knew and hid the decision that matters, which is usually tomorrow's cheap
# window rather than the next half-hour. Rows are grouped by day so a 72-row
# table stays navigable.
PLAN_TABLE_SLOTS: int | None = None

# The Overview keeps a short one, because it is a summary and because sections are
# grid items: a row does not start until the tallest section above it ends, so a
# full-horizon table there would leave a table-high blank beside every neighbour.
OVERVIEW_TABLE_SLOTS = 6

PLACEHOLDER_VIEW_PATH = "waiting"

# Prefixed to the plan's reason whenever an outage hold is narrowing the window it
# can plan in. A storm hold explains an entire day of otherwise baffling behaviour --
# a state of charge pinned flat, an evening bought at the top of the tariff -- so it
# has to survive being skim-read at the top of a card full of prices.
OUTAGE_HOLD_MARK = "⚠️"

# Planning slots are half-hours, matching how the tariff is published, so a rate
# in kW is this many hours' worth of energy.
SLOT_HOURS = 0.5

# Short names, one per entity key.
#
# Home Assistant builds a friendly name by joining the device name to the entity
# name, which is right in a list of every entity in the house and wrong on a
# dashboard where every card belongs to the same device: "AI ESS Controller
# Planned action" wastes the first eighteen characters of every row, and any card
# that lays entities out in columns truncates all of them to the useless prefix.
# These are deliberately terse -- the heading above a card supplies the context
# the entity name would otherwise have to carry.
LABELS: dict[str, str] = {
    # What it is doing
    "plan_action": "Doing now",
    # NOT the next half-hour. The sensor reports the next slot whose action
    # *differs* from the current one, which can be hours away -- so it can never
    # equal "Doing now", and labelling it "Next half-hour" made two correct
    # sensors look like they contradicted each other.
    "next_action": "Next change",
    "control_status": "Control",
    # All of these are the *plan's* figures for this half-hour, and every one of
    # them says so. The section heading was meant to carry that ("This half-hour"),
    # and it does not: "Charge power 0.00 kW" read beside a battery visibly taking
    # 619 W from the array looks like a broken sensor rather than a statement about
    # what was scheduled. Same mistake as labelling the plan's projected SoC "SoC".
    "target_soc": "Planned target SoC",
    "planned_charge_power": "Planned charge power",
    "planned_discharge_power": "Planned discharge power",
    "charging_planned": "Charging planned",
    "discharging_planned": "Discharging planned",
    "exporting_planned": "Exporting planned",
    # Money
    "import_price": "Import price",
    "export_price": "Export price",
    "cheap_slot": "Cheap slot",
    "plan_cost": "Cost over the horizon",
    "plan_saving": "Saving vs self-use",
    "weekly_saving": "Saving this week",
    "tariff_recommendation": "Best tariff for you",
    # Energy
    "planned_grid_import": "Planned grid import",
    "planned_grid_export": "Planned grid export",
    "solar_forecast_today": "Solar left today",
    "solar_forecast_tomorrow": "Solar tomorrow",
    "load_forecast_today": "Load left today",
    "load_forecast_tomorrow": "Load tomorrow",
    # Battery
    "battery_soc": "State of charge",
    "usable_capacity": "Usable capacity",
    "wear_allowance": "Wear in use",
    "learning_progress": "Learning progress",
    # Health
    "plan_valid": "Plan",
    "write_verified": "Inverter writes",
    "control_active": "Controlling",
    "inverter_available": "Inverter link",
    # Events
    "grid_session": "Grid session",
    "session_active": "Running now",
    "free_electricity_now": "Free electricity",
    "outage_risk": "Outage risk",
    "outage_risk_active": "Outage expected",
    "shifted_loads": "Scheduled loads",
    "flexible_load_running": "Load due now",
    # Switches
    "optimiser_enabled": "Optimiser",
    "inverter_control": "Write to inverter",
    "allow_grid_charge": "Charge from the grid",
    "allow_export": "Export",
    "allow_battery_export": "Battery export",
    "sessions_enabled": "Act on grid sessions",
    "shifting_enabled": "Shift flexible loads",
    "appliance_control": "Switch appliances",
    "outage_protection": "Outage protection",
    "derive_wear_from_cost": "Derive from pack cost",
    # Numbers
    "min_soc": "Minimum charge",
    "max_soc": "Maximum charge",
    "reserve_soc": "Emergency reserve",
    "max_charge_power": "Charge limit",
    "max_discharge_power": "Discharge limit",
    "cycle_cost": "Wear allowance",
    "battery_cost": "Pack cost",
    "battery_expected_cycles": "Expected cycles",
    "battery_residual_value": "Residual value",
    "default_daily_load": "Typical daily use",
    "cooling_threshold": "Cooling above",
    "cooling_rate": "Cooling per °C",
    "heating_threshold": "Heating below",
    "heating_rate": "Heating per °C",
    # Controls
    "strategy": "Strategy",
    "replan": "Re-plan now",
    "clear_override": "Clear override",
    "reset_learning": "Reset learning",
    "recommend_tariffs": "Compare tariffs",
    "create_dashboard": "Rebuild dashboard",
}


def label(key: str) -> str:
    """The short name for a key, falling back to something readable.

    A key with no entry is a bug rather than a crash: a new entity that nobody
    remembered to name here should show up as "Solar clipping", not break the
    dashboard build and take the sidebar entry with it.
    """
    return LABELS.get(key) or key.replace("_", " ").capitalize()


def placeholder_dashboard(title: str = DASHBOARD_TITLE) -> dict[str, Any]:
    """A dashboard to register when there are no entities yet.

    Registering nothing until entities exist is what made a failure invisible:
    no sidebar entry, and no way to tell "still starting up" from "the
    integration did not set up at all". A single view that says so is worth more
    than an absence, and it is replaced by the real dashboard as soon as there is
    something to build one from.
    """
    return {
        "title": title,
        "views": [
            {
                "title": "Starting up",
                "path": PLACEHOLDER_VIEW_PATH,
                "icon": "mdi:progress-clock",
                "cards": [
                    {
                        "type": "markdown",
                        "content": (
                            "## Waiting for entities\n\n"
                            "The ESS Controller dashboard is registered, but the "
                            "integration has not published any entities yet. It "
                            "will fill itself in within a minute or so.\n\n"
                            "If this page still looks like this after that:\n\n"
                            "- Check **Settings > Devices & services > AI ESS "
                            "Controller** for a setup error.\n"
                            "- Look in **Developer tools > States** for entities "
                            "beginning `sensor.ess`.\n"
                            "- Press **Rebuild dashboard** on the integration's "
                            "device page to try again and get a notification "
                            "saying what happened."
                        ),
                    }
                ],
            }
        ],
    }


# Stamped into every generated dashboard so a later version can tell its own
# untouched output apart from a dashboard the user has since edited.
GENERATED_KEY = "ess_controller_generated"


def fingerprint(config: Any) -> str:
    """A stable hash of a dashboard's views, ignoring our own stamp.

    Comparing the stored dashboard against a hash of what we last wrote is what
    lets an upgrade improve the dashboard *and* keep the promise that edits are
    never lost. Without it every improvement needed the user to delete the
    dashboard by hand -- which they had to be told, three times.
    """
    import hashlib
    import json

    if not isinstance(config, dict):
        return ""
    subset = {key: value for key, value in config.items() if key != GENERATED_KEY}
    encoded = json.dumps(subset, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def stamp(config: dict[str, Any]) -> dict[str, Any]:
    """Record our fingerprint inside the config we are about to store."""
    stamped = dict(config)
    stamped.pop(GENERATED_KEY, None)
    stamped[GENERATED_KEY] = fingerprint(stamped)
    return stamped


def is_untouched(stored: Any) -> bool:
    """Whether a stored dashboard is exactly what we last generated.

    True means safe to replace: nobody has edited it since. False means either the
    user changed something or it predates stamping, and in both cases it is left
    alone -- a wrong answer here destroys somebody's work, so the doubtful case
    must be "leave it".
    """
    if not isinstance(stored, dict):
        return False
    claimed = stored.get(GENERATED_KEY)
    if not isinstance(claimed, str) or not claimed:
        return False
    return claimed == fingerprint(stored)


def is_placeholder(config: Any) -> bool:
    """Whether a stored configuration is the placeholder above.

    Used to decide whether replacing it is safe: overwriting the placeholder is
    the whole point, overwriting the user's edited dashboard never is.
    """
    if not isinstance(config, dict):
        return False
    views = config.get("views")
    if not isinstance(views, list) or len(views) != 1:
        return False
    return isinstance(views[0], dict) and views[0].get("path") == PLACEHOLDER_VIEW_PATH


def dashboards_mapping(data: Any) -> dict[str, Any] | None:
    """Lovelace's url_path -> dashboard mapping, whichever shape it is in.

    It was a plain dict under ``hass.data["lovelace"]["dashboards"]`` for years
    and is an attribute of a dataclass in newer releases. Lives here rather than
    beside the installer so the shape handling is testable without Home
    Assistant.
    """
    if data is None:
        return None
    found = (
        data.get("dashboards")
        if isinstance(data, dict)
        else getattr(data, "dashboards", None)
    )
    return found if isinstance(found, dict) else None


def _compact(cards: list[Any | None]) -> list[Any]:
    return [card for card in cards if card]


# ---------------------------------------------------------------- single cards


def _entities_card(
    keys: list[str], resolved: dict[str, str], **extra: Any
) -> dict[str, Any] | None:
    """An entities card containing whichever of ``keys`` exist.

    No ``title``: the section heading above it says what it is, and a card title
    under a heading reads as the same words twice. Dense numeric lists are the one
    place rows beat tiles -- fourteen tiles of settings is a lot of scrolling.
    """
    rows = [
        {"entity": resolved[key], "name": label(key)} for key in keys if key in resolved
    ]
    if not rows:
        return None
    card: dict[str, Any] = {"type": "entities", "entities": rows, "state_color": True}
    card.update(extra)
    return card


def _gauge(key: str, resolved: dict[str, str], **extra: Any) -> dict[str, Any] | None:
    if key not in resolved:
        return None
    card = {"type": "gauge", "entity": resolved[key], "name": label(key)}
    card.update(extra)
    return card


def _tile(key: str, resolved: dict[str, str], **extra: Any) -> dict[str, Any] | None:
    """A tile card, which is the card the modern Home Assistant look is made of.

    Icon, short name and state on one line, coloured by state, tappable for more
    info. Where a tile drives something -- a switch, the strategy select -- the
    control goes *inline* on the same row rather than as a second line, so a
    column of them stays a column rather than becoming a ladder.
    """
    if key not in resolved:
        return None
    card: dict[str, Any] = {
        "type": "tile",
        "entity": resolved[key],
        "name": label(key),
    }
    card.update(extra)
    return card


def _tiles(
    keys: list[str], resolved: dict[str, str], **extra: Any
) -> list[dict[str, Any]]:
    return _compact([_tile(key, resolved, **extra) for key in keys])


def _toggles(keys: list[str], resolved: dict[str, str]) -> list[dict[str, Any]]:
    """Prominent switches, as tiles with the toggle on the same row.

    A tile with inline features reports ``min_columns: 12``, so each one takes a
    whole row of its section. That is the right trade for two or three switches
    someone reaches for often, and the wrong one for a list of ten -- those go in
    an entities card instead.
    """
    return _tiles(
        keys,
        resolved,
        features=[{"type": "toggle"}],
        features_position="inline",
        hide_state=True,
    )


def _select(key: str, resolved: dict[str, str]) -> dict[str, Any] | None:
    # ``hide_state``: the dropdown underneath already shows the current option,
    # and without this the tile prints it immediately above as well.
    return _tile(
        key,
        resolved,
        features=[{"type": "select-options"}],
        hide_state=True,
        grid_options={"columns": "full"},
    )


def _actions(keys: list[str], resolved: dict[str, str]) -> list[dict[str, Any]]:
    """Tiles that press a button entity.

    Spelled out as ``perform-action`` rather than left to ``toggle``: pressing is
    not toggling, and a tile whose tap does nothing is worse than no tile.
    """
    cards: list[dict[str, Any]] = []
    for key in keys:
        if key not in resolved:
            continue
        cards.append(
            {
                "type": "tile",
                "entity": resolved[key],
                "name": label(key),
                "hide_state": True,
                "tap_action": {
                    "action": "perform-action",
                    "perform_action": "button.press",
                    "target": {"entity_id": resolved[key]},
                },
            }
        )
    return cards


def _markdown(content: str) -> dict[str, Any]:
    # Prose and tables want the whole width; left to the grid they would sit in a
    # half-width column with a table scrolling sideways inside them.
    return {
        "type": "markdown",
        "content": content,
        "grid_options": {"columns": "full"},
    }


def _badge(key: str, resolved: dict[str, str], **extra: Any) -> dict[str, Any] | None:
    if key not in resolved:
        return None
    badge: dict[str, Any] = {
        "type": "entity",
        "entity": resolved[key],
        "name": label(key),
        "show_name": True,
        "state_content": "state",
    }
    badge.update(extra)
    return badge


# ------------------------------------------------------------------- sections


def _section(heading: str, icon: str, cards: list[Any | None]) -> dict[str, Any] | None:
    """A grid section with a heading, or nothing if it would be empty.

    The heading is a card, so it cannot be what keeps a section alive: a section
    of nothing but its own title is exactly the sort of thing that makes a
    generated dashboard look generated.
    """
    real = _compact(cards)
    if not real:
        return None
    return {
        "type": "grid",
        "cards": [
            {"type": "heading", "heading": heading, "icon": icon},
            *real,
        ],
    }


def _view(
    title: str,
    path: str,
    icon: str,
    sections: list[dict[str, Any] | None],
    badges: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "type": "sections",
        "title": title,
        "path": path,
        "icon": icon,
        "max_columns": 3,
        "sections": _compact(sections),
    }
    if badges:
        real = _compact(badges)
        if real:
            view["badges"] = real
    return view


def has_content(view: dict[str, Any]) -> bool:
    """Whether a view is worth putting in the tab bar."""
    return bool(view.get("sections") or view.get("cards"))


# How wide the price bars are drawn, in characters. Twelve reads clearly on a
# phone and leaves room for the numbers beside it.
BAR_WIDTH = 12


# Eight levels of block character, which is enough to read a price curve at a
# glance and few enough that each step is visually distinct.
SPARK_LEVELS = "▁▂▃▄▅▆▇█"


# ApexCharts Card is a HACS front-end card, so it cannot be a requirement: this
# dashboard has to render on a stock install. When it *is* installed there is no
# reason to keep drawing block characters, so the chart sections are swapped for
# real ones. ``APEX_RESOURCE`` is the substring to look for in Lovelace's
# registered resources.
APEX_CARD = "custom:apexcharts-card"
APEX_RESOURCE = "apexcharts-card"


# What each planned action is called in the plan table. The raw enum values with
# the underscores swapped for spaces read as jargon and, worse, as instructions
# the controller is issuing: "Charge" gives no clue that the energy is being
# bought, and "Idle" sounds like nothing is being decided when in fact the
# battery is being held shut while the house runs off the grid.
#
# These are the *observed* mix, not modes: the optimiser labels a charging slot
# by where the energy came from after it has decided how much to move.

# A display-only refinement of ``idle``, never emitted by the optimiser. It is
# deliberately not a ``SlotAction``: nothing decides it, the table derives it
# from what the slot buys. Kept distinct from every enum value so the mapping in
# ``_action_expr`` cannot collide with a real action.
IDLE_ON_SOLAR = "idle_on_solar"

ACTION_WORDS = {
    "charge": "Grid charge",
    "charge_solar_only": "Solar charge",
    "discharge": "Forced discharge",
    "self_use": "Self use",
    "idle": "Hold (house on grid)",
    # A hold that buys nothing, which the plain "idle" wording described as
    # putting the house on the grid. On a sunny half-hour at 45p that reads as
    # the controller burning money at the top of the tariff, when the import
    # column beside it says 0.00 and the house is running on sunshine. Not a
    # real action -- the optimiser only ever emits ``idle`` -- but the table
    # picks between the two by what the slot actually buys.
    IDLE_ON_SOLAR: "Hold (sun covers the house)",
}


# What each state does, and why the optimiser picks it. Two short columns: any
# more and it stops being a legend and starts being documentation nobody reads.
#
# Keyed by the same values as ``ACTION_WORDS`` so the legend cannot drift out of
# step with the table above it, and so a new action cannot ship undocumented.
ACTION_NOTES: dict[str, tuple[str, str]] = {
    "charge": (
        "Buys from the grid to fill the battery",
        "This half-hour is cheaper than the ones it saves",
    ),
    "charge_solar_only": (
        "Surplus sun goes in; battery still covers the house",
        "Free energy, and room to keep it",
    ),
    "self_use": (
        "Battery covers whatever the sun does not",
        "Stored energy is worth more than this price",
    ),
    # The one people ask about, because on a sunny half-hour it can look like
    # Solar charge. The difference is the direction that is blocked: a hold will
    # not *discharge*, but it does not shut the array out either.
    #
    # "The grid is cheap" was wrong as often as it was right. A hold is chosen
    # because the charge earns more in a later half-hour than it saves in this
    # one, which is a comparison, not a price -- and printing it beside a 45.4p
    # slot made the plan look deranged rather than patient.
    "idle": (
        "Will not discharge; sun may still top it up",
        "The charge saves more in a later half-hour than in this one",
    ),
    IDLE_ON_SOLAR: (
        "Nothing to do: the sun is meeting the house",
        "No shortfall to cover, and nothing bought",
    ),
    "discharge": (
        "Empties past what the house needs",
        "Making room, or export pays more than holding",
    ),
}


def _action_legend() -> str:
    """A short table of what each planned state means."""
    rows = "".join(
        f"| {ACTION_WORDS[key]} | {does} | {why} |\n"
        for key, (does, why) in ACTION_NOTES.items()
    )
    return "| State | What happens | Why |\n|---|---|---|\n" + rows


def _action_expr(var: str) -> str:
    """A Jinja expression mapping a raw action value to its wording."""
    pairs = ", ".join(f"'{key}': '{value}'" for key, value in ACTION_WORDS.items())
    return (
        "{{ {" + pairs + "}.get(" + var + ", " + var + " | replace('_', ' ') "
        "| capitalize) }}"
    )


def _doing_expr(slot: str = "slot") -> str:
    """The wording for one slot's action, split by what it actually buys.

    Only ``idle`` is ambiguous. It covers both "the battery is held while the
    house buys" and "the sun is meeting the house and there is nothing to do",
    and the single label described every one of them as the former. A real plan
    showed two consecutive holds at 38.2p and 45.4p reading "Hold (house on
    grid)" while importing 0.00 kWh, with solar comfortably ahead of the load --
    which is the opposite of what the row said, at the scariest moment to say it.
    """
    return (
        "{% set bought = " + slot + ".grid_import_kwh | default(0) | float(0) %}"
        "{% set doing = '"
        + IDLE_ON_SOLAR
        + "' if ("
        + slot
        + ".action == 'idle' and bought <= 0.005) else "
        + slot
        + ".action %}"
        + _action_expr("doing")
    )


# How far forward the charts look. The horizon is at most 48 hours and usually
# less; anything past the plan simply draws no points.
CHART_SPAN_HOURS = 48

# One hue per chart, stepped by value rather than mixed with a second hue: price
# is a magnitude, so it gets a sequential ramp, and a negative price gets the one
# colour that is not on that ramp because it is a different kind of thing.
PRICE_COLOURS = (
    (0.0, "#3d8f5b"),  # paid to import: the only slot worth a different hue
    (15.0, "#8ecfa5"),
    (25.0, "#5aa8d6"),
    (35.0, "#3f6fae"),
    (50.0, "#2b3f7a"),
)
SOC_COLOUR = "#7a5cb8"


def _apex_price_chart(plan_entity: str) -> dict[str, Any]:
    """Import price over the whole horizon, as columns.

    Columns rather than a line because a price is a value that *holds* for a
    half-hour rather than a point that interpolates between neighbours, and
    reading the trough off a stepped shape is what the chart is for.

    The data comes from the plan sensor's ``slots`` attribute rather than from
    recorded history, which is the only way to draw the future at all.
    """
    return {
        "type": APEX_CARD,
        "graph_span": f"{CHART_SPAN_HOURS}h",
        # Start the window at the current minute so the chart runs forwards.
        # Left alone, the card plots the graph_span *ending* now and the whole
        # plan falls off the right-hand edge.
        "span": {"start": "minute"},
        "header": {"show": True, "title": label("import_price"), "show_states": False},
        "now": {"show": True, "label": "now"},
        "experimental": {"color_threshold": True},
        "yaxis": [{"decimals": 0, "apex_config": {"title": {"text": "p/kWh"}}}],
        "apex_config": {
            "chart": {"height": 260},
            "legend": {"show": False},
            "grid": {"borderColor": "rgba(127,127,127,0.25)"},
            "xaxis": {"labels": {"datetimeUTC": False}},
        },
        "series": [
            {
                "entity": plan_entity,
                "name": label("import_price"),
                "type": "column",
                "unit": "p",
                "float_precision": 1,
                "color_threshold": [
                    {"value": value, "color": colour} for value, colour in PRICE_COLOURS
                ],
                "data_generator": (
                    "return (entity.attributes.slots || []).map(s => "
                    "[new Date(s.start).getTime(), s.import_price]);"
                ),
            }
        ],
        "grid_options": {"columns": "full"},
    }


def _apex_soc_chart(plan_entity: str, soc_entity: str | None) -> dict[str, Any]:
    """Where the plan expects the battery to be, half-hour by half-hour.

    Deliberately its own chart rather than a second axis on the price chart. A
    percentage and a price share no scale, and a chart with two y-axes invites
    reading a crossing point that means nothing.
    """
    series: list[dict[str, Any]] = [
        {
            "entity": plan_entity,
            "name": "Planned",
            "type": "line",
            "curve": "stepline",
            "unit": "%",
            "float_precision": 0,
            "color": SOC_COLOUR,
            "stroke_width": 2,
            "data_generator": (
                "return (entity.attributes.slots || []).map(s => "
                "[new Date(s.end).getTime(), s.soc_end]);"
            ),
        }
    ]
    if soc_entity:
        # The measured SoC up to now, so the projection is read against where the
        # battery actually is rather than in isolation.
        series.append(
            {
                "entity": soc_entity,
                "name": "Measured",
                "type": "line",
                "unit": "%",
                "float_precision": 0,
                "stroke_width": 2,
                "extend_to": "now",
            }
        )
    return {
        "type": APEX_CARD,
        "graph_span": f"{CHART_SPAN_HOURS}h",
        # Half a day of measured SoC behind the projection is enough context to
        # see whether the plan is starting from where it thought it would.
        "span": {"start": "minute", "offset": "-12h"},
        "header": {"show": True, "title": "Battery", "show_states": False},
        "now": {"show": True, "label": "now"},
        "yaxis": [{"min": 0, "max": 100, "decimals": 0}],
        "apex_config": {
            "chart": {"height": 220},
            "grid": {"borderColor": "rgba(127,127,127,0.25)"},
            "xaxis": {"labels": {"datetimeUTC": False}},
        },
        "series": series,
        "grid_options": {"columns": "full"},
    }


def _plan_sparkline(plan_entity: str) -> str:
    """The whole horizon as one line of block characters.

    This is the shape, and the shape is what a person actually wants: where the
    trough is, how deep, and how far away. A table answers that only after
    reading seventy-two rows.

    A real line chart is not available -- ``history-graph`` plots recorded state
    and cannot draw the future, and ApexCharts is a separate HACS install this
    dashboard deliberately does not require -- so it is drawn with block
    characters in a code span, one per half-hour, with a break at midnight.
    Scaled between the cheapest and dearest slot on the horizon, so a negative
    price sits at the bottom of the scale where it belongs.
    """
    return (
        "{% set slots = state_attr('" + plan_entity + "', 'slots') %}"
        "{% if not slots %}No plan yet — waiting for prices.{% else %}"
        "{% set prices = slots | map(attribute='import_price') | list %}"
        "{% set low = prices | min %}{% set high = prices | max %}"
        "{% set span = (high - low) if high > low else 1 %}"
        "{% set bars = namespace(out='', day='') %}"
        "{% for slot in slots %}"
        "{% set day = as_timestamp(slot.start) | timestamp_custom('%j', true) %}"
        "{% if day != bars.day and not loop.first %}"
        "{% set bars.out = bars.out ~ '│' %}"
        "{% endif %}"
        "{% set bars.day = day %}"
        "{% set level = ((slot.import_price - low) / span * "
        + str(len(SPARK_LEVELS) - 1)
        + ") | round | int %}"
        "{% set bars.out = bars.out ~ '" + SPARK_LEVELS + "'[level] %}"
        "{% endfor %}"
        "`{{ bars.out }}`\n\n"
        "{{ '%.1f' | format(low) }}p to {{ '%.1f' | format(high) }}p over "
        "{{ (slots | count / 2) | round | int }} hours, from "
        "{{ as_timestamp(slots[0].start) | timestamp_custom('%H:%M', true) }}. "
        "│ marks midnight."
        "{% endif %}"
    )


def _plan_table(
    plan_entity: str, slots: int | None = PLAN_TABLE_SLOTS, *, bars: bool = True
) -> str:
    """The forward plan, one row per half-hour, in Markdown.

    A table of numbers is not a shape, and the shape is the whole point: what a
    person wants from the plan is to see where the trough is, not to read
    seventy-two prices. Where no chart is available -- ``history-graph`` plots
    recorded state and cannot draw the future, and ApexCharts is a separate HACS
    install this dashboard deliberately does not require -- the shape is drawn
    with block characters in a code span, which needs nothing installed and
    renders anywhere.

    ``bars`` turns that column off, for the case where ApexCharts *is* installed
    and the shape is already on the page as a real chart. The table then earns its
    place on the thing a chart cannot show: what the controller intends to do.

    Bars are scaled to the dearest slot on show, so the picture is always of
    *this* horizon rather than of some fixed price range. Negative prices get
    their own marker: they are the most valuable slots on the page and a
    zero-length bar would bury them.
    """
    limit = "" if slots is None else f"[:{slots}]"
    # "SoC" beside a live battery reading invites reading it as the battery's
    # state now. It is the plan's projection for the end of that half-hour.
    header = (
        "| Time | Price | | Doing | Buy | Planned SoC |\n|---|--:|---|---|--:|--:|\n"
        if bars
        else "| Time | Price | Doing | Buy | Planned SoC |\n|---|--:|---|--:|--:|\n"
    )
    # How much of the battery's charge this half-hour is actually bought.
    #
    # "Grid charge" is set whenever *any* of the charge has to come from the grid,
    # because even a sliver means the inverter needs grid charging permitted --
    # that is a real control difference and the label has to reflect it. But it
    # reads as though the whole slot is being bought, which at 43p is alarming and
    # wrong: a slot topping up from the sun and taking 0.05 kWh from the grid was
    # indistinguishable from one importing at full rate. The number is the answer,
    # so the number goes on the page.
    buy_cell = (
        "{% set rate = slot.charge_power_kw | default(0) | float(0) %}"
        "{% set charge = rate * " + str(SLOT_HOURS) + " %}"
        "{% set sun = slot.pv_kwh | default(0) | float(0) %}"
        "{% set used = slot.load_kwh | default(0) | float(0) %}"
        "{% set spare = [sun - used, 0] | max %}"
        "{% set bought = [charge - spare, 0] | max %}"
        "| {% if bought > 0.005 %}{{ '%.2f' | format(bought) }}{% else %}—{% endif %} "
    )
    bar_cell = (
        (
            "{% set filled = [(p / top * "
            + str(BAR_WIDTH)
            + ") | round | int, "
            + str(BAR_WIDTH)
            + "] | min if p > 0 else 0 %}"
            # A negative price is off the scale in the good direction, so it gets
            # its own fixed marker rather than a proportional bar.
            "| `{% if p < 0 %}◄◄{{ '·' * " + str(BAR_WIDTH - 2) + " }}"
            "{% else %}{{ '█' * filled }}{{ '·' * ("
            + str(BAR_WIDTH)
            + " - filled) }}{% endif %}` "
        )
        if bars
        else ""
    )
    return (
        "{% set slots = state_attr('" + plan_entity + "', 'slots') %}"
        "{% if not slots %}No plan yet — waiting for prices.{% else %}"
        "{% set shown = slots" + limit + " %}"
        "{% set prices = shown | map(attribute='import_price') | list %}"
        # Scaled to the dearest slot, not to the largest magnitude. Scaling by
        # absolute value gave a -5p slot a full-length bar, which reads as
        # expensive when it is the best half-hour on the page.
        "{% set dear = prices | select('gt', 0) | list %}"
        "{% set top = dear | max if dear else 1 %}"
        "{% set ns = namespace(day='') %}"
        "{% for slot in shown %}"
        # A new day starts a new table under its own heading. One long table is a
        # wall; the same rows broken by day are a schedule.
        "{% set day = as_timestamp(slot.start) | timestamp_custom('%a %-d %b', true) %}"
        "{% if day != ns.day %}"
        "{% if not loop.first %}\n{% endif %}"
        "**{{ day }}**\n\n" + header + "{% set ns.day = day %}"
        "{% endif %}"
        "{% set p = slot.import_price %}"
        "| {{ as_timestamp(slot.start) | timestamp_custom('%H:%M', true) }} "
        # A predicted price is marked rather than dressed up as announced: an
        # asterisk costs a character and is the difference between "the plan says
        # 4p at 2am" and "the plan guesses 4p at 2am".
        "| {{ '%.1f' | format(p) }}p"
        "{{ '*' if slot.price_is_forecast else '' }} "
        + bar_cell
        + "| "
        + _doing_expr()
        + " "
        + buy_cell
        + "| {{ '%.0f' | format(slot.soc_end) }}% |\n"
        "{% endfor %}"
        "\nkWh under **Buy** is what comes off the grid into the battery; the rest "
        "of a charge is sunshine.\n\n"
        "{% if prices | select('lt', 0) | list | count %}"
        "◄ paid to import. "
        "{% endif %}"
        "{% if shown | selectattr('price_is_forecast') | list | count %}"
        "\\* predicted, not yet announced by Octopus."
        "{% endif %}"
        "{% endif %}"
    )


def _plan_reason(plan_entity: str, next_entity: str | None = None) -> str:
    """Why the plan looks the way it does, and when it next changes.

    The "next change" sensor reports the next slot whose action *differs* from the
    current one, which can be hours away -- so on its own it reads as imminent
    when it is not. Saying when turns two apparently contradictory readings into
    one sentence.
    """
    when = (
        (
            "{% set nxt = '" + next_entity + "' %}"
            "{% set at = state_attr(nxt, 'starts') %}"
            "{% if at and states(nxt) not in ['unknown', 'unavailable'] %}\n\n"
            "**Next change:** {{ states(nxt) | replace('_', ' ') | capitalize }} at "
            "{{ as_timestamp(at) | timestamp_custom('%H:%M', true) }}"
            "{% if state_attr(nxt, 'import_price') is not none %}"
            ", {{ '%.1f' | format(state_attr(nxt, 'import_price')) }}p"
            "{% endif %}."
            "{% endif %}"
        )
        if next_entity
        else ""
    )
    return (
        "{% set reason = state_attr('" + plan_entity + "', 'reason') %}"
        "{% if reason %}**Why this plan:** {{ reason }}"
        "{% else %}No plan yet — waiting for prices.{% endif %}" + when
    )


def _wear_workings(wear_entity: str) -> str:
    """Show which wear figure is in force and the thresholds it implies.

    The wear allowance is the most consequential number in the system and the
    least obvious, so the dashboard shows the workings rather than just the
    result.

    The two threshold names are the sensor's own: ``spread_needed_to_cycle`` and
    ``negative_price_to_dump_and_reimport``. Shortening them here to something
    more readable is what made this card render "Cycling pays above a **?p**
    spread" -- a template naming an attribute that does not exist fails silently,
    and a unit test written against invented attributes agrees with it.
    """
    return (
        "{% set e = '" + wear_entity + "' %}"
        "**{{ states(e) }} p/kWh** "
        "-- {{ state_attr(e, 'source') or 'unknown source' }}\n\n"
        "{% if state_attr(e, 'net_cost') %}"
        "Pack cost {{ state_attr(e, 'net_cost') | round(2) }} over "
        "{{ state_attr(e, 'expected_cycles') | round | int }} cycles of "
        "{{ state_attr(e, 'usable_kwh') }} kWh = "
        "{{ state_attr(e, 'lifetime_throughput_kwh') | round | int }} kWh "
        "of throughput.\n\n"
        "{% endif %}"
        "{% set spread = state_attr(e, 'spread_needed_to_cycle') %}"
        "{% set dump = state_attr(e, 'negative_price_to_dump_and_reimport') %}"
        "{% if spread is not none %}"
        "Cycling pays above a **{{ spread }}p** spread. "
        "{% endif %}"
        "{% if dump is not none %}"
        "Dumping to re-import pays below **{{ dump }}p**."
        "{% endif %}"
    )


def _performance_report(saving_entity: str) -> str:
    """The weekly report, laid out from the summary attributes.

    Units live *inside* each conditional. Putting them outside is the obvious
    way to write it and renders a missing value as "--%", which reads like a
    broken template rather than "not enough data yet".
    """
    return (
        "{% set e = '" + saving_entity + "' %}"
        "{% set money = state_attr(e, 'money') %}"
        "{% set control = state_attr(e, 'control') %}"
        "{% set err = state_attr(e, 'forecast_error_kwh_per_slot') %}"
        "{% set window = state_attr(e, 'window') %}"
        "{% if not money %}"
        "Nothing recorded yet -- this fills in as half-hours complete."
        "{% else %}"
        "| | |\n|---|--:|\n"
        "| Spent | {{ money.actual }}p |\n"
        "| With no battery | {{ money.if_no_battery }}p |\n"
        "| With self-use only | "
        "{{ (money.if_self_use_only ~ 'p') if money.if_self_use_only is not none "
        "else 'not yet' }} |\n"
        "| Saving vs self-use | "
        "{{ (money.saving_vs_self_use ~ 'p') if money.saving_vs_self_use is not none "
        "else 'not yet' }} |\n"
        "| Wear charged | {{ money.wear_cost }}p |\n"
        "| **Net saving** | **"
        "{{ (money.net_saving_vs_self_use ~ 'p') if money.net_saving_vs_self_use "
        "is not none else 'not yet' }}** |\n"
        "| Solar forecast error | "
        "{{ (err.solar_mae ~ ' kWh/slot') if err.solar_mae is not none "
        "else 'not yet' }} |\n"
        "| Load forecast error | "
        "{{ (err.load_mae ~ ' kWh/slot') if err.load_mae is not none "
        "else 'not yet' }} |\n"
        "| Plan followed | "
        "{{ (((control.plan_fidelity * 100) | round | int) ~ '%') "
        "if control.plan_fidelity is not none else 'not yet' }} |\n"
        "| Round trip | "
        "{{ (((control.round_trip_efficiency * 100) | round | int) ~ '%') "
        "if control.round_trip_efficiency is not none else 'not yet' }} |\n"
        "| Slots recorded | {{ window.slots }} over {{ window.days }} days |\n"
        "{% for note in state_attr(e, 'notes') or [] %}\n> {{ note }}\n{% endfor %}"
        "{% endif %}"
    )


def _flexible_loads(shifted_entity: str) -> str:
    return (
        "{% set e = '" + shifted_entity + "' %}"
        "**{{ state_attr(e, 'advice') or 'nothing scheduled' }}**\n\n"
        "{% set placements = state_attr(e, 'placements') %}"
        "{% if placements %}"
        "| Load | Window | Costs | Switched |\n|---|---|--:|---|\n"
        "{% for p in placements %}"
        "| {{ p.name }} "
        "| {{ as_timestamp(p.start) | timestamp_custom('%H:%M', true) }} to "
        "{{ as_timestamp(p.end) | timestamp_custom('%H:%M', true) }} "
        "| {{ p.cost }}p "
        "| {{ 'yes' if p.switch else 'by hand' }} |\n"
        "{% endfor %}"
        "{% else %}No loads defined. Add them in the integration's options if you "
        "have a dishwasher, immersion or EV whose timing can move.{% endif %}"
    )


def _overview_view(resolved: dict[str, str]) -> dict[str, Any]:
    plan = resolved.get("plan_cost")
    return _view(
        "Overview",
        "overview",
        "mdi:home-lightning-bolt-outline",
        [
            _section(
                "Right now",
                "mdi:flash",
                [
                    _gauge(
                        "battery_soc",
                        resolved,
                        min=0,
                        max=100,
                        severity={"green": 40, "yellow": 20, "red": 0},
                        grid_options={"columns": 6},
                    ),
                    _tile(
                        "plan_action",
                        resolved,
                        icon="mdi:battery-charging-medium",
                        grid_options={"columns": 6},
                    ),
                    _tile("next_action", resolved, grid_options={"columns": 6}),
                    _tile("import_price", resolved, grid_options={"columns": 6}),
                    _markdown(_plan_reason(plan, resolved.get("next_action")))
                    if plan
                    else None,
                ],
            ),
            _section(
                "Next three hours",
                "mdi:clock-outline",
                [_markdown(_plan_table(plan, OVERVIEW_TABLE_SLOTS)) if plan else None],
            ),
            _section(
                "Control",
                "mdi:tune",
                [
                    *_toggles(["optimiser_enabled", "inverter_control"], resolved),
                    _select("strategy", resolved),
                    *_tiles(["control_active", "inverter_available"], resolved),
                    *_actions(["replan", "clear_override"], resolved),
                ],
            ),
            _section(
                "Today",
                "mdi:calendar-today",
                [
                    _entities_card(
                        [
                            "plan_cost",
                            "plan_saving",
                            "planned_grid_import",
                            "planned_grid_export",
                            "solar_forecast_today",
                            "load_forecast_today",
                            "solar_forecast_tomorrow",
                            "load_forecast_tomorrow",
                        ],
                        resolved,
                    )
                ],
            ),
            _section(
                "Watch out for",
                "mdi:alert-outline",
                _tiles(
                    [
                        "plan_valid",
                        "write_verified",
                        "grid_session",
                        "free_electricity_now",
                        "outage_risk",
                    ],
                    resolved,
                ),
            ),
        ],
        badges=[
            _badge("battery_soc", resolved),
            _badge("import_price", resolved),
            _badge("plan_action", resolved),
            _badge("control_status", resolved),
        ],
    )


def _plan_view(resolved: dict[str, str], charts: bool = False) -> dict[str, Any]:
    plan = resolved.get("plan_cost")
    history = [
        {"entity": resolved[key], "name": label(key)}
        for key in ("import_price", "battery_soc")
        if key in resolved
    ]
    return _view(
        "Plan",
        "plan",
        "mdi:chart-timeline-variant",
        # Sections fill columns in order, so a twelve-row table declared first
        # leaves a column-high gap beside it. Short sections first, the table
        # last: it then owns the third column and the rest pack into one and two.
        [
            _section(
                "Prices",
                "mdi:currency-gbp",
                _tiles(["import_price", "export_price", "cheap_slot"], resolved),
            ),
            _section(
                "This half-hour",
                "mdi:battery-charging",
                [
                    _entities_card(
                        [
                            "plan_action",
                            "planned_charge_power",
                            "planned_discharge_power",
                            "target_soc",
                            "charging_planned",
                            "discharging_planned",
                            "exporting_planned",
                        ],
                        resolved,
                    )
                ],
            ),
            _section(
                "Price shape",
                "mdi:chart-bell-curve-cumulative",
                [
                    (
                        _apex_price_chart(plan)
                        if charts
                        else _markdown(_plan_sparkline(plan))
                    )
                    if plan
                    else None
                ],
            ),
            _section(
                "Battery",
                "mdi:battery-clock-outline",
                [
                    _apex_soc_chart(plan, resolved.get("battery_soc"))
                    if plan and charts
                    else None
                ],
            ),
            _section(
                "The whole plan",
                "mdi:clock-outline",
                [_markdown(_plan_table(plan, bars=not charts)) if plan else None],
            ),
            _section(
                "What the states mean",
                "mdi:help-circle-outline",
                # Only alongside the table it explains. A legend on a dashboard
                # with no plan on it is furniture.
                [_markdown(_action_legend()) if plan else None],
            ),
            _section(
                "Last 24 hours",
                "mdi:chart-line",
                [
                    # A history graph is the one chart stock cards do well, and it
                    # is the context the forward table lacks: is today dear or
                    # cheap?
                    {
                        "type": "history-graph",
                        "hours_to_show": 24,
                        "entities": history,
                        "grid_options": {"columns": "full"},
                    }
                    if history
                    else None
                ],
            ),
            _section(
                "Permissions",
                "mdi:key-outline",
                _toggles(
                    ["allow_grid_charge", "allow_export", "allow_battery_export"],
                    resolved,
                ),
            ),
        ],
        badges=[
            _badge("import_price", resolved),
            _badge("plan_action", resolved),
            _badge("target_soc", resolved, name="Target"),
        ],
    )


def _performance_view(resolved: dict[str, str]) -> dict[str, Any]:
    weekly = resolved.get("weekly_saving")
    wear = resolved.get("wear_allowance")
    return _view(
        "Performance",
        "performance",
        "mdi:chart-box-outline",
        [
            _section(
                "This week",
                "mdi:cash-multiple",
                [_markdown(_performance_report(weekly)) if weekly else None],
            ),
            _section(
                "Wear",
                "mdi:battery-heart-variant",
                [
                    _markdown(_wear_workings(wear)) if wear else None,
                    _entities_card(
                        ["battery_soc", "usable_capacity", "wear_allowance"], resolved
                    ),
                ],
            ),
            _section(
                "Learning",
                "mdi:school-outline",
                [
                    _gauge(
                        "learning_progress",
                        resolved,
                        min=0,
                        max=100,
                        severity={"red": 0, "yellow": 33, "green": 66},
                        grid_options={"columns": 6},
                    ),
                    _tile("tariff_recommendation", resolved),
                    *_actions(["recommend_tariffs", "reset_learning"], resolved),
                ],
            ),
            _section(
                "Export the history",
                "mdi:file-download-outline",
                [
                    _markdown(
                        "Run the **ess_controller.export_performance** action from "
                        "Developer tools → Actions to get the recorded half-hours "
                        "out as a summary, as structured data, or as a CSV file "
                        "written to `config/ess_controller/`. That file is what to "
                        "hand to a spreadsheet — or to an AI assistant — and ask "
                        "what to change."
                    )
                    # Prose follows substance: instructions for exporting a history
                    # that cannot exist would be the only thing on the page.
                    if weekly or wear
                    else None
                ],
            ),
        ],
        badges=[
            _badge("weekly_saving", resolved),
            _badge("wear_allowance", resolved),
            _badge("learning_progress", resolved),
        ],
    )


def _loads_view(resolved: dict[str, str]) -> dict[str, Any]:
    shifted = resolved.get("shifted_loads")
    return _view(
        "Loads & events",
        "loads",
        "mdi:washing-machine",
        [
            _section(
                "Flexible loads",
                "mdi:clock-fast",
                [
                    _markdown(_flexible_loads(shifted)) if shifted else None,
                    *_toggles(["shifting_enabled", "appliance_control"], resolved),
                    *_tiles(["shifted_loads", "flexible_load_running"], resolved),
                ],
            ),
            _section(
                "Grid incentives",
                "mdi:transmission-tower",
                [
                    *_toggles(["sessions_enabled"], resolved),
                    *_tiles(
                        ["grid_session", "session_active", "free_electricity_now"],
                        resolved,
                    ),
                ],
            ),
            _section(
                "Power cuts",
                "mdi:power-plug-off-outline",
                [
                    *_toggles(["outage_protection"], resolved),
                    *_tiles(["outage_risk", "outage_risk_active"], resolved),
                ],
            ),
        ],
        badges=[
            _badge("shifted_loads", resolved),
            _badge("grid_session", resolved),
            _badge("outage_risk", resolved),
        ],
    )


def _settings_view(resolved: dict[str, str]) -> dict[str, Any]:
    numbers = _entities_card(
        [
            "min_soc",
            "max_soc",
            "reserve_soc",
            "max_charge_power",
            "max_discharge_power",
        ],
        resolved,
    )
    wear = _entities_card(
        [
            "derive_wear_from_cost",
            "battery_cost",
            "battery_expected_cycles",
            "battery_residual_value",
            "cycle_cost",
            "wear_allowance",
        ],
        resolved,
    )
    load = _entities_card(
        [
            "default_daily_load",
            "cooling_threshold",
            "cooling_rate",
            "heating_threshold",
            "heating_rate",
        ],
        resolved,
    )
    # Ten switches as inline tiles is ten full-width rows and a column twice the
    # height of everything beside it. An entities card puts each on one compact
    # row with its toggle at the right, which is what this list wants to be.
    behaviour = [
        _select("strategy", resolved),
        _entities_card(
            [
                "optimiser_enabled",
                "inverter_control",
                "allow_grid_charge",
                "allow_export",
                "allow_battery_export",
                "sessions_enabled",
                "shifting_enabled",
                "appliance_control",
                "outage_protection",
            ],
            resolved,
        ),
    ]
    anything = bool(_compact([numbers, wear, load]) or _compact(behaviour))
    return _view(
        "Settings",
        "settings",
        "mdi:tune-variant",
        [
            _section("Battery limits", "mdi:battery-70", [numbers]),
            _section("Wear", "mdi:battery-heart-variant", [wear]),
            _section("Load model", "mdi:home-thermometer-outline", [load]),
            _section("Behaviour", "mdi:robot-outline", behaviour),
            _section(
                "Deeper settings",
                "mdi:cog-outline",
                [
                    _markdown(
                        "Entities, tariff sources, forecast sources, flexible load "
                        "definitions and outage thresholds live in **Settings → "
                        "Devices & services → AI ESS Controller → Configure**. "
                        "Everything on this page takes effect on the next plan, "
                        "without a reload."
                    )
                    if anything
                    else None,
                    *_actions(
                        [
                            "replan",
                            "clear_override",
                            "reset_learning",
                            # Home Assistant files this away in a collapsed
                            # Configuration block on the device page, where the
                            # first person to need it could not find it.
                            "create_dashboard",
                        ],
                        resolved,
                    ),
                ],
            ),
        ],
    )


def build_dashboard(
    resolved: dict[str, str], title: str = DASHBOARD_TITLE, *, charts: bool = False
) -> dict[str, Any]:
    """Build the whole dashboard from the entity keys that resolved.

    ``resolved`` maps an entity key (``plan_action``, ``min_soc``, ...) to the
    entity id it ended up with, so the output is correct whatever the device was
    named and whichever entities the user disabled.

    ``charts`` says whether ApexCharts Card is installed. It cannot be required --
    it is a HACS front-end card and this dashboard has to render on a stock
    install -- so the chart sections degrade to block-character drawings when it
    is absent, and become real charts when it is there.
    """
    views = [
        _overview_view(resolved),
        _plan_view(resolved, charts),
        _performance_view(resolved),
        _loads_view(resolved),
        _settings_view(resolved),
    ]
    return {
        "title": title,
        # Not a "strategy" dashboard: a plain view list is what the UI editor can
        # take over, which is the point -- this is a starting point the user owns,
        # not something the integration keeps control of.
        "views": [view for view in views if has_content(view)],
    }
