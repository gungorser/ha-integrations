"""`travel_time`: position estimation for curtain motors that report only at the end.

For Tuya curtain motors via Zigbee2MQTT (observed on the salon curtain):

- No position updates while moving and no reliable moving state.
- The real position arrives only when a move ends by itself or is stopped. A
  position command is first echoed back as the target (`running: true`), and
  Zigbee2MQTT re-publishes that echo ~3 s later with `running: false`.
- After a power loss the first full run ends with a bogus report of the end
  opposite to the command; from the next run on reports are correct.
- HA's MQTT cover drops repeated identical positions, so this feature listens to
  the Zigbee2MQTT device topic directly.

The feature estimates the position while moving from a learned full-run time per
motor direction (start 10 s, learned from every move of at least 30% between two
real reports), opens/closes fully with open_cover/close_cover, and replaces the
estimate with every real report. Use it with `invert` when the motor's open is the
room's close; learned times stay in the motor's frame.

`travel_time:` alone, or `travel_time: {z2m_base_topic: zigbee2mqtt}`. The link must
be a single Zigbee2MQTT cover.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.components import mqtt
from homeassistant.core import State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from ..const import (
    ATTR_CALIBRATED,
    ATTR_CLOSE_TIME,
    ATTR_OPEN_TIME,
    ATTR_POSITION_SOURCE,
    DEFAULT_TRAVEL_TIME,
    DEFAULT_Z2M_BASE_TOPIC,
    MIN_LEARN_DISTANCE,
    POSITION_TOLERANCE,
)
from ..core import Feature, Request, as_float, register

_LOGGER = logging.getLogger(__name__)

KEY = "travel_time"
CONF_Z2M_BASE_TOPIC = "z2m_base_topic"

TICK = timedelta(milliseconds=500)
STOP_REPORT_WAIT = 3  # s to wait for the real position after a stop
COMMAND_GRACE = 1.0  # s after a command in which stray reports are ignored
REPUBLISH_WINDOW = 3.8  # s after a position echo in which Z2M re-publishes it
SOURCE_REPORTED = "reported"
SOURCE_ESTIMATED = "estimated"

_IDLE = "idle"
_MOVING = "moving"
_STOPPING = "stopping"

_CMD_OPEN = "open"
_CMD_CLOSE = "close"
_CMD_POSITION = "position"


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= POSITION_TOLERANCE


def _schema(value: Any) -> dict[str, str]:
    value = vol.Schema(
        vol.Any(None, {vol.Optional(CONF_Z2M_BASE_TOPIC, default=DEFAULT_Z2M_BASE_TOPIC): str})
    )(value)
    return value or {CONF_Z2M_BASE_TOPIC: DEFAULT_Z2M_BASE_TOPIC}


@register("cover")
class TravelTime(Feature):
    feature_name = KEY
    schema = _schema

    _tt_position: float | None = None
    _tt_source = SOURCE_REPORTED
    _tt_calibrated = True
    _tt_motor_open_time = float(DEFAULT_TRAVEL_TIME)
    _tt_motor_close_time = float(DEFAULT_TRAVEL_TIME)
    _tt_restored = False

    _tt_state = _IDLE
    _tt_cmd: str | None = None
    _tt_target = 0.0
    _tt_start_pos = 0.0
    _tt_start_time = 0.0
    _tt_echo_at: float | None = None
    _tt_republish_skipped = False
    _tt_start_reported = False
    _tt_our_stop = False
    _tt_tick_unsub = None
    _tt_deadline_unsub = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {"motion"}

    # --- frames --------------------------------------------------------------

    @property
    def _tt_inverted(self) -> bool:
        return self.gw_to_device(100) < 50

    def _tt_travel_time(self, room_opening: bool) -> float:
        motor_opening = room_opening != self._tt_inverted
        return self._tt_motor_open_time if motor_opening else self._tt_motor_close_time

    def _tt_learn(self, room_opening: bool, full_run: float) -> None:
        if room_opening != self._tt_inverted:
            self._tt_motor_open_time = full_run
        else:
            self._tt_motor_close_time = full_run

    def gw_device_command(self, position: float) -> tuple[str, dict[str, Any]]:
        # Full runs use open/close: the motor ends them itself and reports.
        if position >= 100:
            return "open_cover", {}
        if position <= 0:
            return "close_cover", {}
        return super().gw_device_command(position)

    # --- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        friendly = self._tt_friendly_name()
        if friendly is None:
            _LOGGER.error(
                "%s: %s is not a Zigbee2MQTT device, cannot follow its reports",
                self.entity_id,
                self.gw_conf("link"),
            )
            return
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            _LOGGER.error("%s: MQTT is not available", self.entity_id)
            return
        base = self.gw_conf(KEY)[CONF_Z2M_BASE_TOPIC].rstrip("/")
        self.async_on_remove(
            await mqtt.async_subscribe(self.hass, f"{base}/{friendly}", self._tt_mqtt)
        )

    async def async_will_remove_from_hass(self) -> None:
        self._tt_cancel_timers()
        await super().async_will_remove_from_hass()

    def _tt_friendly_name(self) -> str | None:
        link = self.gw_conf("link")
        if not isinstance(link, str):
            return None
        entry = er.async_get(self.hass).async_get(link)
        if entry is None or entry.device_id is None:
            return None
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if device is None or not any(
            domain == "mqtt" and ident.startswith("zigbee2mqtt_")
            for domain, ident in device.identifiers
        ):
            return None
        return device.name

    # --- requests ------------------------------------------------------------

    async def gw_request(self, request: Request) -> bool:
        if request.data.get("stop") and self._tt_state != _MOVING:
            return True  # nothing moves (as far as we know): a stop would do nothing
        return await super().gw_request(request)

    async def gw_apply(self, request: Request) -> None:
        if request.from_device:
            await super().gw_apply(request)
            return
        if request.data.get("stop"):
            self._tt_update_estimate()
            self._tt_stop_ticking()
            self._tt_state = _STOPPING
            self._tt_our_stop = True
            self.async_write_ha_state()
            await super().gw_apply(request)
            self._tt_arm_deadline(STOP_REPORT_WAIT)
            return
        position = request.data.get("position")
        if position is not None:
            self._tt_begin(float(position))
        await super().gw_apply(request)

    async def gw_push(self) -> None:
        target = self._gw_target
        if target is not None:
            self._tt_begin(float(target))
        await super().gw_push()

    def _tt_begin(self, target: float) -> None:
        target = max(0.0, min(100.0, target))
        if self._tt_state == _MOVING:
            self._tt_update_estimate()
        current = self._tt_position if self._tt_position is not None else 100 - target
        if self._tt_state != _MOVING and _near(current, target):
            return  # already there; the link won't send either
        if target >= 100:
            cmd = _CMD_OPEN
        elif target <= 0:
            cmd = _CMD_CLOSE
        else:
            cmd = _CMD_POSITION
        self._tt_start_reported = self._tt_state == _IDLE and self._tt_source == SOURCE_REPORTED
        self._tt_cmd = cmd
        self._tt_target = target
        self._tt_start_pos = float(current)
        self._tt_start_time = time.monotonic()
        self._tt_echo_at = None
        self._tt_republish_skipped = False
        self._tt_our_stop = False
        self._tt_state = _MOVING
        self._tt_position = float(current)
        self._tt_source = SOURCE_ESTIMATED
        travel = self._tt_travel_time(target > current)
        expected = abs(target - current) / 100 * travel
        self._tt_start_ticking()
        # Safety net: no report at all -> end at the target.
        self._tt_arm_deadline(expected + travel * 0.5 + 5)

    # --- device --------------------------------------------------------------

    @callback
    def gw_device_report(self, position: float | None) -> None:
        """HA's MQTT cover state is not used: reports come from the MQTT topic."""

    @callback
    def gw_baseline(self, position: float) -> None:
        if self._tt_restored and self._tt_position is not None:
            # First start: the restored position beats HA's last MQTT state.
            self._tt_restored = False
            self._gw_target = int(round(self._tt_position))
            self.async_write_ha_state()
            return
        self._tt_restored = False
        self._tt_position = float(position)
        self._tt_source = SOURCE_REPORTED
        super().gw_baseline(position)

    @callback
    def gw_device_available(self, available: bool) -> None:
        if not available and self._tt_state != _IDLE:
            # Stop estimating: no fake movement, no deadline "arrival".
            self._tt_update_estimate()
            self._tt_cancel_timers()
            self._tt_source = SOURCE_ESTIMATED
            self._tt_state = _IDLE
            self._tt_cmd = None
        super().gw_device_available(available)

    @callback
    def _tt_mqtt(self, msg) -> None:
        try:
            payload = json.loads(msg.payload)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        position = as_float(payload.get("position"))
        if position is None:
            return
        self._tt_report(self.gw_from_device(position), payload.get("running"))

    @callback
    def _tt_report(self, position: float, running: Any) -> None:
        if running is True:
            # Echo of a position command's target, not a real position.
            if self._tt_state == _MOVING and self._tt_cmd == _CMD_POSITION:
                self._tt_echo_at = time.monotonic()
            return

        if self._tt_state == _STOPPING:
            self._tt_finish_reported(position)
            return

        if self._tt_state != _MOVING:
            # Moved by something other than us (motor button, pulling the
            # curtain, another controller), or a late correction after a stop.
            self._tt_position = position
            self._tt_source = SOURCE_REPORTED
            if self._gw_target is None or not _near(position, self._gw_target):
                self.hass.async_create_task(self.gw_physical({"position": int(round(position))}))
            self.async_write_ha_state()
            return

        elapsed = time.monotonic() - self._tt_start_time
        if elapsed < COMMAND_GRACE and not _near(position, self._tt_target):
            return  # a leftover report from before this command

        if _near(position, self._tt_target):
            if (
                self._tt_cmd == _CMD_POSITION
                and self._tt_echo_at is not None
                and not self._tt_republish_skipped
                and time.monotonic() - self._tt_echo_at < REPUBLISH_WINDOW
            ):
                # Z2M re-publishes the echoed target before the real arrival.
                self._tt_republish_skipped = True
                return
            self._tt_calibrated = True
            self._tt_finish_reported(position)
            return

        opposite = 0.0 if self._tt_cmd == _CMD_OPEN else 100.0
        if (
            self._tt_cmd in (_CMD_OPEN, _CMD_CLOSE)
            and _near(position, opposite)
            and elapsed >= self._tt_travel_time(self._tt_cmd == _CMD_OPEN) * 0.5
        ):
            # Bogus first end report after a power loss: it did reach the end.
            _LOGGER.warning(
                "%s: end report contradicts the %s command, motor looks uncalibrated "
                "(power loss?); assuming it reached %s",
                self.entity_id,
                self._tt_cmd,
                int(self._tt_target),
            )
            self._tt_calibrated = False
            self._tt_finish(self._tt_target, SOURCE_ESTIMATED)
            return

        # Anything else mid-move: stopped (e.g. by the motor's own button) here.
        self._tt_finish_reported(position)
        self.hass.async_create_task(self.gw_physical({"position": int(round(position))}))

    def _tt_finish_reported(self, position: float) -> None:
        distance = abs(position - self._tt_start_pos)
        if self._tt_start_reported and distance >= MIN_LEARN_DISTANCE:
            elapsed = time.monotonic() - self._tt_start_time
            opening = position > self._tt_start_pos
            full_run = elapsed * 100 / distance
            _LOGGER.debug(
                "%s: learned %s time %.1fs", self.entity_id, "open" if opening else "close", full_run
            )
            self._tt_learn(opening, full_run)
        self._tt_finish(position, SOURCE_REPORTED)

    def _tt_finish(self, position: float, source: str) -> None:
        self._tt_cancel_timers()
        if self._tt_our_stop or self._tt_state == _STOPPING:
            self._gw_target = int(round(position))
        elif _near(position, self._tt_target):
            self._gw_target = int(round(position))
        self._tt_state = _IDLE
        self._tt_cmd = None
        self._tt_our_stop = False
        self._tt_position = float(position)
        self._tt_source = source
        self.async_write_ha_state()

    def _tt_update_estimate(self) -> None:
        if self._tt_state != _MOVING:
            return
        elapsed = time.monotonic() - self._tt_start_time
        step = elapsed / self._tt_travel_time(self._tt_target > self._tt_start_pos) * 100
        if self._tt_target >= self._tt_start_pos:
            self._tt_position = min(self._tt_target, self._tt_start_pos + step)
        else:
            self._tt_position = max(self._tt_target, self._tt_start_pos - step)

    # --- timers --------------------------------------------------------------

    def _tt_start_ticking(self) -> None:
        self._tt_stop_ticking()
        self._tt_tick_unsub = async_track_time_interval(self.hass, self._tt_tick, TICK)

    def _tt_stop_ticking(self) -> None:
        if self._tt_tick_unsub is not None:
            self._tt_tick_unsub()
            self._tt_tick_unsub = None

    @callback
    def _tt_tick(self, _now) -> None:
        self._tt_update_estimate()
        self.async_write_ha_state()

    def _tt_arm_deadline(self, seconds: float) -> None:
        if self._tt_deadline_unsub is not None:
            self._tt_deadline_unsub()
        self._tt_deadline_unsub = async_call_later(self.hass, seconds, self._tt_deadline)

    @callback
    def _tt_deadline(self, _now) -> None:
        self._tt_deadline_unsub = None
        if self._tt_state == _MOVING:
            _LOGGER.warning(
                "%s: no position report, assuming %s", self.entity_id, int(self._tt_target)
            )
            self._tt_finish(self._tt_target, SOURCE_ESTIMATED)
        elif self._tt_state == _STOPPING and self._tt_position is not None:
            self._tt_finish(self._tt_position, SOURCE_ESTIMATED)

    def _tt_cancel_timers(self) -> None:
        self._tt_stop_ticking()
        if self._tt_deadline_unsub is not None:
            self._tt_deadline_unsub()
            self._tt_deadline_unsub = None

    # --- restore -------------------------------------------------------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        super().gw_restore(last, extra)
        own = extra.get(KEY)
        if not isinstance(own, dict):
            # Legacy timed_curtain extra data (flat keys).
            own = {
                "position": extra.get("position"),
                "calibrated": extra.get("calibrated"),
                "motor_open_time": extra.get("motor_open_time"),
                "motor_close_time": extra.get("motor_close_time"),
            }
            if own["position"] is None and last is not None:
                own["position"] = last.attributes.get("current_position")
            if own["calibrated"] is None and last is not None:
                own["calibrated"] = last.attributes.get(ATTR_CALIBRATED)
        position = as_float(own.get("position"))
        if position is not None:
            self._tt_position = position
            self._tt_restored = True
        if own.get("calibrated") is False:
            self._tt_calibrated = False
        for key in ("motor_open_time", "motor_close_time"):
            value = as_float(own.get(key))
            if value is not None and value > 0:
                setattr(self, f"_tt_{key}", value)

    def gw_save(self) -> dict[str, Any]:
        data = super().gw_save()
        data[KEY] = {
            "position": self._tt_position,
            "calibrated": self._tt_calibrated,
            "motor_open_time": self._tt_motor_open_time,
            "motor_close_time": self._tt_motor_close_time,
        }
        return data

    # --- state ---------------------------------------------------------------

    @property
    def gw_actual_position(self) -> float | None:
        return self._tt_position

    @property
    def current_cover_position(self) -> int | None:
        if self._tt_state != _IDLE and self._tt_position is not None:
            return int(round(self._tt_position))
        return super().current_cover_position

    @property
    def is_closed(self) -> bool | None:
        closed = super().is_closed
        return None if closed is None else closed and self._tt_state == _IDLE

    @property
    def is_opening(self) -> bool:
        return self._tt_state == _MOVING and self._tt_target > self._tt_start_pos

    @property
    def is_closing(self) -> bool:
        return self._tt_state == _MOVING and self._tt_target < self._tt_start_pos

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes or {})
        attrs[ATTR_CALIBRATED] = self._tt_calibrated
        attrs[ATTR_POSITION_SOURCE] = self._tt_source
        attrs[ATTR_OPEN_TIME] = round(self._tt_travel_time(True), 1)
        attrs[ATTR_CLOSE_TIME] = round(self._tt_travel_time(False), 1)
        return attrs
