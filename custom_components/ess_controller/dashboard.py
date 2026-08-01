"""A ready-made dashboard, generated from the entities that actually exist.

Nobody should have to assemble a dashboard card by card before the integration
tells them anything. This module builds a complete Lovelace configuration —
five views, stock cards only — which is installed into the sidebar on setup and
is a perfectly ordinary dashboard afterwards: editable, deletable, and never
rewritten once created.

Two constraints shape it:

* **Stock cards only.** ApexCharts and mini-graph-card make far prettier plots,
  but they are separate HACS installs, and a dashboard that renders as a column
  of red "Custom element doesn't exist" boxes is worse than no dashboard. The
  forward plan is therefore drawn as a Markdown table from the plan sensor's
  attributes, which needs nothing installed.
* **Every card is optional.** Entities can be disabled, and a card referencing
  a missing entity shows an error row. Cards are built from the keys that
  resolved and dropped entirely when nothing in them did, so a partial install
  degrades to a smaller dashboard rather than a broken one.

Home Assistant-free: it takes a mapping of entity key to entity id and returns
plain dictionaries, so the whole structure is testable without a running
instance.
"""

from __future__ import annotations

from typing import Any

DASHBOARD_URL_PATH = "ess-controller"
DASHBOARD_TITLE = "ESS Controller"
DASHBOARD_ICON = "mdi:home-battery-outline"

# How many upcoming half-hours the plan table shows. Twelve is six hours: long
# enough to cover the next decision, short enough to read on a phone.
PLAN_TABLE_SLOTS = 12


def _entities_card(
    title: str, keys: list[str], resolved: dict[str, str], **extra: Any
) -> dict[str, Any] | None:
    """An entities card containing whichever of ``keys`` exist."""
    rows = [resolved[key] for key in keys if key in resolved]
    if not rows:
        return None
    card: dict[str, Any] = {"type": "entities", "title": title, "entities": rows}
    card.update(extra)
    return card


def _gauge(key: str, resolved: dict[str, str], **extra: Any) -> dict[str, Any] | None:
    if key not in resolved:
        return None
    return {"type": "gauge", "entity": resolved[key], **extra}


def _glance(
    title: str, keys: list[str], resolved: dict[str, str]
) -> dict[str, Any] | None:
    rows = [resolved[key] for key in keys if key in resolved]
    if not rows:
        return None
    return {"type": "glance", "title": title, "entities": rows, "state_color": True}


def _markdown(content: str) -> dict[str, Any]:
    return {"type": "markdown", "content": content}


def _buttons(keys: list[str], resolved: dict[str, str]) -> dict[str, Any] | None:
    cards = [
        {
            "type": "button",
            "entity": resolved[key],
            "tap_action": {"action": "toggle"},
            "show_state": False,
        }
        for key in keys
        if key in resolved
    ]
    if not cards:
        return None
    return {"type": "horizontal-stack", "cards": cards}


def _compact(cards: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    return [card for card in cards if card]


def _with_footnote(
    cards: list[dict[str, Any]], content: str, extra: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Append an explanatory card, but only to a view that has real content.

    A static prose card counts as a card, which would keep a view alive even
    when every entity in it is missing -- and an empty install would then get a
    dashboard of nothing but instructions. Prose follows substance.
    """
    if not cards:
        return cards
    cards.append(_markdown(content))
    if extra:
        cards.append(extra)
    return cards


def _plan_table(plan_entity: str) -> str:
    """A Markdown table of the forward plan, rendered from sensor attributes.

    Deliberately a template rather than a chart card: the plan lives in the
    ``slots`` attribute as a list of dicts, and Jinja can turn that into a table
    with nothing installed. Prices and the action are what a person actually
    reads off it; the full detail is in the attribute for anyone templating.
    """
    return (
        "### Next six hours\n\n"
        "{% set slots = state_attr('" + plan_entity + "', 'slots') %}"
        "{% if not slots %}No plan yet — waiting for prices.{% else %}"
        "| Time | Price | Doing | SoC after |\n|---|---|---|---|\n"
        "{% for slot in slots[:" + str(PLAN_TABLE_SLOTS) + "] %}"
        "| {{ as_timestamp(slot.start) | timestamp_custom('%H:%M', true) }} "
        "| {{ '%.1f' | format(slot.import_price) }}p "
        "| {{ slot.action | replace('_', ' ') | capitalize }} "
        "| {{ '%.0f' | format(slot.soc_end) }}% |\n"
        "{% endfor %}{% endif %}"
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
    """
    return (
        "### Wear allowance\n\n"
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
        "Cycling pays above a **{{ state_attr(e, 'spread_needed') or '?' }}p** spread. "
        "Dumping to re-import pays below "
        "**{{ state_attr(e, 'negative_price_threshold') or '?' }}p**."
    )


def _performance_report(saving_entity: str) -> str:
    """The weekly report, laid out from the summary attributes.

    Units live *inside* each conditional. Putting them outside is the obvious
    way to write it and renders a missing value as "--%", which reads like a
    broken template rather than "not enough data yet".
    """
    return (
        "### This week\n\n"
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
        "### Flexible loads\n\n"
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
    cards = _compact(
        [
            _glance(
                "Now",
                ["plan_action", "next_action", "control_status", "import_price"],
                resolved,
            ),
            _gauge(
                "battery_soc",
                resolved,
                name="State of charge",
                min=0,
                max=100,
                severity={"green": 40, "yellow": 20, "red": 0},
            ),
            _entities_card(
                "Control",
                [
                    "optimiser_enabled",
                    "inverter_control",
                    "strategy",
                    "control_active",
                    "inverter_available",
                ],
                resolved,
            ),
            _markdown(_plan_reason(resolved["plan_cost"]))
            if "plan_cost" in resolved
            else None,
            _markdown(_plan_table(resolved["plan_cost"]))
            if "plan_cost" in resolved
            else None,
            _entities_card(
                "Today",
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
            ),
            _entities_card(
                "Watch out for",
                [
                    "plan_valid",
                    "write_verified",
                    "grid_session",
                    "free_electricity_now",
                    "outage_risk",
                ],
                resolved,
            ),
            _buttons(["replan", "clear_override"], resolved),
        ]
    )
    return {
        "title": "Overview",
        "path": "overview",
        "icon": "mdi:view-dashboard-outline",
        "cards": cards,
    }


def _plan_view(resolved: dict[str, str]) -> dict[str, Any]:
    cards = _compact(
        [
            _markdown(_plan_table(resolved["plan_cost"]))
            if "plan_cost" in resolved
            else None,
            _entities_card(
                "Prices",
                ["import_price", "export_price", "cheap_slot"],
                resolved,
            ),
            _entities_card(
                "This slot",
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
            ),
            # A price history graph is the one chart stock cards do well, and it
            # is the context the forward table lacks: is today dear or cheap?
            {
                "type": "history-graph",
                "title": "Price and state of charge, last 24 hours",
                "hours_to_show": 24,
                "entities": [
                    resolved[key]
                    for key in ("import_price", "battery_soc")
                    if key in resolved
                ],
            }
            if "import_price" in resolved or "battery_soc" in resolved
            else None,
            _entities_card(
                "Permissions",
                ["allow_grid_charge", "allow_export", "allow_battery_export"],
                resolved,
            ),
        ]
    )
    return {"title": "Plan", "path": "plan", "icon": "mdi:chart-timeline", "cards": cards}


def _performance_view(resolved: dict[str, str]) -> dict[str, Any]:
    cards = _compact(
        [
            _markdown(_performance_report(resolved["weekly_saving"]))
            if "weekly_saving" in resolved
            else None,
            _markdown(_wear_workings(resolved["wear_allowance"]))
            if "wear_allowance" in resolved
            else None,
            _gauge(
                "learning_progress",
                resolved,
                name="Learning progress",
                min=0,
                max=100,
                severity={"red": 0, "yellow": 33, "green": 66},
            ),
            _entities_card(
                "Battery",
                ["battery_soc", "usable_capacity", "wear_allowance"],
                resolved,
            ),
            _entities_card("Tariffs", ["tariff_recommendation"], resolved),
        ]
    )
    cards = _with_footnote(
        cards,
        "### Export the history\n\n"
        "Run the **ess_controller.export_performance** action from "
        "Developer tools → Actions to get the recorded half-hours out as "
        "a summary, structured data, or a CSV file written to "
        "`config/ess_controller/`. That file is what to hand to a "
        "spreadsheet — or to an AI assistant — and ask what to change.",
        _buttons(["recommend_tariffs", "reset_learning"], resolved),
    )
    return {
        "title": "Performance",
        "path": "performance",
        "icon": "mdi:chart-box-outline",
        "cards": cards,
    }


def _loads_view(resolved: dict[str, str]) -> dict[str, Any]:
    cards = _compact(
        [
            _markdown(_flexible_loads(resolved["shifted_loads"]))
            if "shifted_loads" in resolved
            else None,
            _entities_card(
                "Load shifting",
                [
                    "shifting_enabled",
                    "appliance_control",
                    "shifted_loads",
                    "flexible_load_running",
                ],
                resolved,
            ),
            _entities_card(
                "Grid incentives",
                [
                    "sessions_enabled",
                    "grid_session",
                    "session_active",
                    "free_electricity_now",
                ],
                resolved,
            ),
            _entities_card(
                "Power cuts",
                ["outage_protection", "outage_risk", "outage_risk_active"],
                resolved,
            ),
        ]
    )
    return {
        "title": "Loads & events",
        "path": "loads",
        "icon": "mdi:washing-machine",
        "cards": cards,
    }


def _settings_view(resolved: dict[str, str]) -> dict[str, Any]:
    cards = _compact(
        [
            _entities_card(
                "Battery limits",
                [
                    "min_soc",
                    "max_soc",
                    "reserve_soc",
                    "max_charge_power",
                    "max_discharge_power",
                ],
                resolved,
            ),
            _entities_card(
                "Wear",
                [
                    "derive_wear_from_cost",
                    "battery_cost",
                    "battery_expected_cycles",
                    "battery_residual_value",
                    "cycle_cost",
                    "wear_allowance",
                ],
                resolved,
            ),
            _entities_card(
                "Load model",
                [
                    "default_daily_load",
                    "cooling_threshold",
                    "cooling_rate",
                    "heating_threshold",
                    "heating_rate",
                ],
                resolved,
            ),
            _entities_card(
                "Behaviour",
                [
                    "optimiser_enabled",
                    "inverter_control",
                    "strategy",
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
    )
    cards = _with_footnote(
        cards,
        "### Deeper settings\n\n"
        "Entities, tariff sources, forecast sources, flexible load "
        "definitions and outage thresholds live in **Settings → Devices & "
        "services → AI ESS Controller → Configure**. Everything on this "
        "page takes effect on the next plan, without a reload.",
        _buttons(["replan", "clear_override", "reset_learning"], resolved),
    )
    return {
        "title": "Settings",
        "path": "settings",
        "icon": "mdi:tune-variant",
        "cards": cards,
    }


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
        "views": [view for view in views if view["cards"]],
    }
