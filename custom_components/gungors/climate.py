"""Climate platform for the gungors integration.

`GungorsClimate` knows only Home Assistant's climate contract: an HVAC mode
(heat/off), a target temperature and a heating demand. Service calls become
requests that travel down the feature chain:

- set_hvac_mode -> Request({"hvac_mode": m}); leaving OFF also restores the target
  from before OFF.
- set_temperature -> Request({"temperature": t}); while OFF it also switches to heat.

Climate-specific hooks (on top of the GungorsEntity ones):

- ``gw_temperature_changed()``: the room temperature changed (``_gw_current_temp``).
- ``gw_demand(value)``: heating demand 0-100 from a controller; outputs (valve,
  pwm) act on it. The base stores it.
- ``gw_heating`` (property): whether heat is being delivered (hvac_action).
- ``gw_device_min_temp()``: a minimum temperature reported by the device.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    PLATFORM_SCHEMA as CLIMATE_PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import ATTR_PRE_OFF_TARGET_TEMP, TEMP_TOLERANCE
from .core import BASE_KEY, GungorsEntity, Request, as_float
from .platform import async_setup_features_platform, features_schema

DOMAIN_CLIMATE = "climate"

CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TARGET_TEMP = "target_temp"
CONF_TARGET_TEMP_STEP = "target_temp_step"
CONF_PRECISION = "precision"
OPTION_KEYS = [CONF_MIN_TEMP, CONF_MAX_TEMP, CONF_TARGET_TEMP, CONF_TARGET_TEMP_STEP,
               CONF_PRECISION]

DEFAULT_MIN_TEMP = 7.0
DEFAULT_MAX_TEMP = 35.0

# Extra restore data key of the old SyncThermostat (last state while available).
LEGACY_LAST_AVAILABLE_STATE = "last_available_state"

PLATFORM_SCHEMA = features_schema(
    DOMAIN_CLIMATE,
    CLIMATE_PLATFORM_SCHEMA,
    {
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_STEP): vol.Coerce(float),
        vol.Optional(CONF_PRECISION): vol.In([0.1, 0.5, 1.0]),
    },
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    await async_setup_features_platform(
        hass, config, async_add_entities, DOMAIN_CLIMATE, GungorsClimate, OPTION_KEYS
    )


class GungorsClimate(GungorsEntity, ClimateEntity):
    """Feature-agnostic climate: mode, target, demand and the request chain."""

    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, name, unique_id, features, options) -> None:
        super().__init__(name, unique_id, features, options)
        self._gw_hvac_mode: HVACMode | None = None
        self._gw_target_temp: float | None = None
        self._gw_pre_off: float | None = None
        self._gw_current_temp: float | None = None
        self._gw_demand: float = 0
        self._attr_target_temperature_step = options.get(CONF_TARGET_TEMP_STEP)
        if options.get(CONF_PRECISION) is not None:
            self._attr_precision = options[CONF_PRECISION]

    async def async_get_last_state(self) -> State | None:
        """Last state; a legacy unavailable one is replaced by its saved snapshot."""
        state = await super().async_get_last_state()
        if state is None or state.state != STATE_UNAVAILABLE:
            return state
        extra = await self.async_get_last_extra_data()
        snapshot = (extra.as_dict() if extra is not None else {}).get(LEGACY_LAST_AVAILABLE_STATE)
        restored = State.from_dict(snapshot) if snapshot else None
        return restored or state

    # --- HA climate API -> requests ------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        data: dict[str, Any] = {"hvac_mode": HVACMode(hvac_mode)}
        if (
            self._gw_hvac_mode == HVACMode.OFF
            and hvac_mode != HVACMode.OFF
            and self._gw_pre_off is not None
        ):
            data["temperature"] = self._gw_pre_off
        await self.gw_request(Request(data))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = as_float(kwargs.get(ATTR_TEMPERATURE))
        if temperature is None:
            return
        data: dict[str, Any] = {"temperature": temperature}
        mode = kwargs.get("hvac_mode")
        if mode is not None:
            data["hvac_mode"] = HVACMode(mode)
        elif self._gw_hvac_mode == HVACMode.OFF:
            data["hvac_mode"] = HVACMode.HEAT
        await self.gw_request(Request(data))

    # --- chain ends ------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        mode = request.data.get("hvac_mode")
        temperature = as_float(request.data.get("temperature"))
        if mode is not None:
            mode = HVACMode(mode)
            if (
                mode == HVACMode.OFF
                and self._gw_hvac_mode != HVACMode.OFF
                and self._gw_target_temp is not None
            ):
                # Remember the target so leaving OFF restores it.
                self._gw_pre_off = self._gw_target_temp
            self._gw_hvac_mode = mode
        if temperature is not None:
            self._gw_target_temp = temperature
        await super().gw_apply(request)
        self.async_write_ha_state()

    def gw_temperature_changed(self) -> None:
        """End of the temperature chain."""

    async def gw_demand(self, value: float) -> None:
        """End of the demand chain: keep the value."""
        self._gw_demand = value
        self.async_write_ha_state()

    @property
    def gw_heating(self) -> bool:
        return self._gw_demand > 0

    def gw_device_min_temp(self) -> float | None:
        return None

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        base = extra.get(BASE_KEY)
        if isinstance(base, dict):
            mode, target, pre_off = base.get("hvac_mode"), base.get("target"), base.get("pre_off")
        elif last is not None:
            mode = last.state
            target = last.attributes.get(ATTR_TEMPERATURE)
            pre_off = last.attributes.get(ATTR_PRE_OFF_TARGET_TEMP)
        else:
            mode = target = pre_off = None
        try:
            self._gw_hvac_mode = HVACMode(mode) if mode in self.hvac_modes else None
        except ValueError:
            self._gw_hvac_mode = None
        self._gw_target_temp = as_float(target)
        self._gw_pre_off = as_float(pre_off)
        if self._gw_target_temp is None:
            self._gw_target_temp = self.gw_options.get(CONF_TARGET_TEMP) or self.min_temp
        if self._gw_hvac_mode is None:
            self._gw_hvac_mode = HVACMode.OFF

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[BASE_KEY] = {
            "hvac_mode": self._gw_hvac_mode,
            "target": self._gw_target_temp,
            "pre_off": self._gw_pre_off,
        }
        return data

    # --- state -----------------------------------------------------------------

    @property
    def hvac_mode(self) -> HVACMode | None:
        return self._gw_hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._gw_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if self.gw_heating else HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        return self._gw_target_temp

    @property
    def current_temperature(self) -> float | None:
        return self._gw_current_temp

    @property
    def min_temp(self) -> float:
        configured = self.gw_options.get(CONF_MIN_TEMP)
        if configured is not None:
            return configured
        device = self.gw_device_min_temp()
        return device if device is not None else DEFAULT_MIN_TEMP

    @property
    def max_temp(self) -> float:
        configured = self.gw_options.get(CONF_MAX_TEMP)
        return configured if configured is not None else DEFAULT_MAX_TEMP

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_PRE_OFF_TARGET_TEMP] = self._gw_pre_off
        return attrs


def temps_differ(a: float | None, b: float | None, tolerance: float = TEMP_TOLERANCE) -> bool:
    if a is None or b is None:
        return a != b
    return abs(a - b) > tolerance
