"""`link` for lights: the connection to the original light, switch or group.

`link: light.x_lamp` (a light or a switch) or a list of entities (a group: on while
any member is on, commands go to all of them; members may be gungors lights).

- The wrapper mirrors the original's state, attributes and capabilities.
- Accepted requests are sent to the original (switches get on/off only).
- A change of the on state that we did not command is a physical request. A
  change within a few seconds after our own command is its echo; when the echo
  window ends without the device matching, the device's state is taken as
  physical too.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.light import ATTR_SUPPORTED_COLOR_MODES, ColorMode
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from ..core import Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "link"
ECHO_WINDOW = 5.0  # s after a command in which changes are its echo

# Modes that can't be listed together with colour modes.
_SIMPLE_MODES = {ColorMode.ONOFF, ColorMode.BRIGHTNESS}


@register("light")
class LightLink(Feature):
    feature_name = KEY
    schema = vol.Any(
        cv.entity_domain(["light", "switch"]),
        vol.All(cv.ensure_list, [cv.entity_domain(["light", "switch"])], vol.Length(min=2)),
    )
    last = True

    _link_available = False
    _link_sent_at = 0.0
    _link_echo_unsub = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return set(conf) if isinstance(conf, list) else {conf}

    @property
    def _link_entities(self) -> list[str]:
        conf = self.gw_conf(KEY)
        return list(conf) if isinstance(conf, list) else [conf]

    def _link_states(self) -> list[State]:
        states = [self.hass.states.get(e) for e in self._link_entities]
        return [s for s in states if s is not None and s.state != STATE_UNAVAILABLE]

    def _link_device(self) -> dict[str, Any] | None:
        """The original's state as request data: on + attributes + capabilities."""
        states = [s for s in self._link_states() if s.state != STATE_UNKNOWN]
        if not states:
            return None
        on = [s for s in states if s.state == STATE_ON]
        source = on[0] if on else states[0]
        data: dict[str, Any] = {"on": bool(on)}
        if source.domain == "light":
            data.update(source.attributes)
        modes: set[ColorMode] = set()
        features = 0
        effects: list[str] = []
        for state in states:
            if state.domain == "light":
                modes |= {ColorMode(m) for m in state.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or []}
                features |= int(state.attributes.get("supported_features") or 0)
                for effect in state.attributes.get("effect_list") or []:
                    if effect not in effects:
                        effects.append(effect)
            else:
                modes.add(ColorMode.ONOFF)
        if modes - _SIMPLE_MODES:
            modes -= _SIMPLE_MODES
        elif ColorMode.BRIGHTNESS in modes:
            modes = {ColorMode.BRIGHTNESS}
        data[ATTR_SUPPORTED_COLOR_MODES] = sorted(modes) or [ColorMode.ONOFF]
        data["supported_features"] = features
        data["effect_list"] = effects or None
        if source.domain != "light":
            data["color_mode"] = ColorMode.ONOFF
        return data

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._link_available = bool(self._link_states())
        self.async_on_remove(
            async_track_state_change_event(self.hass, self._link_entities, self._link_changed)
        )
        self.async_on_remove(self._link_cancel_echo)
        device = self._link_device()
        if device is not None:
            self._attr_is_on = device["on"]
            self.gw_mirror(device)

    async def async_will_remove_from_hass(self) -> None:
        self._link_cancel_echo()
        await super().async_will_remove_from_hass()

    @callback
    def _link_changed(self, event: Event) -> None:
        available = bool(self._link_states())
        if available != self._link_available:
            self._link_available = available
            self.gw_device_available(available)
        device = self._link_device()
        if device is None:
            self.async_write_ha_state()
            return
        self._link_take(device, echo_window_over=False)

    def _link_take(self, device: dict[str, Any], echo_window_over: bool) -> None:
        in_echo = not echo_window_over and time.monotonic() - self._link_sent_at < ECHO_WINDOW
        if device["on"] == self._attr_is_on or in_echo:
            on = self._attr_is_on
            self.gw_mirror(device)
            self._attr_is_on = on
            self.async_write_ha_state()
            return
        self.hass.async_create_task(self.gw_physical(device))

    @callback
    def _link_echo_over(self, _now) -> None:
        self._link_echo_unsub = None
        device = self._link_device()
        if device is not None:
            self._link_take(device, echo_window_over=True)

    def _link_cancel_echo(self) -> None:
        if self._link_echo_unsub is not None:
            self._link_echo_unsub()
            self._link_echo_unsub = None

    # --- chain ---------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        await super().gw_apply(request)
        if request.from_device or not self._link_available:
            return
        kwargs = {k: v for k, v in request.data.items() if k != "on"}
        await self._link_send(bool(request.data.get("on")), kwargs)

    async def gw_push(self) -> None:
        await super().gw_push()
        if self._attr_is_on is not None and self._link_available:
            await self._link_send(self._attr_is_on, {})

    async def _link_send(self, on: bool, kwargs: dict[str, Any]) -> None:
        self._link_sent_at = time.monotonic()
        self._link_cancel_echo()
        self._link_echo_unsub = async_call_later(self.hass, ECHO_WINDOW, self._link_echo_over)
        service = "turn_on" if on else "turn_off"
        lights = [e for e in self._link_entities if e.startswith("light.")]
        switches = [e for e in self._link_entities if not e.startswith("light.")]
        if lights:
            await self.hass.services.async_call(
                "light", service, {ATTR_ENTITY_ID: lights, **kwargs}, blocking=False
            )
        if switches:
            await self.hass.services.async_call(
                "switch", service, {ATTR_ENTITY_ID: switches}, blocking=False
            )

    # --- state ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._link_available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        if isinstance(self.gw_conf(KEY), list):
            attrs[ATTR_ENTITY_ID] = self._link_entities
        return attrs
