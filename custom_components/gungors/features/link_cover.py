"""`link` for covers: the connection to the original entity.

The original can be:
- a cover (Zigbee blind, curtain motor): moved with cover services,
- an input_number / number holding a position 0-100 (a cover without a device):
  moved by setting the number,
- a list of entities (a group): the position is their average, commands go to all
  of them. Members are usually other gungors wrappers.

The link is the only feature that writes to the original. Accepted requests are
sent (`gw_apply`); the wrapper's position changes only when the original reports.
Every new position goes up the chain (`gw_device_report`, where a movement feature
may take it); by default a report on the way to our last command (from where the
move started to its target) is its result and is taken as is, anything else is a
physical change (a feature that rejects it pushes it back: `gw_push` re-sends the
wrapper's position).
"""
from __future__ import annotations

import time
from typing import Any

import voluptuous as vol

from homeassistant.components.cover import ATTR_CURRENT_POSITION
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..const import ATTR_ACTUAL_POSITION, POSITION_TOLERANCE
from ..core import Feature, Request, as_float, register

KEY = "link"
MOVE_TIMEOUT = 60  # s without a report after which a move is no longer ours


def _position_of(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if state.domain == "cover":
        return as_float(state.attributes.get(ATTR_CURRENT_POSITION))
    return as_float(state.state)


@register("cover")
class CoverLink(Feature):
    feature_name = KEY
    schema = vol.Any(cv.entity_id, vol.All(cv.ensure_list, [cv.entity_id], vol.Length(min=2)))
    last = True

    _link_available = False
    _link_baseline_needed = True
    _link_move: tuple[float, float] | None = None  # (from, to) of our last command
    _link_move_seen = 0.0

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return set(conf) if isinstance(conf, list) else {conf}

    # --- helpers ---------------------------------------------------------

    @property
    def _link_entities(self) -> list[str]:
        conf = self.gw_conf(KEY)
        return list(conf) if isinstance(conf, list) else [conf]

    @property
    def _link_is_group(self) -> bool:
        return isinstance(self.gw_conf(KEY), list)

    def _link_device_position(self) -> float | None:
        """Device-frame position (group: the average of the known members)."""
        positions = [_position_of(self.hass.states.get(e)) for e in self._link_entities]
        known = [p for p in positions if p is not None]
        if not known:
            return None
        return sum(known) / len(known)

    def _link_position(self) -> float | None:
        """The original's reported position in the room frame."""
        position = self._link_device_position()
        return None if position is None else self.gw_from_device(position)

    @property
    def gw_actual_position(self) -> float | None:
        """Where the cover really is (room frame); a feature that knows better
        (an estimate) overrides it."""
        return self._link_position()

    def _link_available_now(self) -> bool:
        states = [self.hass.states.get(e) for e in self._link_entities]
        return any(s is not None and s.state != STATE_UNAVAILABLE for s in states)

    async def _link_send(self, service_domain: str, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            service_domain,
            service,
            {ATTR_ENTITY_ID: self._link_entities, **data},
            blocking=False,
        )

    async def _link_move_to(self, position: float) -> None:
        start = self.gw_actual_position
        if start is None:
            start = self._gw_position if self._gw_position is not None else position
        self._link_move = (float(start), float(position))
        self._link_move_seen = time.monotonic()
        device = self.gw_to_device(position)
        domain = self._link_entities[0].split(".", 1)[0]
        if domain == "cover":
            service, data = self.gw_device_command(device)
            await self._link_send("cover", service, data)
        else:
            await self._link_send(domain, "set_value", {"value": int(round(device))})

    # --- lifecycle -------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._link_available = self._link_available_now()
        self._link_baseline_needed = True
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._link_entities, self._link_state_changed
            )
        )
        position = self._link_position()
        if self._link_available and position is not None:
            self._link_baseline_needed = False
            self.gw_baseline(position)

    @callback
    def _link_state_changed(self, event: Event) -> None:
        available = self._link_available_now()
        if available != self._link_available:
            self._link_available = available
            self.gw_device_available(available)
            if available:
                self._link_baseline_needed = True
            self.async_write_ha_state()
            if not available:
                return
        position = self._link_position()
        if position is None:
            return
        if self._link_baseline_needed:
            self._link_baseline_needed = False
            self.gw_baseline(position)
            return
        self.gw_device_report(position)
        self.async_write_ha_state()

    @callback
    def gw_device_report(self, position: float | None) -> None:
        """Default: the result of our command is taken, anything else is physical."""
        if position is None:
            return
        if self._gw_position is not None and abs(position - self._gw_position) <= POSITION_TOLERANCE:
            return
        if self._link_ours(position):
            self.gw_mirror(position)
            return
        self._link_move = None
        self.hass.async_create_task(self.gw_physical({"position": int(round(position))}))

    def _link_ours(self, position: float) -> bool:
        move = self._link_move
        now = time.monotonic()
        if move is None or now - self._link_move_seen > MOVE_TIMEOUT:
            return False
        low, high = sorted(move)
        if not low - POSITION_TOLERANCE <= position <= high + POSITION_TOLERANCE:
            return False
        self._link_move_seen = now
        if abs(position - move[1]) <= POSITION_TOLERANCE:
            self._link_move = None  # arrived
        return True

    # --- chain -----------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        if request.from_device:
            await super().gw_apply(request)
            return
        # Not passed on to the base on purpose: the wrapper changes when the
        # original reports, not when it is commanded. Features above may have
        # changed what they show (moving, pending).
        await self._link_command(request)
        self.async_write_ha_state()

    async def _link_command(self, request: Request) -> None:
        if not self._link_available:
            return
        if request.data.get("stop"):
            if self._link_entities[0].startswith("cover."):
                await self._link_send("cover", "stop_cover", {})
            return
        position = request.data.get("position")
        if position is None:
            return
        actual = self.gw_actual_position
        moving = self.is_opening or self.is_closing
        if not moving and actual is not None and abs(actual - position) <= POSITION_TOLERANCE:
            return  # already there: the device would not move or report
        await self._link_move_to(position)

    async def gw_push(self) -> None:
        await super().gw_push()
        position = self._gw_position
        if position is not None and self._link_available:
            await self._link_move_to(position)

    # --- state -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._link_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        actual = self.gw_actual_position
        attrs[ATTR_ACTUAL_POSITION] = None if actual is None else int(round(actual))
        if self._link_is_group:
            # Lets templates (expand()) treat the wrapper as a group.
            attrs[ATTR_ENTITY_ID] = self._link_entities
        return attrs
