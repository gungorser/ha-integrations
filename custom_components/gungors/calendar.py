"""Calendar platform for the gungors integration: `calendar.program`.

```yaml
calendar:
  - platform: gungors
    name: program
    defaults:            # optional, these are the built-in ones
      light: {state: "off", rgb_color: [255, 255, 255]}
      climate: {state: "off"}
      cover:             # open while the sun is up (a template, rendered when applied)
        current_position: "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"
```

Each entry is a program (its description: see rules.py); a program's `start`
or `end` can be relative to the sun, and the events shown here carry the real
times of each occurrence. The entries live in
the program manager (program.py), which refuses a change that leaves an entry
unreadable or makes two entries conflict; the error shows where the change was
made (the calendar dialog, the program card, a service call). `defaults` are
the values the device returns to when a `set` rule ends; a template default is
also applied whenever its result changes; `gungors.set_program_defaults` (field `defaults`, the program card's
defaults tab) replaces them and keeps them in the store; the `defaults` attribute
shows the ones in use.
`gungors.pause_program` / `gungors.resume_program` (field `uid`) pause and resume
an entry's running occurrence; the `paused` attribute lists the paused entries.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ical.calendar import Calendar
from ical.event import Event
from ical.exceptions import CalendarParseError
from ical.store import EventStore, EventStoreError
from ical.types import Range, Recur
import voluptuous as vol

from homeassistant.components.calendar import (
    EVENT_END,
    EVENT_RRULE,
    EVENT_START,
    PLATFORM_SCHEMA as CALENDAR_PLATFORM_SCHEMA,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from . import PLATFORMS
from .const import DOMAIN
from .program import Instance, async_get_manager
from .rules import defaults_json, parse_defaults

CONF_DEFAULTS = "defaults"


def _defaults(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise vol.Invalid("defaults: a mapping of domain: {attribute: value}")
    result = {}
    for domain, values in value.items():
        if domain not in ("light", "climate", "cover"):
            raise vol.Invalid(f"defaults: unknown domain '{domain}'")
        result[domain] = parse_defaults(domain, values)
    return result


PLATFORM_SCHEMA = CALENDAR_PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default="program"): cv.string,
        vol.Optional(CONF_DEFAULTS, default={}): _defaults,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    manager = async_get_manager(hass)
    await manager.async_configure(config[CONF_DEFAULTS])
    async_add_entities([ProgramCalendar(config[CONF_NAME])], True)
    platform = entity_platform.async_get_current_platform()
    for service, method in (("pause_program", "async_pause"), ("resume_program", "async_resume")):
        platform.async_register_entity_service(service, {vol.Required("uid"): cv.string}, method)
    platform.async_register_entity_service(
        "set_program_defaults", {vol.Required("defaults"): dict}, "async_set_defaults"
    )


def _parse_event(event: dict[str, Any]) -> Event:
    """An ical event from Home Assistant's event fields (as local_calendar does)."""
    event = dict(event)
    if rrule := event.get(EVENT_RRULE):
        event[EVENT_RRULE] = Recur.from_rrule(rrule)
    # Stored as floating local times, so recurrences follow the local timezone.
    for key in (EVENT_START, EVENT_END):
        value = event.get(key)
        if isinstance(value, datetime) and value.tzinfo is not None:
            event[key] = dt_util.as_local(value).replace(tzinfo=None)
    try:
        return Event(**event)
    except CalendarParseError as err:
        raise vol.Invalid(f"Error parsing event input fields: {err}") from err


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(value)


def _calendar_event(instance: Instance) -> CalendarEvent:
    """An occurrence for Home Assistant, with its real (sun-relative) times."""
    event = instance.event
    assert event is not None
    start: datetime | date
    end: datetime | date
    if isinstance(event.start, datetime) or (
        instance.program is not None
        and (instance.program.start is not None or instance.program.end is not None)
    ):
        start, end = _local(instance.start), _local(instance.end)
    else:
        start, end = event.start, event.end
        if end <= start:
            end = start + timedelta(days=1)
    return CalendarEvent(
        summary=event.summary or "",
        start=start,
        end=end,
        description=event.description,
        uid=event.uid,
        rrule=event.rrule.as_rrule_str() if event.rrule else None,
        recurrence_id=event.recurrence_id,
        location=event.location,
    )


def _range(recurrence_range: str | None) -> Range:
    return Range.THIS_AND_FUTURE if recurrence_range == Range.THIS_AND_FUTURE else Range.NONE


class ProgramCalendar(CalendarEntity):
    """The program entries; changes go through the manager's checks."""

    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
    )

    def __init__(self, name: str) -> None:
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_program_calendar"
        self._event: CalendarEvent | None = None

    @property
    def _manager(self):
        return async_get_manager(self.hass)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._manager.add_listener(self._entries_changed))

    def _entries_changed(self) -> None:
        self.async_schedule_update_ha_state(True)

    @property
    def event(self) -> CalendarEvent | None:
        return self._event

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "paused": self._manager.paused_now(),
            "defaults": defaults_json(self._manager.defaults),
        }

    async def async_set_defaults(self, defaults: dict[str, Any]) -> None:
        await self._manager.async_set_defaults(defaults)

    async def async_pause(self, uid: str) -> None:
        await self._manager.async_pause(uid)

    async def async_resume(self, uid: str) -> None:
        await self._manager.async_resume(uid)

    async def async_update(self) -> None:
        instance = self._manager.next_instance(dt_util.now())
        self._event = _calendar_event(instance) if instance is not None else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [_calendar_event(i) for i in self._manager.instances(start_date, end_date)]

    async def async_create_event(self, **kwargs: Any) -> None:
        event = _parse_event(kwargs)

        def add(calendar: Calendar) -> Event:
            return EventStore(calendar).add(event)

        await self._manager.async_change(add)

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        new_event = _parse_event(event)

        def edit(calendar: Calendar) -> str:
            try:
                EventStore(calendar).edit(
                    uid, new_event, recurrence_id=recurrence_id,
                    recurrence_range=_range(recurrence_range),
                )
            except EventStoreError as err:
                raise HomeAssistantError(f"Error while updating event: {err}") from err
            return uid

        await self._manager.async_change(edit)

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        def delete(calendar: Calendar) -> None:
            try:
                EventStore(calendar).delete(
                    uid, recurrence_id=recurrence_id, recurrence_range=_range(recurrence_range)
                )
            except EventStoreError as err:
                raise HomeAssistantError(f"Error while deleting event: {err}") from err

        await self._manager.async_change(delete)
