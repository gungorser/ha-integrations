"""`link` for lights: the connection to the original light, switch or group.

`link: light.x_lamp` (a light or a switch) or a list of entities (a group: on while
any member is on, commands go to all of them; members may be gungors lights).

- The wrapper is a puppet of the original: its on state, attributes and
  capabilities are always the original's. It keeps no state of its own.
- Accepted requests are sent to the original (switches get on/off only); the
  wrapper changes only when the original reports.
- A change of the on state that we did not command is a physical request (it
  goes through the features, e.g. a program rule brings it back). A report
  within a few seconds after our own command is its result and is taken as is.

Blink (`gungors.blink`, feedback such as a pushbutton's): the original is flashed
`count` times in a colour and then put back as it was just before (off stays
off). It goes straight to the original, below every feature: it is not a request,
so no program rule limits it and `occupancy` does not take it as the user's. A
real request during a blink ends the blink and wins.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.light import ATTR_SUPPORTED_COLOR_MODES, ColorMode
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..core import Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "link"
ECHO_WINDOW = 5.0  # s after a command in which reports are its result
BLINK_ON = 0.5  # s a blink stays lit
BLINK_OFF = 0.4  # s between two blinks
BLINK_SETTLE = 2.0  # s after the last blink step before the rules judge the light again
# Attributes a blink puts back (the colour by the colour mode the light was in).
_RESTORE_COLOR = {
    "color_temp": "color_temp_kelvin",
    "hs": "hs_color",
    "xy": "xy_color",
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
}
# Not sent on: "on" is the service, and HA hands entities the deprecated mired
# "color_temp" next to "color_temp_kelvin", which light.turn_on rejects together.
_NOT_SENT = {"on", "color_temp"}

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
    _link_blink: asyncio.Task | None = None
    _link_blink_before: list[State] | None = None
    gw_blinking = False

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
        # A group shows the first lit member in colour (a white-only member first
        # in the list would hide the others' colour), else the first lit one.
        coloured = [s for s in on if s.attributes.get("color_mode") not in (None, *_SIMPLE_MODES)]
        source = (coloured or on or states)[0]
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
        device = self._link_device()
        if device is not None:
            self._attr_is_on = device["on"]
            self.gw_mirror(device)

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
        in_echo = time.monotonic() - self._link_sent_at < ECHO_WINDOW
        if device["on"] == self._attr_is_on or in_echo:
            self._attr_is_on = device["on"]
            self.gw_mirror(device)
            self.async_write_ha_state()
            return
        self.hass.async_create_task(self.gw_physical(device))

    # --- chain ---------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        if request.from_device:
            await super().gw_apply(request)
            return
        # Not passed on to the base on purpose: the wrapper changes when the
        # original reports, not when it is commanded.
        self._link_stop_blink()
        if not self._link_available:
            return
        kwargs = {k: v for k, v in request.data.items() if k not in _NOT_SENT}
        await self._link_send(bool(request.data.get("on")), kwargs)

    # --- blink ----------------------------------------------------------------

    async def gw_blink(self, rgb_color: list[int] | tuple[int, int, int] | None = None,
                       count: int = 1) -> None:
        """Flash the original `count` times, then put it back as it was."""
        if not self._link_available:
            return
        running = self._link_blink is not None and not self._link_blink.done()
        # A blink started during another keeps that one's "before": the light now
        # shows the blink colour, which must not become the state to put back.
        before = self._link_blink_before if running else None
        self._link_stop_blink()
        if before is None:
            before = [s for s in self._link_states() if s.state != STATE_UNKNOWN]
        self._link_blink_before = before
        self.gw_blinking = True
        self._link_blink = self.hass.async_create_background_task(
            self._link_blink_run(before, tuple(rgb_color) if rgb_color else None, count),
            f"gungors blink {self.entity_id}",
        )

    def _link_stop_blink(self) -> None:
        if self._link_blink is not None and not self._link_blink.done():
            self._link_blink.cancel()
        self._link_blink = None
        self.gw_blinking = False

    async def _link_blink_run(self, before: list[State], rgb: tuple[int, int, int] | None,
                              count: int) -> None:
        try:
            for index in range(count):
                if index or any(s.state == STATE_ON for s in before):
                    await self._link_send(False, {"transition": 0})
                    await asyncio.sleep(BLINK_OFF)
                lit: dict[str, Any] = {"transition": 0, "brightness": 255}
                if rgb is not None:
                    lit["rgb_color"] = rgb
                await self._link_blink_on(lit, before)
                await asyncio.sleep(BLINK_ON)
            await self._link_restore(before)
            await asyncio.sleep(BLINK_SETTLE)
        except asyncio.CancelledError:
            return
        finally:
            if asyncio.current_task() is self._link_blink:
                self._link_blink = None
                self.gw_blinking = False

    async def _link_blink_on(self, data: dict[str, Any], before: list[State]) -> None:
        """Every member lit; colour and brightness only for lights that take them."""
        self._link_sent_at = time.monotonic()
        found = {s.entity_id: s for s in before}
        for entity_id in self._link_entities:
            state = found.get(entity_id) or self.hass.states.get(entity_id)
            if entity_id.startswith("switch."):
                await self.hass.services.async_call(
                    "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=False
                )
                continue
            modes = set((state.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or []) if state else [])
            kwargs = {"transition": 0}
            if modes - {ColorMode.ONOFF}:
                kwargs["brightness"] = data["brightness"]
            if "rgb_color" in data and modes & {"hs", "xy", "rgb", "rgbw", "rgbww"}:
                kwargs["rgb_color"] = data["rgb_color"]
            await self.hass.services.async_call(
                "light", "turn_on", {ATTR_ENTITY_ID: entity_id, **kwargs}, blocking=False
            )

    async def _link_restore(self, before: list[State]) -> None:
        """Put each member back as it was before the blink."""
        self._link_sent_at = time.monotonic()
        for state in before:
            domain = state.domain
            if state.state != STATE_ON:
                data: dict[str, Any] = {ATTR_ENTITY_ID: state.entity_id}
                if domain == "light":
                    data["transition"] = 0
                await self.hass.services.async_call(domain, "turn_off", data, blocking=False)
                continue
            data = {ATTR_ENTITY_ID: state.entity_id}
            if domain == "light":
                data["transition"] = 0
                if state.attributes.get("brightness") is not None:
                    data["brightness"] = state.attributes["brightness"]
                key = _RESTORE_COLOR.get(str(state.attributes.get("color_mode")))
                if key and state.attributes.get(key) is not None:
                    data[key] = state.attributes[key]
            await self.hass.services.async_call(domain, "turn_on", data, blocking=False)

    async def async_will_remove_from_hass(self) -> None:
        self._link_stop_blink()
        await super().async_will_remove_from_hass()

    async def gw_push(self) -> None:
        await super().gw_push()
        if self._attr_is_on is not None and self._link_available:
            await self._link_send(self._attr_is_on, {})

    async def _link_send(self, on: bool, kwargs: dict[str, Any]) -> None:
        self._link_sent_at = time.monotonic()
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
