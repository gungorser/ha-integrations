"""Programs: calendar.program entries enforced by the `program` feature."""
from __future__ import annotations

from datetime import timedelta
import json

from freezegun.api import FrozenDateTimeFactory
import pytest
import voluptuous as vol

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import color as color_util, dt as dt_util

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gungors.rules import (
    Constraint,
    dump_program,
    parse_defaults,
    parse_program,
    rules_conflict,
)

from .conftest import setup_platform

LAMP = "light.salon_lamp"
LIGHT = "light.salon_light"
TRV = "climate.salon_climate"
THERMOSTAT = "climate.salon_thermostat"
BLIND = "cover.salon_blind"
COVER = "cover.salon_cover"
CALENDAR = "calendar.program"


# --- rules ---------------------------------------------------------------------


def test_parse_program() -> None:
    program = parse_program(
        "target: salon\nrules:\n  on: true\n  brightness: {max: 40}\n  temperature: 21"
    )
    assert program.target == ("salon",)
    assert program.rules["on"] == Constraint(eq=True)
    assert program.rules["brightness"] == Constraint(max=40)
    assert program.rules["temperature"] == Constraint(eq=21)
    program = parse_program(json.dumps({"target": ["light.x"], "rules": {"color": "red"}}))
    assert program.rules["color"] == Constraint(eq=(255, 0, 0))


def test_parse_program_by_domain() -> None:
    program = parse_program(
        "target: [Yatak]\nlight:\n  on: false\nclimate:\n  hvac_mode: off\n"
        "  temperature: {min: 21}\n"
    )
    assert program.target == ("Yatak",)
    assert program.rules == {
        "on": Constraint(eq=False),
        "hvac_mode": Constraint(eq="off"),
        "temperature": Constraint(min=21),
    }


def test_dump_program_round_trip() -> None:
    program = parse_program(
        "target: [Yatak, light.x, 'on']\nlight: {on: true, brightness: {max: 40}, color: red}\n"
        "climate: {hvac_mode: heat, temperature: 21.5}\ncover: {position: {min: 0, max: 30}}"
    )
    text = dump_program(program)
    assert text == (
        'target: [Yatak, light.x, "on"]\n'
        "light:\n  on: true\n  brightness: {max: 40}\n  color: [255, 0, 0]\n"
        "climate:\n  hvac_mode: heat\n  temperature: 21.5\n"
        "cover:\n  position: {min: 0, max: 30}\n"
    )
    assert parse_program(text) == program
    off = parse_program("target: x\nclimate: {hvac_mode: 'off'}")
    assert 'hvac_mode: "off"' in dump_program(off)
    assert parse_program(dump_program(off)) == off


@pytest.mark.parametrize(
    "description",
    [
        "",
        "not a mapping",
        "rules: {on: true}",
        "target: salon",
        "target: salon\nrules: {volume: 3}",
        "target: salon\nrules: {brightness: 140}",
        "target: salon\nrules: {brightness: {min: 60, max: 40}}",
        "target: salon\nrules: {color: nocolour}",
        "target: salon\nrules: {color: red, color_temp: 3000}",
        "target: salon\nlight: {color_temp: {min: 2700, max: 4000}}",
        "target: salon\nrules: {hvac_mode: cool}",
        "target: salon\nlight: {temperature: 21}",
        "target: salon\nclimate: {}",
        "target: salon\nheating: {temperature: 21}",
        "target: salon\nrules: {on: true}\nlight: {on: false}",
    ],
)
def test_parse_program_invalid(description: str) -> None:
    with pytest.raises(vol.Invalid):
        parse_program(description)


def test_set_flag_and_eq_only() -> None:
    program = parse_program(
        "target: x\nlight:\n  on: true\n  brightness: {min: 50, set: true}\n"
        "  color_temp: {eq: 3000, set: true}\n"
    )
    assert program.rules == {
        "on": Constraint(eq=True),
        "brightness": Constraint(min=50, set=True),
        "color_temp": Constraint(eq=3000, set=True),
    }
    text = dump_program(program)
    assert "brightness: {min: 50, set: true}" in text
    assert "color_temp: {eq: 3000, set: true}" in text
    assert parse_program(text) == program
    for bad in (
        "target: x\nlight: {color_temp: {min: 3000}}",
        "target: x\nclimate: {hvac_mode: {min: 1}}",
        "target: x\nlight: {brightness: {set: true}}",
        "target: x\nlight: {brightness: {min: 5, set: 1}}",
        # A set brightness keeps the light on.
        "target: x\nlight: {on: false, brightness: {min: 5, set: true}}",
    ):
        with pytest.raises(vol.Invalid):
            parse_program(bad)


def test_conflicts_with_implied_rules() -> None:
    lit = parse_program("target: x\nlight: {brightness: {min: 50, set: true}}").rules
    dark = parse_program("target: x\nlight: {on: false}").rules
    assert rules_conflict(lit, dark, "light") == "on"
    # A let brightness rule is satisfied by an off light.
    let = parse_program("target: x\nlight: {brightness: {min: 50}}").rules
    assert rules_conflict(let, dark, "light") is None
    heat = parse_program("target: x\nclimate: {temperature: {eq: 21, set: true}}").rules
    off = parse_program("target: x\nclimate: {hvac_mode: 'off'}").rules
    assert rules_conflict(heat, off, "climate") == "hvac_mode"


def test_conflicts() -> None:
    a = parse_program("target: x\nrules: {brightness: {max: 40}, color: red}").rules
    assert rules_conflict(a, parse_program("target: x\nrules: {brightness: {min: 60}}").rules,
                          "light") == "brightness"
    assert rules_conflict(a, parse_program("target: x\nrules: {brightness: {min: 30}}").rules,
                          "light") is None
    assert rules_conflict(a, parse_program("target: x\nrules: {color: blue}").rules,
                          "light") == "color"
    assert rules_conflict(a, parse_program("target: x\nrules: {color_temp: 3000}").rules,
                          "light") == "color/color_temp"
    # Rules of another domain never conflict here.
    assert rules_conflict(a, parse_program("target: x\nrules: {temperature: 21}").rules,
                          "light") is None


def test_defaults() -> None:
    assert parse_defaults("light", {"on": False, "color": "white"}) == {
        "on": False, "color": (255, 255, 255)
    }
    assert parse_defaults("cover", {"position": "sun"}) == {"position": "sun"}
    with pytest.raises(vol.Invalid):
        parse_defaults("light", {"temperature": 20})
    with pytest.raises(vol.Invalid):
        parse_defaults("light", {"brightness": {"max": 40}})


# --- helpers -------------------------------------------------------------------


def lamp(hass: HomeAssistant, on: bool, brightness: int | None = 120,
         rgb: tuple[int, int, int] | None = None) -> None:
    attrs = {"supported_color_modes": ["hs", "color_temp"], "supported_features": 0}
    if on:
        attrs["brightness"] = brightness
        if rgb is not None:
            attrs["color_mode"] = "hs"
            attrs["hs_color"] = color_util.color_RGB_to_hs(*rgb)
            attrs["rgb_color"] = rgb
        else:
            attrs["color_mode"] = "color_temp"
            attrs["color_temp_kelvin"] = 3000
            attrs["hs_color"] = (27.0, 44.0)
    hass.states.async_set(LAMP, "on" if on else "off", attrs)


def echo(hass: HomeAssistant) -> None:
    """The lamp, thermostat and blind report what they are told."""

    @callback
    def handle(event) -> None:
        domain, service = event.data["domain"], event.data["service"]
        data = event.data["service_data"]
        ids = data.get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if LAMP in ids:
            if service == "turn_off":
                lamp(hass, False)
                return
            current = hass.states.get(LAMP)
            brightness = data.get("brightness", current.attributes.get("brightness") or 255)
            rgb = data.get("rgb_color") or current.attributes.get("rgb_color")
            lamp(hass, True, brightness, tuple(rgb) if rgb else None)
        elif TRV in ids and domain == "climate":
            state = hass.states.get(TRV)
            mode, attrs = state.state, dict(state.attributes)
            mode = data.get("hvac_mode", mode)
            if "temperature" in data:
                attrs["temperature"] = data["temperature"]
            hass.states.async_set(TRV, mode, attrs)
        elif BLIND in ids and domain == "cover":
            position = data.get("position")
            if position is not None:
                blind(hass, position)

    hass.bus.async_listen(EVENT_CALL_SERVICE, handle)


def trv(hass: HomeAssistant, mode: str = "off", temperature: float = 18) -> None:
    hass.states.async_set(
        TRV, mode, {"hvac_modes": ["heat", "off"], "temperature": temperature, "min_temp": 5}
    )


def blind(hass: HomeAssistant, position: int) -> None:
    hass.states.async_set(
        BLIND, "closed" if position == 0 else "open",
        {"current_position": position, "supported_features": 15},
    )


async def setup_all(hass: HomeAssistant, calendar: dict | None = None) -> None:
    lamp(hass, True, 200)
    trv(hass)
    blind(hass, 100)
    hass.states.async_set("sun.sun", "above_horizon")
    echo(hass)
    await setup_platform(hass, "light",
                         {"name": "salon_light", "features": ["program", {"link": LAMP}]})
    await setup_platform(hass, "climate",
                         {"name": "salon_thermostat", "features": ["program", {"link": TRV}]})
    await setup_platform(hass, "cover",
                         {"name": "salon_cover", "features": ["program", {"link": BLIND}]})
    assert await async_setup_component(
        hass, "calendar", {"calendar": [{"platform": "gungors", **(calendar or {})}]}
    )
    await hass.async_block_till_done()


async def add_entry(
    hass: HomeAssistant, summary: str, program: dict, start: timedelta, end: timedelta,
    **extra,
) -> None:
    now = dt_util.now()
    await hass.services.async_call(
        "calendar",
        "create_event",
        {
            "entity_id": CALENDAR,
            "summary": summary,
            "start_date_time": (now + start).isoformat(),
            "end_date_time": (now + end).isoformat(),
            "description": json.dumps(program),
            **extra,
        },
        blocking=True,
    )
    await hass.async_block_till_done()


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def brightness(hass: HomeAssistant) -> int | None:
    return hass.states.get(LIGHT).attributes.get("brightness")


# --- calendar --------------------------------------------------------------------


async def test_entries_are_validated(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    with pytest.raises(HomeAssistantError, match="target"):
        await add_entry(hass, "empty", {}, timedelta(hours=1), timedelta(hours=2))
    with pytest.raises(HomeAssistantError, match="unknown area"):
        await add_entry(hass, "x", {"target": "nowhere", "rules": {"on": True}},
                        timedelta(hours=1), timedelta(hours=2))
    with pytest.raises(HomeAssistantError, match="not a gungors entity"):
        await add_entry(hass, "x", {"target": "light.other", "rules": {"on": True}},
                        timedelta(hours=1), timedelta(hours=2))

    await add_entry(hass, "melike_uyku", {"target": LIGHT, "rules": {"brightness": {"max": 40}}},
                    timedelta(hours=1), timedelta(hours=3))
    # Another attribute on the same light: no conflict.
    await add_entry(hass, "sercan_uyku", {"target": LIGHT, "rules": {"color": "red"}},
                    timedelta(hours=2), timedelta(hours=4))
    # Same attribute, can't hold together while they overlap: refused.
    with pytest.raises(HomeAssistantError, match="conflicts with 'melike_uyku'"):
        await add_entry(hass, "parlak", {"target": LIGHT, "rules": {"brightness": {"min": 60}}},
                        timedelta(hours=2), timedelta(hours=5))
    # The same rule after the first one ended is fine.
    await add_entry(hass, "parlak", {"target": LIGHT, "rules": {"brightness": {"min": 60}}},
                    timedelta(hours=3), timedelta(hours=5))

    state = hass.states.get(CALENDAR)
    assert state.attributes["message"] == "melike_uyku"


async def test_recurring_conflict(hass: HomeAssistant, calls, hass_ws_client) -> None:
    await setup_all(hass)
    client = await hass_ws_client(hass)
    now = dt_util.now()
    await client.send_json_auto_id(
        {
            "type": "calendar/event/create",
            "entity_id": CALENDAR,
            "event": {
                "summary": "gece",
                "dtstart": (now + timedelta(hours=1)).isoformat(),
                "dtend": (now + timedelta(hours=2)).isoformat(),
                "rrule": "FREQ=DAILY",
                "description": json.dumps({"target": LIGHT, "rules": {"on": False}}),
            },
        }
    )
    assert (await client.receive_json())["success"]
    # Three days later, inside the fourth occurrence.
    with pytest.raises(HomeAssistantError, match="conflicts with 'gece'"):
        await add_entry(hass, "parti", {"target": LIGHT, "rules": {"on": True}},
                        timedelta(days=3, hours=1, minutes=30), timedelta(days=3, hours=3))


async def test_area_target(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    area = ar.async_get(hass).async_create("Salon")
    er.async_get(hass).async_update_entity(LIGHT, area_id=area.id)
    await add_entry(hass, "loş", {"target": "Salon", "rules": {"brightness": {"max": 40}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    assert calls.of("light", "turn_on", LAMP)[-1]["brightness"] == 102
    assert brightness(hass) == 102


# --- light -----------------------------------------------------------------------


async def test_light_limits(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    await add_entry(hass, "melike_uyku", {"target": LIGHT, "rules": {"brightness": {"max": 40}}},
                    timedelta(minutes=1), timedelta(hours=1))
    assert brightness(hass) == 200
    await tick(hass, freezer, timedelta(minutes=1, seconds=2))
    # Corrected when the program started.
    assert brightness(hass) == 102
    program = hass.states.get(LIGHT).attributes["program"]
    assert [p["summary"] for p in program] == ["melike_uyku"]
    assert program[0]["rules"] == {"brightness": {"max": 40}}

    # A request above the limit is clamped.
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT, "brightness": 255},
                                   blocking=True)
    await hass.async_block_till_done()
    assert calls.of("light", "turn_on", LAMP)[-1]["brightness"] == 102
    # Below the limit is left alone; turning off is allowed.
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT, "brightness": 50},
                                   blocking=True)
    await hass.async_block_till_done()
    assert brightness(hass) == 50
    await hass.services.async_call("light", "turn_off", {"entity_id": LIGHT}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "off"

    # Turned on at the wall at full brightness: pushed back to the limit.
    await tick(hass, freezer, timedelta(seconds=15))
    lamp(hass, True, 255)
    await hass.async_block_till_done()
    await tick(hass, freezer, timedelta(seconds=11))
    assert brightness(hass) == 102

    # The limit lifts at the end; the light stays as it is (min/max have no default).
    calls.clear()
    await tick(hass, freezer, timedelta(hours=1))
    assert hass.states.get(LIGHT).attributes["program"] == []
    assert hass.states.get(LIGHT).state == "on"
    assert not calls.of("light", "turn_off", LAMP)


async def test_light_on_and_colour_defaults(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "parti", {"target": LIGHT, "rules": {"on": True, "color": {"eq": "red", "set": True}}},
                    timedelta(minutes=1), timedelta(hours=1))
    await tick(hass, freezer, timedelta(minutes=1, seconds=2))
    state = hass.states.get(LIGHT)
    assert state.state == "on"
    assert state.attributes["rgb_color"] == (255, 0, 0)

    # Turning it off is clamped to on.
    await hass.services.async_call("light", "turn_off", {"entity_id": LIGHT}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "on"
    assert not calls.of("light", "turn_off", LAMP)

    # The end: `color` was set (and keeps the light on): back to the defaults (off).
    await tick(hass, freezer, timedelta(hours=1))
    assert hass.states.get(LIGHT).state == "off"


async def test_colour_only_recolours_a_lit_light(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "sercan_uyku", {"target": LIGHT, "rules": {"color": "red"}},
                    timedelta(seconds=-1), timedelta(hours=1))
    assert hass.states.get(LIGHT).state == "off"
    assert not calls.of("light", "turn_on", LAMP)
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT}, blocking=True)
    await hass.async_block_till_done()
    assert calls.of("light", "turn_on", LAMP)[-1]["rgb_color"] == (255, 0, 0)

    # The end: a let rule just lifts, the light stays red.
    calls.clear()
    await tick(hass, freezer, timedelta(hours=1, seconds=2))
    state = hass.states.get(LIGHT)
    assert state.state == "on"
    assert state.attributes["rgb_color"] == (255, 0, 0)
    assert not calls.of("light", "turn_on", LAMP)


async def test_set_brightness_keeps_the_light_on(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "okuma",
                    {"target": LIGHT, "light": {"brightness": {"min": 50, "set": True}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    # Switched on at the nearest allowed value.
    assert hass.states.get(LIGHT).state == "on"
    assert brightness(hass) == 128
    # Turning it off (an automation): kept on.
    await hass.services.async_call("light", "turn_off", {"entity_id": LIGHT}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "on"
    # The end: brightness has no default, the light's domain default applies (off).
    await tick(hass, freezer, timedelta(hours=1, seconds=2))
    assert hass.states.get(LIGHT).state == "off"


async def test_let_brightness_allows_off(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "okuma", {"target": LIGHT, "light": {"brightness": {"min": 50}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    assert hass.states.get(LIGHT).state == "off"
    assert not calls.of("light", "turn_on", LAMP)
    # Switched on at the wall at 10%: brought to the nearest allowed value.
    lamp(hass, True, 25)
    await hass.async_block_till_done()
    await tick(hass, freezer, timedelta(seconds=11))
    assert brightness(hass) == 128


async def test_user_request_is_refused_with_a_popup(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory, hass_admin_user
) -> None:
    from homeassistant.core import Context  # noqa: PLC0415

    await setup_all(hass)
    popups = []
    hass.services.async_register("browser_mod", "popup", lambda call: popups.append(call.data))
    hass.states.async_set("person.sercan", "home", {"user_id": hass_admin_user.id})
    await add_entry(hass, "okuma", {"target": LIGHT, "light": {"brightness": {"min": 50}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    await tick(hass, freezer, timedelta(seconds=11))
    before = brightness(hass)
    calls.clear()
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LIGHT, "brightness_pct": 49}, blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await hass.async_block_till_done()
    assert not calls.of("light", "turn_on", LAMP)
    assert brightness(hass) == before
    assert len(popups) == 1
    assert popups[0]["user_id"] == ["person.sercan"]
    assert popups[0]["right_button_action"]["data"] == {
        "entity_id": LIGHT, "brightness_pct": 50
    }
    # Allowed values pass; so does turning it off (off satisfies a let rule).
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": LIGHT, "brightness_pct": 70}, blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await hass.async_block_till_done()
    assert brightness(hass) == 178
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": LIGHT}, blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "off"
    assert len(popups) == 1


async def test_default_after_restart(hass: HomeAssistant, calls) -> None:
    """An eq attribute held before a restart, whose program is gone now."""
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        mock_restore_cache_with_extra_data,
    )
    from homeassistant.core import State  # noqa: PLC0415

    mock_restore_cache_with_extra_data(
        hass, [(State(LIGHT, "on"), {"program": {"set": ["on"]}})]
    )
    await setup_all(hass)
    assert calls.of("light", "turn_off", LAMP)
    assert hass.states.get(LIGHT).state == "off"


# --- climate ----------------------------------------------------------------------


async def test_climate_lock(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    await setup_all(hass)
    await add_entry(
        hass, "isitma", {"target": THERMOSTAT,
               "rules": {"hvac_mode": "heat", "temperature": {"eq": 21, "set": True}}},
        timedelta(minutes=1), timedelta(hours=1),
    )
    await tick(hass, freezer, timedelta(minutes=1, seconds=2))
    await tick(hass, freezer, timedelta(seconds=11))
    state = hass.states.get(THERMOSTAT)
    assert (state.state, state.attributes["temperature"]) == ("heat", 21)

    # From the UI: clamped.
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": THERMOSTAT, "temperature": 24}, blocking=True)
    await hass.async_block_till_done()
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 21

    # The dial on the thermostat: pushed back.
    await tick(hass, freezer, timedelta(seconds=20))
    trv(hass, "heat", 25)
    await hass.async_block_till_done()
    await tick(hass, freezer, timedelta(seconds=11))
    assert hass.states.get(THERMOSTAT).attributes["temperature"] == 21

    # The end: the set temperature has no default, the domain default applies (off).
    await tick(hass, freezer, timedelta(hours=1))
    assert hass.states.get(THERMOSTAT).state == "off"


# --- cover ------------------------------------------------------------------------


async def test_cover_rule_and_sun(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    await add_entry(hass, "karanlik", {"target": COVER, "rules": {"position": {"eq": 0, "set": True}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    assert hass.states.get(COVER).attributes["current_position"] == 0
    await hass.services.async_call("cover", "open_cover", {"entity_id": COVER}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).attributes["current_position"] == 0
    # A stop could leave the allowed range: ignored.
    await hass.services.async_call("cover", "stop_cover", {"entity_id": COVER}, blocking=True)
    await hass.async_block_till_done()
    assert not calls.of("cover", "stop_cover", BLIND)

    # The end: position `sun` -> open while the sun is up.
    await tick(hass, freezer, timedelta(hours=1, seconds=2))
    assert hass.states.get(COVER).attributes["current_position"] == 100

    # Sunset closes it, sunrise opens it; in between it is free.
    hass.states.async_set("sun.sun", "below_horizon")
    await hass.async_block_till_done()
    assert hass.states.get(COVER).attributes["current_position"] == 0
    await hass.services.async_call("cover", "set_cover_position",
                                   {"entity_id": COVER, "position": 30}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).attributes["current_position"] == 30
    hass.states.async_set("sun.sun", "above_horizon")
    await hass.async_block_till_done()
    assert hass.states.get(COVER).attributes["current_position"] == 100


async def test_custom_defaults(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    await setup_all(hass, {"defaults": {"light": {"on": True, "brightness": 10}}})
    await add_entry(hass, "kapali", {"target": LIGHT, "rules": {"on": {"eq": False, "set": True}}},
                    timedelta(seconds=-1), timedelta(hours=1))
    assert hass.states.get(LIGHT).state == "off"
    await tick(hass, freezer, timedelta(hours=1, seconds=2))
    state = hass.states.get(LIGHT)
    assert state.state == "on"


async def test_pause_and_resume(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    from custom_components.gungors.program import async_get_manager  # noqa: PLC0415

    await setup_all(hass)
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "parti", {"target": LIGHT, "light": {"on": {"eq": True, "set": True}}},
                    timedelta(minutes=1), timedelta(hours=1))
    uid = async_get_manager(hass).calendar.events[0].uid
    with pytest.raises(HomeAssistantError):  # not running yet
        await hass.services.async_call(
            "gungors", "pause_program", {"entity_id": CALENDAR, "uid": uid}, blocking=True
        )
    await tick(hass, freezer, timedelta(minutes=1, seconds=2))
    assert hass.states.get(LIGHT).state == "on"

    # Paused: as at an end, the set `on` returns to its default and the light is free.
    await hass.services.async_call(
        "gungors", "pause_program", {"entity_id": CALENDAR, "uid": uid}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "off"
    assert hass.states.get(LIGHT).attributes["program"] == []
    assert hass.states.get(CALENDAR).attributes["paused"] == [uid]

    # Resumed (right away): applied again as at a start.
    await hass.services.async_call(
        "gungors", "resume_program", {"entity_id": CALENDAR, "uid": uid}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == "on"
    assert hass.states.get(CALENDAR).attributes["paused"] == []

    # A pause ends with its occurrence.
    await hass.services.async_call(
        "gungors", "pause_program", {"entity_id": CALENDAR, "uid": uid}, blocking=True
    )
    await tick(hass, freezer, timedelta(hours=1))
    assert async_get_manager(hass).paused == set()


async def test_old_entries_become_set_rules(hass: HomeAssistant, calls, hass_storage) -> None:
    from ical.calendar import Calendar  # noqa: PLC0415
    from ical.calendar_stream import IcsCalendarStream  # noqa: PLC0415
    from ical.event import Event  # noqa: PLC0415

    from custom_components.gungors.program import async_get_manager  # noqa: PLC0415

    calendar = Calendar()
    now = dt_util.now()
    calendar.events.append(Event(
        summary="eski", start=now + timedelta(hours=1), end=now + timedelta(hours=2),
        description="target: x\nlight: {on: true, brightness: {max: 40}}",
    ))
    hass_storage["gungors.program"] = {
        "version": 1, "key": "gungors.program",
        "data": {"ics": IcsCalendarStream.calendar_to_ics(calendar), "paused": []},
    }
    await setup_all(hass)
    event = async_get_manager(hass).calendar.events[0]
    assert parse_program(event.description).rules == {
        "on": Constraint(eq=True, set=True),
        "brightness": Constraint(max=40, set=True),
    }
    assert hass_storage["gungors.program"]["data"]["format"] == 2
