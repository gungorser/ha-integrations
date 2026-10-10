"""Light features: link (light, switch, group) and hold."""
from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import setup_platform

LAMP = "light.salon_lamp"
WRAPPER = "light.salon_light"


def lamp(hass: HomeAssistant, state: str, entity_id: str = LAMP, **attrs) -> None:
    hass.states.async_set(
        entity_id,
        state,
        {"supported_color_modes": ["color_temp"], "color_mode": "color_temp",
         "supported_features": 44, "effect_list": ["blink", "breathe"],
         "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500, **attrs},
    )


async def light(hass: HomeAssistant, service: str, entity_id: str = WRAPPER, **data) -> None:
    await hass.services.async_call("light", service, {"entity_id": entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


async def test_mirror_and_commands(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    lamp(hass, "on", brightness=120, color_temp_kelvin=3000)
    await setup_platform(hass, "light", {"name": "salon_light", "features": [{"link": LAMP}]})
    state = hass.states.get(WRAPPER)
    assert state.state == "on"
    assert state.attributes["brightness"] == 120
    assert state.attributes["color_temp_kelvin"] == 3000
    assert state.attributes["supported_color_modes"] == ["color_temp"]
    assert state.attributes["effect_list"] == ["blink", "breathe"]

    await light(hass, "turn_on", brightness=200)
    assert calls.of("light", "turn_on", LAMP)[-1]["brightness"] == 200
    lamp(hass, "on", brightness=200, color_temp_kelvin=3000)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["brightness"] == 200

    await light(hass, "turn_off")
    assert calls.of("light", "turn_off", LAMP)
    assert hass.states.get(WRAPPER).state == "off"
    lamp(hass, "off")
    await hass.async_block_till_done()

    # Turned on at the wall (not by us): physical, the wrapper follows.
    freezer.tick(timedelta(seconds=10))
    lamp(hass, "on", brightness=50)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert state.state == "on"
    assert state.attributes["brightness"] == 50


async def test_command_that_does_not_arrive(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    lamp(hass, "off")
    await setup_platform(hass, "light", {"name": "salon_light", "features": [{"link": LAMP}]})
    await light(hass, "turn_on")
    assert hass.states.get(WRAPPER).state == "on"
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "off"


async def test_switch_original(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("switch.yemek_2", "off")
    await setup_platform(
        hass, "light", {"name": "mutfak_light", "features": [{"link": "switch.yemek_2"}]}
    )
    state = hass.states.get("light.mutfak_light")
    assert state.state == "off"
    assert state.attributes["supported_color_modes"] == ["onoff"]
    await light(hass, "turn_on", "light.mutfak_light", brightness=100)
    assert calls.of("switch", "turn_on", "switch.yemek_2") == [{"entity_id": ["switch.yemek_2"]}]


async def test_group(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(
        "light.bahce_lamp_1", "off",
        {"supported_color_modes": ["brightness"], "color_mode": "brightness", "supported_features": 44},
    )
    lamp(hass, "off", "light.bahce_light_2")
    lamp(hass, "on", "light.bahce_light_3", brightness=90)
    await setup_platform(
        hass,
        "light",
        {"name": "bahce_light_1", "features": [{"link": "light.bahce_lamp_1"}]},
        {
            "name": "bahce_light",
            "features": [{"link": ["light.bahce_light_1", "light.bahce_light_2",
                                   "light.bahce_light_3"]}],
        },
    )
    state = hass.states.get("light.bahce_light")
    assert state.state == "on"
    assert state.attributes["brightness"] == 90
    assert state.attributes["supported_color_modes"] == ["color_temp"]
    assert state.attributes["entity_id"] == [
        "light.bahce_light_1", "light.bahce_light_2", "light.bahce_light_3"
    ]
    await light(hass, "turn_off", "light.bahce_light")
    assert calls.of("light", "turn_off", "light.bahce_light_2")
    # The member wrapper got the command and passed it to its own original.
    assert calls.of("light", "turn_off", "light.bahce_lamp_1")


async def test_hold_light(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    lamp(hass, "off")
    await setup_platform(
        hass, "light", {"name": "salon_light", "features": ["hold", {"link": LAMP}]}
    )
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": WRAPPER, "state": {"on": False}, "duration": {"hours": 1}},
        blocking=True,
    )
    await light(hass, "turn_on")
    assert not calls.of("light", "turn_on", LAMP)
    # On at the wall: turned off again.
    freezer.tick(timedelta(seconds=10))
    lamp(hass, "on", brightness=10)
    await hass.async_block_till_done()
    assert calls.of("light", "turn_off", LAMP)
