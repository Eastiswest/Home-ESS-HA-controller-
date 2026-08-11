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

    hass.data[LOVELACE_DATA] = LovelaceData(
        resource_mode="storage", dashboards={}, resources=None, yaml_dashboards={}
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

    async def test_off_by_default_and_nothing_is_recorded(self, hass):
        """Nobody acquires a third-party dependency without saying yes."""
        coordinator = await self._coordinator(hass)
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
