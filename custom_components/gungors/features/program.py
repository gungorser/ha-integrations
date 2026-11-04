"""`program`: follow the active entries of `calendar.program` (see rules.py).

`- program` (first in the feature list), or `- program: {<n>: <default>}` to
override the domain defaults of the program calendar for this entity (names as
in the rules: `state`, `brightness_pct`, ..., a value or a template).

While programs are active the entity follows their rules:

- A request a user makes (a dashboard, the app, a script they run) is not
  refused. It is brought to the nearest allowed value (brightness 49 with
  `min 50` -> 50, a cover at 100 with `max 60` -> 60) and the user gets a popup
  saying so. Two cases keep the device in its rule instead of turning it off:
  a light turned off while its brightness has a floor (`min 50`) stays on at
  that floor, and a heating turned off while a temperature rule holds stays on.
- A cover stop is ignored while its position lies outside its `position` rule
  (the program is moving it back into range), with a popup saying so. It is
  accepted again once the position is in range, when the position is unknown,
  or when the program has given up correcting the device.
- Other requests (automations, other features) and the device changing by itself
  (a wall switch or dimmer, the thermostat dial) are brought to the nearest
  allowed value without asking; so is the state when a rule starts.
- A `let` rule only shapes what applies in the current state: an off light or
  heating satisfies it, `color: red` recolours a lit light. A `set` rule also
  brings the device into it: a set brightness, colour or colour temperature rule
  keeps the light on, a set temperature rule keeps the heating on.
- A `set` rule with only a min or only a max limit moves the device to that
  limit when it starts, whatever the current value (a cover at 0 with `max 20
  set` goes to 20; a light at 100 with `min 50 set` goes to 50). A `set` range
  (min and max) moves the device only when it is outside the range, to the
  nearest end of it.
- When a `set` rule ends, the device returns to its defaults, except what other
  set rules still hold (light off and white, heating off, covers by the sun:
  open by day, closed at night). `let` rules just lift.
- A template default (the covers' sun one) is applied again whenever its result
  changes, unless a set rule holds the attribute. Defaults are applied at those
  moments only; they never lock anything.

Preheat (climate): `- program: {preheat: {rate: 1.5, max: {hours: 3}}}` (rate in
°C per hour; max 3 h when not given). The thermostat then reaches the set
temperature rule of a coming program (`eq`, or the `min` of a range) by its start:

- From (target - room temperature) / rate before the start (at most `max`), the
  program's temperature rule (and the heating it implies) applies as if the
  program had started: user requests are brought to it with a popup as usual,
  and it goes before the temperature rules of programs running now. Only the
  next coming program counts; none when the room is at its target already.
- The window, once open, stays open until the start (latched, also over a
  restart), so the room warming up does not end it. It closes early when the
  program is deleted or paused (the pause lasts until its end); the heating then
  returns as when a set rule ends.
- Programs with `climate: preheat: false` and timeless runs are not preheated for.

Attribute `program`: the active programs of this entity (summary, start, end,
rules; a preheat window shows with `preheat: true`). `preheat`: the plan for the
next program (`uid`, `summary`, `target`, `at` its start, `start` the planned or
actual window start, `rate`, `active`), or None. `preheat_last`: how the latest
window ended (`target`, `at`, `opened`, `reached` the time the room got there or
None, `late_min` minutes after the start, negative when early).
The attributes `set` rules hold survive a restart, so a program that
ended while Home Assistant was down still restores the defaults.
"""
from __future__ import annotations

from dataclasses import replace
import datetime
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.components.light import ColorMode
from homeassistant.core import Event, State, callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.template import Template
from homeassistant.util import color as color_util, dt as dt_util

from ..core import SOURCE_REMOTE, Feature, Request, register
from ..rules import (
    DOMAINS,
    NAMES,
    Constraint,
    attr_of,
    default_value,
    external,
    implied,
    is_template,
    parse_defaults,
)

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
# Preheat: how often the window is checked, how close the room must get to count as there,
# and how long after the start a window that never got there is given up.
PREHEAT_CHECK = datetime.timedelta(minutes=1)
PREHEAT_REACHED = 0.05
PREHEAT_FOLLOW = datetime.timedelta(hours=2)
PREHEAT_SCHEMA = vol.Schema({
    vol.Required("rate"): vol.All(vol.Coerce(float), vol.Range(min=0.1)),
    vol.Optional("max", default=datetime.timedelta(hours=3)): cv.positive_time_period,
})
# Rules a preheat window brings forward (and takes over from running programs).
PREHEAT_ATTRS = ("temperature", "hvac_mode")
# How close (in the rule's unit) a device must be to a set rule's limit to count as there.
_LIMIT_TOLERANCE = {"position": 0.5, "brightness": 0.5, "temperature": 0.05}

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


def _limit(constraint: Constraint) -> float | None:
    """The limit a one-sided rule moves its device to; None for a range or an exact value."""
    if constraint.eq is not None or (constraint.min is not None and constraint.max is not None):
        return None
    if constraint.min is not None:
        return float(constraint.min)
    if constraint.max is not None:
        return float(constraint.max)
    return None


def _constraint_fields(constraint: Constraint) -> dict[str, Any]:
    """A constraint as the keyword arguments that rebuild it (restore data)."""
    return {"eq": constraint.eq, "min": constraint.min, "max": constraint.max, "set": constraint.set}


def _shown(attr: str, constraint: Constraint) -> dict[str, Any]:
    result = constraint.as_dict()
    if "eq" in result:
        result["eq"] = external(attr, constraint.eq)
    return result


def _preheat_target(constraint: Constraint | None) -> float | None:
    """The temperature a preheat aims at: a set rule's exact value or its floor."""
    if constraint is None or not constraint.set:
        return None
    if constraint.eq is not None:
        return float(constraint.eq)
    return float(constraint.min) if constraint.min is not None else None


def _own_defaults(value: Any) -> dict[str, Any]:
    """`- program: {<n>: <default>, preheat: {...}}`: this entity's own defaults (read per
    domain later) and its preheat settings."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise vol.Invalid("program: a mapping of attribute: default value")
    value = dict(value)
    preheat = value.pop("preheat", None)
    if preheat is not None:
        try:
            preheat = PREHEAT_SCHEMA(preheat)
        except vol.Invalid as err:
            raise vol.Invalid(f"program: preheat: {err}") from err
    for key, default in value.items():
        domains = [d for d in dict.fromkeys(DOMAINS.values()) if attr_of(d, key) is not None]
        if not domains:
            raise vol.Invalid(f"program: unknown attribute '{key}'")
        errors = []
        for domain in domains:
            try:
                parse_defaults(domain, {key: default})
            except vol.Invalid as err:
                errors.append(err)
        if len(errors) == len(domains):
            raise errors[0]
    if preheat is not None:
        value["preheat"] = preheat
    return value


@register("cover", "climate", "light")
class Program(Feature):
    feature_name = KEY
    schema = _own_defaults

    # [{uid, summary, start, end, rules: {attr: Constraint}}] for this entity
    _prog_active: list[dict[str, Any]] = []
    _prog_set: set[str] = set()  # attributes set rules hold (for the defaults)
    # attributes whose set rule just started and whose device has not reached its limit yet
    _prog_pending: set[str] = set()
    _prog_sent_at = -COOLDOWN
    _prog_recheck = None
    _prog_last: dict[str, Any] | None = None  # the last correction sent
    _prog_repeats = 0  # how often in a row it was sent
    # Preheat (climate): the manager's running and coming programs, the open window
    # ({uid, summary, at, end, rules, opened, target}), the latest outcome, the
    # window being followed until the room gets there.
    _prog_given: list[dict[str, Any]] = []
    _prog_coming: list[dict[str, Any]] = []
    _prog_defaults_given: dict[str, Any] = {}
    _prog_window: dict[str, Any] | None = None
    _prog_preheat_last: dict[str, Any] | None = None
    _prog_follow: dict[str, Any] | None = None
    _prog_plan: dict[str, Any] | None = None

    @property
    def _prog_domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    # --- manager interface ---------------------------------------------------

    def gw_program_defaults(self, domain_defaults: dict[str, Any]) -> dict[str, Any]:
        own = self.gw_conf(KEY) or {}
        domain = self._prog_domain
        mine = {k: v for k, v in own.items() if attr_of(domain, k) is not None}
        return {**domain_defaults, **parse_defaults(domain, mine)}

    async def gw_program_set(
        self,
        active: list[dict[str, Any]],
        defaults: dict[str, Any],
        coming: list[dict[str, Any]] | None = None,
    ) -> None:
        """The manager's new set of active (and, for preheat, coming) programs."""
        self._prog_given = active
        self._prog_coming = coming or []
        self._prog_defaults_given = defaults
        self._prog_preheat_eval()
        await self._prog_apply()

    async def _prog_apply(self) -> None:
        """Enforce the active programs (the manager's plus an open preheat window)."""
        defaults = self._prog_defaults_given
        self._prog_active = self._prog_effective()
        self._prog_last, self._prog_repeats = None, 0
        rules = self._prog_rules()
        held = {a for a, c in rules.items() if c.set}
        ended = [a for a in self._prog_set if a not in held]
        started = held - self._prog_set
        self._prog_set = held
        self._prog_pending = (self._prog_pending & held) | started
        self.async_write_ha_state()
        if ended:
            # The device returns to its defaults (a set brightness ending: light off).
            await self.gw_program_apply_defaults(list(DOMAINS), defaults)
        await self._prog_enforce(force=True)

    async def gw_program_apply_defaults(self, attrs: list[str], defaults: dict[str, Any]) -> None:
        """Apply the defaults of attributes no program holds any more."""
        values = self.gw_program_defaults(defaults)
        wanted = {}
        for attr, value in values.items():
            if attr in attrs and attr not in self._prog_set:
                value = self._prog_render(attr, value)
                if value is not None:
                    wanted[attr] = value
        data = self._prog_default_request(wanted)
        if data:
            _LOGGER.debug("%s: program defaults %s", self.entity_id, data)
            await self.gw_request(Request(data, tags={TAG}))

    def _prog_render(self, attr: str, value: Any) -> Any:
        """A default's value now (a template rendered and checked), None if it can't be had."""
        if not is_template(value):
            return value
        try:
            rendered = Template(value, self.hass).async_render(parse_result=True)
            return default_value(attr, rendered)
        except (TemplateError, vol.Invalid) as err:
            _LOGGER.warning("%s: default %s %r: %s", self.entity_id, NAMES[attr], value, err)
            return None

    # --- preheat -------------------------------------------------------------

    def _prog_preheat_conf(self) -> dict[str, Any] | None:
        if self._prog_domain != "climate":
            return None
        return (self.gw_conf(KEY) or {}).get("preheat")

    def _prog_effective(self) -> list[dict[str, Any]]:
        """The manager's programs plus the open window, which takes over their temperature."""
        window = self._prog_window
        if window is None:
            return self._prog_given
        others = [
            {**p, "rules": {a: c for a, c in p["rules"].items() if a not in PREHEAT_ATTRS}}
            for p in self._prog_given if p["uid"] != window["uid"]
        ]
        return [*others, {
            "uid": window["uid"], "summary": window["summary"], "start": window["at"],
            "end": window["end"], "rules": window["rules"], "preheat": True,
        }]

    def _prog_lead(self, target: float, conf: dict[str, Any]) -> datetime.timedelta | None:
        """How long before the start heating must begin; None: the room is there already."""
        current = self.current_temperature
        if current is None or current >= target - PREHEAT_REACHED:
            return None
        hours = (target - current) / conf["rate"]
        return min(datetime.timedelta(hours=hours), conf["max"])

    def _prog_preheat_eval(self) -> bool:
        """Open or close the window for the next coming program. True: it opened or closed."""
        conf = self._prog_preheat_conf()
        now = dt_util.utcnow()
        before = self._prog_window
        window = before
        coming = sorted(self._prog_coming, key=lambda p: p["start"])
        if window is not None and (conf is None or not any(
            p["uid"] == window["uid"] and p["start"] == window["at"] for p in coming
        )):
            window = None  # started, deleted or paused
        plan: dict[str, Any] | None = None
        if conf is not None and coming:
            nxt = next(
                (p for p in coming if window is not None and p["uid"] == window["uid"]
                 and p["start"] == window["at"]),
                coming[0],
            )
            target = _preheat_target(nxt["rules"].get("temperature"))
            at = dt_util.parse_datetime(nxt["start"])
            if target is not None and at is not None:
                plan = {"uid": nxt["uid"], "summary": nxt["summary"], "target": target,
                        "at": nxt["start"], "rate": conf["rate"], "start": None, "active": False}
                if window is not None and window["uid"] == nxt["uid"]:
                    plan.update(start=window["opened"], active=True)
                elif window is None and (lead := self._prog_lead(target, conf)) is not None:
                    opens = at - lead
                    plan["start"] = opens.isoformat()
                    if now >= opens:
                        window = {
                            "uid": nxt["uid"], "summary": nxt["summary"], "at": nxt["start"],
                            "end": nxt["end"], "rules": nxt["rules"], "target": target,
                            "opened": dt_util.as_local(now).isoformat(),
                        }
                        plan.update(start=window["opened"], active=True)
                        self._prog_follow = dict(window)
                        _LOGGER.info(
                            "%s: preheating for '%s' (%.1f° by %s)",
                            self.entity_id, nxt["summary"], target, nxt["start"],
                        )
        self._prog_window = window
        self._prog_plan = plan
        self._prog_follow_check(now)
        from ..program import async_get_manager  # noqa: PLC0415

        info = None
        if plan is not None and plan["start"] is not None:
            info = {"key": f"{plan['uid']}|{plan['at']}", "uid": plan["uid"],
                    "start": plan["start"], "active": plan["active"]}
        async_get_manager(self.hass).preheat_changed(self.entity_id, info)
        return (before is None) != (window is None) or (
            before is not None and window is not None and before["uid"] != window["uid"]
        )

    def _prog_follow_check(self, now: datetime.datetime) -> None:
        """Note when the room reached the target of the latest window."""
        follow = self._prog_follow
        if follow is None:
            return
        at = dt_util.parse_datetime(follow["at"])
        current = self.current_temperature
        reached = current is not None and current >= follow["target"] - PREHEAT_REACHED
        if not reached and (at is None or now < at + PREHEAT_FOLLOW):
            return
        self._prog_follow = None
        self._prog_preheat_last = {
            "summary": follow["summary"], "target": follow["target"], "at": follow["at"],
            "opened": follow["opened"],
            "reached": dt_util.as_local(now).isoformat() if reached else None,
            "late_min": round((now - at).total_seconds() / 60) if reached and at else None,
        }

    def _prog_preheat_tick(self) -> None:
        if self._prog_preheat_conf() is None:
            return
        shown = (self._prog_plan, self._prog_preheat_last)
        if self._prog_preheat_eval():
            self.hass.async_create_task(self._prog_apply())
        elif (self._prog_plan, self._prog_preheat_last) != shown:
            self.async_write_ha_state()

    def gw_temperature_changed(self) -> None:
        super().gw_temperature_changed()
        if self._prog_coming or self._prog_window is not None or self._prog_follow is not None:
            self._prog_preheat_tick()

    # --- chain ---------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        if request.from_device or not self._prog_active:
            accepted = await super().gw_request(request)
            if request.from_device and self._prog_active:
                self.hass.async_create_task(self._prog_enforce())
            return accepted
        user = self._prog_user(request)
        if user is not None and self._prog_domain == "cover" and request.data.get("stop"):
            if self._prog_stop_blocked():
                self.hass.async_create_task(
                    self._prog_inform(user, "perde kurala uyması için ayarlanıyor, durdurma kabul edilmedi")
                )
                return False
            return await super().gw_request(request)
        original = dict(request.data)
        if user is not None:
            request.data = self._prog_user_adjust(dict(request.data))
        data = self._prog_clamp(dict(request.data))
        if data is None:
            _LOGGER.info("%s: %s rejected by a program", self.entity_id, request.data)
            request.adjusted = True
            return False
        if data != original:
            request.adjusted = True
        if data != request.data:
            _LOGGER.debug("%s: program clamped %s to %s", self.entity_id, request.data, data)
        if user is not None and data != original:
            self.hass.async_create_task(self._prog_inform(user, "istek kurala göre ayarlandı"))
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
        if self._prog_preheat_conf() is not None:

            @callback
            def check(_now) -> None:
                if self._prog_coming or self._prog_window is not None or self._prog_follow:
                    self._prog_preheat_tick()

            self.async_on_remove(async_track_time_interval(self.hass, check, PREHEAT_CHECK))

    async def async_will_remove_from_hass(self) -> None:
        self._prog_cancel_recheck()
        if self._prog_preheat_conf() is not None:
            from ..program import async_get_manager  # noqa: PLC0415

            async_get_manager(self.hass).preheat_changed(self.entity_id, None)
        await super().async_will_remove_from_hass()

    @callback
    def _prog_state_changed(self, event: Event) -> None:
        # A blink (gungors.blink) is feedback, not a state the rules judge.
        if self._prog_active and not getattr(self, "gw_blinking", False):
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

    # --- user requests -------------------------------------------------------

    def _prog_user(self, request: Request) -> str | None:
        """The user behind a request from a service call, None for anything else."""
        if request.source != SOURCE_REMOTE or request.tags or request.from_device:
            return None
        context, since = self._context, self._context_set
        if context is None or since is None or time.time() - since > CONTEXT_RECENT:
            return None
        return context.user_id

    def _prog_user_adjust(self, data: dict[str, Any]) -> dict[str, Any]:
        """A user's request turned to fit the rules before it is clamped.

        A light turned off while its brightness has a floor stays on (at the
        clamped brightness); heating turned off while a temperature rule holds
        stays on. Rules that allow the off state are left as they are.
        """
        rules = self._prog_rules()
        if self._prog_domain == "light" and data.get("on") is False and "on" not in rules:
            floor = rules["brightness"].bounds()[0] if "brightness" in rules else 0
            if floor > 0 and self.is_on:
                return {**data, "on": True}
        if (
            self._prog_domain == "climate"
            and data.get("hvac_mode") == HVACMode.OFF
            and "hvac_mode" not in rules
            and "temperature" in rules
            and self.hvac_mode != HVACMode.OFF
        ):
            return {**data, "hvac_mode": HVACMode.HEAT}
        return data

    def _prog_stop_blocked(self) -> bool:
        """A stop is ignored while the cover is outside its position rule.

        The program is moving it back into range then. Not blocked when the
        position is unknown or the program has given up correcting the device.
        """
        rules = self._prog_rules()
        position = self.current_cover_position
        if "position" not in rules or position is None:
            return False
        return not rules["position"].holds(position) and self._prog_repeats < MAX_REPEATS

    async def _prog_inform(self, user_id: str, text: str) -> None:
        """A popup on the browsers of the user whose request was adjusted or ignored."""
        person = next(
            (s.entity_id for s in self.hass.states.async_all("person")
             if s.attributes.get("user_id") == user_id),
            None,
        )
        if person is None or not self.hass.services.has_service("browser_mod", "popup"):
            _LOGGER.info("%s: no browser to tell user %s: %s", self.entity_id, user_id, text)
            return
        name = self.name or self.entity_id
        programs = ", ".join(p["summary"] for p in self._prog_active if p["summary"]) or "program"
        data: dict[str, Any] = {
            "user_id": [person],
            "title": "Program",
            "content": f"{name}: {text} ({programs}).",
            "tag": f"gungors_program_{self.entity_id}",
            "timeout": 15000,
        }
        try:
            await self.hass.services.async_call("browser_mod", "popup", data, blocking=False)
        except Exception as err:  # noqa: BLE001  (a popup must never break a request)
            _LOGGER.warning("%s: program popup failed: %s", self.entity_id, err)

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
        start = self._prog_start_correction(self._prog_rules())
        if start is not None:
            return start
        domain = self._prog_domain
        if domain == "light":
            return self._prog_correct_light()
        if domain == "climate":
            return self._prog_correct_climate()
        return self._prog_correct_cover()

    # set rules that start: move to their limit

    def _prog_start_correction(self, rules: dict[str, Constraint]) -> dict[str, Any] | None:
        """The move a started one-sided set rule asks for: once, to its limit."""
        pending: set[str] = set()
        move: dict[str, Any] | None = None
        for attr in sorted(self._prog_pending):
            constraint = rules.get(attr)
            target = _limit(constraint) if constraint is not None else None
            if target is None:
                continue  # the rule ended, or it is a range: nothing to move to
            current = self._prog_current(attr)
            if current is not None and abs(current - target) <= _LIMIT_TOLERANCE[attr]:
                continue  # already there
            pending.add(attr)
            if move is None and not self._prog_moving():
                move = self._prog_start_move(attr, target, rules)
        self._prog_pending = pending
        return move

    def _prog_current(self, attr: str) -> float | None:
        if attr == "position":
            return self.current_cover_position
        if attr == "brightness":
            return self._prog_brightness_pct()
        if attr == "temperature":
            return self.target_temperature
        return None

    def _prog_moving(self) -> bool:
        return self._prog_domain == "cover" and bool(self.is_opening or self.is_closing)

    def _prog_start_move(
        self, attr: str, target: float, rules: dict[str, Constraint]
    ) -> dict[str, Any] | None:
        if attr == "position":
            return {"position": int(round(target))}
        if attr == "brightness":
            return self._prog_clamp_light(
                {"on": True, "brightness": round(target * 255 / 100)}, rules
            )
        if attr == "temperature":
            data: dict[str, Any] = {"temperature": target}
            if self.hvac_mode != HVACMode.HEAT:
                data["hvac_mode"] = HVACMode.HEAT
            return self._prog_clamp_climate(data, rules)
        return None

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
            position = int(round(float(wanted["position"])))
            if self.current_cover_position != position:
                return {"position": position}
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
            if "on" not in data:
                return data
            # Off keeps only its transition: the rest has no meaning off, and an
            # untouched off request must stay equal (not taken as adjusted).
            return {"on": False, **({"transition": data["transition"]} if "transition" in data else {})}
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
            return None  # a stop could leave the allowed range (user stops: see gw_request)
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
        self._prog_pending = set()
        self._prog_active = []
        self._prog_window = None
        self._prog_preheat_last = None
        if isinstance(own, dict):
            window = own.get("window")
            if isinstance(window, dict) and {"uid", "at", "rules", "target"} <= set(window):
                try:
                    window["rules"] = {
                        a: Constraint(**c) for a, c in window["rules"].items() if a in PREHEAT_ATTRS
                    }
                    self._prog_window = window
                except TypeError:
                    pass
            if isinstance(own.get("preheat_last"), dict):
                self._prog_preheat_last = own["preheat_last"]

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {"set": sorted(self._prog_set)}
        if self._prog_window is not None:
            window = dict(self._prog_window)
            window["rules"] = {a: _constraint_fields(c) for a, c in window["rules"].items()}
            data[KEY]["window"] = window
        if self._prog_preheat_last is not None:
            data[KEY]["preheat_last"] = self._prog_preheat_last
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
                "rules": {NAMES[a]: _shown(a, c) for a, c in p["rules"].items()},
                **({"preheat": True} if p.get("preheat") else {}),
            }
            for p in self._prog_active
        ]
        if self._prog_preheat_conf() is not None:
            attrs["preheat"] = getattr(self, "_prog_plan", None)
            attrs["preheat_last"] = self._prog_preheat_last
        return attrs
