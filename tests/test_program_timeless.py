"""Timeless programs (started and stopped on demand) and pins."""
from __future__ import annotations

from datetime import timedelta
import json

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.gungors.program import async_get_manager

from .test_program import CALENDAR, LIGHT, add_entry, brightness, lamp, setup_all, tick

SLEEP = {"target": LIGHT, "light": {"brightness_pct": {"max": 10, "set": True}}}
EVENING = {"target": LIGHT, "light": {"brightness_pct": {"min": 50, "set": True}}}


async def call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "gungors", service, {"entity_id": CALENDAR, **data}, blocking=True
    )
    await hass.async_block_till_done()


async def save_sleep(hass: HomeAssistant, program: dict = SLEEP) -> str:
    await call(hass, "save_timeless_program", summary="Bebek uyku",
               description=json.dumps(program))
    return async_get_manager(hass).timeless_uid("bebek uyku")


def listed(hass: HomeAssistant) -> dict[str, dict]:
    return {p["summary"]: p for p in async_get_manager(hass).programs()}


async def test_timeless_is_validated(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    with pytest.raises(HomeAssistantError, match="not a gungors entity"):
        await call(hass, "save_timeless_program", summary="x",
                   description=json.dumps({"target": "light.other", "light": {"state": "on"}}))
    with pytest.raises(HomeAssistantError, match="no start or end"):
        await call(hass, "save_timeless_program", summary="x",
                   description=json.dumps({**SLEEP, "start": "sunset"}))
    await save_sleep(hass)
    with pytest.raises(HomeAssistantError, match="exists already"):
        await save_sleep(hass)
    with pytest.raises(HomeAssistantError, match="No timeless program"):
        await call(hass, "start_program", uid="nothing")


async def test_start_stop_and_duration(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, True, 255)
    await hass.async_block_till_done()
    uid = await save_sleep(hass)
    assert listed(hass)["Bebek uyku"]["state"] == "idle"

    await call(hass, "start_program", uid="Bebek uyku")  # by name
    assert brightness(hass) == 26  # 10 %
    item = listed(hass)["Bebek uyku"]
    assert item["state"] == "running" and item["timeless"] and item["uid"] == uid
    # The run is a calendar entry, but it is listed under its program only.
    assert len(async_get_manager(hass).calendar.events) == 1
    assert len(listed(hass)) == 1
    end = dt_util.parse_datetime(item["end"])
    assert end - dt_util.parse_datetime(item["start"]) == timedelta(hours=24)

    # Starting again keeps the run.
    await call(hass, "start_program", uid=uid)
    assert len(async_get_manager(hass).calendar.events) == 1

    await call(hass, "stop_program", uid=uid)
    assert listed(hass)["Bebek uyku"]["state"] == "idle"
    assert hass.states.get(LIGHT).attributes["program"] == []
    # A stopped run leaves the calendar.
    assert async_get_manager(hass).calendar.events == []

    # With a duration it ends by itself.
    await call(hass, "start_program", uid=uid, duration={"minutes": 30})
    assert listed(hass)["Bebek uyku"]["state"] == "running"
    await tick(hass, freezer, timedelta(minutes=30, seconds=2))
    assert listed(hass)["Bebek uyku"]["state"] == "idle"
    assert async_get_manager(hass).calendar.events == []  # so does an ended one


async def test_run_pauses_the_conflicting_program(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    await add_entry(hass, "Akşam", EVENING, timedelta(seconds=-1), timedelta(hours=3))
    await add_entry(hass, "Renk", {"target": LIGHT, "light": {"rgb_color": "red"}},
                    timedelta(seconds=-1), timedelta(hours=3))
    evening = next(e.uid for e in async_get_manager(hass).calendar.events
                   if e.summary == "Akşam")
    assert brightness(hass) >= 127
    uid = await save_sleep(hass)

    # Saving no conflict check for a run: it pauses Akşam instead; Renk doesn't conflict.
    await call(hass, "start_program", uid=uid)
    items = listed(hass)
    assert items["Akşam"]["state"] == "paused"
    assert items["Akşam"]["paused_by"] == uid
    assert items["Renk"]["state"] == "running"
    assert brightness(hass) == 26
    assert hass.states.get(CALENDAR).attributes["paused"] == [evening]

    # Stopped: Akşam comes back.
    await call(hass, "stop_program", uid=uid)
    await tick(hass, freezer, timedelta(seconds=2))
    items = listed(hass)
    assert items["Akşam"]["state"] == "running" and items["Akşam"]["paused_by"] is None
    assert brightness(hass) >= 127

    # A pause by the user is kept when the run ends.
    await call(hass, "pause_program", uid=evening)
    await call(hass, "start_program", uid=uid, duration={"minutes": 10})
    await tick(hass, freezer, timedelta(minutes=10, seconds=2))
    assert listed(hass)["Akşam"]["state"] == "paused"
    await call(hass, "resume_program", uid=evening)

    # Resumed by the user while the run is on: not paused again; it started first, so
    # it keeps the light and the run is skipped there.
    await call(hass, "start_program", uid=uid)
    assert listed(hass)["Akşam"]["state"] == "paused"
    await call(hass, "resume_program", uid=evening)
    assert listed(hass)["Akşam"]["state"] == "running"
    assert brightness(hass) >= 127


async def test_program_starting_during_a_run_is_paused(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    uid = await save_sleep(hass)
    await call(hass, "start_program", uid=uid, duration={"hours": 2})
    await add_entry(hass, "Akşam", EVENING, timedelta(minutes=10), timedelta(hours=3))
    await tick(hass, freezer, timedelta(minutes=10, seconds=2))
    assert listed(hass)["Akşam"]["state"] == "paused"
    assert brightness(hass) == 26
    await tick(hass, freezer, timedelta(hours=2))
    assert listed(hass)["Akşam"]["state"] == "running"
    assert brightness(hass) >= 127


async def test_two_runs_newest_wins(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    sleep = await save_sleep(hass)
    await call(hass, "save_timeless_program", summary="Oyun", description=json.dumps(EVENING))
    play = async_get_manager(hass).timeless_uid("Oyun")
    await call(hass, "start_program", uid=sleep)
    await tick(hass, freezer, timedelta(minutes=1))
    await call(hass, "start_program", uid=play)
    items = listed(hass)
    assert items["Bebek uyku"]["state"] == "paused" and items["Bebek uyku"]["paused_by"] == play
    assert brightness(hass) >= 127
    await call(hass, "stop_program", uid=play)
    await tick(hass, freezer, timedelta(seconds=2))
    assert listed(hass)["Bebek uyku"]["state"] == "running"
    assert brightness(hass) == 26


async def test_edit_follows_running_and_delete_stops(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    lamp(hass, True, 255)
    await hass.async_block_till_done()
    uid = await save_sleep(hass)
    await call(hass, "start_program", uid=uid)
    await call(hass, "save_timeless_program", uid=uid, summary="Bebek uyku",
               description=json.dumps({"target": LIGHT,
                                       "light": {"brightness_pct": {"min": 30, "set": True}}}))
    assert brightness(hass) in (76, 77)  # 30 %
    await call(hass, "delete_timeless_program", uid=uid)
    assert listed(hass) == {}
    assert async_get_manager(hass).calendar.events == []
    assert hass.states.get(LIGHT).attributes["program"] == []


async def test_pins_and_list(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory, hass_ws_client
) -> None:
    await setup_all(hass)
    client = await hass_ws_client(hass)
    now = dt_util.now()
    await client.send_json_auto_id({
        "type": "calendar/event/create",
        "entity_id": CALENDAR,
        "event": {
            "summary": "Sabah",
            "dtstart": (now + timedelta(hours=2)).isoformat(),
            "dtend": (now + timedelta(hours=3)).isoformat(),
            "rrule": "FREQ=DAILY",
            "description": json.dumps({"target": LIGHT, "light": {"state": "on"}}),
        },
    })
    assert (await client.receive_json())["success"]
    await add_entry(hass, "Eski", {"target": LIGHT, "light": {"state": "on"}},
                    timedelta(hours=4), timedelta(hours=5))
    uid = await save_sleep(hass)
    manager = async_get_manager(hass)
    morning = next(e.uid for e in manager.calendar.events if e.summary == "Sabah")
    await call(hass, "pin_program", uid=morning)
    await call(hass, "pin_program", uid=uid)
    await call(hass, "pin_program", uid=uid, pinned=False)
    with pytest.raises(HomeAssistantError):
        await call(hass, "pin_program", uid="nope")

    await tick(hass, freezer, timedelta(hours=6))
    await client.send_json_auto_id({"type": "gungors/programs"})
    result = (await client.receive_json())["result"]["programs"]
    items = {p["summary"]: p for p in result}
    assert items["Sabah"]["pinned"] and items["Sabah"]["state"] == "scheduled"
    assert items["Sabah"]["rrule"] == "FREQ=DAILY" and items["Sabah"]["recurrence_id"]
    assert items["Eski"]["state"] == "done" and items["Eski"]["start"]
    assert not items["Eski"]["all_day"]
    attributes = hass.states.get(CALENDAR).attributes
    assert attributes["pinned"] == [morning] and attributes["timeless"] == [uid]
    assert not items["Bebek uyku"]["pinned"] and items["Bebek uyku"]["state"] == "idle"

    # A deleted entry loses its pin.
    await client.send_json_auto_id(
        {"type": "calendar/event/delete", "entity_id": CALENDAR, "uid": morning}
    )
    assert (await client.receive_json())["success"]
    assert manager.pinned == set()


async def test_one_entry_per_timeless_program(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    uid = await save_sleep(hass)
    manager = async_get_manager(hass)
    # A leftover entry of an earlier run (e.g. from before 1.10.1) goes at the next start.
    await call(hass, "start_program", uid=uid, duration={"minutes": 5})
    old = manager.calendar.events[0]
    await call(hass, "stop_program", uid=uid)
    await tick(hass, freezer, timedelta(minutes=10))
    manager.calendar.events.append(old)  # ended, not dropped yet
    await call(hass, "start_program", uid=uid, duration={"minutes": 5})
    assert len(manager.calendar.events) == 1
    assert manager.calendar.events[0].uid != old.uid
