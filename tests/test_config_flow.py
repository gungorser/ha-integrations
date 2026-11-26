"""Config entry and options flow: presets and Immich in the UI, imported from YAML."""
from __future__ import annotations

import datetime

import pytest
import voluptuous_serialize

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.gungors.const import DOMAIN
from custom_components.gungors.presets import preset

SUN = "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"
IMMICH = {
    "url": "http://immich",
    "users": {"sercan": "k1", "melike": "k2"},
    "owner": "sercan",
    "scan_interval": 0,
    "people": {"Kaan": "Kaan"},
    "trips": {"distance_km": 50, "min_days": {"netherlands": 2}},
    "partou": {"username": "a@b.c", "password": "pw", "after": "18:00"},
}


@pytest.fixture(autouse=True)
def no_immich_network(monkeypatch):
    async def run(self, force: bool = False) -> None:
        return None

    monkeypatch.setattr("custom_components.gungors.immich.ImmichManager.async_run", run)


async def _entry(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Gungor Customizations", data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def test_yaml_is_imported_into_an_entry(hass: HomeAssistant) -> None:
    config = {DOMAIN: {
        "presets": {
            "light": {"dim": {"brightness_pct": 20}},
            "cover": {"default": {"current_position": SUN}},
        },
        "immich": IMMICH,
    }}
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    options = entries[0].options
    assert options["presets"]["light"]["dim"] == {"brightness_pct": 20}
    assert "night" not in options["presets"]["light"]  # still the built-in one
    assert preset(hass, "light", "night") == {"color": (255, 0, 0)}
    assert options["presets"]["cover"]["default"] == {"current_position": SUN}
    assert options["immich"]["users"] == {"sercan": "k1", "melike": "k2"}
    assert options["immich"]["partou"]["password"] == "pw"
    assert options["immich"]["partou"]["after"] == "18:00"
    assert preset(hass, "light", "dim") == {"brightness": 20.0}
    assert hass.states.get("select.immich") is not None


async def test_entry_wins_and_yaml_gets_a_repair(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={},
        options={"presets": {"light": {"dim": {"brightness_pct": 30}}}},
    )
    entry.add_to_hass(hass)
    config = {DOMAIN: {"presets": {"light": {"dim": {"brightness_pct": 20}}}}}
    assert await async_setup_component(hass, DOMAIN, config)
    await hass.async_block_till_done()
    assert preset(hass, "light", "dim") == {"brightness": 30.0}
    assert ir.async_get(hass).async_get_issue(DOMAIN, "yaml_ignored") is not None
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_step_single_instance(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {"presets": {}}
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT


GROUPS = {"light": ("default", "dim", "night"), "climate": ("default",), "cover": ("default",)}


def _form(parts: dict) -> dict:
    """{"light_dim": {...}} -> the presets form (a section per domain, prefixed fields)."""
    return {
        group: {
            f"{name}_{k}": v for name in names for k, v in parts.get(f"{group}_{name}", {}).items()
        }
        for group, names in GROUPS.items()
    }


def _builtin_input() -> dict:
    """The presets form as shown (the built-in presets)."""
    return _form({
        "light_default": {"state": "off", "rgb_color": [255, 255, 255], "brightness_pct": 100},
        "light_dim": {"brightness_pct": 10},
        "light_night": {"rgb_color": [255, 0, 0]},
        "climate_default": {"state": "off"},
        "cover_default": {"current_position": SUN},
    })


def _with(**parts) -> dict:
    """The built-in presets form with some presets changed."""
    form = _builtin_input()
    for key, values in parts.items():
        domain, name = key.split("_", 1)
        form[domain] = {
            **{k: v for k, v in form[domain].items() if not k.startswith(f"{name}_")},
            **{f"{name}_{k}": v for k, v in values.items()},
        }
    return form


async def _open(hass: HomeAssistant, entry: MockConfigEntry, *path: str) -> dict:
    """Configure: menu Presets / Services, Services: menu Immich / Budget Thuis."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["presets", "services"]
    for step in path:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == path[-1]
    if path[-1] == "presets":
        assert list(result["data_schema"].schema) == list(GROUPS)
        shown = voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        assert [(f["name"], f["type"], f["expanded"]) for f in shown] == [
            (g, "expandable", False) for g in GROUPS
        ]
    return result


async def test_light_preset_form_applies_without_restart(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}})
    result = await _open(hass, entry, "presets")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _with(light_night={"rgb_color": [255, 120, 0]})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["presets"]["light"]["night"] == {"rgb_color": [255, 120, 0]}
    assert preset(hass, "light", "night") == {"color": (255, 120, 0)}


async def test_light_preset_template_and_bad_template(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}})
    result = await _open(hass, entry, "presets")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _with(light_dim={"templates": {"brightness_pct": "{{ 5 + "}}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _with(light_dim={"brightness_pct": 50, "templates": {"brightness_pct": "{{ 5 + 5 }}"}}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["presets"]["light"]["dim"] == {"brightness_pct": "{{ 5 + 5 }}"}


async def test_cover_preset_number_or_template(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {"cover": {"default": {"current_position": SUN}}}})
    result = await _open(hass, entry, "presets")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _with(cover_default={"current_position": "40"})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["presets"]["cover"]["default"] == {"current_position": 40}
    await hass.async_block_till_done()
    assert preset(hass, "cover", "default") == {"position": 40.0}


def _immich_input(**changes) -> dict:
    data = {
        "general": {"url": "http://immich", "scan_interval": {"hours": 0, "minutes": 0, "seconds": 0}, "owner": "sercan",
                    "share_albums": True},
        "users": {"users": [{"name": "sercan", "key": ""}, {"name": "melike", "key": ""}]},
        "people": {"people": {"Kaan": "Kaan"}},
        "trips": {"distance_km": 60, "advanced": {"min_days": {"netherlands": 2}}},
        "faceless": {"home": "trash", "trip": "album", "wait": {"hours": 24, "minutes": 0, "seconds": 0}},
        "videos": {"per_run": 3, "match": 0.5, "maybe": 0},
        "partou": {"username": "a@b.c", "password": "********", "after": "18:00:00"},
    }
    for key, value in changes.items():
        data[key] = {**data[key], **value}
    return data


async def test_immich_form_keeps_secrets_and_adds_a_user(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}, "immich": dict(IMMICH)})
    result = await _open(hass, entry, "services", "immich")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _immich_input(users={"users": [
            {"name": "sercan", "key": ""}, {"name": "melike", "key": "new2"},
            {"name": "kaan", "key": "k3"}]}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    immich = entry.options["immich"]
    assert immich["users"] == {"sercan": "k1", "melike": "new2", "kaan": "k3"}
    assert immich["partou"]["password"] == "pw"
    assert immich["partou"]["after"] == "18:00"
    assert immich["trips"] == {"min_days": {"netherlands": 2}, "distance_km": 60.0}
    assert immich["share_albums"] is True
    assert immich["faceless"]["home"] == "trash" and immich["faceless"]["trip"] == "album"


async def test_immich_form_removes_a_user_and_turns_off(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}, "immich": dict(IMMICH)})
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _immich_input(users={"users": [{"name": "sercan"}]})
    )
    assert entry.options["immich"]["users"] == {"sercan": "k1"}
    await hass.async_block_till_done()

    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _immich_input(general={"url": ""})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "immich" not in entry.options


async def test_immich_form_bad_advanced(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}, "immich": dict(IMMICH)})
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _immich_input(trips={"advanced": {"detail": {"turkey": "street"}}})
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid"}


async def test_immich_form_new_user_needs_a_key(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}, "immich": dict(IMMICH)})
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _immich_input(users={"users": [{"name": "kaan", "key": ""}]})
    )
    assert result["errors"] == {"base": "invalid"}
    assert "kaan" in result["description_placeholders"]["error"]


async def test_immich_form_holds_secrets_and_durations(hass: HomeAssistant) -> None:
    from custom_components.gungors.config_flow import _immich_form_values

    values = _immich_form_values({**IMMICH, "scan_interval": 600, "faceless": {"wait_hours": 36}})
    assert values["partou"]["password"] == "pw"
    assert values["users"]["users"][0] == {"name": "sercan", "key": "k1"}
    assert values["general"]["scan_interval"] == {"hours": 0, "minutes": 10, "seconds": 0}
    assert values["faceless"]["wait"] == {"hours": 36, "minutes": 0, "seconds": 0}


async def test_sync_now_checkbox_saves_and_starts_the_run(hass: HomeAssistant, monkeypatch) -> None:
    calls = []

    class Manager:
        async def async_run(self, force=False):
            calls.append(force)

    entry = await _entry(hass, {"presets": {}, "immich": dict(IMMICH)})
    monkeypatch.setattr(
        "custom_components.gungors.config_flow.async_get_manager", lambda h: Manager()
    )
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_immich_input(), "sync_now": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "sync_now" not in entry.options["immich"]
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=5))
    await hass.async_block_till_done()
    assert calls == [True]


async def test_immich_form_video_scan(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}, "immich": {**IMMICH, "videos": {"ml_url": "http://ml", "frames": 6}}})
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _immich_input(videos={"ml_url": "http://ml2", "per_run": 5, "match": 0.55, "maybe": 0.4}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["immich"]["videos"] == {
        "ml_url": "http://ml2", "frames": 6, "per_run": 5, "match": 0.55, "maybe": 0.4
    }
    result = await _open(hass, entry, "services", "immich")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _immich_input(videos={"ml_url": ""})
    )
    assert "videos" not in entry.options["immich"]
