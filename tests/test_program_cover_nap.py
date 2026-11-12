"""A timeless nap program (eq 0, set) closes the blind, also under a running max-15 set rule."""
from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from custom_components.gungors.program import async_get_manager

from .test_program import COVER, add_entry, setup_all, tick
from .test_program_timeless import call

NIGHT = {"target": COVER, "cover": {"current_position": {"max": 15, "set": True}}}
NAP = {"target": COVER, "cover": {"current_position": {"eq": 0, "set": True}}}


async def start_nap(hass: HomeAssistant) -> None:
    import json
    await call(hass, "save_timeless_program", summary="kaan gunduz uyku",
               description=json.dumps(NAP))
    await call(hass, "start_program", uid="Kaan gündüz uyku", duration="03:00:00")


def position(hass: HomeAssistant) -> int:
    return hass.states.get(COVER).attributes["current_position"]


async def test_nap_closes_an_open_blind(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    assert position(hass) == 100
    await start_nap(hass)
    assert position(hass) == 0


async def test_nap_closes_blind_held_by_night_rule(
    hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory
) -> None:
    await setup_all(hass)
    await add_entry(hass, "Kaan uyku", NIGHT, timedelta(seconds=-1), timedelta(hours=10))
    assert position(hass) == 15
    await start_nap(hass)
    assert position(hass) == 0
    await tick(hass, freezer, timedelta(seconds=30))
    assert position(hass) == 0
