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
- A timeless program (`timeless` in the store, no time of its own) is started
  and stopped on demand (`gungors.start_program` / `stop_program`): a start adds
  a one-off entry from now for `duration` (24 h when not given) whose
  description names the program (`timeless: <uid>`); a stop, its end or the
  program's deletion removes that entry again, so the calendar holds at most
  one entry per timeless program, while it runs. While a
  run is active it pauses every running entry it conflicts with (on a shared
  entity, an attribute both can't hold) and resumes them when it ends or is
  paused itself; an entry the user pauses or resumes meanwhile is left as they
  put it. Runs skip the conflict check on saving.
- Any entry or timeless program can be pinned (`gungors.pin_program`); the
  program card lists the pinned ones first.
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
import uuid
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
# A timeless program runs this long unless a duration is given.
DEFAULT_RUN = datetime.timedelta(hours=24)

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


def _run_of_event(event: Event) -> str | None:
    """The timeless program an entry is a run of, else None."""
    if not event.description or "timeless" not in event.description:
        return None
    program, _ = parse_instance(event)
    return program.timeless if program is not None else None


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
        self._assigned: dict[str, list[tuple[str, str, str]]] = {}
        self._skipped: set[tuple[str, str | None, str]] = set()
        self._timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self._loaded = False
        # Paused occurrences: "<uid>|<start isoformat>".
        self.paused: set[str] = set()
        # Paused by a timeless run: paused key -> the run's key.
        self.paused_by: dict[str, str] = {}
        # Resumed by the user while a run conflicts: not paused again by runs.
        self.kept: set[str] = set()
        self.pinned: set[str] = set()
        # Timeless programs: uid -> {"summary", "description"}.
        self.timeless: dict[str, dict[str, str]] = {}
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
                self.paused_by = dict(data.get("paused_by", {}))
                self.kept = set(data.get("kept", []))
                self.pinned = set(data.get("pinned", []))
                self.timeless = dict(data.get("timeless", {}))
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
        self.pinned &= {e.uid for e in candidate.events} | set(self.timeless)
        await self._async_save()
        self._changed()
        await self.async_refresh()

    async def _async_save(self) -> None:
        data: dict[str, Any] = {
            "ics": IcsCalendarStream.calendar_to_ics(self.calendar),
            "paused": sorted(self.paused),
            "paused_by": dict(sorted(self.paused_by.items())),
            "kept": sorted(self.kept),
            "pinned": sorted(self.pinned),
            "timeless": self.timeless,
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

    # --- timeless programs -----------------------------------------------------

    def timeless_uid(self, ref: str) -> str:
        """The uid of a timeless program given by uid or name."""
        if ref in self.timeless:
            return ref
        name = ref.strip().casefold()
        for uid, item in self.timeless.items():
            if item["summary"].strip().casefold() == name:
                return uid
        raise HomeAssistantError(f"No timeless program '{ref}'")

    def _parse_timeless(self, summary: str, description: str) -> Program:
        if not summary.strip():
            raise HomeAssistantError("A timeless program needs a name")
        try:
            program = parse_program(description)
        except vol.Invalid as err:
            raise HomeAssistantError(f"'{summary}': {err}") from err
        if program.start is not None or program.end is not None or program.timeless:
            raise HomeAssistantError(f"'{summary}': a timeless program has no start or end")
        self._check_targets(summary, program)
        return program

    async def async_save_timeless(
        self, summary: str, description: str, uid: str | None = None
    ) -> str:
        """Add (no uid) or change a timeless program; a running run follows the change."""
        if uid is not None and uid not in self.timeless:
            raise HomeAssistantError(f"No timeless program '{uid}'")
        program = self._parse_timeless(summary, description)
        uid = uid or uuid.uuid4().hex
        others = {v["summary"].strip().casefold() for k, v in self.timeless.items() if k != uid}
        if summary.strip().casefold() in others:
            raise HomeAssistantError(f"A timeless program named '{summary}' exists already")
        self.timeless[uid] = {"summary": summary.strip(), "description": dump_program(program)}
        run = self._run_of(uid)
        if run is not None:
            text = dump_program(replace(program, timeless=uid))

            def edit(calendar: Calendar) -> str:
                calendar.events = [
                    e.model_copy(update={"summary": summary.strip(), "description": text})
                    if e.uid == run.uid else e
                    for e in calendar.events
                ]
                return run.uid

            await self.async_change(edit)
        else:
            await self._async_save()
            self._changed()
        return uid

    async def async_delete_timeless(self, uid: str) -> None:
        if uid not in self.timeless:
            raise HomeAssistantError(f"No timeless program '{uid}'")
        await self.async_stop(uid)
        del self.timeless[uid]
        self.pinned.discard(uid)
        await self._async_save()
        self._changed()

    def _run_of(self, uid: str) -> Instance | None:
        """The running (maybe paused) run of a timeless program."""
        now = dt_util.now()
        for instance in self.instances(now, now + datetime.timedelta(microseconds=1)):
            if instance.program is not None and instance.program.timeless == uid:
                return instance
        return None

    async def async_start(self, uid: str, duration: datetime.timedelta | None = None) -> None:
        """Run a timeless program from now; a running one is left as it is."""
        if uid not in self.timeless:
            raise HomeAssistantError(f"No timeless program '{uid}'")
        if self._run_of(uid) is not None:
            return
        duration = duration or DEFAULT_RUN
        if duration < datetime.timedelta(minutes=1):
            raise HomeAssistantError("The duration must be at least a minute")
        item = self.timeless[uid]
        program = self._parse_timeless(item["summary"], item["description"])
        start = dt_util.now().replace(microsecond=0)
        event = Event(
            summary=item["summary"],
            dtstart=start.replace(tzinfo=None),
            dtend=(start + duration).replace(tzinfo=None),
            description=dump_program(replace(program, timeless=uid)),
        )

        def add(calendar: Calendar) -> Event:
            # One entry per timeless program: any leftover of an earlier run goes.
            calendar.events = [e for e in calendar.events if _run_of_event(e) != uid]
            calendar.events.append(event)
            return event

        await self.async_change(add)

    async def async_stop(self, uid: str) -> None:
        """End the run of a timeless program now: its entry leaves the calendar."""
        if not any(_run_of_event(e) == uid for e in self.calendar.events):
            return

        def remove(calendar: Calendar) -> None:
            calendar.events = [e for e in calendar.events if _run_of_event(e) != uid]

        await self.async_change(remove)

    def _drop_ended_runs(self, now: datetime.datetime) -> bool:
        """Remove the entries of runs that ended (by their duration). True if any."""
        ended = {
            e.uid for e in self.calendar.events
            if _run_of_event(e) is not None and e.rrule is None and _as_datetime(e.end) <= now
        }
        if ended:
            self.calendar.events = [e for e in self.calendar.events if e.uid not in ended]
        return bool(ended)

    # --- pins and the list -------------------------------------------------------

    async def async_pin(self, uid: str, pinned: bool = True) -> None:
        if uid not in self.timeless and not any(e.uid == uid for e in self.calendar.events):
            raise HomeAssistantError(f"No program '{uid}'")
        if pinned:
            self.pinned.add(uid)
        else:
            self.pinned.discard(uid)
        await self._async_save()
        self._changed()

    def programs(self) -> list[dict[str, Any]]:
        """Every entry and timeless program with its state (the program card's list).

        state: running, paused, scheduled (its next occurrence follows), done (no
        more occurrences within a year), idle (a timeless program not running) or
        invalid (an unreadable entry). Runs of timeless programs are not listed
        on their own; their program carries them.
        """
        now = dt_util.now()
        tz = dt_util.get_default_time_zone()
        horizon = now + CHECK_HORIZON
        masters: dict[str, Event] = {}
        for event in self.calendar.events:
            if event.recurrence_id is None or event.uid not in masters:
                masters[event.uid] = event
        found: dict[str, Instance] = {}
        runs: dict[str, Instance] = {}
        wanted = {
            uid for uid, e in masters.items()
            if e.rrule is not None or _as_datetime(e.end) + SUN_MARGIN > now
        }
        timeline = self.calendar.timeline_tz(tz)
        for event in timeline.active_after(now - SUN_MARGIN):
            if _as_datetime(event.start) > horizon or not (wanted - set(found)):
                break
            if event.uid in found:
                continue
            instance = instance_of(event, self.hass)
            if instance is None or instance.end <= now:
                continue
            found[event.uid] = instance
        for instance in found.values():
            if instance.program is not None and instance.program.timeless:
                if instance.start <= now:
                    runs[instance.program.timeless] = instance

        def state(instance: Instance | None) -> str:
            if instance is None:
                return "done"
            if instance.start > now:
                return "scheduled"
            return "paused" if self._key(instance) in self.paused else "running"

        def times(instance: Instance | None) -> dict[str, Any]:
            if instance is None:
                return {"start": None, "end": None}
            return {"start": instance.start.isoformat(), "end": instance.end.isoformat()}

        def paused_by(instance: Instance | None) -> str | None:
            by = self.paused_by.get(self._key(instance)) if instance is not None else None
            return by.split("|", 1)[0] if by else None

        result: list[dict[str, Any]] = []
        for uid, event in masters.items():
            program, error = parse_instance(event)
            if program is not None and program.timeless:
                continue
            instance = found.get(uid)
            if instance is None and event.rrule is None:
                instance_done = instance_of(event, self.hass)
                when = times(instance_done)
            else:
                when = times(instance)
            result.append({
                "uid": uid,
                "summary": event.summary or "",
                "description": event.description or "",
                "rrule": event.rrule.as_rrule_str() if event.rrule else None,
                "recurrence_id": instance.recurrence_id if instance is not None else None,
                "timeless": False,
                "all_day": not isinstance(event.dtstart, datetime.datetime),
                "pinned": uid in self.pinned,
                "state": "invalid" if program is None else state(instance),
                "paused_by": paused_by(instance),
                "error": error,
                **when,
            })
        for uid, item in self.timeless.items():
            run = runs.get(uid)
            result.append({
                "uid": uid,
                "summary": item["summary"],
                "description": item["description"],
                "rrule": None,
                "recurrence_id": None,
                "timeless": True,
                "all_day": False,
                "pinned": uid in self.pinned,
                "state": state(run) if run is not None else "idle",
                "paused_by": paused_by(run),
                "run": run.uid if run is not None else None,
                "error": None,
                **times(run),
            })
        # paused_by names a run (its uid); show the timeless program's uid instead.
        run_owner = {r.uid: uid for uid, r in runs.items()}
        for item in result:
            if item["paused_by"] is not None:
                item["paused_by"] = run_owner.get(item["paused_by"], item["paused_by"])
        return result

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
        keys = {self._key(i) for i in running}
        self.paused |= keys
        for key in keys:  # the user's pause now: a run's end leaves it paused
            self.paused_by.pop(key, None)
            self.kept.discard(key)
        await self._async_save()
        self._changed()
        await self.async_refresh()

    async def async_resume(self, uid: str) -> None:
        """Apply the paused running occurrence of entry `uid` again."""
        keys = {k for k in self.paused if k.split("|", 1)[0] == uid}
        if not keys:
            return
        self.paused -= keys
        for key in keys:  # a run had paused it: the user wants it back anyway
            if self.paused_by.pop(key, None) is not None:
                self.kept.add(key)
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
        if any(p.timeless for e in changed_any if (p := parse_instance(e)[0]) is not None):
            return  # a run: conflicts pause the other entry instead (async_refresh)
        instances = self.instances(now, now + CHECK_HORIZON, calendar)
        mine = [i for i in instances if i.uid == uid]
        others = [
            i for i in instances
            if i.uid != uid and i.program is not None and i.program.timeless is None
        ]
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
        dropped = self._drop_ended_runs(now)
        if self._update_run_pauses(running) or dropped:
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
            # The rules too: an edit of a running entry applies at once.
            key = [(i.uid, i.start.isoformat(), repr(r)) for i, r in assigned]
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

    def _update_run_pauses(self, running: list[Instance]) -> bool:
        """Pause what the active timeless runs conflict with, resume what they no longer do.

        Also ends the pauses of occurrences that ended. True when anything changed.
        """
        keys = {self._key(i) for i in running}
        before = (set(self.paused), dict(self.paused_by), set(self.kept))
        # A pause ends with its occurrence.
        self.paused &= keys
        self.kept &= keys
        self.paused_by = {k: v for k, v in self.paused_by.items() if k in keys}
        by_key = {self._key(i): i for i in running if i.program is not None}

        def pausing(key: str) -> bool:
            run = by_key.get(key)
            return run is not None and run.program.timeless is not None and key not in self.paused

        # A run paused in a pass releases what it paused in the next one.
        for _ in range(5):
            if not self._run_pause_pass(by_key, pausing):
                break
        return before != (self.paused, self.paused_by, self.kept)

    def _run_pause_pass(
        self, by_key: dict[str, Instance], pausing: Callable[[str], bool]
    ) -> bool:
        changed_any = False
        # Release: the run that paused it ended or is paused itself. Repeat, as a
        # released run may in turn have paused others.
        changed = True
        while changed:
            changed = False
            for key, by in list(self.paused_by.items()):
                if not pausing(by):
                    del self.paused_by[key]
                    self.paused.discard(key)
                    changed = changed_any = True
        # Pause: the newest run wins over every other occurrence it conflicts with.
        runs = sorted(
            (i for k, i in by_key.items() if pausing(k)), key=lambda i: i.start, reverse=True
        )
        for run in runs:
            run_key = self._key(run)
            if run_key in self.paused:
                continue  # paused by a newer run in this pass
            for other in sorted(by_key.values(), key=lambda i: i.start):
                key = self._key(other)
                if key == run_key or key in self.paused or key in self.kept:
                    continue
                if other.program.timeless is not None and other.start > run.start:
                    continue  # a newer run wins
                if self._instances_conflict(run, other):
                    self.paused.add(key)
                    self.paused_by[key] = run_key
                    changed_any = True
                    _LOGGER.info("Program '%s' paused by '%s'", other.summary, run.summary)
        return changed_any

    def _instances_conflict(self, a: Instance, b: Instance) -> bool:
        assert a.program is not None and b.program is not None
        for entity_id in self.resolve(a.program) & self.resolve(b.program):
            domain = entity_id.split(".", 1)[0]
            if rules_conflict(a.program.rules, b.program.rules, domain) is not None:
                return True
        return False

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
