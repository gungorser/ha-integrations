"""`relay` for pushbuttons: a key's relay (its LED) follows the key's function.

```yaml
event:
  - platform: gungors
    name: yemek_switchbutton
    keys: 3
    door: left
    features:
      - buttons:
          - light
          - light: {button: 3, target: {area_id: [veranda, avlu, bahce]}}
      - relay:
          1: switch.yemek_1   # key 1's LED is wired to this relay
          3: switch.yemek_3
      - link: event.yemek_switchbutton_action
```

The value maps a key to a `switch` or `light` entity. Wall switches whose key LEDs
are wired to a relay without a load (ESPHome switchbuttons) light the LED by
switching that relay; this feature switches it so the LED shows the key's state:

- The state comes from the key's functions (`buttons` entries on that key and
  `custom` slots of that key) and their targets: `light` one of the target's
  lights on, `climate` one not off, `cover` one not closed, `program` its run
  running, `custom` one of the action's target entities `on`. Any of them -> on.
- The relay is written only when it differs from that state: a press does not
  switch it, the relay follows when the target changes. A relay switched by hand
  is switched back. At startup and after `gungors.reload` it is synced.
- Targets that are all unavailable or unknown, or an unavailable relay, leave the
  relay as it is. While a target light blinks (`gungors.blink`) the relay waits.
- The wrapper keeps no state of its own; attribute `relays` shows
  `{key: {entity_id, on}}` (`on` null while the state is not known).
- The relay is claimed (no other feature may write it), so area targets never
  include it. A key the device does not have, or a key without a function, is a
  config error (`check_relay`, called from `event.py`).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.components.cover import CoverState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from ..const import DOMAIN
from ..core import CLAIMS, register
from .button import KEY_BUTTONS, KEY_CUSTOM, _ButtonBase, _gungors

_LOGGER = logging.getLogger(__name__)

KEY = "relay"
DEBOUNCE = 0.3  # s: changes of a group's members come together
RETRY = 1.0  # s: check again while a target light blinks
_UNKNOWN = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _relay_schema(value: Any) -> dict[int, str]:
    if not isinstance(value, dict) or not value:
        raise vol.Invalid("relay: a mapping of key: switch or light")
    result: dict[int, str] = {}
    for key, entity in value.items():
        try:
            button = vol.All(vol.Coerce(int), vol.In((1, 2, 3)))(key)
        except vol.Invalid as err:
            raise vol.Invalid(f"relay: unknown key '{key}' (1-3)") from err
        result[button] = cv.entity_domain(["switch", "light"])(entity)
    entities = list(result.values())
    twice = sorted({e for e in entities if entities.count(e) > 1})
    if twice:
        raise vol.Invalid(f"relay: {', '.join(twice)} on two keys")
    return result


def check_relay(features: list[tuple[str, Any]], keys: int | None) -> None:
    """Each relay key exists on the device and has a function (raises vol.Invalid)."""
    conf = dict(features)
    relay = conf.get(KEY)
    if not relay:
        return
    used = {e["button"] for e in conf.get(KEY_BUTTONS) or []}
    used |= {int(slot.split("_", 1)[0]) for slot in conf.get(KEY_CUSTOM) or {}}
    for key in relay:
        if keys is not None and key > keys:
            raise vol.Invalid(f"relay: key {key} on a {keys}-key pushbutton")
        if key not in used:
            raise vol.Invalid(f"relay: key {key} has no function")


@register("event")
class Relay(_ButtonBase):
    feature_name = KEY
    schema = staticmethod(_relay_schema)

    _relay_unsub: list[CALLBACK_TYPE] | None = None
    _relay_start_unsub: CALLBACK_TYPE | None = None
    _relay_timer: CALLBACK_TYPE | None = None
    _relay_on: dict[int, bool | None] | None = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return set(conf.values())

    # --- sources -------------------------------------------------------------

    def _relay_functions(self, key: int) -> list[tuple[str, dict[str, Any]]]:
        """(function, args) of every function on a key."""
        result = [
            (e["function"], e["args"])
            for e in self.gw_conf(KEY_BUTTONS) or []
            if e["button"] == key
        ]
        for slot, action in (self.gw_conf(KEY_CUSTOM) or {}).items():
            if int(slot.split("_", 1)[0]) == key:
                result.append(("custom", action))
        return result

    def _relay_ids(self, target: dict[str, Any], domain: str | None) -> list[str]:
        """The target's entities (of a domain), unavailable ones too, without claimed ones."""
        if not target:
            return []
        selected = async_extract_referenced_entity_ids(self.hass, TargetSelection(target))
        claimed = self.hass.data.get(DOMAIN, {}).get(CLAIMS, {})
        return sorted(
            e for e in selected.referenced | selected.indirectly_referenced
            if e not in claimed and (domain is None or e.startswith(f"{domain}."))
        )

    def _relay_sources(self, key: int) -> list[tuple[str, list[str], dict[str, Any]]]:
        """(function, watched entities, args) of a key."""
        result = []
        for function, args in self._relay_functions(key):
            if function == "custom":
                ids = self._relay_ids(dict(args.get("target") or {}), None)
            elif function == "program":
                ids = []
            else:
                ids = self._relay_ids(self._btn_target(args.get("target")), function)
            result.append((function, ids, args))
        return result

    def _relay_state(self, key: int) -> bool | None:
        """Whether the key's function is on; None when nothing is known."""
        known = False
        for function, ids, args in self._relay_sources(key):
            if function == "program":
                running = self._relay_program(args)
                if running is None:
                    continue
                known = True
                if running:
                    return True
                continue
            for entity_id in ids:
                state = self.hass.states.get(entity_id)
                if state is None or state.state in _UNKNOWN:
                    continue
                known = True
                if function == "light" or function == "custom":
                    on = state.state == STATE_ON
                elif function == "climate":
                    on = state.state != HVACMode.OFF
                else:
                    on = state.state != CoverState.CLOSED
                if on:
                    return True
        return False if known else None

    def _relay_program(self, args: dict[str, Any]) -> bool | None:
        from ..program import async_get_manager  # noqa: PLC0415

        manager = async_get_manager(self.hass)
        try:
            return manager.is_running(manager.timeless_uid(args["program"]))
        except Exception:  # noqa: BLE001  (no such program: unknown)
            return None

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._relay_on = {}
        self.async_on_remove(self._relay_stop)
        if any(f == "program" for key in self.gw_conf(KEY) for f, _ in self._relay_functions(key)):
            from ..program import async_get_manager  # noqa: PLC0415

            self.async_on_remove(async_get_manager(self.hass).add_listener(self._relay_schedule))
        if not self.hass.is_running:
            # Watched again once every wrapper (their claims) and target is set up. Not
            # through async_on_remove: removing a listener that already fired is an error.
            self._relay_start_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._relay_started
            )
        self._relay_watch()

    @callback
    def _relay_started(self, _event: Event) -> None:
        self._relay_start_unsub = None
        self._relay_watch()

    @callback
    def _relay_watch(self) -> None:
        """(Re)listen to the relays and every target entity, then sync."""
        for unsub in self._relay_unsub or []:
            unsub()
        watched = set(self.gw_conf(KEY).values())
        for key in self.gw_conf(KEY):
            for _function, ids, _args in self._relay_sources(key):
                watched.update(ids)
        self._relay_unsub = [
            async_track_state_change_event(self.hass, sorted(watched), self._relay_schedule)
        ]
        self._relay_schedule()

    @callback
    def _relay_stop(self) -> None:
        if self._relay_start_unsub is not None:
            self._relay_start_unsub()
            self._relay_start_unsub = None
        for unsub in self._relay_unsub or []:
            unsub()
        self._relay_unsub = None
        if self._relay_timer is not None:
            self._relay_timer()
            self._relay_timer = None

    @callback
    def _relay_schedule(self, *_args: Any, delay: float | None = None) -> None:
        if self._relay_timer is not None:
            self._relay_timer()
        self._relay_timer = async_call_later(
            self.hass, DEBOUNCE if delay is None else delay, self._relay_run
        )

    def _relay_blinking(self) -> bool:
        for key in self.gw_conf(KEY):
            for _function, ids, _args in self._relay_sources(key):
                for entity_id in ids:
                    if getattr(_gungors(self.hass, entity_id), "gw_blinking", False):
                        return True
        return False

    @callback
    def _relay_run(self, _now: Any = None) -> None:
        self._relay_timer = None
        if self._relay_unsub is None:
            return
        if self._relay_blinking():
            self._relay_schedule(delay=RETRY)
            return
        changed = False
        for key, relay in self.gw_conf(KEY).items():
            on = self._relay_state(key)
            if self._relay_on.get(key, "unset") != on:
                self._relay_on[key] = on
                changed = True
            state = self.hass.states.get(relay)
            if on is None or state is None or state.state in _UNKNOWN:
                continue
            if (state.state == STATE_ON) == on:
                continue
            self.hass.async_create_task(
                self.hass.services.async_call(
                    relay.split(".", 1)[0], "turn_on" if on else "turn_off",
                    {ATTR_ENTITY_ID: relay}, blocking=False,
                )
            )
        if changed:
            self.async_write_ha_state()

    # --- state ---------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs["relays"] = {
            key: {ATTR_ENTITY_ID: relay, "on": (self._relay_on or {}).get(key)}
            for key, relay in self.gw_conf(KEY).items()
        }
        return attrs
