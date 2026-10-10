"""`window`: hold remote cover commands while a window is open.

`window: binary_sensor.x_window_contact` (on = open; unknown or unavailable counts
as open, fail safe).

- A remote request (UI, automation, voice) while the window is open is not sent:
  it is kept as the pending position and shown as the cover's position, with
  `pending_position` set. The window closing replays it.
- A physical request (a button, the blind moved by hand) is always allowed and
  drops the pending one.
- The window opening during a remote move stops the blind and keeps the target
  pending.
- A pending request survives a restart and an unavailable device: the first
  position after that replays it if the window is closed by then.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..const import ATTR_PENDING_POSITION, POSITION_TOLERANCE
from ..core import SOURCE_INTERNAL, SOURCE_PHYSICAL, Feature, Request, as_float, register

_LOGGER = logging.getLogger(__name__)

KEY = "window"


@register("cover")
class Window(Feature):
    feature_name = KEY
    schema = cv.entity_id

    _win_pending: int | None = None
    _win_remote_move = False  # the last applied move came from a remote request

    @property
    def _win_open(self) -> bool:
        state = self.hass.states.get(self.gw_conf(KEY))
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return True
        return state.state != "off"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.gw_conf(KEY)], self._win_changed)
        )

    # --- requests ------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        if request.source == SOURCE_PHYSICAL:
            self._win_pending = None  # a physical decision overrides a held command
            return await super().gw_request(request)
        if request.data.get("stop"):
            self._win_pending = None
            return await super().gw_request(request)
        position = request.data.get("position")
        if position is not None and request.source != SOURCE_INTERNAL and self._win_open:
            _LOGGER.debug("%s: window open, holding position %s", self.entity_id, position)
            self._win_pending = int(position)
            self.async_write_ha_state()
            return False
        if position is not None:
            self._win_pending = None
        return await super().gw_request(request)

    async def gw_apply(self, request: Request) -> None:
        if request.data.get("position") is not None:
            self._win_remote_move = request.source != SOURCE_PHYSICAL and not request.from_device
        await super().gw_apply(request)

    # --- window --------------------------------------------------------------

    @callback
    def _win_changed(self, event: Event) -> None:
        if event.data.get("new_state") is None or not self.available:
            return  # a held position stays held until the device is back
        if self._win_open:
            if self._win_remote_move and (self.is_opening or self.is_closing):
                # Opened mid-move: stop, keep the target for later.
                pending = self._gw_target
                self.hass.async_create_task(self._win_stop(pending))
            return
        if self._win_pending is not None:
            self.hass.async_create_task(self._win_replay())

    async def _win_stop(self, pending: int | None) -> None:
        await super().gw_request(Request({"stop": True}, source=SOURCE_INTERNAL))
        self._win_pending = pending
        self.async_write_ha_state()

    async def _win_replay(self) -> None:
        target, self._win_pending = self._win_pending, None
        if target is not None:
            await self.gw_request(Request({"position": target}, source=SOURCE_INTERNAL))

    @callback
    def gw_baseline(self, position: float) -> None:
        super().gw_baseline(position)
        pending = self._win_pending
        if pending is None:
            return
        if abs(pending - position) <= POSITION_TOLERANCE:
            self._win_pending = None
        elif not self._win_open:
            self.hass.async_create_task(self._win_replay())
        self.async_write_ha_state()

    # --- restore -------------------------------------------------------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        own = extra.get(KEY)
        if isinstance(own, dict):
            pending = as_float(own.get("pending"))
        elif extra.get("pending") is True or (
            last is not None and last.attributes.get("is_sync") is False
        ):
            # Legacy window_guard: an unsynced setpoint was a held command.
            pending = as_float(extra.get("setpoint"))
        else:
            pending = None
        self._win_pending = None if pending is None else int(pending)

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {"pending": self._win_pending}
        return data

    # --- state ---------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        if self._win_pending is not None:
            return self._win_pending
        return super().current_cover_position

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PENDING_POSITION] = self._win_pending
        return attrs
