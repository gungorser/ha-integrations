"""Programs whose start or end is relative to the sun (`start: sunset-30m`)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json

from freezegun.api import FrozenDateTimeFactory
import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

from custom_components.gungors.rules import SunTime, dump_program, parse_program

from .test_program import CALENDAR, LAMP, LIGHT, setup_all, tick

TODAY = date(2026, 10, 17)


def sun(hass: HomeAssistant, event: str, day: date) -> datetime:
    location, elevation = get_astral_location(hass)
    return dt_util.as_local(getattr(location, event)(day, local=True, observer_elevation=elevation))


def at(hass: HomeAssistant, day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=dt_util.get_default_time_zone())


@pytest.fixture
async def amsterdam(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    hass.config.latitude, hass.config.longitude, hass.config.elevation = 52.37, 4.89, 0
    freezer.move_to(at(hass, TODAY, 12))


async def create(hass: HomeAssistant, hass_ws_client, summary: str, program: dict,
                 start: datetime, end: datetime, rrule: str | None = None) -> None:
    client = await hass_ws_client(hass)
    event = {"summary": summary, "dtstart": start.isoformat(), "dtend": end.isoformat(),
             "description": json.dumps(program)}
    if rrule:
        event["rrule"] = rrule
    await client.send_json_auto_id(
        {"type": "calendar/event/create", "entity_id": CALENDAR, "event": event}
    )
    result = await client.receive_json()
    if not result["success"]:
        raise HomeAssistantError(result["error"]["message"])
    await hass.async_block_till_done()


async def events(hass: HomeAssistant, start: datetime, end: datetime) -> list[dict]:
    response = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": CALENDAR, "start_date_time": start.isoformat(),
         "end_date_time": end.isoformat()},
        blocking=True, return_response=True,
    )
    return [
        {**e, "start": dt_util.parse_datetime(e["start"]), "end": dt_util.parse_datetime(e["end"])}
        for e in response[CALENDAR]["events"]
    ]


def test_parse_sun_times() -> None:
    program = parse_program(
        "target: light.a\nstart: sunset-30m\nend: Sunrise + 1h30m\nlight: {state: 'on'}"
    )
    assert program.start == SunTime("sunset", timedelta(minutes=-30))
    assert program.end == SunTime("sunrise", timedelta(hours=1, minutes=30))
    text = dump_program(program)
    assert "start: sunset-30m\nend: sunrise+1h30m\n" in text
    assert parse_program(text) == program
    assert parse_program("target: light.a\nstart: sunrise+2h\nlight: {state: 'on'}").end is None
    for bad in ("noon", "sunset-", "sunset+30", 5):
        with pytest.raises(vol.Invalid, match="sunrise or sunset"):
            parse_program(f"target: light.a\nstart: {bad}\nlight: {{state: 'on'}}")


async def test_daily_start_follows_the_sun(
    hass: HomeAssistant, amsterdam, calls, freezer: FrozenDateTimeFactory, hass_ws_client
) -> None:
    await setup_all(hass)
    program = {"target": LIGHT, "start": "sunset-30m",
               "light": {"brightness_pct": {"max": 40}}}
    await create(hass, hass_ws_client, "loş", program,
                 at(hass, TODAY, 18), at(hass, TODAY, 23), "FREQ=DAILY")

    found = await events(hass, at(hass, TODAY, 0), at(hass, TODAY + timedelta(days=3), 0))
    assert [e["start"] for e in found] == [
        sun(hass, "sunset", TODAY + timedelta(days=d)) - timedelta(minutes=30) for d in range(3)
    ]
    assert [e["end"] for e in found] == [at(hass, TODAY + timedelta(days=d), 23) for d in range(3)]
    assert found[0]["start"] != found[2]["start"] - timedelta(days=2)  # a new time every day

    begin = sun(hass, "sunset", TODAY) - timedelta(minutes=30)
    await tick(hass, freezer, begin - dt_util.now() - timedelta(minutes=1))
    assert hass.states.get(CALENDAR).state == "off"
    assert not calls.of("light", "turn_on", LAMP)
    await tick(hass, freezer, timedelta(minutes=2))
    assert hass.states.get(CALENDAR).state == "on"
    assert calls.of("light", "turn_on", LAMP)[-1]["brightness"] == 102


async def test_end_at_sunrise_runs_through_the_night(
    hass: HomeAssistant, amsterdam, calls, hass_ws_client
) -> None:
    await setup_all(hass)
    # Stored as 22:00-23:00 today; it really ends at tomorrow's sunrise.
    await create(hass, hass_ws_client, "melike_uyku",
                 {"target": LIGHT, "end": "sunrise", "light": {"state": "off"}},
                 at(hass, TODAY, 22), at(hass, TODAY, 23), "FREQ=DAILY")
    found = await events(hass, at(hass, TODAY, 0), at(hass, TODAY + timedelta(days=1), 23))
    assert found[0]["start"] == at(hass, TODAY, 22)
    assert found[0]["end"] == sun(hass, "sunrise", TODAY + timedelta(days=1))
    assert found[1]["start"] == at(hass, TODAY + timedelta(days=1), 22)


async def test_sun_start_with_fixed_end_and_conflicts(
    hass: HomeAssistant, amsterdam, calls, hass_ws_client
) -> None:
    await setup_all(hass)
    tomorrow = TODAY + timedelta(days=1)
    # Sunrise is about 08:10 in October: sunrise+2h -> 14:00 is about 10:10-14:00.
    assert at(hass, tomorrow, 10) < sun(hass, "sunrise", tomorrow) + timedelta(hours=2)
    await create(hass, hass_ws_client, "gündüz",
                 {"target": LIGHT, "start": "sunrise+2h", "light": {"state": "on"}},
                 at(hass, tomorrow, 10), at(hass, tomorrow, 14))
    found = await events(hass, at(hass, tomorrow, 0), at(hass, tomorrow, 23))
    assert found[0]["start"] == sun(hass, "sunrise", tomorrow) + timedelta(hours=2)
    assert found[0]["end"] == at(hass, tomorrow, 14)

    # Before the real start: no conflict, although the stored 10:00 would overlap.
    await create(hass, hass_ws_client, "sabah", {"target": LIGHT, "light": {"state": "off"}},
                 at(hass, tomorrow, 9), at(hass, tomorrow, 10, 5))
    with pytest.raises(HomeAssistantError, match="conflicts with 'gündüz'"):
        await create(hass, hass_ws_client, "öğle", {"target": LIGHT, "light": {"state": "off"}},
                     at(hass, tomorrow, 12), at(hass, tomorrow, 13))


async def test_sun_start_after_the_end_skips_the_day(
    hass: HomeAssistant, amsterdam, calls, hass_ws_client
) -> None:
    await setup_all(hass)
    await create(hass, hass_ws_client, "akşam",
                 {"target": LIGHT, "start": "sunset", "light": {"state": "on"}},
                 at(hass, TODAY, 13), at(hass, TODAY, 14))
    assert await events(hass, at(hass, TODAY, 0), at(hass, TODAY, 23, 59)) == []
