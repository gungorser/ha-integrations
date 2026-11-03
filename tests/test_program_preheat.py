"""Preheat: a thermostat reaches a coming program's set temperature by its start."""
from __future__ import annotations

from datetime import timedelta
import json

from freezegun.api import FrozenDateTimeFactory

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.gungors.program import async_get_manager
from custom_components.gungors.rules import dump_program, parse_program

from .conftest import setup_platform

TRV = "climate.salon_climate"
THERMOSTAT = "climate.salon_thermostat"
SENSOR = "sensor.salon_temperature"
CALENDAR = "calendar.program"
RULE = {"temperature": {"min": 21, "set": True}}


def trv(hass: HomeAssistant, mode: str = "off", temperature: float = 18) -> None:
    hass.states.async_set(
        TRV, mode, {"hvac_modes": ["heat", "off"], "temperature": temperature, "min_temp": 5}
    )


def room(hass: HomeAssistant, value: float) -> None:
    hass.states.async_set(SENSOR, str(value), {"unit_of_measurement": "°C"})


def echo(hass: HomeAssistant) -> None:
    """The thermostat reports what it is told."""

    @callback
    def handle(event) -> None:
        data = event.data["service_data"]
        ids = data.get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if TRV in ids and event.data["domain"] == "climate":
            state = hass.states.get(TRV)
            attrs = dict(state.attributes)
            if "temperature" in data:
                attrs["temperature"] = data["temperature"]
            hass.states.async_set(TRV, data.get("hvac_mode", state.state), attrs)

    hass.bus.async_listen(EVENT_CALL_SERVICE, handle)


async def setup(hass: HomeAssistant, temperature: float = 19, rate: float = 1.0) -> None:
    trv(hass)
    room(hass, temperature)
    echo(hass)
    await setup_platform(hass, "climate", {
        "name": "salon_thermostat",
        "features": [{"program": {"preheat": {"rate": rate}}}, {"temperature": SENSOR},
                     {"link": TRV}],
    })
    assert await async_setup_component(hass, "calendar", {"calendar": [{"platform": "gungors"}]})
    await hass.async_block_till_done()


async def add_entry(hass: HomeAssistant, summary: str, program: dict, start: timedelta,
                    end: timedelta) -> None:
    now = dt_util.now()
    await hass.services.async_call("calendar", "create_event", {
        "entity_id": CALENDAR, "summary": summary,
        "start_date_time": (now + start).isoformat(), "end_date_time": (now + end).isoformat(),
        "description": json.dumps(program),
    }, blocking=True)
    await hass.async_block_till_done()


async def tick(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def walk(hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta) -> None:
    """Let time pass a minute at a time (the window is checked every minute)."""
    steps = int(delta.total_seconds() // 60)
    for _ in range(steps):
        await tick(hass, freezer, timedelta(minutes=1))


def thermostat(hass: HomeAssistant) -> tuple[str, float | None]:
    state = hass.states.get(THERMOSTAT)
    return state.state, state.attributes.get("temperature")


async def programs(hass: HomeAssistant) -> dict[str, dict]:
    """The program card's list (what websocket gungors/programs sends)."""
    return {p["summary"]: p for p in async_get_manager(hass).programs()}


def test_preheat_flag_round_trip() -> None:
    program = parse_program("target: Salon\nclimate:\n  temperature: 21\n  preheat: false\n")
    assert program.preheat is False
    assert "  preheat: false\n" in dump_program(program)
    assert parse_program(dump_program(program)) == program
    assert parse_program("target: Salon\nclimate:\n  temperature: 21\n").preheat is True
    assert "preheat" not in dump_program(parse_program("target: Salon\nclimate:\n  temperature: 21\n"))


async def test_window_opens_by_the_rate_and_stays_until_the_start(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=19)
    await add_entry(hass, "akşam", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(hours=2, minutes=30), timedelta(hours=4))
    # 2° at 1°/h: the window opens 2 h before the start, not yet.
    plan = hass.states.get(THERMOSTAT).attributes["preheat"]
    assert plan["active"] is False and plan["target"] == 21
    assert dt_util.parse_datetime(plan["start"]) == dt_util.parse_datetime(plan["at"]) - timedelta(hours=2)
    assert (await programs(hass))["akşam"]["preheat"]["active"] is False
    assert thermostat(hass)[0] == "off"

    await walk(hass, freezer, timedelta(minutes=25))
    assert thermostat(hass)[0] == "off"
    await walk(hass, freezer, timedelta(minutes=6))
    assert thermostat(hass) == ("heat", 21)
    state = hass.states.get(THERMOSTAT)
    assert state.attributes["preheat"]["active"] is True
    assert state.attributes["program"][0]["preheat"] is True
    assert hass.states.get(CALENDAR).attributes["preheating"]
    listed = (await programs(hass))["akşam"]
    assert listed["state"] == "preheating" and listed["preheat"]["active"] is True

    # The room gets there early: the window stays open (latched), the outcome is noted.
    room(hass, 21)
    await hass.async_block_till_done()
    await walk(hass, freezer, timedelta(minutes=5))
    assert thermostat(hass) == ("heat", 21)
    last = hass.states.get(THERMOSTAT).attributes["preheat_last"]
    assert last["target"] == 21 and last["late_min"] < 0

    # The start: the program takes over without the heating going off in between.
    calls.clear()
    await walk(hass, freezer, timedelta(hours=2))
    assert thermostat(hass) == ("heat", 21)
    assert not [c for c in calls.of("climate", "set_hvac_mode", TRV) if c["hvac_mode"] == "off"]
    state = hass.states.get(THERMOSTAT)
    assert "preheat" not in state.attributes["program"][0]
    assert not hass.states.get(CALENDAR).attributes["preheating"]

    # The end: back to the default (off).
    await tick(hass, freezer, timedelta(hours=2))
    assert thermostat(hass)[0] == "off"


async def test_the_window_is_part_of_the_program(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=20)
    await add_entry(hass, "akşam", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(minutes=30), timedelta(hours=2))
    await walk(hass, freezer, timedelta(minutes=2))
    assert thermostat(hass) == ("heat", 21)
    # A user lowering it or turning it off: brought back to the rule.
    await hass.services.async_call("climate", "set_temperature",
                                   {"entity_id": THERMOSTAT, "temperature": 18}, blocking=True)
    await hass.async_block_till_done()
    assert calls.of("climate", "set_temperature", TRV)[-1]["temperature"] == 21
    await hass.services.async_call("climate", "set_hvac_mode",
                                   {"entity_id": THERMOSTAT, "hvac_mode": "off"}, blocking=True)
    await hass.async_block_till_done()
    assert thermostat(hass)[0] == "heat"


async def test_window_goes_before_a_running_program(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=17)
    await add_entry(hass, "gündüz", {"target": THERMOSTAT,
                                      "climate": {"temperature": {"max": 18, "set": True}}},
                    timedelta(minutes=1), timedelta(hours=1, minutes=2))
    await walk(hass, freezer, timedelta(minutes=2))
    assert thermostat(hass) == ("heat", 18)
    await add_entry(hass, "akşam", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(hours=1), timedelta(hours=5))
    # 4° at 1°/h: more than the hour left, the window opens at once and wins over max 18.
    await walk(hass, freezer, timedelta(minutes=2))
    assert thermostat(hass) == ("heat", 21)
    assert hass.states.get(THERMOSTAT).attributes["preheat"]["active"] is True


async def test_preheat_off_and_a_warm_room(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=19)
    await add_entry(hass, "kapalı", {"target": THERMOSTAT,
                                      "climate": {**RULE, "preheat": False}},
                    timedelta(minutes=30), timedelta(hours=2))
    await walk(hass, freezer, timedelta(minutes=5))
    assert thermostat(hass)[0] == "off"
    assert hass.states.get(THERMOSTAT).attributes["preheat"] is None
    await walk(hass, freezer, timedelta(minutes=26))
    assert thermostat(hass) == ("heat", 21)  # at the start, as before

    await tick(hass, freezer, timedelta(hours=2))
    room(hass, 21.5)
    await hass.async_block_till_done()
    await add_entry(hass, "sıcak", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(minutes=30), timedelta(hours=1))
    await walk(hass, freezer, timedelta(minutes=5))
    assert thermostat(hass)[0] == "off"
    plan = hass.states.get(THERMOSTAT).attributes["preheat"]
    assert plan["start"] is None and plan["active"] is False


async def test_pausing_ends_the_window_until_the_program_ends(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=20)
    await add_entry(hass, "akşam", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(minutes=30), timedelta(hours=2))
    await walk(hass, freezer, timedelta(minutes=2))
    assert thermostat(hass) == ("heat", 21)
    uid = (await programs(hass))["akşam"]["uid"]
    await hass.services.async_call("gungors", "pause_program",
                                   {"entity_id": CALENDAR, "uid": uid}, blocking=True)
    await hass.async_block_till_done()
    assert thermostat(hass)[0] == "off"
    assert uid in hass.states.get(CALENDAR).attributes["paused"]
    assert (await programs(hass))["akşam"]["state"] == "paused"
    # Still paused at the start.
    await walk(hass, freezer, timedelta(minutes=30))
    assert thermostat(hass)[0] == "off"
    await hass.services.async_call("gungors", "resume_program",
                                   {"entity_id": CALENDAR, "uid": uid}, blocking=True)
    await hass.async_block_till_done()
    assert thermostat(hass) == ("heat", 21)


async def test_deleting_the_program_ends_the_window(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup(hass, temperature=20)
    await add_entry(hass, "akşam", {"target": THERMOSTAT, "climate": RULE},
                    timedelta(minutes=30), timedelta(hours=2))
    await walk(hass, freezer, timedelta(minutes=2))
    assert thermostat(hass) == ("heat", 21)
    uid = (await programs(hass))["akşam"]["uid"]
    entity = hass.data["calendar"].get_entity(CALENDAR)
    await entity.async_delete_event(uid)
    await hass.async_block_till_done()
    assert thermostat(hass)[0] == "off"
    assert hass.states.get(THERMOSTAT).attributes["preheat"] is None
