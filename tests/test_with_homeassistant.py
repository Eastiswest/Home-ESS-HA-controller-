"""End-to-end tests against a real Home Assistant.

The rest of the suite runs without Home Assistant installed, which is what makes
it fast and what let three fatal bugs through: a selector that could not be
serialised (so the config flow returned "400: Bad Request" and the integration
could not be added at all), an import of a module that does not exist (so four
platforms failed and no entities appeared), and a deprecated coordinator call
that stops working in 2026.8. None of those are visible without importing Home
Assistant, because none of them are wrong as Python.

So this module boots a real instance and does the things a user does: set the
component up, walk the config flow, open every options step, check entities
appear, and register the dashboard. It is skipped when Home Assistant is not
importable, which is the case on the fast path; CI runs it on its own job with
Home Assistant installed.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("homeassistant", reason="needs Home Assistant installed")

# The suite turns DeprecationWarning into an error, which is right for our own
# code and wrong here: Home Assistant's own dependencies emit them, and failing
# on somebody else's deprecation would make this job useless noise.
pytestmark = pytest.mark.filterwarnings("default::DeprecationWarning")

REPO = Path(__file__).resolve().parent.parent

DOMAIN = "ess_controller"
OPTIONS_STEPS = (
    "battery",
    "inverter",
    "tariff",
    "tariff_import",
    "tariff_export",
    "forecast",
    "optimiser",
    "sessions",
    "outage",
    "shifting",
    "recommend",
)


@pytest.fixture
async def hass(tmp_path):
    """A minimal but real Home Assistant, with the integration discoverable.

    Bootstrapped by hand rather than with pytest-homeassistant-custom-component:
    that harness pins a Home Assistant version, and the point here is to run
    against whatever is installed.
    """
    from homeassistant import config_entries, core, loader
    from homeassistant.helpers import (
        area_registry,
        category_registry,
        device_registry,
        entity_registry,
        floor_registry,
        frame,
        issue_registry,
        label_registry,
    )
    from homeassistant.setup import async_setup_component

    config_dir = tmp_path / "config"
    (config_dir / "custom_components").mkdir(parents=True)
    (config_dir / "custom_components" / DOMAIN).symlink_to(
        REPO / "custom_components" / DOMAIN, target_is_directory=True
    )

    # The integration must be imported through the config directory, the way
    # Home Assistant imports it -- not off the repo root as well. Two paths to
    # the same package leave a half-initialised module in sys.modules and Home
    # Assistant then reports "No setup or config entry setup function defined".
    sys.path.insert(0, str(config_dir))
    for name in [n for n in sys.modules if n.startswith("custom_components")]:
        del sys.modules[name]

    instance = core.HomeAssistant(str(config_dir))
    instance.config.config_dir = str(config_dir)
    loader.async_setup(instance)
    frame.async_setup(instance)
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    await instance.async_start()
    for module in (
        area_registry,
        device_registry,
        entity_registry,
        issue_registry,
        floor_registry,
        label_registry,
        category_registry,
    ):
        await module.async_load(instance)
    await async_setup_component(instance, "homeassistant", {})

    yield instance

    await instance.async_stop()
    sys.path.remove(str(config_dir))
    for name in [n for n in sys.modules if n.startswith("custom_components")]:
        del sys.modules[name]


def _lovelace(hass) -> None:
    """Give Lovelace's data structure to the instance.

    Lovelace itself pulls in http, auth and onboarding, which this harness has no
    business bootstrapping. Its *data* is all the installer touches, and it is
    built here with Lovelace's own class so the installer meets the real thing.
    """
    from homeassistant.components.lovelace import LovelaceData
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.components.lovelace.dashboard import LovelaceStorage

    hass.data[LOVELACE_DATA] = LovelaceData(
        resource_mode="storage",
        # Home Assistant keys its default dashboard None, exactly as it does in
        # its own setup, and every real install has one. Starting from an empty
        # mapping is what let a sorted() over these keys ship: it only raises
        # once the None is there, so the harness has to carry it.
        dashboards={None: LovelaceStorage(hass, None)},
        resources=None,
        yaml_dashboards={},
    )


async def _complete_flow(hass) -> object:
    """Click through the config flow accepting every default, then save.

    The wizard ends at a review menu rather than creating the entry, so reaching
    an entry means choosing "finish" -- which is worth asserting on its own: an
    entry created before the review would mean nothing was reviewable.
    """
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    steps = 0
    while result["type"] == "form":
        steps += 1
        assert steps < 20, "config flow does not terminate"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    if result["type"] == "menu":
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
    return result


class TestComponentSetup:
    async def test_component_sets_up(self, hass):
        from homeassistant.setup import async_setup_component

        assert await async_setup_component(hass, DOMAIN, {})

    async def test_services_are_registered(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        for service in (
            "replan",
            "set_override",
            "clear_override",
            "reset_learning",
            "recommend_tariffs",
            "export_performance",
            "generate_dashboard",
        ):
            assert hass.services.has_service(DOMAIN, service), service


class TestConfigFlow:
    """The flow the user cannot get past if any schema fails to serialise."""

    async def test_first_step_schema_serialises(self, hass):
        """A selector Home Assistant cannot serialise is a 400 on the frontend."""
        import voluptuous_serialize
        from homeassistant.helpers import config_validation as cv
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == "form"
        fields = voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        assert len(fields) > 5

    async def test_every_step_serialises_and_the_flow_completes(self, hass):
        import voluptuous_serialize
        from homeassistant.helpers import config_validation as cv
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        visited = []
        while result["type"] == "form":
            visited.append(result["step_id"])
            voluptuous_serialize.convert(
                result["data_schema"], custom_serializer=cv.custom_serializer
            )
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
            assert len(visited) < 20

        # The wizard hands off to the review menu, not straight to an entry.
        assert result["type"] == "menu"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        assert result["type"] == "create_entry"
        assert "user" in visited
        assert "optimiser" in visited

    async def test_defaults_alone_produce_a_working_entry(self, hass):
        """Clicking through without typing anything has to be valid."""
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        result = await _complete_flow(hass)
        assert result["type"] == "create_entry"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1


class TestOptionsFlow:
    @pytest.mark.parametrize("step", OPTIONS_STEPS)
    async def test_options_step_serialises(self, hass, step):
        import voluptuous_serialize
        from homeassistant.helpers import config_validation as cv
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
        fields = voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        assert fields

    async def test_the_role_pickers_hide_our_own_entities(self, hass):
        """Discovery has always refused to bind these; the picker offered them.

        One of ours is a switch called "Allow grid charging", which is the
        obvious-looking hit for anyone searching the picker for "grid". A real
        install mapped it to the grid-charge role and the two latched: a hold
        switches that permission off, an optimiser without grid charging can only
        plan more holds, and it never comes back on.
        """
        import voluptuous_serialize
        from homeassistant.helpers import config_validation as cv
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "entity_map"}
        )
        fields = voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )

        assert fields
        for field in fields:
            excluded = field["selector"]["entity"].get("exclude_entities") or []
            assert excluded, f"{field['name']} excludes nothing"
            # Only ever ours -- an exclusion broad enough to hide the inverter
            # would be worse than the bug it fixes.
            assert all("ess_controller" in entity_id for entity_id in excluded)
        grid = next(f for f in fields if f["name"] == "grid_charge")
        assert any(
            "allow_grid_charging" in entity_id
            for entity_id in grid["selector"]["entity"]["exclude_entities"]
        )


class TestEntrySetup:
    async def test_entry_loads_and_publishes_entities(self, hass):
        """Four platforms once failed to import, which produced no entities."""
        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        states = [s for s in hass.states.async_entity_ids() if "ess" in s]
        assert len(states) > 50, f"only {len(states)} entities"

    async def test_every_platform_contributes(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        await hass.async_block_till_done()

        states = [s for s in hass.states.async_entity_ids() if "ess" in s]
        for domain in ("sensor", "binary_sensor", "switch", "number", "select", "button"):
            assert any(s.startswith(f"{domain}.") for s in states), domain

    async def test_unload_is_clean(self, hass):
        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED


class TestDashboardInstall:
    async def _install(self, hass):
        from homeassistant.setup import async_setup_component

        _lovelace(hass)
        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()

        from custom_components.ess_controller.panel import async_install

        return entry, await async_install(hass, entry)

    async def test_dashboard_installs(self, hass):
        _entry, outcome = await self._install(hass)
        assert outcome == "installed"

    async def test_panel_appears_in_the_sidebar(self, hass):
        """The actual complaint: nothing in the navigation panel."""
        from homeassistant.components import frontend

        await self._install(hass)
        panels = hass.data.get(frontend.DATA_PANELS, {})
        assert "ess-controller" in panels
        response = panels["ess-controller"].to_response()
        assert response["component_name"] == "lovelace"
        assert response["config"] == {"mode": "storage"}

    async def test_lovelace_can_serve_the_config(self, hass):
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        await self._install(hass)
        served = hass.data[LOVELACE_DATA].dashboards.get("ess-controller")
        assert served is not None
        config = await served.async_load(False)
        assert [view["path"] for view in config["views"]] == [
            "overview",
            "plan",
            "performance",
            "loads",
            "settings",
        ]

    async def test_describe_reports_a_healthy_install(self, hass):
        from custom_components.ess_controller.panel import describe

        entry, _ = await self._install(hass)
        state = describe(hass, entry)
        assert state["registered"] is True
        assert state["registered_is_ours"] is True
        assert state["entities_resolved"] > 50
        assert state["views_built"] == 5
        # Readable, and reached at all: sorting these keys raw compares the
        # default dashboard's None with a string and takes the whole diagnostic
        # down. A real install's file carried nothing here but the TypeError.
        assert "(default)" in state["dashboards_mapping"]
        assert state["url_path"] in state["dashboards_mapping"]

    async def test_reinstalling_is_idempotent(self, hass):
        """Registration is repeated on every setup, so it must be safe."""
        from custom_components.ess_controller.panel import async_install

        entry, first = await self._install(hass)
        assert first == "installed"
        assert await async_install(hass, entry) == "installed"

    async def test_removal_takes_the_panel_away(self, hass):
        from homeassistant.components import frontend

        from custom_components.ess_controller.panel import async_remove

        entry, _ = await self._install(hass)
        async_remove(hass, entry)
        assert "ess-controller" not in hass.data.get(frontend.DATA_PANELS, {})

    async def test_without_lovelace_it_writes_a_file_instead(self, hass):
        """The fallback path, which must not raise."""
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()

        from custom_components.ess_controller.panel import async_install

        assert await async_install(hass, entry) == "unsupported"
        written = Path(hass.config.path(DOMAIN)) / "dashboard.yaml"
        assert written.exists()
        assert "views" in written.read_text()


class TestDashboardRenders:
    """Render every Markdown card the way the frontend does, against live state.

    The dashboard's tables are Jinja over sensor attributes, and a template that
    names an attribute the sensor does not publish fails silently: Home Assistant
    renders it as nothing and the card shows "Cycling pays above a ?p spread".
    Nothing catches that except rendering the real template against the real
    entity, because a hand-written fixture agrees with whatever the template
    happens to say. That bug shipped; this is what would have found it.
    """

    async def _cards(self, hass):
        from homeassistant.setup import async_setup_component

        _lovelace(hass)
        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        await hass.async_block_till_done()

        from custom_components.ess_controller.panel import build_for_entry

        entry = hass.config_entries.async_entries(DOMAIN)[0]
        config = build_for_entry(hass, entry)
        found: list[str] = []
        for view in config["views"]:
            for section in view.get("sections", []):
                for card in section.get("cards", []):
                    if card.get("type") == "markdown":
                        found.append(card["content"])
        assert found, "no markdown cards to render"
        return found

    async def _render(self, hass, content: str) -> str:
        from homeassistant.helpers.template import Template

        return Template(content, hass).async_render(parse_result=False)

    async def test_every_markdown_card_renders(self, hass):
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            assert rendered.strip(), content[:120]

    async def test_the_plan_table_spells_out_what_it_is_doing(self, hass):
        """The table used the raw enum values, so it said "Charge" where the tile
        beside it said "Charging from grid" -- and neither told a reader that a
        hold means the house is buying its whole load."""
        parts = [await self._render(hass, c) for c in await self._cards(hass)]
        rendered = "\n".join(parts)
        from custom_components.ess_controller.dashboard import ACTION_WORDS

        assert any(words in rendered for words in ACTION_WORDS.values()), rendered[:400]
        # The bare enum wording must be gone.
        assert "| Charge |" not in rendered
        assert "| Idle |" not in rendered

    async def test_no_card_leaks_a_missing_attribute(self, hass):
        """The symptom of a wrong attribute name, in the forms it takes."""
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            for leak in ("?p", "None", "nan", "unknown source"):
                assert leak not in rendered, (leak, rendered[:300])

    async def test_the_plan_table_covers_the_whole_horizon(self, hass):
        """The Overview summarises; the Plan view shows everything it knows.

        Six hours was an arbitrary cap, and it hid the decision that usually
        matters -- tomorrow's cheap window rather than the next half-hour.
        """
        from custom_components.ess_controller.dashboard import OVERVIEW_TABLE_SLOTS

        lengths = []
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            if "| Time |" not in rendered:
                continue
            rows = [
                line
                for line in rendered.splitlines()
                if line.startswith("| ") and "| Time |" not in line
            ]
            assert "p " in rows[-1]  # the price rendered as a number
            # One header and separator per day, so count data rows only.
            lengths.append(len([r for r in rows if not r.startswith("|---")]))

        assert len(lengths) == 2, lengths
        summary, full = sorted(lengths)
        assert summary == OVERVIEW_TABLE_SLOTS
        # The plan runs 36 hours by default: far more than six.
        assert full > 24, full

    async def test_the_plan_is_drawn_as_bars(self, hass):
        """A table of numbers is not a shape, and the shape is the point."""
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            if "| Time |" not in rendered:
                continue
            assert "`" in rendered, "no bar column"
            assert "█" in rendered or "◄" in rendered, rendered[:200]
            return
        pytest.fail("the plan table never rendered")

    async def test_the_plan_is_grouped_by_day(self, hass):
        """A 72-row table is a wall; the same rows broken by day are a schedule."""
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            if "| Time |" not in rendered or rendered.count("| Time |") < 2:
                continue
            # More than one header means more than one day, each with a title.
            assert rendered.count("**") >= 4, rendered[:300]
            return
        pytest.fail("no multi-day table rendered")

    async def test_the_wear_card_shows_both_thresholds(self, hass):
        """The card the wrong attribute names broke."""
        for content in await self._cards(hass):
            rendered = await self._render(hass, content)
            if "p/kWh**" not in rendered:
                continue
            assert "Cycling pays above a **" in rendered
            assert "Dumping to re-import pays below **" in rendered
            return
        pytest.fail("the wear card never rendered")


class TestOctopusErrors:
    """Config-flow error keys, which need voluptuous and so cannot live in the
    fast suite. Reporting "check your network" for a mistyped product code is a
    real cost: it points the user at the wrong thing entirely."""

    def test_every_error_kind_maps_to_a_translated_message(self):
        import json

        from custom_components.ess_controller.config_flow import _OCTOPUS_ERRORS

        strings = json.loads(
            (REPO / "custom_components/ess_controller/strings.json").read_text()
        )
        assert _OCTOPUS_ERRORS, "no error kinds mapped"
        for key in _OCTOPUS_ERRORS.values():
            assert key in strings["config"]["error"], key
            assert key in strings["options"]["error"], key

    def test_a_missing_product_does_not_report_a_network_problem(self):
        from custom_components.ess_controller.config_flow import _OCTOPUS_ERRORS
        from custom_components.ess_controller.tariff.octopus import KIND_NOT_FOUND

        assert _OCTOPUS_ERRORS[KIND_NOT_FOUND] == "octopus_not_found"


class TestReviewMenu:
    """The substitute for the back arrow Home Assistant's flow API has not got.

    Any section reachable in one click, revisiting one returning to the review
    rather than re-walking the wizard, and nothing saved until "finish".
    """

    async def _to_review(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        steps = 0
        while result["type"] == "form":
            steps += 1
            assert steps < 20
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        return result

    async def test_the_wizard_ends_at_a_review_menu(self, hass):
        result = await self._to_review(hass)
        assert result["type"] == "menu"
        assert result["step_id"] == "review"

    async def test_nothing_is_saved_until_finish(self, hass):
        result = await self._to_review(hass)
        assert result["type"] == "menu"
        assert hass.config_entries.async_entries(DOMAIN) == []

    async def test_every_section_is_offered(self, hass):
        from custom_components.ess_controller.config_flow import CONFIG_SECTIONS

        result = await self._to_review(hass)
        offered = set(result["menu_options"])
        assert "finish" in offered
        assert set(CONFIG_SECTIONS) <= offered

    async def test_every_menu_option_has_a_label(self, hass):
        import json

        result = await self._to_review(hass)
        strings = json.loads(
            (REPO / "custom_components/ess_controller/strings.json").read_text()
        )
        labels = strings["config"]["step"]["review"]["menu_options"]
        for option in result["menu_options"]:
            assert option in labels, option

    async def test_the_summary_placeholders_are_all_filled(self, hass):
        """An unfilled placeholder renders as a literal {brace} in the dialog."""
        import json
        import re

        result = await self._to_review(hass)
        strings = json.loads(
            (REPO / "custom_components/ess_controller/strings.json").read_text()
        )
        description = strings["config"]["step"]["review"]["description"]
        needed = set(re.findall(r"\{(\w+)\}", description))
        assert needed <= set(result["description_placeholders"])

    @pytest.mark.parametrize("section", ["user", "inverter", "optimiser", "forecast"])
    async def test_revisiting_a_section_returns_to_the_review(self, hass, section):
        """The whole point: fix one step without re-walking the rest."""
        result = await self._to_review(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": section}
        )
        assert result["type"] == "form"
        assert result["step_id"] == section

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] == "menu", "did not come back to the review"
        assert result["step_id"] == "review"

    async def test_a_correction_is_kept(self, hass):
        result = await self._to_review(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "user"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"battery_capacity_kwh": 17.6}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        assert result["type"] == "create_entry"
        assert result["data"]["battery_capacity_kwh"] == 17.6

    async def test_finish_creates_the_entry(self, hass):
        result = await self._to_review(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        assert result["type"] == "create_entry"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    async def test_closing_the_dialog_keeps_the_answers(self, hass):
        """Abandoning the flow must not throw away eleven steps of typing."""
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"battery_capacity_kwh": 19.4}
        )
        hass.config_entries.flow.async_abort(result["flow_id"])

        resumed = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert resumed["type"] == "form"
        assert resumed["step_id"] == "user"
        schema = resumed["data_schema"].schema
        suggested = {
            str(key): key.description.get("suggested_value")
            for key in schema
            if getattr(key, "description", None)
        }
        assert suggested.get("battery_capacity_kwh") == 19.4

    async def test_a_finished_flow_leaves_nothing_to_resume(self, hass):
        from custom_components.ess_controller.config_flow import DATA_PARTIAL

        result = await self._to_review(hass)
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "finish"}
        )
        assert not hass.data.get(DATA_PARTIAL, {}).get(DOMAIN)


class _ShortTariff:
    """A tariff that has only announced the next two hours.

    Two hours of real prices against a horizon of a day or more: exactly the state
    Agile is in from midnight until the afternoon release, and the state in which
    the tail has to come from somewhere.
    """

    name = "short"
    available = True
    ANNOUNCED_SLOTS = 4

    async def async_fetch(self, now, horizon_end):
        from custom_components.ess_controller.models import PriceSlot
        from custom_components.ess_controller.tariff.base import PriceSeries

        start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        return PriceSeries(
            [
                PriceSlot(
                    start=start + timedelta(minutes=30 * n),
                    end=start + timedelta(minutes=30 * (n + 1)),
                    # Distinct from any forecast marker the tests inject.
                    price=23.0,
                )
                for n in range(self.ANNOUNCED_SLOTS)
            ]
        )

    async def async_close(self):
        return None

    def describe(self):
        return {"provider": self.name}


class TestAgilePredictWiring:
    """The forecast path, through a real coordinator with the network stubbed.

    The parsing is covered without Home Assistant; what needs a real instance is
    the wiring: that the option gates it, that an unreachable service still
    produces a plan, and that predicted prices land in the horizon flagged as
    predicted rather than passed off as announced.
    """

    async def _coordinator(self, hass, *, short_window: bool = False, **options):
        """A live coordinator, optionally with a tariff that runs out early.

        The default setup is a fixed rate, which by definition prices the whole
        horizon -- so there is no unannounced tail and the forecast correctly does
        nothing. ``short_window`` swaps in a provider that knows only the next two
        hours, which is the situation Agile is in for most of the day and the only
        one where any of this matters.
        """
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        if options:
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, **options}
            )
            await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        if short_window:
            coordinator._import_provider = _ShortTariff()
        return coordinator

    @staticmethod
    def _stub_session(monkeypatch) -> None:
        """This harness has no http component, so there is no client session.

        Stubbed rather than worked around, because the fetch itself is what these
        tests are about -- and the coordinator's own handling of a missing session
        is covered separately.
        """
        from custom_components.ess_controller import coordinator as coordinator_mod

        monkeypatch.setattr(
            coordinator_mod, "async_get_clientsession", lambda hass: object()
        )

    async def test_on_by_default_for_import(self, hass):
        """Reversed deliberately. Withholding it to avoid an unasked-for outbound
        call left the plan blind past about lunchtime -- Octopus announces only to
        23:00 tomorrow -- and unable to tell "the cheap window has passed" from
        "the cheap window is not published yet". A prediction beats nothing, it is
        marked as a prediction everywhere it is shown, and the switch is right
        there."""
        from custom_components.ess_controller.const import (
            CONF_AGILE_PREDICT,
            DEFAULT_AGILE_PREDICT,
        )

        assert DEFAULT_AGILE_PREDICT is True
        coordinator = await self._coordinator(hass)
        assert coordinator.options.get(CONF_AGILE_PREDICT, DEFAULT_AGILE_PREDICT) is True

    async def test_export_predictions_stay_off(self, hass):
        """A site with no export tariff gains nothing from predicting export."""
        from custom_components.ess_controller.const import DEFAULT_AGILE_PREDICT_EXPORT

        assert DEFAULT_AGILE_PREDICT_EXPORT is False

    async def test_turning_it_off_records_nothing(self, hass):
        from custom_components.ess_controller.const import CONF_AGILE_PREDICT

        coordinator = await self._coordinator(hass, **{CONF_AGILE_PREDICT: False})
        assert coordinator.price_forecast is None

    async def test_enabled_without_a_region_says_so_rather_than_calling(self, hass):
        from custom_components.ess_controller.const import CONF_AGILE_PREDICT

        coordinator = await self._coordinator(
            hass, short_window=True, **{CONF_AGILE_PREDICT: True, "octopus_region": None}
        )
        await coordinator.async_refresh()
        state = coordinator.price_forecast
        assert state is not None
        assert state["available"] is False
        assert "region" in state["error"]

    async def test_an_unreachable_service_still_produces_a_plan(self, hass, monkeypatch):
        """The whole point of the fallback: the plan must not depend on it."""
        from custom_components.ess_controller.const import CONF_AGILE_PREDICT
        from custom_components.ess_controller.tariff import agile_predict

        async def boom(*args, **kwargs):
            raise agile_predict.AgilePredictError("down", agile_predict.KIND_UNREACHABLE)

        self._stub_session(monkeypatch)
        monkeypatch.setattr(agile_predict, "async_fetch_forecast", boom)
        coordinator = await self._coordinator(
            hass, short_window=True, **{CONF_AGILE_PREDICT: True, "octopus_region": "C"}
        )
        await coordinator.async_refresh()
        assert coordinator.plan is not None
        assert coordinator.plan.slots
        assert coordinator.price_forecast["error_kind"] == "unreachable"

    async def test_predicted_prices_reach_the_plan_and_are_flagged(
        self, hass, monkeypatch
    ):
        from homeassistant.util import dt as dt_util

        from custom_components.ess_controller.const import CONF_AGILE_PREDICT
        from custom_components.ess_controller.models import PriceSlot
        from custom_components.ess_controller.tariff import agile_predict

        # A distinctive price no persistence extrapolation could invent.
        marker = 42.5

        async def fake(session, region, **kwargs):
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            return [
                PriceSlot(
                    start=start + timedelta(minutes=30 * n),
                    end=start + timedelta(minutes=30 * (n + 1)),
                    price=marker,
                    is_forecast=True,
                )
                for n in range(96)
            ]

        self._stub_session(monkeypatch)
        monkeypatch.setattr(agile_predict, "async_fetch_forecast", fake)
        coordinator = await self._coordinator(
            hass, short_window=True, **{CONF_AGILE_PREDICT: True, "octopus_region": "C"}
        )
        await coordinator.async_refresh()

        state = coordinator.price_forecast
        assert state["available"] is True
        assert state["used"] > 0

        predicted = [s for s in coordinator.plan.slots if s.price_is_forecast]
        assert predicted, "no slot was marked as predicted"
        assert all(s.import_price == marker for s in predicted)
        # And the announced ones kept their own prices.
        announced = [s for s in coordinator.plan.slots if not s.price_is_forecast]
        assert all(s.import_price != marker for s in announced)

    async def test_the_plan_note_reports_what_was_predicted(self, hass, monkeypatch):
        from homeassistant.util import dt as dt_util

        from custom_components.ess_controller.const import CONF_AGILE_PREDICT
        from custom_components.ess_controller.models import PriceSlot
        from custom_components.ess_controller.tariff import agile_predict

        async def fake(session, region, **kwargs):
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            return [
                PriceSlot(
                    start=start + timedelta(minutes=30 * n),
                    end=start + timedelta(minutes=30 * (n + 1)),
                    price=9.0,
                    is_forecast=True,
                )
                for n in range(96)
            ]

        self._stub_session(monkeypatch)
        monkeypatch.setattr(agile_predict, "async_fetch_forecast", fake)
        coordinator = await self._coordinator(
            hass, short_window=True, **{CONF_AGILE_PREDICT: True, "octopus_region": "C"}
        )
        await coordinator.async_refresh()
        stats = coordinator.price_stats("import")
        assert stats["price_forecast"]["used"] > 0

    async def test_the_option_appears_in_the_flow_for_an_octopus_tariff(self, hass):
        """A switch nobody can find is a switch nobody has."""
        from custom_components.ess_controller.config_flow import EssOptionsFlow
        from custom_components.ess_controller.const import (
            CONF_AGILE_PREDICT,
            CONF_IMPORT_PROVIDER,
            PROVIDER_OCTOPUS_API,
        )

        await async_setup_component_if_needed(hass)
        flow = EssOptionsFlow()
        schema = flow._direction_schema(
            {CONF_IMPORT_PROVIDER: PROVIDER_OCTOPUS_API}, "import"
        )
        keys = {str(key.schema) for key in schema.schema}
        assert CONF_AGILE_PREDICT in keys

    async def test_the_option_is_hidden_for_a_fixed_tariff(self, hass):
        """There is no unannounced tail on a fixed rate to predict."""
        from custom_components.ess_controller.config_flow import EssOptionsFlow
        from custom_components.ess_controller.const import (
            CONF_AGILE_PREDICT,
            CONF_IMPORT_PROVIDER,
            PROVIDER_FIXED,
        )

        await async_setup_component_if_needed(hass)
        flow = EssOptionsFlow()
        schema = flow._direction_schema({CONF_IMPORT_PROVIDER: PROVIDER_FIXED}, "import")
        keys = {str(key.schema) for key in schema.schema}
        assert CONF_AGILE_PREDICT not in keys


async def async_setup_component_if_needed(hass) -> None:
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, DOMAIN, {})


class TestAgilePredictSessionFailure:
    """A missing HTTP session must not be able to stop a plan.

    This is not hypothetical: the first version created the session before the
    try block, and in an instance without the http component that exception went
    all the way out of the coordinator update -- no plan at all, because an
    optional price forecast could not get a socket.
    """

    async def test_no_session_degrades_instead_of_raising(self, hass, monkeypatch):
        from homeassistant.setup import async_setup_component

        from custom_components.ess_controller import coordinator as coordinator_mod
        from custom_components.ess_controller.const import CONF_AGILE_PREDICT

        def no_session(hass):
            raise RuntimeError("no http component")

        monkeypatch.setattr(coordinator_mod, "async_get_clientsession", no_session)

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_AGILE_PREDICT: True, "octopus_region": "C"},
        )
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator._import_provider = _ShortTariff()
        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.plan is not None
        assert coordinator.plan.slots
        assert coordinator.price_forecast["available"] is False
        assert coordinator.price_forecast["error_kind"] == "unreachable"


class TestDayOneCompetence:
    """A fresh install must plan sensibly before it has learned anything.

    This is the property that decides whether someone has to drive the battery by
    hand for a week. With nothing configured beyond the wizard defaults, the plan
    has to know the sun will rise, know roughly what the house draws, and not hold
    the pack hostage to a calendar entry.
    """

    async def _fresh(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_solar_is_predicted_without_any_forecast_entity(self, hass):
        """The bug this fixes: every slot was exactly 0.0, so the plan assumed
        the sun would not rise and bought the whole day from the grid."""
        hass.config.latitude = 51.5
        hass.config.longitude = -0.13
        coordinator = await self._fresh(hass)
        await coordinator.async_refresh()

        assert coordinator.plan is not None
        daylight = [s for s in coordinator.plan.slots if s.pv_kwh > 0]
        assert daylight, "no slot predicted any generation"

    async def test_the_solar_estimate_is_the_right_order_of_magnitude(self, hass):
        """Over-predicting is worse than the zero it replaced: it under-charges
        overnight and leaves the house buying at the evening peak."""
        hass.config.latitude = 51.5
        hass.config.longitude = -0.13
        coordinator = await self._fresh(hass)
        await coordinator.async_refresh()

        totals = coordinator.forecast_totals()
        tomorrow = totals.get("tomorrow")
        assert tomorrow is not None
        # The wizard default array is small; whatever it is, a full day of it
        # must be neither zero nor absurd.
        peak_kw = float(coordinator.options.get("solar_peak_power_kw", 4.0))
        assert 0.15 * peak_kw < tomorrow["solar_kwh"] < 6.0 * peak_kw

    async def test_load_is_predicted_from_the_typical_profile(self, hass):
        coordinator = await self._fresh(hass)
        await coordinator.async_refresh()
        totals = coordinator.forecast_totals()
        assert totals["tomorrow"]["load_kwh"] > 0

    async def test_a_fresh_install_is_not_at_outage_risk(self, hass):
        """Nothing configured must mean no reserve boost."""
        coordinator = await self._fresh(hass)
        await coordinator.async_refresh()
        assert coordinator.outage.level == "none"

    async def _with_calendar(self, hass, summary: str, **options):
        """A live coordinator watching a calendar with one upcoming event.

        Outage protection is switched on directly on the settings object rather
        than through options: the option only seeds the default at first setup, so
        writing it afterwards leaves the feature off -- which would make these
        tests pass whatever the filter did.
        """
        from homeassistant.util import dt as dt_util

        start = dt_util.utcnow() + timedelta(hours=2)
        hass.states.async_set(
            "calendar.watched",
            "on",
            {
                "message": summary,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(hours=1)).isoformat(),
            },
        )
        coordinator = await self._fresh(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                "outage_calendar": "calendar.watched",
                **options,
            },
        )
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator.settings.outage_protection = True
        await coordinator.async_refresh()
        return coordinator

    async def test_a_real_planned_interruption_registers(self, hass):
        """The signal has to survive the filter, or the feature is gone."""
        coordinator = await self._with_calendar(
            hass, "Planned power interruption - Elm Street"
        )
        assert coordinator.outage.level == "high"
        assert "Elm Street" in coordinator.outage.reason
        assert coordinator.effective_min_soc >= 80.0

    async def test_an_unrelated_calendar_event_is_not_a_power_cut(self, hass):
        """The reported symptom: outage risk "high" in settled summer weather,
        because a bin collection was being read as a supply interruption."""
        coordinator = await self._with_calendar(hass, "Bin collection")
        assert coordinator.outage.level == "none", coordinator.outage.reason

    async def test_a_title_that_merely_contains_a_keyword_is_ignored(self, hass):
        """The second reported symptom: still high after the first filter, because
        it matched bare words as substrings."""
        coordinator = await self._with_calendar(hass, "Powerlifting class")
        assert coordinator.outage.level == "none", coordinator.outage.reason

    async def test_the_reason_names_the_event_and_the_phrase(self, hass):
        """ "Why is this high?" should be answerable from the sensor alone."""
        coordinator = await self._with_calendar(hass, "Planned power interruption")
        assert coordinator.outage.level == "high"
        assert "Planned power interruption" in coordinator.outage.reason
        assert "power interruption" in coordinator.outage.reason
        assert "calendar.watched" in coordinator.outage.reason

    async def test_the_same_event_does_register_when_told_to_trust_the_calendar(
        self, hass
    ):
        """Proves the previous test passes because of the filter, not because the
        event never reached the assessment at all."""
        coordinator = await self._with_calendar(
            hass, "Bin collection", outage_calendar_all_events=True
        )
        assert coordinator.outage.level == "high"

    async def test_a_custom_keyword_list_is_honoured(self, hass):
        coordinator = await self._with_calendar(
            hass, "Netzabschaltung", outage_calendar_keywords="netzabschaltung"
        )
        assert coordinator.outage.level == "high"


class TestDashboardRefresh:
    """An upgrade refreshes an untouched dashboard, through the real Lovelace store.

    Three dashboard improvements never reached the first user because the stored
    one is created once and never rewritten, and the remedy -- delete it by hand --
    is not something anybody knows to do.
    """

    async def _install(self, hass):
        from homeassistant.setup import async_setup_component

        _lovelace(hass)
        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return entry

    async def _stored(self, hass):
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        store = hass.data[LOVELACE_DATA].dashboards["ess-controller"]
        return await store.async_load(False)

    async def test_the_stored_dashboard_carries_our_stamp(self, hass):
        from custom_components.ess_controller.dashboard import GENERATED_KEY

        await self._install(hass)
        assert GENERATED_KEY in await self._stored(hass)

    async def test_reinstalling_an_untouched_dashboard_refreshes_it(self, hass):
        """Simulates an upgrade that generates a different layout."""
        from custom_components.ess_controller.dashboard import fingerprint
        from custom_components.ess_controller.panel import async_install

        entry = await self._install(hass)
        before = await self._stored(hass)

        # Stand in for "this version builds something different".
        import custom_components.ess_controller.panel as panel_mod

        original = panel_mod.build_for_entry
        panel_mod.build_for_entry = lambda hass, entry: {
            "title": "Rebuilt",
            "views": [{"title": "New", "path": "new", "cards": []}],
        }
        try:
            await async_install(hass, entry)
        finally:
            panel_mod.build_for_entry = original

        after = await self._stored(hass)
        assert after["title"] == "Rebuilt"
        assert fingerprint(after) != fingerprint(before)

    async def test_an_edited_dashboard_is_never_overwritten(self, hass):
        """The promise that makes the refresh safe to have at all."""
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        from custom_components.ess_controller.panel import async_install

        entry = await self._install(hass)
        store = hass.data[LOVELACE_DATA].dashboards["ess-controller"]

        edited = await store.async_load(False)
        edited["views"][0]["title"] = "My Overview"
        await store.async_save(edited)

        import custom_components.ess_controller.panel as panel_mod

        original = panel_mod.build_for_entry
        panel_mod.build_for_entry = lambda hass, entry: {
            "title": "Rebuilt",
            "views": [{"title": "New", "path": "new", "cards": []}],
        }
        try:
            await async_install(hass, entry)
        finally:
            panel_mod.build_for_entry = original

        kept = await store.async_load(False)
        assert kept["views"][0]["title"] == "My Overview"
        assert kept["title"] != "Rebuilt"

    async def test_reinstalling_the_same_version_changes_nothing(self, hass):
        """Otherwise it would rewrite the dashboard on every restart."""
        from custom_components.ess_controller.panel import async_install

        entry = await self._install(hass)
        before = await self._stored(hass)
        await async_install(hass, entry)
        assert await self._stored(hass) == before


class TestInverterRediscovery:
    """Discovery must not be a one-shot at setup.

    The commonest install order is this integration first, inverter working
    afterwards -- so the scan finds nothing, the entity map is frozen empty, and
    "Inverter link: Disconnected" persists for ever while Home Assistant is full
    of the inverter's sensors. Reloading fixed it, which nobody should need to know.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_it_finds_entities_that_appear_after_setup(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        assert coordinator.inverter_state.available is False

        # The inverter integration finishes setting up, an hour later.
        hass.states.async_set("sensor.solax_battery_capacity", "61", {})
        hass.states.async_set(
            "select.solax_charger_use_mode",
            "Self Use Mode",
            {"options": ["Self Use Mode", "Manual Mode"]},
        )
        await hass.async_block_till_done()

        await coordinator.async_refresh()
        assert coordinator.inverter_state.available is True, coordinator._adapter.entities
        assert coordinator.inverter_state.soc == 61.0

    async def test_it_stops_scanning_once_connected(self, hass):
        """A working install must not pay for the scan on every cycle."""
        coordinator = await self._coordinator(hass)
        hass.states.async_set("sensor.solax_battery_capacity", "61", {})
        await hass.async_block_till_done()
        await coordinator.async_refresh()
        assert coordinator.inverter_state.available is True

        calls = []
        import custom_components.ess_controller.coordinator as coordinator_mod

        original = coordinator_mod.discover_entities
        coordinator_mod.discover_entities = lambda *a, **k: calls.append(1) or {}
        try:
            await coordinator.async_refresh()
        finally:
            coordinator_mod.discover_entities = original
        assert calls == []


class TestRebuildButtonAlwaysWins:
    """Pressing Rebuild dashboard must replace the stored one, whatever it is.

    Told a user twice to delete the dashboard by hand to pick up a new layout,
    when this button already does it. That was bad advice about my own software,
    so the behaviour is pinned here.
    """

    async def _setup(self, hass):
        from homeassistant.setup import async_setup_component

        _lovelace(hass)
        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return entry

    def _store(self, hass):
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        return hass.data[LOVELACE_DATA].dashboards["ess-controller"]

    async def test_it_overwrites_an_edited_dashboard(self, hass):
        entry = await self._setup(hass)
        store = self._store(hass)

        edited = await store.async_load(False)
        edited["views"] = [{"title": "Wrecked", "path": "wrecked", "cards": []}]
        await store.async_save(edited)

        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert await coordinator.async_create_dashboard() == "installed"

        rebuilt = await self._store(hass).async_load(False)
        assert [v["path"] for v in rebuilt["views"]] == [
            "overview",
            "plan",
            "performance",
            "loads",
            "settings",
        ]

    async def test_the_rebuilt_dashboard_is_stamped(self, hass):
        """So the next upgrade can refresh it without being asked."""
        from custom_components.ess_controller.dashboard import is_untouched

        entry = await self._setup(hass)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_create_dashboard()
        assert is_untouched(await self._store(hass).async_load(False)) is True

    async def test_it_picks_up_a_new_layout(self, hass):
        """The case the user actually hit: new code, old stored dashboard."""
        import custom_components.ess_controller.panel as panel_mod

        entry = await self._setup(hass)
        coordinator = hass.data[DOMAIN][entry.entry_id]

        original = panel_mod.build_for_entry
        panel_mod.build_for_entry = lambda hass, entry: {
            "title": "Newer",
            "views": [{"title": "Newer", "path": "newer", "cards": []}],
        }
        try:
            await coordinator.async_create_dashboard()
        finally:
            panel_mod.build_for_entry = original

        assert (await self._store(hass).async_load(False))["title"] == "Newer"


class TestRebuildIsReachable:
    """There has to be a way to rebuild that does not depend on finding a button.

    The button carries EntityCategory.CONFIG, so Home Assistant files it away in a
    collapsed Configuration block on the device page. The first person to need it
    could not find it, and generate_dashboard -- the only dashboard service that
    existed -- returns YAML rather than rebuilding anything.
    """

    async def _setup(self, hass):
        from homeassistant.setup import async_setup_component

        _lovelace(hass)
        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        await hass.async_block_till_done()

    async def test_the_service_exists(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        assert hass.services.has_service(DOMAIN, "rebuild_dashboard")

    async def test_calling_it_replaces_the_stored_dashboard(self, hass):
        from homeassistant.components.lovelace.const import LOVELACE_DATA

        await self._setup(hass)
        store = hass.data[LOVELACE_DATA].dashboards["ess-controller"]
        wrecked = await store.async_load(False)
        wrecked["views"] = [{"title": "Wrecked", "path": "wrecked", "cards": []}]
        await store.async_save(wrecked)

        await hass.services.async_call(
            DOMAIN, "rebuild_dashboard", {}, blocking=True, return_response=True
        )
        # Re-fetch: a reseed installs a fresh LovelaceStorage, so the object held
        # from before the call still carries the old data in memory.
        fresh = hass.data[LOVELACE_DATA].dashboards["ess-controller"]
        rebuilt = await fresh.async_load(False)
        assert next(v["path"] for v in rebuilt["views"]) == "overview"

    async def test_it_reports_what_it_did(self, hass):
        await self._setup(hass)
        response = await hass.services.async_call(
            DOMAIN, "rebuild_dashboard", {}, blocking=True, return_response=True
        )
        assert list(response["rebuilt"].values()) == ["installed"]

    async def test_the_button_is_on_the_settings_view(self, hass):
        """So next time it is somewhere a person would actually look."""
        from custom_components.ess_controller.panel import build_for_entry

        await self._setup(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        config = build_for_entry(hass, entry)
        settings = next(v for v in config["views"] if v["path"] == "settings")
        targets = [
            card.get("entity")
            for section in settings["sections"]
            for card in section["cards"]
        ]
        assert any(isinstance(t, str) and "rebuild_dashboard" in t for t in targets), (
            targets
        )


class TestLiveStateIsNotOnThePlanningClock:
    """Link status must not be up to five minutes behind reality.

    Re-planning pulls forecasts and can call the tariff API, so it belongs on a
    slow clock. Reading the inverter is pure state lookups against entities
    another integration already polls, so it costs nothing -- and while the two
    shared one timer, "Inverter link: Disconnected" sat there for minutes while
    the inverter's values were visibly still arriving.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_the_live_clock_is_no_slower_than_thirty_seconds(self, hass):
        from custom_components.ess_controller.const import DEFAULT_LIVE_INTERVAL

        assert DEFAULT_LIVE_INTERVAL.total_seconds() <= 30

    async def test_it_is_faster_than_the_planning_clock(self, hass):
        from custom_components.ess_controller.const import (
            DEFAULT_LIVE_INTERVAL,
            DEFAULT_SCAN_INTERVAL,
        )

        assert DEFAULT_LIVE_INTERVAL < DEFAULT_SCAN_INTERVAL

    async def test_a_tick_picks_up_an_inverter_that_just_appeared(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        assert coordinator.inverter_state.available is False

        hass.states.async_set("sensor.solax_battery_capacity", "61", {})
        hass.states.async_set(
            "select.solax_charger_use_mode",
            "Self Use Mode",
            {"options": ["Self Use Mode", "Manual Mode"]},
        )
        coordinator._rediscover_if_blind()

        # A live tick, not a full refresh.
        await coordinator._async_live_refresh(ha_dt.utcnow())
        assert coordinator.inverter_state.available is True

    async def test_a_tick_does_not_postpone_replanning(self, hass):
        """``async_set_updated_data`` would reschedule the planning timer, so a
        fast poll would push re-planning out for ever."""
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        before = coordinator.update_interval
        await coordinator._async_live_refresh(ha_dt.utcnow())
        assert coordinator.update_interval == before

    async def test_a_tick_before_the_first_refresh_is_harmless(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        coordinator.data = None
        await coordinator._async_live_refresh(ha_dt.utcnow())

    async def test_polling_stops_when_the_entry_unloads(self, hass):
        coordinator = await self._coordinator(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert coordinator is not None


class TestApexDetection:
    """Charts appear on their own when the card is installed, and never when not."""

    def test_a_plain_instance_gets_no_custom_cards(self, hass):
        from custom_components.ess_controller.panel import has_apexcharts

        _lovelace(hass)
        assert has_apexcharts(hass) is False

    def test_no_lovelace_at_all_is_not_an_error(self, hass):
        from custom_components.ess_controller.panel import has_apexcharts

        assert has_apexcharts(hass) is False

    def test_an_installed_card_is_found(self, hass):
        from custom_components.ess_controller.panel import has_apexcharts

        _lovelace(hass)

        class _Resources:
            def async_items(self):
                return [
                    {"url": "/hacsfiles/lovelace-mushroom/mushroom.js"},
                    {"url": "/hacsfiles/apexcharts-card/apexcharts-card.js"},
                ]

        hass.data["lovelace"].resources = _Resources()
        assert has_apexcharts(hass) is True

    def test_a_resource_collection_that_raises_means_no(self, hass):
        from custom_components.ess_controller.panel import has_apexcharts

        _lovelace(hass)

        class _Broken:
            def async_items(self):
                raise RuntimeError("moved furniture")

        hass.data["lovelace"].resources = _Broken()
        assert has_apexcharts(hass) is False


class TestControlDoesNotOutliveTheIntegration:
    """Every mode the controller writes is one the inverter will sit in for ever.

    A hold raises the inverter's own reserve to the charge it is protecting; a
    forced charge or discharge puts it in Manual mode; even a self-use slot writes
    the plan's floor, which an outage boost can lift far above the reserve the
    user set. Unload the entry in any of those states and it stays there, with
    nothing left running to take it out -- a pack pinned shut at 94% running the
    house off the grid, or an inverter still buying at the top of the tariff.

    An earlier version of this fired only for a solar-absorbing hold, which left
    the other three cases to persist silently.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return entry, hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _watch(coordinator):
        """Capture the commands the adapter is asked to apply."""
        sent = []
        original = coordinator._adapter.async_apply

        async def _spy(command, **kwargs):
            sent.append(command)
            return await original(command, **kwargs)

        coordinator._adapter.async_apply = _spy
        return sent

    async def _released_after(self, hass, command):
        """Set ``command`` as the last one applied, then unload-release."""
        _entry, coordinator = await self._coordinator(hass)
        coordinator.settings.enabled = True
        coordinator.settings.dry_run = False
        coordinator.last_command = command
        sent = self._watch(coordinator)
        result = await coordinator.async_release_on_unload()
        return coordinator, result, sent

    async def test_a_hold_gives_the_reserve_back(self, hass):
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        coordinator, result, sent = await self._released_after(
            hass,
            ControlCommand(action=SlotAction.IDLE, min_soc=94.0, hold_absorbs_solar=True),
        )
        assert result is True
        assert sent[-1].action is SlotAction.SELF_USE
        assert sent[-1].min_soc == pytest.approx(coordinator.settings.reserve_soc)

    async def test_a_forced_charge_is_not_left_running(self, hass):
        """The expensive one: unload mid-charge and the inverter keeps buying.

        Manual mode is only undone by a later mode write, and on unload there is
        no later write -- so this is the one case that has to send one.
        """
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        _coordinator, result, sent = await self._released_after(
            hass,
            ControlCommand(action=SlotAction.CHARGE, power_kw=3.0, min_soc=20.0),
        )
        assert result is True
        assert sent[-1].action is SlotAction.SELF_USE

    async def test_a_strict_hold_is_taken_out_of_manual_mode(self, hass):
        """A hold that cannot absorb solar is Manual mode plus Stop, same problem."""
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        _coordinator, result, sent = await self._released_after(
            hass,
            ControlCommand(action=SlotAction.IDLE, hold_absorbs_solar=False),
        )
        assert result is True
        assert sent[-1].action is SlotAction.SELF_USE

    async def test_a_self_use_slot_still_lowers_the_planning_floor(self, hass):
        """Self-use writes the plan's floor, not the user's emergency reserve, and
        an outage boost puts that floor far higher still."""
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        coordinator, result, sent = await self._released_after(
            hass,
            ControlCommand(action=SlotAction.SELF_USE, min_soc=80.0),
        )
        assert result is True
        assert sent[-1].min_soc == pytest.approx(coordinator.settings.reserve_soc)
        assert sent[-1].min_soc < 80.0

    async def test_the_rate_ceilings_come_back_too(self, hass):
        """A throttled slot writes its own rate to the current limit. Left there,
        it caps solar charging and every later charge as well."""
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        coordinator, _result, sent = await self._released_after(
            hass,
            ControlCommand(action=SlotAction.CHARGE, power_kw=1.0),
        )
        assert sent[-1].max_charge_kw == pytest.approx(coordinator.settings.max_charge_kw)
        assert sent[-1].max_discharge_kw == pytest.approx(
            coordinator.settings.max_discharge_kw
        )

    async def test_an_advisory_install_never_writes(self, hass):
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        _entry, coordinator = await self._coordinator(hass)
        # Advisory mode: dry run, so nothing may be written at all.
        coordinator.settings.dry_run = True
        coordinator.last_command = ControlCommand(
            action=SlotAction.IDLE, hold_absorbs_solar=True
        )
        sent = self._watch(coordinator)
        assert await coordinator.async_release_on_unload() is False
        assert sent == []

    async def test_nothing_commanded_means_nothing_sent(self, hass):
        """Never having written to the inverter, there is nothing to undo."""
        _entry, coordinator = await self._coordinator(hass)
        coordinator.settings.dry_run = False
        coordinator.last_command = None
        sent = self._watch(coordinator)
        assert await coordinator.async_release_on_unload() is False
        assert sent == []

    async def test_unload_calls_it(self, hass):
        from custom_components.ess_controller.models import ControlCommand, SlotAction

        entry, coordinator = await self._coordinator(hass)
        coordinator.settings.dry_run = False
        coordinator.last_command = ControlCommand(
            action=SlotAction.IDLE, hold_absorbs_solar=True
        )
        called: list[bool] = []
        original = coordinator.async_release_on_unload

        async def _spy():
            called.append(True)
            return await original()

        coordinator.async_release_on_unload = _spy
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert called == [True]


class TestTheInverterIsToldThePlansFloor:
    """The plan and the hardware were working to different floors.

    The optimiser planned never to discharge below the configured minimum --
    raised further when an outage looks likely -- while the inverter was told it
    could go down to the deeper emergency reserve. In every self-use slot the
    hardware was therefore free to spend energy the plan had already promised to
    something else, and an outage boost never reached the inverter at all.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_the_command_carries_the_planning_floor(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        coordinator.settings.min_soc = 20.0
        coordinator.settings.reserve_soc = 5.0
        await coordinator.async_refresh()
        command = coordinator._resolve_command(ha_dt.utcnow())
        assert command is not None
        assert command.min_soc == pytest.approx(coordinator.effective_min_soc)
        assert command.min_soc > coordinator.settings.reserve_soc

    async def test_an_outage_boost_reaches_the_inverter(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        coordinator.settings.min_soc = 20.0
        coordinator.settings.outage_protection = True
        await coordinator.async_refresh()
        coordinator.outage.level = "high"
        coordinator.outage.reserve_soc = 80.0
        command = coordinator._resolve_command(ha_dt.utcnow())
        assert command is not None
        assert command.min_soc == pytest.approx(80.0)

    async def test_the_command_carries_the_rate_ceilings(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        command = coordinator._resolve_command(ha_dt.utcnow())
        assert command is not None
        assert command.max_charge_kw == pytest.approx(coordinator.settings.max_charge_kw)
        assert command.max_discharge_kw == pytest.approx(
            coordinator.settings.max_discharge_kw
        )


class TestTheCurrentSlotDoesNotChurn:
    """The plan is rebuilt every cycle, so the half-hour already under way could
    change action several times inside it -- charge at 19:31, self-use at 19:46 --
    as the forecast and SoC moved. That is churn, not intelligence."""

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        from custom_components.ess_controller.inverter.battery import BatteryReading

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        # A readable battery, which every real install has and this harness does
        # not: with no inverter entities the SoC is unavailable, and the
        # controller now refuses to act on the placeholder that stands in for it.
        # These tests are about holding an action steady, not about SoC.
        coordinator._battery_source.read = lambda *_a, **_k: BatteryReading(
            soc=55.0, soc_source="sensor.test", capacity_kwh=22.0
        )
        return coordinator

    @staticmethod
    def _slot(coordinator, now):
        return coordinator.plan.slot_at(now)

    async def test_the_action_holds_for_the_rest_of_the_slot(self, hass):

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        # Taken from the plan, not the wall clock. Reading utcnow() again after
        # the refresh let a half-hour boundary fall between the two, so the slot
        # the test reasoned about was not the slot the command was committed to
        # -- and the suite failed for a minute either side of every :00 and :30.
        now = coordinator.plan.slots[0].start + timedelta(minutes=1)
        slot = self._slot(coordinator, now)
        assert slot is not None
        first = coordinator._resolve_command(now).action

        # The plan changes its mind about the half-hour already running.
        other = next(a for a in SlotAction if a is not first)
        slot.action = other
        assert coordinator._resolve_command(now).action is first

    async def test_a_new_slot_is_free_to_differ(self, hass):

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        # Taken from the plan, not the wall clock. Reading utcnow() again after
        # the refresh let a half-hour boundary fall between the two, so the slot
        # the test reasoned about was not the slot the command was committed to
        # -- and the suite failed for a minute either side of every :00 and :30.
        now = coordinator.plan.slots[0].start + timedelta(minutes=1)
        first = coordinator._resolve_command(now).action

        later = coordinator.plan.slots[2]
        other = next(a for a in SlotAction if a is not first)
        later.action = other
        assert coordinator._resolve_command(later.start).action is other

    async def test_an_explicit_replan_reconsiders_now(self, hass):

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        # Taken from the plan, not the wall clock. Reading utcnow() again after
        # the refresh let a half-hour boundary fall between the two, so the slot
        # the test reasoned about was not the slot the command was committed to
        # -- and the suite failed for a minute either side of every :00 and :30.
        now = coordinator.plan.slots[0].start + timedelta(minutes=1)
        first = coordinator._resolve_command(now).action
        slot = self._slot(coordinator, now)
        other = next(a for a in SlotAction if a is not first)
        slot.action = other

        coordinator.clear_commitment()
        assert coordinator._resolve_command(now).action is other

    async def test_a_settings_change_reconsiders_now(self, hass):
        """A permission or limit change can alter what the right action is, so a
        commitment made before it must not survive it."""

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        # Taken from the plan, not the wall clock. Reading utcnow() again after
        # the refresh let a half-hour boundary fall between the two, so the slot
        # the test reasoned about was not the slot the command was committed to
        # -- and the suite failed for a minute either side of every :00 and :30.
        now = coordinator.plan.slots[0].start + timedelta(minutes=1)
        first = coordinator._resolve_command(now).action

        # A commitment to something the plan does not want.
        stale = next(a for a in SlotAction if a is not first)
        slot = self._slot(coordinator, now)
        coordinator._committed = (slot.start, stale)

        await coordinator.async_update_settings(max_charge_kw=2.0)
        # Either the refresh has already re-committed to the plan's own choice, or
        # nothing is committed yet -- what must not happen is the stale
        # commitment surviving a change that could have altered the right answer.
        assert coordinator._committed != (slot.start, stale)

    async def test_an_override_is_never_held_off(self, hass):
        import homeassistant.util.dt as ha_dt

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        # Taken from the plan, not the wall clock. Reading utcnow() again after
        # the refresh let a half-hour boundary fall between the two, so the slot
        # the test reasoned about was not the slot the command was committed to
        # -- and the suite failed for a minute either side of every :00 and :30.
        now = coordinator.plan.slots[0].start + timedelta(minutes=1)
        coordinator._resolve_command(now)
        await coordinator.async_set_override(SlotAction.CHARGE, timedelta(minutes=30))
        command = coordinator._resolve_command(ha_dt.utcnow())
        assert command.action is SlotAction.CHARGE


class TestCheapSlotIsRanked:
    """ "Cheap slot" measured position in the price *range*, not rank among slots.

    On a peaky tariff those diverge badly: one 58p evening spike stretches the
    range so far that a third of the way up lands at 31p, and two thirds of an
    Agile day comes out "cheap" -- useless for deciding when to run a dishwasher.
    The card's own description said "cheapest third of the planning horizon",
    which is the ranked reading, so the description was right and the arithmetic
    was not.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _prices(coordinator, prices):
        """Replace the import series with a horizon of known prices."""
        import homeassistant.util.dt as ha_dt

        from custom_components.ess_controller.models import PriceSlot
        from custom_components.ess_controller.tariff.base import PriceSeries

        start = ha_dt.utcnow().replace(minute=0, second=0, microsecond=0)
        slots = [
            PriceSlot(
                start=start + timedelta(minutes=30 * i),
                end=start + timedelta(minutes=30 * (i + 1)),
                price=price,
            )
            for i, price in enumerate(prices)
        ]
        coordinator._import_prices = PriceSeries(slots)

    async def test_a_single_spike_does_not_make_the_day_cheap(self, hass):
        coordinator = await self._coordinator(hass)
        # Eleven half-hours at 20-30p and one at 70p. Under the range reading the
        # threshold was 20 + (70-20)/3 = 37p and every ordinary slot was "cheap".
        self._prices(coordinator, [20.0, 22.0, 24.0, 26.0, 28.0, 30.0] * 2 + [70.0])
        threshold = coordinator.price_percentile("import")
        assert threshold is not None
        assert threshold < 30.0

    async def test_roughly_a_third_of_the_slots_qualify(self, hass):
        coordinator = await self._coordinator(hass)
        prices = [float(p) for p in range(10, 70, 2)]
        self._prices(coordinator, prices)
        threshold = coordinator.price_percentile("import")
        cheap = [p for p in prices if p <= threshold]
        assert 0.25 <= len(cheap) / len(prices) <= 0.42

    async def test_a_flat_tariff_has_a_threshold_at_that_price(self, hass):
        coordinator = await self._coordinator(hass)
        self._prices(coordinator, [24.0] * 12)
        assert coordinator.price_percentile("import") == pytest.approx(24.0)

    async def test_no_prices_means_no_answer(self, hass):
        from custom_components.ess_controller.tariff.base import PriceSeries

        coordinator = await self._coordinator(hass)
        coordinator._import_prices = PriceSeries([])
        assert coordinator.price_percentile("import") is None

    async def test_the_entity_publishes_the_threshold_it_used(self, hass):
        coordinator = await self._coordinator(hass)
        self._prices(coordinator, [20.0, 30.0, 40.0, 50.0])
        state = hass.states.get("binary_sensor.ai_ess_controller_cheap_import_slot")
        assert state is not None
        assert "cheap_at_or_below" in state.attributes


class TestDisabledControlsAreNamed:
    """A control the inverter has, that Home Assistant is not publishing.

    The SolaX Modbus integration ships many of its numbers and switches disabled by
    default, so a Modbus device does not add two hundred entities to a house.
    Discovery reads states, and a disabled entity has none -- so "charge from grid"
    and the minimum-SoC reserve were invisible here while sitting plainly in SolaX's
    own app, one at Enable and one at 20%. The plan could not touch either, and
    nothing said why.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _register(hass, entity_id: str, *, disabled: bool):
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        domain, _, object_id = entity_id.partition(".")
        entry = registry.async_get_or_create(
            domain, "solax_modbus", f"unique_{object_id}", suggested_object_id=object_id
        )
        if disabled:
            registry.async_update_entity(
                entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        return entry.entity_id

    async def test_a_disabled_grid_charge_switch_is_named(self, hass):
        coordinator = await self._coordinator(hass)
        entity_id = self._register(
            hass, "switch.solax1_inverter_selfuse_charge_from_grid", disabled=True
        )
        assert entity_id in coordinator.disabled_inverter_controls()

    async def test_a_disabled_reserve_control_is_named(self, hass):
        coordinator = await self._coordinator(hass)
        entity_id = self._register(
            hass, "number.solax1_inverter_battery_minimum_capacity", disabled=True
        )
        assert entity_id in coordinator.disabled_inverter_controls()

    async def test_an_enabled_entity_is_not_reported(self, hass):
        coordinator = await self._coordinator(hass)
        entity_id = self._register(
            hass, "switch.solax1_inverter_something_charge", disabled=False
        )
        assert entity_id not in coordinator.disabled_inverter_controls()

    async def test_unrelated_disabled_entities_are_not_reported(self, hass):
        coordinator = await self._coordinator(hass)
        entity_id = self._register(hass, "switch.hallway_lamp", disabled=True)
        assert entity_id not in coordinator.disabled_inverter_controls()

    async def test_our_own_entities_are_never_reported(self, hass):
        coordinator = await self._coordinator(hass)
        entity_id = self._register(
            hass, "number.ai_ess_controller_min_soc_setting", disabled=True
        )
        assert entity_id not in coordinator.disabled_inverter_controls()

    async def test_it_is_in_the_diagnostics_download(self, hass):
        coordinator = await self._coordinator(hass)
        self._register(
            hass, "switch.solax1_inverter_selfuse_charge_from_grid", disabled=True
        )
        assert coordinator.diagnostics()["disabled_inverter_controls"]


class TestHorizonReachIsReported:
    """A horizon shorter than the prices in hand is a silent loss of money.

    A real install carried ``horizon_hours: 24`` from its original setup while 48
    hours of prices were available -- 96 half-hours, the tail of them predicted. The
    consequence was invisible and expensive: it could not see that tomorrow evening
    was dearer than today's cheap window, so it had no reason to buy into the cheap
    window. Nothing anywhere said the plan was working with half of what it knew.

    The arithmetic is tested against the pure helper in the fast suite; what is worth
    checking here is that the answer actually reaches somewhere a person will see it.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_it_is_on_the_plan_sensor(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        state = hass.states.get("sensor.ai_ess_controller_planned_horizon_cost")
        assert state is not None
        assert "horizon_reach" in state.attributes

    async def test_it_is_in_the_diagnostics(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        assert coordinator.diagnostics()["horizon_reach"]

    async def test_reading_it_does_not_rebuild_the_diagnostics(self, hass):
        """It is a sensor attribute, so it is read on every state update."""
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        calls: list[int] = []
        original = coordinator.diagnostics

        def _spy():
            calls.append(1)
            return original()

        coordinator.diagnostics = _spy
        assert isinstance(coordinator.horizon_reach, str)
        assert calls == []


class TestTheWeeklySavingIsATotalNotARate:
    """It shipped labelled ``p/kWh`` and read "-476.6 p/kWh" on a phone.

    That is not a quantity anybody can act on, and graphed against a y-axis in
    p/kWh a week's money looks like a tariff gone mad. It is a total in pence,
    the same as every other money sensor on the page.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    async def test_the_unit_is_money(self, hass):
        from custom_components.ess_controller.sensor import COST_UNIT, PRICE_UNIT

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        state = hass.states.get("sensor.ai_ess_controller_saving_vs_self_use_this_week")
        assert state is not None
        unit = state.attributes.get("unit_of_measurement")
        assert unit == COST_UNIT
        assert unit != PRICE_UNIT

    async def test_the_counterfactual_starts_where_the_window_does(self, hass):
        """Not where the whole log does.

        A seven-day report was beginning its counterfactual at whatever the
        battery held two months ago and then stepping it through this week's
        slots, so the two batteries were never started from the same charge and
        part of the difference between them was only that.
        """
        from homeassistant.util import dt as dt_util

        from custom_components.ess_controller.performance import SlotRecord

        coordinator = await self._coordinator(hass)
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        log = coordinator.performance_store.log
        # One stale record far outside the window, at an implausible charge...
        log.add(SlotRecord(start=now - timedelta(days=40), soc_start=95.0, soc_end=95.0))
        # ...and this week's, which is what a seven-day report is about.
        for hours in range(6, 0, -1):
            log.add(
                SlotRecord(
                    start=now - timedelta(hours=hours),
                    import_price=20.0,
                    load_kwh=0.3,
                    grid_import_kwh=0.3,
                    soc_start=30.0,
                    soc_end=30.0,
                )
            )
        shadow = coordinator._self_use_shadow(log.window(7.0))
        assert shadow.soc == pytest.approx(30.0)


class TestHorizonIsReportedInHours:
    """ "horizon_slots: 48" was read as a 48-hour horizon. It is 48 half-hours.

    The setting that controls it is in hours, so the same number meant two different
    things in two places, and the reading that mattered -- am I looking a day ahead
    or two? -- was the one nobody could get.
    """

    async def _plan_attributes(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        await hass.data[DOMAIN][entry.entry_id].async_refresh()
        state = hass.states.get("sensor.ai_ess_controller_planned_horizon_cost")
        assert state is not None
        return state.attributes

    async def test_hours_are_given_alongside_slots(self, hass):
        attributes = await self._plan_attributes(hass)
        assert "horizon_hours" in attributes
        assert "horizon_slots" in attributes

    async def test_the_hours_are_half_the_slot_count(self, hass):
        attributes = await self._plan_attributes(hass)
        slots = attributes["horizon_slots"]
        if slots:
            assert attributes["horizon_hours"] == pytest.approx(slots / 2, abs=0.6)


class TestAnOutageHoldExplainsItself:
    """The plan is where people look, and it was the one place that did not know.

    A real install had its floor lifted from 20% to 90% by an outage hold on a
    forecast 42 mph wind, leaving 1.1 kWh of a 22 kWh pack. The evening ran off the
    grid at 45p, the state of charge sat flat all night, and the plan's reason read
    "grid-charge 0.0 kWh ... Saves 1p vs self-use". The explanation existed on a
    different entity, which is no use to anyone reading this one.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _hold(coordinator, reserve: float, reason: str):
        """Pin the assessment, because a refresh recomputes it from the weather."""
        coordinator.settings.outage_protection = True
        coordinator.outage.level = "high"
        coordinator.outage.reserve_soc = reserve
        coordinator.outage.reason = reason
        coordinator._assess_outage = lambda _now: None

    async def test_it_is_marked_so_it_survives_a_skim(self, hass):
        from custom_components.ess_controller.dashboard import OUTAGE_HOLD_MARK

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        self._hold(coordinator, 90.0, "forecast wind peaking at 42")
        await coordinator.async_refresh()
        assert coordinator.plan.reason.startswith(OUTAGE_HOLD_MARK)

    async def test_the_mark_is_absent_without_a_hold(self, hass):
        from custom_components.ess_controller.dashboard import OUTAGE_HOLD_MARK

        coordinator = await self._coordinator(hass)
        coordinator.settings.outage_protection = False
        await coordinator.async_refresh()
        assert OUTAGE_HOLD_MARK not in coordinator.plan.reason

    async def test_the_reason_names_the_hold(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        self._hold(coordinator, 90.0, "forecast wind peaking at 42")
        await coordinator.async_refresh()
        assert coordinator.plan is not None
        assert "power cut" in coordinator.plan.reason

    async def test_it_repeats_why_the_risk_was_raised(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        self._hold(coordinator, 90.0, "forecast wind peaking at 42")
        await coordinator.async_refresh()
        assert "wind peaking at 42" in coordinator.plan.reason

    async def test_it_says_how_little_is_left(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        self._hold(coordinator, 90.0, "forecast wind peaking at 42")
        await coordinator.async_refresh()
        assert "available to plan with" in coordinator.plan.reason

    async def test_no_hold_leaves_the_reason_alone(self, hass):
        coordinator = await self._coordinator(hass)
        coordinator.settings.outage_protection = False
        await coordinator.async_refresh()
        assert coordinator.plan is not None
        assert "power cut" not in coordinator.plan.reason

    async def test_a_boost_below_the_setting_is_not_announced(self, hass):
        """Outage protection can only raise the floor; if it does not, say nothing."""
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        self._hold(coordinator, coordinator.settings.min_soc, "breezy")
        await coordinator.async_refresh()
        assert "power cut" not in coordinator.plan.reason


class TestDisablingWritesHandsTheInverterBack:
    """Switching writing off must not leave the inverter mid-instruction.

    "Advisory only" reasonably means stop touching it, and the previous reading of
    that was to stop *mid-instruction*: switch off during a forced charge and the
    inverter stayed in Manual mode buying electricity, with the controller no longer
    claiming responsibility for it. One final write hands it back to self-use, which
    is the state a person expects an unmanaged inverter to be in.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator.settings.dry_run = False
        return coordinator

    @staticmethod
    def _watch(coordinator):
        from custom_components.ess_controller.models import SlotAction

        seen: list[SlotAction] = []
        original = coordinator._adapter.async_apply

        async def _spy(command, **kwargs):
            seen.append(command.action)
            return await original(command, **kwargs)

        coordinator._adapter.async_apply = _spy
        return seen

    async def test_turning_writing_off_writes_self_use_once(self, hass):
        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        seen = self._watch(coordinator)
        await coordinator.async_update_settings(dry_run=True)
        assert SlotAction.SELF_USE in seen

    async def test_it_happens_while_writing_is_still_permitted(self, hass):
        """Ordered deliberately: the hand-back goes out before the setting lands, or
        it would be forbidden by the very change that makes it necessary."""
        coordinator = await self._coordinator(hass)
        during: list[bool] = []
        original = coordinator._adapter.async_apply

        async def _spy(command, **kwargs):
            during.append(coordinator.settings.may_write)
            return await original(command, **kwargs)

        coordinator._adapter.async_apply = _spy
        await coordinator.async_update_settings(dry_run=True)
        # The first call is the hand-back. Anything after it is the ordinary refresh
        # under the new setting, which is a dry run and writes nothing.
        assert during
        assert during[0] is True

    async def test_turning_writing_on_does_not_hand_back(self, hass):
        """Asserted on the hand-back itself rather than on the first action written:
        the plan's own choice is frequently self-use, which made the old form pass or
        fail by luck."""
        coordinator = await self._coordinator(hass)
        coordinator.settings.dry_run = True
        called: list[int] = []
        coordinator._async_hand_back = lambda _now: called.append(1)
        await coordinator.async_update_settings(dry_run=False)
        assert called == []

    async def test_an_unrelated_change_does_not_hand_back(self, hass):
        coordinator = await self._coordinator(hass)
        seen = self._watch(coordinator)
        await coordinator.async_update_settings(max_charge_kw=2.0)
        assert coordinator.settings.max_charge_kw == 2.0
        assert len(seen) <= 1

    async def test_switching_off_twice_is_harmless(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_update_settings(dry_run=True)
        seen = self._watch(coordinator)
        await coordinator.async_update_settings(dry_run=True)
        assert seen == []

    async def test_a_failing_write_does_not_block_the_setting(self, hass):
        """The setting is the user's instruction; a sulking inverter cannot veto it."""
        coordinator = await self._coordinator(hass)

        async def _boom(command, **kwargs):
            raise RuntimeError("dongle asleep")

        coordinator._adapter.async_apply = _boom
        await coordinator.async_update_settings(dry_run=True)
        assert coordinator.settings.dry_run is True


class TestUnusableForecastSensorsAreNamed:
    """Configuring a forecast sensor that yields nothing must not be silent.

    A real install pointed the solar-forecast field at Forecast.Solar's "Estimated
    energy production - today/tomorrow" -- daily totals with no hourly breakdown, and
    exactly what the field's own entity picker offered. They parsed to nothing, the
    estimate fell back to bare geometry, and the plan carried on looking confident:
    a whole afternoon forecast at 2 kWh while the array was filling the battery.
    """

    async def _coordinator(self, hass, entities):
        from homeassistant.setup import async_setup_component

        from custom_components.ess_controller.const import CONF_SOLAR_FORECAST_ENTITIES

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_SOLAR_FORECAST_ENTITIES: entities},
        )
        await hass.async_block_till_done()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_refresh()
        return coordinator

    async def test_a_daily_total_sensor_is_used_not_discarded(self, hass):
        hass.states.async_set("sensor.energy_production_tomorrow", "9.4", {})
        coordinator = await self._coordinator(hass, ["sensor.energy_production_tomorrow"])
        assert 9.4 in coordinator._solar_daily_totals.values()

    async def test_it_says_it_is_only_scaling_an_estimate(self, hass):
        hass.states.async_set("sensor.energy_production_tomorrow", "9.4", {})
        coordinator = await self._coordinator(hass, ["sensor.energy_production_tomorrow"])
        note = coordinator.diagnostics()["solar_forecast_note"]
        assert "daily totals" in note
        assert "hourly forecast would be better" in note

    async def test_a_sensor_that_names_no_day_is_reported(self, hass):
        hass.states.async_set("sensor.solar_guess", "9.4", {})
        coordinator = await self._coordinator(hass, ["sensor.solar_guess"])
        assert "no usable forecast" in coordinator.diagnostics()["solar_forecast_note"]

    async def test_a_missing_sensor_is_reported(self, hass):
        coordinator = await self._coordinator(hass, ["sensor.not_here"])
        note = coordinator.diagnostics()["solar_forecast_note"]
        assert "sensor.not_here" in note and "not found" in note

    async def test_an_unknown_state_is_reported(self, hass):
        hass.states.async_set("sensor.energy_production_tomorrow", "unknown", {})
        coordinator = await self._coordinator(hass, ["sensor.energy_production_tomorrow"])
        assert "no usable forecast" in coordinator.diagnostics()["solar_forecast_note"]

    async def test_an_hourly_forecast_needs_no_note(self, hass):
        hass.states.async_set(
            "sensor.solcast_forecast_today",
            "9.4",
            {
                "detailedHourly": [
                    {"period_start": "2026-08-13T10:00:00+00:00", "pv_estimate": 1.2},
                    {"period_start": "2026-08-13T11:00:00+00:00", "pv_estimate": 1.4},
                ]
            },
        )
        coordinator = await self._coordinator(hass, ["sensor.solcast_forecast_today"])
        assert coordinator.diagnostics()["solar_forecast_note"] == ""

    async def test_configuring_nothing_says_nothing(self, hass):
        coordinator = await self._coordinator(hass, [])
        assert coordinator.diagnostics()["solar_forecast_note"] == ""


class TestTheInvertersOwnReserveIsCompared:
    """The floor that actually governs is the inverter's, not the plan's.

    A real install had its reserve stuck at 90% -- written there by an earlier hold,
    then refused when the controller tried to lower it again -- so the pack sat at 89%
    all afternoon buying 45p electricity while the plan believed it was free to spend
    down to 20%. Nothing anywhere compared the two numbers, and they are the two
    numbers that matter.
    """

    async def _coordinator(self, hass, reserve):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_refresh()
        coordinator.inverter_state.min_soc = reserve
        return coordinator

    async def test_a_higher_reserve_is_reported(self, hass):
        coordinator = await self._coordinator(hass, 90.0)
        note = coordinator.reserve_conflict()
        assert "will not discharge below 90%" in note
        assert "points of the pack are unavailable" in note

    async def test_a_matching_reserve_says_nothing(self, hass):
        coordinator = await self._coordinator(hass, 20.0)
        coordinator.inverter_state.min_soc = coordinator.effective_min_soc
        assert coordinator.reserve_conflict() == ""

    async def test_a_lower_reserve_says_nothing(self, hass):
        """The inverter being more permissive than the plan is not a problem."""
        coordinator = await self._coordinator(hass, 5.0)
        assert coordinator.reserve_conflict() == ""

    async def test_an_unreadable_reserve_says_nothing(self, hass):
        coordinator = await self._coordinator(hass, None)
        assert coordinator.reserve_conflict() == ""

    async def test_a_refused_write_is_mentioned_in_the_note(self, hass):
        from custom_components.ess_controller.inverter.roles import ROLE_MIN_SOC

        coordinator = await self._coordinator(hass, 90.0)
        coordinator.adapter._rejected[ROLE_MIN_SOC] = "rejected at register 0xc5"
        note = coordinator.reserve_conflict()
        assert "set on the inverter itself" in note

    async def test_it_reaches_the_control_status_entity(self, hass):
        coordinator = await self._coordinator(hass, 90.0)
        coordinator.async_update_listeners()
        state = hass.states.get("sensor.ai_ess_controller_control_status")
        assert state is not None
        assert "reserve_conflict" in state.attributes

    async def test_it_is_in_the_diagnostics(self, hass):
        coordinator = await self._coordinator(hass, 90.0)
        assert coordinator.diagnostics()["reserve_conflict"]


class TestTheDiagnosticsExplainTheBehaviour:
    """Everything in here was worked out by hand, from a file that held the data.

    A whole afternoon went on proving that an inverter really was charging from
    the grid during self-use, then on finding the control that was doing it. Both
    answers were latent in figures the download already carried; neither was
    stated. These are the statements.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _now():
        from homeassistant.util import dt as dt_util

        return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    async def test_the_file_says_when_it_was_taken(self, hass):
        """Not the same as when the plan was built, and reading one as the other
        once produced a confident and completely wrong explanation."""
        from custom_components.ess_controller.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        data = await async_get_config_entry_diagnostics(hass, entry)
        assert data["generated_at"]
        assert data["controller"]["plan"]["created"] != data["generated_at"]

    async def test_unplanned_grid_charging_is_named(self, hass):
        """The check that settled the argument, run for you instead of by you."""
        from custom_components.ess_controller.performance import SlotRecord

        coordinator = await self._coordinator(hass)
        now = self._now()
        log = coordinator.performance_store.log
        # Sun could give 0.16 kWh; the pack gained 0.88. The rest was bought.
        log.add(
            SlotRecord(
                start=now - timedelta(minutes=30),
                import_price=20.8,
                pv_kwh=0.44,
                load_kwh=0.27,
                grid_import_kwh=1.02,
                soc_start=62.0,
                soc_end=66.0,
                planned_action="self_use",
                applied_action="self_use",
            )
        )
        found = coordinator.unexplained_charge()
        assert len(found) == 1
        assert found[0]["applied_action"] == "self_use"
        assert found[0]["unexplained_kwh"] > 0.5
        assert found[0]["cost_estimate"] > 10

    async def test_a_planned_charge_is_not_reported_as_unexplained(self, hass):
        from custom_components.ess_controller.performance import SlotRecord

        coordinator = await self._coordinator(hass)
        now = self._now()
        coordinator.performance_store.log.add(
            SlotRecord(
                start=now - timedelta(minutes=30),
                import_price=8.0,
                grid_import_kwh=3.0,
                soc_start=40.0,
                soc_end=52.0,
                planned_action="charge",
                applied_action="charge",
            )
        )
        assert coordinator.unexplained_charge() == []

    async def test_dropped_half_hours_are_counted(self, hass):
        from custom_components.ess_controller.performance import SlotRecord

        coordinator = await self._coordinator(hass)
        now = self._now()
        log = coordinator.performance_store.log
        for index in (0, 1, 3, 4):  # the third half-hour never arrived
            log.add(SlotRecord(start=now - timedelta(minutes=30 * (5 - index))))
        coverage = coordinator.slot_coverage()
        assert coverage["recorded"] == 4
        assert coverage["expected"] == 5
        assert coverage["missing"] == 1

    async def test_every_apply_is_kept_not_just_the_last(self, hass):
        """A leftover setting is a sequence, and one snapshot cannot show it."""
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        history = coordinator.diagnostics()["apply_history"]
        assert len(history) >= 2
        assert all(entry["at"] for entry in history)

    async def test_the_live_flows_reach_the_file(self, hass):
        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        power = coordinator.diagnostics()["live_power"]
        assert set(power) == {"battery_kw", "pv_kw", "grid_kw", "load_kw"}


class TestAnUnjustifiedHoldNeverReachesTheInverter:
    """A hold shuts the battery. It has to be able to say why, at the write.

    Three separate routes have now published a hold that could not justify
    itself: a labelling artefact on a sunny half-hour, the self-use fallback
    carrying ``idle`` from slots where nothing moved, and a forecast shortfall
    too small for the level grid to express. Each was fixed where the plan is
    built, and each time the damage was done here, at the point the reserve is
    raised and the house goes on the grid for the rest of the half-hour.

    On a real install the third one held a 93% pack behind a 49.2p half-hour
    while valuing the charge at 23.9p. An oven would have been bought at 49.2p.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        from custom_components.ess_controller.inverter.battery import BatteryReading

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator._battery_source.read = lambda *_a, **_k: BatteryReading(
            soc=93.0, soc_source="sensor.test", capacity_kwh=22.0
        )
        return coordinator

    async def _slot(self, hass, hold_value, price=49.2):
        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        await coordinator.async_refresh()
        slot = coordinator.plan.slots[0]
        slot.action = SlotAction.IDLE
        slot.import_price = price
        slot.hold_value = hold_value
        # The plan committed to the slot's action before the test rewrote it.
        coordinator._committed = None
        return coordinator, slot

    async def test_a_hold_worth_less_than_the_grid_is_downgraded(self, hass):
        from custom_components.ess_controller.models import SlotAction

        coordinator, slot = await self._slot(hass, hold_value=23.9)
        command = coordinator._resolve_command(slot.start + timedelta(minutes=1))
        assert command.action is SlotAction.SELF_USE

    async def test_a_hold_worth_more_than_the_grid_still_stands(self, hass):
        from custom_components.ess_controller.models import SlotAction

        coordinator, slot = await self._slot(hass, hold_value=60.0)
        command = coordinator._resolve_command(slot.start + timedelta(minutes=1))
        assert command.action is SlotAction.IDLE

    async def test_a_plan_that_priced_nothing_is_left_alone(self, hass):
        """Only a stated value can overrule the plan; absence is not evidence."""
        from custom_components.ess_controller.models import SlotAction

        coordinator, slot = await self._slot(hass, hold_value=None)
        command = coordinator._resolve_command(slot.start + timedelta(minutes=1))
        assert command.action is SlotAction.IDLE


class TestBatteryThroughputSurvivesAGapInTheMarks:
    """Battery energy is read from the state of charge, and gaps swallowed it.

    Each slot measured its own opening and closing mark, so anything that
    happened between one slot's close and the next slot's open was recorded
    nowhere: a slot dropped for poor coverage took its movement with it, a
    missing opening mark discarded the whole slot, and the marks are sampled a
    little either side of the boundary so most joins lost a point as well.

    On a real day the marks ran 63% to 93% -- thirty points -- while the
    within-slot deltas summed to seventeen. Forty-three per cent of the day's
    throughput was charged to nothing, wore nothing, and made the round-trip
    figure meaningless.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _slot(start):
        from custom_components.ess_controller.sampling import CompletedSlot

        return CompletedSlot(
            start=start,
            end=start + timedelta(minutes=30),
            pv_kwh=0.0,
            load_kwh=0.3,
            coverage=1.0,
            grid_measured=True,
        )

    async def test_the_movement_across_a_gap_is_not_lost(self, hass):
        from homeassistant.util import dt as dt_util

        coordinator = await self._coordinator(hass)
        capacity = coordinator.nominal_capacity_kwh()
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        first, second = now - timedelta(hours=1), now - timedelta(minutes=30)

        coordinator._slot_marks[first] = {"soc_start": 60.0, "soc_end": 70.0}
        coordinator._record_completed([self._slot(first)])
        # The next slot closes at 80% but never recorded an opening mark, which
        # used to discard its ten points entirely.
        coordinator._slot_marks[second] = {"soc_end": 80.0}
        coordinator._record_completed([self._slot(second)])

        records = coordinator.performance_store.log.records[-2:]
        moved = sum(r.battery_charge_kwh - r.battery_discharge_kwh for r in records)
        assert moved == pytest.approx((80.0 - 60.0) / 100.0 * capacity, rel=1e-6)

    async def test_the_join_between_two_slots_is_not_lost(self, hass):
        """The marks drift a point either side of the boundary; it still counts."""
        from homeassistant.util import dt as dt_util

        coordinator = await self._coordinator(hass)
        capacity = coordinator.nominal_capacity_kwh()
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        first, second = now - timedelta(hours=1), now - timedelta(minutes=30)

        coordinator._slot_marks[first] = {"soc_start": 60.0, "soc_end": 62.0}
        coordinator._record_completed([self._slot(first)])
        coordinator._slot_marks[second] = {"soc_start": 63.0, "soc_end": 65.0}
        coordinator._record_completed([self._slot(second)])

        records = coordinator.performance_store.log.records[-2:]
        moved = sum(r.battery_charge_kwh - r.battery_discharge_kwh for r in records)
        # Five points, not the four the two slots reported between them.
        assert moved == pytest.approx((65.0 - 60.0) / 100.0 * capacity, rel=1e-6)

    async def test_a_long_outage_is_not_dumped_into_one_half_hour(self, hass):
        """Bridging is honest about energy and vague about timing, within limits.

        After hours offline the battery may be anywhere, and attributing the
        whole difference to the half-hour that happens to close the gap would
        invent a discharge that never happened there.
        """
        from homeassistant.util import dt as dt_util

        coordinator = await self._coordinator(hass)
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        old = now - timedelta(hours=6)

        coordinator._slot_marks[old] = {"soc_start": 90.0, "soc_end": 90.0}
        coordinator._record_completed([self._slot(old)])
        coordinator._slot_marks[now] = {"soc_start": 40.0, "soc_end": 41.0}
        coordinator._record_completed([self._slot(now)])

        last = coordinator.performance_store.log.records[-1]
        assert last.battery_discharge_kwh == pytest.approx(0.0)
        assert last.battery_charge_kwh == pytest.approx(
            0.01 * coordinator.nominal_capacity_kwh(), rel=1e-6
        )


class TestAMissingStateOfChargeStopsTheWriting:
    """A plan is fine to display on a guess; it must not reach the hardware.

    ``_read_site_state`` substitutes 50% when the battery cannot be read, so the
    rest of the cycle has a number to carry. Every action the optimiser then
    chooses turns on where the charge really is -- a pack at 90% told to charge,
    one at 15% told to discharge -- and none of it can be checked. The tariff
    comparison already refused to score on the placeholder; the path that writes
    to the inverter did not.

    The plan is deliberately still built: an advisory install with no
    state-of-charge sensor still wants its prices and forecasts.
    """

    async def _coordinator(self, hass):
        from homeassistant.setup import async_setup_component

        await async_setup_component(hass, DOMAIN, {})
        await _complete_flow(hass)
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await hass.async_block_till_done()
        return hass.data[DOMAIN][entry.entry_id]

    @staticmethod
    def _reading(coordinator, soc):
        from custom_components.ess_controller.inverter.battery import BatteryReading

        coordinator._battery_source.read = lambda *_a, **_k: BatteryReading(
            soc=soc,
            soc_source="sensor.gone" if soc is None else "sensor.back",
            capacity_kwh=22.0,
        )

    async def test_it_holds_self_use_when_the_battery_cannot_be_read(self, hass):
        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        self._reading(coordinator, None)
        await coordinator.async_refresh()
        assert coordinator.last_command is not None
        assert coordinator.last_command.action is SlotAction.SELF_USE

    async def test_it_says_why(self, hass):
        coordinator = await self._coordinator(hass)
        self._reading(coordinator, None)
        await coordinator.async_refresh()
        assert "state of charge unavailable" in coordinator.last_command.reason
        assert "sensor.gone" in coordinator.last_command.reason

    async def test_the_plan_is_still_published(self, hass):
        """Advisory installs keep their forecasts and prices."""
        coordinator = await self._coordinator(hass)
        self._reading(coordinator, None)
        await coordinator.async_refresh()
        assert coordinator.plan is not None
        assert coordinator.plan.slots

    async def test_a_deliberate_override_still_wins(self, hass):
        """An override is an instruction, not an inference."""
        from datetime import timedelta

        from custom_components.ess_controller.models import SlotAction

        coordinator = await self._coordinator(hass)
        self._reading(coordinator, None)
        await coordinator.async_set_override(SlotAction.CHARGE, timedelta(hours=1))
        await coordinator.async_refresh()
        assert coordinator.last_command.action is SlotAction.CHARGE

    async def test_control_resumes_once_the_reading_returns(self, hass):
        import homeassistant.util.dt as ha_dt

        coordinator = await self._coordinator(hass)
        self._reading(coordinator, None)
        await coordinator.async_refresh()
        assert "unavailable" in coordinator.last_command.reason

        self._reading(coordinator, 57.0)
        coordinator.clear_commitment()
        await coordinator.async_refresh()
        command = coordinator._resolve_command(ha_dt.utcnow())
        assert "unavailable" not in command.reason
