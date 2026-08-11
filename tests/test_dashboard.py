"""Tests for the generated dashboard.

The point of these is that the dashboard is *valid whatever exists*: entities can
be disabled, renamed, or absent because a platform failed, and none of that may
produce a card pointing at nothing or an empty view.
"""

from __future__ import annotations

import pytest

from custom_components.ess_controller.dashboard import (
    DASHBOARD_TITLE,
    build_dashboard,
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

    def test_plan_table_renders_the_fallback_with_no_plan(self):
        cards = self._cards(build_dashboard(resolved("plan_cost")))
        for card in cards:
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
        assert "| Spent | 1160.0p |" in rendered
        assert "| **Net saving** | **300.0p** |" in rendered
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
        assert "| Immersion | 03:00 to 04:00 | -13.5p | yes |" in rendered
        assert "| Dishwasher | 03:30 to 04:06 | -5.6p | by hand |" in rendered

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
