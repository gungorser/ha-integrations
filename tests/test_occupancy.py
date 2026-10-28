"""Light feature `occupancy`: a motion sensor lights the lamp and darkens it again."""
from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from .conftest import setup_platform

LAMP = "light.koridor0_lamp"
WRAPPER = "light.koridor0_light"
SENSOR = "binary_sensor.koridor0_sensor_occupancy"
LIGHT_ATTRS = {
    "supported_color_modes": ["brightness"],
    "color_mode": "brightness",
    "supported_features": 32,
}


async def set_state(hass: HomeAssistant, entity_id: str, state: str, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await hass.async_block_till_done()


async def lamp_reports(hass: HomeAssistant, state: str, **attrs) -> None:
    await set_state(hass, LAMP, state, **LIGHT_ATTRS, **attrs)


async def setup(
    hass: HomeAssistant,
    lamp: str = "off",
    sensor: str = "off",
    sun: str | None = "below_horizon",
    occupancy=SENSOR,
) -> None:
    hass.states.async_set(LAMP, lamp, {**LIGHT_ATTRS, "brightness": 200})
    hass.states.async_set(SENSOR, sensor)
    if sun is not None:
        hass.states.async_set("sun.sun", sun)
    await setup_platform(
        hass,
        "light",
        {"name": "koridor0_light", "features": [{"occupancy": occupancy}, {"link": LAMP}]},
    )


async def user(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "light", service, {"entity_id": WRAPPER, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def test_schema(hass: HomeAssistant) -> None:
    from custom_components.gungors.features.occupancy import Occupancy  # noqa: PLC0415

    assert Occupancy.schema(SENSOR) == {
        "sensor": SENSOR,
        "brightness_pct": 50,
        "transition": 2,
        "after_sunset_only": True,
    }
    options = Occupancy.schema(
        {"sensor": SENSOR, "brightness_pct": 30, "transition": 0, "after_sunset_only": False}
    )
    assert options["brightness_pct"] == 30
    assert options["transition"] == 0
    assert options["after_sunset_only"] is False
    with pytest.raises(vol.Invalid):
        Occupancy.schema("light.koridor0_lamp")  # not a binary_sensor
    with pytest.raises(vol.Invalid):
        Occupancy.schema({"sensor": SENSOR, "brightness_pct": 0})
    with pytest.raises(vol.Invalid):
        Occupancy.schema(None)


async def test_motion_lights_and_darkens(hass: HomeAssistant, calls) -> None:
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    turned_on = calls.of("light", "turn_on", LAMP)
    assert len(turned_on) == 1
    assert turned_on[0]["brightness"] == 128  # 50 %
    assert turned_on[0]["transition"] == 2
    await lamp_reports(hass, "on", brightness=128)
    assert hass.states.get(WRAPPER).state == "on"

    await set_state(hass, SENSOR, "off")
    assert len(calls.of("light", "turn_off", LAMP)) == 1
    await lamp_reports(hass, "off")
    assert hass.states.get(WRAPPER).state == "off"


async def test_options(hass: HomeAssistant, calls) -> None:
    await setup(
        hass,
        occupancy={"sensor": SENSOR, "brightness_pct": 100, "transition": 0.5},
    )
    await set_state(hass, SENSOR, "on")
    turned_on = calls.of("light", "turn_on", LAMP)[-1]
    assert turned_on["brightness"] == 255
    assert turned_on["transition"] == 0.5


async def test_daylight_does_nothing(hass: HomeAssistant, calls) -> None:
    await setup(hass, sun="above_horizon")
    await set_state(hass, SENSOR, "on")
    assert not calls.of("light", "turn_on", LAMP)
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)


async def test_unknown_sun_does_nothing(hass: HomeAssistant, calls) -> None:
    await setup(hass, sun=None)
    await set_state(hass, SENSOR, "on")
    assert not calls.of("light", "turn_on", LAMP)


async def test_any_time_of_day(hass: HomeAssistant, calls) -> None:
    await setup(
        hass,
        sun="above_horizon",
        occupancy={"sensor": SENSOR, "after_sunset_only": False},
    )
    await set_state(hass, SENSOR, "on")
    assert len(calls.of("light", "turn_on", LAMP)) == 1


async def test_only_clean_edges_count(hass: HomeAssistant, calls) -> None:
    await setup(hass, sensor="unavailable")
    await set_state(hass, SENSOR, "on")  # unavailable -> on is no motion start
    assert not calls.of("light", "turn_on", LAMP)
    await set_state(hass, SENSOR, "unknown")
    await set_state(hass, SENSOR, "off")
    await set_state(hass, SENSOR, "on")
    assert len(calls.of("light", "turn_on", LAMP)) == 1


async def test_light_that_is_on_wins_over_motion(hass: HomeAssistant, calls) -> None:
    """On at the start (also after a restart): the user's, never touched."""
    await setup(hass, lamp="on")
    assert hass.states.get(WRAPPER).state == "on"
    await set_state(hass, SENSOR, "on")
    assert not calls.of("light", "turn_on", LAMP)
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)


async def test_user_turning_on_takes_the_light(hass: HomeAssistant, calls) -> None:
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    await lamp_reports(hass, "on", brightness=128)
    await user(hass, "turn_on", brightness=255)  # any user request takes ownership
    await lamp_reports(hass, "on", brightness=255)
    calls.clear()
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)
    assert hass.states.get(WRAPPER).state == "on"


async def test_user_turning_off_during_motion(hass: HomeAssistant, calls) -> None:
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    await lamp_reports(hass, "on", brightness=128)
    await user(hass, "turn_off")
    await lamp_reports(hass, "off")
    calls.clear()

    # Motion goes on: the light stays off, nothing sends the sensor's request again.
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)
    assert not calls.of("light", "turn_on", LAMP)
    # The next motion lights it again.
    await set_state(hass, SENSOR, "on")
    assert len(calls.of("light", "turn_on", LAMP)) == 1


async def test_user_off_then_on_during_motion(hass: HomeAssistant, calls) -> None:
    """The user turned it off and on again: it is theirs for good."""
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    await lamp_reports(hass, "on", brightness=128)
    await user(hass, "turn_off")
    await lamp_reports(hass, "off")
    await user(hass, "turn_on")
    await lamp_reports(hass, "on", brightness=200)
    calls.clear()
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)


async def test_turned_on_at_the_device_is_the_users(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, sun="above_horizon")  # no motion light by day
    freezer.tick(timedelta(seconds=10))
    await set_state(hass, SENSOR, "on")
    await lamp_reports(hass, "on", brightness=100)  # a physical request
    calls.clear()
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)


async def test_late_report_is_not_the_users(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """A slow device confirms the sensor's turn-on after the link's echo window."""
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    assert len(calls.of("light", "turn_on", LAMP)) == 1
    freezer.tick(timedelta(seconds=8))
    await lamp_reports(hass, "on", brightness=128)
    assert hass.states.get(WRAPPER).state == "on"

    await set_state(hass, SENSOR, "off")
    assert len(calls.of("light", "turn_off", LAMP)) == 1


async def test_physical_change_after_the_grace_is_the_users(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    await lamp_reports(hass, "on", brightness=128)
    freezer.tick(timedelta(seconds=30))
    # Turned off and on again at the device: the user's.
    await lamp_reports(hass, "off")
    freezer.tick(timedelta(seconds=10))
    await lamp_reports(hass, "on", brightness=255)
    calls.clear()
    await set_state(hass, SENSOR, "off")
    assert not calls.of("light", "turn_off", LAMP)


async def test_short_motion_before_the_light_reports(hass: HomeAssistant, calls) -> None:
    """Motion ends before the device reported on: the turn-off is still sent."""
    await setup(hass)
    await set_state(hass, SENSOR, "on")
    assert hass.states.get(WRAPPER).state == "off"
    await set_state(hass, SENSOR, "off")
    assert len(calls.of("light", "turn_off", LAMP)) == 1


async def test_unavailable_light_is_left_alone(hass: HomeAssistant, calls) -> None:
    await setup(hass)
    await set_state(hass, LAMP, "unavailable")
    await set_state(hass, SENSOR, "on")
    assert not calls.of("light", "turn_on", LAMP)
