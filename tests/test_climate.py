"""Climate features: link, temperature, pid, valve, pwm."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

import pytest

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from .conftest import setup_platform

TRV = "climate.sercan_climate"
SENSOR = "sensor.sercan_temperature"
OPENING = "number.sercan_valve_opening"
CLOSING = "number.sercan_valve_closing"
WRAPPER = "climate.sercan_thermostat"


def trv(hass: HomeAssistant, mode: str = "heat", temperature: float = 19, **attrs) -> None:
    hass.states.async_set(
        TRV,
        mode,
        {"hvac_modes": ["heat", "off", "auto"], "temperature": temperature, "min_temp": 5,
         "target_temp_step": 0.5, **attrs},
    )


def echo_trv(hass: HomeAssistant, entity: str = TRV) -> None:
    """The thermostat reports what it is told, like the real device does."""

    @callback
    def echo(event) -> None:
        data = event.data["service_data"]
        if event.data["domain"] != "climate" or data.get("entity_id") != entity:
            return
        state = hass.states.get(entity)
        mode, attrs = state.state, dict(state.attributes)
        if "hvac_mode" in data:
            mode = data["hvac_mode"]
        if "temperature" in data:
            attrs["temperature"] = data["temperature"]
        hass.states.async_set(entity, mode, attrs)

    hass.bus.async_listen(EVENT_CALL_SERVICE, echo)


async def setup_trv(hass: HomeAssistant, *extra, echo: bool = False) -> None:
    if echo:
        echo_trv(hass)
    hass.states.async_set(SENSOR, "18")
    hass.states.async_set(OPENING, "0")
    hass.states.async_set(CLOSING, "100")
    await setup_platform(
        hass,
        "climate",
        {
            "name": "sercan_thermostat",
            "features": [
                *extra,
                {"pid": {"kp": 16, "ki": 0.01, "kd": 4, "keep_alive": {"minutes": 15},
                         "force_off": False}},
                {"valve": {"opening": OPENING, "closing": CLOSING, "min": 10}},
                {"temperature": SENSOR},
                {"link": TRV},
            ],
        },
    )


async def climate(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "climate", service, {"entity_id": WRAPPER, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def test_pid_drives_valves(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    state = hass.states.get(WRAPPER)
    # Mode and target are the TRV's; nothing is pushed to it.
    assert state.state == "heat"
    assert state.attributes["temperature"] == 21
    assert state.attributes["current_temperature"] == 18
    assert state.attributes["min_temp"] == 5  # from the TRV
    assert not calls.of("climate", "set_temperature", TRV)

    # error 3 * kp 16 = 48 -> opening 10 + 48 * 0.9 = 53
    assert state.attributes["control_output"] == 48
    assert state.attributes["heater_value"] == 48
    assert state.attributes["hvac_action"] == "heating"
    assert calls.of("number", "set_value", OPENING)[-1]["value"] == 53
    assert calls.of("number", "set_value", CLOSING)[-1]["value"] == 47

    hass.states.async_set(OPENING, "53")
    hass.states.async_set(CLOSING, "47")
    hass.states.async_set(SENSOR, "22")
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert state.attributes["heater_value"] == 0
    assert state.attributes["hvac_action"] == "idle"
    assert calls.of("number", "set_value", OPENING)[-1]["value"] == 10


async def test_wrapper_to_trv_and_off(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass, echo=True)
    calls.clear()

    await climate(hass, "set_temperature", temperature=22.5)
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 22.5
    assert hass.states.get(WRAPPER).attributes["temperature"] == 22.5

    await climate(hass, "set_hvac_mode", hvac_mode="off")
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "off"
    state = hass.states.get(WRAPPER)
    assert state.state == "off"
    assert "pre_off_target_temp" not in state.attributes
    assert state.attributes["hvac_action"] == "off"
    # Switching off writes demand 0 once.
    assert calls.of("number", "set_value", OPENING)[-1]["value"] == 10

    # Setting a temperature while OFF switches to heat.
    calls.clear()
    await climate(hass, "set_temperature", temperature=20)
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("heat", 20)
    services = [s for d, s, data in calls.items if d == "climate" and data["entity_id"] == TRV]
    assert sorted(services) == ["set_hvac_mode", "set_temperature"]


async def test_command_shows_only_when_the_trv_reports(hass: HomeAssistant, calls) -> None:
    """A wrapper with only `link` is the TRV: a command changes nothing by itself."""
    trv(hass, temperature=21)
    await setup_trv(hass)
    await climate(hass, "set_temperature", temperature=22.5)
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 22.5
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21
    trv(hass, temperature=22.5)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 22.5

    await climate(hass, "set_hvac_mode", hvac_mode="off")
    assert hass.states.get(WRAPPER).state == "heat"
    # The TRV's own off setpoint comes with it.
    trv(hass, mode="off", temperature=5)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("off", 5)


async def test_leaving_off_uses_the_trv_target(hass: HomeAssistant, calls) -> None:
    trv(hass, mode="off", temperature=5)
    await setup_trv(hass, echo=True)
    await climate(hass, "set_hvac_mode", hvac_mode="heat")
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("heat", 5)
    assert not calls.of("climate", "set_temperature", TRV)


async def test_trv_change_is_taken_at_once(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    trv(hass, temperature=23)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 23
    assert not calls.of("climate", "set_temperature", TRV)

    # Unsupported mode on the device (auto): ignored.
    trv(hass, mode="auto", temperature=23)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "heat"


async def test_unavailable_trv(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    hass.states.async_set(TRV, "unavailable")
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert state.state == "unavailable"
    calls.clear()
    hass.states.async_set(SENSOR, "17")
    await hass.async_block_till_done()
    assert not calls.of("number", "set_value")

    # Back: the wrapper takes the TRV's state; nothing is pushed.
    trv(hass, temperature=15)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 15
    assert not calls.of("climate", "set_temperature", TRV)


async def test_pid_services(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    await hass.services.async_call(
        "gungors", "set_pid_gain", {"entity_id": WRAPPER, "kp": 10}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["kp"] == 10
    await hass.services.async_call(
        "gungors", "clear_integral", {"entity_id": WRAPPER}, blocking=True
    )
    assert hass.states.get(WRAPPER).attributes["pid_i"] == 0
    await hass.services.async_call(
        "gungors", "set_pid_mode", {"entity_id": WRAPPER, "mode": "off"}, blocking=True
    )
    assert hass.states.get(WRAPPER).attributes["pid_mode"] == "off"


async def test_pwm(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("climate.thermostat_hc1", "heat",
                          {"hvac_modes": ["heat", "off"], "temperature": 21})
    hass.states.async_set("sensor.hc1_temperature", "15")
    hass.states.async_set("switch.koridor0_socket", "off")
    mock_restore_cache(hass, [State("climate.salon_thermostat", "heat", {"temperature": 21})])
    await setup_platform(
        hass,
        "climate",
        {
            "name": "salon_thermostat",
            "features": [
                {"pid": {"kp": 150, "ki": 0, "kd": 0}},
                {"pwm": {"entity": "switch.koridor0_socket", "period": "00:15:00",
                         "min_cycle_duration": 60}},
                {"temperature": "sensor.hc1_temperature"},
                {"link": "climate.thermostat_hc1"},
            ],
        },
    )
    # error 6 * 150 -> clamped 100: heater on.
    assert calls.of("switch", "turn_on", "switch.koridor0_socket")
    hass.states.async_set("switch.koridor0_socket", "on")
    await hass.async_block_till_done()
    assert hass.states.get("climate.salon_thermostat").attributes["hvac_action"] == "heating"


async def test_bad_compositions_are_not_created(hass: HomeAssistant, calls) -> None:
    trv(hass)
    hass.states.async_set(SENSOR, "18")
    await setup_platform(
        hass,
        "climate",
        # pid without temperature
        {"name": "a", "features": [{"pid": {"kp": 1, "ki": 0, "kd": 0}}, {"link": TRV}]},
        # link not last
        {"name": "b", "features": [{"link": "climate.other"}, {"temperature": SENSOR}]},
        # TRV already written by the first valid wrapper
        {"name": "c", "features": [{"link": "climate.free"}]},
        {"name": "d", "features": [{"link": "climate.free"}]},
    )
    assert hass.states.get("climate.a") is None
    assert hass.states.get("climate.b") is None
    assert hass.states.get("climate.c") is not None
    assert hass.states.get("climate.d") is None


# --- helpers for the tests below ---------------------------------------------

HC1 = "climate.thermostat_hc1"
HC1_SENSOR = "sensor.hc1_temperature"
SOCKET = "switch.koridor0_socket"
SALON = "climate.salon_thermostat"


async def advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def set_state(hass: HomeAssistant, entity_id: str, state: str, **attrs) -> None:
    hass.states.async_set(entity_id, state, attrs)
    await hass.async_block_till_done()


def attr(hass: HomeAssistant, entity_id: str, name: str):
    return hass.states.get(entity_id).attributes.get(name)


def entity(hass: HomeAssistant, entity_id: str = WRAPPER):
    return hass.data["gungors"]["entities"][entity_id]


async def setup_pwm(hass: HomeAssistant, sensor: str = "19", **pid) -> None:
    """kp 20 and 2 °C below 21: demand 40, i.e. 6 min on per 15 min period."""
    hass.states.async_set(HC1, "heat", {"hvac_modes": ["heat", "off"], "temperature": 21})
    hass.states.async_set(HC1_SENSOR, sensor)
    hass.states.async_set(SOCKET, "off")
    echo_trv(hass, HC1)
    await setup_platform(
        hass,
        "climate",
        {
            "name": "salon_thermostat",
            "features": [
                {"pid": {"kp": 20, "ki": 0, "kd": 0, **pid}},
                {"pwm": {"entity": SOCKET, "period": "00:15:00", "min_cycle_duration": 60}},
                {"temperature": HC1_SENSOR},
                {"link": HC1},
            ],
        },
    )


# --- pwm -----------------------------------------------------------------------


async def test_pwm_cycle(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    await setup_pwm(hass)
    assert attr(hass, SALON, "control_output") == 40
    assert calls.of("switch", "turn_on", SOCKET)  # off long enough: on
    await set_state(hass, SOCKET, "on")
    assert attr(hass, SALON, "hvac_action") == "heating"

    # A new sample within the on time: stays on (refreshed).
    calls.clear()
    await set_state(hass, HC1_SENSOR, "19.1")
    assert calls.of("switch", "turn_on", SOCKET)
    assert not calls.of("switch", "turn_off", SOCKET)

    # 6 min later the on time (~5.4 min at demand 36) is used up: off.
    await advance(hass, freezer, 360)
    calls.clear()
    await set_state(hass, HC1_SENSOR, "19.2")
    assert calls.of("switch", "turn_off", SOCKET)
    await set_state(hass, SOCKET, "off")
    assert attr(hass, SALON, "hvac_action") == "idle"

    # Within the off time: stays off (refreshed).
    calls.clear()
    await set_state(hass, HC1_SENSOR, "19.1")
    assert calls.of("switch", "turn_off", SOCKET)
    assert not calls.of("switch", "turn_on", SOCKET)

    # Full demand right after switching off: rejected, the off cycle is too short.
    calls.clear()
    await set_state(hass, HC1_SENSOR, "15")
    assert attr(hass, SALON, "control_output") == 100
    assert not calls.of("switch", "turn_on", SOCKET)
    # Two minutes later it may switch on.
    await advance(hass, freezer, 120)
    await set_state(hass, HC1_SENSOR, "15.1")
    assert calls.of("switch", "turn_on", SOCKET)
    await set_state(hass, SOCKET, "on")

    # No demand right after switching on: rejected, the on cycle is too short.
    calls.clear()
    await set_state(hass, HC1_SENSOR, "23")
    assert attr(hass, SALON, "control_output") == 0
    assert not calls.of("switch", "turn_off", SOCKET)
    await advance(hass, freezer, 120)
    await set_state(hass, HC1_SENSOR, "23.1")
    assert calls.of("switch", "turn_off", SOCKET)


async def test_pwm_target_below_room_switches_off(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass)
    await set_state(hass, SOCKET, "on")
    await advance(hass, freezer, 120)
    calls.clear()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": SALON, "temperature": 18}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls.of("climate", "set_temperature", HC1)[-1]["temperature"] == 18
    assert calls.of("switch", "turn_off", SOCKET)


async def test_pwm_target_above_room_switches_on_at_once(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass, sensor="21")  # at target: no demand, socket off
    assert not calls.of("switch", "turn_on", SOCKET)
    await advance(hass, freezer, 120)
    calls.clear()
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": SALON, "temperature": 22}, blocking=True
    )
    await hass.async_block_till_done()
    # Demand 20 (3 min on): switched on now, not at the end of an off time.
    assert attr(hass, SALON, "control_output") == 20
    assert calls.of("switch", "turn_on", SOCKET)


async def test_pwm_device_unavailable_forces_off(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass)
    await set_state(hass, SOCKET, "on")
    calls.clear()
    # Even right after switching on (shorter than min_cycle_duration).
    await set_state(hass, HC1, "unavailable")
    assert hass.states.get(SALON).state == "unavailable"
    assert calls.of("switch", "turn_off", SOCKET)


async def test_pwm_switch_unavailable(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass, sensor="21")
    await set_state(hass, SOCKET, "unavailable")
    calls.clear()
    await set_state(hass, HC1_SENSOR, "15")  # full demand
    await set_state(hass, HC1_SENSOR, "25")  # no demand
    assert not calls.of("switch", "turn_on", SOCKET)
    assert not calls.of("switch", "turn_off", SOCKET)


async def test_pid_force_off_while_heater_still_on(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass)
    await set_state(hass, SOCKET, "on")
    calls.clear()
    # Off right after switching on: demand 0, but the on cycle is too short.
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": SALON, "hvac_mode": "off"}, blocking=True
    )
    await hass.async_block_till_done()
    assert attr(hass, SALON, "control_output") == 0
    assert not calls.of("switch", "turn_off", SOCKET)
    # force_off: the next cycle sets 0 again, now accepted.
    await advance(hass, freezer, 120)
    await set_state(hass, HC1_SENSOR, "19.1")
    assert calls.of("switch", "turn_off", SOCKET)


# --- pid -----------------------------------------------------------------------


async def test_pid_mode_off_is_a_hysteresis_switch(hass: HomeAssistant, calls) -> None:
    await setup_pwm(hass)
    await hass.services.async_call(
        "gungors", "set_pid_mode", {"entity_id": SALON, "mode": "off"}, blocking=True
    )
    assert attr(hass, SALON, "control_output") == 100  # 2 °C below: full
    await set_state(hass, HC1_SENSOR, "21.2")  # within the hot tolerance: unchanged
    assert attr(hass, SALON, "control_output") == 100
    await set_state(hass, HC1_SENSOR, "21.4")  # 0.4 above: nothing
    assert attr(hass, SALON, "control_output") == 0
    await set_state(hass, HC1_SENSOR, "20.9")  # within the cold tolerance: unchanged
    assert attr(hass, SALON, "control_output") == 0
    await set_state(hass, HC1_SENSOR, "20.6")  # 0.4 below: full
    assert attr(hass, SALON, "control_output") == 100


async def test_pid_sensor_stall_uses_output_safety(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(
        hass, keep_alive={"minutes": 5}, sensor_stall={"minutes": 10}, output_safety=15,
    )
    assert attr(hass, SALON, "control_output") == 40
    await advance(hass, freezer, 5 * 60 + 1)
    assert attr(hass, SALON, "control_output") == 40
    await advance(hass, freezer, 5 * 60 + 1)  # no sample for > 10 min
    assert attr(hass, SALON, "control_output") == 15
    await set_state(hass, HC1_SENSOR, "19.5")  # samples again
    assert attr(hass, SALON, "control_output") == 30


async def test_pid_integer_output(hass: HomeAssistant, calls) -> None:
    await setup_pwm(hass, sensor="19.43", output_precision=0)
    output = attr(hass, SALON, "control_output")
    assert output == 31 and isinstance(output, int)


async def test_pid_save(hass: HomeAssistant, calls) -> None:
    await setup_pwm(hass)
    await hass.services.async_call(
        "gungors", "set_pid_gain", {"entity_id": SALON, "kp": 25, "ki": 0.5}, blocking=True
    )
    await hass.services.async_call(
        "gungors", "clear_integral", {"entity_id": SALON, "value": 3.5}, blocking=True
    )
    saved = entity(hass, SALON).extra_restore_state_data.as_dict()
    assert saved["pid"] == {"kp": 25, "ki": 0.5, "kd": 0, "ke": 0, "integral": 3.5,
                            "mode": "AUTO"}
    assert "base" not in saved  # mode and target are the thermostat's


async def test_pid_restore_new_format(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(HC1, "heat", {"hvac_modes": ["heat", "off"], "temperature": 20})
    hass.states.async_set(HC1_SENSOR, "20")
    hass.states.async_set(SOCKET, "off")
    mock_restore_cache_with_extra_data(
        hass,
        [(State(SALON, "heat", {"temperature": 99}),
          {"base": {"hvac_mode": "heat", "target": 20, "pre_off": 18},
           "pid": {"kp": 30, "ki": 0.1, "kd": 2, "ke": 0, "integral": 7.0, "mode": "OFF"}})],
    )
    await setup_platform(
        hass,
        "climate",
        {"name": "salon_thermostat", "precision": 0.5, "min_temp": 10, "features": [
            {"pid": {"kp": 20, "ki": 0, "kd": 0}},
            {"pwm": {"entity": SOCKET, "period": "00:15:00"}},
            {"temperature": HC1_SENSOR},
            {"link": HC1},
        ]},
    )
    state = hass.states.get(SALON)
    assert state.attributes["temperature"] == 20  # the thermostat's, not the restored 99
    assert (state.attributes["kp"], state.attributes["pid_i"]) == (30, 7.0)
    assert state.attributes["pid_mode"] == "off"
    assert state.attributes["min_temp"] == 10
    assert entity(hass, SALON).precision == 0.5


# --- climate base ----------------------------------------------------------------


async def test_climate_without_link(hass: HomeAssistant, calls) -> None:
    """Only a sensor: no device, so no mode or target."""
    hass.states.async_set(SENSOR, "18")
    await setup_platform(
        hass, "climate", {"name": "sercan_thermostat", "features": [{"temperature": SENSOR}]}
    )
    state = hass.states.get(WRAPPER)
    assert state.state == "unknown"
    assert state.attributes["min_temp"] == 7
    assert state.attributes["temperature"] is None
    assert state.attributes["current_temperature"] == 18
    # The sensor removed: the last value stays.
    hass.states.async_remove(SENSOR)
    await hass.async_block_till_done()
    assert attr(hass, WRAPPER, "current_temperature") == 18


async def test_set_temperature_with_mode(
    hass: HomeAssistant, calls
) -> None:
    trv(hass, mode="off", temperature=5)
    await setup_trv(hass, echo=True)
    calls.clear()
    await climate(hass, "set_temperature", temperature=21, hvac_mode="heat")
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("heat", 21)
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "heat"


async def test_pwm_minimum_cycle_stretch(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    # Demand 5: 45 s on per period, stretched to the 60 s minimum.
    await setup_pwm(hass, sensor="20.75")
    assert attr(hass, SALON, "control_output") == 5
    assert calls.of("switch", "turn_on", SOCKET)
    await set_state(hass, SOCKET, "on")
    await advance(hass, freezer, 50)
    calls.clear()
    await set_state(hass, HC1_SENSOR, "20.76")
    assert not calls.of("switch", "turn_off", SOCKET)  # 60 s not yet used up
    await advance(hass, freezer, 15)
    await set_state(hass, HC1_SENSOR, "20.75")
    assert calls.of("switch", "turn_off", SOCKET)


async def test_pwm_minimum_off_stretch(hass: HomeAssistant, calls) -> None:
    # Demand 95: 45 s off per period, stretched to the 60 s minimum.
    await setup_pwm(hass, sensor="16.25")
    assert attr(hass, SALON, "control_output") == 95
    assert calls.of("switch", "turn_on", SOCKET)


# --- climate link: more ----------------------------------------------------------


async def test_trv_not_usable_at_start(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(TRV, "unknown")
    await setup_trv(hass)
    assert hass.states.get(WRAPPER).attributes["temperature"] is None
    # First real report: the wrapper is the TRV.
    trv(hass, temperature=17)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 17
    assert not calls.of("climate", "set_temperature", TRV)


async def test_request_while_trv_unknown_is_not_sent(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    await set_state(hass, TRV, "unknown")  # e.g. Zigbee2MQTT restarted
    calls.clear()
    await climate(hass, "set_temperature", temperature=22)
    assert not calls.of("climate", "set_temperature", TRV)  # nothing to send to
    trv(hass, temperature=21)  # reports again
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21
    assert not calls.of("climate", "set_temperature", TRV)


async def test_trv_attribute_only_change_is_ignored(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    calls.clear()
    trv(hass, temperature=21, current_temperature=19.5)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21
    assert not calls.items


async def test_trv_unknown_keeps_the_last_report(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    trv(hass, temperature=23)
    await hass.async_block_till_done()
    await set_state(hass, TRV, "unknown")
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("heat", 23)


async def test_trv_state_outside_hvac_modes(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass)
    trv(hass, mode="manual", temperature=23)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["temperature"]) == ("heat", 23)


async def test_command_to_what_the_trv_already_has_is_not_sent(
    hass: HomeAssistant, calls
) -> None:
    trv(hass, mode="off", temperature=5)
    await setup_trv(hass)
    calls.clear()
    await climate(hass, "set_hvac_mode", hvac_mode="off")
    # Off: no setpoint is written either.
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": WRAPPER, "temperature": 20, "hvac_mode": "off"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert not [c for c in calls.items if c[0] == "climate" and c[2]["entity_id"] == TRV]


async def test_switch_on_from_off_waits_for_mode(hass: HomeAssistant, calls) -> None:
    """The setpoint is sent only after the TRV reports heat (here: at once)."""
    trv(hass, mode="off", temperature=5)
    await setup_trv(hass)

    async def set_hvac_mode(call) -> None:
        trv(hass, mode=call.data["hvac_mode"], temperature=5)

    hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
    calls.clear()
    await climate(hass, "set_temperature", temperature=20)
    services = [s for d, s, data in calls.items if d == "climate" and data["entity_id"] == TRV]
    assert services == ["set_hvac_mode", "set_temperature"]
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 20


async def test_switch_on_from_off_mode_reported_later(hass: HomeAssistant, calls) -> None:
    trv(hass, mode="off", temperature=5)
    await setup_trv(hass)
    calls.clear()
    task = hass.async_create_task(
        hass.services.async_call(
            "climate", "set_temperature", {"entity_id": WRAPPER, "temperature": 20}, blocking=True
        )
    )
    while not calls.of("climate", "set_hvac_mode", TRV):
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    assert not calls.of("climate", "set_temperature", TRV)  # waiting for the mode
    trv(hass, mode="heat", temperature=5)  # the TRV reports heat
    await task
    await hass.async_block_till_done()
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 20


async def test_trv_service_failure_is_logged(
    hass: HomeAssistant, calls, caplog: pytest.LogCaptureFixture
) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)

    async def fail(call) -> None:
        raise HomeAssistantError("Zigbee2MQTT timeout")

    # Replaces the climate entity service too, so the wrapper is called directly.
    hass.services.async_register("climate", "set_temperature", fail)
    await entity(hass).async_set_temperature(temperature=22)
    await hass.async_block_till_done()
    assert "climate.set_temperature on climate.sercan_climate failed" in caplog.text
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21  # the TRV's


# --- valve: more ---------------------------------------------------------------


async def test_valve_numbers_already_set(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    hass.states.async_set(SENSOR, "18")
    hass.states.async_set(OPENING, "53")
    hass.states.async_set(CLOSING, "47")
    await setup_platform(
        hass,
        "climate",
        {"name": "sercan_thermostat", "features": [
            {"pid": {"kp": 16, "ki": 0, "kd": 0}},
            {"valve": {"opening": OPENING, "closing": CLOSING, "min": 10}},
            {"temperature": SENSOR},
            {"link": TRV},
        ]},
    )
    assert hass.states.get(WRAPPER).attributes["heater_value"] == 48
    assert not calls.of("number", "set_value")


async def test_valve_write_failure_is_logged(
    hass: HomeAssistant, calls, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(call) -> None:
        raise HomeAssistantError("device offline")

    hass.services.async_register("number", "set_value", fail)
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    assert "setting ['number.sercan_valve_opening'] to 53 failed" in caplog.text
    assert hass.states.get(WRAPPER).attributes["heater_value"] == 48



# --- heating above the target ----------------------------------------------------


async def test_pid_no_heat_above_target_with_wound_up_integral(
    hass: HomeAssistant, calls
) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass, echo=True)
    await hass.services.async_call(
        "gungors", "clear_integral", {"entity_id": WRAPPER, "value": 100.0}, blocking=True
    )
    await set_state(hass, SENSOR, "21")
    # Target just below the room: 100 - 16 = 84 before, now no heat.
    await climate(hass, "set_temperature", temperature=20.5)
    state = hass.states.get(WRAPPER)
    assert state.attributes["control_output"] == 0
    assert state.attributes["hvac_action"] == "idle"


async def test_pid_integral_unwinds_above_target(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    trv(hass, temperature=20)
    await setup_trv(hass, echo=True)
    await hass.services.async_call(
        "gungors", "clear_integral", {"entity_id": WRAPPER, "value": 100.0}, blocking=True
    )
    await set_state(hass, SENSOR, "30")  # 100 - 16 * 10: output below 0
    await advance(hass, freezer, 600)
    await set_state(hass, SENSOR, "30.1")
    # ki 0.01 * -10.1 * 600 s = -60.6: the integral shrinks although the output is below 0.
    assert attr(hass, WRAPPER, "pid_i") == pytest.approx(39.4, abs=0.1)


async def test_pid_recalc_without_sample_integrates_nothing(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    trv(hass, temperature=20)
    await setup_trv(hass, echo=True)
    await advance(hass, freezer, 600)
    await set_state(hass, SENSOR, "19")
    before = attr(hass, WRAPPER, "pid_i")
    # Recalculations without a new sample do not add the last 600 s again.
    for _ in range(3):
        await hass.services.async_call(
            "gungors", "set_pid_gain", {"entity_id": WRAPPER, "kp": 16}, blocking=True
        )
    assert attr(hass, WRAPPER, "pid_i") == before


async def test_pid_off_clears_integral(hass: HomeAssistant, calls) -> None:
    trv(hass, temperature=21)
    await setup_trv(hass, echo=True)
    await hass.services.async_call(
        "gungors", "clear_integral", {"entity_id": WRAPPER, "value": 60.0}, blocking=True
    )
    await climate(hass, "set_hvac_mode", hvac_mode="off")
    assert attr(hass, WRAPPER, "pid_i") == 0


async def test_pwm_rejected_off_is_retried(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_pwm(hass)
    await set_state(hass, SOCKET, "on")
    calls.clear()
    # Target below the room right after switching on: rejected (on cycle too short) ...
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": SALON, "temperature": 18}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls.of("switch", "turn_off", SOCKET)
    # ... and done once min_cycle_duration is over, without a new sample.
    await advance(hass, freezer, 65)
    assert calls.of("switch", "turn_off", SOCKET)
