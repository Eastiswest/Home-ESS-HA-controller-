"""Adapter for SolaX hybrid inverters via the SolaX Modbus integration.

Control model for an X1/X3 Hybrid G4:

* ``Self Use`` -- normal behaviour; PV first, battery covers the shortfall.
* ``Manual Mode`` plus a manual sub-mode -- ``Force Charge``, ``Force
  Discharge``, or ``Stop Charge and Discharge`` for a true hold.

So every plan action maps onto a working mode, and charge/discharge rate is set
through the battery current limits. Two details are handled carefully because
they cause silent failures in practice:

1. **The lock.** SolaX inverters reject setting writes unless the integration's
   ``Lock State`` is ``Unlocked``. Unlocking is therefore part of applying a
   command, not a manual prerequisite.
2. **Read-back.** A Pocket WiFi dongle will accept a Modbus write and quietly
   drop it. The base class re-reads every write, so a dropped write shows up as
   unverified instead of being mistaken for success.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import ControlCommand, SlotAction
from .base import (
    Capabilities,
    InverterAdapter,
    InverterState,
    Write,
    clamp_to_number,
    pick_option,
    power_kw,
    power_limit_value,
    state_float,
    state_options,
    state_value,
)
from .roles import (
    ROLE_BATTERY_POWER,
    ROLE_BATTERY_VOLTAGE,
    ROLE_CHARGE_LIMIT,
    ROLE_DISCHARGE_LIMIT,
    ROLE_EXPORT_LIMIT,
    ROLE_GRID_CHARGE,
    ROLE_GRID_POWER,
    ROLE_LOAD_POWER,
    ROLE_LOCK,
    ROLE_MANUAL_MODE,
    ROLE_MIN_SOC,
    ROLE_PV_POWER,
    ROLE_SOC,
    ROLE_TARGET_SOC,
    ROLE_USE_MODE,
    SOLAX_ROLE_SPECS,
    unmatched_candidates,
)

_LOGGER = logging.getLogger(__name__)

# Wording varies across firmware and integration versions, so each is a list of
# candidates matched loosely rather than one exact string.
MODE_SELF_USE = ("Self Use", "Self Use Mode", "selfuse")
MODE_MANUAL = ("Manual Mode", "Manual")
MODE_FEED_IN = ("Feedin Priority", "Feed-in Priority", "Feedin")
MODE_BACKUP = ("Back Up Mode", "Backup Mode", "Backup")

MANUAL_FORCE_CHARGE = ("Force Charge",)
MANUAL_FORCE_DISCHARGE = ("Force Discharge",)
MANUAL_STOP = ("Stop Charge and Discharge", "Stop Charge & Discharge", "Stop")

LOCK_UNLOCKED = ("Unlocked", "Unlock")
LOCK_LOCKED = ("Locked", "Lock")

GRID_CHARGE_ON = ("Enabled", "On", "Yes", "true")
GRID_CHARGE_OFF = ("Disabled", "Off", "No", "false")

DEFAULT_NOMINAL_VOLTAGE = 360.0


class SolaxModbusAdapter(InverterAdapter):
    """Drives a SolaX hybrid through the SolaX Modbus integration's entities."""

    name = "solax_modbus"

    # Option vocabularies, as class attributes so another inverter with the
    # same mode-select control model can reuse this adapter by overriding them
    # rather than duplicating the logic.
    OPT_SELF_USE: tuple[str, ...] = MODE_SELF_USE
    OPT_MANUAL: tuple[str, ...] = MODE_MANUAL
    OPT_FORCE_CHARGE: tuple[str, ...] = MANUAL_FORCE_CHARGE
    OPT_FORCE_DISCHARGE: tuple[str, ...] = MANUAL_FORCE_DISCHARGE
    OPT_STOP: tuple[str, ...] = MANUAL_STOP
    OPT_UNLOCKED: tuple[str, ...] = LOCK_UNLOCKED
    OPT_GRID_ON: tuple[str, ...] = GRID_CHARGE_ON
    OPT_GRID_OFF: tuple[str, ...] = GRID_CHARGE_OFF

    def __init__(
        self,
        hass: Any,
        entities: dict[str, str],
        nominal_voltage: float = DEFAULT_NOMINAL_VOLTAGE,
        manage_export_limit: bool = False,
        manage_min_soc: bool = True,
    ) -> None:
        super().__init__(hass)
        self._entities = dict(entities)
        self._nominal_voltage = nominal_voltage or DEFAULT_NOMINAL_VOLTAGE
        self._manage_export_limit = manage_export_limit
        self._manage_min_soc = manage_min_soc

    # -- role helpers ----------------------------------------------------

    def entity(self, role: str) -> str | None:
        return self._entities.get(role)

    @property
    def entities(self) -> dict[str, str]:
        return dict(self._entities)

    # -- capabilities ----------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        use_mode = self.entity(ROLE_USE_MODE)
        manual = self.entity(ROLE_MANUAL_MODE)
        options = state_options(self.hass, manual)
        return Capabilities(
            force_charge=bool(manual)
            and pick_option(options, self.OPT_FORCE_CHARGE) is not None,
            force_discharge=bool(manual)
            and pick_option(options, self.OPT_FORCE_DISCHARGE) is not None,
            idle=bool(manual) and pick_option(options, self.OPT_STOP) is not None,
            self_use=bool(use_mode)
            and pick_option(state_options(self.hass, use_mode), self.OPT_SELF_USE)
            is not None,
            charge_power_limit=bool(self.entity(ROLE_CHARGE_LIMIT)),
            discharge_power_limit=bool(self.entity(ROLE_DISCHARGE_LIMIT)),
            target_soc=bool(self.entity(ROLE_TARGET_SOC)),
            min_soc=bool(self.entity(ROLE_MIN_SOC)),
            export_limit=bool(self.entity(ROLE_EXPORT_LIMIT)),
            grid_charge_toggle=bool(self.entity(ROLE_GRID_CHARGE)),
        )

    # -- reading ---------------------------------------------------------

    async def async_read_state(self) -> InverterState:
        soc = state_float(self.hass, self.entity(ROLE_SOC))
        mode = state_value(self.hass, self.entity(ROLE_USE_MODE))
        lock = state_value(self.hass, self.entity(ROLE_LOCK))
        locked: bool | None = None
        if lock is not None:
            locked = pick_option([lock], self.OPT_UNLOCKED) is None

        return InverterState(
            available=mode is not None or soc is not None,
            soc=soc,
            mode=mode,
            manual_mode=state_value(self.hass, self.entity(ROLE_MANUAL_MODE)),
            battery_voltage=state_float(self.hass, self.entity(ROLE_BATTERY_VOLTAGE)),
            battery_power_kw=power_kw(self.hass, self.entity(ROLE_BATTERY_POWER)),
            pv_power_kw=power_kw(self.hass, self.entity(ROLE_PV_POWER)),
            grid_power_kw=power_kw(self.hass, self.entity(ROLE_GRID_POWER)),
            load_power_kw=power_kw(self.hass, self.entity(ROLE_LOAD_POWER)),
            min_soc=state_float(self.hass, self.entity(ROLE_MIN_SOC)),
            locked=locked,
            raw={"entities": self._entities},
        )

    # -- writing ---------------------------------------------------------

    def plan_writes(self, command: ControlCommand) -> tuple[list[Write], list[str]]:
        writes: list[Write] = []
        skipped: list[str] = []

        use_mode_entity = self.entity(ROLE_USE_MODE)
        manual_entity = self.entity(ROLE_MANUAL_MODE)
        if not use_mode_entity:
            return [], ["no working-mode entity found; cannot control the inverter"]

        use_options = state_options(self.hass, use_mode_entity)
        manual_options = state_options(self.hass, manual_entity)

        action = command.action
        voltage = state_float(self.hass, self.entity(ROLE_BATTERY_VOLTAGE))

        # Unlock first: SolaX refuses setting writes while locked, and a locked
        # inverter is the single most common reason control appears to do nothing.
        lock_entity = self.entity(ROLE_LOCK)
        if lock_entity:
            current_lock = state_value(self.hass, lock_entity)
            unlocked = pick_option(
                state_options(self.hass, lock_entity), self.OPT_UNLOCKED
            )
            already_unlocked = (
                current_lock is not None
                and pick_option([current_lock], self.OPT_UNLOCKED) is not None
            )
            if unlocked and current_lock is not None and not already_unlocked:
                writes.append(_select_write(lock_entity, unlocked, ROLE_LOCK))

        # A hold expressed as self-use only holds if the reserve can be raised to
        # the current charge. Where the inverter exposes no minimum-SoC control --
        # and a real install exposes none -- that "hold" is plain self-use and the
        # battery cheerfully discharges through the half-hour the plan meant to
        # save it. Manual mode with charge and discharge stopped is the cruder
        # instrument, and where it is the only one, it is the right one.
        soft_hold = (
            action is SlotAction.IDLE
            and command.hold_absorbs_solar
            and self._manage_min_soc
            and bool(self.entity(ROLE_MIN_SOC))
            and state_float(self.hass, self.entity(ROLE_SOC)) is not None
        )
        manual = action in (SlotAction.CHARGE, SlotAction.DISCHARGE) or (
            action is SlotAction.IDLE and not soft_hold
        )
        if manual:
            manual_target = {
                SlotAction.CHARGE: self.OPT_FORCE_CHARGE,
                SlotAction.DISCHARGE: self.OPT_FORCE_DISCHARGE,
                SlotAction.IDLE: self.OPT_STOP,
            }[action]
            manual_option = pick_option(manual_options, manual_target)
            manual_mode_option = pick_option(use_options, self.OPT_MANUAL)

            if manual_option is None or manual_mode_option is None or not manual_entity:
                skipped.append(
                    f"{action.value} not supported by this inverter; using self-use"
                )
                writes.extend(
                    self._self_use_writes(use_mode_entity, use_options, skipped)
                )
            else:
                writes.extend(
                    _mode_writes(
                        self.hass,
                        use_mode_entity,
                        manual_mode_option,
                        manual_entity,
                        manual_option,
                    )
                )
                writes.extend(self._power_writes(action, command, voltage, skipped))
        else:
            # SELF_USE and CHARGE_SOLAR_ONLY both map to plain self-use; the
            # difference between them is whether grid charging is permitted. A
            # hold that must not throw solar away joins them, held in place by the
            # inverter's own reserve rather than by Manual mode -- see
            # _hold_floor.
            writes.extend(self._self_use_writes(use_mode_entity, use_options, skipped))
            # Handing the inverter back its own logic means handing back its full
            # rate as well. A forced charge writes the planned rate to the
            # charge-current limit and nothing used to write it back, so a slot
            # throttled to 1 kW left solar charging capped at 1 kW for the rest of
            # the day.
            writes.extend(self._restore_rate_writes(command, voltage, skipped))

        writes.extend(self._grid_charge_writes(command, action, skipped))
        writes.extend(self._min_soc_writes(command, action, manual, skipped))
        writes.extend(self._export_limit_writes(command, skipped))

        return _dedupe(writes), skipped

    def _self_use_writes(
        self, use_mode_entity: str, use_options: list[str], skipped: list[str]
    ) -> list[Write]:
        option = pick_option(use_options, self.OPT_SELF_USE)
        if option is None:
            skipped.append("no Self Use option found on the working-mode entity")
            return []
        if _normalised_state(self.hass, use_mode_entity) == _norm(option):
            return []
        return [_select_write(use_mode_entity, option, ROLE_USE_MODE)]

    def _restore_rate_writes(
        self, command: ControlCommand, voltage: float | None, skipped: list[str]
    ) -> list[Write]:
        """Put both current limits back to the configured maxima."""
        writes: list[Write] = []
        for role, limit in (
            (ROLE_CHARGE_LIMIT, command.max_charge_kw),
            (ROLE_DISCHARGE_LIMIT, command.max_discharge_kw),
        ):
            if limit is None or limit <= 0:
                continue
            entity_id = self.entity(role)
            if not entity_id:
                continue
            raw = power_limit_value(
                self.hass, entity_id, limit, voltage, self._nominal_voltage
            )
            value = clamp_to_number(self.hass, entity_id, raw)
            current = state_float(self.hass, entity_id)
            if current is not None and abs(current - value) <= max(value * 0.03, 0.5):
                continue
            writes.append(_number_write(entity_id, value, role))
        return writes

    def _power_writes(
        self,
        action: SlotAction,
        command: ControlCommand,
        voltage: float | None,
        skipped: list[str],
    ) -> list[Write]:
        role = ROLE_CHARGE_LIMIT if action is SlotAction.CHARGE else ROLE_DISCHARGE_LIMIT
        if action is SlotAction.IDLE:
            return []
        entity_id = self.entity(role)
        if not entity_id:
            skipped.append(f"no {role} entity; rate will be inverter default")
            return []
        if command.power_kw <= 0:
            return []
        raw = power_limit_value(
            self.hass, entity_id, command.power_kw, voltage, self._nominal_voltage
        )
        value = clamp_to_number(self.hass, entity_id, raw)
        current = state_float(self.hass, entity_id)
        if current is not None and abs(current - value) <= max(value * 0.03, 0.5):
            return []
        return [_number_write(entity_id, value, role)]

    def _grid_charge_writes(
        self, command: ControlCommand, action: SlotAction, skipped: list[str]
    ) -> list[Write]:
        entity_id = self.entity(ROLE_GRID_CHARGE)
        if not entity_id:
            # Worth saying rather than passing over in silence: without this
            # control, "Charge from the grid" is advisory and whatever the inverter
            # is set to is what happens.
            skipped.append(
                "no grid-charge control found; the inverter's own setting decides"
            )
            return []
        # A forced charge inherently draws from the grid, so the toggle only
        # matters while the inverter is running its own self-use logic.
        if action is SlotAction.CHARGE:
            return []
        # Never during a hold. A hold expressed as self-use with a raised reserve
        # leaves the inverter running its own logic, and its own logic with grid
        # charging enabled will buy energy to fill the pack -- which is the
        # opposite of holding, and expensive at the prices a hold happens at.
        want_on = command.allow_grid_charge and action not in (
            SlotAction.CHARGE_SOLAR_ONLY,
            SlotAction.IDLE,
        )
        domain = entity_id.split(".", 1)[0]

        if domain == "switch":
            current = state_value(self.hass, entity_id)
            if current is not None and (current == "on") == want_on:
                return []
            return [
                Write(
                    domain="switch",
                    service="turn_on" if want_on else "turn_off",
                    entity_id=entity_id,
                    value="on" if want_on else "off",
                    role=ROLE_GRID_CHARGE,
                    field="",
                )
            ]

        options = state_options(self.hass, entity_id)
        option = pick_option(options, self.OPT_GRID_ON if want_on else self.OPT_GRID_OFF)
        if option is None:
            skipped.append("could not match a grid-charge option")
            return []
        if _normalised_state(self.hass, entity_id) == _norm(option):
            return []
        return [_select_write(entity_id, option, ROLE_GRID_CHARGE)]

    def _hold_floor(self, command: ControlCommand, skipped: list[str]) -> float | None:
        """The reserve that turns self-use into a hold, or None if unavailable.

        Manual mode with "Stop Charge and Discharge" is the obvious way to hold a
        battery, and it is the wrong one whenever export earns nothing: it stops
        the battery charging from the array too, so a sunnier half-hour than the
        forecast expected is given to the grid for nothing. It also means a hold
        that outlives its slot -- a restart, a lost link -- leaves the inverter in
        Manual mode, which is not a state to be stuck in.

        Self-use with the inverter's own reserve raised to the current state of
        charge holds the battery just as firmly against discharge, still lets the
        array charge it, and degrades to ordinary self-use if the controller stops
        talking. Rounded down so a reading drifting by a fraction of a percent
        does not rewrite the setting every cycle.
        """
        soc = state_float(self.hass, self.entity(ROLE_SOC))
        if soc is None:
            skipped.append("hold wanted but no SoC reading; using the plain reserve")
            return None
        return max(command.min_soc, float(int(soc)))

    def _min_soc_writes(
        self,
        command: ControlCommand,
        action: SlotAction,
        manual: bool,
        skipped: list[str],
    ) -> list[Write]:
        if not self._manage_min_soc:
            return []
        if manual:
            # The reserve belongs to the inverter's self-use logic, and in Manual
            # mode there is no self-use logic to govern -- a forced charge or
            # discharge does not consult it. Writing it anyway achieved nothing and
            # was not harmless: a real inverter rejected the register outright
            # (Modbus exception 4 at 0xc5) while in Force Charge, which failed the
            # apply, marked the writes unverified, and put an error on the dashboard
            # for a setting that did not need touching.
            return []
        entity_id = self.entity(ROLE_MIN_SOC)
        if not entity_id:
            return []
        target = command.min_soc
        if action is SlotAction.IDLE and command.hold_absorbs_solar:
            floor = self._hold_floor(command, skipped)
            if floor is not None:
                target = floor
        value = clamp_to_number(self.hass, entity_id, target)
        current = state_float(self.hass, entity_id)
        if current is not None and abs(current - value) <= 0.5:
            return []
        return [_number_write(entity_id, value, ROLE_MIN_SOC)]

    def _export_limit_writes(
        self, command: ControlCommand, skipped: list[str]
    ) -> list[Write]:
        if not self._manage_export_limit or command.export_limit_kw is None:
            return []
        entity_id = self.entity(ROLE_EXPORT_LIMIT)
        if not entity_id:
            return []
        raw = power_limit_value(
            self.hass, entity_id, command.export_limit_kw, None, self._nominal_voltage
        )
        value = clamp_to_number(self.hass, entity_id, raw)
        current = state_float(self.hass, entity_id)
        if current is not None and abs(current - value) <= max(value * 0.03, 1.0):
            return []
        return [_number_write(entity_id, value, ROLE_EXPORT_LIMIT)]

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["entities"] = self._entities
        data["nominal_voltage"] = self._nominal_voltage
        data["manage_export_limit"] = self._manage_export_limit
        # Every control a role failed to find is a control the plan cannot use, and
        # the inverter's own app will happily show it sitting there enabled. Listing
        # the near-misses turns "why is charge-from-grid still on?" into a name that
        # can be mapped, rather than another guess at an entity ID.
        data["unbound_candidates"] = unmatched_candidates(self.hass, self._entities)
        data["unfilled_roles"] = sorted(
            spec.role for spec in SOLAX_ROLE_SPECS if not self.entity(spec.role)
        )
        return data


def _mode_writes(
    hass: Any,
    use_mode_entity: str,
    use_option: str,
    manual_entity: str,
    manual_option: str,
) -> list[Write]:
    """Set the working mode and its manual sub-mode, skipping no-op writes.

    The manual sub-mode is written first: switching into Manual Mode while the
    sub-mode still says ``Force Discharge`` would briefly discharge the battery
    before the second write lands.
    """
    writes: list[Write] = []
    if _normalised_state(hass, manual_entity) != _norm(manual_option):
        writes.append(_select_write(manual_entity, manual_option, ROLE_MANUAL_MODE))
    if _normalised_state(hass, use_mode_entity) != _norm(use_option):
        writes.append(_select_write(use_mode_entity, use_option, ROLE_USE_MODE))
    return writes


def _select_write(entity_id: str, option: str, role: str) -> Write:
    return Write(
        domain="select",
        service="select_option",
        entity_id=entity_id,
        value=option,
        role=role,
        field="option",
    )


def _number_write(entity_id: str, value: float, role: str) -> Write:
    return Write(
        domain="number",
        service="set_value",
        entity_id=entity_id,
        value=value,
        role=role,
        field="value",
    )


def _dedupe(writes: list[Write]) -> list[Write]:
    """Keep the last write per entity, preserving order."""
    seen: dict[str, Write] = {}
    for write in writes:
        seen[write.entity_id] = write
    ordered: list[Write] = []
    used: set[str] = set()
    for write in writes:
        if write.entity_id in used:
            continue
        used.add(write.entity_id)
        ordered.append(seen[write.entity_id])
    return ordered


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _normalised_state(hass: Any, entity_id: str) -> str | None:
    raw = state_value(hass, entity_id)
    return _norm(raw) if raw is not None else None
