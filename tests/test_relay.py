"""Pushbuttons: `relay` (a key's LED relay follows the key's function)."""
from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gungors.features import button as button_mod, link_light, relay as relay_mod

from .conftest import setup_platform
from .test_button import EVT, WRAPPER, original, press
from .test_program import LIGHT, THERMOSTAT, lamp, setup_all

RELAY = "switch.salon_led"
RELAY_3 = "switch.salon_led_3"
FAN = "fan.salon_fan"


@pytest.fixture(autouse=True)
def fast(monkeypatch) -> None:
    monkeypatch.setattr(link_light, "BLINK_ON", 0)
    monkeypatch.setattr(link_light, "BLINK_OFF", 0)
    monkeypatch.setattr(link_light, "BLINK_SETTLE", 0)
    monkeypatch.setattr(button_mod, "WARN_DELAY", 0)


def relay_echo(hass: HomeAssistant, *relays: str) -> None:
    """The relays report what they are told."""

    @callback
    def handle(event) -> None:
        if event.data["domain"] != "switch":
            return
        ids = event.data["service_data"].get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        for entity_id in ids:
            if entity_id in relays:
                hass.states.async_set(entity_id, "on" if event.data["service"] == "turn_on" else "off")

    hass.bus.async_listen(EVENT_CALL_SERVICE, handle)


async def settle(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float = 1) -> None:
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await hass.async_block_till_done()


async def setup_relay(hass: HomeAssistant, relay: dict, buttons=None, custom=None, keys=3,
                      relay_state: str = "off") -> None:
    await setup_all(hass)
    for entity_id in relay.values():
        hass.states.async_set(entity_id, relay_state)
    relay_echo(hass, *relay.values())
    original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    features = [{"buttons": buttons or [{"light": {"button": 1, "target": {"entity_id": LIGHT}}}]}]
    if custom:
        features.append({"custom": custom})
    features += [{"relay": relay}, {"link": EVT}]
    await setup_platform(
        hass, "event", {"name": "salon_pushbutton", "keys": keys, "door": "left", "features": features}
    )


def relay_calls(calls, service: str, entity_id: str = RELAY) -> list[dict]:
    return calls.of("switch", service, entity_id)


async def test_relay_follows_light(hass: HomeAssistant, calls, freezer) -> None:
    # The light is on at start: the relay is synced on.
    await setup_relay(hass, {1: RELAY})
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "on"
    assert hass.states.get(WRAPPER).attributes["relays"] == {1: {"entity_id": RELAY, "on": True}}
    # The light goes off (any way): the relay follows.
    lamp(hass, False)
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "off"
    lamp(hass, True, 100)
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "on"


async def test_press_does_not_switch_relay_itself(hass: HomeAssistant, calls, freezer) -> None:
    await setup_relay(hass, {1: RELAY})
    await settle(hass, freezer)
    calls.clear()
    # Single press turns the light off; the relay is switched once, after the lamp reports.
    await press(hass, "1_single")
    await settle(hass, freezer)
    assert hass.states.get(LIGHT).state == "off"
    assert len(relay_calls(calls, "turn_off")) == 1
    assert not relay_calls(calls, "turn_on")
    # A press that keeps the light on (double: dim) does not touch the relay.
    await press(hass, "1_single")
    await settle(hass, freezer)
    calls.clear()
    await press(hass, "1_double")
    await settle(hass, freezer)
    assert hass.states.get(LIGHT).state == "on"
    assert not relay_calls(calls, "turn_on") and not relay_calls(calls, "turn_off")


async def test_relay_switched_by_hand_goes_back(hass: HomeAssistant, calls, freezer) -> None:
    await setup_relay(hass, {1: RELAY})
    await settle(hass, freezer)
    hass.states.async_set(RELAY, "off")
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "on"


async def test_unknown_target_or_relay_left_alone(hass: HomeAssistant, calls, freezer) -> None:
    await setup_relay(hass, {1: RELAY})
    await settle(hass, freezer)
    calls.clear()
    hass.states.async_set("light.salon_lamp", "unavailable")
    await settle(hass, freezer)
    assert not relay_calls(calls, "turn_off")
    assert hass.states.get(WRAPPER).attributes["relays"][1]["on"] is None
    # Relay unavailable: nothing is sent to it.
    hass.states.async_set(RELAY, "unavailable")
    lamp(hass, False)
    await settle(hass, freezer)
    assert not relay_calls(calls, "turn_off")
    # Back: synced.
    hass.states.async_set(RELAY, "on")
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "off"


async def test_relay_waits_for_blink(hass: HomeAssistant, calls, freezer, monkeypatch) -> None:
    await setup_relay(hass, {1: RELAY})
    lamp(hass, False)
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "off"
    calls.clear()
    light = hass.data["gungors"]["entities"][LIGHT]
    # During a blink the light flashes on: the relay waits.
    light.gw_blinking = True
    lamp(hass, True, 255)
    await settle(hass, freezer)
    lamp(hass, False)
    await settle(hass, freezer)
    assert not relay_calls(calls, "turn_on")
    light.gw_blinking = False
    await settle(hass, freezer, 2)
    assert not relay_calls(calls, "turn_on") and not relay_calls(calls, "turn_off")


async def test_relay_other_functions(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set(FAN, "off")
    await setup_relay(
        hass, {1: RELAY, 3: RELAY_3},
        buttons=[{"light": {"button": 1, "target": {"entity_id": LIGHT}}},
                 {"climate": {"button": 3, "target": {"entity_id": THERMOSTAT}}}],
        custom={"2_single": {"action": "fan.toggle", "target": {"entity_id": FAN}}},
        relay_state="on",
    )
    await settle(hass, freezer)
    # Climate off -> key 3's relay off.
    assert hass.states.get(RELAY_3).state == "off"
    hass.states.async_set("climate.salon_climate", "heat", {"hvac_modes": ["heat", "off"], "temperature": 20})
    await settle(hass, freezer)
    assert hass.states.get(RELAY_3).state == "on"


async def test_relay_custom_slot(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set(FAN, "off")
    await setup_relay(
        hass, {2: RELAY},
        custom={"2_single": {"action": "fan.toggle", "target": {"entity_id": FAN}}},
        keys=2,
    )
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "off"
    hass.states.async_set(FAN, "on")
    await settle(hass, freezer)
    assert hass.states.get(RELAY).state == "on"


@pytest.mark.parametrize(
    ("relay", "keys", "buttons"),
    [
        ({3: RELAY}, 2, None),  # no such key
        ({2: RELAY}, 3, None),  # key without a function
        ({1: "cover.salon_cover"}, 3, None),  # not a switch or light
        ({1: RELAY, 2: RELAY}, 3, [{"light": {"button": 1}}, {"light": {"button": 2}}]),
    ],
)
async def test_relay_config_errors(hass: HomeAssistant, calls, relay, keys, buttons) -> None:
    await setup_all(hass)
    original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    features = [{"buttons": buttons or [{"light": {"button": 1, "target": {"entity_id": LIGHT}}}]},
                {"relay": relay}, {"link": EVT}]
    await setup_platform(
        hass, "event", {"name": "salon_pushbutton", "keys": keys, "door": "left", "features": features}
    )
    assert hass.states.get(WRAPPER) is None


async def test_relay_claimed_twice(hass: HomeAssistant, calls) -> None:
    """A relay another wrapper writes is a config error."""
    await setup_all(hass)
    hass.states.async_set(RELAY, "off")
    hass.data["gungors"]["claims"][RELAY] = "led_light"  # another wrapper's link
    original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    await setup_platform(
        hass, "event",
        {"name": "salon_pushbutton", "keys": 1, "features": [
            {"buttons": [{"light": {"target": {"entity_id": LIGHT}}}]}, {"relay": {1: RELAY}},
            {"link": EVT}]},
    )
    assert hass.states.get(WRAPPER) is None
