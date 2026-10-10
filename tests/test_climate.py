"""Climate features: link, temperature, pid, valve, pwm, hold."""
from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant, State

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


async def setup_trv(hass: HomeAssistant, *extra) -> None:
    hass.states.async_set(SENSOR, "18")
    hass.states.async_set(OPENING, "0")
    hass.states.async_set(CLOSING, "100")
    await setup_platform(
        hass,
        "climate",
        {
            "name": "sercan_thermostat",
            "target_temp": 19,
            "features": [
                *extra,
                {"pid": {"kp": 16, "ki": 0.01, "kd": 4, "keep_alive": {"minutes": 15},
                         "force_off": False}},
                {"valve": {"opening": OPENING, "closing": CLOSING, "min": 10}},
                {"temperature": SENSOR},
                {"link": {"entity": TRV, "cooldown_time": 1}},
            ],
        },
    )


async def climate(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "climate", service, {"entity_id": WRAPPER, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def test_pid_drives_valves(hass: HomeAssistant, calls) -> None:
    trv(hass)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    state = hass.states.get(WRAPPER)
    assert state.state == "heat"
    assert state.attributes["temperature"] == 21
    assert state.attributes["current_temperature"] == 18
    assert state.attributes["min_temp"] == 5  # from the TRV
    # Startup: the wrapper's target is pushed to the TRV.
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 21

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
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    calls.clear()

    await climate(hass, "set_temperature", temperature=22.5)
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 22.5

    await climate(hass, "set_hvac_mode", hvac_mode="off")
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "off"
    state = hass.states.get(WRAPPER)
    assert state.state == "off"
    assert state.attributes["pre_off_target_temp"] == 22.5
    assert state.attributes["hvac_action"] == "off"
    # Switching off writes demand 0 once.
    assert calls.of("number", "set_value", OPENING)[-1]["value"] == 10

    # Setting a temperature while OFF switches to heat.
    trv(hass, mode="off", temperature=5)
    await hass.async_block_till_done()
    calls.clear()
    await climate(hass, "set_temperature", temperature=20)
    assert hass.states.get(WRAPPER).state == "heat"
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "heat"


async def test_leaving_off_restores_target(hass: HomeAssistant, calls) -> None:
    trv(hass, mode="off", temperature=5)
    mock_restore_cache(
        hass, [State(WRAPPER, "off", {"temperature": 5, "pre_off_target_temp": 21})]
    )
    await setup_trv(hass)
    await climate(hass, "set_hvac_mode", hvac_mode="heat")
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21


async def test_trv_change_applied_after_cooldown(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass)
    trv(hass, temperature=23)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 21
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 23

    # Unsupported mode on the device (auto): ignored.
    trv(hass, mode="auto", temperature=23)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
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

    # Back: the wrapper's state is pushed again.
    trv(hass, temperature=15)
    await hass.async_block_till_done()
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 21


async def test_legacy_restore(hass: HomeAssistant, calls) -> None:
    """SmartThermostat attributes and an unavailable last state are restored."""
    snapshot = State(
        WRAPPER, "heat",
        {"temperature": 20.5, "kp": 30, "ki": 0.02, "kd": 5, "ke": 0, "pid_i": 12.5,
         "pid_mode": "auto", "pre_off_target_temp": 21},
    ).as_dict()
    mock_restore_cache_with_extra_data(
        hass, [(State(WRAPPER, "unavailable"), {"last_available_state": snapshot})]
    )
    trv(hass, temperature=20.5)
    await setup_trv(hass)
    state = hass.states.get(WRAPPER)
    assert state.state == "heat"
    assert state.attributes["temperature"] == 20.5
    assert state.attributes["kp"] == 30
    assert state.attributes["pid_i"] == 12.5


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


async def test_hold_climate(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    trv(hass, temperature=21)
    mock_restore_cache(hass, [State(WRAPPER, "heat", {"temperature": 21})])
    await setup_trv(hass, "hold")
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": WRAPPER, "state": {"hvac_mode": "heat", "temperature": 17}},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 17
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 17
    trv(hass, temperature=17)
    await hass.async_block_till_done()

    # Someone turns the TRV knob: pushed back after the cooldown.
    calls.clear()
    trv(hass, temperature=24)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["temperature"] == 17
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 17


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
