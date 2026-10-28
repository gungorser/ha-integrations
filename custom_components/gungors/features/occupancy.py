"""`occupancy`: a motion sensor lights the light, and darkens it again if it lit it.

`occupancy: binary_sensor.x_occupancy`, or
`occupancy: {sensor, brightness_pct, transition, after_sunset_only}`:

- `sensor`: the motion sensor (on = motion).
- `brightness_pct` (default 50): brightness of the light the sensor turns on.
- `transition` (default 2): seconds of the turn-on transition.
- `after_sunset_only` (default true): only turn the light on while `sun.sun` is
  below the horizon. False: at any time of the day.

Put it after `program` and before `link`: its own requests then go through the
program rules like any other request, and the link sends them.

The events of the light are of two kinds: the sensor's and everything else. A
request that does not come from this feature (a dashboard, the app, a pushbutton,
an automation, a program, the device changing by itself) counts as the user's.

The light is owned by the sensor or by the user:

- Motion starts (sensor off -> on; unknown and unavailable are ignored) while the
  light is off, and it is dark enough (see `after_sunset_only`): the light is turned
  on and the sensor owns it. A light that is already on is never touched: being on
  wins over motion, and it belongs to whoever turned it on.
- Any request that is not the sensor's gives the light to the user. So does
  turning the light on by hand, while it is lit by the sensor.
- Motion ends (sensor on -> off) while the sensor owns the light: it is turned
  off. A light the user owns stays as it is.
- The user turns the light off during motion: it stays off until the next motion
  (a new off -> on of the sensor), and the sensor owns it again once it is off.

Ownership is not stored. After a restart a light that is on is the user's, one
that is off belongs to the sensor.

The link reports the result of a command within a few seconds as the device's own
state. A late report (a slow Zigbee device) that only confirms the last request of
this feature, within 15 s, is not taken as the user's.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from ..core import SOURCE_INTERNAL, Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "occupancy"
TAG = "occupancy"
# s within which a device report that only confirms our last request is its echo
ECHO_GRACE = 15.0

_OPTIONS = vol.Schema(
    {
        vol.Required("sensor"): cv.entity_domain("binary_sensor"),
        vol.Optional("brightness_pct", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional("transition", default=2): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=300)
        ),
        vol.Optional("after_sunset_only", default=True): cv.boolean,
    }
)


def _schema(value: Any) -> dict[str, Any]:
    """`occupancy: binary_sensor.x` or the mapping of options."""
    if isinstance(value, str):
        value = {"sensor": value}
    return _OPTIONS(value)


@register("light")
class Occupancy(Feature):
    feature_name = KEY
    schema = _schema

    _occ_owned = False  # the sensor lit the light and nobody has taken it over
    _occ_sent_at: float | None = None  # our last request
    _occ_sent_on: bool | None = None

    @property
    def _occ_conf(self) -> dict[str, Any]:
        return self.gw_conf(KEY)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._occ_conf["sensor"]], self._occ_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self.entity_id], self._occ_self_changed)
        )

    # --- chain ---------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        if TAG in request.tags:
            accepted = await super().gw_request(request)
            on = bool(request.data.get("on"))  # a program may have changed it
            self._occ_sent_at = time.monotonic()
            self._occ_sent_on = on
            self._occ_owned = bool(accepted and on)
            return accepted
        if not (request.from_device and self._occ_is_echo(request)):
            self._occ_owned = False  # anything else is the user
        return await super().gw_request(request)

    def _occ_is_echo(self, request: Request) -> bool:
        """A late device report that only confirms what this feature just asked for."""
        if self._occ_sent_at is None or self._occ_sent_on is None:
            return False
        return (
            time.monotonic() - self._occ_sent_at < ECHO_GRACE
            and bool(request.data.get("on")) == self._occ_sent_on
        )

    # --- events --------------------------------------------------------------

    @callback
    def _occ_self_changed(self, event: Event) -> None:
        """The light going off ends any ownership (the report path skips the chain)."""
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if (
            self._occ_owned
            and old is not None
            and old.state == "on"
            and new is not None
            and new.state == "off"
        ):
            self._occ_owned = False

    @callback
    def _occ_sensor_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None:
            return
        if old.state == "off" and new.state == "on":
            self.hass.async_create_task(self._occ_motion_start())
        elif old.state == "on" and new.state == "off":
            self.hass.async_create_task(self._occ_motion_end())

    async def _occ_motion_start(self) -> None:
        if not self.available or self.is_on is not False:
            return  # a light that is on wins over motion
        conf = self._occ_conf
        if conf["after_sunset_only"]:
            sun = self.hass.states.get("sun.sun")
            if sun is None or sun.state != "below_horizon":
                return
        self._occ_owned = False
        data = {
            "on": True,
            "brightness": round(conf["brightness_pct"] * 255 / 100),
            "transition": conf["transition"],
        }
        _LOGGER.debug("%s: motion, turning on %s", self.entity_id, data)
        await self.gw_request(Request(data, source=SOURCE_INTERNAL, tags={TAG}))

    async def _occ_motion_end(self) -> None:
        if not self._occ_owned or not self.available:
            return
        # Also when the light has not reported on yet: the device keeps the order.
        _LOGGER.debug("%s: motion ended, turning off", self.entity_id)
        await self.gw_request(Request({"on": False}, source=SOURCE_INTERNAL, tags={TAG}))
