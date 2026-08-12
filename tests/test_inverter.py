"""Tests for the inverter adapters, driven against a stub Home Assistant."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.ess_controller.inverter.base import (
    InverterState,
    NullAdapter,
    clamp_to_number,
    current_from_power,
    pick_option,
    power_kw,
    power_limit_value,
)
from custom_components.ess_controller.inverter.battery import BatterySource
from custom_components.ess_controller.inverter.generic import GenericEntityAdapter
from custom_components.ess_controller.inverter.roles import (
    ROLE_CHARGE_LIMIT,
    ROLE_LOCK,
    ROLE_MANUAL_MODE,
    ROLE_MIN_SOC,
    ROLE_SOC,
    ROLE_USE_MODE,
    SOLAX_ROLE_SPECS,
    discover_entities,
    merge_overrides,
)
from custom_components.ess_controller.inverter.solax import SolaxModbusAdapter
from custom_components.ess_controller.models import ControlCommand, SlotAction

# ---------------------------------------------------------------------------
# Stub Home Assistant
# ---------------------------------------------------------------------------


@dataclass
class StubState:
    entity_id: str
    state: Any
    attributes: dict[str, Any] = field(default_factory=dict)


class StubStates:
    def __init__(self) -> None:
        self._states: dict[str, StubState] = {}

    def set(self, entity_id: str, state: Any, **attributes: Any) -> None:
        self._states[entity_id] = StubState(entity_id, state, attributes)

    def get(self, entity_id: str) -> StubState | None:
        return self._states.get(entity_id)

    def async_all(self) -> list[StubState]:
        return list(self._states.values())


class StubServices:
    def __init__(self, states: StubStates, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._states = states
        self._fail = fail or set()
        # When True, accept the call but do not update state, which is exactly
        # how a dropped Modbus write over a flaky WiFi dongle behaves.
        self.silently_drop: set[str] = set()

    async def async_call(
        self, domain: str, service: str, data: dict[str, Any], blocking: bool = False
    ) -> None:
        self.calls.append((domain, service, dict(data)))
        entity_id = data.get("entity_id")
        if entity_id in self._fail:
            raise RuntimeError("modbus write failed")
        if entity_id in self.silently_drop:
            return
        existing = self._states.get(entity_id)
        attributes = existing.attributes if existing else {}
        if service == "select_option":
            self._states.set(entity_id, data["option"], **attributes)
        elif service == "set_value":
            self._states.set(entity_id, data["value"], **attributes)
        elif service == "turn_on":
            self._states.set(entity_id, "on", **attributes)
        elif service == "turn_off":
            self._states.set(entity_id, "off", **attributes)


class StubHass:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.states = StubStates()
        self.services = StubServices(self.states, fail)


SOLAX_MODES = [
    "Self Use",
    "Feedin Priority",
    "Back Up Mode",
    "Manual Mode",
]
SOLAX_MANUAL = ["Stop Charge and Discharge", "Force Charge", "Force Discharge"]


def build_solax_hass(**overrides: Any) -> StubHass:
    """A stub mirroring a real SolaX Modbus X1 Hybrid G4 entity set."""
    hass = StubHass()
    hass.states.set("select.solax_charger_use_mode", "Self Use", options=SOLAX_MODES)
    hass.states.set(
        "select.solax_manual_mode_select",
        "Stop Charge and Discharge",
        options=SOLAX_MANUAL,
    )
    hass.states.set("select.solax_lock_state", "Locked", options=["Locked", "Unlocked"])
    hass.states.set(
        "number.solax_battery_charge_max_current",
        20.0,
        unit_of_measurement="A",
        min=0,
        max=30,
        step=0.1,
    )
    hass.states.set(
        "number.solax_battery_discharge_max_current",
        20.0,
        unit_of_measurement="A",
        min=0,
        max=30,
        step=0.1,
    )
    hass.states.set(
        "number.solax_battery_minimum_capacity",
        15.0,
        unit_of_measurement="%",
        min=10,
        max=99,
        step=1,
    )
    hass.states.set(
        "number.solax_export_control_user_limit",
        3680,
        unit_of_measurement="W",
        min=0,
        max=6000,
        step=10,
    )
    hass.states.set("sensor.solax_battery_capacity", 55.0, unit_of_measurement="%")
    hass.states.set("sensor.solax_battery_voltage_charge", 360.0, unit_of_measurement="V")
    hass.states.set("sensor.solax_battery_power_charge", -500, unit_of_measurement="W")
    hass.states.set("sensor.solax_house_load", 800, unit_of_measurement="W")
    hass.states.set("sensor.solax_pv_power_total", 1200, unit_of_measurement="W")
    for entity_id, value in overrides.items():
        hass.states.set(entity_id.replace("__", "."), value)
    return hass


def solax_adapter(hass: StubHass, **kwargs: Any) -> SolaxModbusAdapter:
    entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
    return SolaxModbusAdapter(hass, entities, **kwargs)


def command(action: SlotAction, **kwargs: Any) -> ControlCommand:
    defaults = dict(power_kw=3.0, min_soc=15.0, max_soc=95.0, allow_grid_charge=True)
    defaults.update(kwargs)
    return ControlCommand(action=action, **defaults)


def apply(adapter, cmd, dry_run=False, verify=False):
    return asyncio.run(adapter.async_apply(cmd, dry_run=dry_run, verify=verify))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestPickOption:
    def test_exact_match(self):
        assert pick_option(["Self Use", "Manual Mode"], ["Manual Mode"]) == "Manual Mode"

    def test_case_and_punctuation_insensitive(self):
        assert pick_option(["Feed-in Priority"], ["Feedin Priority"]) == (
            "Feed-in Priority"
        )

    def test_matches_reworded_option(self):
        # An upstream relabel from "Back Up Mode" to "Backup" must not break control.
        assert pick_option(["Backup"], ["Back Up Mode"]) == "Backup"

    def test_prefers_first_candidate(self):
        options = ["Stop Charge and Discharge", "Force Charge"]
        assert pick_option(options, ["Force Charge", "Stop"]) == "Force Charge"

    def test_locked_does_not_match_unlocked(self):
        """Regression: substring matching made "Locked" match "Unlocked", so the
        inverter was never unlocked and every setting write was silently
        rejected while the controller reported success."""
        assert pick_option(["Locked"], ["Unlocked", "Unlock"]) is None
        assert pick_option(["Locked", "Unlocked"], ["Unlocked"]) == "Unlocked"

    def test_enabled_does_not_match_disabled(self):
        assert pick_option(["Disabled"], ["Enabled"]) is None
        assert pick_option(["Off"], ["On"]) is None

    def test_no_match_returns_none(self):
        assert pick_option(["Self Use"], ["Force Charge"]) is None

    def test_empty_options(self):
        assert pick_option([], ["anything"]) is None


class TestPowerConversion:
    def test_current_from_power_uses_live_voltage(self):
        # 3.6 kW at 360 V is 10 A.
        assert current_from_power(3.6, 360.0, 200.0) == pytest.approx(10.0)

    def test_falls_back_when_voltage_missing(self):
        assert current_from_power(3.6, None, 360.0) == pytest.approx(10.0)

    def test_ignores_implausible_voltage(self):
        # A 0 V reading from a sleeping BMS must not produce infinite current.
        assert current_from_power(3.6, 0.0, 360.0) == pytest.approx(10.0)

    def test_amps_unit_converts(self):
        hass = build_solax_hass()
        value = power_limit_value(
            hass, "number.solax_battery_charge_max_current", 3.6, 360.0
        )
        assert value == pytest.approx(10.0)

    def test_watts_unit_passes_power(self):
        hass = StubHass()
        hass.states.set("number.x", 0, unit_of_measurement="W", min=0, max=6000)
        assert power_limit_value(hass, "number.x", 3.6) == pytest.approx(3600.0)

    def test_kilowatts_unit_passes_power(self):
        hass = StubHass()
        hass.states.set("number.x", 0, unit_of_measurement="kW", min=0, max=10)
        assert power_limit_value(hass, "number.x", 3.6) == pytest.approx(3.6)

    def test_unitless_inferred_from_range(self):
        hass = StubHass()
        hass.states.set("number.big", 0, min=0, max=6000)
        assert power_limit_value(hass, "number.big", 3.6) == pytest.approx(3600.0)
        hass.states.set("number.small", 0, min=0, max=10)
        assert power_limit_value(hass, "number.small", 3.6) == pytest.approx(3.6)

    def test_power_sensor_units_normalised(self):
        hass = StubHass()
        hass.states.set("sensor.w", 2500, unit_of_measurement="W")
        hass.states.set("sensor.kw", 2.5, unit_of_measurement="kW")
        assert power_kw(hass, "sensor.w") == pytest.approx(2.5)
        assert power_kw(hass, "sensor.kw") == pytest.approx(2.5)

    def test_clamp_respects_number_bounds_and_step(self):
        hass = build_solax_hass()
        entity = "number.solax_battery_charge_max_current"
        # Above max.
        assert clamp_to_number(hass, entity, 99.0) == pytest.approx(30.0)
        # Below min.
        assert clamp_to_number(hass, entity, -5.0) == pytest.approx(0.0)
        # Quantised to the 0.1 step.
        assert clamp_to_number(hass, entity, 10.04) == pytest.approx(10.0)


class TestDiscovery:
    def test_finds_solax_roles(self):
        hass = build_solax_hass()
        found = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        assert found[ROLE_USE_MODE] == "select.solax_charger_use_mode"
        assert found[ROLE_MANUAL_MODE] == "select.solax_manual_mode_select"
        assert found[ROLE_CHARGE_LIMIT] == "number.solax_battery_charge_max_current"
        assert found[ROLE_SOC] == "sensor.solax_battery_capacity"
        assert found[ROLE_LOCK] == "select.solax_lock_state"

    def test_prefers_most_specific_suffix(self):
        # battery_voltage_charge must win over a bare battery_voltage.
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_voltage", 350.0)
        found = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        assert found["battery_voltage"] == "sensor.solax_battery_voltage_charge"

    def test_prefix_disambiguates_two_inverters(self):
        hass = build_solax_hass()
        hass.states.set("select.shed_charger_use_mode", "Self Use", options=SOLAX_MODES)
        found = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="shed")
        assert found[ROLE_USE_MODE] == "select.shed_charger_use_mode"

    def test_missing_roles_simply_absent(self):
        hass = StubHass()
        found = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        assert found == {}

    def test_overrides_replace_and_disable(self):
        discovered = {ROLE_USE_MODE: "select.a", ROLE_MIN_SOC: "number.b"}
        merged = merge_overrides(
            discovered, {ROLE_USE_MODE: "select.custom", ROLE_MIN_SOC: ""}
        )
        assert merged[ROLE_USE_MODE] == "select.custom"
        # An explicit blank means "do not touch this".
        assert ROLE_MIN_SOC not in merged

    def test_unknown_role_ignored(self):
        merged = merge_overrides({}, {"not_a_role": "select.x"})
        assert merged == {}


class TestCapabilities:
    def test_full_solax_capabilities(self):
        adapter = solax_adapter(build_solax_hass())
        caps = adapter.capabilities
        assert caps.force_charge
        assert caps.force_discharge
        assert caps.idle
        assert caps.self_use
        assert caps.charge_power_limit
        assert caps.min_soc

    def test_missing_manual_mode_drops_forcing(self):
        hass = build_solax_hass()
        # An inverter without a manual sub-mode cannot be forced.
        hass.states.set("select.solax_manual_mode_select", "unavailable", options=[])
        adapter = solax_adapter(hass)
        caps = adapter.capabilities
        assert not caps.force_charge
        assert not caps.idle
        assert caps.self_use


class TestSolaxCharge:
    def test_force_charge_sets_mode_and_current(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        result = apply(adapter, command(SlotAction.CHARGE, power_kw=3.6))
        assert result.ok

        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_manual_mode_select"]["option"] == "Force Charge"
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Manual Mode"
        # 3.6 kW at 360 V is 10 A.
        assert by_entity["number.solax_battery_charge_max_current"]["value"] == (
            pytest.approx(10.0)
        )

    def test_unlocks_before_writing(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert entities[0] == "select.solax_lock_state"
        assert hass.services.calls[0][2]["option"] == "Unlocked"

    def test_does_not_relock_when_already_unlocked(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "select.solax_lock_state" not in entities

    def test_manual_submode_written_before_mode(self):
        """Order matters: entering Manual Mode while the sub-mode still says
        Force Discharge would briefly discharge the battery."""
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_manual_mode_select", "Force Discharge", options=SOLAX_MANUAL
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert entities.index("select.solax_manual_mode_select") < entities.index(
            "select.solax_charger_use_mode"
        )

    def test_charge_current_clamped_to_inverter_limit(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        # 20 kW at 360 V would be 55 A, above the 30 A entity maximum.
        apply(adapter, command(SlotAction.CHARGE, power_kw=20.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_battery_charge_max_current"]["value"] == (
            pytest.approx(30.0)
        )

    def test_uses_live_voltage_for_current(self):
        hass = build_solax_hass()
        # A nearly empty EV pack sits well below nominal.
        hass.states.set(
            "sensor.solax_battery_voltage_charge", 300.0, unit_of_measurement="V"
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE, power_kw=3.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        # 3 kW at 300 V is 10 A, not the 8.3 A a 360 V assumption would give.
        assert by_entity["number.solax_battery_charge_max_current"]["value"] == (
            pytest.approx(10.0)
        )


class TestSolaxOtherActions:
    def test_force_discharge(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.DISCHARGE, power_kw=2.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_manual_mode_select"]["option"] == (
            "Force Discharge"
        )
        # 2 kW at 360 V is 5.56 A, quantised to the entity's 0.1 A step.
        assert by_entity["number.solax_battery_discharge_max_current"]["value"] == (
            pytest.approx(5.6)
        )

    def test_strict_hold_stops_charge_and_discharge(self):
        """Where export pays, a hold really does stop the battery both ways."""
        hass = build_solax_hass()
        hass.states.set("select.solax_charger_use_mode", "Self Use", options=SOLAX_MODES)
        hass.states.set(
            "select.solax_manual_mode_select", "Force Charge", options=SOLAX_MANUAL
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE, hold_absorbs_solar=False))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Manual Mode"
        assert by_entity["select.solax_manual_mode_select"]["option"] == (
            "Stop Charge and Discharge"
        )

    def test_hold_keeps_self_use_so_solar_still_charges(self):
        """With nothing earned for exporting, a hold must not shut the array out.

        Manual mode with charge and discharge stopped holds the battery against
        the array as well as against the house, so a sunnier half-hour than
        forecast is handed to the grid for nothing. Self-use with the reserve
        raised to the current SoC holds it against discharge only.
        """
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        hass.states.set("sensor.solax_battery_capacity", 62.4)
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Self Use"
        assert "select.solax_manual_mode_select" not in by_entity
        # Pinned at the SoC it is holding, not at the configured reserve.
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == 62.0

    def test_hold_never_leaves_grid_charging_enabled(self):
        """Self-use plus grid charging would buy energy to fill the pack."""
        hass = build_solax_hass()
        hass.states.set("switch.solax_selfuse_night_charge_enable", "on")
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE, allow_grid_charge=True))
        calls = [
            c
            for c in hass.services.calls
            if c[2]["entity_id"] == "switch.solax_selfuse_night_charge_enable"
        ]
        assert [c[1] for c in calls] == ["turn_off"]

    def test_hold_without_a_soc_reading_writes_no_reserve_at_all(self):
        """With no SoC to pin to, the hold is Manual mode -- and in Manual mode the
        reserve is not consulted, so writing it is pointless. A real inverter
        rejected the register outright while in a forced mode."""
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_capacity", "unknown")
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE, min_soc=15.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_minimum_capacity" not in entities

    def test_idle_does_not_set_current_limits(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_charge_max_current" not in entities

    def test_self_use_returns_to_self_use_mode(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.SELF_USE))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Self Use"

    def test_solar_only_charge_uses_self_use(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE_SOLAR_ONLY))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Self Use"

    def test_unsupported_action_falls_back_to_self_use(self):
        hass = build_solax_hass()
        hass.states.set("select.solax_manual_mode_select", "n/a", options=[])
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        adapter = solax_adapter(hass)
        result = apply(adapter, command(SlotAction.CHARGE))
        assert any("not supported" in reason for reason in result.skipped)
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Self Use"


class TestMinSocAndExport:
    def test_min_soc_written(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=25.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == (
            pytest.approx(25.0)
        )

    def test_min_soc_clamped_to_entity_minimum(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        # The entity floor is 10%, so a 5% request must be trimmed not rejected.
        apply(adapter, command(SlotAction.SELF_USE, min_soc=5.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == (
            pytest.approx(10.0)
        )

    def test_min_soc_not_managed_when_disabled(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass, manage_min_soc=False)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=30.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_minimum_capacity" not in entities

    def test_export_limit_only_when_enabled(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass, manage_export_limit=False)
        apply(adapter, command(SlotAction.SELF_USE, export_limit_kw=2.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_export_control_user_limit" not in entities

        hass2 = build_solax_hass()
        adapter2 = solax_adapter(hass2, manage_export_limit=True)
        apply(adapter2, command(SlotAction.SELF_USE, export_limit_kw=2.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass2.services.calls}
        assert by_entity["number.solax_export_control_user_limit"]["value"] == (
            pytest.approx(2000.0)
        )


class TestGridChargeToggle:
    def test_switch_turned_off_for_solar_only(self):
        hass = build_solax_hass()
        hass.states.set("switch.solax_selfuse_night_charge_enable", "on")
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE_SOLAR_ONLY))
        calls = [
            c
            for c in hass.services.calls
            if c[2]["entity_id"] == "switch.solax_selfuse_night_charge_enable"
        ]
        assert calls and calls[0][1] == "turn_off"

    def test_switch_left_alone_during_forced_charge(self):
        # A forced charge draws from the grid regardless of the toggle.
        hass = build_solax_hass()
        hass.states.set("switch.solax_selfuse_night_charge_enable", "off")
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "switch.solax_selfuse_night_charge_enable" not in entities

    def test_no_write_when_switch_already_correct(self):
        hass = build_solax_hass()
        hass.states.set("switch.solax_selfuse_night_charge_enable", "on")
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.SELF_USE, allow_grid_charge=True))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "switch.solax_selfuse_night_charge_enable" not in entities


class TestIdempotence:
    def test_no_writes_when_already_in_target_state(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE, power_kw=3.6))
        first = len(hass.services.calls)
        assert first > 0

        # Applying the identical command again must be a no-op: hammering a
        # flaky WiFi dongle every cycle is how these links fall over.
        result = apply(adapter, command(SlotAction.CHARGE, power_kw=3.6))
        assert len(hass.services.calls) == first
        assert not result.changed

    def test_small_current_drift_does_not_rewrite(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE, power_kw=3.6))
        before = len(hass.services.calls)
        # A 1% power change is not worth a Modbus write.
        apply(adapter, command(SlotAction.CHARGE, power_kw=3.63))
        assert len(hass.services.calls) == before

    def test_action_change_does_write(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE))
        before = len(hass.services.calls)
        result = apply(adapter, command(SlotAction.DISCHARGE))
        assert len(hass.services.calls) > before
        assert result.changed


class TestDryRun:
    def test_dry_run_writes_nothing(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        result = apply(adapter, command(SlotAction.CHARGE), dry_run=True)
        assert hass.services.calls == []
        assert result.dry_run
        assert result.writes
        assert "would set" in result.summary()

    def test_dry_run_shows_the_same_writes_real_control_would_make(self):
        dry_hass = build_solax_hass()
        dry = apply(solax_adapter(dry_hass), command(SlotAction.CHARGE), dry_run=True)

        live_hass = build_solax_hass()
        apply(solax_adapter(live_hass), command(SlotAction.CHARGE))

        planned = {(w.entity_id, str(w.value)) for w in dry.writes}
        actual = {
            (c[2]["entity_id"], str(c[2].get("option", c[2].get("value"))))
            for c in live_hass.services.calls
        }
        assert planned == actual


class TestFailureHandling:
    def test_write_error_reported_and_retried_next_cycle(self):
        hass = build_solax_hass(**{})
        hass.services._fail = {"select.solax_charger_use_mode"}
        adapter = solax_adapter(hass)
        result = apply(adapter, command(SlotAction.CHARGE))
        assert not result.ok
        assert result.errors
        # A failed apply must not be remembered, or the next cycle would skip it.
        assert adapter.last_applied is None

        hass.services._fail = set()
        retry = apply(adapter, command(SlotAction.CHARGE))
        assert retry.ok

    def test_silently_dropped_write_detected_by_readback(self):
        """The Pocket WiFi failure mode: the write is accepted but ignored."""
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        hass.services.silently_drop = {"select.solax_charger_use_mode"}
        adapter = solax_adapter(hass)

        import custom_components.ess_controller.inverter.base as base_mod

        original = base_mod.VERIFY_DELAY_SECONDS
        base_mod.VERIFY_DELAY_SECONDS = 0
        try:
            result = apply(adapter, command(SlotAction.CHARGE), verify=True)
        finally:
            base_mod.VERIFY_DELAY_SECONDS = original

        assert not result.verified
        assert any("charger_use_mode" in item for item in result.unverified)

    def test_missing_use_mode_entity_is_reported(self):
        hass = StubHass()
        adapter = SolaxModbusAdapter(hass, {})
        result = apply(adapter, command(SlotAction.CHARGE))
        assert result.writes == []
        assert any("working-mode" in reason for reason in result.skipped)


class TestNullAdapter:
    def test_writes_nothing_and_says_why(self):
        adapter = NullAdapter(StubHass())
        result = apply(adapter, command(SlotAction.CHARGE))
        assert result.writes == []
        assert result.skipped
        assert not adapter.capabilities.force_charge


class TestGenericAdapter:
    def test_custom_option_wording(self):
        hass = StubHass()
        hass.states.set(
            "select.myinv_work_mode",
            "General",
            options=["General", "Battery Priority", "Manual"],
        )
        hass.states.set(
            "select.myinv_manual_mode",
            "Idle",
            options=["Idle", "Charging", "Discharging"],
        )
        hass.states.set(
            "number.myinv_charge_power", 0, unit_of_measurement="W", min=0, max=5000
        )
        adapter = GenericEntityAdapter(
            hass,
            {
                ROLE_USE_MODE: "select.myinv_work_mode",
                ROLE_MANUAL_MODE: "select.myinv_manual_mode",
                ROLE_CHARGE_LIMIT: "number.myinv_charge_power",
            },
            options={
                "self_use": "General",
                "manual": "Manual",
                "force_charge": "Charging",
                "force_discharge": "Discharging",
                "stop": "Idle",
            },
        )
        assert adapter.capabilities.force_charge
        apply(adapter, command(SlotAction.CHARGE, power_kw=2.5))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.myinv_manual_mode"]["option"] == "Charging"
        assert by_entity["select.myinv_work_mode"]["option"] == "Manual"
        assert by_entity["number.myinv_charge_power"]["value"] == pytest.approx(2500.0)

    def test_comma_separated_options_accepted(self):
        hass = StubHass()
        hass.states.set("select.x_mode", "A", options=["A", "B"])
        adapter = GenericEntityAdapter(
            hass, {ROLE_USE_MODE: "select.x_mode"}, options={"self_use": "B, A"}
        )
        assert adapter.capabilities.self_use


class TestBatterySource:
    def test_prefers_configured_bms_soc(self):
        hass = StubHass()
        hass.states.set("sensor.be_soc", 62.5, unit_of_measurement="%")
        source = BatterySource(hass, 22.0, soc_entity="sensor.be_soc")
        reading = source.read(InverterState(soc=55.0))
        assert reading.soc == pytest.approx(62.5)
        assert reading.soc_source == "sensor.be_soc"

    def test_falls_back_to_inverter_soc(self):
        source = BatterySource(StubHass(), 22.0)
        reading = source.read(InverterState(soc=48.0))
        assert reading.soc == pytest.approx(48.0)
        assert reading.soc_source == "inverter"

    def test_no_soc_at_all_is_invalid(self):
        reading = BatterySource(StubHass(), 22.0).read(InverterState())
        assert not reading.valid

    def test_implausible_soc_rejected(self):
        hass = StubHass()
        hass.states.set("sensor.be_soc", 6500)
        source = BatterySource(hass, 22.0, soc_entity="sensor.be_soc")
        reading = source.read(None)
        assert not reading.valid
        assert reading.soc_source == "invalid"

    def test_measured_capacity_overrides_nameplate(self):
        """A used EV pack must be planned against its real capacity."""
        hass = StubHass()
        hass.states.set("sensor.be_capacity", 17.4, unit_of_measurement="kWh")
        source = BatterySource(hass, 22.0, capacity_entity="sensor.be_capacity")
        reading = source.read(None)
        assert reading.capacity_kwh == pytest.approx(17.4)
        assert reading.capacity_source == "sensor.be_capacity"

    def test_capacity_in_watt_hours_converted(self):
        hass = StubHass()
        hass.states.set("sensor.be_capacity", 17400, unit_of_measurement="Wh")
        source = BatterySource(hass, 22.0, capacity_entity="sensor.be_capacity")
        assert source.read(None).capacity_kwh == pytest.approx(17.4)

    def test_soh_derating(self):
        hass = StubHass()
        hass.states.set("sensor.be_soh", 79.0, unit_of_measurement="%")
        source = BatterySource(
            hass, 22.0, health_entity="sensor.be_soh", derate_capacity_by_soh=True
        )
        reading = source.read(None)
        assert reading.capacity_kwh == pytest.approx(22.0 * 0.79)
        assert reading.capacity_source == "nameplate x SoH"

    def test_soh_ignored_when_derating_disabled(self):
        hass = StubHass()
        hass.states.set("sensor.be_soh", 79.0)
        source = BatterySource(hass, 22.0, health_entity="sensor.be_soh")
        assert source.read(None).capacity_kwh == pytest.approx(22.0)

    def test_voltage_falls_back_to_inverter(self):
        source = BatterySource(StubHass(), 22.0)
        reading = source.read(InverterState(soc=50.0, battery_voltage=352.0))
        assert reading.voltage == pytest.approx(352.0)


class TestReadState:
    def test_reads_solax_measurements(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        state = asyncio.run(adapter.async_read_state())
        assert state.available
        assert state.soc == pytest.approx(55.0)
        assert state.mode == "Self Use"
        assert state.locked is True
        assert state.battery_voltage == pytest.approx(360.0)
        # Watt sensors normalised to kW.
        assert state.pv_power_kw == pytest.approx(1.2)
        assert state.load_power_kw == pytest.approx(0.8)
        assert state.battery_power_kw == pytest.approx(-0.5)

    def test_unavailable_inverter_reported(self):
        hass = StubHass()
        adapter = SolaxModbusAdapter(hass, {})
        state = asyncio.run(adapter.async_read_state())
        assert not state.available


class TestDiscoveryIgnoresOurOwnEntities:
    """Our own sensors are named like the inverter's, and must never be bound.

    "AI ESS Controller" publishes sensor.ai_ess_controller_usable_battery_capacity,
    whose object ID ends in a suffix the state-of-charge role matches. Binding it
    would have the controller read its own 18 kWh capacity as an 18% state of
    charge, and report the inverter as connected while it was not. A real-HA test
    caught it; nothing else would have.
    """

    def _hass(self, *entity_ids: str):
        class FakeState:
            def __init__(self, entity_id):
                self.entity_id = entity_id
                self.state = "1"
                self.attributes = {"options": ["Self Use Mode", "Manual Mode"]}

        class FakeStates:
            def __init__(self, ids):
                self._states = [FakeState(i) for i in ids]

            def async_all(self):
                return self._states

            def get(self, entity_id):
                return next((s for s in self._states if s.entity_id == entity_id), None)

        class FakeHass:
            def __init__(self, ids):
                self.states = FakeStates(ids)

        return FakeHass(entity_ids)

    def _discover(self, hass):
        from custom_components.ess_controller.inverter.roles import (
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        return discover_entities(hass, SOLAX_ROLE_SPECS)

    def test_our_own_capacity_sensor_is_not_taken_for_a_state_of_charge(self):
        hass = self._hass("sensor.ai_ess_controller_usable_battery_capacity")
        assert self._discover(hass) == {}

    def test_none_of_our_entities_are_ever_bound(self):
        hass = self._hass(
            "sensor.ai_ess_controller_usable_battery_capacity",
            "sensor.ai_ess_controller_battery_state_of_charge",
            "number.ai_ess_controller_minimum_state_of_charge",
            "select.ai_ess_controller_strategy",
            "number.ess_controller_max_charge_power",
        )
        assert self._discover(hass) == {}

    def test_the_inverter_is_still_discovered_alongside_them(self):
        """The exclusion must not be so broad that it hides the real thing."""
        hass = self._hass(
            "sensor.ai_ess_controller_usable_battery_capacity",
            "sensor.solax_battery_capacity",
            "select.solax_charger_use_mode",
        )
        found = self._discover(hass)
        assert found.get("soc") == "sensor.solax_battery_capacity"
        assert found.get("use_mode") == "select.solax_charger_use_mode"


class TestRateLimitsAreHandedBack:
    """A forced charge writes the *planned* rate to the charge-current limit.

    Nothing used to write it back, so a slot throttled to 1 kW left the limit at
    1 kW for the rest of the day: solar charging capped, the evening discharge
    capped, and nothing on any screen to say why.
    """

    def test_self_use_restores_both_limits(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_charge_max_current", 3.0)
        hass.states.set("number.solax_battery_discharge_max_current", 3.0)
        adapter = solax_adapter(hass)
        apply(
            adapter,
            command(SlotAction.SELF_USE, max_charge_kw=3.6, max_discharge_kw=3.6),
        )
        written = {c[2]["entity_id"] for c in hass.services.calls}
        assert "number.solax_battery_charge_max_current" in written
        assert "number.solax_battery_discharge_max_current" in written

    def test_a_hold_restores_them_too(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_charge_max_current", 1.0)
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE, max_charge_kw=3.6, max_discharge_kw=3.6))
        written = {c[2]["entity_id"] for c in hass.services.calls}
        assert "number.solax_battery_charge_max_current" in written

    def test_solar_charging_is_not_left_throttled(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_charge_max_current", 1.0)
        adapter = solax_adapter(hass)
        apply(
            adapter,
            command(
                SlotAction.CHARGE_SOLAR_ONLY, max_charge_kw=3.6, max_discharge_kw=3.6
            ),
        )
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        limit = by_entity["number.solax_battery_charge_max_current"]["value"]
        assert limit > 1.0

    def test_a_forced_charge_still_uses_the_planned_rate(self):
        """Restoring the maxima must not override a deliberately throttled charge."""
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_charge_max_current", 20.0)
        adapter = solax_adapter(hass)
        apply(
            adapter,
            command(
                SlotAction.CHARGE, power_kw=1.0, max_charge_kw=3.6, max_discharge_kw=3.6
            ),
        )
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        planned = by_entity["number.solax_battery_charge_max_current"]["value"]
        full = by_entity.get("number.solax_battery_discharge_max_current")
        assert planned < 20.0
        # And the discharge limit is left alone during a forced charge.
        assert full is None

    def test_no_ceilings_given_means_leave_the_limits_alone(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_charge_max_current", 1.0)
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.SELF_USE))
        written = {c[2]["entity_id"] for c in hass.services.calls}
        assert "number.solax_battery_charge_max_current" not in written

    def test_an_unchanged_limit_is_not_rewritten(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        apply(
            adapter,
            command(SlotAction.SELF_USE, max_charge_kw=3.6, max_discharge_kw=3.6),
        )
        first = len(hass.services.calls)
        apply(
            adapter,
            command(SlotAction.SELF_USE, max_charge_kw=3.6, max_discharge_kw=3.6),
        )
        assert len(hass.services.calls) == first


class TestTheHandBackUndoesEverything:
    """What the coordinator sends on unload, seen from the hardware's side.

    The coordinator's job is to send a self-use command at the user's own reserve
    with the configured rate ceilings; this is the other half -- that such a
    command really does take a mid-charge inverter out of Manual mode, put the
    reserve back and lift the throttle, and that it costs nothing at all on an
    install already sitting where it puts things.
    """

    @staticmethod
    def _hand_back():
        return command(
            SlotAction.SELF_USE,
            power_kw=0.0,
            min_soc=10.0,
            max_charge_kw=3.6,
            max_discharge_kw=3.6,
        )

    def test_a_forced_charge_is_undone(self):
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.CHARGE, power_kw=1.0, min_soc=94.0))
        assert hass.states.get("select.solax_charger_use_mode").state == "Manual Mode"

        apply(adapter, self._hand_back())
        assert hass.states.get("select.solax_charger_use_mode").state == "Self Use"
        # 10% of a 360 V pack is the reserve, and the throttle is off: 3.6 kW at
        # 360 V is 10 A, well above the 1 kW slot's limit.
        assert hass.states.get("number.solax_battery_minimum_capacity").state == 10.0
        assert hass.states.get("number.solax_battery_charge_max_current").state > 5.0

    def test_a_raised_reserve_comes_back_down(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        # A hold expresses itself by raising the inverter's own reserve to the
        # charge it is protecting -- here, the stub's 55% state of charge.
        apply(adapter, command(SlotAction.IDLE, min_soc=20.0, hold_absorbs_solar=True))
        assert hass.states.get("number.solax_battery_minimum_capacity").state == 55.0

        apply(adapter, self._hand_back())
        assert hass.states.get("number.solax_battery_minimum_capacity").state == 10.0

    def test_a_settled_install_is_not_written_to(self):
        """The reason the coordinator no longer guesses from the last command: an
        inverter already in self-use at its reserve costs nothing to hand back, so
        the guard belongs here, against the hardware, not there."""
        hass = build_solax_hass()
        hass.states.set(
            "select.solax_lock_state", "Unlocked", options=["Locked", "Unlocked"]
        )
        adapter = solax_adapter(hass)
        apply(adapter, self._hand_back())
        before = len(hass.services.calls)
        assert before > 0

        result = apply(adapter, self._hand_back())
        assert len(hass.services.calls) == before
        assert not result.changed


class TestExportLimitFollowsThePermission:
    """ "Export: off" reaches the inverter as a zero limit, not as a zero price."""

    def test_refusing_export_writes_a_zero_limit(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_export_control_user_limit", 3680)
        adapter = solax_adapter(hass, manage_export_limit=True)
        apply(adapter, command(SlotAction.SELF_USE, export_limit_kw=0.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_export_control_user_limit"]["value"] == 0

    def test_permitting_export_writes_the_connection_limit(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_export_control_user_limit", 0)
        adapter = solax_adapter(hass, manage_export_limit=True)
        apply(adapter, command(SlotAction.SELF_USE, export_limit_kw=3.68))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_export_control_user_limit"]["value"] > 0


class TestHoldWithoutAReserveControl:
    """A hold expressed as self-use only holds if the reserve can be raised.

    A real install exposes no minimum-SoC control at all -- diagnostics showed
    ``min_soc: false`` -- so the soft hold became plain self-use and the battery
    discharged through the very half-hour the plan meant to save it for. Manual
    mode with charge and discharge stopped is the cruder instrument, and where it
    is the only one available it is the right one.
    """

    @staticmethod
    def without_min_soc(hass):
        from custom_components.ess_controller.inverter.roles import (
            ROLE_MIN_SOC,
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        entities.pop(ROLE_MIN_SOC, None)
        return SolaxModbusAdapter(hass, entities)

    def test_it_falls_back_to_stopping_the_battery(self):
        hass = build_solax_hass()
        hass.states.set("select.solax_charger_use_mode", "Self Use", options=SOLAX_MODES)
        hass.states.set(
            "select.solax_manual_mode_select", "Force Charge", options=SOLAX_MANUAL
        )
        adapter = self.without_min_soc(hass)
        apply(adapter, command(SlotAction.IDLE))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_charger_use_mode"]["option"] == "Manual Mode"
        assert by_entity["select.solax_manual_mode_select"]["option"] == (
            "Stop Charge and Discharge"
        )

    def test_a_hold_is_never_silently_just_self_use(self):
        hass = build_solax_hass()
        hass.states.set("select.solax_charger_use_mode", "Self Use", options=SOLAX_MODES)
        adapter = self.without_min_soc(hass)
        apply(adapter, command(SlotAction.IDLE))
        modes = [
            c[2].get("option")
            for c in hass.services.calls
            if c[2]["entity_id"] == "select.solax_charger_use_mode"
        ]
        assert "Self Use" not in modes

    def test_with_a_reserve_control_the_soft_hold_is_still_preferred(self):
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_capacity", 62.4)
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert "select.solax_manual_mode_select" not in by_entity
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == 62.0

    def test_no_soc_reading_also_falls_back(self):
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_capacity", "unknown")
        hass.states.set("select.solax_charger_use_mode", "Self Use", options=SOLAX_MODES)
        hass.states.set(
            "select.solax_manual_mode_select", "Force Charge", options=SOLAX_MANUAL
        )
        adapter = solax_adapter(hass)
        apply(adapter, command(SlotAction.IDLE))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["select.solax_manual_mode_select"]["option"] == (
            "Stop Charge and Discharge"
        )


class TestPeakShavingIsNotGridCharging:
    """Suffix matching bound "charge from grid" to the peak-shaving switch.

    A real install resolved ``switch.<prefix>_peakshaving_charge_from_grid`` for
    the grid-charge role. That is a different feature; toggling it did nothing,
    and the plan believed grid charging was permitted when nothing had changed.
    """

    def test_the_peak_shaving_switch_is_not_bound(self):
        from custom_components.ess_controller.inverter.roles import (
            ROLE_GRID_CHARGE,
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        hass = build_solax_hass()
        hass.states.set("switch.solax_peakshaving_charge_from_grid", "off")
        entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        assert "peakshaving" not in entities.get(ROLE_GRID_CHARGE, "")

    def test_the_real_switch_is_still_found(self):
        from custom_components.ess_controller.inverter.roles import (
            ROLE_GRID_CHARGE,
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        hass = build_solax_hass()
        hass.states.set("switch.solax_peakshaving_charge_from_grid", "off")
        hass.states.set("switch.solax_selfuse_night_charge_enable", "off")
        entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        assert entities[ROLE_GRID_CHARGE] == "switch.solax_selfuse_night_charge_enable"

    def test_finding_nothing_is_reported_rather_than_passed_over(self):
        from custom_components.ess_controller.inverter.roles import (
            ROLE_GRID_CHARGE,
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        hass = build_solax_hass()
        entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        entities.pop(ROLE_GRID_CHARGE, None)
        adapter = SolaxModbusAdapter(hass, entities)
        result = apply(adapter, command(SlotAction.SELF_USE))
        assert any("grid-charge control" in note for note in result.skipped)


class TestUnboundControlsAreNamed:
    """A control plainly present on the inverter's own app, and no role found it.

    "Charge from grid" is the case that mattered: it sat enabled on the inverter
    with "charge battery to 99%", the plan believed it had turned it off, and
    nothing anywhere said which entity it should have been writing to. Guessing IDs
    is what bound it to a peak-shaving switch in the first place, so the adapter now
    reports what it could see and did not use.
    """

    def test_it_names_a_control_no_role_claimed(self):
        hass = build_solax_hass()
        hass.states.set("switch.solax_selfuse_charge_from_grid_enable", "on")
        adapter = solax_adapter(hass)
        described = adapter.describe()
        assert (
            "switch.solax_selfuse_charge_from_grid_enable"
            in described["unbound_candidates"]
        )

    def test_bound_entities_are_not_listed_as_unused(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass)
        described = adapter.describe()
        for entity_id in described["entities"].values():
            assert entity_id not in described["unbound_candidates"]

    def test_it_says_which_roles_went_unfilled(self):
        from custom_components.ess_controller.inverter.roles import (
            ROLE_MIN_SOC,
            SOLAX_ROLE_SPECS,
            discover_entities,
        )

        hass = build_solax_hass()
        entities = discover_entities(hass, SOLAX_ROLE_SPECS, prefix="solax")
        entities.pop(ROLE_MIN_SOC, None)
        described = SolaxModbusAdapter(hass, entities).describe()
        assert ROLE_MIN_SOC in described["unfilled_roles"]

    def test_our_own_entities_are_never_offered(self):
        hass = build_solax_hass()
        hass.states.set("number.ai_ess_controller_min_soc", 20)
        adapter = solax_adapter(hass)
        assert not [
            e for e in adapter.describe()["unbound_candidates"] if "ess_controller" in e
        ]

    def test_unrelated_entities_are_not_offered(self):
        hass = build_solax_hass()
        hass.states.set("switch.kitchen_lights", "on")
        adapter = solax_adapter(hass)
        assert "switch.kitchen_lights" not in adapter.describe()["unbound_candidates"]

    def test_sensors_are_not_offered_as_controls(self):
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_charge_something", 1)
        adapter = solax_adapter(hass)
        assert not [
            e for e in adapter.describe()["unbound_candidates"] if e.startswith("sensor.")
        ]


class TestTheReserveIsOnlyWrittenWhereItApplies:
    """A real inverter rejected the reserve register while in Force Charge.

    ``selfuse_backup_soc`` belongs to the inverter's self-use logic, and in Manual
    mode there is no self-use logic to govern: a forced charge does not consult it.
    Writing it anyway was not harmless -- Modbus exception 4 at register 0xc5 failed
    the whole apply, marked the writes unverified, and put an error on the dashboard
    for a setting that never needed touching.
    """

    def test_a_forced_charge_leaves_the_reserve_alone(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.CHARGE, min_soc=20.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_minimum_capacity" not in entities

    def test_a_forced_discharge_leaves_the_reserve_alone(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.DISCHARGE, min_soc=20.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_minimum_capacity" not in entities

    def test_a_strict_hold_leaves_it_alone_too(self):
        hass = build_solax_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(
            adapter,
            command(SlotAction.IDLE, min_soc=20.0, hold_absorbs_solar=False),
        )
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "number.solax_battery_minimum_capacity" not in entities

    def test_self_use_still_gets_the_planning_floor(self):
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_minimum_capacity", 5.0)
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == 20.0

    def test_a_soft_hold_still_gets_its_pin(self):
        hass = build_solax_hass()
        hass.states.set("sensor.solax_battery_capacity", 57.3)
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.IDLE, min_soc=20.0))
        by_entity = {c[2]["entity_id"]: c[2] for c in hass.services.calls}
        assert by_entity["number.solax_battery_minimum_capacity"]["value"] == 57.0


class TestARefusedWriteIsNotRetriedForEver:
    """A real inverter refused its own reserve register -- Modbus exception 4 -- and
    the controller asked again every five minutes for hours.

    Each attempt failed the whole apply, so "Inverter writes" stayed unverified and
    every genuine problem was buried under a known one. Asking once and saying so is
    more useful than asking three hundred times.
    """

    @staticmethod
    def refusing_hass():
        hass = build_solax_hass()
        hass.states.set("number.solax_battery_minimum_capacity", 90.0)

        original = hass.services.async_call

        async def _call(domain, service, data, blocking=False):
            if data.get("entity_id") == "number.solax_battery_minimum_capacity":
                raise RuntimeError("write was rejected by device 1 at register 0xc5")
            return await original(domain, service, data, blocking=blocking)

        hass.services.async_call = _call
        return hass

    def test_the_refusal_is_recorded(self):
        from custom_components.ess_controller.inverter.roles import ROLE_MIN_SOC

        hass = self.refusing_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        result = apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        assert result.errors
        assert ROLE_MIN_SOC in adapter.rejected_roles()

    def test_it_is_not_attempted_again(self):
        hass = self.refusing_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        before = len(hass.services.calls)
        result = apply(adapter, command(SlotAction.SELF_USE, min_soc=21.0))
        assert len(hass.services.calls) == before
        assert any("not retrying" in note for note in result.skipped)

    def test_the_skip_says_what_the_inverter_said(self):
        hass = self.refusing_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        result = apply(adapter, command(SlotAction.SELF_USE, min_soc=21.0))
        assert any("0xc5" in note for note in result.skipped)

    def test_other_writes_still_go_out(self):
        """One refused register must not stop the controller doing its job."""
        hass = self.refusing_hass()
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        hass.services.calls.clear()
        hass.states.set(
            "select.solax_charger_use_mode", "Manual Mode", options=SOLAX_MODES
        )
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        entities = [c[2]["entity_id"] for c in hass.services.calls]
        assert "select.solax_charger_use_mode" in entities

    def test_a_deliberate_action_clears_the_sulk(self):
        """An override or a settings change is the moment to try once more."""
        hass = self.refusing_hass()
        adapter = solax_adapter(hass, manage_min_soc=True)
        apply(adapter, command(SlotAction.SELF_USE, min_soc=20.0))
        adapter.reset_last_applied()
        assert adapter.rejected_roles() == {}
