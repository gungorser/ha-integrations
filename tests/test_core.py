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
