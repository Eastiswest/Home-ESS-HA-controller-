"""Tests for the generated dashboard.

The point of these is that the dashboard is *valid whatever exists*: entities can
be disabled, renamed, or absent because a platform failed, and none of that may
produce a card pointing at nothing or an empty view.
"""

from __future__ import annotations

import pytest

from custom_components.ess_controller.dashboard import (
    DASHBOARD_TITLE,
    PLAN_TABLE_SLOTS,
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
    """Every card in the dashboard, including nested stacks."""
    found: list[dict] = []

    def visit(cards: list[dict]) -> None:
        for card in cards:
            found.append(card)
            if "cards" in card:
                visit(card["cards"])

    for view in config["views"]:
        visit(view["cards"])
    return found


def referenced_entities(config: dict) -> set[str]:
    """Entity ids a card points at directly, not ones mentioned in templates."""
    ids: set[str] = set()
    for card in walk_cards(config):
        if isinstance(card.get("entity"), str):
            ids.add(card["entity"])
        for item in card.get("entities", []):
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict) and isinstance(item.get("entity"), str):
                ids.add(item["entity"])
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

    def test_every_view_has_a_title_icon_and_cards(self):
        for view in build_dashboard(resolved())["views"]:
            assert view["title"]
            assert view["icon"].startswith("mdi:")
            assert view["cards"]

    def test_every_card_declares_a_type(self):
        for card in walk_cards(build_dashboard(resolved())):
            assert card.get("type"), card

    def test_only_stock_card_types_are_used(self):
        """A custom card would render as an error box unless separately installed."""
        stock = {
            "entities",
            "gauge",
            "glance",
            "markdown",
            "button",
            "horizontal-stack",
            "history-graph",
        }
        used = {card["type"] for card in walk_cards(build_dashboard(resolved()))}
        assert used <= stock, used - stock


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

    def test_views_with_no_cards_are_dropped_entirely(self):
        """An empty view would be a blank tab in the sidebar."""
        config = build_dashboard(resolved("min_soc", "max_soc"))
        assert [view["path"] for view in config["views"]] == ["settings"]

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
        assert f"slots[:{PLAN_TABLE_SLOTS}]" in content

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
                        "import_price": -5.0 + hour,
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
        assert "| 00:00 | -5.0p | Charge solar only | 40% |" in table
        assert table.count("\n|") >= 5  # header, separator, four slots
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
        attributes = {
            wear: {
                "source": "derived from pack cost",
                "net_cost": 500.0,
                "expected_cycles": 1500.0,
                "usable_kwh": 17.6,
                "lifetime_throughput_kwh": 26400.0,
                "spread_needed": 2.1,
                "negative_price_threshold": -1.8,
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
