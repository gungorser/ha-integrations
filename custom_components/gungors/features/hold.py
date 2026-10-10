"""`hold`: set a state for a while and (optionally) enforce it.

Generic over domains: the held state is request data of the entity's domain
(cover `{position: 0}`, climate `{hvac_mode: heat, temperature: 21}`, light
`{on: true, brightness: 128}`).

Services (target any gungors entity with this feature):

- `gungors.hold`: `state`, `duration` or `until` (none: until released), `mode`:
  - `strict`: every request that conflicts with the held state is rejected; a
    physical change is pushed back to the held state.
  - `manual`: the state is applied at the start; later changes are allowed.
- `gungors.release`: end the hold now.

A hold survives a restart. Put `hold` first in the feature list so it sees every
request before the others.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from ..const import ATTR_HOLD, HOLD_MANUAL, HOLD_STRICT
from ..core import SOURCE_INTERNAL, Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "hold"
TAG = "hold"

HOLD_SCHEMA = {
    vol.Required("state"): dict,
    vol.Exclusive("duration", "end"): cv.positive_time_period,
    vol.Exclusive("until", "end"): cv.datetime,
    vol.Optional("mode", default=HOLD_STRICT): vol.In([HOLD_STRICT, HOLD_MANUAL]),
}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 0.05
    return a == b


@register("cover", "climate", "light")
class Hold(Feature):
    feature_name = KEY
    services = {
        "hold": (HOLD_SCHEMA, "async_gw_hold"),
        "release": ({}, "async_gw_release"),
    }

    _hold: dict[str, Any] | None = None  # {state, mode, until (iso or None)}
    _hold_unsub = None

    # --- services ------------------------------------------------------------

    async def async_gw_hold(
        self,
        state: dict[str, Any],
        mode: str = HOLD_STRICT,
        duration: timedelta | None = None,
        until: datetime | None = None,
    ) -> None:
        if duration is not None:
            end: datetime | None = dt_util.utcnow() + duration
        elif until is not None:
            end = dt_util.as_utc(until if until.tzinfo else until.replace(
                tzinfo=dt_util.get_default_time_zone()))
        else:
            end = None
        self._hold_cancel_timer()
        self._hold = {"state": dict(state), "mode": mode, "until": end.isoformat() if end else None}
        self._hold_arm(end)
        await self.gw_request(Request(dict(state), source=SOURCE_INTERNAL, tags={TAG}))
        self.async_write_ha_state()

    async def async_gw_release(self) -> None:
        self._hold_cancel_timer()
        self._hold = None
        self.async_write_ha_state()

    # --- chain ---------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        hold = self._hold
        if hold is not None and hold["mode"] == HOLD_STRICT and TAG not in request.tags:
            if self._hold_conflicts(hold["state"], request.data):
                _LOGGER.info(
                    "%s: held (%s), rejecting %s from %s",
                    self.entity_id, hold["state"], request.data, request.source,
                )
                return False
        return await super().gw_request(request)

    @staticmethod
    def _hold_conflicts(held: dict[str, Any], data: dict[str, Any]) -> bool:
        if data.get("stop"):
            return True  # a stop would leave the held position
        if "on" in held and "on" in data and bool(data["on"]) != bool(held["on"]):
            return True
        return any(
            key in data and not _same(data[key], value)
            for key, value in held.items()
            if key != "on"
        )

    # --- timer ---------------------------------------------------------------

    def _hold_arm(self, end: datetime | None) -> None:
        if end is None:
            return
        self._hold_unsub = async_track_point_in_utc_time(self.hass, self._hold_expired, end)

    @callback
    def _hold_expired(self, _now) -> None:
        self._hold_unsub = None
        self._hold = None
        self.async_write_ha_state()

    def _hold_cancel_timer(self) -> None:
        if self._hold_unsub is not None:
            self._hold_unsub()
            self._hold_unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._hold is not None:
            until = self._hold.get("until")
            end = dt_util.parse_datetime(until) if until else None
            if end is not None and end <= dt_util.utcnow():
                self._hold = None
            else:
                self._hold_arm(end)

    async def async_will_remove_from_hass(self) -> None:
        self._hold_cancel_timer()
        await super().async_will_remove_from_hass()

    # --- restore -------------------------------------------------------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        own = extra.get(KEY)
        self._hold = own if isinstance(own, dict) and isinstance(own.get("state"), dict) else None

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = self._hold
        return data

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_HOLD] = self._hold
        return attrs
