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
- An entry's `start`/`end` can be relative to the sun (`sunset-30m`): every
  occurrence gets its real times from its own day's sun (occurrence_times), and
  everything here (conflicts, starts and ends, the calendar) uses those.
- A running occurrence can be paused (`gungors.pause_program`): until it is
  resumed or ends it counts as not active, so its `set` attributes return to
  their defaults as at an end, and resuming applies it again as at a start.
- A default can be a template (the covers' built-in one follows the sun:
  open while it is up); whenever its result changes, the entities whose default
  it is take the new value (unless a set rule holds that attribute).
- The defaults come from the YAML (`defaults:` of the calendar platform) over
  the built-in ones; `gungors.reload` picks up a change. Defaults stored by
  gungors before 1.9.0 (from the program card) are dropped once, with a warning
  if they differed from the YAML ones.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    TrackTemplate,
    async_track_point_in_utc_time,
    async_track_template_result,
)
from homeassistant.helpers.template import Template
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .rules import (
    DOMAINS,
    SUN_TEMPLATE,
    Program,
    SunTime,
    defaults_json,
    dump_program,
    is_template,
    parse_program,
    rules_conflict,
)

if TYPE_CHECKING:
    from .features.program import Program as ProgramFeature

_LOGGER = logging.getLogger(__name__)

DATA_MANAGER = "program"
STORAGE_KEY = f"{DOMAIN}.program"
STORAGE_VERSION = 1
# Entry format: 2 = rules carry the set/let flag; 3 = Home Assistant's attribute names.
FORMAT = 3
CHECK_HORIZON = datetime.timedelta(days=366)
# Refresh at least this often (an entry far in the future, a missed timer).
MAX_SLEEP = datetime.timedelta(hours=1)
# How far an occurrence's sun-relative time may lie from the entry's own time
# (the time an entry is stored with is only a guess for sun-relative entries).
SUN_MARGIN = datetime.timedelta(days=1)

DEFAULT_DEFAULTS: dict[str, dict[str, Any]] = {
    "light": {"on": False, "color": (255, 255, 255)},
    "climate": {"hvac_mode": "off"},
    "cover": {"position": SUN_TEMPLATE},
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
    event: Event | None = field(default=None, compare=False, repr=False)

    def overlaps(self, other: Instance) -> bool:
        return self.start < other.end and other.start < self.end


def _as_datetime(value: datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.get_default_time_zone())
        return value
    return datetime.datetime.combine(value, datetime.time(), dt_util.get_default_time_zone())


def sun_moment(hass: HomeAssistant, moment: SunTime, day: datetime.date) -> datetime.datetime | None:
    """`moment` on `day` (house time), None if the sun doesn't rise or set that day."""
    location, elevation = get_astral_location(hass)
    try:
        at = getattr(location, moment.event)(day, local=True, observer_elevation=elevation)
    except ValueError:  # polar day or night
        return None
    return dt_util.as_local(at) + moment.offset


def occurrence_times(
    hass: HomeAssistant, program: Program | None, start: datetime.datetime, end: datetime.datetime
) -> tuple[datetime.datetime, datetime.datetime] | None:
    """An occurrence's real start and end: its own times, or relative to the sun.

    A sun-relative start is taken on the occurrence's start day; a sun-relative
    end on its end day, or the first day after the start where it falls after
    it (`sunset` -> `sunrise` runs through the night). None when that day's
    start does not come before the end: that occurrence is left out.
    """
    if program is None or (program.start is None and program.end is None):
        return start, end
    day = dt_util.as_local(start).date()
    if program.start is not None:
        sun_start = sun_moment(hass, program.start, day)
        if sun_start is None:
            return None
        start = sun_start
    if program.end is not None:
        end_day = max(dt_util.as_local(end).date(), day)
        for days in range(3):
            sun_end = sun_moment(hass, program.end, end_day + datetime.timedelta(days=days))
            if sun_end is not None and sun_end > start:
                return start, sun_end
        return None
    return (start, end) if end > start else None


def parse_instance(event: Event) -> tuple[Program | None, str | None]:
    try:
        return parse_program(event.description), None
    except vol.Invalid as err:
        return None, str(err)


def instance_of(event: Event, hass: HomeAssistant | None = None) -> Instance | None:
    """One occurrence with its real times (None: it doesn't happen, see occurrence_times)."""
    start = _as_datetime(event.start)
    end = _as_datetime(event.end)
    if end <= start:
        end = start + datetime.timedelta(minutes=30)
    program, error = parse_instance(event)
    if hass is not None:
        times = occurrence_times(hass, program, start, end)
        if times is None:
            return None
        start, end = times
    return Instance(
        uid=event.uid,
        recurrence_id=event.recurrence_id,
        summary=event.summary or "",
        start=start,
        end=end,
        program=program,
        error=error,
        event=event,
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
        # Paused occurrences: "<uid>|<start isoformat>".
        self.paused: set[str] = set()
        self._yaml_defaults: dict[str, dict[str, Any]] = {}
        # Template defaults being tracked: text -> remove callback.
        self._templates: dict[str, CALLBACK_TYPE] = {}

    # --- setup ---------------------------------------------------------------

    async def async_configure(self, defaults: dict[str, dict[str, Any]]) -> None:
        """Called by the calendar platform: load the entries and start."""
        self._yaml_defaults = defaults
        self._apply_defaults()
        if not self._loaded:
            data = await self._store.async_load()
            if data and data.get("ics"):
                self.calendar = IcsCalendarStream.calendar_from_ics(data["ics"])
            if data:
                self.paused = set(data.get("paused", []))
            self._loaded = True
            save = False
            if data and "defaults" in data:
                # Set from the program card before 1.9.0; the YAML is the only source now.
                if data["defaults"] != defaults_json(self.defaults):
                    _LOGGER.warning(
                        "program defaults set from the card are dropped, the YAML ones apply"
                        " (put these in the calendar's `defaults:` to keep them): %s",
                        data["defaults"],
                    )
                save = True
            if data and data.get("format", 1) < FORMAT:
                self._migrate(data.get("format", 1))
                save = True
            if save:
                await self._async_save()
        self.configured = True
        await self.async_refresh(force=True)

    @callback
    def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for remove in self._templates.values():
            remove()
        self._templates.clear()
        self._cancel_timer()
        self.configured = False

    @callback
    def register(self, entity: ProgramFeature) -> CALLBACK_TYPE:
        self.entities[entity.entity_id] = entity
        self._assigned.pop(entity.entity_id, None)
        if self.configured:
            self._track_templates()
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
        """The occurrences overlapping [start, end), with their real (sun-relative) times."""
        timeline = (calendar or self.calendar).timeline_tz(dt_util.get_default_time_zone())
        result = []
        for event in timeline.overlapping(start - SUN_MARGIN, end + SUN_MARGIN):
            instance = instance_of(event, self.hass)
            if instance is not None and instance.start < end and start < instance.end:
                result.append(instance)
        return result

    def next_instance(self, now: datetime.datetime) -> Instance | None:
        """The running occurrence, or else the next one (the calendar entity's state)."""
        best: Instance | None = None
        timeline = self.calendar.timeline_tz(dt_util.get_default_time_zone())
        for event in timeline.active_after(now - SUN_MARGIN):
            if best is not None and _as_datetime(event.start) > best.start + 2 * SUN_MARGIN:
                break
            instance = instance_of(event, self.hass)
            if instance is None or instance.end <= now:
                continue
            if best is None or (instance.start, instance.end) < (best.start, best.end):
                best = instance
        return best

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
        await self._async_save()
        self._changed()
        await self.async_refresh()

    async def _async_save(self) -> None:
        data: dict[str, Any] = {
            "ics": IcsCalendarStream.calendar_to_ics(self.calendar),
            "paused": sorted(self.paused),
            "format": FORMAT,
        }
        await self._store.async_save(data)

    # --- defaults ------------------------------------------------------------

    def _apply_defaults(self) -> None:
        """The YAML defaults over the built-in ones, per domain."""
        self.defaults = {
            domain: {**DEFAULT_DEFAULTS.get(domain, {}), **self._yaml_defaults.get(domain, {})}
            for domain in DEFAULT_DEFAULTS
        }
        self._track_templates()

    @callback
    def _track_templates(self) -> None:
        """Follow every template default (the domains' and the entities' own)."""
        texts: set[str] = set()
        for domain, values in self.defaults.items():
            texts |= {v for v in values.values() if is_template(v)}
        for entity_id, entity in self.entities.items():
            own = entity.gw_program_defaults(self.defaults.get(entity_id.split(".")[0], {}))
            texts |= {v for v in own.values() if is_template(v)}
        for text in set(self._templates) - texts:
            self._templates.pop(text)()
        for text in texts - set(self._templates):
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(Template(text, self.hass), None)],
                self._template_action(text),
            )
            self._templates[text] = info.async_remove

    def _template_action(self, text: str) -> Callable[..., None]:
        @callback
        def action(_event: HAEvent | None, _updates: Any) -> None:
            self._template_changed(text)

        return action

    @callback
    def _template_changed(self, text: str) -> None:
        """A template default has a new result: the entities it is the default of take it."""
        for entity_id, entity in list(self.entities.items()):
            domain_defaults = self.defaults.get(entity_id.split(".")[0], {})
            values = entity.gw_program_defaults(domain_defaults)
            attrs = [a for a, v in values.items() if v == text]
            if attrs:
                self.hass.async_create_task(
                    entity.gw_program_apply_defaults(attrs, domain_defaults)
                )

    def _migrate(self, old: int) -> None:
        """Rewrite the entries in the current format.

        Format 2: rules are `let` unless marked `set`; earlier entries all become `set`.
        Format 3: Home Assistant's attribute names (the description is written again).
        """
        for event in self.calendar.events:
            try:
                program = parse_program(event.description)
            except vol.Invalid:
                continue
            if old < 2:
                program = replace(
                    program, rules={a: replace(c, set=True) for a, c in program.rules.items()}
                )
            event.description = dump_program(program)
        _LOGGER.info("program entries migrated from format %s to %s", old, FORMAT)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    # --- pause ---------------------------------------------------------------

    @staticmethod
    def _key(instance: Instance) -> str:
        return f"{instance.uid}|{instance.start.isoformat()}"

    def _running(self, uid: str) -> list[Instance]:
        now = dt_util.now()
        return [i for i in self.instances(now, now + datetime.timedelta(microseconds=1))
                if i.uid == uid]

    def paused_now(self) -> list[str]:
        """The uids of the running occurrences that are paused."""
        now = dt_util.now()
        return sorted({
            i.uid for i in self.instances(now, now + datetime.timedelta(microseconds=1))
            if self._key(i) in self.paused
        })

    async def async_pause(self, uid: str) -> None:
        """Pause the running occurrence of entry `uid` until resumed or its end."""
        running = self._running(uid)
        if not running:
            raise HomeAssistantError(f"Program {uid} is not running now")
        self.paused |= {self._key(i) for i in running}
        await self._async_save()
        self._changed()
        await self.async_refresh()

    async def async_resume(self, uid: str) -> None:
        """Apply the paused running occurrence of entry `uid` again."""
        keys = {k for k in self.paused if k.split("|", 1)[0] == uid}
        if not keys:
            return
        self.paused -= keys
        await self._async_save()
        self._changed()
        await self.async_refresh()

    def check(self, calendar: Calendar, uid: str) -> None:
        """Raise HomeAssistantError if the entry `uid` is invalid or conflicts."""
        now = dt_util.now()
        changed_any = [e for e in calendar.events if e.uid == uid]
        if not changed_any:
            return  # deleted
        # The entry's own content (also when it lies in the past).
        for event in changed_any:
            program, error = parse_instance(event)
            if program is None:
                raise HomeAssistantError(f"'{event.summary or ''}': {error}")
            self._check_targets(event.summary or "", program)
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

    def _check_targets(self, summary: str, program: Program) -> None:
        areas = ar.async_get(self.hass)
        for target in program.target:
            if "." in target:
                if target not in self.entities:
                    raise HomeAssistantError(
                        f"'{summary}': {target} is not a gungors entity with `program`"
                    )
            elif self._area_id(areas, target) is None:
                raise HomeAssistantError(f"'{summary}': unknown area '{target}'")

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
        running = self.instances(now, now + datetime.timedelta(microseconds=1))
        # A pause ends with its occurrence.
        still_paused = self.paused & {self._key(i) for i in running}
        if still_paused != self.paused:
            self.paused = still_paused
            await self._async_save()
            self._changed()
        active = sorted(
            (i for i in running if i.program is not None and self._key(i) not in self.paused),
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
