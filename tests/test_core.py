"""Feature machinery (core.py): the feature list and the composition rules."""
from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant

from custom_components.gungors.core import Feature, feature_list_validator, register

from .conftest import setup_platform


@pytest.mark.parametrize(
    ("features", "message"),
    [
        ([], "features must be a non-empty list"),
        ("link", "features must be a non-empty list"),
        ([{"invert": None, "link": "cover.x"}], "invalid feature entry"),
        (["fly"], "unknown cover feature 'fly'"),
        ([{"window": "not an entity id"}], "feature 'window'"),
    ],
)
async def test_feature_list_validation(hass: HomeAssistant, features, message) -> None:
    with pytest.raises(vol.Invalid, match=message):
        feature_list_validator("cover")(features)


async def test_feature_registered_twice() -> None:
    class Again(Feature):
        feature_name = "link"

    with pytest.raises(ValueError, match="cover feature link registered twice"):
        register("cover")(Again)


async def test_invalid_entries_are_not_created(
    hass: HomeAssistant, calls, caplog: pytest.LogCaptureFixture
) -> None:
    hass.states.async_set("cover.a_blind", "open", {"current_position": 50})
    hass.states.async_set("cover.b_blind", "open", {"current_position": 50})
    hass.states.async_set("cover.c_blind", "open", {"current_position": 50})
    await setup_platform(
        hass,
        "cover",
        # Unknown feature: rejected by the platform schema.
        {"name": "a_cover", "features": ["fly", {"link": "cover.a_blind"}]},
        # A feature listed twice.
        {"name": "b_cover", "features": ["invert", "invert", {"link": "cover.b_blind"}]},
        # Two movement trackers.
        {"name": "c_cover", "features": ["direction", "travel_time", {"link": "cover.c_blind"}]},
    )
    assert hass.states.get("cover.a_cover") is None
    assert hass.states.get("cover.b_cover") is None
    assert hass.states.get("cover.c_cover") is None
    assert "feature listed twice: invert" in caplog.text
    assert "'motion' is claimed by both 'direction' and 'travel_time'" in caplog.text


async def test_entity_id_rename_keeps_state_and_claims(hass: HomeAssistant) -> None:
    """A registry rename re-adds the same object: no restore, claims kept."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    from custom_components.gungors.core import CLAIMS, ENTITIES  # noqa: PLC0415

    hass.states.async_set("light.salon_lamp", "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff"})
    await setup_platform(
        hass, "light",
        {"name": "salon_light", "unique_id": "salon_light", "features": [{"link": "light.salon_lamp"}]},
    )
    entity = hass.data["gungors"][ENTITIES]["light.salon_light"]
    restored = []
    original_restore = entity.gw_restore
    entity.gw_restore = lambda last, extra: (restored.append(last), original_restore(last, extra))
    er.async_get(hass).async_update_entity("light.salon_light", new_entity_id="light.salon")
    await hass.async_block_till_done()
    assert hass.states.get("light.salon_light") is None
    assert hass.states.get("light.salon").state == "on"
    assert hass.data["gungors"][ENTITIES] == {"light.salon": entity}
    assert hass.data["gungors"][CLAIMS].get("light.salon_lamp") == "light.salon_light"
    assert restored == []


async def test_same_name_in_two_domains(hass: HomeAssistant) -> None:
    """light.salon and cover.salon share a name; their claims stay apart."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    from custom_components.gungors.core import CLAIMS  # noqa: PLC0415

    hass.states.async_set("light.salon_lamp", "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff"})
    hass.states.async_set("cover.salon_blind", "open", {"current_position": 50, "supported_features": 15})
    await setup_platform(
        hass, "light", {"name": "salon", "unique_id": "salon_light", "features": [{"link": "light.salon_lamp"}]}
    )
    await setup_platform(
        hass, "cover", {"name": "salon", "unique_id": "salon_cover", "features": [{"link": "cover.salon_blind"}]}
    )
    assert hass.states.get("light.salon") is not None
    assert hass.states.get("cover.salon") is not None
    claims = hass.data["gungors"][CLAIMS]
    assert claims == {"light.salon_lamp": "light.salon", "cover.salon_blind": "cover.salon"}
    er.async_get(hass).async_remove("light.salon")
    await hass.async_block_till_done()
    assert claims == {"cover.salon_blind": "cover.salon"}
