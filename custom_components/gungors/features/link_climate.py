"""`link` for climates: the physical thermostat (TRV, boiler circuit).

`link: climate.x`.

- The wrapper is a puppet of the thermostat: its HVAC mode and target are always
  the thermostat's. It keeps neither of its own, so nothing has to be synced.
- Accepted requests are sent to the thermostat (only what differs); the wrapper
  changes when the thermostat reports. When the thermostat is switched on from
  OFF, the setpoint waits until it reports the new mode (EMS-ESP stores a
  setpoint received while OFF as its off temperature).
- A change on the thermostat that we did not command is a physical request (it
  goes through the features, e.g. a program rule brings it back). Reports within
  a few seconds after our own command are its result and are taken as is.
- The wrapper is unavailable while the thermostat is; the thermostat's min_temp
  is used when the wrapper has none configured.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    ATTR_PHYSICAL_THERMOSTAT,
    PHYSICAL_MODE_WAIT,
    PUSH_ECHO_TIMEOUT,
    TEMP_TOLERANCE,
)
from ..core import SOURCE_PHYSICAL, Feature, Request, as_float, register

_LOGGER = logging.getLogger(__name__)

KEY = "link"


def _usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


@register("climate")
class ClimateLink(Feature):
    feature_name = KEY
    schema = cv.entity_domain("climate")
    last = True

    _link_available = False
    _link_min_temp: float | None = None
    _link_sent_at = -PUSH_ECHO_TIMEOUT

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {conf}

    @property
    def _link_entity(self) -> str:
        return self.gw_conf(KEY)

    def _link_state(self) -> State | None:
        return self.hass.states.get(self._link_entity)

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        state = self._link_state()
        self._link_available = state is not None and state.state != STATE_UNAVAILABLE
        if state is not None:
            self._link_update_min_temp(state)
        if _usable(state):
            device = self._link_device(state)
            self._gw_hvac_mode = device.get("hvac_mode")
            self._gw_target_temp = device.get("temperature")
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._link_entity], self._link_changed)
        )

    # --- chain ---------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        if request.from_device:
            await super().gw_apply(request)
            return
        # Not passed on to the base on purpose: the wrapper changes when the
        # thermostat reports, not when it is commanded.
        await self._link_send(request.data.get("hvac_mode"), as_float(request.data.get("temperature")))

    async def gw_push(self) -> None:
        await super().gw_push()
        await self._link_send(self._gw_hvac_mode, self._gw_target_temp)

    def gw_device_min_temp(self) -> float | None:
        return self._link_min_temp

    # --- wrapper -> thermostat -----------------------------------------------

    async def _link_send(self, mode: Any, target: float | None) -> None:
        physical = self._link_state()
        if not _usable(physical):
            return
        mode_str = HVACMode(mode).value if mode is not None else None
        physical_modes = [str(m) for m in (physical.attributes.get("hvac_modes") or [])]
        physical_temp = as_float(physical.attributes.get(ATTR_TEMPERATURE))
        mode_change = (
            mode_str is not None and mode_str in physical_modes and physical.state != mode_str
        )
        heating = (mode_str or physical.state) != HVACMode.OFF.value
        temp_change = (
            heating
            and target is not None
            and (physical_temp is None or abs(physical_temp - target) > TEMP_TOLERANCE)
        )
        if not mode_change and not temp_change:
            return
        self._link_sent_at = time.monotonic()
        if mode_change:
            await self._link_call("set_hvac_mode", {"hvac_mode": mode_str})
            if temp_change and physical.state == HVACMode.OFF.value:
                await self._link_wait_mode(mode_str)
        if temp_change:
            self._link_sent_at = time.monotonic()
            await self._link_call("set_temperature", {ATTR_TEMPERATURE: target})

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

    # --- thermostat -> wrapper -----------------------------------------------

    @callback
    def _link_changed(self, event: Event) -> None:
        new_state: State | None = event.data["new_state"]
        if new_state is not None:
            self._link_update_min_temp(new_state)

        available = new_state is not None and new_state.state != STATE_UNAVAILABLE
        if available != self._link_available:
            self._link_available = available
            if available:
                _LOGGER.info("%s: %s is available again", self.entity_id, self._link_entity)
            else:
                _LOGGER.warning("%s: %s is unavailable", self.entity_id, self._link_entity)
            self.gw_device_available(available)
            self.async_write_ha_state()
        if not _usable(new_state):
            return
        data = self._link_changes(self._link_device(new_state))
        if not data:
            return
        if time.monotonic() - self._link_sent_at < PUSH_ECHO_TIMEOUT:
            # The result of our own command: taken as is.
            self.hass.async_create_task(
                self.gw_apply(Request(data, source=SOURCE_PHYSICAL, from_device=True))
            )
        else:
            self.hass.async_create_task(self.gw_physical(data))

    # --- helpers -------------------------------------------------------------

    def _link_device(self, state: State) -> dict[str, Any]:
        """The thermostat's mode and target as request data (what the wrapper knows)."""
        data: dict[str, Any] = {}
        try:
            mode = HVACMode(state.state)
        except ValueError:
            mode = None
        if mode is not None and mode in self.hvac_modes:
            data["hvac_mode"] = mode
        temp = as_float(state.attributes.get(ATTR_TEMPERATURE))
        if temp is not None:
            data["temperature"] = temp
        return data

    def _link_changes(self, device: dict[str, Any]) -> dict[str, Any]:
        """What in the thermostat's data differs from the wrapper."""
        data: dict[str, Any] = {}
        mode = device.get("hvac_mode")
        if mode is not None and mode != self._gw_hvac_mode:
            data["hvac_mode"] = mode
        temp = device.get("temperature")
        if temp is not None and (
            self._gw_target_temp is None or abs(temp - self._gw_target_temp) > TEMP_TOLERANCE
        ):
            data["temperature"] = temp
        return data

    def _link_update_min_temp(self, state: State) -> None:
        value = as_float(state.attributes.get("min_temp"))
        if value is not None:
            self._link_min_temp = value

    # --- state ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._link_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PHYSICAL_THERMOSTAT] = self._link_entity
        return attrs
