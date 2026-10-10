"""`link` for climates: two-way sync with a physical thermostat (TRV, boiler circuit).

`link: climate.x` or `link: {entity: climate.x, cooldown_time: 1}`.

- Wrapper -> device: every accepted request and every push writes the mode and
  target to the device (only what differs). Around OFF only the mode is written:
  the device applies its own off setpoint, which is mirrored back. When the
  device is switched on from OFF, the setpoint waits until it reports the new mode
  (EMS-ESP stores a setpoint received while OFF as its off temperature).
- Device -> wrapper: a change on the device is applied after `cooldown_time`
  seconds without further changes, as a physical request. Events right after our
  own write are echoes.
- At startup and when the device comes back, the wrapper is the source: its state
  is pushed to the device.
- The wrapper is unavailable while the device is; the device's min_temp is used
  when the wrapper has none configured.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_start

from ..const import (
    ATTR_PHYSICAL_THERMOSTAT,
    PHYSICAL_MODE_WAIT,
    PUSH_ECHO_TIMEOUT,
    TEMP_TOLERANCE,
)
from ..core import Feature, Request, as_float, register

_LOGGER = logging.getLogger(__name__)

KEY = "link"
CONF_ENTITY = "entity"
CONF_COOLDOWN_TIME = "cooldown_time"
DEFAULT_COOLDOWN = 1.0


def _schema(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = {CONF_ENTITY: value}
    value = vol.Schema(
        {
            vol.Required(CONF_ENTITY): cv.entity_domain("climate"),
            vol.Optional(CONF_COOLDOWN_TIME, default=DEFAULT_COOLDOWN): vol.All(
                cv.time_period, cv.positive_timedelta, lambda t: t.total_seconds()
            ),
        }
    )(value)
    return value


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


@register("climate")
class ClimateLink(Feature):
    feature_name = KEY
    schema = _schema
    last = True

    _link_available = False
    _link_synced = False
    _link_min_temp: float | None = None
    _link_pending: dict[str, Any] | None = None
    _link_pending_unsub = None
    _link_cooling_unsub = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {conf[CONF_ENTITY]}

    @property
    def _link_entity(self) -> str:
        return self.gw_conf(KEY)[CONF_ENTITY]

    def _link_state(self) -> State | None:
        return self.hass.states.get(self._link_entity)

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        state = self._link_state()
        self._link_available = state is not None and state.state != STATE_UNAVAILABLE
        await super().async_added_to_hass()
        self.async_on_remove(self._link_cancel_timers)
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._link_entity], self._link_changed)
        )
        if state is not None:
            self._link_update_min_temp(state)
        self.async_on_remove(async_at_start(self.hass, self._link_on_start))

    async def _link_on_start(self, _hass) -> None:
        await self._link_initial_push()

    async def _link_initial_push(self) -> None:
        if self._link_synced or not _usable(self._link_state()):
            return  # retried when the device becomes available
        self._link_synced = True
        await self._link_push()

    # --- chain ---------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        await super().gw_apply(request)
        if not request.from_device:
            await self._link_push()

    async def gw_push(self) -> None:
        await super().gw_push()
        await self._link_push()

    def gw_device_min_temp(self) -> float | None:
        return self._link_min_temp

    # --- wrapper -> device ---------------------------------------------------

    async def _link_push(self) -> None:
        physical = self._link_state()
        if not _usable(physical):
            return
        self._link_cancel_cooling()  # the wrapper is the truth now

        mode = self._gw_hvac_mode
        mode_str = mode.value if isinstance(mode, HVACMode) else (str(mode) if mode else None)
        target = self._gw_target_temp
        physical_modes = [str(m) for m in (physical.attributes.get("hvac_modes") or [])]
        physical_temp = as_float(physical.attributes.get(ATTR_TEMPERATURE))
        mode_supported = mode_str in physical_modes
        temp_differs = target is not None and (
            physical_temp is None or abs(physical_temp - target) > TEMP_TOLERANCE
        )

        calls: list[tuple[str, dict[str, Any]]] = []
        expected_mode: str | None = None
        expected_temp: float | None = None
        wait_for_mode = False
        if mode_str == HVACMode.OFF.value:
            if mode_supported and physical.state != HVACMode.OFF.value:
                calls.append(("set_hvac_mode", {"hvac_mode": mode_str}))
                expected_mode = mode_str
        else:
            if mode_supported and physical.state != mode_str:
                calls.append(("set_hvac_mode", {"hvac_mode": mode_str}))
                expected_mode = mode_str
                wait_for_mode = physical.state == HVACMode.OFF.value
            if temp_differs and (mode_supported or physical.state != HVACMode.OFF.value):
                calls.append(("set_temperature", {ATTR_TEMPERATURE: target}))
                expected_temp = target
        if not calls:
            return

        self._link_clear_pending()
        self._link_pending = {"mode": expected_mode, "temp": expected_temp}
        for service, data in calls:
            await self._link_call(service, data)
            if service == "set_hvac_mode" and wait_for_mode and len(calls) > 1:
                await self._link_wait_mode(mode_str)
        if self._link_pending is not None:
            self._link_pending_unsub = async_call_later(
                self.hass, PUSH_ECHO_TIMEOUT, self._link_pending_expired
            )

    async def _link_wait_mode(self, mode: str) -> None:
        state = self._link_state()
        if state is not None and state.state == mode:
            return
        reached = asyncio.Event()

        @callback
        def check(event: Event) -> None:
            new_state = event.data["new_state"]
            if new_state is not None and new_state.state == mode:
                reached.set()

        unsub = async_track_state_change_event(self.hass, [self._link_entity], check)
        try:
            await asyncio.wait_for(reached.wait(), PHYSICAL_MODE_WAIT)
        except TimeoutError:
            _LOGGER.warning(
                "%s: %s did not report %s within %ss; sending the setpoint anyway",
                self.entity_id, self._link_entity, mode, PHYSICAL_MODE_WAIT,
            )
        finally:
            unsub()

    async def _link_call(self, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(
                "climate", service, {ATTR_ENTITY_ID: self._link_entity, **data}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "%s: climate.%s on %s failed", self.entity_id, service, self._link_entity
            )

    @callback
    def _link_pending_expired(self, _now) -> None:
        self._link_pending_unsub = None
        self._link_pending = None

    # --- device -> wrapper ---------------------------------------------------

    @callback
    def _link_changed(self, event: Event) -> None:
        new_state: State | None = event.data["new_state"]
        old_state: State | None = event.data["old_state"]
        if new_state is not None:
            self._link_update_min_temp(new_state)

        available = new_state is not None and new_state.state != STATE_UNAVAILABLE
        if available != self._link_available:
            self._link_available = available
            if available:
                _LOGGER.info("%s: %s is available again", self.entity_id, self._link_entity)
                self._link_synced = False
            else:
                _LOGGER.warning("%s: %s is unavailable", self.entity_id, self._link_entity)
                self._link_cancel_timers()
            self.gw_device_available(available)
            self.async_write_ha_state()
            if available and self.hass.state == CoreState.running:
                self.hass.async_create_task(self._link_initial_push())
            return

        if not _usable(new_state):
            return
        if not self._link_synced:
            self.hass.async_create_task(self._link_initial_push())
            return
        if self._link_pending is not None:
            if self._link_pending_matches(new_state):
                self._link_clear_pending()
                # The echo may carry values we did not command (the device's off
                # setpoint): read the device once it settles.
                self._link_schedule_apply()
            return
        if old_state is not None and _signature(old_state) == _signature(new_state):
            return  # e.g. only current_temperature changed
        self._link_schedule_apply()

    @callback
    def _link_schedule_apply(self) -> None:
        self._link_cancel_cooling()
        self._link_cooling_unsub = async_call_later(
            self.hass, self.gw_conf(KEY)[CONF_COOLDOWN_TIME], self._link_apply
        )

    async def _link_apply(self, _now) -> None:
        """Cooldown elapsed: apply the device's mode and target as physical."""
        self._link_cooling_unsub = None
        physical = self._link_state()
        if not _usable(physical):
            return
        data: dict[str, Any] = {}
        try:
            mode = HVACMode(physical.state)
        except ValueError:
            mode = None
        if mode is not None and mode in self.hvac_modes and mode != self._gw_hvac_mode:
            data["hvac_mode"] = mode
        temp = as_float(physical.attributes.get(ATTR_TEMPERATURE))
        if temp is not None and (
            self._gw_target_temp is None or abs(temp - self._gw_target_temp) > TEMP_TOLERANCE
        ):
            data["temperature"] = temp
        if data:
            await self.gw_physical(data)

    # --- helpers -------------------------------------------------------------

    def _link_pending_matches(self, state: State) -> bool:
        pending = self._link_pending or {}
        if pending.get("mode") is not None and state.state != pending["mode"]:
            return False
        if pending.get("temp") is not None:
            temp = as_float(state.attributes.get(ATTR_TEMPERATURE))
            step = as_float(state.attributes.get("target_temp_step"))
            tolerance = max(TEMP_TOLERANCE, step / 2 + 0.01) if step else TEMP_TOLERANCE
            if temp is None or abs(temp - pending["temp"]) > tolerance:
                return False
        return True

    def _link_update_min_temp(self, state: State) -> None:
        value = as_float(state.attributes.get("min_temp"))
        if value is not None:
            self._link_min_temp = value

    def _link_cancel_cooling(self) -> None:
        if self._link_cooling_unsub is not None:
            self._link_cooling_unsub()
            self._link_cooling_unsub = None

    def _link_clear_pending(self) -> None:
        if self._link_pending_unsub is not None:
            self._link_pending_unsub()
            self._link_pending_unsub = None
        self._link_pending = None

    @callback
    def _link_cancel_timers(self) -> None:
        self._link_cancel_cooling()
        self._link_clear_pending()

    # --- state ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._link_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PHYSICAL_THERMOSTAT] = self._link_entity
        return attrs


def _signature(state: State) -> tuple[Any, Any]:
    return state.state, state.attributes.get(ATTR_TEMPERATURE)
