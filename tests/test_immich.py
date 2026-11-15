"""Tests for the Immich album rules (immich.py)."""
from __future__ import annotations

import datetime
from typing import Any

import pytest

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
        self.busy = False
        self.writes = 0

    def asset(self, aid: str, user: str, taken: datetime.datetime, where=None, *,
              uploaded: datetime.datetime | None = None, kind="IMAGE", faces=1,
              people=(), country=None, state=None, city=None, **extra: Any) -> None:
        exif: dict[str, Any] = {"make": "Apple", "country": country, "state": state, "city": city}
        if where:
            exif["latitude"], exif["longitude"] = where
        self.assets[aid] = {
            "id": aid, "ownerId": user, "type": kind, "isTrashed": False, "isFavorite": False,
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
            if a["ownerId"] != self.user or a["isTrashed"]:
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

    async def trash(self, ids):
        self.s.writes += 1
        for i in ids:
            self.s.assets[i]["isTrashed"] = True
        self.s.trashed.extend(ids)

    async def face_queue_busy(self):
        return self.s.busy


@pytest.fixture
async def setup(hass: HomeAssistant):
    hass.config.latitude, hass.config.longitude = HOME
    server = FakeImmich()
    config = IMMICH_SCHEMA(
        {"url": "http://immich", "users": {"sercan": "k1", "melike": "k2"},
         "owner": "sercan", "people": {"Kaan": "Kaan"}}
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
    server.people["melike"] = [{"id": "mk", "name": "kaan"}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    server.asset("k2", "melike", t(2, 1), HOME, people={"mk"})
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    server.asset("a2", "melike", t(2), ANTALYA, country="Turkey", state="Antalya")
    server.asset("h1", "sercan", t(20), HOME)
    manager.mode = MODE_ALBUM
    await manager.async_run()

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
    manager.mode = MODE_TRASH
    await manager.async_run()
    assert server.trashed == ["label", "bin"]  # trip photo "view" is never trashed
    albums = [a for a in server.albums.values() if a["albumName"] == "check_before_delete"]
    assert [(a["_users"], a["_assets"]) for a in albums] == [(["id-melike"], {"view"})]


async def test_faceless_album_mode_never_trashes(setup) -> None:
    manager, server = setup
    now = dt_util.utcnow()
    manager.mode = MODE_ALBUM
    await manager.async_run()
    manager._state["since"] = (now - datetime.timedelta(days=2)).isoformat()
    server.asset("label", "sercan", now, HOME, faces=0, uploaded=now - datetime.timedelta(days=1, hours=1))
    await manager.async_run()
    assert server.trashed == []
    assert album_named(server, "check_before_delete")["_assets"] == {"label"}


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
