"""Immich albums: person albums, trip albums and faceless new photos (`gungors: immich:`).

```yaml
gungors:
  immich:
    url: https://foto.example.org
    users:                      # one Immich API key per user (faces are per user)
      sercan: !secret immich_api_sercan
      melike: !secret immich_api_melike
    owner: sercan               # owns the shared albums; the others are editors
    people: {Kaan: Kaan}        # person name in Immich -> album name
    trips:
      distance_km: 50           # farther from home (zone.home) = away
      detail: {turkey: state, turkiye: state, netherlands: city}
      names: {spain: ispanya}   # country -> name (over the built-in Turkish names)
    faceless:
      album: check_before_delete
      wait_hours: 24
```

The mode is `select.immich` (restored): `Kapalı` does nothing, `Deneme` plans
without writing (the plan is on `sensor.immich`), `Albüm` writes albums, `Albüm +
çöp` also moves faceless new photos that are not trip photos to the Immich trash.
A run every `scan_interval` (10 min) and on `gungors.immich_run`:

- People: every asset with the named person (in each user's own library) goes to
  the person's album.
- Trips: per user, a trip starts at the first photo farther than `distance_km`
  from home and ends at the next photo taken at home; days without photos in
  between do not matter. Photos without GPS between its first and last away photo
  (with a camera make: no screenshots) belong to it too. Overlapping trips of
  different users (one started before the other was home again) are one trip. Album name: the most frequent place + start year,
  ascii lowercase (`antalya_2025`, `ispanya_2025`; `_2` on a clash). The place is
  the `detail` field (state or city) for the countries listed there, else the
  country's Turkish name. An open trip's album is renamed once it closes if the
  name changed and nobody renamed it meanwhile.
- Faceless: images uploaded after the first run (never the existing library),
  `wait_hours` after upload and with the face detection queue empty, with no face
  found, not a favourite, not archived and in no album of their own -> the user's
  `faceless.album`; in `Albüm + çöp` mode those outside trips go to the trash
  (recoverable for 30 days) instead. Videos are never touched.
- An asset is added to an album once: one removed by hand is not added again.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
import datetime
import logging
import math
import re
import unicodedata
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_IMMICH = "immich"
DATA_IMMICH = "immich"
SIGNAL_UPDATE = f"{DOMAIN}_immich_update"

MODE_OFF = "Kapalı"
MODE_DRY = "Deneme"
MODE_ALBUM = "Albüm"
MODE_TRASH = "Albüm + çöp"
MODES = [MODE_OFF, MODE_DRY, MODE_ALBUM, MODE_TRASH]

PAGE_SIZE = 1000
ADD_CHUNK = 500
FULL_SCAN_EVERY = datetime.timedelta(hours=24)
INCREMENTAL_MARGIN = datetime.timedelta(hours=1)

# English country name (slug) -> Turkish album name.
COUNTRY_NAMES = {
    "albania": "arnavutluk", "austria": "avusturya", "belgium": "belcika",
    "bosnia_and_herzegovina": "bosna_hersek", "bulgaria": "bulgaristan",
    "croatia": "hirvatistan", "cyprus": "kibris", "czechia": "cekya",
    "czech_republic": "cekya", "denmark": "danimarka", "egypt": "misir",
    "finland": "finlandiya", "france": "fransa", "georgia": "gurcistan",
    "germany": "almanya", "greece": "yunanistan", "hungary": "macaristan",
    "iceland": "izlanda", "ireland": "irlanda", "italy": "italya", "japan": "japonya",
    "luxembourg": "luksemburg", "malta": "malta", "montenegro": "karadag",
    "morocco": "fas", "netherlands": "hollanda", "the_netherlands": "hollanda",
    "norway": "norvec", "poland": "polonya", "portugal": "portekiz",
    "romania": "romanya", "serbia": "sirbistan", "slovenia": "slovenya",
    "slovakia": "slovakya", "spain": "ispanya", "sweden": "isvec",
    "switzerland": "isvicre", "thailand": "tayland", "turkey": "turkiye",
    "turkiye": "turkiye", "united_arab_emirates": "bae", "united_kingdom": "ingiltere",
    "united_states": "abd", "united_states_of_america": "abd",
}
DEFAULT_DETAIL = {"turkey": "state", "turkiye": "state", "netherlands": "city",
                  "the_netherlands": "city"}

TURKISH = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                         "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"})


def slug(text: str) -> str:
    """Ascii lowercase with `_`: `Türkiye` -> `turkiye`, `Den Haag` -> `den_haag`."""
    text = unicodedata.normalize("NFKD", text.translate(TURKISH))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _slug_keys(value: Any) -> dict[str, str]:
    return {slug(str(k)): str(v) for k, v in (value or {}).items()}


IMMICH_SCHEMA = vol.Schema(
    {
        vol.Required("url"): cv.url,
        vol.Required("users"): vol.All({cv.string: cv.string}, vol.Length(min=1)),
        vol.Optional("owner"): cv.string,
        vol.Optional("scan_interval", default=600): vol.All(vol.Coerce(int), vol.Range(min=60)),
        vol.Optional("people", default={}): {cv.string: cv.string},
        vol.Optional("trips", default={}): vol.Schema(
            {
                vol.Optional("distance_km", default=50): vol.Coerce(float),
                vol.Optional("detail", default={}): vol.All(
                    {cv.string: vol.In(["city", "state"])}, _slug_keys
                ),
                vol.Optional("names", default={}): vol.All({cv.string: cv.string}, _slug_keys),
            }
        ),
        vol.Optional("faceless", default={}): vol.Schema(
            {
                vol.Optional("album", default="check_before_delete"): cv.string,
                vol.Optional("wait_hours", default=24): vol.Coerce(float),
            }
        ),
    }
)


# --- pure logic ----------------------------------------------------------------------------------


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


@dataclass(frozen=True)
class Point:
    """One asset as the trip logic needs it."""

    id: str
    user: str
    time: datetime.datetime  # UTC capture time
    year: int  # local capture year
    lat: float | None = None
    lon: float | None = None
    country: str | None = None
    state: str | None = None
    city: str | None = None
    camera: bool = False  # has a camera make (screenshots have none)


@dataclass
class Trip:
    """A trip: from the first away photo to the last one before coming home."""

    start: datetime.datetime
    end: datetime.datetime  # last away photo
    closed: bool
    year: int
    until: datetime.datetime | None = None  # first photo at home again (None: still away)
    assets: dict[str, set[str]] = field(default_factory=dict)  # user -> asset ids
    places: Counter = field(default_factory=Counter)

    def all_assets(self) -> set[str]:
        return set().union(*self.assets.values()) if self.assets else set()


def place_of(point: Point, detail: dict[str, str], names: dict[str, str]) -> str | None:
    """`state`/`city` for the countries in `detail`, else the country's (Turkish) name."""
    if not point.country:
        return None
    country = slug(point.country)
    field_name = detail.get(country)
    if field_name:
        value = getattr(point, field_name) or point.city or point.state
        if value:
            return slug(value)
    return slug(names.get(country) or COUNTRY_NAMES.get(country) or country)


def find_trips(
    points: Iterable[Point],
    home: tuple[float, float],
    away_km: float,
    detail: dict[str, str],
    names: dict[str, str],
) -> list[Trip]:
    """Trips per user, then overlapping trips of different users merged."""
    by_user: dict[str, list[Point]] = {}
    for point in points:
        by_user.setdefault(point.user, []).append(point)

    trips: list[Trip] = []
    for user, user_points in by_user.items():
        user_points.sort(key=lambda p: p.time)
        user_trips: list[Trip] = []
        current: Trip | None = None
        for point in user_points:
            if point.lat is None or point.lon is None:
                continue
            away = distance_km(home[0], home[1], point.lat, point.lon) > away_km
            if away:
                if current is None:
                    current = Trip(point.time, point.time, False, point.year)
                    user_trips.append(current)
                current.end = point.time
                current.assets.setdefault(user, set()).add(point.id)
                place = place_of(point, detail, names)
                if place:
                    current.places[place] += 1
            elif current is not None:
                current.closed = True
                current.until = point.time
                current = None
        # Photos without GPS taken during a trip (camera photos only).
        for point in user_points:
            if point.lat is not None or not point.camera:
                continue
            for trip in user_trips:
                if trip.start <= point.time <= trip.end:
                    trip.assets[user].add(point.id)
                    break
        trips.extend(user_trips)

    trips.sort(key=lambda t: t.start)
    merged: list[Trip] = []
    for trip in trips:
        last = merged[-1] if merged else None
        # Overlap until the user was home again: one of them may take no photos for days.
        if last is not None and (last.until is None or trip.start <= last.until):
            last.end = max(last.end, trip.end)
            last.closed = last.closed and trip.closed
            last.until = (
                None if last.until is None or trip.until is None else max(last.until, trip.until)
            )
            for user, ids in trip.assets.items():
                last.assets.setdefault(user, set()).update(ids)
            last.places.update(trip.places)
        else:
            merged.append(trip)
    return merged


def trip_base_name(trip: Trip) -> str:
    """`<most frequent place>_<start year>` (`yurtdisi` when no place is known)."""
    place = trip.places.most_common(1)[0][0] if trip.places else "gezi"
    return f"{place}_{trip.year}"


def unique_name(base: str, taken: set[str]) -> str:
    """`base`, or `base_2`, `base_3`, ... when taken."""
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _parse_time(value: str | None) -> datetime.datetime | None:
    return dt_util.parse_datetime(value) if value else None


def point_from_asset(user: str, asset: dict[str, Any]) -> Point | None:
    """Asset JSON (with exifInfo) -> Point; None without a capture time."""
    time = _parse_time(asset.get("fileCreatedAt"))
    if time is None:
        return None
    local = _parse_time(asset.get("localDateTime")) or time
    exif = asset.get("exifInfo") or {}
    lat, lon = exif.get("latitude"), exif.get("longitude")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        lat = lon = None
    return Point(
        id=asset["id"],
        user=user,
        time=dt_util.as_utc(time),
        year=local.year,
        lat=lat,
        lon=lon,
        country=exif.get("country"),
        state=exif.get("state"),
        city=exif.get("city"),
        camera=bool(exif.get("make")),
    )


# --- Immich API ----------------------------------------------------------------------------------


class ImmichError(Exception):
    """An Immich API call failed."""


class ImmichApi:
    """Minimal async Immich client for one user's API key."""

    def __init__(self, session: aiohttp.ClientSession, url: str, key: str) -> None:
        self._session = session
        self._base = url.rstrip("/") + "/api"
        self._headers = {"x-api-key": key, "Accept": "application/json"}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method,
                self._base + path,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=120),
                **kwargs,
            ) as resp:
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    raise ImmichError(f"{method} {path}: {resp.status} {text}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except aiohttp.ClientError as err:
            raise ImmichError(f"{method} {path}: {err}") from err

    async def me(self) -> dict[str, Any]:
        return await self.request("GET", "/users/me")

    async def search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """All pages of a metadata search."""
        items: list[dict[str, Any]] = []
        body = {"size": PAGE_SIZE, **body}
        while True:
            result = (await self.request("POST", "/search/metadata", json=body))["assets"]
            items.extend(result.get("items") or [])
            if result.get("nextCursor"):
                body = {**body, "cursor": result["nextCursor"]}
                body.pop("page", None)
            elif result.get("nextPage"):
                body = {**body, "page": int(result["nextPage"])}
            else:
                return items

    async def people(self) -> list[dict[str, Any]]:
        people: list[dict[str, Any]] = []
        page = 1
        while True:
            result = await self.request(
                "GET", "/people", params={"withHidden": "true", "size": 1000, "page": page}
            )
            people.extend(result.get("people") or [])
            if not result.get("hasNextPage"):
                return people
            page += 1

    async def albums(self, **params: Any) -> list[dict[str, Any]]:
        return await self.request(
            "GET", "/albums", params={k: str(v).lower() if isinstance(v, bool) else v
                                      for k, v in params.items()}
        )

    async def album(self, album_id: str) -> dict[str, Any] | None:
        try:
            return await self.request(
                "GET", f"/albums/{album_id}", params={"withoutAssets": "true"}
            )
        except ImmichError as err:
            if ": 404" in str(err) or ": 400" in str(err):
                return None
            raise

    async def create_album(self, name: str, user_ids: list[str]) -> dict[str, Any]:
        body: dict[str, Any] = {"albumName": name}
        if user_ids:
            body["albumUsers"] = [{"userId": uid, "role": "editor"} for uid in user_ids]
        return await self.request("POST", "/albums", json=body)

    async def rename_album(self, album_id: str, name: str) -> None:
        await self.request("PATCH", f"/albums/{album_id}", json={"albumName": name})

    async def add_to_album(self, album_id: str, ids: list[str]) -> set[str]:
        """Add assets; returns the ids now in the album (added or already there)."""
        done: set[str] = set()
        for i in range(0, len(ids), ADD_CHUNK):
            result = await self.request(
                "PUT", f"/albums/{album_id}/assets", json={"ids": ids[i : i + ADD_CHUNK]}
            )
            for row in result or []:
                if row.get("success") or row.get("error") == "duplicate":
                    done.add(row["id"])
        return done

    async def faces(self, asset_id: str) -> list[dict[str, Any]]:
        return await self.request("GET", "/faces", params={"id": asset_id})

    async def trash(self, ids: list[str]) -> None:
        await self.request("DELETE", "/assets", json={"ids": ids, "force": False})

    async def face_queue_busy(self) -> bool | None:
        """True while face detection has jobs; None when the queues are not readable."""
        try:
            queues = await self.request("GET", "/queues")
        except ImmichError:
            return None
        for queue in queues or []:
            if queue.get("name") == "faceDetection":
                stats = queue.get("statistics") or {}
                return any(stats.get(k) for k in ("active", "waiting", "delayed", "paused"))
        return None


# --- manager -------------------------------------------------------------------------------------


@callback
def async_get_manager(hass: HomeAssistant) -> ImmichManager | None:
    return hass.data.get(DOMAIN, {}).get(DATA_IMMICH)


class ImmichManager:
    """Runs the album rules; state in `.storage/gungors.immich`."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self.mode = MODE_DRY
        self.status: str = "başlamadı"
        self.summary: dict[str, Any] = {}
        self._store: Store = Store(hass, 1, f"{DOMAIN}.immich")
        self._state: dict[str, Any] = {}
        self._apis: dict[str, ImmichApi] = {}
        self._user_ids: dict[str, str] = {}
        self._index: dict[str, dict[str, Point]] = {}
        self._last_full: datetime.datetime | None = None
        self._last_scan: datetime.datetime | None = None
        self._unsub = None
        self._running = False

    # lifecycle

    async def async_start(self) -> None:
        self._state = await self._store.async_load() or {}
        for key in ("albums", "added", "faceless"):
            self._state.setdefault(key, {})
        self._state.setdefault("trips", [])
        self._apply_config()

    def _apply_config(self) -> None:
        session = async_get_clientsession(self.hass)
        self._apis = {
            user: ImmichApi(session, self.config["url"], key)
            for user, key in self.config["users"].items()
        }
        self._user_ids = {}
        self._index = {}
        self._last_full = None
        if self._unsub:
            self._unsub()
        self._unsub = async_track_time_interval(
            self.hass,
            self._async_tick,
            datetime.timedelta(seconds=self.config["scan_interval"]),
            name="gungors immich",
        )

    async def async_reconfigure(self, config: dict[str, Any]) -> None:
        self.config = config
        self._apply_config()

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now: datetime.datetime) -> None:
        await self.async_run()

    async def async_set_mode(self, mode: str, run: bool = True) -> None:
        self.mode = mode
        self._notify()
        if run:
            self.hass.async_create_task(self.async_run(), "gungors immich run")

    @property
    def owner(self) -> str:
        return self.config.get("owner") or next(iter(self.config["users"]))

    @property
    def writing(self) -> bool:
        return self.mode in (MODE_ALBUM, MODE_TRASH)

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # one run

    async def async_run(self) -> None:
        if self.mode == MODE_OFF or self._running:
            return
        self._running = True
        now = dt_util.utcnow()
        summary: dict[str, Any] = {"mode": self.mode}
        try:
            if "since" not in self._state:
                # Faceless photos are only looked at from the first run on.
                self._state["since"] = now.isoformat()
                await self._store.async_save(self._state)
            await self._ensure_users()
            await self._refresh_index(now)
            summary["people"] = await self._run_people()
            trips, summary["trips"] = await self._run_trips()
            summary["faceless"] = await self._run_faceless(now, trips)
            if self.writing:
                await self._store.async_save(self._state)
            self.status = "tamam"
        except ImmichError as err:
            _LOGGER.warning("Immich run failed: %s", err)
            self.status = "hata"
            summary["error"] = str(err)
        finally:
            self._running = False
        summary["last_run"] = now.isoformat()
        self.summary = summary
        self._notify()

    async def _ensure_users(self) -> None:
        for user, api in self._apis.items():
            if user not in self._user_ids:
                self._user_ids[user] = (await api.me())["id"]

    async def _refresh_index(self, now: datetime.datetime) -> None:
        """All assets with EXIF per user: full daily, else changed since the last scan."""
        full = self._last_full is None or now - self._last_full > FULL_SCAN_EVERY
        for user, api in self._apis.items():
            body: dict[str, Any] = {"withExif": True}
            if not full and self._last_scan is not None:
                body["updatedAfter"] = (self._last_scan - INCREMENTAL_MARGIN).isoformat()
            assets = await api.search(body)
            index = {} if full else self._index.setdefault(user, {})
            for asset in assets:
                if asset.get("isTrashed"):
                    index.pop(asset["id"], None)
                    continue
                point = point_from_asset(user, asset)
                if point is not None:
                    index[point.id] = point
            self._index[user] = index
        if full:
            self._last_full = now
        self._last_scan = now

    # albums

    def _added(self, album_id: str) -> set[str]:
        return set(self._state["added"].get(album_id, []))

    async def _album(self, key: str, name: str, user: str, shared: bool) -> str | None:
        """The managed album for `key` (created in write mode); None in dry run."""
        api = self._apis[user]
        album_id = self._state["albums"].get(key)
        if album_id and await api.album(album_id) is not None:
            return album_id
        album_id = None
        for album in await api.albums(name=name):
            if album.get("albumName") == name and (
                (album.get("albumUsers") or [{}])[0].get("user", {}).get("id")
                in (None, self._user_ids[user])
            ):
                album_id = album["id"]
                break
        if album_id is None:
            if not self.writing:
                return None
            others = [uid for u, uid in self._user_ids.items() if u != user] if shared else []
            album_id = (await api.create_album(name, others))["id"]
        self._state["albums"][key] = album_id
        return album_id

    async def _add(self, album_id: str | None, user: str, ids: set[str]) -> int:
        """Add the not yet added `ids` (owned by `user`); returns how many are new."""
        new = sorted(ids - self._added(album_id)) if album_id else sorted(ids)
        if not new or not self.writing or album_id is None:
            return len(new)
        done = await self._apis[user].add_to_album(album_id, new)
        self._state["added"][album_id] = sorted(self._added(album_id) | done)
        return len(new)

    async def _run_people(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for person, album_name in self.config["people"].items():
            album_id = await self._album(f"person:{person}", album_name, self.owner, True)
            new = 0
            for user, api in self._apis.items():
                ids = [
                    p["id"]
                    for p in await api.people()
                    if (p.get("name") or "").casefold() == person.casefold()
                ]
                assets: set[str] = set()
                for person_id in ids:
                    assets |= {a["id"] for a in await api.search({"personIds": [person_id]})}
                new += await self._add(album_id, user, assets)
            result[album_name] = new
        return result

    async def _run_trips(self) -> tuple[list[Trip], list[str]]:
        conf = self.config["trips"]
        detail = {**DEFAULT_DETAIL, **conf["detail"]}
        points = [p for index in self._index.values() for p in index.values()]
        trips = find_trips(
            points,
            (self.hass.config.latitude, self.hass.config.longitude),
            conf["distance_km"],
            detail,
            conf["names"],
        )
        stored: list[dict[str, Any]] = self._state["trips"]
        taken = {t["name"] for t in stored}
        lines: list[str] = []
        for trip in trips:
            record = next(
                (
                    t
                    for t in stored
                    if _parse_time(t["start"]) <= trip.end and trip.start <= _parse_time(t["end"])
                ),
                None,
            )
            base = trip_base_name(trip)
            if record is None:
                name = unique_name(base, taken)
                taken.add(name)
                record = {"name": name, "base": base, "album": None}
                if self.writing:
                    stored.append(record)
            elif trip.closed and not record.get("closed") and record.get("base") != base:
                await self._rename_trip(record, unique_name(base, taken - {record["name"]}), base)
            record["start"] = min(trip.start, _parse_time(record.get("start")) or trip.start).isoformat()
            record["end"] = max(trip.end, _parse_time(record.get("end")) or trip.end).isoformat()
            record["closed"] = trip.closed
            album_id = record.get("album")
            if album_id is None and self.writing:
                album_id = await self._album(f"trip:{record['name']}", record["name"], self.owner, True)
                record["album"] = album_id
            new = 0
            for user, ids in trip.assets.items():
                new += await self._add(album_id, user, ids)
            lines.append(
                f"{record['name']}: {trip.start.date()}..{trip.end.date()}, "
                f"{len(trip.all_assets())} foto/video, +{new}"
                + ("" if trip.closed else ", sürüyor")
            )
        return trips, lines

    async def _rename_trip(self, record: dict[str, Any], name: str, base: str) -> None:
        """Rename a trip album that nobody renamed by hand."""
        album_id = record.get("album")
        if album_id and self.writing:
            album = await self._apis[self.owner].album(album_id)
            if album is None or album.get("albumName") != record["name"]:
                record["base"] = base  # renamed by hand (or gone): keep it
                return
            await self._apis[self.owner].rename_album(album_id, name)
        record["name"], record["base"] = name, base

    # faceless

    async def _run_faceless(self, now: datetime.datetime, trips: list[Trip]) -> dict[str, int]:
        conf = self.config["faceless"]
        decided: dict[str, str] = self._state["faceless"]
        managed = set(self._state["albums"].values())
        in_trip = set().union(*(t.all_assets() for t in trips)) if trips else set()
        busy = await self._apis[self.owner].face_queue_busy()
        counts = Counter()
        for user, api in self._apis.items():
            album_ids = {
                aid for key, aid in self._state["albums"].items() if key == f"faceless:{user}"
            }
            to_album: set[str] = set()
            to_trash: list[str] = []
            assets = await api.search({"type": "IMAGE", "createdAfter": self._state["since"]})
            for asset in assets:
                aid = asset["id"]
                if aid in decided:
                    continue
                uploaded = _parse_time(asset.get("createdAt"))
                if (
                    uploaded is None
                    or now - uploaded < datetime.timedelta(hours=conf["wait_hours"])
                    or busy
                ):
                    counts["bekliyor"] += 1
                    continue
                if (
                    asset.get("isFavorite")
                    or asset.get("isArchived")
                    or asset.get("visibility", "timeline") != "timeline"
                ):
                    self._decide(aid, "skip")
                    continue
                if await api.faces(aid):
                    self._decide(aid, "face")
                    continue
                albums = {a["id"] for a in await api.albums(assetId=aid)}
                if albums - managed - album_ids:
                    self._decide(aid, "skip")
                    continue
                if self.mode == MODE_TRASH and aid not in in_trip:
                    to_trash.append(aid)
                else:
                    to_album.add(aid)
            if to_album:
                album_id = await self._album(f"faceless:{user}", conf["album"], user, False)
                await self._add(album_id, user, to_album)
                for aid in to_album:
                    self._decide(aid, "album")
            if to_trash:
                await api.trash(to_trash)
                for aid in to_trash:
                    self._decide(aid, "trash")
            counts["albüme"] += len(to_album)
            counts["çöpe"] += len(to_trash)
        return {k: v for k, v in counts.items() if v}

    def _decide(self, asset_id: str, decision: str) -> None:
        if self.writing:
            self._state["faceless"][asset_id] = decision
