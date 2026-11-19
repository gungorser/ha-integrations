"""Partou parent portal (OuderPortaal / KidsKonnect): the child albums, read only.

Used by the Immich run (`gungors: immich: partou:`, immich.py) to copy the daycare
photos and videos into Immich. The portal gives the full size file (long side
2400 px), a day-only `date` and, for photos, the capture time in the EXIF.
"""
from __future__ import annotations

import datetime
import io
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL = "https://partou.ouderportaal.nl"
PAGE = 100
MAX_PAGES = 200  # 20 000 items: a guard against a portal that never ends the list


class PartouError(Exception):
    """A Partou portal call failed."""


class PartouApi:
    """Login + the child albums of one parent account."""

    def __init__(self, session: aiohttp.ClientSession, url: str, username: str,
                 password: str) -> None:
        self._session = session
        self._base = url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.request(
                method, self._base + path, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60), **kwargs,
            ) as resp:
                if resp.status >= 400:
                    raise PartouError(f"{method} {path}: {resp.status}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise PartouError(f"{method} {path}: {err}") from err

    async def login(self) -> None:
        result = await self._request(
            "PUT", "/auth-api/login",
            json={"username": self._username, "password": self._password},
        )
        payload = (result or {}).get("payload", result) or {}
        token = payload.get("authToken")
        if not token:
            raise PartouError("login: no token (wrong username or password?)")
        if payload.get("mustResetPassword"):
            raise PartouError("login: the portal asks for a new password")
        self._token = token

    async def children(self) -> list[dict[str, Any]]:
        """`[{id, firstName}]` of the children that have an album."""
        albums = await self._request("GET", "/restservices-parent/v2/albums/overview")
        return [
            a["child"] for a in albums or []
            if a.get("type") == "child" and (a.get("child") or {}).get("id")
        ]

    async def album(self, child_id: str) -> list[dict[str, Any]]:
        """Every photo and video of a child's album, newest first (offset paging)."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            result = await self._request(
                "GET",
                f"/restservices-parent/children/{child_id}/album",
                params={"index": page * PAGE, "nrOfPhotos": PAGE},
            )
            photos = (result or {}).get("photos") or []
            fresh = [p for p in photos if p.get("id") and p["id"] not in seen]
            if not fresh:
                break
            seen.update(p["id"] for p in fresh)
            items.extend(fresh)
        return items

    async def download(self, url: str) -> bytes:
        """A (signed, about one hour valid) resource URL; no portal token needed."""
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=300)
            ) as resp:
                if resp.status >= 400:
                    raise PartouError(f"download: {resp.status}")
                return await resp.read()
        except aiohttp.ClientError as err:
            raise PartouError(f"download: {err}") from err


def file_name(item: dict[str, Any]) -> str:
    """The file name in the resource URL: `IMG-5239-<uuid>.jpeg`."""
    path = (item.get("fullSizeUrl") or "").split("?", 1)[0]
    name = path.rsplit("/", 1)[-1]
    if name:
        return name
    return f"{item['id']}.{'mp4' if item.get('mediaType') == 'video' else 'jpeg'}"


def item_day(item: dict[str, Any], tz: datetime.tzinfo) -> datetime.datetime:
    """The portal's day (ms since epoch, local midnight) at noon local time."""
    day = datetime.datetime.fromtimestamp(int(item["date"]) / 1000, tz)
    return day.replace(hour=12, minute=0, second=0, microsecond=0)


def exif_time(data: bytes, tz: datetime.tzinfo) -> datetime.datetime | None:
    """Capture time from a JPEG's EXIF (DateTimeOriginal, else DateTime), in `tz`."""
    try:
        from PIL import Image  # noqa: PLC0415 (Home Assistant ships Pillow)

        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            value = exif.get_ifd(0x8769).get(36867) or exif.get(306)
    except Exception:  # noqa: BLE001 (not an image, no EXIF, no Pillow)
        return None
    if not value:
        return None
    try:
        when = datetime.datetime.strptime(str(value).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return when.replace(tzinfo=tz)
