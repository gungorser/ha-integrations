"""Pushbuttons: the event wrapper, its slots, ready functions, custom actions, blink, presets."""
from __future__ import annotations

from datetime import timedelta
import json

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import color as color_util, dt as dt_util

from custom_components.gungors.features import button as button_mod, link_light
from custom_components.gungors.program import async_get_manager

from .conftest import setup_platform
from .test_program import LAMP, LIGHT, THERMOSTAT, TRV, add_entry, lamp, setup_all, trv

EVT = "event.salon_pushbutton_action"
WRAPPER = "event.salon_pushbutton"
TYPES = ["single", "double", "hold"]  # Zigbee2MQTT
TARGET = {"entity_id": [LIGHT, THERMOSTAT, "cover.salon_cover"]}


@pytest.fixture(autouse=True)
def fast_blink(monkeypatch) -> None:
    monkeypatch.setattr(link_light, "BLINK_ON", 0)
    monkeypatch.setattr(link_light, "BLINK_OFF", 0)
    monkeypatch.setattr(link_light, "BLINK_SETTLE", 0)
    monkeypatch.setattr(button_mod, "WARN_DELAY", 0)


_count = 0


def original(hass: HomeAssistant, event_type: str | None = None, state: str | None = None) -> None:
    global _count  # noqa: PLW0603
    _count += 1
    if state is None:
        state = (dt_util.utcnow() + timedelta(milliseconds=_count)).isoformat()
    # Zigbee2MQTT's shape: `2_hold` is event_type `hold` with attribute button `2`.
    attrs = {"event_type": event_type, "event_types": TYPES}
    if event_type and "_" in event_type:
        attrs["button"], attrs["event_type"] = event_type.split("_", 1)
    hass.states.async_set(EVT, state, attrs)


async def press(hass: HomeAssistant, slot: str) -> None:
    original(hass, slot)
    await hass.async_block_till_done()
    await hass.async_block_till_done()


async def setup_button(hass: HomeAssistant, *features, target=TARGET, name="salon_pushbutton"):
    if hass.states.get(EVT) is None:
        original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    entry = {"name": name, "features": [*features, {"link": EVT}]}
    if target is not None:
        entry["target"] = target
    await setup_platform(hass, "event", entry)


def lamp_calls(calls, service: str) -> list[dict]:
    return calls.of("light", service, LAMP)


# --- config ------------------------------------------------------------------------


async def test_slots_are_checked(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    toggle = {"action": "switch.toggle"}
    entries = [
        # climate without a program leaves 2_double free; 2_single is taken.
        ("b1", [{"buttons": {2: "climate"}}, {"custom": {"2_single": toggle}}]),
        ("b2", [{"buttons": {1: "light", 2: "light"}}]),
        ("b3", [{"buttons": {2: "climate"}}, {"custom": {"2_double": toggle, "2_hold": toggle}}]),
        ("b4", [{"buttons": {2: {"climate": {"program": "x"}}}}, {"custom": {"2_double": toggle}}]),
    ]
    await setup_platform(hass, "event", *[
        {"name": name, "features": [*features, {"link": EVT}]} for name, features in entries
    ])
    assert hass.states.get("event.b1") is None
    assert hass.states.get("event.b2") is None
    assert hass.states.get("event.b3") is not None
    assert hass.states.get("event.b4") is None


def _placed(buttons, keys=None, door=None) -> dict[str, str]:
    """{slot: function} after the location rule."""
    entries = button_mod.place_buttons(button_mod._buttons_schema(buttons), keys, door)
    return {f"{e['button']}_{g}": e["function"] for e in entries for g in e["gestures"]}


def test_location_rule() -> None:
    full = ["light", "cover", "climate", {"program": "x"}, {"custom": {"action": "switch.toggle"}}]
    right = _placed(full, 3, "right")
    assert {k: right[k] for k in ("3_single", "1_hold", "2_single", "2_double", "2_hold")} == {
        "3_single": "light", "1_hold": "cover", "2_single": "climate",
        "2_double": "program", "2_hold": "custom",
    }
    left = _placed(["light", "cover"], 3, "left")
    assert left["1_single"] == "light" and left["3_single"] == "cover"
    # Two keys: light near, cover far; the middle group needs its button.
    assert _placed(["light", "cover"], 2, "left") == {
        **{f"1_{g}": "light" for g in TYPES}, **{f"2_{g}": "cover" for g in TYPES},
    }
    assert _placed(["light", {"climate": {"button": 2}}], 2, "left")["2_single"] == "climate"
    # One key: only near. No door: every function gives its button.
    assert _placed(["light"], 1)["1_single"] == "light"
    assert _placed([{"light": {"button": 1}}], 2)["1_hold"] == "light"
    # A second light with its key and target.
    two = button_mod.place_buttons(
        button_mod._buttons_schema(["light", {"light": {"button": 2, "target": {"area_id": "araba"}}}]),
        3, "right",
    )
    assert [(e["button"], e["args"].get("target")) for e in two] == [
        (3, None), (2, {"area_id": ["araba"]}),
    ]
    # The old numbered form still reads (climate's program becomes its own function).
    old = _placed({1: "light", 2: {"climate": {"program": "x"}}, 3: "cover"})
    assert old["2_single"] == "climate" and old["2_double"] == "program"


@pytest.mark.parametrize(
    ("buttons", "keys", "door"),
    [
        (["climate"], 2, "left"),                  # no middle key
        (["cover"], 1, None),                      # one key: no far
        (["light"], 2, None),                      # no door
        (["light"], None, "left"),                 # no keys
        (["light", "light"], 3, "left"),           # one slot twice
        ([{"light": {"button": 3}}], 2, "left"),   # no such key
        ([{"climate": {"program": "x"}}], 3, "left"),  # program is its own function
    ],
)
def test_location_rule_errors(buttons, keys, door) -> None:
    import voluptuous as vol  # noqa: PLC0415

    with pytest.raises(vol.Invalid):
        button_mod.place_buttons(button_mod._buttons_schema(buttons), keys, door)


async def test_keys_and_door(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await hass.services.async_call(
        "gungors", "save_timeless_program",
        {"entity_id": "calendar.program", "summary": "Bebek uyku",
         "description": json.dumps({"target": THERMOSTAT, "climate": {"temperature": {"max": 20}}})},
        blocking=True,
    )
    original(hass, None, state="2026-01-01T00:00:00.000+00:00")
    features = [{"buttons": ["light", "climate", {"program": "Bebek uyku"}]}, {"link": EVT}]
    bad = [{"buttons": ["climate"]}, {"link": EVT}]  # two keys: no middle for climate
    await setup_platform(
        hass, "event",
        {"name": "salon_pushbutton", "keys": 3, "door": "right", "target": TARGET,
         "features": features},
        {"name": "bad", "keys": 2, "door": "right", "features": bad},
    )
    assert hass.states.get(WRAPPER) is not None
    assert hass.states.get("event.bad") is None
    calls.clear()
    await press(hass, "3_single")  # near the door on the right: light (on -> off)
    assert lamp_calls(calls, "turn_off")
    await press(hass, "2_double")  # middle double: program
    assert async_get_manager(hass).is_running(async_get_manager(hass).timeless_uid("Bebek uyku"))
    calls.clear()
    await press(hass, "1_single")  # far: nothing configured
    assert not calls.of("light", "turn_on", LAMP) and not calls.of("light", "turn_off", LAMP)


async def test_presses_are_mirrored(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {3: "cover"}})
    state = hass.states.get(WRAPPER)
    assert state.state == "unknown"  # the original's last event is not a press
    await press(hass, "2_hold")
    state = hass.states.get(WRAPPER)
    assert state.attributes["event_type"] == "2_hold"
    # Back after unavailable with the same event: not a press.
    last = hass.states.get(EVT).state
    before = hass.states.get(WRAPPER).state
    hass.states.async_set(EVT, "unavailable", {})
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "unavailable"
    hass.states.async_set(EVT, last, {"event_type": "hold", "button": "2", "event_types": TYPES})
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == before


async def test_original_added_later_is_not_a_press(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    # The wrapper starts before the original exists (Home Assistant startup).
    await setup_platform(hass, "event", {"name": "salon_pushbutton",
                                         "features": [{"buttons": {3: "cover"}}, {"link": EVT}],
                                         "target": TARGET})
    calls.clear()
    original(hass, "3_double", state="2026-01-01T00:00:00.000+00:00")  # restored last event
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "unknown"
    assert not calls.of("cover", "set_cover_position", "cover.salon_blind")
    # MQTT: the original first shows unknown, then its restored last event.
    hass.states.async_set(EVT, "unknown", {"event_types": TYPES})
    await hass.async_block_till_done()
    original(hass, "3_double", state="2026-01-02T00:00:00.000+00:00")
    await hass.async_block_till_done()
    assert hass.states.get(WRAPPER).state == "unknown"
    assert not calls.of("cover", "set_cover_position", "cover.salon_blind")
    await press(hass, "3_double")
    assert calls.of("cover", "set_cover_position", "cover.salon_blind")


async def test_one_button_model(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"custom": {"single": {"action": "switch.toggle",
                                                    "target": {"entity_id": "switch.x"}}}})
    hass.services.async_register("switch", "toggle", lambda call: None)
    await press(hass, "single")
    assert hass.states.get(WRAPPER).attributes["event_type"] == "1_single"
    assert calls.of("switch", "toggle", "switch.x")


# --- light ---------------------------------------------------------------------------


async def test_light_single(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: "light"}})
    await press(hass, "1_single")  # on -> off
    assert lamp_calls(calls, "turn_off")
    await press(hass, "1_single")  # off -> default (white, 100 %)
    data = lamp_calls(calls, "turn_on")[-1]
    assert data["brightness"] == 255
    assert tuple(data["rgb_color"]) == (255, 255, 255)


async def test_light_double_toggles_brightness(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: "light"}})
    lamp(hass, True, 200, rgb=(255, 0, 0))
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_double")  # -> dim, colour kept
    data = lamp_calls(calls, "turn_on")[-1]
    assert data["brightness"] == round(10 * 255 / 100)
    assert "rgb_color" not in data
    lamp(hass, True, round(10 * 255 / 100), rgb=(255, 0, 0))
    await hass.async_block_till_done()
    await press(hass, "1_double")  # at dim -> default brightness
    assert lamp_calls(calls, "turn_on")[-1]["brightness"] == 255
    # All off: on at the default colour, dim.
    lamp(hass, False)
    await hass.async_block_till_done()
    await press(hass, "1_double")
    data = lamp_calls(calls, "turn_on")[-1]
    assert data["brightness"] == round(10 * 255 / 100)
    assert tuple(data["rgb_color"]) == (255, 255, 255)


async def test_light_hold_toggles_colour(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: "light"}})
    lamp(hass, True, 100, rgb=(255, 255, 255))
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_hold")  # -> night, brightness kept
    data = lamp_calls(calls, "turn_on")[-1]
    assert tuple(data["rgb_color"]) == (255, 0, 0)
    assert "brightness" not in data
    lamp(hass, True, 100, rgb=(255, 0, 0))
    await hass.async_block_till_done()
    await press(hass, "1_hold")
    assert tuple(lamp_calls(calls, "turn_on")[-1]["rgb_color"]) == (255, 255, 255)
    lamp(hass, False)
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_hold")  # all off: nothing
    assert not lamp_calls(calls, "turn_on")


async def test_presets_from_yaml_and_args(hass: HomeAssistant, calls) -> None:
    assert await async_setup_component(
        hass, "gungors",
        {"gungors": {"presets": {"light": {"dim": {"brightness_pct": 20},
                                           "warm": {"rgb_color": [255, 180, 100]}}}}},
    )
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: {"light": {"hold": "warm"}}}})
    lamp(hass, True, 200, rgb=(255, 255, 255))
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_double")
    assert lamp_calls(calls, "turn_on")[-1]["brightness"] == round(20 * 255 / 100)
    await press(hass, "1_hold")
    assert tuple(lamp_calls(calls, "turn_on")[-1]["rgb_color"]) == (255, 180, 100)


async def test_rule_limit_warns(hass: HomeAssistant, calls, freezer: FrozenDateTimeFactory) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: "light"}})
    lamp(hass, False)
    await hass.async_block_till_done()
    await add_entry(hass, "sleep", {"target": LIGHT, "light": {"brightness_pct": {"max": 40}}},
                    timedelta(minutes=-1), timedelta(hours=2))
    calls.clear()
    await press(hass, "1_single")
    await hass.async_block_till_done()
    ons = lamp_calls(calls, "turn_on")
    # Turned on in the default preset: clamped to the rule, no warning.
    assert ons[0]["brightness"] == round(40 * 255 / 100)
    assert not [d for d in ons if tuple(d.get("rgb_color") or ()) == (255, 0, 0)]
    # Turned off: an untouched request, no warning either.
    lamp(hass, True, round(40 * 255 / 100), rgb=(255, 255, 255))
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_single")
    await hass.async_block_till_done()
    assert lamp_calls(calls, "turn_off")
    assert not lamp_calls(calls, "turn_on")
    # Double (dim) with a stricter rule: clamped, then two red blinks.
    await add_entry(hass, "night", {"target": LIGHT, "light": {"brightness_pct": {"max": 5}}},
                    timedelta(minutes=-1), timedelta(hours=2))
    lamp(hass, False)
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_double")
    await hass.async_block_till_done()
    ons = lamp_calls(calls, "turn_on")
    assert ons[0]["brightness"] == round(5 * 255 / 100)
    reds = [d for d in ons if tuple(d.get("rgb_color") or ()) == (255, 0, 0)]
    assert len(reds) == 2


# --- climate ---------------------------------------------------------------------


async def test_climate_toggle_blinks(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {2: "climate"}})
    calls.clear()
    await press(hass, "2_single")
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "heat"
    greens = [d for d in lamp_calls(calls, "turn_on") if tuple(d.get("rgb_color") or ()) == (0, 255, 0)]
    assert len(greens) == 1
    trv(hass, "heat")
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "2_single")
    assert calls.of("climate", "set_hvac_mode", TRV)[-1]["hvac_mode"] == "off"
    blues = [d for d in lamp_calls(calls, "turn_on") if tuple(d.get("rgb_color") or ()) == (0, 0, 255)]
    assert len(blues) == 1


async def test_climate_program(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await hass.services.async_call(
        "gungors", "save_timeless_program",
        {"entity_id": "calendar.program", "summary": "Bebek uyku",
         "description": json.dumps({"target": THERMOSTAT, "climate": {"temperature": {"max": 20}}})},
        blocking=True,
    )
    await setup_button(hass, {"buttons": {2: {"climate": {"program": "bebek uyku"}}}})
    manager = async_get_manager(hass)
    uid = manager.timeless_uid("Bebek uyku")
    calls.clear()
    await press(hass, "2_double")
    assert manager.is_running(uid)
    greens = [d for d in lamp_calls(calls, "turn_on") if tuple(d.get("rgb_color") or ()) == (0, 255, 0)]
    assert len(greens) == 2
    await press(hass, "2_double")
    await hass.async_block_till_done()
    assert not manager.is_running(uid)


# --- cover -----------------------------------------------------------------------


async def test_cover_single_double(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    await setup_button(hass, {"buttons": {3: "cover"}})
    calls.clear()
    await press(hass, "3_double")
    assert calls.of("cover", "set_cover_position", "cover.salon_blind")[-1]["position"] == 0


async def test_cover_press_while_moving_stops(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    hass.services.async_register("cover", "stop_cover", lambda call: None)
    hass.states.async_set("cover.plain", "opening", {"supported_features": 15})
    await setup_button(hass, {"buttons": {3: "cover"}},
                       target={"entity_id": ["cover.plain", "cover.salon_cover"]})
    calls.clear()
    await press(hass, "3_single")
    assert calls.of("cover", "stop_cover", "cover.plain")
    assert not calls.of("cover", "open_cover", "cover.plain")
    # A gungors cover shown as moving: the stop goes through its link.
    state = hass.states.get("cover.salon_cover")
    hass.states.async_set("cover.salon_cover", "closing", state.attributes)
    calls.clear()
    await press(hass, "3_double")
    assert calls.of("cover", "stop_cover", "cover.salon_blind")
    assert not calls.of("cover", "set_cover_position", "cover.salon_blind")


async def test_cover_hold_floor(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    hass.services.async_register("scene", "create", lambda call: None)
    hass.services.async_register("scene", "turn_on", lambda call: None)
    area = ar.async_get(hass).async_create("Salon")
    registry = er.async_get(hass)
    registry.async_update_entity("cover.salon_cover", area_id=area.id)
    await setup_button(hass, {"buttons": {3: "cover"}}, target={"area_id": area.id})
    calls.clear()
    await press(hass, "3_hold")
    assert calls.of("scene", "create")[-1]["snapshot_entities"] == ["cover.salon_cover"]
    assert calls.of("cover", "close_cover", "cover.salon_cover")


# --- target ----------------------------------------------------------------------


async def test_target_defaults_to_device_area(hass: HomeAssistant, calls) -> None:
    await setup_all(hass)
    area = ar.async_get(hass).async_create("Salon")
    registry = er.async_get(hass)
    registry.async_update_entity(LIGHT, area_id=area.id)
    entry = registry.async_get_or_create("event", "mqtt", "salon_btn", suggested_object_id="salon_pushbutton_action")
    registry.async_update_entity(entry.entity_id, area_id=area.id)
    assert entry.entity_id == EVT
    await setup_button(hass, {"buttons": {1: "light"}}, target=None)
    calls.clear()
    await press(hass, "1_single")
    assert lamp_calls(calls, "turn_off")


# --- blink -----------------------------------------------------------------------


async def test_blink_restores(hass: HomeAssistant, calls, monkeypatch) -> None:
    # Short real steps: with no delay at all, the non-blocking calls of one step may
    # run after those of the next, so the recorded order is not the sent order.
    monkeypatch.setattr(link_light, "BLINK_ON", 0.01)
    monkeypatch.setattr(link_light, "BLINK_OFF", 0.01)
    await setup_all(hass)
    lamp(hass, True, 120, rgb=(0, 255, 0))
    await hass.async_block_till_done()
    calls.clear()
    await hass.services.async_call(
        "gungors", "blink", {"entity_id": LIGHT, "rgb_color": [0, 0, 255], "count": 2}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    ons = lamp_calls(calls, "turn_on")
    assert [tuple(d.get("rgb_color") or ()) for d in ons[:2]] == [(0, 0, 255), (0, 0, 255)]
    assert ons[-1]["brightness"] == 120
    # Put back in the colour mode it was in (hs).
    assert tuple(ons[-1]["hs_color"]) == tuple(color_util.color_RGB_to_hs(0, 255, 0))
    # An off light stays off.
    lamp(hass, False)
    await hass.async_block_till_done()
    calls.clear()
    await hass.services.async_call("gungors", "blink", {"entity_id": LIGHT}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert lamp_calls(calls, "turn_off")


async def test_blink_during_blink_restores_first(hass: HomeAssistant, calls, monkeypatch) -> None:
    """A blink started during another puts back the state from before the first."""
    monkeypatch.setattr(link_light, "BLINK_ON", 0.05)
    monkeypatch.setattr(link_light, "BLINK_OFF", 0.05)
    await setup_all(hass)
    lamp(hass, True, 120, rgb=(0, 255, 0))
    await hass.async_block_till_done()
    calls.clear()
    await hass.services.async_call(
        "gungors", "blink", {"entity_id": LIGHT, "rgb_color": [255, 0, 0], "count": 2}, blocking=True
    )
    await hass.async_block_till_done()
    # The lamp reports the blink colour, then a second blink starts.
    lamp(hass, True, 255, rgb=(255, 0, 0))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "gungors", "blink", {"entity_id": LIGHT, "rgb_color": [255, 0, 0], "count": 2}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    last = lamp_calls(calls, "turn_on")[-1]
    assert last["brightness"] == 120
    assert tuple(last["hs_color"]) == tuple(color_util.color_RGB_to_hs(0, 255, 0))


# --- presets as program defaults -----------------------------------------------------


async def test_default_preset_is_program_default(hass: HomeAssistant, calls) -> None:
    assert await async_setup_component(
        hass, "gungors",
        {"gungors": {"presets": {"light": {"default": {"state": "off", "rgb_color": [255, 200, 150]}}}}},
    )
    await setup_all(hass)
    defaults = hass.states.get("calendar.program").attributes["defaults"]
    assert defaults["light"]["rgb_color"] == [255, 200, 150]


async def test_light_hold_back_with_a_white_only_light(hass: HomeAssistant, calls) -> None:
    """A brightness-only light in the target does not keep the colour from going back."""
    plain = "light.salon_plain"
    await setup_all(hass)
    await setup_button(hass, {"buttons": {1: "light"}}, target={"entity_id": [LIGHT, plain]})
    lamp(hass, True, 100, rgb=(255, 255, 255))
    hass.states.async_set(plain, "on", {"supported_color_modes": ["brightness"],
                                        "color_mode": "brightness", "brightness": 100})
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "1_hold")  # -> night
    assert tuple(lamp_calls(calls, "turn_on")[-1]["rgb_color"]) == (255, 0, 0)
    lamp(hass, True, 100, rgb=(255, 0, 0))
    await hass.async_block_till_done()
    await press(hass, "1_hold")  # the colour light is at night -> back
    assert tuple(lamp_calls(calls, "turn_on")[-1]["rgb_color"]) == (255, 255, 255)


async def test_program_name_ignores_accents(hass: HomeAssistant, calls) -> None:
    """`Kaan gündüz uyku` on the button starts the stored `kaan gunduz uyku`."""
    await setup_all(hass)
    await hass.services.async_call(
        "gungors", "save_timeless_program",
        {"entity_id": "calendar.program", "summary": "kaan gunduz uyku",
         "description": json.dumps({"target": THERMOSTAT, "climate": {"temperature": {"max": 20}}})},
        blocking=True,
    )
    await setup_button(hass, {"buttons": [
        {"program": {"program": "Kaan Gündüz Uyku", "button": 1}},
        {"program": {"program": "Yok böyle", "button": 2}},
    ]})
    manager = async_get_manager(hass)
    uid = manager.timeless_uid("KAAN GÜNDÜZ UYKU")
    await press(hass, "1_double")
    assert manager.is_running(uid)
    # A name that matches nothing: the lights warn (two red blinks), nothing starts.
    lamp(hass, True, 255, rgb=(255, 255, 255))
    await hass.async_block_till_done()
    calls.clear()
    await press(hass, "2_double")
    await hass.async_block_till_done()
    reds = [d for d in lamp_calls(calls, "turn_on") if tuple(d.get("rgb_color") or ()) == (255, 0, 0)]
    assert len(reds) == 2
