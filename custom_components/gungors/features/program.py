"""`program`: follow the active entries of `calendar.program` (see rules.py).

`- program` (first in the feature list), or `- program: {<attr>: <default>}` to
override the domain defaults of the program calendar for this entity.

While programs are active the entity follows their rules:

- A request a user makes (a dashboard, the app, a script they run) that breaks a
  rule is refused: nothing changes and the user's browsers show a browser_mod
  popup saying it is not allowed, with the nearest allowed value to apply when
  there is one (brightness 49 with `min 50` -> 50).
- Other requests (automations, other features) and the device changing by itself
  (a wall switch or dimmer, the thermostat dial) are brought to the nearest
  allowed value without asking; so is the state when a rule starts.
- A `let` rule only shapes what applies in the current state: an off light or
  heating satisfies it, `color: red` recolours a lit light. A `set` rule also
  brings the device into it: a set brightness, colour or colour temperature rule
  keeps the light on, a set temperature rule keeps the heating on.
- When a `set` rule ends, the device returns to its defaults, except what other
  set rules still hold (light off and white, heating off, cover `position: sun`:
  open by day, closed at night). `let` rules just lift.
- Covers whose position no program holds follow the `position` default at
  sunrise and sunset. Defaults are applied at those moments only; they never
  lock anything.

Attribute `program`: the active programs of this entity (summary, start, end,
rules). The attributes `set` rules hold survive a restart, so a program that
ended while Home Assistant was down still restores the defaults.
"""
from __future__ import annotations

from dataclasses import replace
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.components.light import ColorMode
from homeassistant.core import Event, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import color as color_util

from ..core import SOURCE_REMOTE, Feature, Request, register
from ..rules import DOMAINS, Constraint, implied, parse_defaults

_LOGGER = logging.getLogger(__name__)

KEY = "program"
TAG = "program"
ATTR_PROGRAM = "program"
# s after a correction before the same entity is corrected again (the device
# needs time to report the result of the first one).
COOLDOWN = 10.0
# The same correction is sent at most this often in a row.
MAX_REPEATS = 3
# s a service call's context counts as the request's origin
CONTEXT_RECENT = 5.0
LABELS = {
    "on": "açık/kapalı", "brightness": "parlaklık", "color": "renk",
    "color_temp": "renk sıcaklığı", "hvac_mode": "ısıtma", "temperature": "sıcaklık",
    "position": "konum",
}

_COLOR_MODES = {ColorMode.HS, ColorMode.XY, ColorMode.RGB, ColorMode.RGBW, ColorMode.RGBWW}
_COLOR_KEYS = (
    "hs_color", "xy_color", "rgb_color", "rgbw_color", "rgbww_color",
    "color_temp_kelvin", "color_temp", "color_name", "white", "effect",
)


def _merge(rule_sets: list[dict[str, Constraint]]) -> dict[str, Constraint]:
    """The combined constraint per attribute of non-conflicting rule sets."""
    merged: dict[str, Constraint] = {}
    for rules in rule_sets:
        for attr, constraint in implied(rules).items():
            current = merged.get(attr)
            flag = constraint.set or (current is not None and current.set)
            if current is None or (current.eq is None and constraint.eq is not None):
                merged[attr] = replace(constraint, set=flag)
            elif current.eq is None:
                lows = [v for v in (current.min, constraint.min) if v is not None]
                highs = [v for v in (current.max, constraint.max) if v is not None]
                merged[attr] = Constraint(
                    min=max(lows) if lows else None,
                    max=min(highs) if highs else None,
                    set=flag,
                )
            elif flag != current.set:
                merged[attr] = replace(current, set=flag)
    return merged


def _hs_close(wanted: tuple[float, float], current: tuple[float, float]) -> bool:
    hue, sat = wanted
    cur_hue, cur_sat = current
    if sat < 10:
        return cur_sat < 12
    diff = abs(hue - cur_hue) % 360
    return min(diff, 360 - diff) <= 8 and abs(sat - cur_sat) <= 12


def _rgb_close(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return _hs_close(color_util.color_RGB_to_hs(*b), color_util.color_RGB_to_hs(*a))


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
    _prog_set: set[str] = set()  # attributes set rules hold (for the defaults)
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
        held = {a for a, c in rules.items() if c.set}
        ended = [a for a in self._prog_set if a not in held]
        self._prog_set = held
        self.async_write_ha_state()
        if ended:
            # The device returns to its defaults (a set brightness ending: light off).
            await self.gw_program_apply_defaults(list(DOMAINS), defaults)
        await self._prog_enforce(force=True)

    async def gw_program_apply_defaults(self, attrs: list[str], defaults: dict[str, Any]) -> None:
        """Apply the defaults of attributes no program holds any more."""
        values = self.gw_program_defaults(defaults)
        wanted = {a: v for a, v in values.items() if a in attrs and a not in self._prog_set}
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
        user = self._prog_user(request)
        if user is not None:
            refused = self._prog_refusal(request.data)
            if refused is not None:
                _LOGGER.info("%s: %s refused by a program", self.entity_id, request.data)
                self.hass.async_create_task(self._prog_notify(user, *refused))
                return False
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

    # --- refusing user requests -----------------------------------------------

    def _prog_user(self, request: Request) -> str | None:
        """The user behind a request from a service call, None for anything else."""
        if request.source != SOURCE_REMOTE or request.tags or request.from_device:
            return None
        context, since = self._context, self._context_set
        if context is None or since is None or time.time() - since > CONTEXT_RECENT:
            return None
        return context.user_id

    def _prog_refusal(self, data: dict[str, Any]) -> tuple[str, dict[str, Any] | None] | None:
        """(what breaks a rule, the service data of a suggestion or None), or None."""
        rules = self._prog_rules()
        domain = self._prog_domain

        def number(attr: str, value: float, unit: str, step: float) -> tuple[str, Any] | None:
            if attr not in rules:
                return None
            allowed = rules[attr].clamp(value)
            if abs(allowed - value) <= step:
                return None
            return f"{LABELS[attr]} {value:g}{unit}", allowed

        if domain == "light":
            on = data.get("on")
            if "on" in rules and on is not None and on != rules["on"].eq:
                return LABELS["on"], None
            if not on:
                return None
            if "brightness" in data and data["brightness"] is not None:
                pct = data["brightness"] * 100 / 255
                hit = number("brightness", round(pct), "%", 0.5)
                if hit:
                    return hit[0], {"brightness_pct": int(round(hit[1]))}
            given = self._prog_given_rgb(data)
            if "color" in rules and given is not None and not _rgb_close(given, rules["color"].eq):
                return LABELS["color"], {"rgb_color": list(rules["color"].eq)}
            if "color_temp" in rules:
                kelvin = data.get("color_temp_kelvin")
                wanted = rules["color_temp"].eq
                if given is not None or (kelvin is not None and abs(kelvin - wanted) > 50):
                    return LABELS["color_temp"], {"color_temp_kelvin": int(wanted)}
            return None
        if domain == "climate":
            mode = data.get("hvac_mode")
            if "hvac_mode" in rules and mode is not None and mode != rules["hvac_mode"].eq:
                return LABELS["hvac_mode"], None
            if data.get("temperature") is not None and mode != HVACMode.OFF:
                hit = number("temperature", float(data["temperature"]), "°", 0.05)
                if hit:
                    return hit[0], {"temperature": hit[1]}
            return None
        if "position" not in rules:
            return None
        if data.get("stop"):
            return "durdurma", None
        if data.get("position") is not None:
            hit = number("position", float(data["position"]), "%", 0.5)
            if hit:
                return hit[0], {"position": int(round(hit[1]))}
        return None

    @staticmethod
    def _prog_given_rgb(data: dict[str, Any]) -> tuple[int, int, int] | None:
        if data.get("rgb_color") is not None:
            return tuple(int(v) for v in data["rgb_color"][:3])  # type: ignore[return-value]
        if data.get("hs_color") is not None:
            return color_util.color_hs_to_RGB(*data["hs_color"])
        if data.get("xy_color") is not None:
            return color_util.color_xy_to_RGB(*data["xy_color"])
        if data.get("color_name") is not None:
            try:
                return tuple(color_util.color_name_to_rgb(data["color_name"]))  # type: ignore[return-value]
            except ValueError:
                return None
        return None

    async def _prog_notify(self, user_id: str, what: str, suggestion: dict[str, Any] | None) -> None:
        """The browser_mod popup on the browsers of the user who was refused."""
        person = next(
            (s.entity_id for s in self.hass.states.async_all("person")
             if s.attributes.get("user_id") == user_id),
            None,
        )
        if person is None or not self.hass.services.has_service("browser_mod", "popup"):
            _LOGGER.info("%s: no browser to tell user %s about the refusal", self.entity_id, user_id)
            return
        name = self.name or self.entity_id
        programs = ", ".join(p["summary"] for p in self._prog_active if p["summary"]) or "program"
        text = f"{name}: {what} izin verilmiyor ({programs})."
        data: dict[str, Any] = {
            "user_id": [person],
            "title": "İzin verilmiyor",
            "content": text,
            "tag": f"gungors_program_{self.entity_id}",
            "timeout": 15000,
        }
        if suggestion is not None:
            service = {"light": "light.turn_on", "climate": "climate.set_temperature",
                       "cover": "cover.set_cover_position"}[self._prog_domain]
            value = next(iter(suggestion.values()))
            shown = ", ".join(map(str, value)) if isinstance(value, list) else f"{value:g}"
            data["content"] = f"{text} Öneri: {shown}"
            data["right_button"] = f"{shown} uygula"
            data["right_button_action"] = {
                "service": service, "data": {"entity_id": self.entity_id, **suggestion}
            }
            data["left_button"] = "Vazgeç"
        try:
            await self.hass.services.async_call("browser_mod", "popup", data, blocking=False)
        except Exception as err:  # noqa: BLE001  (a popup must never break a request)
            _LOGGER.warning("%s: refusal popup failed: %s", self.entity_id, err)

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
        return _hs_close(color_util.color_RGB_to_hs(*rgb), self.hs_color)

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
        held = (own.get("set") or own.get("eq")) if isinstance(own, dict) else None
        self._prog_set = {a for a in held if a in DOMAINS} if isinstance(held, (dict, list)) else set()
        self._prog_active = []

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {"set": sorted(self._prog_set)}
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
