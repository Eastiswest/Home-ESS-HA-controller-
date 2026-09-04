"""Tests for the generated dashboard.

The point of these is that the dashboard is *valid whatever exists*: entities can
be disabled, renamed, or absent because a platform failed, and none of that may
produce a card pointing at nothing or an empty view.
"""

from __future__ import annotations

import pytest

from custom_components.ess_controller.dashboard import (
    ACTION_NOTES,
    ACTION_WORDS,
    APEX_CARD,
    DASHBOARD_TITLE,
    IDLE_ON_SOLAR,
    _action_legend,
    _plan_table,
    build_dashboard,
    fingerprint,
)

# Every key the integration can publish, as the registry would report them.
ALL_KEYS = (
    # sensors
    "plan_action",
    "next_action",
    "control_status",
    "plan_cost",
    "plan_saving",
    "import_price",
    "export_price",
    "target_soc",
    "solar_forecast_today",
    "solar_forecast_tomorrow",
    "load_forecast_today",
    "load_forecast_tomorrow",
    "planned_grid_import",
    "planned_grid_export",
    "battery_soc",
    "battery_energy",
    "usable_capacity",
    "learning_progress",
    "planned_charge_power",
    "planned_discharge_power",
    "grid_session",
    "outage_risk",
    "shifted_loads",
    "tariff_recommendation",
    "wear_allowance",
    "weekly_saving",
    # binary sensors
    "charging_planned",
    "discharging_planned",
    "exporting_planned",
    "cheap_slot",
    "control_active",
    "inverter_available",
    "plan_valid",
    "write_verified",
    "session_active",
    "free_electricity_now",
    "outage_risk_active",
    "flexible_load_running",
    # switches
    "optimiser_enabled",
    "inverter_control",
    "allow_grid_charge",
    "allow_export",
    "allow_battery_export",
    "sessions_enabled",
    "shifting_enabled",
    "appliance_control",
    "outage_protection",
    "derive_wear_from_cost",
    # numbers
    "min_soc",
    "max_soc",
    "reserve_soc",
    "max_charge_power",
    "max_discharge_power",
    "cycle_cost",
    "default_daily_load",
    "cooling_rate",
    "cooling_threshold",
    "heating_rate",
    "heating_threshold",
    "battery_cost",
    "battery_expected_cycles",
    "battery_residual_value",
    # select and buttons
    "strategy",
    "replan",
    "clear_override",
    "reset_learning",
    "recommend_tariffs",
    "create_dashboard",
)

DOMAIN_FOR = {
    "optimiser_enabled": "switch",
    "inverter_control": "switch",
    "allow_grid_charge": "switch",
    "allow_export": "switch",
    "allow_battery_export": "switch",
    "sessions_enabled": "switch",
    "shifting_enabled": "switch",
    "appliance_control": "switch",
    "outage_protection": "switch",
    "derive_wear_from_cost": "switch",
    "strategy": "select",
    "replan": "button",
    "clear_override": "button",
    "reset_learning": "button",
    "recommend_tariffs": "button",
    "create_dashboard": "button",
}

BINARY = {
    "charging_planned",
    "discharging_planned",
    "exporting_planned",
    "cheap_slot",
    "control_active",
    "inverter_available",
    "plan_valid",
    "write_verified",
    "session_active",
    "free_electricity_now",
    "outage_risk_active",
    "flexible_load_running",
}

NUMBERS = {
    "min_soc",
    "max_soc",
    "reserve_soc",
    "max_charge_power",
    "max_discharge_power",
    "cycle_cost",
    "default_daily_load",
    "cooling_rate",
    "cooling_threshold",
    "heating_rate",
    "heating_threshold",
    "battery_cost",
    "battery_expected_cycles",
    "battery_residual_value",
}


def entity_id(key: str) -> str:
    if key in DOMAIN_FOR:
        domain = DOMAIN_FOR[key]
    elif key in BINARY:
        domain = "binary_sensor"
    elif key in NUMBERS:
        domain = "number"
    else:
        domain = "sensor"
    return f"{domain}.ess_{key}"


def resolved(*keys: str) -> dict[str, str]:
    chosen = keys or ALL_KEYS
    return {key: entity_id(key) for key in chosen}


def walk_cards(config: dict) -> list[dict]:
    """Every card in the dashboard, including sections and nested stacks.

    A section is itself a card of type ``grid`` in Lovelace's model, so it is
    walked into and counted like any other.
    """
    found: list[dict] = []

    def visit(cards: list[dict]) -> None:
        for card in cards:
            found.append(card)
            if isinstance(card.get("cards"), list):
                visit(card["cards"])

    for view in config["views"]:
        visit(view.get("cards", []))
        visit(view.get("sections", []))
    return found


def badges(config: dict) -> list[dict]:
    return [badge for view in config["views"] for badge in view.get("badges", [])]


def referenced_entities(config: dict) -> set[str]:
    """Entity ids a card points at directly, not ones mentioned in templates."""
    ids: set[str] = set()
    for card in [*walk_cards(config), *badges(config)]:
        if isinstance(card.get("entity"), str):
            ids.add(card["entity"])
        for item in card.get("entities", []):
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict) and isinstance(item.get("entity"), str):
                ids.add(item["entity"])
        # A tile that presses a button names its target in the tap action, which
        # is just as capable of pointing at something that does not exist.
        target = card.get("tap_action", {}).get("target", {}).get("entity_id")
        if isinstance(target, str):
            ids.add(target)
    return ids


class TestOverviewShowsEnergyBesideTheGauge:
    def test_stored_energy_is_on_the_overview(self):
        """The gauge says 61%; this says what that is in kWh right now. Usable
        capacity turned out to be the wrong companion -- it is the fixed window
        the plan works in, not the state of the tank."""
        config = build_dashboard(resolved())
        overview = next(v for v in config["views"] if v["path"] == "overview")
        cards = []

        def visit(items):
            for card in items:
                cards.append(card)
                if isinstance(card.get("cards"), list):
                    visit(card["cards"])

        visit(overview.get("sections", []))
        assert any(card.get("entity") == entity_id("battery_energy") for card in cards)


class TestStructure:
    def test_five_views_with_a_full_install(self):
        config = build_dashboard(resolved())
        assert [view["path"] for view in config["views"]] == [
            "overview",
            "plan",
            "performance",
            "loads",
            "settings",
        ]

    def test_title_defaults_and_can_be_overridden(self):
        assert build_dashboard(resolved())["title"] == DASHBOARD_TITLE
        assert build_dashboard(resolved(), title="Garage")["title"] == "Garage"

    def test_every_view_has_a_title_icon_and_sections(self):
        for view in build_dashboard(resolved())["views"]:
            assert view["title"]
            assert view["icon"].startswith("mdi:")
            assert view["type"] == "sections"
            assert view["sections"]

    def test_every_card_declares_a_type(self):
        for card in walk_cards(build_dashboard(resolved())):
            assert card.get("type"), card

    def test_only_stock_card_types_are_used(self):
        """A custom card would render as an error box unless separately installed."""
        stock = {
            "entities",
            "gauge",
            "grid",
            "heading",
            "history-graph",
            "markdown",
            "tile",
        }
        used = {card["type"] for card in walk_cards(build_dashboard(resolved()))}
        assert used <= stock, used - stock

    def test_every_section_opens_with_a_heading(self):
        for card in walk_cards(build_dashboard(resolved())):
            if card["type"] != "grid":
                continue
            assert card["cards"][0]["type"] == "heading"
            assert card["cards"][0]["heading"]
            assert card["cards"][0]["icon"].startswith("mdi:")

    def test_a_section_is_never_just_its_own_heading(self):
        """A heading is a card, so it must not be what keeps a section alive."""
        for key in ALL_KEYS:
            for card in walk_cards(build_dashboard(resolved(key))):
                if card["type"] == "grid":
                    assert len(card["cards"]) > 1, (key, card)

    def test_the_overview_carries_badges(self):
        overview = build_dashboard(resolved())["views"][0]
        assert overview["path"] == "overview"
        assert [badge["entity"] for badge in overview["badges"]] == [
            entity_id("battery_soc"),
            entity_id("import_price"),
            entity_id("plan_action"),
            entity_id("control_status"),
        ]

    def test_badges_are_dropped_rather_than_left_empty(self):
        """An empty ``badges`` list renders as a stray gap above the first card."""
        config = build_dashboard(resolved("min_soc"))
        for view in config["views"]:
            assert view.get("badges", ["something"]) != []


class TestNames:
    """Every entity reference must carry a short name of its own.

    Left to Home Assistant, a friendly name is the device name joined to the
    entity name, so a dashboard entirely about one device reads "AI ESS
    Controller ..." on every single row and truncates in any card that lays
    entities out in columns. These tests are what stops that coming back.
    """

    def _named(self, config: dict) -> list[dict]:
        """Every reference that is allowed to display a name."""
        found = []
        for card in walk_cards(config):
            if card["type"] in {"tile", "gauge"}:
                found.append(card)
            for item in card.get("entities", []):
                if isinstance(item, dict):
                    found.append(item)
        found.extend(badges(config))
        return found

    def test_every_key_has_a_label(self):
        from custom_components.ess_controller.dashboard import LABELS

        missing = [key for key in ALL_KEYS if key not in LABELS]
        assert missing == []

    def test_labels_are_short_enough_not_to_truncate(self):
        from custom_components.ess_controller.dashboard import LABELS

        too_long = {key: text for key, text in LABELS.items() if len(text) > 28}
        assert too_long == {}

    def test_no_label_repeats_the_integration_name(self):
        from custom_components.ess_controller.dashboard import LABELS

        for text in LABELS.values():
            assert "ESS" not in text
            assert "AI " not in text

    def test_every_reference_is_renamed(self):
        for item in self._named(build_dashboard(resolved())):
            assert item.get("name"), item

    def test_labels_survive_a_key_nobody_named(self):
        """A new entity must not be able to break the whole dashboard build."""
        from custom_components.ess_controller.dashboard import label

        assert label("solar_clipping") == "Solar clipping"

    def test_toggles_show_the_control_inline_and_hide_the_word(self):
        """'On' next to a switch that is on is noise; the toggle already says so."""
        config = build_dashboard(resolved())
        tiles = {
            card["entity"]: card for card in walk_cards(config) if card["type"] == "tile"
        }
        switch = tiles[entity_id("optimiser_enabled")]
        assert switch["features"] == [{"type": "toggle"}]
        assert switch["features_position"] == "inline"
        assert switch["hide_state"] is True

    def test_the_strategy_select_is_pickable_from_the_card(self):
        config = build_dashboard(resolved())
        tile = next(
            card
            for card in walk_cards(config)
            if card.get("entity") == entity_id("strategy")
        )
        assert tile["features"] == [{"type": "select-options"}]

    def test_buttons_press_rather_than_toggle(self):
        """``toggle`` on a button entity is not a press, so it is spelled out."""
        config = build_dashboard(resolved())
        for key in ("replan", "clear_override", "reset_learning"):
            tile = next(
                card
                for card in walk_cards(config)
                if card.get("entity") == entity_id(key)
            )
            assert tile["tap_action"] == {
                "action": "perform-action",
                "perform_action": "button.press",
                "target": {"entity_id": entity_id(key)},
            }


class TestEntityReferences:
    def test_references_only_entities_that_were_given(self):
        config = build_dashboard(resolved())
        assert referenced_entities(config) <= set(resolved().values())

    def test_the_headline_entities_all_appear(self):
        config = build_dashboard(resolved())
        used = referenced_entities(config)
        for key in ("plan_action", "battery_soc", "inverter_control", "strategy"):
            assert entity_id(key) in used

    def test_a_disabled_entity_is_never_referenced(self):
        """Entities the registry reports as disabled are simply not passed in."""
        available = {k: v for k, v in resolved().items() if k != "battery_soc"}
        config = build_dashboard(available)
        assert entity_id("battery_soc") not in referenced_entities(config)

    def test_settings_view_carries_every_number(self):
        config = build_dashboard(resolved())
        settings = next(v for v in config["views"] if v["path"] == "settings")
        used = referenced_entities({"views": [settings]})
        for key in NUMBERS:
            assert entity_id(key) in used, key


class TestDegradation:
    def test_nothing_resolved_yields_no_views(self):
        config = build_dashboard({})
        assert config["views"] == []

    def test_one_entity_yields_one_small_dashboard(self):
        config = build_dashboard(resolved("plan_action"))
        assert len(config["views"]) >= 1
        assert referenced_entities(config) == {entity_id("plan_action")}

    def test_views_with_no_sections_are_dropped_entirely(self):
        """An empty view would be a blank tab in the sidebar."""
        config = build_dashboard(resolved("min_soc", "max_soc"))
        assert [view["path"] for view in config["views"]] == ["settings"]

    def test_no_view_is_only_static_prose(self):
        """A view of nothing but instructions is worse than no view at all.

        A templated Markdown card counts as content -- the plan table is prose
        only in the sense that Lovelace renders it as Markdown -- so this looks
        for an entity mentioned anywhere in the view, template included.
        """
        for key in ALL_KEYS:
            for view in build_dashboard(resolved(key))["views"]:
                one = {"views": [view]}
                templated = any(
                    entity_id(key) in card.get("content", "") for card in walk_cards(one)
                )
                assert referenced_entities(one) or templated, (key, view["path"])

    @pytest.mark.parametrize("missing", ALL_KEYS)
    def test_any_single_entity_missing_still_builds(self, missing: str):
        available = {k: v for k, v in resolved().items() if k != missing}
        config = build_dashboard(available)
        assert config["views"]
        assert referenced_entities(config) <= set(available.values())

    def test_every_key_in_isolation_builds(self):
        for key in ALL_KEYS:
            config = build_dashboard(resolved(key))
            assert isinstance(config["views"], list)


class TestTemplates:
    def _markdown(self, config: dict) -> str:
        return "\n".join(
            card["content"] for card in walk_cards(config) if card["type"] == "markdown"
        )

    def test_plan_table_templates_against_the_plan_sensor(self):
        content = self._markdown(build_dashboard(resolved()))
        assert f"state_attr('{entity_id('plan_cost')}', 'slots')" in content

    def test_the_plan_view_shows_the_whole_horizon(self):
        """Six hours was an arbitrary cap that hid tomorrow's cheap window."""
        from custom_components.ess_controller.dashboard import (
            OVERVIEW_TABLE_SLOTS,
            PLAN_TABLE_SLOTS,
        )

        assert PLAN_TABLE_SLOTS is None
        content = self._markdown(build_dashboard(resolved()))
        # The Overview still slices; the Plan view must not.
        assert f"slots[:{OVERVIEW_TABLE_SLOTS}]" in content
        assert "{% set shown = slots %}" in content

    def test_templates_balance_their_jinja_blocks(self):
        content = self._markdown(build_dashboard(resolved()))
        assert content.count("{%") == content.count("%}")
        assert content.count("{{") == content.count("}}")
        assert content.count("{% if") + content.count("{% for") == content.count(
            "{% endif %}"
        ) + content.count("{% endfor %}")

    def test_plan_template_handles_no_plan(self):
        content = self._markdown(build_dashboard(resolved("plan_cost")))
        assert "No plan yet" in content

    def test_performance_template_handles_an_empty_history(self):
        content = self._markdown(build_dashboard(resolved("weekly_saving")))
        assert "Nothing recorded yet" in content

    def test_flexible_loads_template_handles_no_loads(self):
        content = self._markdown(build_dashboard(resolved("shifted_loads")))
        assert "No loads defined" in content

    def test_markdown_cards_are_dropped_when_their_entity_is_missing(self):
        config = build_dashboard(
            {k: v for k, v in resolved().items() if k != "shifted_loads"}
        )
        assert "advice" not in self._markdown(config)

    def test_export_instructions_name_the_action(self):
        content = self._markdown(build_dashboard(resolved()))
        assert "ess_controller.export_performance" in content


class TestTemplatesRender:
    """Render the templates with stubbed HA functions.

    Balanced braces prove the syntax parses; they do not prove the template
    produces a table rather than an error. HA's engine is Jinja with a handful of
    functions bolted on, so stubbing those functions and rendering the real
    template strings catches a bad filter or a wrong attribute name here rather
    than as a red card on someone's dashboard.
    """

    def _env(self):
        jinja2 = pytest.importorskip("jinja2")
        return jinja2.Environment(autoescape=False)

    def _render(self, template: str, attributes: dict, states: dict | None = None):
        import datetime as _dt

        states = states or {}

        def state_attr(entity: str, name: str):
            return attributes.get(entity, {}).get(name)

        def as_timestamp(value, default=None):
            try:
                return _dt.datetime.fromisoformat(str(value)).timestamp()
            except (TypeError, ValueError):
                return default

        def timestamp_custom(value, fmt, local=True, default=None):
            if value is None:
                return default
            return _dt.datetime.fromtimestamp(value, _dt.UTC).strftime(fmt)

        env = self._env()
        env.filters["timestamp_custom"] = timestamp_custom
        env.globals.update(
            state_attr=state_attr,
            states=lambda entity: states.get(entity, "unknown"),
            as_timestamp=as_timestamp,
        )
        return env.from_string(template).render()

    def _cards(self, config: dict) -> list[str]:
        return [
            card["content"] for card in walk_cards(config) if card["type"] == "markdown"
        ]

    def test_plan_table_renders_a_row_per_slot(self):
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "reason": "grid-charge 19.8 kWh at avg 1.6p",
                "slots": [
                    {
                        "start": f"2026-02-18T0{hour}:00:00+00:00",
                        # Spans both signs so the negative marker and the
                        # proportional bar are both exercised.
                        "import_price": -5.0 + hour * 10,
                        "action": "charge_solar_only",
                        "soc_end": 40.0 + hour,
                    }
                    for hour in range(4)
                ],
            }
        }
        rendered = [
            self._render(card, attributes)
            for card in self._cards(build_dashboard(resolved()))
        ]
        table = next(text for text in rendered if "| Time |" in text)
        # Cheap is a short bar, dear is a long one, and a negative price gets its
        # own marker because it is off the scale in the good direction.
        assert "| 00:00 | -5.0p | `◄◄" in table
        assert "◄ paid to import." in table
        # 25p is the dearest of -5/5/15/25, so it fills the bar; 5p is a fifth of
        # it and must not.
        dearest = next(row for row in table.splitlines() if "| 03:00 |" in row)
        assert "████████████" in dearest, dearest
        cheapish = next(row for row in table.splitlines() if "| 01:00 |" in row)
        assert "██·" in cheapish, cheapish
        assert table.count("\n|") >= 5  # header, separator, four slots
        assert "**Wed 18 Feb**" in table  # grouped by day
        assert any("grid-charge 19.8 kWh" in text for text in rendered)

    def test_plan_table_splits_a_hold_by_what_it_buys(self):
        """Two holds, one importing and one not, must not read the same.

        The report that prompted this: two consecutive holds at 38.2p and 45.4p
        labelled "Hold (house on grid)" while the Buy column beside them said
        nothing and the sun was ahead of the house. The label was the only thing
        wrong -- the plan was right -- but at the top of the tariff it reads as
        the controller deliberately buying at peak.
        """
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "reason": "holding",
                "slots": [
                    {
                        "start": "2026-02-18T17:00:00+00:00",
                        "import_price": 45.4,
                        "action": "idle",
                        "grid_import_kwh": 0.0,
                        "soc_end": 62.0,
                    },
                    {
                        "start": "2026-02-18T17:30:00+00:00",
                        "import_price": 45.4,
                        "action": "idle",
                        "grid_import_kwh": 0.4,
                        "soc_end": 62.0,
                    },
                ],
            }
        }
        table = next(
            text
            for text in (
                self._render(card, attributes)
                for card in self._cards(build_dashboard(resolved()))
            )
            if "| Time |" in text
        )
        sunny = next(row for row in table.splitlines() if "| 17:00 |" in row)
        bought = next(row for row in table.splitlines() if "| 17:30 |" in row)
        assert ACTION_WORDS[IDLE_ON_SOLAR] in sunny, sunny
        assert ACTION_WORDS["idle"] in bought, bought

    def test_plan_table_prices_each_slot(self):
        """What the half-hour costs, from the energies already on the row.

        Import at the import price less export at the export price, so the
        column sums to the day's bill -- including on a slot that buys nothing
        for the battery, where the house is simply drawing more than the sun.
        """
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "reason": "holding",
                "slots": [
                    {
                        "start": "2026-02-18T17:00:00+00:00",
                        "import_price": 30.0,
                        "export_price": 15.0,
                        "action": "idle",
                        "grid_import_kwh": 1.2,
                        "soc_end": 62.0,
                    },
                    {
                        "start": "2026-02-18T17:30:00+00:00",
                        "import_price": 30.0,
                        "export_price": 15.0,
                        "action": "idle",
                        "grid_export_kwh": 0.5,
                        "soc_end": 62.0,
                    },
                    {
                        "start": "2026-02-18T18:00:00+00:00",
                        "import_price": 30.0,
                        "export_price": 15.0,
                        "action": "idle",
                        "soc_end": 62.0,
                    },
                ],
            }
        }
        table = next(
            text
            for text in (
                self._render(card, attributes)
                for card in self._cards(build_dashboard(resolved()))
            )
            if "| Time |" in text
        )

        def cost_cell(time):
            row = next(r for r in table.splitlines() if f"| {time} |" in r)
            return row.split("|")[-3].strip()

        assert cost_cell("17:00") == "36.0p"
        # Exporting is money back, and the column has to be able to say so.
        assert cost_cell("17:30") == "-7.5p"
        assert cost_cell("18:00") == "—"
        assert "whole half-hour's grid bill" in table

    def _soc_line(self, attributes, states=None):
        from custom_components.ess_controller.dashboard import _soc_summary

        return self._render(
            _soc_summary(entity_id("plan_cost"), entity_id("battery_soc")),
            attributes,
            states,
        )

    def test_the_battery_card_states_now_and_the_end_of_the_plan(self):
        """The chart's own legend cannot: it takes its figures from the series'
        entity, and the projection is drawn from the plan sensor, whose state is
        the horizon's cost in pence. It read "Planned: 58 %" beside a plan that
        wanted 94% and a pack holding 92% -- a cost, in the wrong unit, read as
        the controller being thirty-six points adrift of its own plan.
        """
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "slots": [
                    {
                        "start": "2026-02-18T00:00:00+00:00",
                        "end": "2026-02-18T00:30:00+00:00",
                        "soc_end": 94.0,
                    },
                    {
                        "start": "2026-02-18T00:30:00+00:00",
                        "end": "2026-02-18T01:00:00+00:00",
                        "soc_end": 21.0,
                    },
                ]
            }
        }
        line = self._soc_line(attributes, {entity_id("battery_soc"): "92.4"})
        assert "Battery now **92%**" in line
        # The *end* of the plan, not the middle and not a cost.
        assert "plan ends at **21%**" in line
        assert "01:00" in line
        assert "58" not in line

    def test_an_unreadable_state_of_charge_does_not_blank_the_card(self):
        """An unreadable SoC is a fault this integration exists to report, so it
        is the worst possible moment for the card describing the battery to
        raise instead of render."""
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "slots": [
                    {
                        "start": "2026-02-18T00:00:00+00:00",
                        "end": "2026-02-18T00:30:00+00:00",
                        "soc_end": 21.0,
                    }
                ]
            }
        }
        line = self._soc_line(attributes, {entity_id("battery_soc"): "unavailable"})
        assert "Battery now" not in line
        assert "plan ends at **21%**" in line

    def test_no_plan_yet_says_so_rather_than_showing_nothing(self):
        assert "waiting for prices" in self._soc_line({entity_id("plan_cost"): {}})

    def test_the_weekly_table_adds_up(self):
        """A real week showed "Saving vs self-use -159.77p" and "Wear charged
        58.77p" above a bold "Net saving 64.78p". Two negatives making a
        positive, because the largest term of the three -- the charge left in
        the battery, +283p -- had no row. The reader could not get from the
        numbers shown to the number in bold.
        """
        from custom_components.ess_controller.dashboard import _performance_report

        saving = entity_id("weekly_saving")
        attributes = {
            saving: {
                "money": {
                    "actual": 1136.03,
                    "if_no_battery": 1692.6,
                    "if_self_use_only": 976.26,
                    "saving_vs_self_use": -159.77,
                    "wear_cost": 58.77,
                    "stored_energy_value": 283.32,
                    "net_saving_vs_self_use": 64.78,
                },
                "control": {"plan_fidelity": 0.98, "round_trip_efficiency": 0.91},
                "forecast_error_kwh_per_slot": {"solar_mae": 0.07, "load_mae": 0.15},
                "window": {"slots": 332, "days": 7.02},
                "notes": [],
            }
        }
        table = self._render(_performance_report(saving), attributes)
        assert "| Charge left in the battery | +\u00a32.83 |" in table
        # And the two deductions read as deductions.
        assert "-\u00a31.60" in table
        assert "| Wear charged | -\u00a30.59 |" in table
        # -1.60 - 0.59 + 2.83 = 0.65 (rounded per row), which is now visibly the case.
        assert "**\u00a30.65**" in table

    def test_a_week_with_nothing_stored_still_renders(self):
        from custom_components.ess_controller.dashboard import _performance_report

        saving = entity_id("weekly_saving")
        attributes = {
            saving: {
                "money": {
                    "actual": 100.0,
                    "if_no_battery": 200.0,
                    "if_self_use_only": None,
                    "saving_vs_self_use": None,
                    "wear_cost": 1.0,
                    "stored_energy_value": None,
                    "net_saving_vs_self_use": None,
                },
                "control": {"plan_fidelity": None, "round_trip_efficiency": None},
                "forecast_error_kwh_per_slot": {"solar_mae": None, "load_mae": None},
                "window": {"slots": 4, "days": 0.1},
                "notes": [],
            }
        }
        table = self._render(_performance_report(saving), attributes)
        assert "not yet" in table
        assert "None" not in table

    def test_the_spare_solar_line_separates_the_sun_from_the_surplus(self):
        """ "Solar left today 5.8 kWh" beside a plan buying from the grid reads
        as a contradiction and is the question this dashboard has been asked
        most. It is not one: on a real August day 5.8 kWh of sun met 7.5 kWh of
        demand, so the sun did not cover the house, let alone have anything
        spare for a 22 kWh battery. The surplus was the one figure not shown.
        """
        from custom_components.ess_controller.dashboard import _spare_solar

        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "slots": [
                    # Two sunny half-hours with a surplus, one where the house
                    # wins, and one belonging to tomorrow which must be ignored.
                    {
                        "start": "2026-08-23T11:00:00+00:00",
                        "pv_kwh": 1.0,
                        "load_kwh": 0.4,
                    },
                    {
                        "start": "2026-08-23T11:30:00+00:00",
                        "pv_kwh": 1.0,
                        "load_kwh": 0.6,
                    },
                    {
                        "start": "2026-08-23T18:00:00+00:00",
                        "pv_kwh": 0.2,
                        "load_kwh": 0.9,
                    },
                    {
                        "start": "2026-08-24T11:00:00+00:00",
                        "pv_kwh": 5.0,
                        "load_kwh": 0.1,
                    },
                ]
            }
        }
        line = self._render(_spare_solar(plan), attributes)
        assert "2.2 kWh** of sun left today" in line
        # 0.6 + 0.4 spare; the evening deficit does not net off against it.
        assert "1.0 kWh** is spare for the battery" in line
        assert "1.2 kWh** as it arrives" in line

    def test_the_spare_solar_line_copes_with_no_plan(self):
        from custom_components.ess_controller.dashboard import _spare_solar

        plan = entity_id("plan_cost")
        assert "waiting for prices" in self._render(_spare_solar(plan), {plan: {}})

    def test_a_hold_with_no_import_figure_at_all_still_reads_sensibly(self):
        """Older plans, and diagnostics captures, carry no import column."""
        plan = entity_id("plan_cost")
        attributes = {
            plan: {
                "slots": [
                    {
                        "start": "2026-02-18T17:00:00+00:00",
                        "import_price": 45.4,
                        "action": "idle",
                        "soc_end": 62.0,
                    }
                ]
            }
        }
        rendered = [
            self._render(card, attributes)
            for card in self._cards(build_dashboard(resolved()))
        ]
        table = next(text for text in rendered if "| Time |" in text)
        assert "Hold" in table
        assert "Undefined" not in table

    def test_plan_table_renders_the_fallback_with_no_plan(self):
        cards = self._cards(build_dashboard(resolved("plan_cost")))
        # Only the cards that read the plan. The state legend is static prose and
        # is as true with no plan as with one.
        templated = [card for card in cards if "state_attr" in card]
        assert templated
        for card in templated:
            assert "No plan yet" in self._render(card, {})

    def test_performance_report_renders_the_numbers(self):
        saving = entity_id("weekly_saving")
        attributes = {
            saving: {
                "window": {"slots": 336, "days": 7.0},
                "money": {
                    "actual": 1160.0,
                    "if_no_battery": 2100.0,
                    "if_self_use_only": 1500.0,
                    "saving_vs_self_use": 340.0,
                    "wear_cost": 40.0,
                    "net_saving_vs_self_use": 300.0,
                },
                "forecast_error_kwh_per_slot": {"solar_mae": 0.05, "load_mae": 0.12},
                "control": {"plan_fidelity": 0.94, "round_trip_efficiency": 0.883},
                "notes": ["armed for 300 of 336 slots"],
            }
        }
        card = self._cards(build_dashboard(resolved("weekly_saving")))[0]
        rendered = self._render(card, attributes)
        assert "| Spent | \u00a311.60 |" in rendered
        assert "| **Net saving** | **\u00a33.00** |" in rendered
        assert "| Plan followed | 94% |" in rendered
        assert "| Round trip | 88% |" in rendered
        assert "> armed for 300 of 336 slots" in rendered

    def test_performance_report_renders_missing_figures_as_a_dash(self):
        saving = entity_id("weekly_saving")
        attributes = {
            saving: {
                "window": {"slots": 4, "days": 0.1},
                "money": {
                    "actual": 10.0,
                    "if_no_battery": 12.0,
                    "if_self_use_only": None,
                    "saving_vs_self_use": None,
                    "wear_cost": 0.0,
                    "net_saving_vs_self_use": None,
                },
                "forecast_error_kwh_per_slot": {"solar_mae": None, "load_mae": None},
                "control": {"plan_fidelity": None, "round_trip_efficiency": None},
                "notes": [],
            }
        }
        rendered = self._render(
            self._cards(build_dashboard(resolved("weekly_saving")))[0], attributes
        )
        assert "| Round trip | not yet |" in rendered
        assert "| Saving vs self-use | not yet |" in rendered
        assert "None" not in rendered
        # A missing figure must not render as a bare unit, which reads as a bug.
        assert "%" not in rendered.split("| Plan followed |")[1].split("|")[0]

    def test_wear_workings_render_with_a_derived_allowance(self):
        wear = entity_id("wear_allowance")
        # These attribute names are the sensor's, verbatim. Writing the fixture
        # with tidier names of my own is what let the real card render "Cycling
        # pays above a ?p spread" while this test passed.
        attributes = {
            wear: {
                "source": "derived from pack cost",
                "net_cost": 500.0,
                "expected_cycles": 1500.0,
                "usable_kwh": 17.6,
                "lifetime_throughput_kwh": 26400.0,
                "spread_needed_to_cycle": 2.1,
                "negative_price_to_dump_and_reimport": -1.8,
            }
        }
        rendered = self._render(
            self._cards(build_dashboard(resolved("wear_allowance")))[0],
            attributes,
            {wear: "1.894"},
        )
        assert "**1.894 p/kWh**" in rendered
        assert "derived from pack cost" in rendered
        assert "1500 cycles of 17.6 kWh = 26400 kWh of throughput" in rendered
        assert "**2.1p** spread" in rendered
        assert "**-1.8p**" in rendered

    def test_flexible_loads_render_the_advice_and_the_schedule(self):
        shifted = entity_id("shifted_loads")
        attributes = {
            shifted: {
                "advice": "start Immersion at 03:00",
                "placements": [
                    {
                        "name": "Immersion",
                        "start": "2026-02-18T03:00:00+00:00",
                        "end": "2026-02-18T04:00:00+00:00",
                        "cost": -13.5,
                        "switch": "switch.immersion",
                    },
                    {
                        "name": "Dishwasher",
                        "start": "2026-02-18T03:30:00+00:00",
                        "end": "2026-02-18T04:06:00+00:00",
                        "cost": -5.6,
                        "switch": None,
                    },
                ],
            }
        }
        rendered = self._render(
            self._cards(build_dashboard(resolved("shifted_loads")))[0], attributes
        )
        assert "**start Immersion at 03:00**" in rendered
        assert "| Immersion | 03:00 to 04:00 | -\u00a30.14 | yes |" in rendered
        assert "| Dishwasher | 03:30 to 04:06 | -\u00a30.06 | by hand |" in rendered

    def test_every_markdown_card_renders_against_empty_state(self):
        """Nothing may raise or leak "None" before any data exists."""
        for card in self._cards(build_dashboard(resolved())):
            rendered = self._render(card, {})
            assert "None" not in rendered


class TestDashboardsMapping:
    """Lovelace's dashboards mapping has changed shape across HA releases.

    Getting this wrong is exactly how the first attempt failed silently: it
    looked for the live dashboards *collection*, which Home Assistant keeps as a
    local variable and never publishes, so the lookup always missed and the
    install quietly fell back to writing a file.
    """

    def test_reads_the_legacy_dict_shape(self):
        from custom_components.ess_controller.dashboard import dashboards_mapping

        board = {"my-dash": object()}
        assert dashboards_mapping({"dashboards": board, "mode": "storage"}) is board

    def test_reads_the_dataclass_shape(self):
        from custom_components.ess_controller.dashboard import dashboards_mapping

        class LovelaceData:
            def __init__(self, dashboards):
                self.dashboards = dashboards
                self.mode = "storage"

        board = {"my-dash": object()}
        assert dashboards_mapping(LovelaceData(board)) is board

    def test_returns_none_when_lovelace_is_absent(self):
        from custom_components.ess_controller.dashboard import dashboards_mapping

        assert dashboards_mapping(None) is None

    def test_returns_none_for_an_unrecognised_shape(self):
        from custom_components.ess_controller.dashboard import dashboards_mapping

        assert dashboards_mapping({"mode": "yaml"}) is None
        assert dashboards_mapping(object()) is None
        assert dashboards_mapping({"dashboards": ["not", "a", "mapping"]}) is None

    def test_an_empty_mapping_is_still_usable(self):
        """No dashboards yet is the normal case, not a failure."""
        from custom_components.ess_controller.dashboard import dashboards_mapping

        board: dict = {}
        assert dashboards_mapping({"dashboards": board}) is board


class TestPlaceholder:
    """Something must appear in the sidebar even before entities exist.

    Registering nothing until entities resolve is what made the failure
    invisible: an absent sidebar entry looks identical whether the integration is
    still starting or never set up at all.
    """

    def _placeholder(self):
        from custom_components.ess_controller.dashboard import placeholder_dashboard

        return placeholder_dashboard()

    def test_placeholder_has_exactly_one_view_with_a_card(self):
        config = self._placeholder()
        assert len(config["views"]) == 1
        assert len(config["views"][0]["cards"]) == 1
        assert config["views"][0]["cards"][0]["type"] == "markdown"

    def test_placeholder_says_what_to_check(self):
        content = self._placeholder()["views"][0]["cards"][0]["content"]
        assert "Devices & services" in content
        assert "Rebuild dashboard" in content
        assert "sensor.ess" in content

    def test_placeholder_carries_the_title(self):
        from custom_components.ess_controller.dashboard import placeholder_dashboard

        assert placeholder_dashboard("Garage")["title"] == "Garage"

    def test_placeholder_references_no_entities(self):
        """It has to render before anything exists, so it can depend on nothing."""
        assert referenced_entities(self._placeholder()) == set()

    def test_the_placeholder_is_recognised_as_replaceable(self):
        from custom_components.ess_controller.dashboard import is_placeholder

        assert is_placeholder(self._placeholder()) is True

    def test_a_real_dashboard_is_never_treated_as_replaceable(self):
        """Overwriting the user's edited dashboard would destroy their work."""
        from custom_components.ess_controller.dashboard import is_placeholder

        assert is_placeholder(build_dashboard(resolved())) is False

    def test_a_single_view_dashboard_of_their_own_is_not_the_placeholder(self):
        from custom_components.ess_controller.dashboard import is_placeholder

        assert is_placeholder(build_dashboard(resolved("min_soc"))) is False
        assert is_placeholder({"views": [{"path": "home", "cards": []}]}) is False

    def test_junk_is_not_the_placeholder(self):
        from custom_components.ess_controller.dashboard import is_placeholder

        assert is_placeholder(None) is False
        assert is_placeholder({}) is False
        assert is_placeholder({"views": "nonsense"}) is False
        assert is_placeholder({"views": [None]}) is False


class TestSparkline:
    """The horizon as one line of blocks -- the shape, not the numbers.

    Rendered against a realistic Agile day rather than a synthetic ramp, because
    what matters is whether a person can see the overnight trough and the evening
    peak, and a monotonic fixture would prove nothing about that.
    """

    def _slots(self) -> list[dict]:
        import datetime as dt
        import math

        start = dt.datetime(2026, 8, 12, 0, 0, tzinfo=dt.UTC)
        slots = []
        for n in range(72):
            when = start + dt.timedelta(minutes=30 * n)
            hour = when.hour + when.minute / 60
            price = (
                18
                + 12 * math.sin((hour - 9) / 24 * 2 * math.pi)
                + 9 * math.exp(-((hour - 18) ** 2) / 3)
            )
            if 2 <= hour < 4 and n < 48:
                price = -3.5  # a negative window overnight
            slots.append({"start": when.isoformat(), "import_price": round(price, 2)})
        return slots

    def _render(self, slots: list[dict]) -> str:
        import datetime as dt

        jinja2 = pytest.importorskip("jinja2")
        from custom_components.ess_controller.dashboard import _plan_sparkline

        env = jinja2.Environment(autoescape=False)
        env.filters["timestamp_custom"] = lambda v, f, local=True, default=None: (
            dt.datetime.fromtimestamp(v, dt.UTC).strftime(f) if v is not None else default
        )
        env.globals.update(
            state_attr=lambda e, a: {"slots": slots}.get(a),
            as_timestamp=lambda v, default=None: dt.datetime.fromisoformat(
                str(v)
            ).timestamp(),
        )
        return env.from_string(_plan_sparkline("sensor.plan")).render()

    def test_one_character_per_slot_plus_a_midnight_break(self):
        rendered = self._render(self._slots())
        bar = rendered.split("`")[1]
        assert len(bar) == 72 + 1, bar  # 36 hours, one midnight crossing

    def test_the_cheapest_slot_is_at_the_bottom_of_the_scale(self):
        """A negative price must read as the best slot, not an outlier."""
        rendered = self._render(self._slots())
        bar = rendered.split("`")[1]
        assert bar[4] == "▁", bar  # 02:00, the negative window

    def test_the_evening_peak_is_at_the_top_of_the_scale(self):
        rendered = self._render(self._slots())
        bar = rendered.split("`")[1]
        assert "█" in bar[34:40], bar[30:44]  # around 18:00

    def test_the_range_and_span_are_stated(self):
        rendered = self._render(self._slots())
        assert "-3.5p to" in rendered
        assert "36 hours" in rendered
        assert "midnight" in rendered

    def test_a_flat_tariff_does_not_divide_by_zero(self):
        flat = [
            {"start": f"2026-08-12T{h:02d}:00:00+00:00", "import_price": 25.0}
            for h in range(6)
        ]
        rendered = self._render(flat)
        assert "`" in rendered
        assert "nan" not in rendered.lower()

    def test_no_plan_says_so(self):
        assert "No plan yet" in self._render([])


class TestNextChangeIsExplained:
    """ "Doing now" and "Next change" can never be equal, by construction.

    The sensor reports the next slot whose action *differs* from the current one,
    so it is always something else -- which made two correct sensors look like
    they contradicted each other, especially under the old label "Next
    half-hour" when the change was six hours away.
    """

    def test_the_label_does_not_promise_the_next_half_hour(self):
        from custom_components.ess_controller.dashboard import LABELS

        assert LABELS["next_action"] == "Next change"
        assert "half-hour" not in LABELS["next_action"]

    def test_the_overview_says_when_the_change_happens(self):
        from custom_components.ess_controller.dashboard import _plan_reason

        content = _plan_reason(entity_id("plan_cost"), entity_id("next_action"))
        assert "Next change:" in content
        assert entity_id("next_action") in content

    def test_it_renders_the_time_and_price(self):
        import datetime as dt

        jinja2 = pytest.importorskip("jinja2")
        from custom_components.ess_controller.dashboard import _plan_reason

        attributes = {
            entity_id("plan_cost"): {"reason": "hold battery"},
            entity_id("next_action"): {
                "starts": "2026-08-11T20:00:00+00:00",
                "import_price": 4.31,
            },
        }
        env = jinja2.Environment(autoescape=False)
        env.filters["timestamp_custom"] = lambda v, f, local=True, default=None: (
            dt.datetime.fromtimestamp(v, dt.UTC).strftime(f) if v is not None else default
        )
        env.globals.update(
            state_attr=lambda e, a: attributes.get(e, {}).get(a),
            states=lambda e: "charge" if "next_action" in e else "unknown",
            as_timestamp=lambda v, default=None: dt.datetime.fromisoformat(
                str(v)
            ).timestamp(),
        )
        rendered = env.from_string(
            _plan_reason(entity_id("plan_cost"), entity_id("next_action"))
        ).render()
        assert "**Why this plan:** hold battery" in rendered
        assert "**Next change:** Charge at 20:00, 4.3p." in rendered

    def test_nothing_is_claimed_when_the_plan_never_changes(self):
        """A flat plan leaves the sensor unknown, and silence is the right answer."""

        jinja2 = pytest.importorskip("jinja2")
        from custom_components.ess_controller.dashboard import _plan_reason

        env = jinja2.Environment(autoescape=False)
        env.filters["timestamp_custom"] = lambda v, f, local=True, default=None: v
        env.globals.update(
            state_attr=lambda e, a: {"reason": "hold battery"}.get(a),
            states=lambda e: "unknown",
            as_timestamp=lambda v, default=None: 0,
        )
        rendered = env.from_string(
            _plan_reason(entity_id("plan_cost"), entity_id("next_action"))
        ).render()
        assert "Next change" not in rendered

    def test_it_still_works_with_no_next_change_sensor(self):
        from custom_components.ess_controller.dashboard import _plan_reason

        content = _plan_reason(entity_id("plan_cost"))
        assert "Why this plan" in content
        assert "Next change" not in content


class TestRefreshWithoutLosingEdits:
    """An upgrade must be able to improve the dashboard AND keep your edits.

    Created-once-never-rewritten protected edits and made every improvement
    invisible: three separate dashboard changes never reached the first user,
    because the fix was "delete it by hand" and nobody knows that. Fingerprinting
    the generated config separates the two cases.
    """

    def _generated(self, *keys: str) -> dict:
        from custom_components.ess_controller.dashboard import stamp

        return stamp(build_dashboard(resolved(*keys)))

    def test_our_own_output_is_recognised(self):
        from custom_components.ess_controller.dashboard import is_untouched

        assert is_untouched(self._generated()) is True

    def test_an_edited_dashboard_is_not(self):
        """The case that must never be replaced."""
        from custom_components.ess_controller.dashboard import is_untouched

        stored = self._generated()
        stored["views"][0]["title"] = "My Overview"
        assert is_untouched(stored) is False

    def test_adding_a_view_counts_as_an_edit(self):
        from custom_components.ess_controller.dashboard import is_untouched

        stored = self._generated()
        stored["views"].append({"title": "Mine", "cards": []})
        assert is_untouched(stored) is False

    def test_a_dashboard_from_before_stamping_is_left_alone(self):
        """No stamp means unknown provenance, and unknown must mean "do not touch"."""
        from custom_components.ess_controller.dashboard import is_untouched

        assert is_untouched(build_dashboard(resolved())) is False

    def test_junk_is_never_replaceable(self):
        from custom_components.ess_controller.dashboard import is_untouched

        for value in (None, {}, [], "", {"ess_controller_generated": ""}):
            assert is_untouched(value) is False

    def test_a_different_layout_has_a_different_fingerprint(self):
        """Otherwise an upgrade could not tell there was anything to refresh."""
        from custom_components.ess_controller.dashboard import fingerprint

        assert fingerprint(self._generated()) != fingerprint(
            self._generated("min_soc", "max_soc")
        )

    def test_the_same_layout_fingerprints_the_same_twice(self):
        from custom_components.ess_controller.dashboard import fingerprint

        assert fingerprint(self._generated()) == fingerprint(self._generated())

    def test_the_stamp_does_not_change_the_dashboard(self):
        from custom_components.ess_controller.dashboard import GENERATED_KEY, stamp

        plain = build_dashboard(resolved())
        stamped = stamp(plain)
        assert {k: v for k, v in stamped.items() if k != GENERATED_KEY} == plain

    def test_stamping_is_idempotent(self):
        """Re-saving must not change the fingerprint, or it would refresh forever."""
        from custom_components.ess_controller.dashboard import stamp

        once = stamp(build_dashboard(resolved()))
        assert stamp(once) == once


class TestActionWording:
    """The plan table used the raw enum values, which read as jargon.

    "Charge" gave no hint that the energy was being bought and "Idle" sounded
    like nothing had been decided, when in fact the battery was being held shut
    while the house ran off the grid at whatever the price happened to be.
    """

    def test_every_action_has_wording(self):
        from custom_components.ess_controller.models import SlotAction

        for action in SlotAction:
            assert action.value in ACTION_WORDS

    def test_grid_charging_says_grid(self):
        assert "grid" in ACTION_WORDS["charge"].lower()

    def test_solar_charging_says_solar(self):
        assert "solar" in ACTION_WORDS["charge_solar_only"].lower()

    def test_a_hold_says_where_the_house_gets_its_power(self):
        assert "grid" in ACTION_WORDS["idle"].lower()

    def test_a_hold_that_buys_nothing_does_not_blame_the_grid(self):
        words = ACTION_WORDS[IDLE_ON_SOLAR].lower()
        assert "grid" not in words
        assert "sun" in words
        # Still recognisably the same decision, so the two rows read as a pair
        # rather than as unrelated states.
        assert "hold" in words

    def test_the_solar_hold_is_display_only(self):
        """Nothing decides it: the table derives it from what the slot buys."""
        from custom_components.ess_controller.models import SlotAction

        assert IDLE_ON_SOLAR not in {action.value for action in SlotAction}

    def test_the_table_maps_the_raw_values(self):
        table = _plan_table("sensor.plan")
        for value, words in ACTION_WORDS.items():
            assert f"'{value}': '{words}'" in table

    def test_the_soc_column_says_it_is_the_plan(self):
        """ "SoC" beside a live battery badge reads as the battery's state now."""
        assert "Planned SoC" in _plan_table("sensor.plan")
        assert "| SoC |" not in _plan_table("sensor.plan")


class TestChartsWhenApexIsInstalled:
    """ApexCharts cannot be required, so both shapes have to be right."""

    def test_without_apex_nothing_custom_is_emitted(self):
        types = {card["type"] for card in walk_cards(build_dashboard(resolved()))}
        assert not any(kind.startswith("custom:") for kind in types)

    def test_with_apex_the_price_shape_becomes_a_chart(self):
        types = {
            card["type"] for card in walk_cards(build_dashboard(resolved(), charts=True))
        }
        assert APEX_CARD in types

    @staticmethod
    def plan_view(charts: bool) -> dict:
        config = build_dashboard(resolved(), charts=charts)
        view = next(v for v in config["views"] if v["path"] == "plan")
        return {"views": [view]}

    def test_the_bars_go_away_when_a_real_chart_replaces_them(self):
        """On the Plan view only. The Overview's six-row glance has no chart
        beside it, so its bars are still the only shape on offer there."""
        markdown = [
            c for c in walk_cards(self.plan_view(True)) if c["type"] == "markdown"
        ]
        assert markdown
        assert not any("█" in c.get("content", "") for c in markdown)

    def test_the_bars_stay_when_there_is_no_chart(self):
        markdown = [
            c for c in walk_cards(self.plan_view(False)) if c["type"] == "markdown"
        ]
        assert any("█" in c.get("content", "") for c in markdown)

    def test_the_table_survives_either_way(self):
        for charts in (False, True):
            cards = walk_cards(build_dashboard(resolved(), charts=charts))
            content = " ".join(
                c.get("content", "") for c in cards if c["type"] == "markdown"
            )
            assert "Planned SoC" in content

    def test_price_and_soc_are_separate_charts(self):
        """One y-axis each. A price and a percentage share no scale, and a
        reader invited to find where they cross is being misled."""
        charts = [
            c
            for c in walk_cards(build_dashboard(resolved(), charts=True))
            if c["type"] == APEX_CARD
        ]
        assert len(charts) == 2
        for chart in charts:
            units = {series.get("unit") for series in chart["series"]}
            assert len(units) == 1

    def test_charts_read_the_plan_attribute_not_history(self):
        charts = [
            c
            for c in walk_cards(build_dashboard(resolved(), charts=True))
            if c["type"] == APEX_CARD
        ]
        generators = [
            series.get("data_generator", "")
            for chart in charts
            for series in chart["series"]
        ]
        assert any("attributes.slots" in gen for gen in generators)

    def test_charts_look_forwards(self):
        """Left alone the card plots the span *ending* now, which would put the
        entire forward plan off the right-hand edge."""
        for chart in [
            c
            for c in walk_cards(build_dashboard(resolved(), charts=True))
            if c["type"] == APEX_CARD
        ]:
            assert chart["span"]["start"] == "minute"

    def test_installing_apex_changes_the_fingerprint(self):
        """So a dashboard nobody has edited picks the charts up on its own."""
        plain = fingerprint(build_dashboard(resolved()))
        charted = fingerprint(build_dashboard(resolved(), charts=True))
        assert plain != charted


class TestTheBuyColumnSeparatesSunFromGrid:
    """ "Grid charge" is set whenever *any* of a charge has to be bought.

    That is the right label for control -- even a sliver from the grid means the
    inverter needs grid charging permitted -- but it reads as though the whole
    slot is being imported, which at 43p looks like a serious mistake and is not
    one. A slot topping up from the sun and taking 0.05 kWh off the grid was
    indistinguishable from one importing flat out, so the number goes on the page.
    """

    @staticmethod
    def render(slot: dict) -> str:
        import datetime as _dt

        import jinja2

        # Parsed for real, not stubbed to zero. A stub that returns 0.0 for every
        # timestamp cannot tell a half-hour from eighteen minutes, which is
        # precisely the difference that put 0.11 kWh of grid import beside the
        # words "Solar charge" on a live dashboard.
        def as_timestamp(value, default=None):
            try:
                return _dt.datetime.fromisoformat(str(value)).timestamp()
            except (TypeError, ValueError):
                return default

        template = _plan_table("sensor.plan", bars=False)
        env = jinja2.Environment(autoescape=False)
        env.globals["state_attr"] = lambda *_: [slot]
        env.globals["as_timestamp"] = as_timestamp
        env.filters["timestamp_custom"] = lambda *_a, **_k: "17:00"
        return env.from_string(template).render()

    @staticmethod
    def slot(**kwargs) -> dict:
        base = {
            "start": "2026-08-12T17:00:00+00:00",
            "end": "2026-08-12T17:30:00+00:00",
            "import_price": 43.0,
            "action": "charge",
            "soc_end": 95.0,
            "charge_power_kw": 0.6,
            "pv_kwh": 0.25,
            "load_kwh": 0.0,
        }
        base.update(kwargs)
        return base

    def test_a_mostly_solar_charge_shows_the_small_purchase(self):
        # 0.3 kWh of charge, 0.25 kWh of it sunshine.
        rendered = self.render(self.slot())
        assert "0.05" in rendered

    def test_a_pure_solar_charge_shows_nothing_bought(self):
        rendered = self.render(
            self.slot(action="charge_solar_only", charge_power_kw=0.4, pv_kwh=0.5)
        )
        assert "—" in rendered

    def test_a_full_grid_charge_shows_the_whole_amount(self):
        rendered = self.render(self.slot(charge_power_kw=3.6, pv_kwh=0.0))
        assert "1.80" in rendered

    def test_household_load_is_not_counted_as_battery_charge(self):
        """Grid import for the house is not the battery being bought."""
        rendered = self.render(self.slot(charge_power_kw=0.0, pv_kwh=0.0, load_kwh=2.0))
        assert "—" in rendered

    def test_the_sun_covering_the_house_first_is_respected(self):
        # 1 kWh of sun, 0.9 used by the house: only 0.1 spare for the battery.
        rendered = self.render(self.slot(charge_power_kw=0.6, pv_kwh=1.0, load_kwh=0.9))
        assert "0.20" in rendered

    def test_the_half_hour_already_running_is_not_counted_as_a_whole_one(self):
        """The first row of a live plan is a part-slot, and this assumed thirty
        minutes for the charge while taking the real, shorter sun and load beside
        it. On a real dashboard that put 0.11 kWh of grid import against the words
        "Solar charge"."""
        rendered = self.render(
            self.slot(
                start="2026-08-14T17:12:00+00:00",
                end="2026-08-14T17:30:00+00:00",
                action="charge_solar_only",
                charge_power_kw=0.72,  # 0.216 kWh over eighteen minutes
                pv_kwh=0.30,
                load_kwh=0.08,
            )
        )
        assert "0.11" not in rendered
        assert "—" in rendered

    def test_a_solar_only_charge_never_shows_a_purchase(self):
        """The optimiser only uses that label when the charge fits in the surplus,
        so a figure derived by subtraction can only ever contradict it."""
        rendered = self.render(
            self.slot(action="charge_solar_only", charge_power_kw=3.6, pv_kwh=0.0)
        )
        assert "1.80" not in rendered
        assert "—" in rendered

    def test_the_column_is_explained(self):
        assert "off the grid into the battery" in _plan_table("sensor.plan")


class TestTheStateLegend:
    """State names on a page mean nothing without a line each.

    Tied to ``ACTION_WORDS`` by key so the legend cannot drift out of step with
    the table above it, and so a new action cannot ship undocumented.
    """

    def test_every_action_is_explained(self):
        from custom_components.ess_controller.models import SlotAction

        for action in SlotAction:
            assert action.value in ACTION_NOTES, action.value

    def test_the_legend_names_match_the_table(self):
        legend = _action_legend()
        for words in ACTION_WORDS.values():
            assert words in legend

    def test_it_stays_brief(self):
        """A legend, not documentation. Two short columns per state."""
        for does, why in ACTION_NOTES.values():
            assert len(does) <= 60, does
            assert len(why) <= 60, why

    def test_it_is_a_markdown_table(self):
        legend = _action_legend()
        assert legend.startswith("| State | What happens | Why |")
        assert legend.count("\n") == len(ACTION_NOTES) + 2

    def test_it_is_on_the_plan_view(self):
        config = build_dashboard(resolved())
        view = next(v for v in config["views"] if v["path"] == "plan")
        content = " ".join(
            card.get("content", "")
            for card in walk_cards({"views": [view]})
            if card["type"] == "markdown"
        )
        assert "What happens" in content

    def test_a_hold_is_distinguished_from_a_solar_charge(self):
        """The question this legend exists to answer: on a sunny half-hour the two
        look the same, because a hold blocks discharging but not the array."""
        hold_does, hold_why = ACTION_NOTES["idle"]
        assert "discharge" in hold_does.lower()
        assert "sun" in hold_does.lower()
        assert "later" in hold_why.lower()
        assert ACTION_NOTES["charge_solar_only"][0] != hold_does

    def test_the_hold_state_name_says_where_the_power_comes_from(self):
        assert "grid" in ACTION_WORDS["idle"].lower()

    def test_the_hold_reason_is_a_comparison_not_a_price(self):
        """ "The grid is cheap" was wrong as often as it was right.

        Printed beside a 45.4p half-hour it made the plan look deranged rather
        than patient: the hold is chosen because the charge earns more later, and
        that is true whatever this half-hour costs.
        """
        _, why = ACTION_NOTES["idle"]
        assert "cheap" not in why.lower()

    def test_the_solar_hold_has_its_own_line(self):
        does, why = ACTION_NOTES[IDLE_ON_SOLAR]
        assert does != ACTION_NOTES["idle"][0]
        assert "sun" in does.lower()
        # It is the *absence* of a purchase that distinguishes it, so the legend
        # has to say so or the two hold rows look like a rendering bug.
        assert "bought" in why.lower() or "buy" in why.lower()


class TestGettingTheDiagnosticsOut:
    """The file is the first thing asked for, and the hardest thing to find.

    Settings, then Devices & services, then the integration, then a menu behind
    three dots. Nothing on the dashboard said so.
    """

    @staticmethod
    def _prose(config):
        return " ".join(
            card.get("content", "")
            for card in walk_cards(config)
            if card["type"] == "markdown"
        )

    def test_the_shortcut_is_on_the_settings_view(self):
        config = build_dashboard(resolved())
        view = next(v for v in config["views"] if v["path"] == "settings")
        prose = self._prose({"views": [view]})
        assert "/config/integrations/integration/ess_controller" in prose
        assert "Download diagnostics" in prose

    def test_it_says_what_to_press_when_it_gets_there(self):
        """A link to a page whose button is behind a menu needs the last step."""
        prose = self._prose(build_dashboard(resolved()))
        assert "three dots" in prose

    def test_it_is_not_a_tile_without_an_entity(self):
        """A tile card wants an entity; one without renders as an error box."""
        for card in walk_cards(build_dashboard(resolved())):
            if card["type"] == "tile":
                assert "entity" in card, card

    def test_it_does_not_conjure_a_view_out_of_nothing(self):
        """The rule against views that are only static prose still applies."""
        config = build_dashboard(resolved("plan_action"))
        paths = [view["path"] for view in config["views"]]
        assert "settings" not in paths


class TestPlannedFiguresSayPlanned:
    """A plan's number read as a live reading is the recurring bug on this page.

    The entity names already said "Planned charge power"; the dashboard shortened
    them to "Charge power" on the grounds that the section heading supplied the
    context. It does not. "Charge power 0.00 kW" beside a battery visibly taking
    619 W from the array reads as a broken sensor, and the card disagreed with its
    own more-info dialog. Same mistake as labelling the projected SoC "SoC".
    """

    PLANNED_KEYS = (
        "planned_charge_power",
        "planned_discharge_power",
        "target_soc",
        "charging_planned",
        "discharging_planned",
        "exporting_planned",
        "planned_grid_import",
        "planned_grid_export",
    )

    def test_every_planned_figure_is_labelled_as_planned(self):
        from custom_components.ess_controller.dashboard import LABELS

        for key in self.PLANNED_KEYS:
            assert "planned" in LABELS[key].lower(), key

    def test_the_labels_match_the_entity_names(self):
        """A card row and its dialog must not describe the same number differently."""
        import json
        from pathlib import Path

        from custom_components.ess_controller.dashboard import LABELS

        strings = json.loads(
            (Path("custom_components/ess_controller/strings.json")).read_text()
        )["entity"]
        for key in self.PLANNED_KEYS:
            for domain in ("sensor", "binary_sensor"):
                entry = strings.get(domain, {}).get(key)
                if entry is None:
                    continue
                assert "planned" in entry["name"].lower(), f"{domain}.{key}"
                assert "planned" in LABELS[key].lower(), key

    def test_the_badge_keeps_a_short_name(self):
        """Four words do not fit in a badge; the row above it carries the detail."""
        config = build_dashboard(resolved())
        view = next(v for v in config["views"] if v["path"] == "plan")
        names = [b.get("name") for b in view.get("badges", [])]
        assert "Target" in names

    def test_the_card_still_uses_the_full_name(self):
        from custom_components.ess_controller.dashboard import LABELS

        config = build_dashboard(resolved())
        rows = [
            row.get("name")
            for card in walk_cards(config)
            if card["type"] == "entities"
            for row in card.get("entities", [])
        ]
        assert LABELS["planned_charge_power"] in rows
        assert LABELS["target_soc"] in rows
