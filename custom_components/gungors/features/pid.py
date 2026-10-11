"""`pid`: heating demand (0-100) from a PID controller.

The control loop of ScratMan's SmartThermostat with its PID class
(vendor/pid_controller.py). Needs `temperature`.

    - pid:
        kp: 16
        ki: 0.01
        kd: 4
        keep_alive: {minutes: 15}   # re-applies the output periodically
        force_off: false            # while OFF, leave the output as it is
        sensor_stall: 0             # s without a sample before output_safety (0: off)

- A new temperature sample, a new target and a new mode recalculate the output;
  keep_alive re-applies the last one. The output goes down ``gw_demand()``.
- Switching to OFF sets the demand to 0 once; with `force_off` (default) an output
  that is still active while OFF is set to 0 again on each cycle.
- The loop pauses while the entity is unavailable.
- Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`, `gungors.clear_integral`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import HVACMode
from homeassistant.core import State, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from ..core import Feature, Request, as_float, register
from ..vendor.pid_controller import PID

_LOGGER = logging.getLogger(__name__)

KEY = "pid"
OUT_MIN = 0
OUT_MAX = 100

SCHEMA = vol.Schema(
    {
        vol.Required("kp"): vol.Coerce(float),
        vol.Required("ki"): vol.Coerce(float),
        vol.Required("kd"): vol.Coerce(float),
        vol.Optional("ke", default=0.0): vol.Coerce(float),
        vol.Optional("keep_alive"): cv.positive_time_period,
        vol.Optional("force_off", default=True): cv.boolean,
        vol.Optional("sensor_stall", default=0): vol.All(
            cv.time_period, lambda t: t.total_seconds()
        ),
        vol.Optional("output_safety", default=5.0): vol.Coerce(float),
        vol.Optional("output_precision", default=1): vol.Coerce(int),
        vol.Optional("cold_tolerance", default=0.3): vol.Coerce(float),
        vol.Optional("hot_tolerance", default=0.3): vol.Coerce(float),
        vol.Optional("debug", default=False): cv.boolean,
    }
)

GAIN_SCHEMA = {vol.Optional(k): vol.Coerce(float) for k in ("kp", "ki", "kd", "ke")}
MODE_SCHEMA = {vol.Required("mode"): vol.In(["auto", "off"])}
INTEGRAL_SCHEMA = {vol.Optional("value", default=0.0): vol.Coerce(float)}


@register("climate")
class Pid(Feature):
    feature_name = KEY
    schema = SCHEMA
    requires = frozenset({"current_temperature"})
    provides = frozenset({"demand"})
    services = {
        "set_pid_gain": (GAIN_SCHEMA, "async_set_pid_gain"),
        "set_pid_mode": (MODE_SCHEMA, "async_set_pid_mode"),
        "clear_integral": (INTEGRAL_SCHEMA, "async_clear_integral"),
    }

    _pid_ctrl: PID | None = None
    _pid_lock: asyncio.Lock | None = None
    _pid_active = False
    _pid_output: float = 0
    _pid_control: float = 0
    _pid_gains: dict[str, float] | None = None
    _pid_p = _pid_i = _pid_d = _pid_dt = 0.0

    def _pid_setup(self) -> None:
        if self._pid_ctrl is not None:
            return
        conf = self.gw_conf(KEY)
        self._pid_gains = {k: conf[k] for k in ("kp", "ki", "kd", "ke")}
        self._pid_ctrl = PID(
            conf["kp"], conf["ki"], conf["kd"], conf["ke"], OUT_MIN, OUT_MAX, 0,
            conf["cold_tolerance"], conf["hot_tolerance"],
        )
        self._pid_ctrl.mode = "AUTO"
        self._pid_lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        self._pid_setup()
        await super().async_added_to_hass()
        keep_alive = self.gw_conf(KEY).get("keep_alive")
        if keep_alive:
            self.async_on_remove(
                async_track_time_interval(self.hass, self._pid_keep_alive, keep_alive)
            )
        self.async_on_remove(async_at_started(self.hass, self._pid_started))

    async def _pid_started(self, _hass) -> None:
        await self.gw_pid_run(calc=True)

    async def _pid_keep_alive(self, _now) -> None:
        await self.gw_pid_run(calc=False)

    # --- chain ---------------------------------------------------------------

    def gw_temperature_changed(self) -> None:
        super().gw_temperature_changed()
        self.hass.async_create_task(self.gw_pid_run(calc=True))

    async def gw_apply(self, request: Request) -> None:
        previous_mode = self._gw_hvac_mode
        await super().gw_apply(request)
        if "hvac_mode" not in request.data and "temperature" not in request.data:
            return
        if self._gw_hvac_mode == HVACMode.OFF:
            if previous_mode != HVACMode.OFF:
                # Leaving heat: output to 0 once, forget the samples so the
                # off period is not integrated.
                self._pid_control = OUT_MIN
                self._gw_temp_prev_time = None
                self._pid_ctrl.clear_samples()
                if self.available:
                    await self.gw_demand(self._pid_control)
            return
        await self.gw_pid_run(calc=True)

    @callback
    def gw_device_available(self, available: bool) -> None:
        if not available:
            # Don't integrate over the unavailable period once we resume.
            self._gw_temp_prev_time = None
            self._pid_ctrl.clear_samples()
        else:
            self.hass.async_create_task(self.gw_pid_run(calc=True))
        super().gw_device_available(available)

    # --- control loop --------------------------------------------------------

    async def gw_pid_run(self, calc: bool) -> None:
        """One control cycle (ScratMan's _async_control_heating)."""
        if not self.available or self._pid_lock is None:
            return
        conf = self.gw_conf(KEY)
        async with self._pid_lock:
            current, target = self._gw_current_temp, self._gw_target_temp
            if not self._pid_active and None not in (current, target):
                self._pid_active = True
                _LOGGER.info(
                    "%s: temperature %s, set point %s: PID active", self.entity_id, current, target
                )
            if not self._pid_active or self._gw_hvac_mode == HVACMode.OFF:
                if (
                    conf["force_off"]
                    and self._gw_hvac_mode == HVACMode.OFF
                    and self.gw_heating
                ):
                    self._pid_control = OUT_MIN
                    await self.gw_demand(self._pid_control)
                self.async_write_ha_state()
                return

            stall = conf["sensor_stall"]
            if stall and time.time() - self._gw_temp_updated > stall:
                self._pid_control = conf["output_safety"]
                await self.gw_demand(self._pid_control)
                return
            if calc:
                self._pid_calc()

            precision = conf["output_precision"]
            output = round(self._pid_output, precision)
            if not precision:
                output = int(output)
            self._pid_control = max(min(output, OUT_MAX), OUT_MIN)
            await self.gw_demand(self._pid_control)

    def _pid_calc(self) -> None:
        now = time.time()
        if self._gw_temp_prev_time is None:
            self._gw_temp_prev_time = now
        if self._gw_temp_time is None:
            self._gw_temp_time = now
        if self._gw_temp_prev_time > self._gw_temp_time:
            self._gw_temp_prev_time = self._gw_temp_time
        ctrl = self._pid_ctrl
        output, update = ctrl.calc(
            self._gw_current_temp, self._gw_target_temp,
            self._gw_temp_time, self._gw_temp_prev_time, None,
        )
        precision = self.gw_conf(KEY)["output_precision"]
        self._pid_p = round(ctrl.proportional, precision)
        self._pid_i = round(ctrl.integral, precision)
        self._pid_d = round(ctrl.derivative, precision)
        self._pid_dt = ctrl.dt
        self._pid_output = round(output, precision)
        if not precision:
            self._pid_output = int(self._pid_output)
        if update:
            _LOGGER.debug(
                "%s: PID output %s (error %.2f, dt %.2f, p %.2f, i %.2f, d %.2f)",
                self.entity_id, self._pid_output, ctrl.error, self._pid_dt,
                self._pid_p, self._pid_i, self._pid_d,
            )

    # --- services ------------------------------------------------------------

    async def async_set_pid_gain(self, **gains: float) -> None:
        for key, value in gains.items():
            if value is not None:
                self._pid_gains[key] = float(value)
        self._pid_ctrl.set_pid_param(**self._pid_gains)
        await self.gw_pid_run(calc=True)

    async def async_set_pid_mode(self, mode: str) -> None:
        self._pid_ctrl.mode = mode.upper()
        await self.gw_pid_run(calc=True)

    async def async_clear_integral(self, value: float = 0.0) -> None:
        self._pid_ctrl.integral = float(value)
        self._pid_i = self._pid_ctrl.integral
        self.async_write_ha_state()

    # --- restore -------------------------------------------------------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        self._pid_setup()
        own = extra.get(KEY)
        if not isinstance(own, dict):
            # Legacy SmartThermostat attributes.
            attrs = last.attributes if last is not None else {}
            own = {
                "integral": attrs.get("pid_i"),
                "mode": attrs.get("pid_mode"),
                **{k: attrs.get(k) for k in ("kp", "ki", "kd", "ke")},
            }
        for key in ("kp", "ki", "kd", "ke"):
            value = as_float(own.get(key))
            if value is not None:
                self._pid_gains[key] = value
        self._pid_ctrl.set_pid_param(**self._pid_gains)
        integral = as_float(own.get("integral"))
        if integral is not None:
            self._pid_i = integral
            self._pid_ctrl.integral = integral
        if str(own.get("mode")).upper() in ("AUTO", "OFF"):
            self._pid_ctrl.mode = str(own["mode"]).upper()

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {
            **(self._pid_gains or {}),
            "integral": self._pid_ctrl.integral if self._pid_ctrl else None,
            "mode": self._pid_ctrl.mode if self._pid_ctrl else None,
        }
        return data

    # --- state ---------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        gains = self._pid_gains or {}
        attrs.update(
            {
                "control_output": self._pid_control,
                "kp": gains.get("kp"),
                "ki": gains.get("ki"),
                "kd": gains.get("kd"),
                "ke": gains.get("ke"),
                "pid_mode": self._pid_ctrl.mode.lower() if self._pid_ctrl else "off",
                "pid_i": self._pid_i,
            }
        )
        if self.gw_conf(KEY)["debug"]:
            attrs.update(
                {"pid_p": self._pid_p, "pid_d": self._pid_d, "pid_e": 0, "pid_dt": self._pid_dt}
            )
        return attrs
