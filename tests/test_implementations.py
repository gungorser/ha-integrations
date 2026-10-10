"""One entity per implementation used in ha-configs, tested in both directions.

Each test sets up a wrapper with the exact feature list of its ha-configs entry and
checks both sync directions:
- wrapper -> original: a request on the wrapper reaches the original (service call),
  and the original's echo leaves the wrapper in the requested state;
- original -> wrapper: a change made on the original (device, wall button, UI)
  shows on the wrapper.
"""
from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant, State

from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
    mock_restore_cache,
)

from .conftest import setup_platform


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def call(hass: HomeAssistant, domain: str, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def set_state(hass: HomeAssistant, entity_id: str, state: str, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await hass.async_block_till_done()


def attr(hass: HomeAssistant, entity_id: str, name: str):
    return hass.states.get(entity_id).attributes.get(name)


# --- covers ------------------------------------------------------------------


def blind(hass: HomeAssistant, entity_id: str, position: int) -> None:
    hass.states.async_set(
        entity_id,
        "closed" if position == 0 else "open",
        {"current_position": position, "supported_features": 15},
    )


async def blind_moves(hass: HomeAssistant, entity_id: str, *positions: int) -> None:
    for position in positions:
        blind(hass, entity_id, position)
        await hass.async_block_till_done()


async def blind_travels(hass: HomeAssistant, entity_id: str, start: int, end: int) -> None:
    """Real movement: a report every 3 % (an IKEA blind reports about once a second)."""
    step = 3 if end > start else -3
    await blind_moves(hass, entity_id, *range(start + step, end, step), end)


async def test_cover_linked_to_number(hass: HomeAssistant, calls) -> None:
    """mutfak_cover, yemek_cover, ...: `link: input_number.x`."""
    number = "input_number.mutfak_cover_position"
    wrapper = "cover.mutfak_cover"
    hass.states.async_set(number, "40")
    await setup_platform(
        hass, "cover",
        {"name": "mutfak_cover", "device_class": "shade", "features": [{"link": number}]},
    )
    assert attr(hass, wrapper, "current_position") == 40

    # wrapper -> original
    for service, data, value in (
        ("set_cover_position", {"position": 70}, 70),
        ("close_cover", {}, 0),
        ("open_cover", {}, 100),
    ):
        await call(hass, "cover", service, wrapper, **data)
        assert calls.of("input_number", "set_value", number)[-1]["value"] == value
        await set_state(hass, number, str(value))
        assert attr(hass, wrapper, "current_position") == value

    # original -> wrapper
    await set_state(hass, number, "25")
    assert attr(hass, wrapper, "current_position") == 25
    assert hass.states.get(wrapper).state == "open"
    await set_state(hass, number, "0")
    assert hass.states.get(wrapper).state == "closed"


async def test_ikea_blind_with_window(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """sercan_cover, bebek_cover, yatak_cover_window: hold, buttons, window, direction, link."""
    raw, window, wrapper = "cover.sercan_blind", "binary_sensor.sercan_window", "cover.sercan_cover"
    hass.states.async_set(window, "off")
    blind(hass, raw, 100)
    await setup_platform(
        hass, "cover",
        {"name": "sercan_cover", "device_class": "blind",
         "features": ["hold", "buttons", {"window": window}, "direction", {"link": raw}]},
    )
    assert attr(hass, wrapper, "current_position") == 100

    # wrapper -> original: command, echo, real movement, arrival.
    await call(hass, "cover", "set_cover_position", wrapper, position=40)
    assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 40
    assert hass.states.get(wrapper).state == "closing"
    await blind_moves(hass, raw, 40)  # Zigbee2MQTT echo of the target
    await blind_travels(hass, raw, 100, 40)
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["current_position"]) == ("open", 40)

    # original -> wrapper: moved with the IKEA remote (not through HA).
    await advance(hass, freezer, 30)
    calls.clear()
    await blind_moves(hass, raw, 43, 46)
    assert hass.states.get(wrapper).state == "opening"
    await blind_travels(hass, raw, 46, 100)
    await advance(hass, freezer, 6)
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["current_position"]) == ("open", 100)
    assert not calls.of("cover", "set_cover_position", raw)  # not pushed back

    # Window open: a remote request waits, the wall button goes through.
    await set_state(hass, window, "on")
    await call(hass, "cover", "close_cover", wrapper)
    assert not calls.of("cover", "set_cover_position", raw)
    hass.bus.async_fire("gungors_physical_cover", {"entity_id": [wrapper], "action": "close"})
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 0
    await blind_moves(hass, raw, 0)
    await blind_travels(hass, raw, 100, 0)
    assert hass.states.get(wrapper).state == "closed"


async def test_ikea_blind_without_window(hass: HomeAssistant, calls) -> None:
    """koridor1_cover: hold, buttons, direction, link."""
    raw, wrapper = "cover.koridor1_blind", "cover.koridor1_cover"
    blind(hass, raw, 0)
    await setup_platform(
        hass, "cover",
        {"name": "koridor1_cover", "device_class": "blind",
         "features": ["hold", "buttons", "direction", {"link": raw}]},
    )
    assert hass.states.get(wrapper).state == "closed"

    # wrapper -> original
    await call(hass, "cover", "open_cover", wrapper)
    assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 100
    assert hass.states.get(wrapper).state == "opening"
    await blind_moves(hass, raw, 100)
    await blind_travels(hass, raw, 0, 100)
    assert hass.states.get(wrapper).state == "open"

    # wall button
    hass.bus.async_fire("gungors_physical_cover", {"entity_id": [wrapper], "action": "close"})
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 0


async def test_ikea_blind_direction_only(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """yatak_cover_1 / yatak_cover_3: hold, direction, link."""
    raw, wrapper = "cover.yatak_blind_1", "cover.yatak_cover_1"
    blind(hass, raw, 100)
    await setup_platform(
        hass, "cover",
        {"name": "yatak_cover_1", "device_class": "blind",
         "features": ["hold", "direction", {"link": raw}]},
    )
    assert attr(hass, wrapper, "physical_buttons") is None

    # wrapper -> original
    await call(hass, "cover", "close_cover", wrapper)
    assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 0
    await blind_moves(hass, raw, 0)
    await blind_travels(hass, raw, 100, 0)
    assert hass.states.get(wrapper).state == "closed"

    # original -> wrapper
    await advance(hass, freezer, 30)
    await blind_moves(hass, raw, 4, 8)
    assert hass.states.get(wrapper).state == "opening"
    await advance(hass, freezer, 6)  # stalled: stopped where it is
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["current_position"]) == ("open", 8)


async def test_cover_group_of_wrappers(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """yatak_cover: hold, link [three wrappers]."""
    members = {}
    for n in (1, 3):
        raw = f"cover.yatak_blind_{n}"
        blind(hass, raw, 100)
        members[f"cover.yatak_cover_{n}"] = raw
    await setup_platform(
        hass, "cover",
        *[
            {"name": wrapper.split(".")[1], "features": ["hold", "direction", {"link": raw}]}
            for wrapper, raw in members.items()
        ],
        {"name": "yatak_cover", "device_class": "blind",
         "features": ["hold", {"link": list(members)}]},
    )
    group = "cover.yatak_cover"
    assert attr(hass, group, "current_position") == 100

    # wrapper -> original: the group commands each member wrapper, which commands its blind.
    await call(hass, "cover", "set_cover_position", group, position=30)
    for raw in members.values():
        assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 30
    for raw in members.values():
        await blind_moves(hass, raw, 30)
        await blind_travels(hass, raw, 100, 30)
    assert attr(hass, group, "current_position") == 30

    # original -> wrapper: one blind moved at the remote, the group shows the mean.
    await blind_travels(hass, "cover.yatak_blind_1", 30, 90)
    await advance(hass, freezer, 6)
    assert attr(hass, "cover.yatak_cover_1", "current_position") == 90
    assert attr(hass, group, "current_position") == 60


async def test_tuya_curtain(
    hass: HomeAssistant, mqtt_mock, calls, freezer: FrozenDateTimeFactory
) -> None:
    """salon_cover / misafir_cover: hold, invert, travel_time, link."""
    raw, wrapper = "cover.salon_curtain", "cover.salon_cover"
    hass.states.async_set(raw, "open", {"current_position": 0})
    with patch(
        "custom_components.gungors.features.travel_time.TravelTime._tt_friendly_name",
        return_value="salon_curtain",
    ):
        await setup_platform(
            hass, "cover",
            {"name": "salon_cover", "device_class": "curtain",
             "features": ["hold", "invert", "travel_time", {"link": raw}]},
        )
        assert attr(hass, wrapper, "current_position") == 100  # motor 0 = room open

        # wrapper -> original (inverted)
        await call(hass, "cover", "set_cover_position", wrapper, position=40)
        assert calls.of("cover", "set_cover_position", raw)[-1]["position"] == 60
        assert hass.states.get(wrapper).state == "closing"
        await advance(hass, freezer, 8)
        async_fire_mqtt_message(hass, "zigbee2mqtt/salon_curtain", json.dumps({"position": 60}))
        await hass.async_block_till_done()
        state = hass.states.get(wrapper)
        assert (state.state, state.attributes["current_position"]) == ("open", 40)

        # original -> wrapper: pulled by hand, the motor reports.
        await advance(hass, freezer, 30)
        calls.clear()
        async_fire_mqtt_message(hass, "zigbee2mqtt/salon_curtain", json.dumps({"position": 10}))
        await hass.async_block_till_done()
        assert attr(hass, wrapper, "current_position") == 90
        assert not calls.of("cover", "set_cover_position", raw)


# --- climate -----------------------------------------------------------------


async def test_trv_thermostat(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """sercan_thermostat ... yatak_thermostat: hold, pid, valve, temperature, link."""
    trv, sensor = "climate.sercan_climate", "sensor.sercan_sensor_temperature"
    opening = "number.sercan_climate_valve_opening_degree"
    closing = "number.sercan_climate_valve_closing_degree"
    wrapper = "climate.sercan_thermostat"
    trv_attrs = {"hvac_modes": ["heat", "off"], "min_temp": 5, "target_temp_step": 0.5}
    hass.states.async_set(trv, "heat", {**trv_attrs, "temperature": 19})
    hass.states.async_set(sensor, "18")
    hass.states.async_set(opening, "0")
    hass.states.async_set(closing, "100")
    await setup_platform(
        hass, "climate",
        {"name": "sercan_thermostat", "target_temp": 19, "max_temp": 35,
         "features": [
             "hold",
             {"pid": {"kp": 16, "ki": 0.01, "kd": 4, "keep_alive": {"minutes": 15},
                      "force_off": False, "debug": True}},
             {"valve": {"opening": opening, "closing": closing, "min": 10}},
             {"temperature": sensor},
             {"link": {"entity": trv, "cooldown_time": 1}},
         ]},
    )
    assert attr(hass, wrapper, "temperature") == 19
    assert attr(hass, wrapper, "current_temperature") == 18

    # wrapper -> original: target to the TRV, demand to the valve.
    await call(hass, "climate", "set_temperature", wrapper, temperature=21)
    assert calls.of("climate", "set_temperature", trv)[-1]["temperature"] == 21
    assert calls.of("number", "set_value", opening)[-1]["value"] > 10
    await set_state(hass, trv, "heat", **trv_attrs, temperature=21)
    await advance(hass, freezer, 2)
    assert attr(hass, wrapper, "temperature") == 21

    await call(hass, "climate", "set_hvac_mode", wrapper, hvac_mode="off")
    assert calls.of("climate", "set_hvac_mode", trv)[-1]["hvac_mode"] == "off"
    assert calls.of("number", "set_value", opening)[-1]["value"] == 10
    await set_state(hass, trv, "off", **trv_attrs, temperature=5)
    await advance(hass, freezer, 2)
    assert hass.states.get(wrapper).state == "off"

    # original -> wrapper: the TRV switched on and turned at its knob.
    await set_state(hass, trv, "heat", **trv_attrs, temperature=23)
    await advance(hass, freezer, 2)
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["temperature"]) == ("heat", 23)

    # The room sensor feeds the wrapper.
    await set_state(hass, sensor, "20.5")
    assert attr(hass, wrapper, "current_temperature") == 20.5


async def test_pwm_thermostat(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """salon_thermostat: hold, pid, pwm, temperature, link (EMS-ESP hc1)."""
    hc1, sensor, socket = "climate.thermostat_hc1", "sensor.thermostat_hc1_currtemp", "switch.koridor0_socket"
    wrapper = "climate.salon_thermostat"
    hc1_attrs = {"hvac_modes": ["heat", "off"], "min_temp": 5}
    hass.states.async_set(hc1, "heat", {**hc1_attrs, "temperature": 19})
    hass.states.async_set(sensor, "19")
    hass.states.async_set(socket, "off")
    mock_restore_cache(hass, [State(wrapper, "heat", {"temperature": 19})])
    await setup_platform(
        hass, "climate",
        {"name": "salon_thermostat", "target_temp": 19, "max_temp": 35,
         "features": [
             "hold",
             {"pid": {"kp": 150, "ki": 0, "kd": 0, "keep_alive": {"minutes": 15},
                      "force_off": False, "debug": True}},
             {"pwm": {"entity": socket, "period": "00:15:00", "min_cycle_duration": 60}},
             {"temperature": sensor},
             {"link": {"entity": hc1, "cooldown_time": 1}},
         ]},
    )
    assert not calls.of("switch", "turn_on", socket)  # at target: no demand

    # wrapper -> original: target to hc1, demand switches the socket.
    await call(hass, "climate", "set_temperature", wrapper, temperature=22)
    assert calls.of("climate", "set_temperature", hc1)[-1]["temperature"] == 22
    assert calls.of("switch", "turn_on", socket)
    await set_state(hass, socket, "on")
    assert attr(hass, wrapper, "hvac_action") == "heating"

    await set_state(hass, hc1, "heat", **hc1_attrs, temperature=22)  # echo
    await advance(hass, freezer, 2)

    # original -> wrapper: hc1 changed on the boiler panel.
    await set_state(hass, hc1, "heat", **hc1_attrs, temperature=18)
    await advance(hass, freezer, 2)
    assert attr(hass, wrapper, "temperature") == 18

    # The socket switched off by hand: shown at once, switched on again by
    # keep_alive while there is demand.
    await call(hass, "climate", "set_temperature", wrapper, temperature=23)
    await set_state(hass, socket, "off")
    assert attr(hass, wrapper, "hvac_action") == "idle"
    calls.clear()
    await advance(hass, freezer, 15 * 60 + 1)
    assert calls.of("switch", "turn_on", socket)


# --- lights ------------------------------------------------------------------

LIGHT_ATTRS = {
    "supported_color_modes": ["color_temp"], "color_mode": "color_temp",
    "supported_features": 44, "min_color_temp_kelvin": 2200, "max_color_temp_kelvin": 6500,
}


async def test_light_linked_to_light(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """banyo_light ... yemek_light: hold, link (Zigbee2MQTT light)."""
    lamp, wrapper = "light.salon_lamp", "light.salon_light"
    hass.states.async_set(lamp, "off", LIGHT_ATTRS)
    await setup_platform(hass, "light", {"name": "salon_light", "features": ["hold", {"link": lamp}]})
    assert hass.states.get(wrapper).state == "off"

    # wrapper -> original
    await call(hass, "light", "turn_on", wrapper, brightness=150)
    assert calls.of("light", "turn_on", lamp)[-1]["brightness"] == 150
    await call(hass, "light", "turn_on", wrapper, color_temp_kelvin=4000)
    assert calls.of("light", "turn_on", lamp)[-1]["color_temp_kelvin"] == 4000
    await set_state(hass, lamp, "on", **LIGHT_ATTRS, brightness=150, color_temp_kelvin=4000)
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["brightness"]) == ("on", 150)
    await call(hass, "light", "turn_off", wrapper)
    assert calls.of("light", "turn_off", lamp)
    await set_state(hass, lamp, "off", **LIGHT_ATTRS)
    assert hass.states.get(wrapper).state == "off"

    # original -> wrapper: wall switch / Zigbee2MQTT frontend.
    await advance(hass, freezer, 10)
    await set_state(hass, lamp, "on", **LIGHT_ATTRS, brightness=40, color_temp_kelvin=2700)
    state = hass.states.get(wrapper)
    assert (state.state, state.attributes["brightness"]) == ("on", 40)
    assert state.attributes["color_temp_kelvin"] == 2700
    await set_state(hass, lamp, "off", **LIGHT_ATTRS)
    assert hass.states.get(wrapper).state == "off"


async def test_light_linked_to_switch(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    """mutfak_light: hold, link: switch.yemek_2 (relay)."""
    relay, wrapper = "switch.yemek_2", "light.mutfak_light"
    hass.states.async_set(relay, "off")
    await setup_platform(hass, "light", {"name": "mutfak_light", "features": ["hold", {"link": relay}]})

    # wrapper -> original
    await call(hass, "light", "turn_on", wrapper)
    assert calls.of("switch", "turn_on", relay)
    await set_state(hass, relay, "on")
    assert hass.states.get(wrapper).state == "on"
    await call(hass, "light", "turn_off", wrapper)
    assert calls.of("switch", "turn_off", relay)
    await set_state(hass, relay, "off")
    assert hass.states.get(wrapper).state == "off"

    # original -> wrapper
    await advance(hass, freezer, 10)
    await set_state(hass, relay, "on")
    assert hass.states.get(wrapper).state == "on"
    await set_state(hass, relay, "unavailable")
    assert hass.states.get(wrapper).state == "unavailable"


async def test_light_group(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    """bahce_light_1 + bahce_light: hold, link [wrapper, UI group, light]."""
    hass.states.async_set("light.bahce_lamp_1", "off", LIGHT_ATTRS)
    hass.states.async_set("light.bahce_light_2", "off", LIGHT_ATTRS)
    hass.states.async_set("light.bahce_light_3", "off", LIGHT_ATTRS)
    members = ["light.bahce_light_1", "light.bahce_light_2", "light.bahce_light_3"]
    await setup_platform(
        hass, "light",
        {"name": "bahce_light_1", "features": ["hold", {"link": "light.bahce_lamp_1"}]},
        {"name": "bahce_light", "features": ["hold", {"link": members}]},
    )
    group = "light.bahce_light"
    assert hass.states.get(group).state == "off"

    # wrapper -> original: every member, the member wrapper passes it to its lamp.
    await call(hass, "light", "turn_on", group, brightness=200)
    assert calls.of("light", "turn_on", "light.bahce_lamp_1")[-1]["brightness"] == 200
    for member in members[1:]:
        assert calls.of("light", "turn_on", member)[-1]["brightness"] == 200
    for entity_id in ("light.bahce_lamp_1", *members[1:]):
        await set_state(hass, entity_id, "on", **LIGHT_ATTRS, brightness=200)
    assert hass.states.get(group).state == "on"
    assert hass.states.get("light.bahce_light_1").state == "on"

    # original -> wrapper: all off at the wall, lamp 1 last.
    await advance(hass, freezer, 10)
    for entity_id in (*members[1:], "light.bahce_lamp_1"):
        await set_state(hass, entity_id, "off", **LIGHT_ATTRS)
    await hass.async_block_till_done()  # lamp -> member wrapper -> group: two hops
    assert hass.states.get("light.bahce_light_1").state == "off"
    assert hass.states.get(group).state == "off"
    await set_state(hass, "light.bahce_light_3", "on", **LIGHT_ATTRS, brightness=30)
    assert hass.states.get(group).state == "on"
