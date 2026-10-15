"""Cover feature `travel_time` (Tuya curtain via Zigbee2MQTT)."""
from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from .conftest import setup_platform

RAW = "cover.salon_curtain"
WRAPPER = "cover.salon_cover"
TOPIC = "zigbee2mqtt/salon_curtain"


@pytest.fixture
def real_z2m():
    """Requested by tests that resolve the Zigbee2MQTT friendly name for real."""


@pytest.fixture(autouse=True)
def friendly_name(request):
    if "real_z2m" in request.fixturenames:
        yield
        return
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


async def wrapper_call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call("cover", service, {"entity_id": WRAPPER, **data}, blocking=True)
    await hass.async_block_till_done()


def entity(hass: HomeAssistant):
    return hass.data["gungors"]["entities"][WRAPPER]


async def test_open_estimates_and_learns(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=100)  # motor 100 = room 0 (inverted)
    assert state(hass).state == "closed"

    await wrapper_call(hass, "open_cover")
    # Room open = motor close; a full run uses close_cover, not a position.
    assert calls.of("cover", "close_cover", RAW)
    assert not calls.of("cover", "set_cover_position", RAW)
    assert state(hass).state == "opening"

    await advance(hass, freezer, 4)
    assert 30 <= state(hass).attributes["current_position"] <= 50

    await advance(hass, freezer, 8)  # 12 s in total
    await mqtt_report(hass, 0)
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 100)
    assert s.attributes["open_time"] == pytest.approx(12, abs=0.6)
    assert s.attributes["close_time"] == 10


async def test_stop_mid_move_takes_reported_position(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 4)
    await wrapper_call(hass, "stop_cover")
    assert calls.of("cover", "stop_cover", RAW)
    s = state(hass)
    assert s.state == "open"  # no longer closing, waiting for the real position
    assert 55 <= s.attributes["current_position"] <= 65

    await advance(hass, freezer, 1)
    await mqtt_report(hass, 42)  # the motor reports where it stopped
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 58)
    assert s.attributes["position_source"] == "reported"
    # 42 % in ~5 s: the motor-open (room close) time is learned.
    assert s.attributes["close_time"] == pytest.approx(11.9, abs=0.6)

    # The stop deadline was cancelled by the report.
    await advance(hass, freezer, 5)
    assert state(hass).attributes["current_position"] == 58


async def test_stop_without_report_keeps_estimate(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 3)
    await wrapper_call(hass, "stop_cover")
    await advance(hass, freezer, 3.5)  # STOP_REPORT_WAIT elapsed
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 70)
    assert s.attributes["position_source"] == "estimated"
    # The wrapper's target is where the curtain stopped: no move back.
    await wrapper_call(hass, "set_cover_position", position=70)
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_no_report_ends_at_target(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 12)
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("closing", 0)  # clamped estimate
    await advance(hass, freezer, 9)  # deadline: 10 s run + 5 s + 5 s
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("closed", 0)
    assert s.attributes["position_source"] == "estimated"
    assert s.attributes["calibrated"] is True


async def test_retarget_and_motor_button_stop(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 0.5)
    await mqtt_report(hass, 0)  # leftover report from before the command
    assert state(hass).state == "closing"

    await advance(hass, freezer, 2.5)  # ~70
    await wrapper_call(hass, "set_cover_position", position=20)
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 80
    assert state(hass).state == "closing"

    await advance(hass, freezer, 2)  # ~50
    await mqtt_report(hass, 45)  # stopped by the motor's own button at room 55
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 55)
    # Not learned: the run did not start from a reported position.
    assert (s.attributes["open_time"], s.attributes["close_time"]) == (10, 10)
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 80  # not sent back


async def test_request_for_current_position_sends_nothing(
    hass: HomeAssistant, mqtt_mock, calls
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "set_cover_position", position=100)
    await wrapper_call(hass, "open_cover")
    assert not [c for c in calls.items if c[1].endswith("_cover") and c[0] == "cover"
                and RAW in c[2].get("entity_id", [])]
    assert state(hass).attributes["position_source"] == "reported"


async def test_unavailable_mid_move_stops_estimating(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 3)
    hass.states.async_set(RAW, "unavailable")
    await hass.async_block_till_done()
    assert state(hass).state == "unavailable"
    await advance(hass, freezer, 30)  # no deadline "arrival" meanwhile
    assert state(hass).state == "unavailable"

    hass.states.async_set(RAW, "open", {"current_position": 30})
    await hass.async_block_till_done()
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 70)
    assert s.attributes["position_source"] == "reported"


async def test_unavailable_while_waiting_for_stop_report(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await advance(hass, freezer, 3)
    await wrapper_call(hass, "stop_cover")
    hass.states.async_set(RAW, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 5)
    hass.states.async_set(RAW, "open", {"current_position": 25})
    await hass.async_block_till_done()
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 75)


async def test_strict_hold_pushes_back_with_estimate(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    hass.states.async_set(RAW, "open", {"current_position": 0})
    await setup_platform(
        hass,
        "cover",
        {"name": "salon_cover", "device_class": "curtain",
         "features": ["hold", "invert", "travel_time", {"link": RAW}]},
    )
    await hass.services.async_call(
        "gungors", "hold", {"entity_id": WRAPPER, "state": {"position": 100}}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls.of("cover", "close_cover", RAW)  # already there

    await mqtt_report(hass, 50)  # pulled by hand to room 50
    # Pushed back: room open = motor close, as a full run.
    assert calls.of("cover", "close_cover", RAW)
    assert state(hass).state == "opening"
    await advance(hass, freezer, 5)
    await mqtt_report(hass, 0)
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 100)


async def test_malformed_reports_are_ignored(hass: HomeAssistant, mqtt_mock, calls) -> None:
    await setup_curtain(hass, device_position=0)
    for payload in ("not json", "[1, 2]", json.dumps({"state": "OPEN"}),
                    json.dumps({"position": None})):
        async_fire_mqtt_message(hass, TOPIC, payload)
        await hass.async_block_till_done()
    s = state(hass)
    assert (s.state, s.attributes["current_position"]) == ("open", 100)
    assert not calls.items


async def test_friendly_name_from_zigbee2mqtt_device(
    hass: HomeAssistant, mqtt_mock, calls, real_z2m
) -> None:
    entry = hass.config_entries.async_entries("mqtt")[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("mqtt", "zigbee2mqtt_0x00158d0001a2b3c4")},
        name="salon_curtain",
    )
    er.async_get(hass).async_get_or_create(
        "cover", "mqtt", "0x00158d0001a2b3c4_cover", suggested_object_id="salon_curtain",
        device_id=device.id, config_entry=entry,
    )
    hass.states.async_set(RAW, "open", {"current_position": 0})
    await setup_platform(
        hass,
        "cover",
        {"name": "salon_cover", "features": [
            "invert", {"travel_time": {"z2m_base_topic": "z2m/"}}, {"link": RAW}]},
    )
    async_fire_mqtt_message(hass, "z2m/salon_curtain", json.dumps({"position": 30}))
    await hass.async_block_till_done()
    assert state(hass).attributes["current_position"] == 70


async def test_link_that_is_not_zigbee2mqtt(
    hass: HomeAssistant, mqtt_mock, calls, real_z2m, caplog: pytest.LogCaptureFixture
) -> None:
    entry = hass.config_entries.async_entries("mqtt")[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("mqtt", "tasmota_curtain")}, name="other",
    )
    er.async_get(hass).async_get_or_create(
        "cover", "mqtt", "tasmota_curtain", suggested_object_id="other_curtain",
        device_id=device.id, config_entry=entry,
    )
    for raw in ("cover.other_curtain", "cover.unregistered", "cover.g1", "cover.g2"):
        hass.states.async_set(raw, "open", {"current_position": 0})
    await setup_platform(
        hass,
        "cover",
        {"name": "other_cover", "features": ["travel_time", {"link": "cover.other_curtain"}]},
        {"name": "unregistered_cover", "features": ["travel_time", {"link": "cover.unregistered"}]},
        {"name": "group_cover", "features": ["travel_time", {"link": ["cover.g1", "cover.g2"]}]},
    )
    assert caplog.text.count("is not a Zigbee2MQTT device") == 3
    # The wrappers exist and still follow commands; only the reports are missing.
    assert hass.states.get("cover.other_cover").attributes["current_position"] == 0


async def test_mqtt_not_available(
    hass: HomeAssistant, calls, caplog: pytest.LogCaptureFixture
) -> None:
    with patch(
        "homeassistant.components.mqtt.async_wait_for_mqtt_client", return_value=False
    ):
        await setup_curtain(hass, device_position=0)
    assert "MQTT is not available" in caplog.text
    assert state(hass).attributes["current_position"] == 100


async def test_restore_from_last_state_only(hass: HomeAssistant, mqtt_mock, calls) -> None:
    mock_restore_cache(
        hass, [State(WRAPPER, "open", {"current_position": 35, "calibrated": False})]
    )
    await setup_curtain(hass, device_position=0)
    s = state(hass)
    assert s.attributes["current_position"] == 35
    assert s.attributes["calibrated"] is False


async def test_restore_and_save_round_trip(hass: HomeAssistant, mqtt_mock, calls) -> None:
    own = {"position": 35.0, "calibrated": True, "motor_open_time": 12.0,
           "motor_close_time": 15.0}
    mock_restore_cache_with_extra_data(
        hass,
        [(State(WRAPPER, "open", {"current_position": 35}),
          {"base": {"target": 35}, "travel_time": own})],
    )
    await setup_curtain(hass, device_position=0)
    s = state(hass)
    assert s.attributes["current_position"] == 35
    assert (s.attributes["open_time"], s.attributes["close_time"]) == (15.0, 12.0)
    assert entity(hass).extra_restore_state_data.as_dict() == {"travel_time": own}


async def test_remove_mid_move_cancels_timers(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_curtain(hass, device_position=0)
    await wrapper_call(hass, "close_cover")
    await entity(hass).async_remove()
    await hass.async_block_till_done()
    # A registered entity leaves a restored placeholder behind.
    assert hass.states.get(WRAPPER).attributes.get("restored") is True
    assert WRAPPER not in hass.data["gungors"]["entities"]
    assert RAW not in hass.data["gungors"]["claims"]
    await advance(hass, freezer, 30)  # no tick or deadline on the removed entity
    assert hass.states.get(WRAPPER).attributes.get("restored") is True
