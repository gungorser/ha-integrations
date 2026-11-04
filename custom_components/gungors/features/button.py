"""Pushbutton features: `buttons` (ready functions) and `custom` (any action).

```yaml
keys: 3            # the wrapper's: how many keys the device has
door: right        # the wrapper's: the side of the door it is on (left / right)
features:
  - buttons:
      - light                        # near the door: 3_single / 3_double / 3_hold
      - cover                        # far: 1_*
      - climate                      # middle: 2_single
      - program: "Kaan gunduz uyku"  # middle: 2_double
      - custom: {action: switch.toggle, target: {entity_id: switch.plug}}  # middle: 2_hold
      - light: {button: 2, target: {area_id: araba}}   # a second light, its key given
  - link: event.bebek_pushbutton_action
```

Location rule: the key nearest the door is `near`, the farthest `far`, the one
between them on a three-key device `middle` (Moes key 1 is the leftmost). `light`
takes `near` (all three gestures), `cover` `far` (all three), and on the middle
key `climate` single, `program` double and `custom` hold. A function's `button`
overrides its key. A one-key device has only `near`; a two-key device has no
middle key, so climate, program and custom there need a `button`; without a
`door` every function of a two- or three-key device needs one. A function the
rule cannot place, a key the device does not have, or a slot taken twice is a
config error and the entity is not created. The old numbered form (`{1: light,
2: {climate: {program: ...}}}`) still reads.

`custom` (feature): `{<slot>: {action, target, data}}` on any slot no function took.

Targets: a function's `target` (`area_id` and/or `entity_id`), else the
wrapper's, else the linked device's area. Each function uses its domain's
entities of the target. Entities a gungors wrapper drives (the hidden
originals) are never targeted. A custom function's `target` is its action's.

`light` (presets.py: `default`, `dim`, `night`; a button may name others with
`single:`, `double:`, `hold:`):

- single: a light of the target on -> all off; all off -> on with `default`.
- double (brightness): the lit lights at `dim` -> back to `default`'s
  brightness, otherwise to `dim`; the colour stays. All off -> on in `default`'s
  colour at `dim`.
- hold (colour): the lit lights in `night` -> back to `default`'s colour,
  otherwise to `night`; the brightness stays. All off -> nothing.
- In general a press toggles the lit lights between its preset and `default`, in
  the attributes the preset carries.

`climate`: toggle: one of the target on -> all off, all off -> all heat. The
lights blink once: green on, blue off.

`program` (a timeless program's name or uid, `max:` optional, the longest run,
24 h when not given; `duration:` is the old name): start the program, or stop
its run; a run ends by itself after `max`. The lights blink twice: green started, blue stopped.

`cover` (as the old pushbutton blueprint):

- single / double: the target's covers open / close; a cover that is moving
  stops instead. A physical request:
  gungors covers with `window` move even with the window open; groups are
  expanded to their members.
- hold: every cover on the target's floor (the area alone without a floor): if
  one is not closed, a backup scene (`scene.cover_backup_<floor>`) is taken and
  all close; if all are closed, the backup is restored. These are remote
  commands (each room's window guard holds).

Requests go to gungors entities straight through their feature chain (so the
pushbutton knows when a program rule changed one) and to other entities by
their services. A request a program rule limited or refused is still applied as
the rule allows (nearest allowed value), and the target's lights then blink twice
in red (`warning`). Feedback colours and counts are fixed in code.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.components.cover import CoverState
from homeassistant.const import ATTR_AREA_ID, ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.util import color as color_util

from ..const import DOMAIN
from ..core import CLAIMS, ENTITIES, SOURCE_PHYSICAL, SOURCE_REMOTE, Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY_BUTTONS = "buttons"
KEY_CUSTOM = "custom"
TAG = "button"
GESTURES = ("single", "double", "hold")
FUNCTIONS = ("light", "climate", "program", "cover", "custom")
TRANSITION = 1

# Location rule: the key a function takes when it gives no `button`, and its gestures.
NEAR, MIDDLE, FAR = "near", "middle", "far"
POSITION = {"light": NEAR, "cover": FAR, "climate": MIDDLE, "program": MIDDLE, "custom": MIDDLE}
GESTURES_OF = {
    "light": GESTURES,
    "cover": GESTURES,
    "climate": ("single",),
    "program": ("double",),
    "custom": ("hold",),
}
DOORS = ("left", "right")

# Feedback (fixed): rgb colour, blinks.
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
RED = (255, 0, 0)
FEEDBACK_TOGGLE = 1
FEEDBACK_PROGRAM = 2
FEEDBACK_WARNING = 2
# s to wait after a light change before a warning blink (it puts back what it found)
WARN_DELAY = 1.5

# A lit light counts as at a preset within these tolerances.
BRIGHTNESS_PCT_TOLERANCE = 3
HS_TOLERANCE = (8.0, 12.0)  # hue degrees, saturation %

_TARGET = vol.Schema(
    {
        vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)
_BUTTON = vol.All(vol.Coerce(int), vol.In((1, 2, 3)))
_ARGS = {
    "light": vol.Schema(
        {
            vol.Optional("button"): _BUTTON,
            vol.Optional("target"): _TARGET,
            vol.Optional("single"): cv.string,
            vol.Optional("double"): cv.string,
            vol.Optional("hold"): cv.string,
        }
    ),
    "climate": vol.Schema({vol.Optional("button"): _BUTTON, vol.Optional("target"): _TARGET}),
    "program": vol.Schema(
        {
            vol.Required("program"): cv.string,
            vol.Exclusive("max", "max"): cv.positive_time_period,
            vol.Exclusive("duration", "max"): cv.positive_time_period,
            vol.Optional("button"): _BUTTON,
            vol.Optional("target"): _TARGET,
        }
    ),
    "cover": vol.Schema({vol.Optional("button"): _BUTTON, vol.Optional("target"): _TARGET}),
    "custom": vol.Schema(
        {
            vol.Required("action"): cv.service,
            vol.Optional("target", default={}): dict,
            vol.Optional("data", default={}): dict,
            vol.Optional("button"): _BUTTON,
        }
    ),
}
# The old numbered form (`2: {climate: {program: ...}}`) still reads.
_LEGACY_CLIMATE = vol.Schema(
    {
        vol.Optional("target"): _TARGET,
        vol.Optional("program"): cv.string,
        vol.Optional("duration"): cv.positive_time_period,
    }
)


def _slot(value: Any) -> str:
    text = str(value)
    if text in GESTURES:
        return f"1_{text}"
    button, _, gesture = text.partition("_")
    if button not in ("1", "2", "3") or gesture not in GESTURES:
        raise vol.Invalid(f"unknown slot '{value}' (<button 1-3>_<single|double|hold>)")
    return text


def _entry(function: str, args: dict[str, Any], button: int | None = None) -> dict[str, Any]:
    """One function of a pushbutton; `button` None until the location rule places it."""
    args = dict(args)
    button = args.pop("button", button)
    return {"function": function, "args": args, "button": button, "gestures": GESTURES_OF[function]}


def _list_entry(value: Any) -> dict[str, Any]:
    """`light`, `{light: {args}}` or `{program: "<name>"}` -> an entry."""
    if isinstance(value, str):
        name, args = value, {}
    elif isinstance(value, dict) and len(value) == 1:
        name, args = next(iter(value.items()))
        if name == "program" and isinstance(args, str):
            args = {"program": args}
        args = args or {}
    else:
        raise vol.Invalid(f"a function is a name or {{function: {{arguments}}}}: {value!r}")
    if name not in FUNCTIONS:
        raise vol.Invalid(f"unknown function '{name}' (known: {', '.join(FUNCTIONS)})")
    try:
        return _entry(name, _ARGS[name](args))
    except vol.Invalid as err:
        raise vol.Invalid(f"{name}: {err.msg}") from err


def _legacy_entries(button: int, value: Any) -> list[dict[str, Any]]:
    """`light` or `{climate: {program: ...}}` on a numbered button -> entries."""
    if isinstance(value, str):
        name, args = value, {}
    elif isinstance(value, dict) and len(value) == 1:
        name, args = next(iter(value.items()))
        args = args or {}
    else:
        raise vol.Invalid(f"a button is a function name or {{function: {{arguments}}}}: {value!r}")
    if name not in ("light", "climate", "cover"):
        raise vol.Invalid(f"unknown function '{name}' (known: light, climate, cover)")
    if name != "climate":
        return [_entry(name, _ARGS[name](args), button)]
    args = _LEGACY_CLIMATE(args)
    target = {"target": args["target"]} if "target" in args else {}
    entries = [_entry("climate", target, button)]
    if args.get("program"):
        program = {k: args[k] for k in ("program", "duration") if k in args}
        entries.append(_entry("program", {**program, **target}, button))
    return entries


def _buttons_schema(value: Any) -> list[dict[str, Any]]:
    """A list of functions (placed by the location rule), or the old {button: function}."""
    if isinstance(value, list) and value:
        result = []
        for index, item in enumerate(value):
            try:
                result.append(_list_entry(item))
            except vol.Invalid as err:
                raise vol.Invalid(f"buttons: item {index + 1}: {err.msg}") from err
        return result
    if not isinstance(value, dict) or not value:
        raise vol.Invalid("buttons: a list of functions")
    result = []
    for key, item in value.items():
        try:
            button = _BUTTON(key)
        except vol.Invalid as err:
            raise vol.Invalid(f"buttons: unknown button '{key}' (1-3)") from err
        try:
            result.extend(_legacy_entries(button, item))
        except vol.Invalid as err:
            raise vol.Invalid(f"buttons: {button}: {err.msg}") from err
    functions = [e["function"] for e in result if e["function"] != "program"]
    twice = sorted({f for f in functions if functions.count(f) > 1})
    if twice:
        raise vol.Invalid(f"buttons: '{', '.join(twice)}' on two buttons of one device")
    return result


def key_of(position: str, keys: int, door: str | None) -> int | None:
    """The key number of a position: `near` / `middle` / `far` from the door.

    Moes key 1 is the leftmost. A one-key device has only `near`; without a door
    (two or three keys) nothing is placed.
    """
    if keys == 1:
        return 1 if position == NEAR else None
    if door is None:
        return None
    if position == MIDDLE:
        return 2 if keys == 3 else None
    near, far = (1, keys) if door == "left" else (keys, 1)
    return near if position == NEAR else far


def place_buttons(entries: list[dict[str, Any]], keys: int | None, door: str | None
                  ) -> list[dict[str, Any]]:
    """Give every function its key (its `button`, else the location rule); check slots."""
    result = []
    taken: dict[str, str] = {}
    for entry in entries:
        function = entry["function"]
        button = entry["button"]
        if button is None:
            if keys is None:
                raise vol.Invalid(f"buttons: {function} has no button and the pushbutton no keys")
            position = POSITION[function]
            button = key_of(position, keys, door)
            if button is None:
                where = "no door" if door is None and keys > 1 else f"no {position} key"
                raise vol.Invalid(
                    f"buttons: {function}: the pushbutton has {where}; give the function a button"
                )
        if keys is not None and button > keys:
            raise vol.Invalid(f"buttons: {function}: button {button} on a {keys}-key pushbutton")
        for gesture in entry["gestures"]:
            slot = f"{button}_{gesture}"
            if slot in taken:
                raise vol.Invalid(f"buttons: slot {slot} taken by both {taken[slot]} and {function}")
            taken[slot] = function
        result.append({**entry, "button": button})
    return result


_ACTION = vol.Schema(
    {
        vol.Required("action"): cv.service,
        vol.Optional("target", default={}): dict,
        vol.Optional("data", default={}): dict,
    }
)


def _custom_schema(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise vol.Invalid("custom: a mapping of slot: action")
    result: dict[str, dict[str, Any]] = {}
    for key, action in value.items():
        slot = _slot(key)
        if slot in result:
            raise vol.Invalid(f"custom: slot {slot} listed twice")
        result[slot] = _ACTION(action)
    return result


# --- helpers ------------------------------------------------------------------


def _gungors(hass: HomeAssistant, entity_id: str) -> Any:
    return hass.data.get(DOMAIN, {}).get(ENTITIES, {}).get(entity_id)


def _hs_close(a: tuple[float, float], b: tuple[float, float]) -> bool:
    hue = abs(a[0] - b[0]) % 360
    return min(hue, 360 - hue) <= HS_TOLERANCE[0] and abs(a[1] - b[1]) <= HS_TOLERANCE[1]


class _ButtonBase(Feature):
    """Shared: targets, sending requests, feedback."""

    def _btn_target(self, own: dict[str, Any] | None) -> dict[str, Any]:
        """The target of a button: its own, the wrapper's, or the device's area."""
        if own:
            return own
        if self.gw_options.get("target"):
            return self.gw_options["target"]
        area = self._btn_device_area()
        return {ATTR_AREA_ID: [area]} if area else {}

    def _btn_device_area(self) -> str | None:
        link = getattr(self, "gw_link_entity", None)
        if not link:
            return None
        entry = er.async_get(self.hass).async_get(link)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        if entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            return device.area_id if device else None
        return None

    def _btn_entities(self, target: dict[str, Any], domain: str) -> list[str]:
        """The target's entities of a domain, without the originals gungors drives."""
        if not target:
            return []
        selected = async_extract_referenced_entity_ids(self.hass, TargetSelection(target))
        claimed = self.hass.data.get(DOMAIN, {}).get(CLAIMS, {})
        ids = selected.referenced | selected.indirectly_referenced
        result = []
        for entity_id in sorted(ids):
            if not entity_id.startswith(f"{domain}.") or entity_id in claimed:
                continue
            state = self.hass.states.get(entity_id)
            if state is None or state.state == STATE_UNAVAILABLE:
                continue
            result.append(entity_id)
        return result

    async def _btn_send(self, entity_id: str, data: dict[str, Any], service: tuple[str, str],
                        service_data: dict[str, Any], physical: bool = False) -> bool:
        """A request to a gungors entity (True if a rule changed it) or a service call."""
        entity = _gungors(self.hass, entity_id)
        if entity is not None:
            request = Request(
                dict(data), source=SOURCE_PHYSICAL if physical else SOURCE_REMOTE, tags={TAG}
            )
            await entity.gw_request(request)
            return request.adjusted
        await self.hass.services.async_call(
            service[0], service[1], {ATTR_ENTITY_ID: entity_id, **service_data}, blocking=False
        )
        return False

    async def _btn_blink(self, lights: list[str], rgb: tuple[int, int, int], count: int) -> None:
        for entity_id in lights:
            entity = _gungors(self.hass, entity_id)
            blink = getattr(entity, "gw_blink", None)
            if blink is not None:
                await blink(rgb, count)

    async def _btn_warn(self, target: dict[str, Any]) -> None:
        await asyncio.sleep(WARN_DELAY)
        await self._btn_blink(self._btn_entities(target, "light"), RED, FEEDBACK_WARNING)

    async def _btn_action(self, action: dict[str, Any]) -> None:
        """A custom action: `{action, target, data}`."""
        domain, service = action["action"].split(".", 1)
        try:
            await self.hass.services.async_call(
                domain, service, dict(action["data"]), blocking=False,
                target=dict(action["target"]) or None,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: %s failed", self.entity_id, action["action"])


@register("event")
class Buttons(_ButtonBase):
    feature_name = KEY_BUTTONS
    schema = staticmethod(_buttons_schema)

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {
            f"{entry['button']}_{gesture}"
            for entry in conf
            if entry["button"] is not None
            for gesture in entry["gestures"]
        }

    async def gw_apply(self, request: Request) -> None:
        await super().gw_apply(request)
        slot = request.data.get("event_type")
        if not slot or "_" not in slot:
            return
        button, gesture = slot.split("_", 1)
        entry = next(
            (
                e for e in self.gw_conf(KEY_BUTTONS)
                if str(e["button"]) == button and gesture in e["gestures"]
            ),
            None,
        )
        if entry is None:
            return
        function, args = entry["function"], entry["args"]
        try:
            if function == "custom":
                await self._btn_action(args)
                return
            target = self._btn_target(args.get("target"))
            if function == "light":
                await self._btn_light(gesture, args, target)
            elif function == "climate":
                await self._btn_climate(target)
            elif function == "program":
                await self._btn_program(args, target)
            else:
                await self._btn_cover(gesture, target)
        except Exception:  # noqa: BLE001  (a press must never break the wrapper)
            _LOGGER.exception("%s: %s failed", self.entity_id, slot)

    # --- light -------------------------------------------------------------------

    def _btn_preset(self, name: str) -> dict[str, Any]:
        from ..presets import preset  # noqa: PLC0415

        values = preset(self.hass, "light", name)
        if values is None:
            _LOGGER.warning("%s: no light preset '%s'", self.entity_id, name)
            return {}
        return values

    @staticmethod
    def _btn_light_data(values: dict[str, Any]) -> dict[str, Any]:
        """Preset values (rules names) -> light.turn_on data."""
        data: dict[str, Any] = {}
        if values.get("brightness") is not None:
            data["brightness"] = max(1, round(float(values["brightness"]) * 255 / 100))
        if values.get("color") is not None:
            data["rgb_color"] = tuple(values["color"])
        elif values.get("color_temp") is not None:
            data["color_temp_kelvin"] = int(values["color_temp"])
        return data

    def _btn_at(self, entity_id: str, values: dict[str, Any]) -> bool:
        """Whether a lit light shows every attribute of a preset."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state != STATE_ON:
            return False
        attrs = state.attributes
        if values.get("brightness") is not None:
            brightness = attrs.get("brightness")
            if brightness is None or abs(brightness * 100 / 255 - float(values["brightness"])) > BRIGHTNESS_PCT_TOLERANCE:
                return False
        if values.get("color") is not None:
            hs = attrs.get("hs_color")
            if hs is None or not _hs_close(color_util.color_RGB_to_hs(*values["color"]), tuple(hs)):
                return False
        if values.get("color_temp") is not None:
            kelvin = attrs.get("color_temp_kelvin")
            if kelvin is None or abs(kelvin - int(values["color_temp"])) > 100:
                return False
        return True

    async def _btn_lights(self, lights: list[str], on: bool, data: dict[str, Any]) -> bool:
        adjusted = False
        for entity_id in lights:
            payload = {"on": on, "transition": TRANSITION, **data}
            service = ("light", "turn_on" if on else "turn_off")
            sent = {"transition": TRANSITION, **(data if on else {})}
            adjusted |= await self._btn_send(entity_id, payload, service, sent)
        return adjusted

    async def _btn_light(self, gesture: str, args: dict[str, Any], target: dict[str, Any]) -> None:
        lights = self._btn_entities(target, "light")
        if not lights:
            return
        lit = [e for e in lights if self.hass.states.get(e).state == STATE_ON]
        default = self._btn_preset("default")
        name = args.get(gesture) or {"single": "default", "double": "dim", "hold": "night"}[gesture]
        chosen = self._btn_preset(name)
        adjusted = False
        if gesture == "single":
            if lit:
                adjusted = await self._btn_lights(lights, False, {})
            else:
                adjusted = await self._btn_lights(lights, True, self._btn_light_data({**default, **chosen}))
        elif not lit:
            if gesture == "double":
                # On in the default's other axes at this preset.
                adjusted = await self._btn_lights(
                    lights, True, self._btn_light_data({**default, **chosen})
                )
            else:
                return
        else:
            back = all(self._btn_at(e, chosen) for e in lit)
            values = {k: default[k] for k in chosen if k in default} if back else chosen
            data = self._btn_light_data(values)
            if not data:
                return
            adjusted = await self._btn_lights(lit, True, data)
        if adjusted:
            await self._btn_warn(target)

    # --- climate -----------------------------------------------------------------

    async def _btn_program(self, args: dict[str, Any], target: dict[str, Any]) -> None:
        """Start the timeless program, or stop its run."""
        from ..program import async_get_manager  # noqa: PLC0415

        lights = self._btn_entities(target, "light")
        manager = async_get_manager(self.hass)
        try:
            uid = manager.timeless_uid(args["program"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: %s", self.entity_id, err)
            return
        if manager.is_running(uid):
            await manager.async_stop(uid)
            await self._btn_blink(lights, BLUE, FEEDBACK_PROGRAM)
        else:
            duration = args.get("max", args.get("duration"))
            await manager.async_start(
                uid, duration if isinstance(duration, datetime.timedelta) else None
            )
            await self._btn_blink(lights, GREEN, FEEDBACK_PROGRAM)

    async def _btn_climate(self, target: dict[str, Any]) -> None:
        lights = self._btn_entities(target, "light")
        climates = self._btn_entities(target, "climate")
        if not climates:
            return
        on = not any(self.hass.states.get(e).state != HVACMode.OFF for e in climates)
        mode = HVACMode.HEAT if on else HVACMode.OFF
        adjusted = False
        for entity_id in climates:
            adjusted |= await self._btn_send(
                entity_id, {"hvac_mode": mode}, ("climate", "turn_on" if on else "turn_off"), {}
            )
        if adjusted:
            await self._btn_warn(target)
        else:
            await self._btn_blink(lights, GREEN if on else BLUE, FEEDBACK_TOGGLE)

    # --- cover -------------------------------------------------------------------

    def _btn_expand(self, covers: list[str]) -> list[str]:
        """Groups (an `entity_id` attribute) replaced by their members."""
        result: list[str] = []
        todo = list(covers)
        seen: set[str] = set()
        while todo:
            entity_id = todo.pop(0)
            if entity_id in seen:
                continue
            seen.add(entity_id)
            state = self.hass.states.get(entity_id)
            members = state.attributes.get(ATTR_ENTITY_ID) if state else None
            if isinstance(members, (list, tuple)) and members:
                todo.extend(members)
            elif state is not None and state.state != STATE_UNAVAILABLE:
                result.append(entity_id)
        return result

    async def _btn_cover(self, gesture: str, target: dict[str, Any]) -> None:
        if gesture == "hold":
            await self._btn_cover_floor(target)
            return
        position = 100 if gesture == "single" else 0
        service = ("cover", "open_cover" if position else "close_cover")
        adjusted = False
        for entity_id in self._btn_expand(self._btn_entities(target, "cover")):
            state = self.hass.states.get(entity_id)
            if state is not None and state.state in (CoverState.OPENING, CoverState.CLOSING):
                # A press while it moves stops it (as the wall buttons always did).
                await self._btn_send(
                    entity_id, {"stop": True}, ("cover", "stop_cover"), {}, physical=True
                )
                continue
            adjusted |= await self._btn_send(
                entity_id, {"position": position}, service, {}, physical=True
            )
        if adjusted:
            await self._btn_warn(target)

    def _btn_floor(self, target: dict[str, Any]) -> tuple[str | None, list[str]]:
        """The floor of the target's first area and the areas to act on."""
        areas = list(target.get(ATTR_AREA_ID) or [])
        if not areas:
            return None, []
        registry = ar.async_get(self.hass)
        area = registry.async_get_area(areas[0])
        floor = area.floor_id if area else None
        if floor is None:
            return None, areas
        return floor, [a.id for a in registry.async_list_areas() if a.floor_id == floor]

    async def _btn_cover_floor(self, target: dict[str, Any]) -> None:
        floor, areas = self._btn_floor(target)
        covers = self._btn_entities({ATTR_AREA_ID: areas}, "cover") if areas else []
        if not covers:
            covers = self._btn_entities(target, "cover")
        if not covers:
            return
        name = floor or (areas[0] if areas else self.gw_wrapper)
        backup = f"cover_backup_{name}"
        if any(self.hass.states.get(e).state != "closed" for e in covers):
            await self.hass.services.async_call(
                "scene", "create", {"scene_id": backup, "snapshot_entities": covers}, blocking=True
            )
            await self.hass.services.async_call(
                "cover", "close_cover", {ATTR_ENTITY_ID: covers}, blocking=False
            )
            return
        scene = self.hass.states.get(f"scene.{backup}")
        if scene is not None and scene.state not in ("unknown", "unavailable"):
            await self.hass.services.async_call(
                "scene", "turn_on", {ATTR_ENTITY_ID: f"scene.{backup}"}, blocking=False
            )


@register("event")
class Custom(_ButtonBase):
    feature_name = KEY_CUSTOM
    schema = staticmethod(_custom_schema)

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return set(conf)

    async def gw_apply(self, request: Request) -> None:
        await super().gw_apply(request)
        action = self.gw_conf(KEY_CUSTOM).get(request.data.get("event_type") or "")
        if action is None:
            return
        await self._btn_action(action)
