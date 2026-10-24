"""Root cause check: Home Assistant refuses start/end with different UTC offsets."""
from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from .test_program import CALENDAR, LIGHT, setup_all


async def test_offsets_across_dst(hass: HomeAssistant, calls, hass_ws_client) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam")
    await setup_all(hass)
    client = await hass_ws_client(hass)
    event = {
        "summary": "gece",
        "description": json.dumps({"target": LIGHT, "light": {"brightness_pct": {"max": 30}}}),
        "rrule": "FREQ=DAILY",
    }
    # As card 1.10.1 sends it: each side with its own offset (DST ends between them).
    await client.send_json_auto_id({"type": "calendar/event/create", "entity_id": CALENDAR,
        "event": {**event, "dtstart": "2026-10-24T19:00:00+02:00", "dtend": "2026-10-25T08:00:00+01:00"}})
    result = await client.receive_json()
    assert not result["success"] and "same timezone" in result["error"]["message"]
    # Both in UTC: accepted, and stored in house time (19:00-08:00 every day).
    await client.send_json_auto_id({"type": "calendar/event/create", "entity_id": CALENDAR,
        "event": {**event, "dtstart": "2026-10-24T17:00:00Z", "dtend": "2026-10-25T07:00:00Z"}})
    assert (await client.receive_json())["success"]
    from custom_components.gungors.program import async_get_manager  # noqa: PLC0415
    stored = async_get_manager(hass).calendar.events[0]
    assert (stored.dtstart.hour, stored.dtend.hour) == (19, 8)
