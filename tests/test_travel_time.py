"""Cover feature `travel_time` (Tuya curtain via Zigbee2MQTT)."""
from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, State

from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from .conftest import setup_platform

RAW = "cover.salon_curtain"
WRAPPER = "cover.salon_cover"
TOPIC = "zigbee2mqtt/salon_curtain"


@pytest.fixture(autouse=True)
def friendly_name():
    with patch(
        "custom_components.gungors.features.travel_time.TravelTime._tt_friendly_name",
        return_value="salon_curtain",
    ):
        yield


async def setup_curtain(hass: HomeAssistant, device_position: int) -> None:
    hass.states.async_set(RAW, "open", {"current_position": device_position})
    await setup_platform(
        hass,
        "cover",
        {
            "name": "salon_cover",
            "device_class": "curtain",
            "features": ["invert", "travel_time", {"link": RAW}],
        },
    )


async def mqtt_report(hass: HomeAssistant, device_position: int, running=None) -> None:
    payload = {"position": device_position}
    if running is not None:
        payload["running"] = running
    async_fire_mqtt_message(hass, TOPIC, json.dumps(payload))
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def state(hass: HomeAssistant):
    return hass.states.get(WRAPPER)


async def test_close_estimates_and_learns(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)  # motor 0 = room 100 (inverted)
    assert state(hass).attributes["current_position"] == 100

    await hass.services.async_call("cover", "close_cover", {"entity_id": WRAPPER}, blocking=True)
    await hass.async_block_till_done()
    # Room close = motor open.
    assert calls.of("cover", "open_cover", RAW)
    assert state(hass).state == "closing"

    await advance(hass, freezer, 5)
    assert 40 <= state(hass).attributes["current_position"] <= 60
    assert state(hass).attributes["position_source"] == "estimated"

    await advance(hass, freezer, 13)  # 18 s in total (deadline at 20), report arrives
    await mqtt_report(hass, 100)
    s = state(hass)
    assert s.state == "closed"
    assert s.attributes["current_position"] == 0
    assert s.attributes["position_source"] == "reported"
    assert s.attributes["close_time"] == pytest.approx(18, abs=0.6)
    assert s.attributes["open_time"] == 10


async def test_position_echo_and_republish(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await hass.services.async_call(
        "cover", "set_cover_position", {"entity_id": WRAPPER, "position": 40}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 60
    await advance(hass, freezer, 1.5)
    await mqtt_report(hass, 60, running=True)  # echo
    await advance(hass, freezer, 2)
    await mqtt_report(hass, 60, running=False)  # republished echo
    assert state(hass).state == "closing"
    await advance(hass, freezer, 3)
    await mqtt_report(hass, 60)  # real arrival
    assert state(hass).state == "open"
    assert state(hass).attributes["current_position"] == 40


async def test_uncalibrated_end_report(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await hass.services.async_call("cover", "close_cover", {"entity_id": WRAPPER}, blocking=True)
    await advance(hass, freezer, 8)
    await mqtt_report(hass, 0)  # bogus: the end opposite to the command
    s = state(hass)
    assert s.attributes["calibrated"] is False
    assert s.attributes["current_position"] == 0


async def test_physical_report_moves_target(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await mqtt_report(hass, 30)  # moved by hand / motor button
    assert state(hass).attributes["current_position"] == 70
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_stop_when_idle_is_ignored(hass: HomeAssistant, mqtt_mock, calls) -> None:
    await setup_curtain(hass, device_position=0)
    await hass.services.async_call("cover", "stop_cover", {"entity_id": WRAPPER}, blocking=True)
    await hass.async_block_till_done()
    assert not calls.of("cover", "stop_cover", RAW)


async def test_legacy_restore(hass: HomeAssistant, mqtt_mock, calls) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(
            State(WRAPPER, "open", {"current_position": 35, "calibrated": True}),
            {"motor_open_time": 21.5, "motor_close_time": 19.0, "position": 35, "calibrated": True},
        )],
    )
    await setup_curtain(hass, device_position=0)
    s = state(hass)
    assert s.attributes["current_position"] == 35
    # Motor open = room close (inverted).
    assert s.attributes["close_time"] == 21.5
    assert s.attributes["open_time"] == 19.0
