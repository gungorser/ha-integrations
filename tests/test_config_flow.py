"""Config entry and options flow: presets and Immich in the UI, imported from YAML."""
from __future__ import annotations

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


def _builtin_input() -> dict:
    """The presets form as shown (the built-in presets), section by section."""
    return {
        "light_default": {"state": "off", "rgb_color": [255, 255, 255], "brightness_pct": 100},
        "light_dim": {"brightness_pct": 10},
        "light_night": {"rgb_color": [255, 0, 0]},
        "climate_default": {"state": "off"},
        "cover_default": {"current_position": SUN},
    }


async def _open(hass: HomeAssistant, entry: MockConfigEntry, *steps: str) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    for step in steps:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


async def test_light_preset_form_applies_without_restart(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {}})
    result = await _open(hass, entry, "presets")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_builtin_input(), "light_night": {"rgb_color": [255, 120, 0]}}
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
        {**_builtin_input(), "light_dim": {"templates": {"brightness_pct": "{{ 5 + "}}},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_preset"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**_builtin_input(),
         "light_dim": {"brightness_pct": 50, "templates": {"brightness_pct": "{{ 5 + 5 }}"}}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["presets"]["light"]["dim"] == {"brightness_pct": "{{ 5 + 5 }}"}


async def test_cover_preset_number_or_template(hass: HomeAssistant) -> None:
    entry = await _entry(hass, {"presets": {"cover": {"default": {"current_position": SUN}}}})
    result = await _open(hass, entry, "presets")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_builtin_input(), "cover_default": {"current_position": "40"}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["presets"]["cover"]["default"] == {"current_position": 40}
    await hass.async_block_till_done()
    assert preset(hass, "cover", "default") == {"position": 40.0}


def _immich_input(**changes) -> dict:
    data = {
        "general": {"url": "http://immich", "scan_interval": 0, "owner": "sercan",
                    "share_albums": True},
        "users": {},
        "people": {"people": {"Kaan": "Kaan"}},
        "trips": {"distance_km": 60, "advanced": {"min_days": {"netherlands": 2}}},
        "faceless": {"home": "trash", "trip": "album", "album": "check_before_delete",
                     "wait_hours": 24},
        "partou": {"username": "a@b.c", "after": "18:00:00"},
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
        _immich_input(users={"melike": "new2", "new_user": "kaan", "new_user_key": "k3"}),
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
        result["flow_id"], _immich_input(users={"melike": "-"})
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
    assert result["errors"] == {"base": "invalid_immich"}
