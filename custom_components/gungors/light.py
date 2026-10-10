"""Light platform for the gungors integration.

`GungorsLight` knows only Home Assistant's light contract. turn_on / turn_off
become requests that travel down the feature chain:

- turn_on(**kwargs) -> Request({"on": True, **kwargs})
- turn_off(**kwargs) -> Request({"on": False, **kwargs})

The base keeps the on state; the link mirrors everything else (brightness, colour,
capabilities) from the original with ``gw_mirror(attributes)``. A device-originated
request carries the device's attributes and is mirrored the same way.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_XY_COLOR,
    PLATFORM_SCHEMA as LIGHT_PLATFORM_SCHEMA,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .core import BASE_KEY, GungorsEntity, Request
from .platform import async_setup_features_platform, features_schema

DOMAIN_LIGHT = "light"

PLATFORM_SCHEMA = features_schema(DOMAIN_LIGHT, LIGHT_PLATFORM_SCHEMA, {})

# Attributes mirrored from the original: state attribute -> entity attribute.
MIRRORED = {
    ATTR_BRIGHTNESS: "_attr_brightness",
    ATTR_COLOR_MODE: "_attr_color_mode",
    ATTR_COLOR_TEMP_KELVIN: "_attr_color_temp_kelvin",
    ATTR_HS_COLOR: "_attr_hs_color",
    ATTR_XY_COLOR: "_attr_xy_color",
    ATTR_RGB_COLOR: "_attr_rgb_color",
    ATTR_EFFECT: "_attr_effect",
    ATTR_EFFECT_LIST: "_attr_effect_list",
    ATTR_MIN_COLOR_TEMP_KELVIN: "_attr_min_color_temp_kelvin",
    ATTR_MAX_COLOR_TEMP_KELVIN: "_attr_max_color_temp_kelvin",
}
TUPLES = (ATTR_HS_COLOR, ATTR_XY_COLOR, ATTR_RGB_COLOR)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    await async_setup_features_platform(
        hass, config, async_add_entities, DOMAIN_LIGHT, GungorsLight, []
    )


class GungorsLight(GungorsEntity, LightEntity):
    """Feature-agnostic light: the on state and the request chain."""

    def __init__(self, name, unique_id, features, options) -> None:
        super().__init__(name, unique_id, features, options)
        self._attr_is_on: bool | None = None
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF
        self._attr_supported_features = LightEntityFeature(0)

    # --- HA light API -> requests --------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.gw_request(Request({"on": True, **kwargs}))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.gw_request(Request({"on": False, **kwargs}))

    # --- chain ends ------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        if "on" in request.data:
            self._attr_is_on = bool(request.data["on"])
        if request.from_device:
            self.gw_mirror(request.data)
        await super().gw_apply(request)
        self.async_write_ha_state()

    def gw_mirror(self, attributes: dict[str, Any]) -> None:
        """Take the original's attributes and capabilities."""
        modes = attributes.get(ATTR_SUPPORTED_COLOR_MODES)
        if modes:
            self._attr_supported_color_modes = {ColorMode(m) for m in modes}
        if "supported_features" in attributes:
            self._attr_supported_features = LightEntityFeature(
                int(attributes["supported_features"] or 0)
            )
        for key, attr in MIRRORED.items():
            if key in attributes:
                value = attributes[key]
                if key in TUPLES and value is not None:
                    value = tuple(value)
                setattr(self, attr, value)
        mode = self._attr_color_mode
        supported = self._attr_supported_color_modes or {ColorMode.ONOFF}
        if mode not in supported:
            # A group member in a mode the group does not list (or none while off).
            for candidate in (ColorMode.COLOR_TEMP, ColorMode.XY, ColorMode.HS,
                              ColorMode.BRIGHTNESS, ColorMode.ONOFF):
                if candidate in supported:
                    self._attr_color_mode = candidate
                    break
            else:
                self._attr_color_mode = next(iter(supported))

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        base = extra.get(BASE_KEY)
        if isinstance(base, dict) and base.get("on") is not None:
            self._attr_is_on = bool(base["on"])
        elif last is not None:
            self._attr_is_on = last.state == STATE_ON

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[BASE_KEY] = {"on": self._attr_is_on}
        return data
