"""Event platform for the gungors integration: pushbuttons.

`GungorsButton` is an event entity that mirrors a button device's own event
entity (the `link`, a Zigbee2MQTT `event.<device>_action`) and acts on its
presses through features (features/button.py):

```yaml
event:
  - platform: gungors
    name: sercan_pushbutton
    target: {area_id: sercan}     # optional; the linked device's area otherwise
    features:
      - buttons:
          1: light
          2: climate              # or {climate: {program: "Bebek uyku"}}
          3: cover
      - custom:
          2_hold: {action: switch.toggle, target: {entity_id: switch.sercan_plug}}
      - link: event.sercan_pushbutton_action
```

A press is a request ``{"event_type": "<button>_<gesture>"}``: every press of the
original goes through the features and is shown as the wrapper's own event (with
the same event type). Gestures are `single`, `double` and `hold`; a one-button
model's plain `single` / `double` / `hold` are button 1's (`1_single`, ...).
The wrapper keeps no state of its own besides the last event (as every event
entity does).
"""
from __future__ import annotations

import voluptuous as vol

from homeassistant.components.event import (
    PLATFORM_SCHEMA as EVENT_PLATFORM_SCHEMA,
    EventEntity,
)
from homeassistant.const import ATTR_AREA_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .core import GungorsEntity, Request
from .platform import async_setup_features_platform, features_schema

DOMAIN_EVENT = "event"
CONF_TARGET = "target"

GESTURES = ("single", "double", "hold")
BUTTONS = (1, 2, 3)
SLOTS = [f"{b}_{g}" for b in BUTTONS for g in GESTURES]

TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)

PLATFORM_SCHEMA = features_schema(
    DOMAIN_EVENT, EVENT_PLATFORM_SCHEMA, {vol.Optional(CONF_TARGET): TARGET_SCHEMA}
)


def slot_of(event_type: str) -> str:
    """`single` (one-button model) -> `1_single`; `2_hold` stays."""
    return f"1_{event_type}" if event_type in GESTURES else event_type


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    await async_setup_features_platform(
        hass, config, async_add_entities, DOMAIN_EVENT, GungorsButton, [CONF_TARGET]
    )


class GungorsButton(GungorsEntity, EventEntity):
    """Feature-agnostic pushbutton: shows each press as an event."""

    def __init__(self, name, unique_id, features, options) -> None:
        super().__init__(name, unique_id, features, options)
        self._attr_event_types = list(SLOTS)

    async def gw_apply(self, request: Request) -> None:
        event_type = request.data.get("event_type")
        if event_type:
            if event_type not in self._attr_event_types:
                self._attr_event_types = [*self._attr_event_types, event_type]
            self._trigger_event(event_type)
            self.async_write_ha_state()
        await super().gw_apply(request)

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        # The event entity's own (last event type); a button keeps nothing else.
        return EventEntity.extra_restore_state_data.fget(self)
