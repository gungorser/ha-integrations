"""Faces in videos for the Immich person albums (`immich: videos:`).

Immich looks for faces in a video on one preview frame only, so a video where the person is not
in that frame never reaches the person's album. This module takes a few frames of each video
(ffmpeg, lowest priority, one video at a time), sends them to Immich's own machine-learning
server (`/predict`, the same face model Immich uses; nothing leaves the house) and compares the
faces with reference faces of each named person taken from that person's photos close in time
(a child's face changes). The best cosine similarity of any face in any frame is the video's
score for that person.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import shutil
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL = "buffalo_l"
FRAME_SIZE = 1440  # longest side of a frame sent to the model


class MlError(Exception):
    """The machine-learning server or ffmpeg failed."""


def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Overlap of two boxes (x1, y1, x2, y2)."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def parse_duration(value: Any) -> float:
    """Immich `duration` -> seconds; 0 when unknown.

    Immich 3 gives milliseconds (41299), older versions "0:01:02.345000".
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, value / 1000)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) / 1000
    try:
        parts = [float(p) for p in str(value).split(":")]
    except ValueError:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def frame_times(duration: float, every: float, max_frames: int) -> list[float]:
    """When to take frames: every `every` seconds, spread evenly when that is too many."""
    if duration <= 0:
        return [0.0]
    count = max(1, min(max_frames, int(duration // every) + 1))
    step = duration / count
    return [round(step * (i + 0.5), 2) for i in range(count)]


def face_box(face: dict[str, Any], width: float, height: float) -> tuple[float, float, float, float]:
    """A face's box scaled to 0..1 (Immich `/faces` or ML `boundingBox`)."""
    box = face.get("boundingBox") or face
    x1 = box.get("x1", box.get("boundingBoxX1", 0))
    y1 = box.get("y1", box.get("boundingBoxY1", 0))
    x2 = box.get("x2", box.get("boundingBoxX2", 0))
    y2 = box.get("y2", box.get("boundingBoxY2", 0))
    width = width or 1
    height = height or 1
    return (x1 / width, y1 / height, x2 / width, y2 / height)


def pick_face(
    faces: list[dict[str, Any]], target: tuple[float, float, float, float], least: float = 0.3
) -> list[float] | None:
    """The embedding of the ML face that overlaps `target` most (boxes 0..1)."""
    best: tuple[float, list[float]] | None = None
    for face in faces:
        overlap = iou(face["box"], target)
        if overlap >= least and (best is None or overlap > best[0]):
            best = (overlap, face["embedding"])
    return best[1] if best else None


def best_score(
    faces: list[list[float]],
    refs: list[tuple[datetime.datetime, list[float]]],
    when: datetime.datetime | None,
    days: float,
) -> float | None:
    """Best similarity of any face against the references within `days` of `when`.

    All references count when none is that close. None without faces or references.
    """
    if not faces or not refs:
        return None
    near = refs
    if when is not None:
        near = [r for r in refs if abs((r[0] - when).total_seconds()) <= days * 86400] or refs
    return max(dot(f, r[1]) for f in faces for r in near)


class MlApi:
    """Immich machine-learning server: faces with embeddings for one image."""

    def __init__(self, session: aiohttp.ClientSession, url: str, model: str = DEFAULT_MODEL,
                 min_score: float = 0.7) -> None:
        self._session = session
        self._url = url.rstrip("/") + "/predict"
        self._entries = json.dumps({
            "facial-recognition": {
                "detection": {"modelName": model, "options": {"minScore": min_score}},
                "recognition": {"modelName": model},
            }
        })

    async def faces(self, image: bytes) -> list[dict[str, Any]]:
        """[{box: (x1, y1, x2, y2) in 0..1, embedding: normalised}]."""
        form = aiohttp.FormData()
        form.add_field("entries", self._entries)
        form.add_field("image", image, filename="frame.jpg", content_type="image/jpeg")
        try:
            async with self._session.post(
                self._url, data=form, timeout=aiohttp.ClientTimeout(total=300)
            ) as resp:
                if resp.status >= 400:
                    raise MlError(f"predict: {resp.status} {(await resp.text())[:200]}")
                result = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise MlError(f"predict: {err}") from err
        width = result.get("imageWidth") or 1
        height = result.get("imageHeight") or 1
        out = []
        for face in result.get("facial-recognition") or []:
            emb = face.get("embedding")
            if isinstance(emb, str):
                emb = json.loads(emb)
            if not emb:
                continue
            out.append({"box": face_box(face, width, height),
                        "embedding": normalize([float(v) for v in emb])})
        return out


def ffmpeg_command() -> list[str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MlError("ffmpeg yok")
    nice = shutil.which("nice")
    return [nice, "-n", "19", ffmpeg] if nice else [ffmpeg]


async def extract_frame(command: list[str], path: str, at: float) -> bytes | None:
    """One JPEG frame at `at` seconds (longest side FRAME_SIZE), None if there is none."""
    scale = (
        f"scale='if(gt(iw,ih),min({FRAME_SIZE},iw),-2)':'if(gt(iw,ih),-2,min({FRAME_SIZE},ih))'"
    )
    proc = await asyncio.create_subprocess_exec(
        *command, "-nostdin", "-loglevel", "error", "-threads", "1",
        "-ss", str(at), "-i", path, "-frames:v", "1", "-vf", scale,
        "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MlError("ffmpeg timeout") from None
    if proc.returncode != 0:
        _LOGGER.debug("ffmpeg at %s: %s", at, err[:200])
        return None
    return out or None
