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
      min_days: {netherlands: 2}  # shorter trips mostly in that country are no trips
    faceless:
      wait_hours: 24
      home: keep                # keep | trash | delete (outside trips)
      trip: album               # album | keep | trash | delete (trip photos)
    videos:                     # faces on several video frames (video_faces.py)
      ml_url: http://immich-machine-learning:3003
      per_run: 3                # videos per run
      match: 0.5                # similarity -> person album
      maybe: 0                  # similarity -> <album>_aday (0: off)
    partou:                     # daycare photos -> Immich (partou.py)
      username: !secret partou_email
      password: !secret partou_password
      albums: {Kaan: kaan_partou}  # Partou first name -> album prefix (default: <name>_partou)
      after: "18:00"            # once a day from this local time (and on gungors.immich_run)
```

Partou: new items of each child's Partou album are uploaded to the owner's library, once,
into monthly albums `kaan_partou_mart_2026` (capture month). This writes in every mode but
`Kapalı` (also in `Deneme`); the uploads stay out of the person, trip and faceless rules.

The mode is `select.immich` (restored): `Kapalı` does nothing, `Deneme` plans
without writing (the plan is on `sensor.immich`), `Albüm` writes (a restored
`Albüm + çöp` from before 1.21.0 reads as `Albüm`). What happens to faceless photos
is `faceless: home:` (keep, the default; trash; delete) and `faceless: trip:` (album, the
default: into the trip album like any trip photo; keep; trash; delete) in Configure > Immich.
A run every `scan_interval` seconds (600; 0 = no timer) and on `button.immich_sync`
or `gungors.immich_run`:

- People: every asset with the named person (in each user's own library) goes to
  the person's album.
- Trips: per user, a trip starts at the first photo farther than `distance_km`
  from home and ends at the next photo taken at home; days without photos in
  between do not matter, up to `max_gap_days` (10) without photos. Photos without
  GPS between its first and last away photo (with a camera make: no screenshots)
  belong to it too. Trips (of any user) less than `max_gap_days` apart are one trip.
  A trip with fewer than `min_assets` (3) photos is no trip. Album name: every
  place in visit order + start year, ascii lowercase (`ankara_samsun_2024`,
  `ispanya_fransa_2023`; `_2` on a clash). The place is the `detail` field (state
  or city) for the countries listed there, else the country's Turkish name; a trip
  in more than one country (`home_country` not counted) is named by its countries.
  A place or country with less than `name_min_share` (10 %) of the photos or
  a country with fewer than `name_min_photos` (2) photos (a stopover) is left out of the name.
  An open trip's album is renamed once it closes if the
  name changed and nobody renamed it meanwhile.
- Faceless: images uploaded after the first run (never the existing library),
  `wait_hours` after upload and with the face detection queue empty, with no face
  found, not a favourite, not archived and in no album of their own: `home` (outside
  trips) or `trip` (a trip photo) decides: `album` (trips only) -> stays in the trip
  album, `keep` -> left as it is (a trip photo stays out of the trip album), `trash` ->
  the Immich trash (recoverable for 30 days), `delete` -> deleted for good. Unless
  `trip` is `album`, a new image joins its trip album only once it has a face (or is
  skipped). Videos are never touched. (Before 1.25.0 faceless photos went to a
  `check_before_delete` album; that album is left as it is.)
- `share_albums: true`: every album a user owns (also ones made by hand or by the
  phone app) is shared with the other users as editors; the old faceless albums stay
  private.
- Videos (`videos: ml_url:`): faces on several frames of each video, scored against reference
  faces of each person through Immich's machine-learning server (`video_faces.py`); a few
  videos per run.
- An asset is added to an album once: one removed by hand is not added again.
- `on_upload: true`: an Immich workflow `gungors` per user (trigger AssetMetadataExtraction,
  step webhook) posts to an HA webhook; gungors runs once the uploads stop and the Immich
  queues up to face recognition are empty (workflows fire before faces are known).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
import datetime
import logging
import asyncio
import math
import os
import re
import shutil
import tempfile
import unicodedata
from typing import Any

import aiohttp
import voluptuous as vol

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .partou import DEFAULT_URL as PARTOU_URL, PartouApi, PartouError, exif_time, file_name, item_day
from .video_faces import MlApi, MlError, best_score, extract_frame, face_box, ffmpeg_command, frame_times, parse_duration, pick_face

_LOGGER = logging.getLogger(__name__)

CONF_IMMICH = "immich"
DATA_IMMICH = "immich"
SIGNAL_UPDATE = f"{DOMAIN}_immich_update"

MODE_OFF = "Kapalı"
MODE_DRY = "Deneme"
MODE_ALBUM = "Albüm"
MODE_TRASH = "Albüm + çöp"  # before 1.21.0; restored as MODE_ALBUM (faceless actions decide now)
MODES = [MODE_OFF, MODE_DRY, MODE_ALBUM]
# What happens to a faceless new photo, at home and on a trip (Configure > Immich).
FACELESS_ALBUM = "album"    # trips only: into the trip album like any trip photo (default)
FACELESS_KEEP = "keep"      # left as it is, out of any album (the default at home)
FACELESS_TRASH = "trash"    # Immich trash (recoverable for 30 days)
FACELESS_DELETE = "delete"  # deleted for good
FACELESS_ACTIONS = [FACELESS_ALBUM, FACELESS_KEEP, FACELESS_TRASH, FACELESS_DELETE]
OLD_FACELESS_ALBUM = "check_before_delete"  # the faceless album before 1.25.0 (stays private)
FACELESS_HOME_ACTIONS = [FACELESS_KEEP, FACELESS_TRASH, FACELESS_DELETE]
# Run on upload (`on_upload`): an Immich workflow per user posts to an HA webhook.
WORKFLOW_NAME = "gungors"
WORKFLOW_TRIGGER = "AssetMetadataExtraction"  # after EXIF (GPS for trips), before faces
WORKFLOW_STEP = "immich-plugin-core#webhook"
UPLOAD_DEBOUNCE = 120  # s after the last webhook: a bulk upload is one run
QUEUE_POLL = 60  # s between queue checks
QUEUE_WAIT_MAX = datetime.timedelta(minutes=30)  # then run anyway; the timer catches the rest
QUEUE_UNREADABLE_WAIT = 600  # s to wait instead when the queues are not readable
# Upload -> EXIF -> thumbnails -> face detection -> face recognition: all empty = faces known.
PIPELINE_QUEUES = ("metadataExtraction", "thumbnailGeneration", "faceDetection", "facialRecognition")
# Faceless decisions that let a trip photo into its trip album.
TRIP_ALBUM_DECISIONS = {"face", "skip", FACELESS_ALBUM}


def _home_action(value: Any) -> str:
    """`album` at home (before 1.25.0: the faceless album) is `keep` now."""
    return FACELESS_KEEP if value == FACELESS_ALBUM else value
MONTHS = ["ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul",
          "ekim", "kasim", "aralik"]

PAGE_SIZE = 1000
VIDEO_MATCH = 0.5        # cosine similarity a video face needs to count as the person
REFS_PER_RUN = 12        # reference faces made per run (one model call each)
VIDEO_TRIES = 3          # a video that fails this often is left out
ML_PAUSE = 1.0           # seconds between model calls: the ML server shares one CPU
ADD_CHUNK = 500
FULL_SCAN_EVERY = datetime.timedelta(hours=24)
INCREMENTAL_MARGIN = datetime.timedelta(hours=1)

# English country name (slug) -> Turkish album name.
COUNTRY_NAMES = {
    "albania": "arnavutluk", "austria": "avusturya", "belgium": "belcika",
    "bosnia_and_herzegovina": "bosna_hersek", "bulgaria": "bulgaristan",
    "croatia": "hirvatistan", "cyprus": "kibris", "czechia": "cekya", "denmark": "danimarka", "egypt": "misir",
    "finland": "finlandiya", "france": "fransa", "georgia": "gurcistan",
    "germany": "almanya", "greece": "yunanistan", "hungary": "macaristan",
    "iceland": "izlanda", "ireland": "irlanda", "italy": "italya", "japan": "japonya",
    "luxembourg": "luksemburg", "malta": "malta", "montenegro": "karadag",
    "morocco": "fas", "netherlands": "hollanda",
    "norway": "norvec", "poland": "polonya", "portugal": "portekiz",
    "romania": "romanya", "serbia": "sirbistan", "slovenia": "slovenya",
    "slovakia": "slovakya", "spain": "ispanya", "sweden": "isvec",
    "switzerland": "isvicre", "thailand": "tayland", "turkey": "turkiye", "united_arab_emirates": "bae", "united_kingdom": "ingiltere",
    "united_states": "abd",
}
DEFAULT_DETAIL = {"turkey": "state", "netherlands": "city"}

TURKISH = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                         "ç": "c", "Ç": "c", "ö": "o", "Ö": "o", "ü": "u", "Ü": "u"})


def slug(text: str) -> str:
    """Ascii lowercase with `_`: `Türkiye` -> `turkiye`, `Den Haag` -> `den_haag`."""
    text = unicodedata.normalize("NFKD", text.translate(TURKISH))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


COUNTRY_ALIASES = {"turkiye": "turkey", "republic_of_turkiye": "turkey",
                   "czech_republic": "czechia", "united_states_of_america": "united_states"}


def country_key(name: str) -> str:
    """One key per country: `The Netherlands` -> `netherlands`, `Türkiye` -> `turkey`."""
    key = slug(name).removeprefix("the_")
    return COUNTRY_ALIASES.get(key, key)


def _slug_keys(value: Any) -> dict[str, str]:
    return {country_key(str(k)): str(v) for k, v in (value or {}).items()}


IMMICH_SCHEMA = vol.Schema(
    {
        vol.Required("url"): cv.url,
        vol.Required("users"): vol.All({cv.string: cv.string}, vol.Length(min=1)),
        vol.Optional("owner"): cv.string,
        vol.Optional("scan_interval", default=600): vol.All(
            vol.Coerce(int), vol.Any(0, vol.Range(min=60))
        ),
        vol.Optional("people", default={}): {cv.string: cv.string},
        vol.Optional("share_albums", default=False): cv.boolean,
        vol.Optional("on_upload", default=False): cv.boolean,
        vol.Optional("trips", default={}): vol.Schema(
            {
                vol.Optional("distance_km", default=50): vol.Coerce(float),
                vol.Optional("max_gap_days", default=10): vol.Coerce(float),
                vol.Optional("name_min_share", default=0.1): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
                vol.Optional("name_min_photos", default=2): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional("min_assets", default=3): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional("home_country", default="netherlands"): vol.All(cv.string, country_key),
                vol.Optional("detail", default={}): vol.All(
                    {cv.string: vol.In(["city", "state"])}, _slug_keys
                ),
                vol.Optional("names", default={}): vol.All({cv.string: cv.string}, _slug_keys),
                vol.Optional("min_days", default={}): vol.All(
                    {cv.string: vol.All(vol.Coerce(int), vol.Range(min=1))},
                    lambda v: {country_key(str(k)): n for k, n in v.items()},
                ),
            }
        ),
        vol.Optional("partou"): vol.Schema(
            {
                vol.Optional("url", default=PARTOU_URL): cv.url,
                vol.Required("username"): cv.string,
                vol.Required("password"): cv.string,
                vol.Optional("albums", default={}): {cv.string: cv.string},
                vol.Optional("after", default="18:00"): cv.time,
            }
        ),
        vol.Optional("videos", default={}): vol.Schema(
            {
                vol.Optional("ml_url"): cv.url,  # empty: no video face scan
                vol.Optional("per_run", default=3): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional("frames", default=10): vol.All(vol.Coerce(int), vol.Range(min=1)),
                vol.Optional("every", default=3.0): vol.All(vol.Coerce(float), vol.Range(min=0.5)),
                vol.Optional("match", default=VIDEO_MATCH): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
                vol.Optional("maybe", default=0.0): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1)
                ),
                vol.Optional("ref_per_month", default=3): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional("ref_days", default=120): vol.Coerce(float),
                vol.Optional("max_mb", default=500): vol.Coerce(float),
            }
        ),
        vol.Optional("faceless", default={}): vol.Schema(
            {
                vol.Optional("album"): cv.string,  # before 1.25.0; unused
                vol.Optional("home", default=FACELESS_KEEP): vol.All(
                    _home_action, vol.In(FACELESS_HOME_ACTIONS)
                ),
                vol.Optional("trip", default=FACELESS_ALBUM): vol.In(FACELESS_ACTIONS),
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
    uploaded: datetime.datetime | None = None  # upload time (faceless rule)
    image: bool = True


@dataclass
class Trip:
    """A trip: from the first away photo to the last one before coming home."""

    start: datetime.datetime
    end: datetime.datetime  # last away photo
    closed: bool
    year: int
    assets: dict[str, set[str]] = field(default_factory=dict)  # user -> asset ids
    stops: list[tuple[datetime.datetime, str, str | None]] = field(default_factory=list)
    countries: Counter = field(default_factory=Counter)
    home: set[str] = field(default_factory=set)  # users whose part ended at home

    @property
    def days(self) -> int:
        """Calendar days from the first to the last away photo."""
        return (self.end.date() - self.start.date()).days + 1

    def all_assets(self) -> set[str]:
        return set().union(*self.assets.values()) if self.assets else set()


def place_of(point: Point, detail: dict[str, str], names: dict[str, str]) -> str | None:
    """`state`/`city` for the countries in `detail`, else the country's (Turkish) name."""
    if not point.country:
        return None
    country = country_key(point.country)
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
    min_days: dict[str, int] | None = None,
    max_gap_days: float = 10,
    min_assets: int = 1,
) -> list[Trip]:
    """Trips per user, then trips less than `max_gap_days` apart merged.

    A user's trip also ends (before a photo at home) when the next away photo comes
    more than `max_gap_days` later: few photos at home carry GPS, so two trips
    months apart would join otherwise.

    `min_days` {country slug: days}: a trip mostly in that country that is shorter
    is not a trip (day trips inside the home country). A trip with fewer than
    `min_assets` assets is not a trip either.
    """

    def apart(a: datetime.datetime, b: datetime.datetime) -> bool:
        """More than `max_gap_days` calendar days from `a` to `b`."""
        return (b.date() - a.date()).days > max_gap_days
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
            if away and current is not None:
                if apart(current.end, point.time):
                    current.closed = True
                    current = None
            if away:
                if current is None:
                    current = Trip(point.time, point.time, False, point.year)
                    user_trips.append(current)
                current.end = point.time
                current.assets.setdefault(user, set()).add(point.id)
                country = country_key(point.country) if point.country else None
                place = place_of(point, detail, names)
                if place:
                    current.stops.append((point.time, place, country))
                if country:
                    current.countries[country] += 1
            elif current is not None:
                current.closed = True
                current.home.add(user)
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
        # Less than `max_gap_days` apart: the same trip, unless a user of this trip
        # came home in between (a day apart at most always merges).
        if last is not None and (
            trip.start <= last.end + datetime.timedelta(days=1)
            or (not apart(last.end, trip.start) and not last.home & set(trip.assets))
        ):
            last.end = max(last.end, trip.end)
            last.closed = last.closed and trip.closed
            for user, ids in trip.assets.items():
                last.assets.setdefault(user, set()).update(ids)
            last.home |= trip.home
            last.stops = sorted(last.stops + trip.stops, key=lambda s: s[0])
            last.countries.update(trip.countries)
        else:
            merged.append(trip)
    min_days = min_days or {}
    return [
        trip
        for trip in merged
        if len(trip.all_assets()) >= min_assets
        and (
            not trip.countries
            or trip.days >= min_days.get(trip.countries.most_common(1)[0][0], 1)
        )
    ]


def trip_base_name(
    trip: Trip,
    names: dict[str, str] | None = None,
    home_country: str | None = None,
    min_share: float = 0.0,
    min_photos: int = 1,
) -> str:
    """Every place in visit order + start year (`ankara_samsun_2024`).

    A trip in more than one country (the home country not counted) is named by
    its countries instead (`ispanya_fransa_almanya_2023`); `gezi` when no place
    is known. A place or country with less than `min_share` of the trip's photos
    (a stopover) is left out of the name, and so is a country with fewer than
    `min_photos` photos; every city of the trip's one country stays.
    """
    names = names or {}

    def kept(keys: list[str], least: int = 1) -> list[str]:
        counts = Counter(keys)
        order = list(dict.fromkeys(keys))
        big = [k for k in order if counts[k] >= max(least, min_share * len(keys))]
        return big or [counts.most_common(1)[0][0]] if order else []

    countries = kept([c for _, _, c in trip.stops if c and c != home_country], min_photos)
    if len(countries) > 1:
        parts = [slug(names.get(c) or COUNTRY_NAMES.get(c) or c) for c in countries]
    else:
        parts = kept(
            [p for _, p, c in trip.stops if not countries or c is None or c == countries[0]]
        )
    return f"{'_'.join(parts) or 'gezi'}_{trip.year}"


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
        uploaded=_parse_time(asset.get("createdAt")),
        image=asset.get("type", "IMAGE") == "IMAGE",
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

    async def share_album(self, album_id: str, user_ids: list[str]) -> None:
        await self.request(
            "PUT",
            f"/albums/{album_id}/users",
            json={"albumUsers": [{"userId": uid, "role": "editor"} for uid in user_ids]},
        )

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

    async def upload(self, data: bytes, name: str, taken: datetime.datetime) -> str:
        """Upload one file (Immich 3 API); returns the asset id, also for a duplicate."""
        form = aiohttp.FormData()
        form.add_field("fileCreatedAt", taken.isoformat())
        form.add_field("fileModifiedAt", taken.isoformat())
        form.add_field("filename", name)
        form.add_field("assetData", data, filename=name,
                       content_type="application/octet-stream")
        result = await self.request("POST", "/assets", data=form)
        return result["id"]

    async def faces(self, asset_id: str) -> list[dict[str, Any]]:
        return await self.request("GET", "/faces", params={"id": asset_id})

    async def thumbnail(self, asset_id: str) -> bytes:
        """The preview image (JPEG) of an asset."""
        try:
            async with self._session.get(
                f"{self._base}/assets/{asset_id}/thumbnail",
                params={"size": "preview"},
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status >= 400:
                    raise ImmichError(f"thumbnail {asset_id}: {resp.status}")
                return await resp.read()
        except aiohttp.ClientError as err:
            raise ImmichError(f"thumbnail {asset_id}: {err}") from err

    async def download(self, asset_id: str, path: str, max_bytes: int, hass: HomeAssistant) -> bool:
        """The original file to `path`; False when it is larger than `max_bytes`."""
        try:
            async with self._session.get(
                f"{self._base}/assets/{asset_id}/original",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=900),
            ) as resp:
                if resp.status >= 400:
                    raise ImmichError(f"original {asset_id}: {resp.status}")
                if (resp.content_length or 0) > max_bytes:
                    return False
                handle = await hass.async_add_executor_job(open, path, "wb")
                size = 0
                try:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        size += len(chunk)
                        if size > max_bytes:
                            return False
                        await hass.async_add_executor_job(handle.write, chunk)
                finally:
                    await hass.async_add_executor_job(handle.close)
                return True
        except aiohttp.ClientError as err:
            raise ImmichError(f"original {asset_id}: {err}") from err

    async def trash(self, ids: list[str], force: bool = False) -> None:
        """To the trash; `force` deletes for good."""
        await self.request("DELETE", "/assets", json={"ids": ids, "force": force})

    async def face_queue_busy(self) -> bool | None:
        """True while face detection has jobs; None when the queues are not readable."""
        return await self.queues_busy(("faceDetection",))

    async def queues_busy(self, names: Iterable[str]) -> bool | None:
        """True while any of these queues has jobs; None when none of them is readable."""
        try:
            queues = await self.request("GET", "/queues")
        except ImmichError:
            return None
        wanted = set(names)
        found = False
        for queue in queues or []:
            if queue.get("name") in wanted:
                found = True
                stats = queue.get("statistics") or {}
                if any(stats.get(k) for k in ("active", "waiting", "delayed", "paused")):
                    return True
        return False if found else None

    async def workflows(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/workflows") or []

    async def save_workflow(self, workflow_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
        """Create the workflow, or update it when `workflow_id` is given."""
        if workflow_id:
            return await self.request("PUT", f"/workflows/{workflow_id}", json=body)
        return await self.request("POST", "/workflows", json=body)


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
        self._accounts: dict[str, str | None] = {}
        self._index: dict[str, dict[str, Point]] = {}
        self._last_full: datetime.datetime | None = None
        self._last_scan: datetime.datetime | None = None
        self._unsub = None
        self._running = False
        self._partou_writing = False  # Partou writes in every mode but `Kapalı`
        # run on upload: webhook -> debounce -> wait for the Immich queues -> run
        self._webhook_id: str | None = None
        self._debounce = None
        self._waiter: asyncio.Task | None = None
        self._uploads = 0  # webhook calls since the last run
        self._upload: dict[str, Any] = {}  # on `sensor.immich` as `upload`
        # person -> user -> (Immich person ids, assets with that person); from the people run
        self._people_found: dict[str, dict[str, tuple[list[str], list[dict[str, Any]]]]] = {}

    # lifecycle

    async def async_start(self) -> None:
        self._state = await self._store.async_load() or {}
        for key in ("albums", "added", "faceless", "partou", "video_refs", "videos"):
            self._state.setdefault(key, {})
        self._state.setdefault("trips", [])
        self._apply_config()

    def _apply_config(self) -> None:
        session = async_get_clientsession(self.hass)
        self._stop_upload()
        self._apis = {
            user: ImmichApi(session, self.config["url"], key)
            for user, key in self.config["users"].items()
        }
        self._user_ids = {}
        self._accounts = {}
        self._index = {}
        self._last_full = None
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._upload = {}
        if self.config.get("on_upload") or self._state.get("workflows"):
            self.hass.async_create_task(self._async_setup_upload(), "gungors immich workflows")
        if not self.config["scan_interval"]:
            return
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
        self._stop_upload()

    # run on upload

    @callback
    def _stop_upload(self) -> None:
        if self._webhook_id:
            webhook.async_unregister(self.hass, self._webhook_id)
            self._webhook_id = None
        if self._debounce:
            self._debounce()
            self._debounce = None
        if self._waiter and not self._waiter.done():
            self._waiter.cancel()
        self._waiter = None

    async def _async_setup_upload(self) -> None:
        """Webhook + one `gungors` workflow per user (on), or the workflows disabled (off)."""
        on = bool(self.config.get("on_upload"))
        url: str | None = None
        if on:
            webhook_id = self._state.get("webhook_id")
            if not webhook_id:
                webhook_id = self._state["webhook_id"] = webhook.async_generate_id()
                await self._store.async_save(self._state)
            if self._webhook_id != webhook_id:
                webhook.async_register(
                    self.hass, DOMAIN, "gungors immich", webhook_id, self._async_webhook,
                    local_only=True, allowed_methods=["POST", "PUT"],
                )
                self._webhook_id = webhook_id
            try:
                url = webhook.async_generate_url(
                    self.hass, webhook_id, allow_external=False, prefer_external=False
                )
            except NoURLAvailableError:
                self._upload = {"durum": "hata", "hata": "HA'nın iç adresi bulunamadı"}
                self.summary = {**self.summary, "upload": dict(self._upload)}
                self._notify()
                return
        result: dict[str, str] = {}
        workflows: dict[str, str] = self._state.setdefault("workflows", {})
        for user, api in self._apis.items():
            try:
                result[user] = await self._save_workflow(api, on, url)
                if on:
                    workflows[user] = result[user]
            except ImmichError as err:
                text = str(err)
                if " 403 " in text:
                    text = "API anahtarında workflow izni yok (workflow.read/create/update)"
                result[user] = f"hata: {text}"
                _LOGGER.warning("Immich workflow for %s: %s", user, err)
        if not on:
            self._state.pop("workflows", None)
        await self._store.async_save(self._state)
        self._upload = {
            "durum": "açık" if on else "kapalı",
            "workflow": result,
            **({"adres": url.split("/api/webhook/")[0]} if url else {}),
        }
        self.summary = {**self.summary, "upload": dict(self._upload)}
        self._notify()

    @staticmethod
    async def _save_workflow(api: ImmichApi, on: bool, url: str | None) -> str:
        existing = next(
            (w for w in await api.workflows() if w.get("name") == WORKFLOW_NAME), None
        )
        if not on:
            if existing and existing.get("enabled"):
                await api.save_workflow(existing["id"], {"enabled": False})
                return "kapatıldı"
            return "yok" if existing is None else "kapalı"
        body = {
            "trigger": WORKFLOW_TRIGGER,
            "name": WORKFLOW_NAME,
            "description": "Home Assistant gungors: yeni fotoğraflarda albümleri güncelle",
            "enabled": True,
            "steps": [{"method": WORKFLOW_STEP, "config": {"url": url, "method": "POST"}}],
        }
        await api.save_workflow(existing["id"] if existing else None, body)
        return "güncellendi" if existing else "kuruldu"

    async def _async_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> None:
        """An upload: run once the uploads stop and Immich has found the faces."""
        self._uploads += 1
        self._upload["son_yükleme"] = dt_util.utcnow().isoformat()
        if self._debounce:
            self._debounce()
        self._debounce = async_call_later(hass, UPLOAD_DEBOUNCE, self._upload_settled)

    @callback
    def _upload_settled(self, _now: Any) -> None:
        self._debounce = None
        if self._waiter and not self._waiter.done():
            return  # already waiting; it runs for these uploads too
        self._waiter = self.hass.async_create_background_task(
            self._async_wait_and_run(), "gungors immich upload run"
        )

    async def _async_wait_and_run(self) -> None:
        """Wait until the Immich queues are empty twice in a row (faces known), then run."""
        api = self._apis.get(self.owner)
        start = dt_util.utcnow()
        idle = 0
        while api is not None and dt_util.utcnow() - start < QUEUE_WAIT_MAX:
            busy = await api.queues_busy(PIPELINE_QUEUES)
            if busy is None:
                await asyncio.sleep(QUEUE_UNREADABLE_WAIT)
                break
            idle = 0 if busy else idle + 1
            if idle >= 2:
                break
            await asyncio.sleep(QUEUE_POLL)
        while self._running:
            await asyncio.sleep(QUEUE_POLL)
        await self.async_run()

    async def _async_tick(self, _now: datetime.datetime) -> None:
        await self.async_run()

    async def async_set_mode(self, mode: str, run: bool = True) -> None:
        self.mode = MODE_ALBUM if mode == MODE_TRASH else mode
        self._notify()
        if run:
            self.hass.async_create_task(self.async_run(), "gungors immich run")

    @property
    def owner(self) -> str:
        return self.config.get("owner") or next(iter(self.config["users"]))

    @property
    def writing(self) -> bool:
        return self._partou_writing or self.mode == MODE_ALBUM

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # one run

    async def async_run(self, force: bool = False) -> None:
        """One run; `force` (gungors.immich_run) also checks Partou outside its daily time."""
        if self.mode == MODE_OFF or self._running:
            return
        self._running = True
        now = dt_util.utcnow()
        summary: dict[str, Any] = {"mode": self.mode}
        uploads, self._uploads = self._uploads, 0
        try:
            if "since" not in self._state:
                # Faceless photos are only looked at from the first run on.
                self._state["since"] = now.isoformat()
                await self._store.async_save(self._state)
            await self._ensure_users()
            if self.config.get("partou"):
                summary["partou"] = await self._run_partou(now, force)
            await self._refresh_index(now)
            # Which Immich account each key is, and how many of its own assets it sees.
            summary["users"] = {
                user: {"hesap": self._accounts.get(user), "foto": len(self._index.get(user, {}))}
                for user in self._apis
            }
            summary["people"], summary["people_users"] = await self._run_people()
            trips, summary["trips"] = await self._run_trips()
            summary["faceless"] = await self._run_faceless(now, trips)
            if self.config["videos"].get("ml_url"):
                summary["videos"] = await self._run_videos()
            if self.config["share_albums"]:
                summary["shared"] = await self._run_share()
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
        if self._upload:
            summary["upload"] = {**self._upload, "yeni": uploads}
        self.summary = summary
        self._notify()

    async def _ensure_users(self) -> None:
        for user, api in self._apis.items():
            if user not in self._user_ids:
                me = await api.me()
                self._user_ids[user] = me["id"]
                self._accounts[user] = me.get("email") or me.get("name")

    async def _refresh_index(self, now: datetime.datetime) -> None:
        """All assets with EXIF per user: full daily, else changed since the last scan."""
        full = self._last_full is None or now - self._last_full > FULL_SCAN_EVERY
        # Daycare photos carry the daycare's GPS: they would end a trip of a parent away.
        partou = self._partou_ids()
        for user, api in self._apis.items():
            body: dict[str, Any] = {"withExif": True}
            if not full and self._last_scan is not None:
                body["updatedAfter"] = (self._last_scan - INCREMENTAL_MARGIN).isoformat()
            assets = self._own(user, await api.search(body))
            index = {} if full else self._index.setdefault(user, {})
            for asset in assets:
                if asset.get("isTrashed"):
                    index.pop(asset["id"], None)
                    continue
                point = point_from_asset(user, asset)
                if point is not None and point.id not in partou:
                    index[point.id] = point
            self._index[user] = index
        if full:
            self._last_full = now
        self._last_scan = now

    def _own(self, user: str, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only the user's own assets: a search also returns a partner's shared ones."""
        me = self._user_ids[user]
        return [a for a in assets if a.get("ownerId") in (None, me)]

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

    async def _run_people(self) -> tuple[dict[str, int], dict[str, dict[str, str]]]:
        """New assets per person album, and per user what was found (for the sensor)."""
        result: dict[str, int] = {}
        found_by: dict[str, dict[str, str]] = {}
        for person, album_name in self.config["people"].items():
            album_id = await self._album(f"person:{person}", album_name, self.owner, True)
            new = 0
            found_by[album_name] = {}
            self._people_found[person] = {}
            for user, api in self._apis.items():
                # People are per Immich account; names match without case or outer spaces.
                ids = [
                    p["id"]
                    for p in await api.people()
                    if (p.get("name") or "").strip().casefold() == person.strip().casefold()
                ]
                assets: set[str] = set()
                found_assets: list[dict[str, Any]] = []
                for person_id in ids:
                    found = self._own(user, await api.search({"personIds": [person_id]}))
                    found_assets.extend(a for a in found if a["id"] not in assets)
                    assets |= {a["id"] for a in found}
                self._people_found[person][user] = (ids, found_assets)
                assets -= self._partou_ids()
                new += await self._add(album_id, user, assets)
                found_by[album_name][user] = (
                    f"{len(ids)} kişi, {len(assets)} foto" if ids else "kişi yok"
                )
            result[album_name] = new
        return result, found_by

    def _held_from_trips(self) -> set[str]:
        """New images that may not join a trip album (yet): faceless `trip` is not `album`.

        Undecided ones wait for the faceless rule; `keep`, `trash` and `delete` never join.
        """
        if self.config["faceless"]["trip"] == FACELESS_ALBUM:
            return set()
        since = _parse_time(self._state.get("since"))
        if since is None:
            return set()
        decided: dict[str, str] = self._state["faceless"]
        return {
            point.id
            for index in self._index.values()
            for point in index.values()
            if point.image
            and point.uploaded is not None
            and point.uploaded > since
            and decided.get(point.id) not in TRIP_ALBUM_DECISIONS
        }

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
            conf["min_days"],
            conf["max_gap_days"],
            conf["min_assets"],
        )
        held = self._held_from_trips()
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
            base = trip_base_name(
                trip,
                conf["names"],
                conf["home_country"],
                conf["name_min_share"],
                conf["name_min_photos"],
            )
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
                new += await self._add(album_id, user, ids - held)
            lines.append(
                f"{record['name']}: {trip.start.date()}..{trip.end.date()}, "
                f"{len(trip.all_assets())} foto/video ("
                + ", ".join(f"{u} {len(ids)}" for u, ids in sorted(trip.assets.items()))
                + f"), +{new}"
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

    async def _run_share(self) -> list[str]:
        """Share every album a user owns with the other users (editors), except faceless ones."""
        faceless = {aid for key, aid in self._state["albums"].items() if key.startswith("faceless:")}
        lines: list[str] = []
        for user, api in self._apis.items():
            me = self._user_ids[user]
            for album in await api.albums():
                members = [u.get("user", {}).get("id") for u in album.get("albumUsers") or []]
                if not members or members[0] != me or album["id"] in faceless:
                    continue
                if album.get("albumName") == self.config["faceless"].get("album", OLD_FACELESS_ALBUM):
                    continue
                missing = [uid for uid in self._user_ids.values() if uid not in members]
                if not missing:
                    continue
                if self.writing:
                    await api.share_album(album["id"], missing)
                lines.append(album.get("albumName") or album["id"])
        return lines

    # partou

    def _partou_ids(self) -> set[str]:
        return set(self._state["partou"].get("assets", {}).values())

    async def _run_partou(self, now: datetime.datetime, force: bool) -> dict[str, Any]:
        """Copy new Partou album items to the owner's library, into monthly albums.

        Once a day after `after` (local), or when forced. Writes in every mode but
        `Kapalı`. Each Partou id is uploaded once (`.storage`: Partou id -> Immich id).
        """
        conf = self.config["partou"]
        local = dt_util.as_local(now)
        today = local.date().isoformat()
        if not force and (local.time() < conf["after"] or self._state["partou"].get("_day") == today):
            return self.summary.get("partou") or {"durum": "bekliyor"}
        result: dict[str, Any] = {}
        tz = dt_util.get_default_time_zone()
        uploaded: dict[str, str] = self._state["partou"].setdefault("assets", {})
        api = PartouApi(async_get_clientsession(self.hass), conf["url"],
                        conf["username"], conf["password"])
        immich = self._apis[self.owner]
        self._partou_writing = True
        try:
            await api.login()
            for child in await api.children():
                first = child.get("firstName") or child["id"]
                prefix = conf["albums"].get(first) or f"{slug(first)}_partou"
                items = await api.album(child["id"])
                new = [i for i in items if i["id"] not in uploaded and i.get("fullSizeUrl")]
                months: dict[str, set[str]] = {}
                for item in reversed(new):  # oldest first
                    data = await api.download(item["fullSizeUrl"])
                    taken = None
                    if item.get("mediaType") != "video":
                        taken = await self.hass.async_add_executor_job(exif_time, data, tz)
                    taken = taken or item_day(item, tz)
                    asset_id = await immich.upload(data, file_name(item), taken)
                    uploaded[item["id"]] = asset_id
                    self._state["faceless"][asset_id] = "partou"
                    month = f"{prefix}_{MONTHS[taken.month - 1]}_{taken.year}"
                    months.setdefault(month, set()).add(asset_id)
                    if len(uploaded) % 25 == 0:
                        await self._store.async_save(self._state)
                for name, ids in sorted(months.items()):
                    album_id = await self._album(f"partou:{name}", name, self.owner, True)
                    await self._add(album_id, self.owner, ids)
                result[first] = {"toplam": len(items), "yeni": len(new),
                                 "albümler": sorted(months)}
            self._state["partou"]["_day"] = today
        except PartouError as err:
            _LOGGER.warning("Partou failed: %s", err)
            result["hata"] = str(err)
        finally:
            self._partou_writing = False
            await self._store.async_save(self._state)
        return result

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
            to_album: list[str] = []
            to_trash: list[str] = []
            to_delete: list[str] = []
            assets = self._own(
                user, await api.search({"type": "IMAGE", "createdAfter": self._state["since"]})
            )
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
                action = conf["trip"] if aid in in_trip else _home_action(conf["home"])
                if action == FACELESS_TRASH:
                    to_trash.append(aid)
                elif action == FACELESS_DELETE:
                    to_delete.append(aid)
                elif action == FACELESS_KEEP:
                    counts["kalsın"] += 1
                    self._decide(aid, "keep")
                else:  # album (trips only): the trip run adds it like any trip photo
                    to_album.append(aid)
                    self._decide(aid, FACELESS_ALBUM)
            if self.writing and to_trash:
                await api.trash(to_trash)
                for aid in to_trash:
                    self._decide(aid, "trash")
            if self.writing and to_delete:
                await api.trash(to_delete, force=True)
                for aid in to_delete:
                    self._decide(aid, "delete")
            counts["albüme"] += len(to_album)
            counts["çöpe"] += len(to_trash)
            counts["silinecek" if not self.writing else "silindi"] += len(to_delete)
        return {k: v for k, v in counts.items() if v}

    def _decide(self, asset_id: str, decision: str) -> None:
        if self.writing:
            self._state["faceless"][asset_id] = decision

    # videos (faces on more than Immich's one preview frame; video_faces.py)

    async def _run_videos(self) -> dict[str, Any]:
        """Reference faces first, then up to `per_run` new videos; matches into person albums."""
        conf = self.config["videos"]
        ml = MlApi(async_get_clientsession(self.hass), conf["ml_url"])
        result: dict[str, Any] = {}
        try:
            refs_done = await self._video_refs(ml)
            result["referans"] = {
                p: len(self._state["video_refs"].get(p, {}).get("refs", []))
                for p in self.config["people"]
            }
            pending = await self._pending_videos()
            result["bekleyen"] = len(pending)
            if refs_done and pending:
                command = ffmpeg_command()
                for user, asset in pending[: conf["per_run"]]:
                    await self._scan_video(ml, command, user, asset)
                    await self._store.async_save(self._state)
            elif not refs_done:
                result["durum"] = "referans yüzler hazırlanıyor"
        except MlError as err:
            _LOGGER.warning("Video face scan failed: %s", err)
            result["hata"] = str(err)
        await self._store.async_save(self._state)
        scanned = {k: v for k, v in self._state["videos"].items() if "faces" in v}
        result["taranan"] = len(scanned)
        result["yüzsüz"] = sum(1 for v in scanned.values() if not v["faces"])
        result["eşleşen"], result["aday"], result["skorlar"] = await self._video_albums(scanned)
        return result

    async def _video_refs(self, ml: MlApi) -> bool:
        """Up to `ref_per_month` reference faces per person and month; True when complete."""
        conf = self.config["videos"]
        budget = REFS_PER_RUN
        for person in self.config["people"]:
            record = self._state["video_refs"].setdefault(person, {"refs": [], "tried": []})
            tried = set(record["tried"])
            have = Counter(r["t"][:7] for r in record["refs"])
            for user, (ids, assets) in self._people_found.get(person, {}).items():
                months: dict[str, list[dict[str, Any]]] = {}
                for asset in sorted(assets, key=lambda a: a.get("fileCreatedAt") or ""):
                    if asset.get("type") == "IMAGE" and asset.get("fileCreatedAt"):
                        months.setdefault(asset["fileCreatedAt"][:7], []).append(asset)
                for month, items in months.items():
                    # spread over the month: every n-th photo first
                    step = max(1, len(items) // conf["ref_per_month"])
                    order = items[::step] + [a for i, a in enumerate(items) if i % step]
                    for asset in order:
                        if have[month] >= conf["ref_per_month"]:
                            break
                        if asset["id"] in tried:
                            continue
                        if budget <= 0:
                            return False
                        budget -= 1
                        tried.add(asset["id"])
                        record["tried"].append(asset["id"])
                        embedding = await self._ref_face(user, asset["id"], set(ids), ml)
                        if embedding is not None:
                            record["refs"].append({
                                "a": asset["id"], "t": asset["fileCreatedAt"],
                                "e": [round(v, 5) for v in embedding],
                            })
                            have[month] += 1
        return True

    async def _ref_face(
        self, user: str, asset_id: str, person_ids: set[str], ml: MlApi
    ) -> list[float] | None:
        api = self._apis[user]
        target = None
        for face in await api.faces(asset_id):
            if (face.get("person") or {}).get("id") in person_ids:
                target = face_box(face, face.get("imageWidth"), face.get("imageHeight"))
                break
        if target is None:
            return None
        faces = await ml.faces(await api.thumbnail(asset_id))
        await asyncio.sleep(ML_PAUSE)
        return pick_face(faces, target)

    async def _pending_videos(self) -> list[tuple[str, dict[str, Any]]]:
        """Videos not scanned yet and not already found by Immich for every person, newest first."""
        partou = self._partou_ids()
        assigned = [
            {a["id"] for _ids, assets in by_user.values() for a in assets}
            for by_user in self._people_found.values()
        ]
        pending: list[tuple[str, dict[str, Any]]] = []
        for user, api in self._apis.items():
            for asset in self._own(user, await api.search({"type": "VIDEO"})):
                aid = asset["id"]
                record = self._state["videos"].get(aid) or {}
                if (
                    asset.get("isTrashed")
                    or asset.get("visibility", "timeline") in ("hidden", "locked")
                    or aid in partou
                    or "faces" in record
                    or "skip" in record
                    or record.get("tries", 0) >= VIDEO_TRIES
                    or (assigned and all(aid in found for found in assigned))
                ):
                    continue
                pending.append((user, asset))
        pending.sort(key=lambda ua: ua[1].get("fileCreatedAt") or "", reverse=True)
        return pending

    async def _scan_video(
        self, ml: MlApi, command: list[str], user: str, asset: dict[str, Any]
    ) -> None:
        conf = self.config["videos"]
        aid = asset["id"]
        record: dict[str, Any] = {"u": user, "t": asset.get("fileCreatedAt")}
        folder = await self.hass.async_add_executor_job(tempfile.mkdtemp, None, "gungors-video-")
        try:
            path = os.path.join(folder, "video")
            if not await self._apis[user].download(
                aid, path, int(conf["max_mb"] * 1024 * 1024), self.hass
            ):
                record["skip"] = "büyük"
                self._state["videos"][aid] = record
                return
            faces: list[list[float]] = []
            duration = parse_duration(asset.get("duration"))
            for at in frame_times(duration, conf["every"], conf["frames"]):
                frame = await extract_frame(command, path, at)
                if frame:
                    faces.extend(f["embedding"] for f in await ml.faces(frame))
                    await asyncio.sleep(ML_PAUSE)
        except (ImmichError, MlError) as err:
            old = self._state["videos"].get(aid) or {}
            self._state["videos"][aid] = {**record, "tries": old.get("tries", 0) + 1,
                                          "hata": str(err)[:200]}
            if isinstance(err, MlError):
                raise
            _LOGGER.warning("Video %s: %s", aid, err)
            return
        finally:
            await self.hass.async_add_executor_job(shutil.rmtree, folder, True)
        when = _parse_time(asset.get("fileCreatedAt"))
        record["faces"] = len(faces)
        for person in self.config["people"]:
            refs = [
                (t, r["e"])
                for r in self._state["video_refs"].get(person, {}).get("refs", [])
                if (t := _parse_time(r["t"])) is not None
            ]
            score = best_score(faces, refs, when, conf["ref_days"])
            record[person] = None if score is None else round(score, 3)
        self._state["videos"][aid] = record

    async def _video_albums(
        self, scanned: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, int], dict[str, int], dict[str, dict[str, int]]]:
        """Scanned videos at or over `match` into the person album, `maybe` into `<album>_aday`."""
        conf = self.config["videos"]
        matched: dict[str, int] = {}
        maybe: dict[str, int] = {}
        buckets: dict[str, dict[str, int]] = {}
        for person, album_name in self.config["people"].items():
            assigned = {
                a["id"]
                for _ids, assets in self._people_found.get(person, {}).values()
                for a in assets
            }
            sure: dict[str, set[str]] = {}
            unsure: dict[str, set[str]] = {}
            hist: Counter = Counter()
            for aid, record in scanned.items():
                score = record.get(person)
                if score is None or aid in assigned or record.get("u") not in self._apis:
                    continue
                hist[f"{math.floor(score * 10) / 10:.1f}"] += 1
                if score >= conf["match"]:
                    sure.setdefault(record["u"], set()).add(aid)
                elif conf["maybe"] and score >= conf["maybe"]:
                    unsure.setdefault(record["u"], set()).add(aid)
            buckets[person] = dict(sorted(hist.items()))
            # totals (the album gets each video once; one removed by hand stays out)
            matched[album_name] = sum(len(ids) for ids in sure.values())
            if sure:
                album_id = await self._album(f"person:{person}", album_name, self.owner, True)
                for user, ids in sure.items():
                    await self._add(album_id, user, ids)
            if unsure:
                name = f"{album_name}_aday"
                album_id = await self._album(f"video_maybe:{person}", name, self.owner, True)
                maybe[name] = sum(len(ids) for ids in unsure.values())
                for user, ids in unsure.items():
                    await self._add(album_id, user, ids)
        return matched, maybe, buckets
