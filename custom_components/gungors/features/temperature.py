"""`temperature`: the room temperature from a sensor.

`temperature: sensor.x_temperature`. Sets the climate's current temperature and
the sample times a controller needs (``_gw_temp_time``, ``_gw_temp_prev_time``,
``_gw_temp_updated``: the "current_temperature" contract), then calls
``gw_temperature_changed()``.
"""
from __future__ import annotations

import time

from homeassistant.core import Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started

from ..core import Feature, as_float, register

KEY = "temperature"


@register("climate")
class Temperature(Feature):
    feature_name = KEY
    schema = cv.entity_id
    provides = frozenset({"current_temperature"})

    _gw_temp_time: float | None = None  # time of the latest sample
    _gw_temp_prev_time: float | None = None  # time of the sample before it
    _gw_temp_updated = 0.0  # last successful update, for stall detection

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._gw_temp_updated = time.time()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.gw_conf(KEY)], self._temp_changed)
        )
        self.async_on_remove(async_at_started(self.hass, self._temp_started))

    @callback
    def _temp_started(self, _hass) -> None:
        self._temp_read()

    @callback
    def _temp_read(self) -> None:
        state = self.hass.states.get(self.gw_conf(KEY))
        value = as_float(state.state) if state is not None else None
        if value is not None:
            self._gw_current_temp = value
            self._gw_temp_updated = time.time()
            self.async_write_ha_state()

    @callback
    def _temp_changed(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._gw_temp_prev_time = self._gw_temp_time
        self._gw_temp_time = time.time()
        value = as_float(new_state.state)
        if value is not None:
            self._gw_current_temp = value
            self._gw_temp_updated = time.time()
        self.gw_temperature_changed()
        self.async_write_ha_state()
