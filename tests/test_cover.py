"""Cover features: link, direction, window, buttons, invert, hold."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from .conftest import setup_platform

RAW = "cover.sercan_blind"
WINDOW = "binary_sensor.sercan_window"
WRAPPER = "cover.sercan_cover"


def raw(hass: HomeAssistant, position: int, state: str | None = None) -> None:
    hass.states.async_set(
        RAW,
        state or ("closed" if position == 0 else "open"),
        {"current_position": position, "supported_features": 15},
    )


async def report(hass: HomeAssistant, position: int) -> None:
    raw(hass, position)
    await hass.async_block_till_done()


async def cover(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        "cover", service, {"entity_id": WRAPPER, **data}, blocking=True
    )
    await hass.async_block_till_done()


def pos(hass: HomeAssistant, entity_id: str = WRAPPER) -> int | None:
    return hass.states.get(entity_id).attributes.get("current_position")


async def setup_guarded(hass: HomeAssistant, *extra_features) -> None:
    await setup_platform(
        hass,
        "cover",
        {
            "name": "sercan_cover",
            "device_class": "blind",
            "features": [*extra_features, {"window": WINDOW}, "direction", {"link": RAW}],
        },
    )


# --- link with a number (template covers) ----------------------------------


async def test_number_link(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.mutfak_position", "40")
    await setup_platform(
        hass, "cover", {"name": "mutfak_cover", "features": [{"link": "input_number.mutfak_position"}]}
    )
    assert pos(hass, "cover.mutfak_cover") == 40

    await hass.services.async_call(
        "cover", "set_cover_position", {"entity_id": "cover.mutfak_cover", "position": 70},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value") == [
        {"entity_id": ["input_number.mutfak_position"], "value": 70}
    ]
    # Only link: the wrapper is the number; it changes when the number does.
    assert pos(hass, "cover.mutfak_cover") == 40
    hass.states.async_set("input_number.mutfak_position", "70")
    await hass.async_block_till_done()
    assert pos(hass, "cover.mutfak_cover") == 70
    assert len(calls.of("input_number", "set_value")) == 1

    # The number changed by something else: the wrapper follows.
    hass.states.async_set("input_number.mutfak_position", "10")
    await hass.async_block_till_done()
    assert pos(hass, "cover.mutfak_cover") == 10


async def test_unavailable_follows_original(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "40")
    await setup_platform(hass, "cover", {"name": "x_cover", "features": [{"link": "input_number.x"}]})
    hass.states.async_set("input_number.x", "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get("cover.x_cover").state == "unavailable"
    hass.states.async_set("input_number.x", "55")
    await hass.async_block_till_done()
    assert pos(hass, "cover.x_cover") == 55


# --- direction ---------------------------------------------------------------


async def test_direction_commanded_move(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    assert pos(hass) == 100

    await cover(hass, "set_cover_position", position=0)
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0
    assert hass.states.get(WRAPPER).state == "closing"

    # Z2M echo of the target: ignored.
    await report(hass, 0)
    assert hass.states.get(WRAPPER).state == "closing"
    assert pos(hass) == 100

    for p in (97, 94, 91):
        await report(hass, p)
    assert pos(hass) == 91  # real position while moving
    assert hass.states.get(WRAPPER).state == "closing"

    await report(hass, 3)
    await report(hass, 0)
    assert hass.states.get(WRAPPER).state == "closed"
    assert pos(hass) == 0


async def test_direction_physical_move_and_stall(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 50)
    await setup_guarded(hass)
    await report(hass, 53)
    assert hass.states.get(WRAPPER).state == "opening"
    await report(hass, 56)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "open"
    assert pos(hass) == 56
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_direction_retry_once(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "set_cover_position", position=40)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=9))
    await hass.async_block_till_done()
    assert [c["position"] for c in calls.of("cover", "set_cover_position", RAW)] == [40, 40]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()
    assert len(calls.of("cover", "set_cover_position", RAW)) == 2
    assert hass.states.get(WRAPPER).state == "open"


# --- window ------------------------------------------------------------------


async def test_window_holds_and_replays(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)

    await cover(hass, "close_cover")
    assert not calls.of("cover", "set_cover_position", RAW)
    state = hass.states.get(WRAPPER)
    assert state.attributes["pending_position"] == 0
    assert state.attributes["current_position"] == 0

    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0
    assert hass.states.get(WRAPPER).attributes["pending_position"] is None


async def test_window_unknown_counts_as_open(hass: HomeAssistant, calls) -> None:
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_window_opened_mid_move_stops(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    await report(hass, 95)
    hass.states.async_set(WINDOW, "on")
    await hass.async_block_till_done()
    assert calls.of("cover", "stop_cover", RAW)
    assert hass.states.get(WRAPPER).attributes["pending_position"] == 0
    await report(hass, 93)
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0


async def test_physical_move_drops_pending(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    await report(hass, 97)
    assert hass.states.get(WRAPPER).attributes["pending_position"] is None
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_legacy_restore_pending(hass: HomeAssistant, calls) -> None:
    """A held command saved by the old window_guard survives the upgrade."""
    mock_restore_cache_with_extra_data(
        hass,
        [(
            State(WRAPPER, "open", {"current_position": 30, "is_sync": False}),
            {"setpoint": 30, "pending": True},
        )],
    )
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    state = hass.states.get(WRAPPER)
    assert state.attributes["pending_position"] == 30
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 30


async def test_restart_in_sync_takes_real_position(hass: HomeAssistant, calls) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(State(WRAPPER, "open", {"current_position": 30, "is_sync": True}), {"setpoint": 30})],
    )
    hass.states.async_set(WINDOW, "off")
    raw(hass, 80)
    await setup_guarded(hass)
    assert pos(hass) == 80
    assert not calls.of("cover", "set_cover_position", RAW)


# --- buttons -----------------------------------------------------------------


async def test_buttons_ignore_window(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass, "buttons")
    assert hass.states.get(WRAPPER).attributes["physical_buttons"] is True
    hass.bus.async_fire("gungors_physical_cover", {"entity_id": [WRAPPER], "action": "close"})
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0


# --- invert ------------------------------------------------------------------


async def test_invert(hass: HomeAssistant, calls) -> None:
    raw(hass, 30)
    await setup_platform(
        hass, "cover", {"name": "sercan_cover", "features": ["invert", {"link": RAW}]}
    )
    assert pos(hass) == 70
    assert hass.states.get(WRAPPER).attributes["actual_position"] == 70
    await cover(hass, "set_cover_position", position=20)
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 80


# --- group -------------------------------------------------------------------


async def test_group(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.a", "20")
    hass.states.async_set("input_number.b", "40")
    await setup_platform(
        hass,
        "cover",
        {"name": "a_cover", "features": [{"link": "input_number.a"}]},
        {"name": "b_cover", "features": [{"link": "input_number.b"}]},
        {"name": "ab_cover", "features": [{"link": ["cover.a_cover", "cover.b_cover"]}]},
    )
    state = hass.states.get("cover.ab_cover")
    assert state.attributes["current_position"] == 30
    assert state.attributes["entity_id"] == ["cover.a_cover", "cover.b_cover"]
    await hass.services.async_call(
        "cover", "close_cover", {"entity_id": "cover.ab_cover"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value", "input_number.a")[-1]["value"] == 0
    assert calls.of("input_number", "set_value", "input_number.b")[-1]["value"] == 0


async def test_group_follows_its_members(hass: HomeAssistant, calls) -> None:
    """The group's position is its members' (link only): our move is taken as it
    goes, a move against it is physical (here pushed back by a strict hold)."""
    hass.states.async_set("input_number.a", "100")
    hass.states.async_set("input_number.b", "100")
    await setup_platform(
        hass,
        "cover",
        {"name": "a_cover", "features": [{"link": "input_number.a"}]},
        {"name": "b_cover", "features": [{"link": "input_number.b"}]},
        {"name": "ab_cover", "features": ["hold", {"link": ["cover.a_cover", "cover.b_cover"]}]},
    )
    await hass.services.async_call(
        "gungors", "hold", {"entity_id": "cover.ab_cover", "state": {"position": 0}},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert pos(hass, "cover.ab_cover") == 100  # nothing reported yet
    for a, b in (("0", "100"), ("0", "40"), ("0", "0")):
        hass.states.async_set("input_number.a", a)
        hass.states.async_set("input_number.b", b)
        await hass.async_block_till_done()
        await hass.async_block_till_done()  # the members' new states reach the group
    assert pos(hass, "cover.ab_cover") == 0
    calls.clear()

    # b opened by hand: physical, the hold sends the group back to 0.
    hass.states.async_set("input_number.b", "60")
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert pos(hass, "cover.ab_cover") == 0
    assert calls.of("cover", "set_cover_position", "cover.b_cover")[-1]["position"] == 0


# --- hold --------------------------------------------------------------------


async def test_hold_strict(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "50")
    await setup_platform(
        hass, "cover", {"name": "x_cover", "features": ["hold", {"link": "input_number.x"}]}
    )
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": "cover.x_cover", "state": {"position": 0}, "duration": {"minutes": 10}},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value")[-1]["value"] == 0
    hass.states.async_set("input_number.x", "0")
    await hass.async_block_till_done()
    assert hass.states.get("cover.x_cover").attributes["hold"]["mode"] == "strict"

    # Remote request: rejected.
    calls.clear()
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": "cover.x_cover"}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls.of("input_number", "set_value")
    assert pos(hass, "cover.x_cover") == 0

    # Physical change: pushed back.
    hass.states.async_set("input_number.x", "60")
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value")[-1]["value"] == 0

    # Expired: changes allowed again.
    hass.states.async_set("input_number.x", "0")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await hass.async_block_till_done()
    assert hass.states.get("cover.x_cover").attributes["hold"] is None
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": "cover.x_cover"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value")[-1]["value"] == 100


async def test_hold_manual_and_release(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "50")
    await setup_platform(
        hass, "cover", {"name": "x_cover", "features": ["hold", {"link": "input_number.x"}]}
    )
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": "cover.x_cover", "state": {"position": 0}, "mode": "manual"},
        blocking=True,
    )
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": "cover.x_cover"}, blocking=True
    )
    await hass.async_block_till_done()
    assert calls.of("input_number", "set_value")[-1]["value"] == 100
    await hass.services.async_call(
        "gungors", "release", {"entity_id": "cover.x_cover"}, blocking=True
    )
    assert hass.states.get("cover.x_cover").attributes["hold"] is None


async def test_hold_service_on_entity_without_feature(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "50")
    await setup_platform(hass, "cover", {"name": "x_cover", "features": [{"link": "input_number.x"}]})
    with pytest.raises(Exception, match="not a gungors entity"):
        await hass.services.async_call(
            "gungors", "release", {"entity_id": "cover.x_cover"}, blocking=True
        )


def entity(hass: HomeAssistant, entity_id: str = WRAPPER):
    return hass.data["gungors"]["entities"][entity_id]


async def travel(hass: HomeAssistant, start: int, end: int) -> None:
    """Real IKEA movement: a report about every 3 %."""
    step = 3 if end > start else -3
    for p in [*range(start + step, end, step), end]:
        await report(hass, p)


# --- link: more ----------------------------------------------------------------


async def test_number_link_unknown_and_same_position(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "40")
    await setup_platform(hass, "cover", {"name": "x_cover", "features": [{"link": "input_number.x"}]})
    # Asking for where it already is sends nothing.
    await hass.services.async_call(
        "cover", "set_cover_position", {"entity_id": "cover.x_cover", "position": 40},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert not calls.of("input_number", "set_value")

    # The number goes unknown (e.g. its template fails): keep the last position.
    hass.states.async_set("input_number.x", "unknown")
    await hass.async_block_till_done()
    state = hass.states.get("cover.x_cover")
    assert state.state == "open"
    assert state.attributes["current_position"] == 40
    hass.states.async_set("input_number.x", "45")
    await hass.async_block_till_done()
    assert pos(hass, "cover.x_cover") == 45


async def test_original_missing_at_start(hass: HomeAssistant, calls) -> None:
    await setup_platform(hass, "cover", {"name": "x_cover", "features": [{"link": "input_number.x"}]})
    assert hass.states.get("cover.x_cover").state == "unavailable"
    hass.states.async_set("input_number.x", "30")
    await hass.async_block_till_done()
    assert pos(hass, "cover.x_cover") == 30


async def test_unknown_without_restore_is_unknown(hass: HomeAssistant, calls) -> None:
    hass.states.async_set("input_number.x", "unknown")
    await setup_platform(hass, "cover", {"name": "x_cover", "features": [{"link": "input_number.x"}]})
    state = hass.states.get("cover.x_cover")
    assert state.state == "unknown"
    assert state.attributes.get("current_position") is None


# --- direction: more -----------------------------------------------------------


async def test_direction_request_for_current_position(
    hass: HomeAssistant, calls, freezer
) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "set_cover_position", position=100)
    freezer.tick(timedelta(seconds=20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)  # no command, no retry
    assert hass.states.get(WRAPPER).state == "open"


async def test_direction_reversed_mid_move_is_physical(
    hass: HomeAssistant, calls, freezer
) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    await report(hass, 0)  # echo
    await travel(hass, 100, 88)
    assert hass.states.get(WRAPPER).state == "closing"
    # Someone presses "open" on the IKEA remote.
    await travel(hass, 88, 97)
    assert hass.states.get(WRAPPER).state == "opening"
    freezer.tick(timedelta(seconds=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["current_position"]) == ("open", 97)
    assert len(calls.of("cover", "set_cover_position", RAW)) == 1  # not fought


async def test_direction_attribute_only_update(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 50)
    await setup_guarded(hass)
    hass.states.async_set(
        RAW, "open", {"current_position": 50, "supported_features": 15, "linkquality": 80}
    )
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["current_position"]) == ("open", 50)


async def test_direction_unavailable_mid_move(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "set_cover_position", position=40)
    await travel(hass, 100, 91)
    hass.states.async_set(RAW, "unavailable")
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "unavailable"
    freezer.tick(timedelta(seconds=20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls.of("cover", "set_cover_position", RAW)) == 1  # no retry while away
    # Back where it stopped: that is the position now.
    raw(hass, 85)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert (state.state, state.attributes["current_position"]) == ("open", 85)


async def test_direction_strict_hold_pushes_back_small_drift(
    hass: HomeAssistant, calls, freezer
) -> None:
    raw(hass, 50)
    await setup_platform(
        hass, "cover",
        {"name": "sercan_cover", "features": ["hold", "direction", {"link": RAW}]},
    )
    await hass.services.async_call(
        "gungors", "hold", {"entity_id": WRAPPER, "state": {"position": 50}}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)
    freezer.tick(timedelta(minutes=5))  # past the settling window of the hold's request
    await report(hass, 51)  # the blind sagged by 1 %
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 50


async def test_direction_remove_cancels_timers(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set(WINDOW, "off")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "set_cover_position", position=40)
    await entity(hass).async_remove()
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(calls.of("cover", "set_cover_position", RAW)) == 1  # no retry after removal


# --- window: more --------------------------------------------------------------


async def test_window_stop_drops_pending(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    assert hass.states.get(WRAPPER).attributes["pending_position"] == 0
    await cover(hass, "stop_cover")
    assert hass.states.get(WRAPPER).attributes["pending_position"] is None
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_window_pending_survives_unavailable_device(hass: HomeAssistant, calls) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    hass.states.async_set(RAW, "unavailable")
    await hass.async_block_till_done()
    # Closed while the blind is away: nothing can be sent yet.
    hass.states.async_set(WINDOW, "off")
    await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)
    # The blind is back: the held command is replayed.
    raw(hass, 100)
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0
    assert hass.states.get(WRAPPER).attributes["pending_position"] is None


async def test_window_pending_already_reached_after_unavailable(
    hass: HomeAssistant, calls
) -> None:
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    await cover(hass, "close_cover")
    hass.states.async_set(RAW, "unavailable")
    await hass.async_block_till_done()
    raw(hass, 0)  # comes back already closed (closed by hand meanwhile)
    await hass.async_block_till_done()
    state = hass.states.get(WRAPPER)
    assert state.attributes["pending_position"] is None
    assert (state.state, state.attributes["current_position"]) == ("closed", 0)
    assert not calls.of("cover", "set_cover_position", RAW)


async def test_window_restore_and_save(hass: HomeAssistant, calls) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [(State(WRAPPER, "open", {"current_position": 30}),
          {"base": {"target": 30}, "window": {"pending": 30}})],
    )
    hass.states.async_set(WINDOW, "on")
    raw(hass, 100)
    await setup_guarded(hass)
    assert hass.states.get(WRAPPER).attributes["pending_position"] == 30
    # The position is the blind's, so only the pending command is saved.
    assert entity(hass).extra_restore_state_data.as_dict() == {"window": {"pending": 30}}


# --- buttons: more -------------------------------------------------------------


async def test_buttons_event_filtering(hass: HomeAssistant, calls) -> None:
    raw(hass, 100)
    await setup_platform(
        hass, "cover", {"name": "sercan_cover", "features": ["buttons", {"link": RAW}]}
    )
    for data in (
        {"entity_id": "cover.other_cover", "action": "close"},  # another cover
        {"entity_id": WRAPPER, "action": "toggle"},  # unknown action
    ):
        hass.bus.async_fire("gungors_physical_cover", data)
        await hass.async_block_till_done()
    assert not calls.of("cover", "set_cover_position", RAW)
    hass.bus.async_fire("gungors_physical_cover", {"entity_id": WRAPPER, "action": "close"})
    await hass.async_block_till_done()
    assert calls.of("cover", "set_cover_position", RAW)[-1]["position"] == 0


# --- hold: more ----------------------------------------------------------------


async def test_hold_strict_rejects_stop_and_rearms(hass: HomeAssistant, calls, freezer) -> None:
    raw(hass, 100)
    await setup_platform(
        hass, "cover", {"name": "sercan_cover", "features": ["hold", "direction", {"link": RAW}]}
    )
    hold = {"entity_id": WRAPPER, "state": {"position": 0}}
    await hass.services.async_call(
        "gungors", "hold", {**hold, "duration": {"minutes": 10}}, blocking=True
    )
    await travel(hass, 100, 70)
    await cover(hass, "stop_cover")
    assert not calls.of("cover", "stop_cover", RAW)

    # A new hold replaces the old one and its timer.
    await hass.services.async_call(
        "gungors", "hold", {**hold, "duration": {"minutes": 30}}, blocking=True
    )
    freezer.tick(timedelta(minutes=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["hold"]["state"] == {"position": 0}
    await hass.services.async_call("gungors", "release", {"entity_id": WRAPPER}, blocking=True)
    assert hass.states.get(WRAPPER).attributes["hold"] is None
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).attributes["hold"] is None


async def test_hold_until(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set("input_number.x", "50")
    await setup_platform(
        hass, "cover", {"name": "x_cover", "features": ["hold", {"link": "input_number.x"}]}
    )
    until = dt_util.now().replace(tzinfo=None) + timedelta(hours=1)  # naive local time
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": "cover.x_cover", "state": {"position": 0}, "until": until.isoformat()},
        blocking=True,
    )
    held = hass.states.get("cover.x_cover").attributes["hold"]
    assert dt_util.parse_datetime(held["until"]) == dt_util.as_utc(
        until.replace(tzinfo=dt_util.get_default_time_zone())
    )
    freezer.tick(timedelta(minutes=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("cover.x_cover").attributes["hold"] is None


async def test_hold_survives_restart(hass: HomeAssistant, calls, freezer) -> None:
    until = (dt_util.utcnow() + timedelta(minutes=10)).isoformat()
    expired = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
    held = {"state": {"position": 0}, "mode": "strict", "until": until}
    mock_restore_cache_with_extra_data(
        hass,
        [
            (State("cover.a_cover", "closed", {"current_position": 0}),
             {"base": {"target": 0}, "hold": held}),
            (State("cover.b_cover", "closed", {"current_position": 0}),
             {"base": {"target": 0}, "hold": {**held, "until": expired}}),
        ],
    )
    hass.states.async_set("input_number.a", "0")
    hass.states.async_set("input_number.b", "0")
    await setup_platform(
        hass, "cover",
        {"name": "a_cover", "features": ["hold", {"link": "input_number.a"}]},
        {"name": "b_cover", "features": ["hold", {"link": "input_number.b"}]},
    )
    assert hass.states.get("cover.a_cover").attributes["hold"] == held
    assert hass.states.get("cover.b_cover").attributes["hold"] is None
    assert entity(hass, "cover.a_cover").extra_restore_state_data.as_dict()["hold"] == held

    # Still enforced after the restart ...
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": "cover.a_cover"}, blocking=True
    )
    await hass.async_block_till_done()
    assert not calls.of("input_number", "set_value", "input_number.a")
    # ... until the restored end.
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("cover.a_cover").attributes["hold"] is None


async def test_hold_remove_cancels_timer(hass: HomeAssistant, calls, freezer) -> None:
    hass.states.async_set("input_number.x", "50")
    await setup_platform(
        hass, "cover", {"name": "x_cover", "features": ["hold", {"link": "input_number.x"}]}
    )
    await hass.services.async_call(
        "gungors", "hold",
        {"entity_id": "cover.x_cover", "state": {"position": 0}, "duration": {"minutes": 10}},
        blocking=True,
    )
    ent = entity(hass, "cover.x_cover")
    await ent.async_remove()
    await hass.async_block_till_done()
    assert ent._hold_unsub is None
