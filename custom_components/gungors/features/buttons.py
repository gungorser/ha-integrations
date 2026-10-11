"""`buttons`: physical wall buttons that reach the cover through Home Assistant.

The pushbutton blueprint fires `gungors_physical_cover` (entity_id, action
open/close) for covers whose `physical_buttons` attribute is true. A press is a
physical request: features let it through as such (the window feature does not
hold it, a hold in manual mode lets it change the position).
"""
from __future__ import annotations

from typing import Any

from homeassistant.core import Event, callback

from ..const import ATTR_PHYSICAL_BUTTONS, EVENT_PHYSICAL_COVER
from ..core import SOURCE_PHYSICAL, Feature, Request, register

KEY = "buttons"


@register("cover")
class Buttons(Feature):
    feature_name = KEY

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_PHYSICAL_COVER, self._buttons_event)
        )

    @callback
    def _buttons_event(self, event: Event) -> None:
        ids = event.data.get("entity_id", [])
        if isinstance(ids, str):
            ids = [ids]
        action = event.data.get("action")
        if self.entity_id not in ids or action not in ("open", "close") or not self.available:
            return
        position = 100 if action == "open" else 0
        self.hass.async_create_task(
            self.gw_request(Request({"position": position}, source=SOURCE_PHYSICAL))
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PHYSICAL_BUTTONS] = True
        return attrs
