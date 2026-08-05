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
    """Click through the config flow accepting every default."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    steps = 0
    while result["type"] == "form":
        steps += 1
        assert steps < 20, "config flow does not terminate"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
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
