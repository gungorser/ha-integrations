"""The program manager: entries of `calendar.program` -> rules on gungors entities.

One manager per Home Assistant instance (``hass.data[DOMAIN]["program"]``). It owns
the calendar (an iCalendar kept in ``.storage/gungors.program``), validates every
change to it, and hands each entity with the `program` feature the programs that
are active for it now (features/program.py enforces them).

- An entry's description is its program (rules.py). A change that leaves an
  entry unreadable, names an unknown target, or makes two entries conflict on an
  entity while they overlap in time is refused with an error; only conflicts
  that involve the changed entry are checked, up to a year ahead.
- Targets are resolved again at every refresh (each start and end of an entry),
  so an entity added to an area later is covered too. A conflict that only shows
  up then (an area changed after the entries were saved) keeps the program that
  started first; the later one is skipped for that entity and a notification
  says so.
- At sunrise and sunset the covers take their `position` default.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any

from ical.calendar import Calendar
from ical.calendar_stream import IcsCalendarStream
from ical.event import Event
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.core import CALLBACK_TYPE, Event as HAEvent, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .rules import DOMAINS, Program, parse_program, rules_conflict

if TYPE_CHECKING:
    from .features.program import Program as ProgramFeature

_LOGGER = logging.getLogger(__name__)

DATA_MANAGER = "program"
STORAGE_KEY = f"{DOMAIN}.program"
STORAGE_VERSION = 1
CHECK_HORIZON = datetime.timedelta(days=366)
# Refresh at least this often (an entry far in the future, a missed timer).
MAX_SLEEP = datetime.timedelta(hours=1)

DEFAULT_DEFAULTS: dict[str, dict[str, Any]] = {
    "light": {"on": False, "color": (255, 255, 255)},
    "climate": {"hvac_mode": "off"},
    "cover": {"position": "sun"},
}


@dataclass(frozen=True)
class Instance:
    """One occurrence of an entry."""

    uid: str
    recurrence_id: str | None
    summary: str
    start: datetime.datetime
    end: datetime.datetime
    program: Program | None
    error: str | None = None

    def overlaps(self, other: Instance) -> bool:
        return self.start < other.end and other.start < self.end


def _as_datetime(value: datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.get_default_time_zone())
        return value
    return datetime.datetime.combine(value, datetime.time(), dt_util.get_default_time_zone())


def instance_of(event: Event) -> Instance:
    start = _as_datetime(event.start)
    end = _as_datetime(event.end)
    if end <= start:
        end = start + datetime.timedelta(minutes=30)
    try:
        program: Program | None = parse_program(event.description)
        error = None
    except vol.Invalid as err:
        program, error = None, str(err)
    return Instance(
        uid=event.uid,
        recurrence_id=event.recurrence_id,
        summary=event.summary or "",
        start=start,
        end=end,
        program=program,
        error=error,
    )


def copy_calendar(calendar: Calendar) -> Calendar:
    return IcsCalendarStream.calendar_from_ics(IcsCalendarStream.calendar_to_ics(calendar))


@callback
def async_get_manager(hass: HomeAssistant) -> ProgramManager:
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_MANAGER not in data:
        data[DATA_MANAGER] = ProgramManager(hass)
    return data[DATA_MANAGER]


class ProgramManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calendar = Calendar()
        self.defaults: dict[str, dict[str, Any]] = {k: dict(v) for k, v in DEFAULT_DEFAULTS.items()}
        self.configured = False
        self.entities: dict[str, ProgramFeature] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._assigned: dict[str, list[tuple[str, str]]] = {}
        self._skipped: set[tuple[str, str | None, str]] = set()
        self._timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self._loaded = False

    # --- setup ---------------------------------------------------------------

    async def async_configure(self, defaults: dict[str, dict[str, Any]]) -> None:
        """Called by the calendar platform: load the entries and start."""
        self.defaults = {
            domain: {**DEFAULT_DEFAULTS.get(domain, {}), **defaults.get(domain, {})}
            for domain in DEFAULT_DEFAULTS
        }
        if not self._loaded:
            data = await self._store.async_load()
            if data and data.get("ics"):
                self.calendar = IcsCalendarStream.calendar_from_ics(data["ics"])
            self._loaded = True
        if not self._unsubs:
            self._unsubs.append(
                async_track_state_change_event(self.hass, ["sun.sun"], self._sun_changed)
            )
        self.configured = True
        await self.async_refresh(force=True)

    @callback
    def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_timer()
        self.configured = False

    @callback
    def register(self, entity: ProgramFeature) -> CALLBACK_TYPE:
        self.entities[entity.entity_id] = entity
        self._assigned.pop(entity.entity_id, None)
        if self.configured:
            self.hass.async_create_task(self.async_refresh())

        @callback
        def unregister() -> None:
            if self.entities.get(entity.entity_id) is entity:
                del self.entities[entity.entity_id]
                self._assigned.pop(entity.entity_id, None)

        return unregister

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Called after every change of the entries (the calendar entity)."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    # --- entries -------------------------------------------------------------

    def instances(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        calendar: Calendar | None = None,
    ) -> list[Instance]:
        timeline = (calendar or self.calendar).timeline_tz(dt_util.get_default_time_zone())
        return [instance_of(event) for event in timeline.overlapping(start, end)]

    async def async_change(self, change: Callable[[Calendar], Event | str | None]) -> None:
        """Apply a change to a copy of the calendar, check it, then keep it.

        `change` edits the copy and returns the uid of the entry it touched.
        """
        candidate = copy_calendar(self.calendar)
        try:
            touched = change(candidate)
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001  (ical raises its own errors)
            raise HomeAssistantError(f"Program entry not saved: {err}") from err
        uid = touched.uid if isinstance(touched, Event) else touched
        if uid is not None:
            self.check(candidate, uid)
        self.calendar = candidate
        await self._store.async_save(
            {"ics": IcsCalendarStream.calendar_to_ics(self.calendar)}
        )
        for listener in list(self._listeners):
            listener()
        await self.async_refresh()

    def check(self, calendar: Calendar, uid: str) -> None:
        """Raise HomeAssistantError if the entry `uid` is invalid or conflicts."""
        now = dt_util.now()
        changed_any = [e for e in calendar.events if e.uid == uid]
        if not changed_any:
            return  # deleted
        # The entry's own content (also when it lies in the past).
        for event in changed_any:
            instance = instance_of(event)
            if instance.program is None:
                raise HomeAssistantError(f"'{instance.summary}': {instance.error}")
            self._check_targets(instance)
        instances = self.instances(now, now + CHECK_HORIZON, calendar)
        mine = [i for i in instances if i.uid == uid]
        others = [i for i in instances if i.uid != uid and i.program is not None]
        for instance in mine:
            assert instance.program is not None
            entities = self.resolve(instance.program)
            for other in others:
                if not instance.overlaps(other):
                    continue
                assert other.program is not None
                shared = entities & self.resolve(other.program)
                for entity_id in sorted(shared):
                    domain = entity_id.split(".", 1)[0]
                    attr = rules_conflict(instance.program.rules, other.program.rules, domain)
                    if attr is not None:
                        when = dt_util.as_local(max(instance.start, other.start))
                        raise HomeAssistantError(
                            f"'{instance.summary}' conflicts with '{other.summary}' on "
                            f"{entity_id} ({attr}) at {when:%Y-%m-%d %H:%M}"
                        )

    def _check_targets(self, instance: Instance) -> None:
        assert instance.program is not None
        areas = ar.async_get(self.hass)
        for target in instance.program.target:
            if "." in target:
                if target not in self.entities:
                    raise HomeAssistantError(
                        f"'{instance.summary}': {target} is not a gungors entity with `program`"
                    )
            elif self._area_id(areas, target) is None:
                raise HomeAssistantError(f"'{instance.summary}': unknown area '{target}'")

    # --- targets -------------------------------------------------------------

    @staticmethod
    def _area_id(areas: ar.AreaRegistry, name: str) -> str | None:
        area = areas.async_get_area(name) or areas.async_get_area_by_name(name)
        return area.id if area is not None else None

    def resolve(self, program: Program) -> set[str]:
        """Program entities the program targets, limited to its rules' domains."""
        areas = ar.async_get(self.hass)
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        domains = program.domains()
        wanted_areas = {a for t in program.target if "." not in t
                        if (a := self._area_id(areas, t)) is not None}
        result: set[str] = set()
        for entity_id in self.entities:
            if entity_id.split(".", 1)[0] not in domains:
                continue
            if entity_id in program.target:
                result.add(entity_id)
                continue
            if not wanted_areas:
                continue
            entry = entities.async_get(entity_id)
            area_id = entry.area_id if entry is not None else None
            if area_id is None and entry is not None and entry.device_id:
                device = devices.async_get(entry.device_id)
                area_id = device.area_id if device is not None else None
            if area_id in wanted_areas:
                result.add(entity_id)
        return result

    # --- runtime -------------------------------------------------------------

    async def async_refresh(self, force: bool = False) -> None:
        """Hand every entity its active programs; plan the next refresh."""
        if not self.configured:
            return
        self._cancel_timer()
        now = dt_util.now()
        active = sorted(
            (i for i in self.instances(now, now + datetime.timedelta(microseconds=1))
             if i.program is not None),
            key=lambda i: (i.start, i.uid),
        )
        per_entity: dict[str, list[tuple[Instance, dict]]] = {e: [] for e in self.entities}
        for instance in active:
            assert instance.program is not None
            for entity_id in self.resolve(instance.program):
                domain = entity_id.split(".", 1)[0]
                rules = {a: c for a, c in instance.program.rules.items() if DOMAINS[a] == domain}
                accepted = per_entity[entity_id]
                clash = next(
                    (p for p, r in accepted if rules_conflict(rules, r, domain) is not None), None
                )
                if clash is not None:
                    self._notify_skip(instance, clash, entity_id)
                    continue
                accepted.append((instance, rules))

        for entity_id, entity in list(self.entities.items()):
            assigned = per_entity.get(entity_id, [])
            key = [(i.uid, i.start.isoformat()) for i, _ in assigned]
            if not force and self._assigned.get(entity_id) == key:
                continue
            self._assigned[entity_id] = key
            programs = [
                {
                    "uid": i.uid,
                    "summary": i.summary,
                    "start": i.start.isoformat(),
                    "end": i.end.isoformat(),
                    "rules": rules,
                }
                for i, rules in assigned
            ]
            try:
                await entity.gw_program_set(programs, self.defaults.get(entity_id.split(".")[0], {}))
            except Exception:  # noqa: BLE001  (one entity must not stop the others)
                _LOGGER.exception("%s: applying programs failed", entity_id)

        self._schedule(now)

    def _schedule(self, now: datetime.datetime) -> None:
        next_at = now + MAX_SLEEP
        for instance in self.instances(now, next_at):
            for moment in (instance.start, instance.end):
                if now < moment < next_at:
                    next_at = moment
        self._timer = async_track_point_in_utc_time(
            self.hass, self._timer_fired, dt_util.as_utc(next_at) + datetime.timedelta(seconds=1)
        )

    @callback
    def _timer_fired(self, _now: datetime.datetime) -> None:
        self._timer = None
        self.hass.async_create_task(self.async_refresh())

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    def _notify_skip(self, instance: Instance, winner: Instance, entity_id: str) -> None:
        key = (instance.uid, instance.recurrence_id, entity_id)
        if key in self._skipped:
            return
        self._skipped.add(key)
        _LOGGER.warning(
            "Program '%s' skipped for %s: conflicts with '%s'",
            instance.summary, entity_id, winner.summary,
        )
        persistent_notification.async_create(
            self.hass,
            f"'{instance.summary}' is not applied to {entity_id}: it conflicts with "
            f"'{winner.summary}', which started first.",
            title="Program conflict",
            notification_id=f"{DOMAIN}_program_{instance.uid}_{entity_id}",
        )

    @callback
    def _sun_changed(self, event: HAEvent) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or old.state == new.state:
            return
        if new.state not in ("above_horizon", "below_horizon"):
            return
        defaults = self.defaults.get("cover", {})
        if "position" not in defaults:
            return
        for entity_id, entity in list(self.entities.items()):
            if entity_id.startswith("cover."):
                self.hass.async_create_task(
                    entity.gw_program_apply_defaults(["position"], defaults)
                )
