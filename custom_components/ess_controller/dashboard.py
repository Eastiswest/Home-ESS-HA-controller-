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
    "next_action": "Next half-hour",
    "control_status": "Control",
    "target_soc": "Target",
    "planned_charge_power": "Charge power",
    "planned_discharge_power": "Discharge power",
    "charging_planned": "Charging",
    "discharging_planned": "Discharging",
    "exporting_planned": "Exporting",
    # Money
    "import_price": "Import price",
    "export_price": "Export price",
    "cheap_slot": "Cheap slot",
    "plan_cost": "Cost over the horizon",
    "plan_saving": "Saving vs self-use",
    "weekly_saving": "Saving this week",
    "tariff_recommendation": "Best tariff for you",
    # Energy
    "planned_grid_import": "Grid import",
    "planned_grid_export": "Grid export",
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


def _plan_table(plan_entity: str, slots: int | None = PLAN_TABLE_SLOTS) -> str:
    """The forward plan as a horizontal bar chart, in Markdown.

    A table of numbers is not a shape, and the shape is the whole point: what a
    person wants from the plan is to see where the trough is, not to read
    seventy-two prices. A proper line chart is not available -- ``history-graph``
    plots recorded state and cannot draw the future, and ApexCharts is a separate
    HACS install this dashboard deliberately does not require -- so the bars are
    drawn with block characters in a code span, which needs nothing installed and
    renders anywhere.

    Bars are scaled to the dearest slot on show, so the picture is always of
    *this* horizon rather than of some fixed price range. Negative prices get
    their own marker: they are the most valuable slots on the page and a
    zero-length bar would bury them.
    """
    limit = "" if slots is None else f"[:{slots}]"
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
        "**{{ day }}**\n\n"
        "| Time | Price | | Doing | SoC |\n|---|--:|---|---|--:|\n"
        "{% set ns.day = day %}"
        "{% endif %}"
        "{% set p = slot.import_price %}"
        "{% set filled = [(p / top * "
        + str(BAR_WIDTH)
        + ") | round | int, "
        + str(BAR_WIDTH)
        + "] | min if p > 0 else 0 %}"
        "| {{ as_timestamp(slot.start) | timestamp_custom('%H:%M', true) }} "
        # A predicted price is marked rather than dressed up as announced: an
        # asterisk costs a character and is the difference between "the plan says
        # 4p at 2am" and "the plan guesses 4p at 2am".
        "| {{ '%.1f' | format(p) }}p"
        "{{ '*' if slot.price_is_forecast else '' }} "
        # A negative price is off the scale in the good direction, so it gets its
        # own fixed marker rather than a proportional bar.
        "| `{% if p < 0 %}◄◄{{ '·' * " + str(BAR_WIDTH - 2) + " }}"
        "{% else %}{{ '█' * filled }}{{ '·' * ("
        + str(BAR_WIDTH)
        + " - filled) }}{% endif %}` "
        "| {{ slot.action | replace('_', ' ') | capitalize }} "
        "| {{ '%.0f' | format(slot.soc_end) }}% |\n"
        "{% endfor %}"
        "\n{% if prices | select('lt', 0) | list | count %}"
        "◄ paid to import. "
        "{% endif %}"
        "{% if shown | selectattr('price_is_forecast') | list | count %}"
        "\\* predicted, not yet announced by Octopus."
        "{% endif %}"
        "{% endif %}"
    )


def _plan_reason(plan_entity: str) -> str:
    return (
        "{% set reason = state_attr('" + plan_entity + "', 'reason') %}"
        "{% if reason %}**Why this plan:** {{ reason }}"
        "{% else %}No plan yet — waiting for prices.{% endif %}"
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
                    _markdown(_plan_reason(plan)) if plan else None,
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


def _plan_view(resolved: dict[str, str]) -> dict[str, Any]:
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
                [_markdown(_plan_sparkline(plan)) if plan else None],
            ),
            _section(
                "The whole plan",
                "mdi:clock-outline",
                [_markdown(_plan_table(plan)) if plan else None],
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
            _badge("target_soc", resolved),
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
                    *_actions(["replan", "clear_override", "reset_learning"], resolved),
                ],
            ),
        ],
    )


def build_dashboard(
    resolved: dict[str, str], title: str = DASHBOARD_TITLE
) -> dict[str, Any]:
    """Build the whole dashboard from the entity keys that resolved.

    ``resolved`` maps an entity key (``plan_action``, ``min_soc``, ...) to the
    entity id it ended up with, so the output is correct whatever the device was
    named and whichever entities the user disabled.
    """
    views = [
        _overview_view(resolved),
        _plan_view(resolved),
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
