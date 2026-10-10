"""`valve`: write the demand to a TRV's valve opening/closing degree numbers.

    - valve:
        opening: number.x_valve_opening_degree
        closing: number.x_valve_closing_degree
        min: 10        # minimum opening (%) the demand is scaled onto

opening = min + demand * (100 - min) / 100, closing = 100 - opening. A number that
already holds its value is not written again. hvac_action is heating while the
demand (`heater_value`) is above 0. Nothing is written while the TRV is
unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from ..core import Feature, as_float, register

_LOGGER = logging.getLogger(__name__)

KEY = "valve"

SCHEMA = vol.Schema(
    {
        vol.Required("opening"): cv.entity_ids,
        vol.Required("closing"): cv.entity_ids,
        vol.Optional("min", default=0): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
    }
)


@register("climate")
class Valve(Feature):
    feature_name = KEY
    schema = SCHEMA
    requires = frozenset({"demand"})

    _valve_value = 0
    _valve_lock: asyncio.Lock | None = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {*conf["opening"], *conf["closing"]}

    async def gw_demand(self, value: float) -> None:
        self._valve_value = max(0, min(100, int(value)))
        await super().gw_demand(self._valve_value)
        await self._valve_write()

    @callback
    def gw_device_available(self, available: bool) -> None:
        if not available:
            self._valve_value = 0  # the valves belong to the unreachable TRV
            self._gw_demand = 0
        super().gw_device_available(available)

    async def _valve_write(self) -> None:
        if not self.available:
            return
        if self._valve_lock is None:
            self._valve_lock = asyncio.Lock()
        conf = self.gw_conf(KEY)
        minimum = conf["min"]
        opening = int(round(minimum + self._valve_value * (100 - minimum) / 100, 6))
        closing = 100 - opening
        async with self._valve_lock:
            for entity_ids, value in ((conf["opening"], opening), (conf["closing"], closing)):
                todo = [e for e in entity_ids if not self._valve_holds(e, value)]
                if not todo:
                    continue
                try:
                    await self.hass.services.async_call(
                        "number", "set_value", {ATTR_ENTITY_ID: todo, "value": value},
                        blocking=True,
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("%s: setting %s to %s failed", self.entity_id, todo, value)

    def _valve_holds(self, entity_id: str, value: float) -> bool:
        state = self.hass.states.get(entity_id)
        current = as_float(state.state) if state is not None else None
        return current is not None and abs(current - value) < 0.01

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs["heater_value"] = self._valve_value
        return attrs
