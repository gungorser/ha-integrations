"""`link` for pushbuttons: the button device's own event entity.

`link: event.sercan_pushbutton_action` (Zigbee2MQTT's event entity of the device).

- Every new event of the original (its state is the event's time) is a press:
  a physical request ``{"event_type": ...}`` through the features. Zigbee2MQTT
  splits `2_hold` into event type `hold` and attribute `button: "2"`; both become
  the slot `2_hold` (`single` without a button is `1_single`).
- The wrapper is available while the original is, and takes its event types.
- The state the original has when Home Assistant starts (its last event), and
  the same event again after the original was unavailable, are not presses.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..core import Feature, register

_LOGGER = logging.getLogger(__name__)

KEY = "link"


@register("event")
class EventLink(Feature):
    feature_name = KEY
    schema = cv.entity_domain("event")
    last = True

    _link_available = False
    _link_last: str | None = None  # the original's last event (its state) seen

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        # Read only: the wrapper never writes the original.
        return set()

    def _link_ok(self) -> bool:
        state = self.hass.states.get(self.gw_conf(KEY))
        return state is not None and state.state != STATE_UNAVAILABLE

    def _link_types(self) -> None:
        from ..event import slot_of  # noqa: PLC0415

        state = self.hass.states.get(self.gw_conf(KEY))
        types = (state.attributes.get("event_types") if state else None) or []
        for slot in (slot_of(t) for t in types):
            if slot not in self._attr_event_types:
                self._attr_event_types = [*self._attr_event_types, slot]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._link_available = self._link_ok()
        state = self.hass.states.get(self.gw_conf(KEY))
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._link_last = state.state
        self._link_types()
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.gw_conf(KEY)], self._link_changed)
        )

    @callback
    def _link_changed(self, event: Event) -> None:
        from ..event import slot_of  # noqa: PLC0415

        new = event.data.get("new_state")
        available = self._link_ok()
        if available != self._link_available:
            self._link_available = available
            self.gw_device_available(available)
            self._link_types()
            self.async_write_ha_state()
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if new.state == self._link_last:
            return  # an attribute change, or the same event back after unavailable
        self._link_last = new.state
        event_type = new.attributes.get("event_type")
        if not event_type:
            return
        button = new.attributes.get("button")
        slot = f"{button}_{event_type}" if button else slot_of(str(event_type))
        self.hass.async_create_task(self.gw_physical({"event_type": slot}))

    @property
    def available(self) -> bool:
        return self._link_available

    @property
    def gw_link_entity(self) -> str:
        return self.gw_conf(KEY)
