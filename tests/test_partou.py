"""Tests for the Partou step of the Immich run (partou.py, immich.py `_run_partou`)."""
from __future__ import annotations

import datetime
import io
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.gungors import immich as immich_module
from custom_components.gungors.immich import IMMICH_SCHEMA, MODE_ALBUM, MODE_DRY, ImmichManager
from custom_components.gungors.partou import PartouError, exif_time, file_name, item_day

from .test_immich import HOME, ANTALYA, FakeApi, FakeImmich, album_named, t

KAAN = "child-kaan"


def item(pid: str, day: datetime.date, kind: str = "photo") -> dict[str, Any]:
    midnight = datetime.datetime.combine(day, datetime.time(), dt_util.get_default_time_zone())
    ext = "mp4" if kind == "video" else "jpeg"
    return {
        "id": pid,
        "date": int(midnight.timestamp() * 1000),
        "mediaType": kind,
        "fullSizeUrl": f"https://resource.example/photo/IMG-{pid}.{ext}?Expires=1&Signature=x",
    }


class FakePartou:
    """The portal: one child album, newest first."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.logins = 0
        self.fail: str | None = None
        self.files: dict[str, bytes] = {}

    def api(self, _session, url, username, password):
        assert (url, username, password) == ("https://partou.ouderportaal.nl", "u", "p")
        portal = self

        class Api:
            async def login(self):
                portal.logins += 1
                if portal.fail:
                    raise PartouError(portal.fail)

            async def children(self):
                return [{"id": KAAN, "firstName": "Kaan"}]

            async def album(self, child_id):
                assert child_id == KAAN
                return list(portal.items)

            async def download(self, url):
                return portal.files.get(url, b"not a jpeg")

        return Api()


class UploadApi(FakeApi):
    async def upload(self, data, name, taken):
        self.s.writes += 1
        aid = "im-partou-" + name.removeprefix("IMG-").rsplit(".", 1)[0]
        self.s.asset(aid, self.user, taken, None, uploaded=dt_util.utcnow(), faces=0,
                     _name=name)
        return aid


@pytest.fixture
async def setup(hass: HomeAssistant, monkeypatch):
    hass.config.latitude, hass.config.longitude = HOME
    server, portal = FakeImmich(), FakePartou()
    monkeypatch.setattr(immich_module, "PartouApi", portal.api)
    config = IMMICH_SCHEMA(
        {"url": "http://immich", "users": {"sercan": "k1", "melike": "k2"},
         "owner": "sercan", "people": {"Kaan": "Kaan"}, "trips": {"min_assets": 1},
         "partou": {"username": "u", "password": "p", "after": "00:00"}}
    )
    manager = ImmichManager(hass, config)
    await manager.async_start()
    manager.async_stop()
    manager._apis = {u: UploadApi(server, u) for u in ("sercan", "melike")}
    return manager, server, portal


async def test_uploads_once_into_monthly_albums_also_in_dry_run(setup) -> None:
    manager, server, portal = setup
    portal.items = [item("b", datetime.date(2026, 11, 19), "video"),
                    item("a", datetime.date(2026, 3, 26))]
    manager.mode = MODE_DRY  # Partou writes in every mode but Kapalı
    await manager.async_run(force=True)
    assert manager.summary["partou"] == {"Kaan": {
        "toplam": 2, "yeni": 2, "albümler": ["kaan_partou_kasim_2026", "kaan_partou_mart_2026"]}}
    assert album_named(server, "kaan_partou_mart_2026")["_assets"] == {"im-partou-a"}
    assert album_named(server, "kaan_partou_kasim_2026")["_assets"] == {"im-partou-b"}
    assert server.assets["im-partou-b"]["_name"] == "IMG-b.mp4"
    # No EXIF: the portal's day at noon, local time.
    taken = dt_util.parse_datetime(server.assets["im-partou-a"]["fileCreatedAt"])
    assert dt_util.as_local(taken).replace(tzinfo=None) == datetime.datetime(2026, 3, 26, 12)

    portal.items.insert(0, item("c", datetime.date(2026, 11, 20)))
    await manager.async_run(force=True)
    assert manager.summary["partou"]["Kaan"]["yeni"] == 1
    assert len([a for a in server.assets if a.startswith("im-")]) == 3
    assert album_named(server, "kaan_partou_kasim_2026")["_assets"] == {"im-partou-b", "im-partou-c"}
    assert manager.writing is False


async def test_off_mode_does_nothing(setup) -> None:
    manager, server, portal = setup
    portal.items = [item("a", datetime.date(2026, 3, 26))]
    manager.mode = "Kapalı"
    await manager.async_run(force=True)
    assert portal.logins == 0 and server.writes == 0


async def test_uploads_skip_the_person_faceless_and_trip_rules(setup) -> None:
    manager, server, portal = setup
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    manager.mode = MODE_ALBUM
    await manager.async_run(force=True)  # first run: faceless looks from now on
    manager._state["since"] = (dt_util.utcnow() - datetime.timedelta(days=2)).isoformat()
    server.asset("a1", "sercan", t(1), ANTALYA, country="Turkey", state="Antalya")
    portal.items = [item("p", datetime.date(2025, 7, 2))]
    await manager.async_run(force=True)
    pid = "im-partou-p"
    server.assets[pid]["createdAt"] = (dt_util.utcnow() - datetime.timedelta(days=1, hours=1)).isoformat()
    server.assets[pid]["exifInfo"].update(latitude=HOME[0], longitude=HOME[1])  # daycare GPS
    server.assets[pid]["_people"] = {"pk"}  # Immich found Kaan's face
    server.asset("a2", "sercan", t(5), ANTALYA, country="Turkey", state="Antalya")
    await manager.async_run(force=True)
    assert manager.summary.get("faceless", {}) == {}
    assert album_named(server, "check_before_delete") is None
    assert pid not in (album_named(server, "Kaan") or {"_assets": set()})["_assets"]
    # The daycare photo (at home, 2 July) does not split the Antalya trip.
    assert album_named(server, "antalya_2025")["_assets"] == {"a1", "a2"}


async def test_once_a_day_after_the_time(setup, freezer) -> None:
    manager, server, portal = setup
    manager.config["partou"]["after"] = datetime.time(18, 0)
    manager.mode = MODE_DRY
    tz = dt_util.get_default_time_zone()
    freezer.move_to(datetime.datetime(2026, 11, 19, 17, 0, tzinfo=tz))
    await manager.async_run()
    assert portal.logins == 0
    freezer.move_to(datetime.datetime(2026, 11, 19, 18, 5, tzinfo=tz))
    await manager.async_run()
    await manager.async_run()
    assert portal.logins == 1
    await manager.async_run(force=True)  # gungors.immich_run
    assert portal.logins == 2
    freezer.move_to(datetime.datetime(2026, 11, 20, 18, 5, tzinfo=tz))
    await manager.async_run()
    assert portal.logins == 3


async def test_portal_error_does_not_stop_the_run(setup) -> None:
    manager, server, portal = setup
    server.people["sercan"] = [{"id": "pk", "name": "Kaan"}]
    server.asset("k1", "sercan", t(1, 1), HOME, people={"pk"})
    portal.fail = "login: 401"
    manager.mode = MODE_DRY
    await manager.async_run(force=True)
    assert manager.status == "tamam"
    assert manager.summary["partou"] == {"hata": "login: 401"}
    assert manager.summary["people"] == {"Kaan": 1}


def test_helpers() -> None:
    tz = dt_util.get_default_time_zone()
    it = item("x", datetime.date(2026, 3, 26))
    assert file_name(it) == "IMG-x.jpeg"
    assert item_day(it, tz) == datetime.datetime(2026, 3, 26, 12, tzinfo=tz)
    assert exif_time(b"no image", tz) is None

    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    exif = Image.Exif()
    exif[306] = "2026:11:19 11:36:08"
    Image.new("RGB", (4, 4)).save(buf, "JPEG", exif=exif)
    assert exif_time(buf.getvalue(), tz) == datetime.datetime(2026, 11, 19, 11, 36, 8, tzinfo=tz)
