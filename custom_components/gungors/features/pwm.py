"""`pwm`: drive an on/off heater from the demand by pulse-width modulation.

ScratMan's pwm_switch logic:

    - pwm:
        entity: switch.koridor0_socket
        period: "00:15:00"
        min_cycle_duration: 60      # shortest on (and, unless set, off) time

Demand 100 keeps the heater on, 0 keeps it off; in between the heater is on for
`period * demand / 100` per period, stretched to the minimum on/off times. A new
target above (below) the room temperature switches on (off) at the next cycle. The
heater is switched off while the entity is unavailable. hvac_action is heating
while the heater is on.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..core import Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "pwm"
FULL = 100

SCHEMA = vol.Schema(
    {
        vol.Required("entity"): cv.entity_id,
        vol.Required("period"): vol.All(cv.time_period, lambda t: t.total_seconds()),
        vol.Optional("min_cycle_duration", default=0): vol.All(
            cv.time_period, lambda t: t.total_seconds()
        ),
        vol.Optional("min_off_cycle_duration"): vol.All(
            cv.time_period, lambda t: t.total_seconds()
        ),
    }
)


@register("climate")
class Pwm(Feature):
    feature_name = KEY
    schema = SCHEMA
    requires = frozenset({"demand"})

    _pwm_last_cycle = 0.0
    _pwm_force_on = False
    _pwm_force_off = False

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {conf["entity"]}

    @property
    def _pwm_entity(self) -> str:
        return self.gw_conf(KEY)["entity"]

    @property
    def _pwm_min_on(self) -> float:
        return self.gw_conf(KEY)["min_cycle_duration"]

    @property
    def _pwm_min_off(self) -> float:
        conf = self.gw_conf(KEY)
        off = conf.get("min_off_cycle_duration")
        return conf["min_cycle_duration"] if off is None else off

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._pwm_entity], self._pwm_changed)
        )

    @callback
    def _pwm_changed(self, event: Event) -> None:
        self.async_write_ha_state()  # hvac_action follows the switch

    # --- chain ---------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        temperature = request.data.get("temperature")
        current = self._gw_current_temp
        if temperature is not None and current is not None and not request.from_device:
            if temperature > current:
                self._pwm_force_on = True
            elif temperature < current:
                self._pwm_force_off = True
        await super().gw_apply(request)

    async def gw_demand(self, value: float) -> None:
        await super().gw_demand(value)
        if abs(value) >= FULL:
            await self._pwm_turn_on()
        elif abs(value) > 0:
            await self._pwm_switch(abs(value))
        else:
            await self._pwm_turn_off()

    @callback
    def gw_device_available(self, available: bool) -> None:
        if not available:
            self.hass.async_create_task(self._pwm_turn_off(force=True))
        super().gw_device_available(available)

    @property
    def gw_heating(self) -> bool:
        state = self.hass.states.get(self._pwm_entity)
        return state is not None and state.state == STATE_ON

    # --- switching -----------------------------------------------------------

    def _pwm_ready(self) -> bool:
        state = self.hass.states.get(self._pwm_entity)
        return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)

    async def _pwm_call(self, service: str) -> None:
        domain = self._pwm_entity.split(".", 1)[0]
        domain = domain if domain in ("switch", "input_boolean", "light") else "homeassistant"
        await self.hass.services.async_call(domain, service, {ATTR_ENTITY_ID: self._pwm_entity})

    async def _pwm_turn_on(self) -> None:
        if not self._pwm_ready():
            return
        change = False
        if self.gw_heating:
            pass  # refresh
        elif time.time() - self._pwm_last_cycle >= self._pwm_min_off:
            change = True
        else:
            _LOGGER.info("%s: turning on rejected, cycle too short", self.entity_id)
            return
        await self._pwm_call("turn_on")
        if change:
            self._pwm_last_cycle = time.time()

    async def _pwm_turn_off(self, force: bool = False) -> None:
        if not self._pwm_ready():
            return
        change = False
        if not self.gw_heating:
            pass  # refresh
        elif time.time() - self._pwm_last_cycle >= self._pwm_min_on or force:
            change = True
        else:
            _LOGGER.info("%s: turning off rejected, cycle too short", self.entity_id)
            return
        await self._pwm_call("turn_off")
        if change:
            self._pwm_last_cycle = time.time()

    async def _pwm_switch(self, output: float) -> None:
        period = self.gw_conf(KEY)["period"]
        passed = time.time() - self._pwm_last_cycle
        time_on = period * output / FULL
        time_off = period - time_on
        if 0 < time_on < self._pwm_min_on:
            time_off *= self._pwm_min_on / time_on
            time_on = self._pwm_min_on
        if 0 < time_off < self._pwm_min_off:
            time_on *= self._pwm_min_off / time_off
            time_off = self._pwm_min_off
        if self.gw_heating:
            if time_on <= passed or self._pwm_force_off:
                await self._pwm_turn_off()
            else:
                await self._pwm_turn_on()  # refresh
        elif time_off <= passed or self._pwm_force_on:
            await self._pwm_turn_on()
        else:
            await self._pwm_turn_off()  # refresh
        self._pwm_force_on = False
        self._pwm_force_off = False
