"""Cover platform for the gungors integration.

`GungorsCover` knows only Home Assistant's cover contract. It turns the cover
services into requests that travel down the feature chain:

- open / close / set_position -> Request({"position": p})
- stop -> Request({"stop": True})

Its position is the device's (room frame): it changes only with requests from
the device (``from_device``), i.e. what the link reports. With only `link` the
wrapper is the original; features change what is shown (an estimate while
moving, a pending command) and what is sent.

Cover-specific hooks (on top of the GungorsEntity ones):

- ``gw_device_report(position)``: the link saw a new device position (room frame);
  the link decides whether it is the result of our command or physical.
- ``gw_baseline(position)``: first valid device position after a start or after
  the device came back; the base takes it.
- ``gw_mirror(position)``: take a device position as the wrapper's (no request).
- ``gw_to_device(position)`` / ``gw_from_device(position)``: room <-> device frame.
- ``gw_device_command(position) -> (service, data)``: how the link moves the device.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.cover import (
    ATTR_POSITION,
    DEVICE_CLASSES_SCHEMA,
    PLATFORM_SCHEMA as COVER_PLATFORM_SCHEMA,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import CONF_DEVICE_CLASS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .core import GungorsEntity, Request
from .platform import async_setup_features_platform, features_schema

DOMAIN_COVER = "cover"

PLATFORM_SCHEMA = features_schema(
    DOMAIN_COVER,
    COVER_PLATFORM_SCHEMA,
    {vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA},
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    await async_setup_features_platform(
        hass, config, async_add_entities, DOMAIN_COVER, GungorsCover, [CONF_DEVICE_CLASS]
    )


class GungorsCover(GungorsEntity, CoverEntity):
    """Feature-agnostic cover: the device's position and the request chain."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, name, unique_id, features, options) -> None:
        super().__init__(name, unique_id, features, options)
        self._attr_device_class = options.get(CONF_DEVICE_CLASS)
        self._gw_position: int | None = None

    # --- HA cover API -> requests ------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self.gw_request(Request({"position": 100}))

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self.gw_request(Request({"position": 0}))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = max(0, min(100, int(kwargs[ATTR_POSITION])))
        await self.gw_request(Request({"position": position}))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.gw_request(Request({"stop": True}))

    # --- chain ends --------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        position = request.data.get("position")
        if position is not None and request.from_device:
            self._gw_position = int(round(position))
        await super().gw_apply(request)
        self.async_write_ha_state()

    @callback
    def gw_device_report(self, position: float | None) -> None:
        """End of the report chain (the link handles reports)."""

    @callback
    def gw_mirror(self, position: float) -> None:
        self._gw_position = int(round(position))

    @callback
    def gw_baseline(self, position: float) -> None:
        """After a start the device's position is the wrapper's."""
        self.gw_mirror(position)
        self.async_write_ha_state()

    def gw_to_device(self, position: float) -> float:
        return position

    def gw_from_device(self, position: float) -> float:
        return position

    def gw_device_command(self, position: float) -> tuple[str, dict[str, Any]]:
        return "set_cover_position", {ATTR_POSITION: int(round(position))}

    # --- state -------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        return self._gw_position

    @property
    def is_closed(self) -> bool | None:
        position = self.current_cover_position
        if position is None:
            return None
        return position <= 0

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False
