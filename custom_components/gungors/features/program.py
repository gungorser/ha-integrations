"""`program`: follow the active entries of `calendar.program` (see rules.py).

`- program` (first in the feature list), or `- program: {<attr>: <default>}` to
override the domain defaults of the program calendar for this entity.

While programs are active the entity follows their rules:

- A request that breaks a rule is clamped (brightness 80 with `max 40` -> 40;
  turning off a light whose program says `on: true` keeps it on).
- When a rule starts, or the device changes by itself (a wall dimmer, the
  thermostat dial), the state is corrected to the nearest allowed value.
- A rule only shapes what applies in the current state: `color: red` recolours a
  lit light and doesn't switch it on; a temperature rule acts while heating.
- When a program ends, its `eq` attributes go back to their default (light `on`
  -> off, `color` -> white, `hvac_mode` -> off, cover `position: sun` -> open by
  day, closed at night); `min`/`max` rules just lift. Attributes without a
  default keep their value.
- Covers whose position no program holds follow the `position` default at
  sunrise and sunset. Defaults are applied at those moments only; they never
  lock anything.

Attribute `program`: the active programs of this entity (summary, start, end,
rules). The attributes that an `eq` rule enforces survive a restart, so a program
that ended while Home Assistant was down still restores the defaults.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.components.light import ColorMode
from homeassistant.core import Event, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import color as color_util

from ..core import Feature, Request, register
from ..rules import DOMAINS, Constraint, parse_defaults

_LOGGER = logging.getLogger(__name__)

KEY = "program"
TAG = "program"
ATTR_PROGRAM = "program"
# s after a correction before the same entity is corrected again (the device
# needs time to report the result of the first one).
COOLDOWN = 10.0
# The same correction is sent at most this often in a row.
MAX_REPEATS = 3

_COLOR_MODES = {ColorMode.HS, ColorMode.XY, ColorMode.RGB, ColorMode.RGBW, ColorMode.RGBWW}
_COLOR_KEYS = (
    "hs_color", "xy_color", "rgb_color", "rgbw_color", "rgbww_color",
    "color_temp_kelvin", "color_temp", "color_name", "white", "effect",
)


def _merge(rule_sets: list[dict[str, Constraint]]) -> dict[str, Constraint]:
    """The combined constraint per attribute of non-conflicting rule sets."""
    merged: dict[str, Constraint] = {}
    for rules in rule_sets:
        for attr, constraint in rules.items():
            current = merged.get(attr)
            if current is None or (current.eq is None and constraint.eq is not None):
                merged[attr] = constraint
            elif current.eq is None:
                lows = [v for v in (current.min, constraint.min) if v is not None]
                highs = [v for v in (current.max, constraint.max) if v is not None]
                merged[attr] = Constraint(
                    min=max(lows) if lows else None, max=min(highs) if highs else None
                )
    return merged


def _finite(constraint: Constraint, fallback: float | None) -> float | None:
    """A value inside the constraint, near `fallback` when given."""
    low, high = constraint.bounds()
    if fallback is not None:
        return constraint.clamp(fallback)
    if high != float("inf"):
        return high
    if low != float("-inf"):
        return low
    return None


def _own_defaults(value: Any) -> dict[str, Any]:
    """`- program: {<attr>: <default>}`: this entity's own defaults."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise vol.Invalid("program: a mapping of attribute: default value")
    result: dict[str, Any] = {}
    for attr, default in value.items():
        attr = "on" if attr is True else attr
        if attr not in DOMAINS:
            raise vol.Invalid(f"program: unknown attribute '{attr}'")
        result.update(parse_defaults(DOMAINS[attr], {attr: default}))
    return result


@register("cover", "climate", "light")
class Program(Feature):
    feature_name = KEY
    schema = _own_defaults

    # [{uid, summary, start, end, rules: {attr: Constraint}}] for this entity
    _prog_active: list[dict[str, Any]] = []
    _prog_eq: dict[str, Any] = {}  # attributes an eq rule enforces (for the defaults)
    _prog_sent_at = -COOLDOWN
    _prog_recheck = None
    _prog_last: dict[str, Any] | None = None  # the last correction sent
    _prog_repeats = 0  # how often in a row it was sent

    @property
    def _prog_domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    # --- manager interface ---------------------------------------------------

    def gw_program_defaults(self, domain_defaults: dict[str, Any]) -> dict[str, Any]:
        own = self.gw_conf(KEY) or {}
        domain = self._prog_domain
        return {**domain_defaults, **{a: v for a, v in own.items() if DOMAINS[a] == domain}}

    async def gw_program_set(self, active: list[dict[str, Any]], defaults: dict[str, Any]) -> None:
        """The manager's new set of active programs for this entity."""
        self._prog_active = active
        self._prog_last, self._prog_repeats = None, 0
        rules = self._prog_rules()
        new_eq = {a: c.eq for a, c in rules.items() if c.eq is not None}
        ended = [a for a in self._prog_eq if a not in new_eq]
        self._prog_eq = new_eq
        self.async_write_ha_state()
        if ended:
            await self.gw_program_apply_defaults(ended, defaults)
        await self._prog_enforce(force=True)

    async def gw_program_apply_defaults(self, attrs: list[str], defaults: dict[str, Any]) -> None:
        """Apply the defaults of attributes no program holds any more."""
        values = self.gw_program_defaults(defaults)
        wanted = {a: values[a] for a in attrs if a in values and a not in self._prog_eq}
        data = self._prog_default_request(wanted)
        if data:
            _LOGGER.debug("%s: program defaults %s", self.entity_id, data)
            await self.gw_request(Request(data, tags={TAG}))

    # --- chain ---------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        if request.from_device or not self._prog_active:
            accepted = await super().gw_request(request)
            if request.from_device and self._prog_active:
                self.hass.async_create_task(self._prog_enforce())
            return accepted
        data = self._prog_clamp(dict(request.data))
        if data is None:
            _LOGGER.info("%s: %s rejected by a program", self.entity_id, request.data)
            return False
        if data != request.data:
            _LOGGER.debug("%s: program clamped %s to %s", self.entity_id, request.data, data)
        request.data = data
        return await super().gw_request(request)

    # --- enforcement ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.entity_id], self._prog_state_changed)
        )
        from ..program import async_get_manager  # noqa: PLC0415

        self.async_on_remove(async_get_manager(self.hass).register(self))

    async def async_will_remove_from_hass(self) -> None:
        self._prog_cancel_recheck()
        await super().async_will_remove_from_hass()

    @callback
    def _prog_state_changed(self, event: Event) -> None:
        if self._prog_active:
            self.hass.async_create_task(self._prog_enforce())

    async def _prog_enforce(self, force: bool = False) -> None:
        if not self._prog_active or not self.available:
            return
        if not force and time.monotonic() - self._prog_sent_at < COOLDOWN:
            self._prog_schedule_recheck()
            return
        data = self._prog_correction()
        if not data:
            self._prog_last, self._prog_repeats = None, 0
            return
        if data == self._prog_last:
            self._prog_repeats += 1
            if self._prog_repeats >= MAX_REPEATS:
                if self._prog_repeats == MAX_REPEATS:
                    _LOGGER.warning(
                        "%s: the device does not follow the program (%s); stopped until the "
                        "programs change or the device complies", self.entity_id, data,
                    )
                return
        else:
            self._prog_last, self._prog_repeats = data, 0
        self._prog_sent_at = time.monotonic()
        _LOGGER.debug("%s: program correction %s", self.entity_id, data)
        await self.gw_request(Request(data, tags={TAG}))
        self._prog_schedule_recheck()

    def _prog_schedule_recheck(self) -> None:
        if self._prog_recheck is not None:
            return

        @callback
        def recheck(_now) -> None:
            self._prog_recheck = None
            self.hass.async_create_task(self._prog_enforce())

        self._prog_recheck = async_call_later(self.hass, COOLDOWN, recheck)

    def _prog_cancel_recheck(self) -> None:
        if self._prog_recheck is not None:
            self._prog_recheck()
            self._prog_recheck = None

    # --- rules per domain ----------------------------------------------------

    def _prog_rules(self) -> dict[str, Constraint]:
        """Combined constraints the entity supports."""
        rules = _merge([p["rules"] for p in self._prog_active])
        domain = self._prog_domain
        return {
            a: c for a, c in rules.items() if DOMAINS[a] == domain and self._prog_supports(a)
        }

    def _prog_supports(self, attr: str) -> bool:
        if self._prog_domain != "light" or attr == "on":
            return True
        modes = set(self.supported_color_modes or ())
        if attr == "brightness":
            return bool(modes - {ColorMode.ONOFF})
        if attr == "color":
            return bool(modes & _COLOR_MODES)
        if attr == "color_temp":
            return ColorMode.COLOR_TEMP in modes
        return True

    def _prog_clamp(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Request data made to follow the rules; None: reject it."""
        rules = self._prog_rules()
        domain = self._prog_domain
        if domain == "light":
            return self._prog_clamp_light(data, rules)
        if domain == "climate":
            return self._prog_clamp_climate(data, rules)
        return self._prog_clamp_cover(data, rules)

    def _prog_correction(self) -> dict[str, Any] | None:
        domain = self._prog_domain
        if domain == "light":
            return self._prog_correct_light()
        if domain == "climate":
            return self._prog_correct_climate()
        return self._prog_correct_cover()

    def _prog_default_request(self, wanted: dict[str, Any]) -> dict[str, Any] | None:
        if not wanted:
            return None
        domain = self._prog_domain
        if domain == "light":
            on = self.is_on
            if "on" in wanted and wanted["on"] != on:
                return {"on": wanted["on"]} if not wanted["on"] else self._prog_clamp(
                    {"on": True, **self._prog_light_values(wanted)}
                )
            if not on:
                return None
            values = self._prog_light_values(wanted)
            return {"on": True, **values} if values else None
        if domain == "climate":
            data: dict[str, Any] = {}
            if "hvac_mode" in wanted and wanted["hvac_mode"] != self.hvac_mode:
                data["hvac_mode"] = HVACMode(wanted["hvac_mode"])
            mode = data.get("hvac_mode", self.hvac_mode)
            if "temperature" in wanted and mode == HVACMode.HEAT:
                data["temperature"] = float(wanted["temperature"])
            return data or None
        if "position" in wanted:
            position = wanted["position"]
            if position == "sun":
                sun = self.hass.states.get("sun.sun")
                if sun is None:
                    return None
                position = 100 if sun.state == "above_horizon" else 0
            if self.current_cover_position != position:
                return {"position": int(position)}
        return None

    def _prog_light_values(self, wanted: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if "brightness" in wanted and self._prog_supports("brightness"):
            values["brightness"] = round(float(wanted["brightness"]) * 255 / 100)
        if "color" in wanted and self._prog_supports("color"):
            values["rgb_color"] = tuple(wanted["color"])
        elif "color_temp" in wanted and self._prog_supports("color_temp"):
            values["color_temp_kelvin"] = int(wanted["color_temp"])
        return values

    # light

    def _prog_brightness_pct(self) -> float | None:
        if not self.is_on or self.brightness is None:
            return None
        return self.brightness * 100 / 255

    def _prog_color_matches(self, rgb: tuple[int, int, int]) -> bool:
        if self.color_mode not in _COLOR_MODES or self.hs_color is None:
            return False
        hue, sat = color_util.color_RGB_to_hs(*rgb)
        cur_hue, cur_sat = self.hs_color
        if sat < 10:
            return cur_sat < 12
        diff = abs(hue - cur_hue) % 360
        return min(diff, 360 - diff) <= 8 and abs(sat - cur_sat) <= 12

    def _prog_clamp_light(
        self, data: dict[str, Any], rules: dict[str, Constraint]
    ) -> dict[str, Any] | None:
        if "on" in rules:
            data["on"] = rules["on"].eq
        if not data.get("on"):
            return {"on": False} if "on" in data else data
        if "brightness" in rules:
            given = data.get("brightness")
            current = given * 100 / 255 if given is not None else self._prog_brightness_pct()
            value = _finite(rules["brightness"], current)
            if value is not None and (given is not None or current is None
                                      or not rules["brightness"].holds(current)):
                data["brightness"] = max(1, round(value * 255 / 100))
                data.pop("brightness_step", None)
        if "color" in rules:
            for key in _COLOR_KEYS:
                data.pop(key, None)
            data["rgb_color"] = tuple(rules["color"].eq)
        elif "color_temp" in rules:
            for key in _COLOR_KEYS:
                if key != "color_temp_kelvin":
                    data.pop(key, None)
            given = data.get("color_temp_kelvin")
            current = given
            if current is None and self.is_on and self.color_mode == ColorMode.COLOR_TEMP:
                current = self.color_temp_kelvin
            value = _finite(rules["color_temp"], current)
            if value is not None and (current is None or not rules["color_temp"].holds(current)
                                      or given is not None):
                data["color_temp_kelvin"] = int(round(value))
        return data

    def _prog_correct_light(self) -> dict[str, Any] | None:
        rules = self._prog_rules()
        if "on" in rules and self.is_on != rules["on"].eq:
            return self._prog_clamp_light({"on": rules["on"].eq}, rules)
        if not self.is_on:
            return None
        wrong = False
        if "brightness" in rules:
            current = self._prog_brightness_pct()
            wrong |= current is not None and not rules["brightness"].holds(current)
        if "color" in rules:
            wrong |= not self._prog_color_matches(rules["color"].eq)
        elif "color_temp" in rules:
            wrong |= (self.color_mode != ColorMode.COLOR_TEMP
                      or not rules["color_temp"].holds(self.color_temp_kelvin))
        return self._prog_clamp_light({"on": True}, rules) if wrong else None

    # climate

    def _prog_clamp_climate(
        self, data: dict[str, Any], rules: dict[str, Constraint]
    ) -> dict[str, Any] | None:
        if "hvac_mode" in rules and ("hvac_mode" in data or "temperature" in data):
            data["hvac_mode"] = HVACMode(rules["hvac_mode"].eq)
        mode = data.get("hvac_mode", self.hvac_mode)
        if mode == HVACMode.OFF:
            data.pop("temperature", None)
            return data or None
        if "temperature" in rules:
            given = data.get("temperature")
            current = given if given is not None else self.target_temperature
            if given is not None or (current is not None and not rules["temperature"].holds(current)):
                value = _finite(rules["temperature"], current)
                if value is not None:
                    data["temperature"] = value
        return data or None

    def _prog_correct_climate(self) -> dict[str, Any] | None:
        rules = self._prog_rules()
        data: dict[str, Any] = {}
        if "hvac_mode" in rules and self.hvac_mode != rules["hvac_mode"].eq:
            data["hvac_mode"] = HVACMode(rules["hvac_mode"].eq)
        mode = data.get("hvac_mode", self.hvac_mode)
        if mode == HVACMode.HEAT and "temperature" in rules:
            current = self.target_temperature
            if current is None or not rules["temperature"].holds(current):
                value = _finite(rules["temperature"], current)
                if value is not None:
                    data["temperature"] = value
        return data or None

    # cover

    def _prog_clamp_cover(
        self, data: dict[str, Any], rules: dict[str, Constraint]
    ) -> dict[str, Any] | None:
        if "position" not in rules:
            return data
        if data.get("stop"):
            return None  # a stop could leave the allowed range
        if data.get("position") is not None:
            data["position"] = int(round(rules["position"].clamp(float(data["position"]))))
        return data

    def _prog_correct_cover(self) -> dict[str, Any] | None:
        rules = self._prog_rules()
        position = self.current_cover_position
        if "position" not in rules or position is None:
            return None
        if self.is_opening or self.is_closing or rules["position"].holds(position):
            return None
        return {"position": int(round(rules["position"].clamp(float(position))))}

    # --- restore / state -----------------------------------------------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        own = extra.get(KEY)
        eq = own.get("eq") if isinstance(own, dict) else None
        self._prog_eq = dict(eq) if isinstance(eq, dict) else {}
        self._prog_active = []

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {"eq": {a: list(v) if isinstance(v, tuple) else v
                            for a, v in self._prog_eq.items()}}
        return data

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PROGRAM] = [
            {
                "uid": p["uid"],
                "summary": p["summary"],
                "start": p["start"],
                "end": p["end"],
                "rules": {a: c.as_dict() for a, c in p["rules"].items()},
            }
            for p in self._prog_active
        ]
        return attrs
