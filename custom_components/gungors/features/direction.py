"""`direction`: movement tracking for blinds that report a slowly changing position.

For blinds (IKEA via Zigbee2MQTT) that publish no opening/closing state, only a
position about once a second while moving. This feature:

- derives is_opening / is_closing from consecutive position reports,
- shows the real position while the blind moves and the target at rest,
- ignores Zigbee2MQTT's echo of a commanded target (a jump straight to it),
- treats a move it did not command, or a reversal mid-move, as physical,
- ends a move when the target is reached or reports stop for `stop_silence` s;
  reports near the target right after that are the blind settling,
- re-sends a command once when no report arrives within `start_timeout` s.

Options: `direction:` alone, or `direction: {start_timeout: 8, stop_silence: 3}`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from ..const import (
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_SILENCE,
    MAX_REAL_STEP,
    POSITION_TOLERANCE,
)
from ..core import Feature, Request, register

_LOGGER = logging.getLogger(__name__)

KEY = "direction"

_IDLE = "idle"
_COMMANDED = "commanded"
_PHYSICAL = "physical"
_STOPPING = "stopping"  # we sent a stop; trailing reports are not physical

CONF_START_TIMEOUT = "start_timeout"
CONF_STOP_SILENCE = "stop_silence"


def _schema(value: Any) -> dict[str, float]:
    value = vol.Schema(
        vol.Any(
            None,
            {
                vol.Optional(CONF_START_TIMEOUT, default=DEFAULT_START_TIMEOUT): vol.Coerce(float),
                vol.Optional(CONF_STOP_SILENCE, default=DEFAULT_STOP_SILENCE): vol.Coerce(float),
            },
        )
    )(value)
    if value is None:
        value = {CONF_START_TIMEOUT: DEFAULT_START_TIMEOUT, CONF_STOP_SILENCE: DEFAULT_STOP_SILENCE}
    return value


@register("cover")
class Direction(Feature):
    feature_name = KEY
    schema = _schema

    _dir_state = _IDLE
    _dir_actual: int | None = None
    _dir_opening = False
    _dir_closing = False
    _dir_target: int | None = None  # commanded target of the current move
    _dir_retried = False
    _dir_retrying = False
    _dir_settle_until = 0.0
    _dir_stall_unsub = None
    _dir_start_unsub = None

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        return {"motion"}  # one movement tracker per cover

    @property
    def _dir_stop_silence(self) -> float:
        return self.gw_conf(KEY)[CONF_STOP_SILENCE]

    @property
    def _dir_start_timeout(self) -> float:
        return self.gw_conf(KEY)[CONF_START_TIMEOUT]

    async def async_will_remove_from_hass(self) -> None:
        self._dir_cancel_timers()
        await super().async_will_remove_from_hass()

    # --- requests ------------------------------------------------------------

    async def gw_apply(self, request: Request) -> None:
        if request.from_device:
            await super().gw_apply(request)
            return

        if request.data.get("stop"):
            # A stop cancels the move; the target settles where the blind stops.
            self._dir_target = None
            self._dir_settle_until = 0.0
            self._dir_cancel_timers()
            self._dir_state = _STOPPING
            self._dir_opening = self._dir_closing = False
            await super().gw_apply(request)
            if self._dir_actual is not None:
                self._gw_target = self._dir_actual
            self._dir_arm_stall()
            self.async_write_ha_state()
            return

        position = request.data.get("position")
        if position is None:
            await super().gw_apply(request)
            return

        actual = self._dir_actual
        if actual is not None and abs(position - actual) <= POSITION_TOLERANCE:
            # Already there: the blind won't move or report, so a command would
            # only end in a start_timeout retry and a false "no response".
            self._dir_state = _IDLE
            await super().gw_apply(request)
            self._dir_finish(actual)
            return

        self._dir_begin(int(position))
        await super().gw_apply(request)
        self._dir_arm_start()

    async def gw_push(self) -> None:
        target = self._gw_target
        if target is not None and not (
            self._dir_actual is not None and abs(target - self._dir_actual) <= POSITION_TOLERANCE
        ):
            retrying = self._dir_retrying
            self._dir_begin(target)
            self._dir_retried = retrying
            await super().gw_push()
            self._dir_arm_start()
            return
        await super().gw_push()

    def _dir_begin(self, target: int) -> None:
        self._dir_target = target
        self._dir_retried = False
        self._dir_settle_until = 0.0
        self._dir_state = _COMMANDED
        if self._dir_actual is not None:
            self._dir_opening = target > self._dir_actual
            self._dir_closing = target < self._dir_actual

    # --- device --------------------------------------------------------------

    @callback
    def gw_baseline(self, position: float) -> None:
        self._dir_actual = int(round(position))
        super().gw_baseline(position)

    @callback
    def gw_device_available(self, available: bool) -> None:
        if not available:
            # Drop the in-flight move; a held setpoint stays held.
            self._dir_cancel_timers()
            self._dir_target = None
            self._dir_state = _IDLE
            self._dir_opening = self._dir_closing = False
            self._dir_settle_until = 0.0
        super().gw_device_available(available)

    @callback
    def gw_device_report(self, position: float | None) -> None:
        if position is None:
            return
        position = int(round(position))
        previous = self._dir_actual
        self._dir_actual = position

        if self._dir_state == _STOPPING:
            # Trailing reports after our own stop: track, don't treat as physical.
            self._dir_arm_stall()
            return

        if self._dir_state == _COMMANDED and self._dir_target is not None:
            target = self._dir_target
            if (
                previous is not None
                and position == target
                and abs(position - previous) > MAX_REAL_STEP
            ):
                # Zigbee2MQTT echo of our own target, not a real position.
                self._dir_actual = previous
                return
            self._dir_cancel_start()  # a report arrived, no retry needed
            if abs(position - target) <= POSITION_TOLERANCE:
                self._dir_finish(position)
                return
            if previous is not None and position != previous:
                expected_opening = target > previous
                actually_opening = position > previous
                if expected_opening != actually_opening:
                    # Reversed mid-move: someone overrode us physically.
                    self._dir_target = None
                    self._dir_state = _PHYSICAL
                    self._dir_opening = actually_opening
                    self._dir_closing = not actually_opening
                    self._dir_arm_stall()
                    self.hass.async_create_task(self.gw_physical({"position": position}))
                    return
            self._dir_opening = target > position
            self._dir_closing = target < position
            self._dir_arm_stall()
            return

        if self._dir_state == _IDLE and position == previous:
            return  # attribute-only update, nothing moved

        target = self._gw_target
        if (
            self._dir_state == _IDLE
            and target is not None
            and time.monotonic() < self._dir_settle_until
            and abs(position - target) <= POSITION_TOLERANCE
        ):
            # Last report of a finished commanded move: settling, not physical.
            self._gw_target = position
            return

        # Not something we commanded: physical movement (a bound button, or
        # anything else moving the original). The target follows.
        self._dir_state = _PHYSICAL
        self._dir_settle_until = 0.0
        if previous is not None and position != previous:
            self._dir_opening = position > previous
            self._dir_closing = position < previous
        self._dir_arm_stall()
        self.hass.async_create_task(self.gw_physical({"position": position}))

    def _dir_finish(self, position: int) -> None:
        self._gw_target = position
        self._dir_state = _IDLE
        self._dir_opening = self._dir_closing = False
        self._dir_target = None
        self._dir_settle_until = time.monotonic() + self._dir_stop_silence
        self._dir_cancel_timers()
        self.async_write_ha_state()

    # --- timers --------------------------------------------------------------

    def _dir_arm_stall(self) -> None:
        self._dir_cancel_stall()
        self._dir_stall_unsub = async_call_later(
            self.hass, self._dir_stop_silence, self._dir_stalled
        )

    @callback
    def _dir_stalled(self, _now) -> None:
        """No report for stop_silence seconds: the blind stopped."""
        self._dir_stall_unsub = None
        if self._dir_actual is not None:
            self._gw_target = self._dir_actual
        self._dir_state = _IDLE
        self._dir_opening = self._dir_closing = False
        self._dir_target = None
        self.async_write_ha_state()

    def _dir_arm_start(self) -> None:
        self._dir_cancel_start()
        self._dir_start_unsub = async_call_later(
            self.hass, self._dir_start_timeout, self._dir_start_expired
        )

    @callback
    def _dir_start_expired(self, _now) -> None:
        """No report at all after a command: retry once, then give up."""
        self._dir_start_unsub = None
        if self._dir_target is None:
            return
        if not self._dir_retried:
            self.hass.async_create_task(self._dir_retry())
            return
        _LOGGER.warning("%s: no response from the device after a retry", self.entity_id)
        self._dir_state = _IDLE
        self._dir_opening = self._dir_closing = False
        self.async_write_ha_state()

    async def _dir_retry(self) -> None:
        self._dir_retrying = True
        try:
            await self.gw_push()
        finally:
            self._dir_retrying = False
        self._dir_retried = True

    def _dir_cancel_stall(self) -> None:
        if self._dir_stall_unsub is not None:
            self._dir_stall_unsub()
            self._dir_stall_unsub = None

    def _dir_cancel_start(self) -> None:
        if self._dir_start_unsub is not None:
            self._dir_start_unsub()
            self._dir_start_unsub = None

    def _dir_cancel_timers(self) -> None:
        self._dir_cancel_stall()
        self._dir_cancel_start()

    # --- state ---------------------------------------------------------------

    @property
    def current_cover_position(self) -> int | None:
        # While the blind moves, follow its real position; at rest the target.
        if self._dir_state != _IDLE and self._dir_actual is not None:
            return self._dir_actual
        return super().current_cover_position

    @property
    def is_opening(self) -> bool:
        return self._dir_state != _IDLE and self._dir_opening

    @property
    def is_closing(self) -> bool:
        return self._dir_state != _IDLE and self._dir_closing
