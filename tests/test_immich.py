"""Tests for the Immich album rules (immich.py)."""
from __future__ import annotations

import datetime
from typing import Any

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.gungors.immich import (
    IMMICH_SCHEMA,
    MODE_ALBUM,
    MODE_DRY,
    MODE_TRASH,
    ImmichManager,
    Point,
    find_trips,
    place_of,
    slug,
    trip_base_name,
    unique_name,
)

HOME = (51.44, 5.47)  # Eindhoven
ANTALYA = (36.88, 30.70)
VALENCIA = (39.47, -0.38)
UTC = datetime.timezone.utc


def t(day: int, month: int = 7, year: int = 2025, hour: int = 12) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, tzinfo=UTC)


def p(pid: str, when: datetime.datetime, where=None, user="sercan", country=None,
      state=None, city=None, camera=True) -> Point:
    lat, lon = where if where else (None, None)
    return Point(pid, user, when, when.year, lat, lon, country, state, city, camera)


DETAIL = {"turkey": "state", "netherlands": "city"}


# --- pure logic ----------------------------------------------------------------------------------


def test_slug() -> None:
    assert slug("Türkiye") == "turkiye"
    assert slug("İzmir") == "izmir"
    assert slug("Den Haag") == "den_haag"
    assert slug("Muğla") == "mugla"


def test_place_of() -> None:
    assert place_of(p("a", t(1), ANTALYA, country="Turkey", state="Antalya", city="Kemer"),
                    DETAIL, {}) == "antalya"
    assert place_of(p("a", t(1), (52.37, 4.9), country="Netherlands", state="North Holland",
                      city="Amsterdam"), DETAIL, {}) == "amsterdam"
    assert place_of(p("a", t(1), VALENCIA, country="Spain", city="Valencia"), DETAIL, {}) == "ispanya"
    assert place_of(p("a", t(1), VALENCIA, country="Spain"), DETAIL, {"spain": "espana"}) == "espana"
    assert place_of(p("a", t(1), VALENCIA, country="Narnia"), DETAIL, {}) == "narnia"


def test_trip_spans_days_without_photos_and_ends_at_home() -> None:
    points = [
        p("h0", t(30, 6), HOME),
        p("a1", t(1), ANTALYA, country="Turkey", state="Antalya"),
        p("n1", t(5)),  # camera photo without GPS during the trip
        p("s1", t(6), camera=False),  # screenshot without GPS
        p("a2", t(10), ANTALYA, country="Turkey", state="Antalya"),
        p("h1", t(21), HOME),
        p("n2", t(22)),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert len(trips) == 1
    trip = trips[0]
    assert trip.closed
    assert trip.assets == {"sercan": {"a1", "n1", "a2"}}
    assert (trip.start, trip.end) == (t(1), t(10))
    assert trip_base_name(trip) == "antalya_2025"


def test_open_trip_and_near_home_is_not_away() -> None:
    near = (51.69, 5.30)  # 's-Hertogenbosch, ~30 km
    trips = find_trips(
        [p("x", t(1), near), p("a", t(2), VALENCIA, country="Spain")], HOME, 50, DETAIL, {}
    )
    assert len(trips) == 1 and not trips[0].closed
    assert trips[0].all_assets() == {"a"}
    assert trip_base_name(trips[0]) == "ispanya_2025"


def test_trips_of_two_users_merge_and_one_staying_home_does_not_end_it() -> None:
    points = [
        p("s1", t(1), ANTALYA, country="Turkey", state="Antalya"),
        p("s2", t(8), ANTALYA, country="Turkey", state="Antalya"),
        p("m0", t(3), HOME, user="melike"),
        p("m1", t(4), ANTALYA, user="melike", country="Turkey", state="Antalya"),
        p("m2", t(9), ANTALYA, user="melike", country="Turkey", state="Antalya"),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert len(trips) == 1
    assert trips[0].assets == {"sercan": {"s1", "s2"}, "melike": {"m1", "m2"}}
    assert trips[0].end == t(9)


def test_unique_name() -> None:
    assert unique_name("antalya_2025", set()) == "antalya_2025"
    assert unique_name("antalya_2025", {"antalya_2025"}) == "antalya_2025_2"
    assert unique_name("antalya_2025", {"antalya_2025", "antalya_2025_2"}) == "antalya_2025_3"


# --- manager with a fake Immich ------------------------------------------------------------------


class FakeImmich:
    """In-memory Immich shared by the per-user fake APIs."""

    def __init__(self) -> None:
        self.assets: dict[str, dict[str, Any]] = {}
        self.people: dict[str, list[dict[str, Any]]] = {"sercan": [], "melike": []}
        self.faces: dict[str, int] = {}
        self.albums: dict[str, dict[str, Any]] = {}
        self.trashed: list[str] = []
        self.deleted: list[str] = []
        self.busy = False
        self.partners = False  # searches also return the other user's assets
        self.writes = 0

    def asset(self, aid: str, user: str, taken: datetime.datetime, where=None, *,
              uploaded: datetime.datetime | None = None, kind="IMAGE", faces=1,
              people=(), country=None, state=None, city=None, **extra: Any) -> None:
        exif: dict[str, Any] = {"make": "Apple", "country": country, "state": state, "city": city}
        if where:
            exif["latitude"], exif["longitude"] = where
        self.assets[aid] = {
            "id": aid, "ownerId": f"id-{user}", "type": kind, "isTrashed": False, "isFavorite": False,
            "isArchived": False, "visibility": "timeline",
            "fileCreatedAt": taken.isoformat(), "localDateTime": taken.isoformat(),
            "createdAt": (uploaded or taken).isoformat(), "updatedAt": taken.isoformat(),
            "exifInfo": exif, "_people": set(people), **extra,
        }
        self.faces[aid] = faces


class FakeApi:
    def __init__(self, server: FakeImmich, user: str) -> None:
        self.s, self.user = server, user

    async def me(self):
        return {"id": f"id-{self.user}"}

    async def search(self, body):
        out = []
        for a in self.s.assets.values():
            if (a["ownerId"] != f"id-{self.user}" and not self.s.partners) or a["isTrashed"]:
                continue
            if "personIds" in body and not set(body["personIds"]) & a["_people"]:
                continue
            if body.get("type") and a["type"] != body["type"]:
                continue
            if body.get("createdAfter") and a["createdAt"] <= body["createdAfter"]:
                continue
            out.append(a)
        return out

    async def people(self):
        return self.s.people[self.user]

    async def albums(self, name=None, assetId=None):
        out = []
        for alb in self.s.albums.values():
            if f"id-{self.user}" not in alb["_users"]:
                continue
            if name is not None and alb["albumName"] != name:
                continue
            if assetId is not None and assetId not in alb["_assets"]:
                continue
            alb["albumUsers"] = [{"user": {"id": uid}} for uid in alb["_users"]]
            out.append(alb)
        return out

    async def album(self, album_id):
        return self.s.albums.get(album_id)

    async def create_album(self, name, user_ids):
        self.s.writes += 1
        aid = f"album{len(self.s.albums) + 1}"
        self.s.albums[aid] = {
            "id": aid, "albumName": name, "_assets": set(),
            "_users": [f"id-{self.user}", *user_ids],
        }
        return self.s.albums[aid]

    async def share_album(self, album_id, user_ids):
        self.s.writes += 1
        self.s.albums[album_id]["_users"].extend(user_ids)

    async def rename_album(self, album_id, name):
        self.s.writes += 1
        self.s.albums[album_id]["albumName"] = name

    async def add_to_album(self, album_id, ids):
        self.s.writes += 1
        self.s.albums[album_id]["_assets"].update(ids)
        return set(ids)

    async def faces(self, asset_id):
        return [{}] * self.s.faces.get(asset_id, 0)

    async def trash(self, ids, force=False):
        self.s.writes += 1
        for i in ids:
            self.s.assets[i]["isTrashed"] = True
        (self.s.deleted if force else self.s.trashed).extend(ids)

    async def face_queue_busy(self):
        return self.s.busy


@pytest.fixture
async def setup(hass: HomeAssistant):
    hass.config.latitude, hass.config.longitude = HOME
    server = FakeImmich()
    config = IMMICH_SCHEMA(
        {"url": "http://immich", "users": {"sercan": "k1", "melike": "k2"},
         "owner": "sercan", "people": {"Kaan": "Kaan"}, "trips": {"min_assets": 1}}
    )
    manager = ImmichManager(hass, config)
    await manager.async_start()
    manager.async_stop()
    manager._apis = {u: FakeApi(server, u) for u in ("sercan", "melike")}
    return manager, server


def album_named(server: FakeImmich, name: str) -> dict[str, Any] | None:
    return next((a for a in server.albums.values() if a["albumName"] == name), None)


async def test_dry_run_writes_nothing(setup) -> None:
    manager, server = setup
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    manager.mode = MODE_DRY
    await manager.async_run()
    assert server.writes == 0
    assert manager.status == "tamam"
    assert manager.summary["people"] == {"Kaan": 1}
    assert manager.summary["trips"][0].startswith("antalya_2025: 2025-07-01..2025-07-01, 1 foto")


async def test_albums_written_once(setup) -> None:
    manager, server = setup
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    server.people["melike"] = [{"id": "mk", "name": "kaan "}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    server.asset("k2", "melike", t(2, 1), HOME, people={"mk"})
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    server.asset("a2", "melike", t(2), ANTALYA, country="Turkey", state="Antalya")
    server.asset("h1", "sercan", t(20), HOME)
    manager.mode = MODE_ALBUM
    await manager.async_run()
    assert manager.summary["people_users"] == {
        "Kaan": {"sercan": "1 kişi, 1 foto", "melike": "1 kişi, 1 foto"}
    }

    kaan = album_named(server, "Kaan")
    assert kaan["_assets"] == {"k1", "k2"}
    assert kaan["_users"] == ["id-sercan", "id-melike"]
    assert album_named(server, "antalya_2025")["_assets"] == {"a1", "a2"}

    kaan["_assets"].discard("k1")  # removed by hand: not added again
    writes = server.writes
    await manager.async_run()
    assert "k1" not in kaan["_assets"]
    assert server.writes == writes


async def test_trip_album_renamed_when_closed(setup) -> None:
    manager, server = setup
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    manager.mode = MODE_ALBUM
    await manager.async_run()
    assert album_named(server, "antalya_2025")
    for day in range(2, 6):
        server.asset(f"v{day}", "sercan", t(day), VALENCIA, country="Spain")
    server.asset("h", "sercan", t(7), HOME)
    await manager.async_run()
    assert album_named(server, "antalya_2025") is None
    assert album_named(server, "ispanya_2025")["_assets"] == {"a1", "v2", "v3", "v4", "v5"}


async def test_faceless_new_uploads(setup) -> None:
    manager, server = setup
    now = dt_util.utcnow()
    server.asset("old", "sercan", now - datetime.timedelta(days=400), HOME, faces=0,
                 uploaded=now - datetime.timedelta(days=400))
    manager.mode = MODE_ALBUM
    await manager.async_run()  # first run: from now on

    later = now + datetime.timedelta(minutes=1)
    server.asset("label", "sercan", later, HOME, faces=0, uploaded=later)
    server.asset("face", "sercan", later, HOME, faces=2, uploaded=later)
    server.asset("fav", "sercan", later, HOME, faces=0, uploaded=later, isFavorite=True)
    server.asset("video", "sercan", later, HOME, faces=0, uploaded=later, kind="VIDEO")
    server.asset("view", "melike", later, VALENCIA, faces=0, uploaded=later, country="Spain")
    server.asset("bin", "melike", later, HOME, faces=0, uploaded=later)

    await manager.async_run()
    assert manager.summary["faceless"] == {"bekliyor": 5}  # the video is never looked at

    manager._state["since"] = (now - datetime.timedelta(days=2)).isoformat()
    for a in server.assets.values():
        if a["id"] != "old":
            a["createdAt"] = (now - datetime.timedelta(days=1, hours=1)).isoformat()
    server.busy = True
    await manager.async_run()
    assert manager.summary["faceless"] == {"bekliyor": 5}  # face detection still busy

    server.busy = False
    manager.config["faceless"]["home"] = "trash"  # trip photos stay "album"
    await manager.async_run()
    assert server.trashed == ["label", "bin"]  # trip photo "view" stays in its trip album
    assert manager.summary["faceless"] == {"albüme": 1, "çöpe": 2}
    assert manager._state["faceless"]["view"] == "album"
    assert album_named(server, "check_before_delete") is None


async def test_faceless_defaults_never_trash(setup) -> None:
    manager, server = setup
    now = dt_util.utcnow()
    manager.mode = MODE_ALBUM
    await manager.async_run()
    manager._state["since"] = (now - datetime.timedelta(days=2)).isoformat()
    server.asset("label", "sercan", now, HOME, faces=0, uploaded=now - datetime.timedelta(days=1, hours=1))
    await manager.async_run()
    assert server.trashed == [] and server.deleted == []
    assert manager.summary["faceless"] == {"kalsın": 1}  # home default: keep, no album
    assert album_named(server, "check_before_delete") is None


async def test_faceless_trip_keep_stays_out_of_the_trip_album(setup) -> None:
    manager, server = setup
    now = dt_util.utcnow()
    manager.mode = MODE_ALBUM
    await manager.async_run()
    manager.config["faceless"]["trip"] = "keep"
    since = now - datetime.timedelta(days=30)
    manager._state["since"] = since.isoformat()
    fresh = now - datetime.timedelta(hours=1)  # face detection not decided yet
    server.asset("v1", "sercan", t(1), VALENCIA, country="Spain", faces=1, uploaded=since - datetime.timedelta(days=1))
    server.asset("v2", "sercan", t(2), VALENCIA, country="Spain", faces=1, uploaded=fresh)
    server.asset("v3", "sercan", t(3), VALENCIA, country="Spain", faces=0, uploaded=fresh)
    server.asset("v4", "sercan", t(4), VALENCIA, country="Spain", faces=0, uploaded=since - datetime.timedelta(days=1))
    server.asset("h", "sercan", t(6), HOME)
    await manager.async_run()
    # old uploads join at once; new ones wait for the faceless rule
    assert album_named(server, "ispanya_2025")["_assets"] == {"v1", "v4"}

    for aid in ("v2", "v3"):
        server.assets[aid]["createdAt"] = (now - datetime.timedelta(days=2)).isoformat()
        server.assets[aid]["updatedAt"] = now.isoformat()
    await manager.async_run()  # v2 has a face, v3 is kept out
    await manager.async_run()
    assert album_named(server, "ispanya_2025")["_assets"] == {"v1", "v2", "v4"}
    assert manager._state["faceless"]["v3"] == "keep"


async def test_setup_from_yaml(hass: HomeAssistant, monkeypatch) -> None:
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    server = FakeImmich()
    monkeypatch.setattr(
        "custom_components.gungors.immich.ImmichApi",
        lambda _session, _url, key: FakeApi(server, {"k1": "sercan", "k2": "melike"}[key]),
    )
    config = {"gungors": {"immich": {"url": "http://immich",
                                     "users": {"sercan": "k1", "melike": "k2"}}}}
    assert await async_setup_component(hass, "gungors", config)
    await hass.async_block_till_done()
    assert hass.states.get("select.immich").state == MODE_DRY
    assert hass.states.get("sensor.immich").state == "tamam"
    assert hass.services.has_service("gungors", "immich_run")

    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.immich", "option": MODE_ALBUM},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.immich").attributes["mode"] == MODE_ALBUM

    before = hass.states.get("sensor.immich").attributes["last_run"]
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.immich_sync"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.immich").attributes["last_run"] != before


async def test_scan_interval_zero_means_no_timer(hass: HomeAssistant) -> None:
    base = {"url": "http://immich", "users": {"sercan": "k1"}}
    assert IMMICH_SCHEMA({**base, "scan_interval": 0})["scan_interval"] == 0
    with pytest.raises(vol.Invalid):
        IMMICH_SCHEMA({**base, "scan_interval": 30})
    manager = ImmichManager(hass, IMMICH_SCHEMA({**base, "scan_interval": 0}))
    manager._apply_config()
    assert manager._unsub is None


async def test_share_albums(setup) -> None:
    manager, server = setup
    manager.config = {**manager.config, "share_albums": True}
    api = manager._apis["melike"]
    manager.mode = MODE_DRY
    await manager.async_run()
    holiday = await api.create_album("tatil_elle", [])
    await api.create_album("check_before_delete", [])
    shared = await manager._apis["sercan"].create_album("ortak", ["id-melike"])
    writes = server.writes

    await manager.async_run()
    assert manager.summary["shared"] == ["tatil_elle"]
    assert server.writes == writes  # dry run

    manager.mode = MODE_ALBUM
    await manager.async_run()
    assert holiday["_users"] == ["id-melike", "id-sercan"]
    assert album_named(server, "check_before_delete")["_users"] == ["id-melike"]
    assert shared["_users"] == ["id-sercan", "id-melike"]
    await manager.async_run()
    assert manager.summary["shared"] == []


async def test_partner_assets_are_not_counted_twice(setup) -> None:
    manager, server = setup
    server.partners = True
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    server.asset("k2", "melike", t(2, 1), HOME, people={"pk"})
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    server.asset("a2", "melike", t(2), ANTALYA, country="Turkey", state="Antalya")
    manager.mode = MODE_DRY
    await manager.async_run()
    assert manager.summary["people"] == {"Kaan": 1}
    assert manager.summary["trips"][0].endswith("2 foto/video (melike 1, sercan 1), +2, sürüyor")
    assert manager.summary["users"] == {"sercan": {"hesap": None, "foto": 2}, "melike": {"hesap": None, "foto": 2}}


def test_min_days_drops_day_trips_in_the_home_country() -> None:
    utrecht, rotterdam = (52.09, 5.12), (51.92, 4.48)
    points = [
        p("u1", t(1), utrecht, country="Netherlands", city="Utrecht"),
        p("h1", t(1, hour=20), HOME),
        p("r1", t(5), rotterdam, country="Netherlands", city="Rotterdam"),
        p("r2", t(6), rotterdam, country="Netherlands", city="Rotterdam"),
        p("h2", t(7), HOME),
        p("b1", t(9), (50.85, 4.35), country="Belgium"),
        p("h3", t(9, hour=20), HOME),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {}, {"netherlands": 2})
    assert [trip_base_name(x) for x in trips] == ["rotterdam_2025", "belcika_2025"]


def test_country_key() -> None:
    from custom_components.gungors.immich import country_key  # noqa: PLC0415

    assert country_key("The Netherlands") == "netherlands"
    assert country_key("Türkiye") == "turkey"
    assert place_of(p("a", t(1), ANTALYA, country="Türkiye", state="Antalya"), DETAIL, {}) == "antalya"


def test_min_days_with_the_netherlands_name() -> None:
    points = [
        p("r1", t(5), (51.92, 4.48), country="The Netherlands", city="Rotterdam"),
        p("h", t(5, hour=20), HOME),
    ]
    assert find_trips(points, HOME, 50, DETAIL, {}, {"netherlands": 2}) == []


def test_long_gap_starts_a_new_trip_and_countries_join() -> None:
    croatia, belgium, slovenia = (45.81, 15.98), (50.85, 4.35), (46.05, 14.51)
    points = [
        p("c1", t(22, 6), croatia, country="Croatia"),
        p("c2", t(1, 7), croatia, country="Croatia"),  # 9 days later: same trip
        p("b1", t(13, 7), belgium, country="Belgium"),  # 12 days later: new trip
        p("b2", t(14, 7), belgium, country="Belgium"),
        p("c3", t(20, 7), croatia, country="Croatia"),  # other country, 6 days: same trip
        p("s1", t(21, 7), slovenia, country="Slovenia"),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert [sorted(x.all_assets()) for x in trips] == [["c1", "c2"], ["b1", "b2", "c3", "s1"]]
    assert [x.closed for x in trips] == [True, False]
    assert trip_base_name(trips[1]) == "belcika_hirvatistan_slovenya_2025"


def test_trips_days_apart_merge_with_every_city_and_the_start_year() -> None:
    ankara, samsun = (39.93, 32.85), (41.29, 36.33)
    points = [
        p("a1", t(27, 12, 2024), ankara, country="Türkiye", state="Ankara"),
        p("s1", t(31, 12, 2024), samsun, user="melike", country="Türkiye", state="Samsun"),
        p("a2", t(9, 1, 2025), ankara, user="melike", country="Türkiye", state="Ankara"),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert len(trips) == 1
    assert trip_base_name(trips[0]) == "ankara_samsun_2024"
    # A single-photo city stays in the name; only stopover countries go.
    assert trip_base_name(trips[0], {}, "netherlands", 0.1, 2) == "ankara_samsun_2024"


def test_coming_home_ends_the_trip_of_that_user() -> None:
    ankara = (39.93, 32.85)
    points = [
        p("a1", t(1), ankara, country="Turkey", state="Ankara"),
        p("h1", t(3), HOME),
        p("a2", t(6), ankara, country="Turkey", state="Ankara"),
    ]
    assert len(find_trips(points, HOME, 50, DETAIL, {})) == 2


def test_multi_country_name_skips_the_home_country() -> None:
    utrecht, paris, berlin = (52.09, 5.12), (48.86, 2.35), (52.52, 13.40)
    points = [
        p("u1", t(26), utrecht, country="Netherlands", city="Utrecht"),
        p("v1", t(27), VALENCIA, country="Spain"),
        p("f1", t(3, 8), paris, country="France"),
        p("v2", t(5, 8), VALENCIA, country="Spain"),
        p("g1", t(10, 8), berlin, country="Germany"),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert trip_base_name(trips[0], {}, "netherlands") == "ispanya_fransa_almanya_2025"
    # One foreign country: its places only, without the home country's.
    one = find_trips(points[:2], HOME, 50, DETAIL, {})
    assert trip_base_name(one[0], {}, "netherlands") == "ispanya_2025"


def test_min_assets() -> None:
    points = [p("v1", t(1), VALENCIA, country="Spain"), p("v2", t(2), VALENCIA, country="Spain")]
    assert find_trips(points, HOME, 50, DETAIL, {}, min_assets=3) == []
    assert len(find_trips(points, HOME, 50, DETAIL, {}, min_assets=2)) == 1


def test_gap_counts_calendar_days() -> None:
    ankara = (39.93, 32.85)
    points = [
        p("a1", t(31, 12, 2024, hour=9), ankara, country="Turkey", state="Ankara"),
        p("a2", t(10, 1, 2025, hour=20), ankara, country="Turkey", state="Ankara"),
        p("a3", t(22, 1, 2025), ankara, country="Turkey", state="Ankara"),
    ]
    trips = find_trips(points, HOME, 50, DETAIL, {})
    assert [sorted(x.all_assets()) for x in trips] == [["a1", "a2"], ["a3"]]


def test_stopovers_are_left_out_of_the_name() -> None:
    frankfurt, samsun, istanbul = (50.11, 8.68), (41.29, 36.33), (41.01, 28.98)
    points = [p("f1", t(4, 10), frankfurt, country="Germany")]
    points += [p("i1", t(4, 10, hour=18), istanbul, country="Turkey", state="Istanbul")]
    points += [
        p(f"s{n}", t(5 + n, 10), samsun, country="Turkey", state="Samsun") for n in range(18)
    ]
    trip = find_trips(points, HOME, 50, DETAIL, {})[0]
    assert trip_base_name(trip, {}, "netherlands", 0.1) == "samsun_2025"
    assert trip_base_name(trip, {}, "netherlands") == "almanya_turkiye_2025"


def test_single_photo_places_are_left_out_of_the_name() -> None:
    frankfurt, ankara = (50.11, 8.68), (39.93, 32.85)
    points = [
        p("f1", t(30, 10), frankfurt, country="Germany"),
        p("a1", t(31, 10), ankara, country="Turkey", state="Ankara"),
        p("a2", t(1, 11), ankara, country="Turkey", state="Ankara"),
    ]
    trip = find_trips(points, HOME, 50, DETAIL, {})[0]
    assert trip_base_name(trip, {}, "netherlands", 0.1) == "almanya_turkiye_2025"
    assert trip_base_name(trip, {}, "netherlands", 0.1, 2) == "ankara_2025"
    # Only single-photo places: the first most common one still names it.
    lone = find_trips(points[:1], HOME, 50, DETAIL, {})[0]
    assert trip_base_name(lone, {}, "netherlands", 0.1, 2) == "almanya_2025"


async def test_faceless_actions_home_and_trip(setup) -> None:
    manager, server = setup
    now = dt_util.utcnow()
    manager.mode = MODE_ALBUM
    await manager.async_run()
    manager._state["since"] = (now - datetime.timedelta(days=2)).isoformat()
    old = now - datetime.timedelta(days=1, hours=1)
    server.asset("home1", "sercan", now, HOME, faces=0, uploaded=old)
    server.asset("trip1", "melike", now, VALENCIA, faces=0, uploaded=old, country="Spain")
    manager.config["faceless"].update(home="keep", trip="delete")

    manager.mode = MODE_DRY  # the plan: nothing written
    await manager.async_run()
    assert manager.summary["faceless"] == {"kalsın": 1, "silinecek": 1}
    assert server.deleted == []

    manager.mode = MODE_ALBUM
    await manager.async_run()
    assert manager.summary["faceless"] == {"kalsın": 1, "silindi": 1}
    assert server.deleted == ["trip1"] and server.trashed == []
    assert album_named(server, "check_before_delete") is None
    await manager.async_run()  # decided once
    assert server.deleted == ["trip1"]


async def test_old_trash_mode_restores_as_album(setup) -> None:
    manager, _server = setup
    await manager.async_set_mode(MODE_TRASH, run=False)
    assert manager.mode == MODE_ALBUM
    assert manager.config["faceless"]["home"] == "keep"
    assert manager.config["faceless"]["trip"] == "album"


# --- faces in videos -----------------------------------------------------------------------------

from custom_components.gungors import immich as immich_module  # noqa: E402
from custom_components.gungors.video_faces import (  # noqa: E402
    best_score,
    frame_times,
    parse_duration,
    pick_face,
)


def test_frame_times_and_duration() -> None:
    assert parse_duration("0:00:25.500000") == 25.5
    assert parse_duration("1:02:03") == 3723
    assert parse_duration(None) == 0
    assert frame_times(0, 3, 10) == [0.0]
    assert len(frame_times(25.5, 3, 10)) == 9
    times = frame_times(120, 3, 10)
    assert len(times) == 10 and times[0] == 6.0 and times[-1] == 114.0


def test_best_score_uses_references_close_in_time() -> None:
    old = (t(1, 1, 2024), [1.0, 0.0])
    new = (t(1, 7, 2025), [0.0, 1.0])
    face = [[0.0, 1.0]]
    assert best_score(face, [old, new], t(10, 7, 2025), 120) == 1.0
    assert best_score(face, [old, new], t(10, 1, 2024), 120) == 0.0
    assert best_score(face, [old], t(10, 7, 2025), 120) == 0.0  # none close: all count
    assert best_score([], [old], None, 120) is None


def test_pick_face_by_overlap() -> None:
    faces = [{"box": (0.0, 0.0, 0.2, 0.2), "embedding": [1.0]},
             {"box": (0.5, 0.5, 0.9, 0.9), "embedding": [2.0]}]
    assert pick_face(faces, (0.55, 0.5, 0.9, 0.95)) == [2.0]
    assert pick_face(faces, (0.3, 0.3, 0.4, 0.4)) is None


class FakeMl:
    """Image bytes -> faces: `thumb-<asset>` and `frame-<asset>` from the test's tables."""

    faces_by_image: dict[bytes, list[dict[str, Any]]] = {}
    calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def faces(self, image: bytes):
        FakeMl.calls += 1
        return FakeMl.faces_by_image.get(image, [])


@pytest.fixture
async def video_setup(setup, monkeypatch, tmp_path):
    manager, server = setup
    manager.config = IMMICH_SCHEMA(
        {"url": "http://immich", "users": {"sercan": "k1", "melike": "k2"},
         "owner": "sercan", "people": {"Kaan": "Kaan"}, "trips": {"min_assets": 1},
         "videos": {"ml_url": "http://ml", "per_run": 2, "maybe": 0.3}}
    )
    FakeMl.faces_by_image = {}
    FakeMl.calls = 0
    monkeypatch.setattr(immich_module, "MlApi", FakeMl)
    monkeypatch.setattr(immich_module, "ML_PAUSE", 0)
    monkeypatch.setattr(immich_module, "ffmpeg_command", lambda: ["ffmpeg"])

    async def extract_frame(command, path, at):
        with open(path, "rb") as handle:
            return b"frame-" + handle.read()

    monkeypatch.setattr(immich_module, "extract_frame", extract_frame)
    box = {"boundingBoxX1": 10, "boundingBoxY1": 10, "boundingBoxX2": 50, "boundingBoxY2": 50,
           "imageWidth": 100, "imageHeight": 100}

    async def faces(self, asset_id):
        if asset_id in self.s.assets and "pk" in self.s.assets[asset_id]["_people"]:
            return [{"person": {"id": "pk", "name": "Kaan"}, **box}]
        return [{}] * self.s.faces.get(asset_id, 0)

    async def thumbnail(self, asset_id):
        return b"thumb-" + asset_id.encode()

    async def download(self, asset_id, path, max_bytes, hass):
        with open(path, "wb") as handle:
            handle.write(asset_id.encode())
        return True

    monkeypatch.setattr(FakeApi, "faces", faces, raising=False)
    monkeypatch.setattr(FakeApi, "thumbnail", thumbnail, raising=False)
    monkeypatch.setattr(FakeApi, "download", download, raising=False)
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    FakeMl.faces_by_image[b"thumb-k1"] = [{"box": (0.1, 0.1, 0.5, 0.5), "embedding": [1.0, 0.0]}]
    return manager, server


def kaan_face(x: float) -> list[dict[str, Any]]:
    import math
    return [{"box": (0, 0, 1, 1), "embedding": [x, math.sqrt(1 - x * x)]}]


async def test_video_faces_dry_run_then_album(video_setup) -> None:
    manager, server = video_setup
    server.asset("v1", "sercan", t(2, 1), kind="VIDEO", duration="0:00:09.000")
    server.asset("v2", "melike", t(3, 1), kind="VIDEO", duration="0:00:09.000")
    server.asset("v3", "sercan", t(4, 1), kind="VIDEO", duration="0:00:09.000")
    server.asset("hidden", "sercan", t(5, 1), kind="VIDEO", visibility="hidden")
    server.asset("known", "sercan", t(6, 1), kind="VIDEO", people={"pk"})
    FakeMl.faces_by_image[b"frame-v1"] = kaan_face(0.9)
    FakeMl.faces_by_image[b"frame-v2"] = kaan_face(0.4)
    FakeMl.faces_by_image[b"frame-v3"] = kaan_face(0.1)

    manager.mode = MODE_DRY
    await manager.async_run()
    videos = manager.summary["videos"]
    assert server.writes == 0
    assert videos["referans"] == {"Kaan": 1}
    assert videos["bekleyen"] == 3  # hidden motion part and the known video left out
    assert videos["taranan"] == 2   # per_run: the two newest (v3, v2)
    assert manager._state["videos"]["v3"]["Kaan"] == 0.1

    await manager.async_run()
    videos = manager.summary["videos"]
    assert videos["taranan"] == 3 and videos["bekleyen"] == 1
    assert videos["eşleşen"] == {"Kaan": 1}
    assert videos["aday"] == {"Kaan_aday": 1}
    assert videos["skorlar"]["Kaan"] == {"0.1": 1, "0.4": 1, "0.9": 1}
    assert server.writes == 0

    manager.mode = MODE_ALBUM
    calls = FakeMl.calls
    await manager.async_run()
    assert FakeMl.calls == calls  # nothing scanned twice
    assert album_named(server, "Kaan")["_assets"] == {"k1", "known", "v1"}
    assert album_named(server, "Kaan_aday")["_assets"] == {"v2"}


async def test_video_faces_wait_for_references(video_setup, monkeypatch) -> None:
    manager, server = video_setup
    monkeypatch.setattr(immich_module, "REFS_PER_RUN", 0)
    server.asset("v1", "sercan", t(2, 1), kind="VIDEO")
    manager.mode = MODE_DRY
    await manager.async_run()
    assert manager.summary["videos"]["durum"] == "referans yüzler hazırlanıyor"
    assert manager.summary["videos"]["taranan"] == 0
