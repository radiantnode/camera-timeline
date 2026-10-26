"""The timeline on disk: one folder per capture ("wake"), newest last.

<config>/camera_timeline/<camera object id>/<epoch ms>/
    0.jpg 1.jpg ...   the frames, KEEP_WIDTH wide
    wake.json         {"time", "offsets", "reason", "event", "clip"}
    clip.mp4          the camera's own recording of an event, when there is one

`reason` is why it was taken: "sample" (the schedule), "look" (someone asked)
or "event" (a detection), and `event` says which.

Plain functions over the filesystem, run in the executor; the manager does the
scheduling.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import io
import json
import os
import shutil
import time

from .const import FOLDER, KEEP_WIDTH


def camera_folder(root: str, object_id: str) -> str:
    return os.path.join(root, FOLDER, object_id)


def wake_time(folder: str) -> float:
    """When a wake was, from its folder's name (milliseconds)."""
    return int(os.path.basename(folder)) / 1000


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def wakes(root: str, object_id: str) -> list[str]:
    """This camera's wake folders, newest first."""
    base = camera_folder(root, object_id)
    if not os.path.isdir(base):
        return []
    names = sorted((n for n in os.listdir(base) if n.isdigit()), key=int, reverse=True)
    return [os.path.join(base, n) for n in names]


def read_meta(folder: str) -> dict | None:
    try:
        with open(os.path.join(folder, "wake.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_meta(folder: str, meta: dict) -> None:
    path = os.path.join(folder, "wake.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(path + ".tmp", path)


def resized(data: bytes, width: int) -> bytes:
    """A JPEG no wider than `width`, as JPEG (quality 75, as Home Assistant's
    own camera proxy scales)."""
    from PIL import Image  # noqa: PLC0415 — Home Assistant ships Pillow

    image = Image.open(io.BytesIO(data))
    if image.width <= width:
        return data
    height = round(image.height * width / image.width)
    out = io.BytesIO()
    image.convert("RGB").resize((width, height), Image.LANCZOS).save(out, "JPEG", quality=75)
    return out.getvalue()


def save_wake(root: str, object_id: str, woken: float, frames: list[tuple[float, bytes]],
              reason: str, event: dict | None = None) -> str:
    """Write one wake's frames and its description. Returns its folder."""
    folder = os.path.join(camera_folder(root, object_id), f"{int(woken * 1000)}")
    os.makedirs(folder, exist_ok=True)
    for i, (_, data) in enumerate(frames):
        with open(os.path.join(folder, f"{i}.jpg"), "wb") as f:
            f.write(resized(data, KEEP_WIDTH))
    _write_meta(folder, {
        "time": iso(woken),
        "offsets": [offset for offset, _ in frames],
        "reason": reason,
        "event": event,
        "clip": None,
    })
    return folder


def mark_event(folder: str, event: dict) -> None:
    """Say a wake also caught an event: as its event if it had none, else
    counted among the ones that came after (`also`)."""
    meta = read_meta(folder)
    if meta is None:
        return
    if not meta.get("event"):
        meta["event"] = event
        meta["reason"] = "event"
    else:
        meta.setdefault("also", []).append({**event, "time": iso(time.time())})
    _write_meta(folder, meta)


def attach_clip(folder: str, data: bytes) -> None:
    with open(os.path.join(folder, "clip.mp4"), "wb") as f:
        f.write(data)
    meta = read_meta(folder)
    if meta is not None:
        meta["clip"] = "clip.mp4"
        _write_meta(folder, meta)


def prune(root: str, object_id: str, keep_s: float, now: float) -> int:
    """Remove wakes older than `keep_s`, always keeping the newest. Returns how
    many went."""
    gone = 0
    for i, folder in enumerate(wakes(root, object_id)):
        if i and now - wake_time(folder) > keep_s:
            shutil.rmtree(folder, ignore_errors=True)
            gone += 1
    return gone


def remove_camera(root: str, object_id: str) -> None:
    shutil.rmtree(camera_folder(root, object_id), ignore_errors=True)


def spread(items: list, most: int) -> list:
    """At most `most` of `items`, evenly spread, keeping the first and last."""
    if len(items) <= most:
        return items
    if most == 1:
        return items[:1]
    step = (len(items) - 1) / (most - 1)
    return [items[round(i * step)] for i in range(most)]


def newest_frame(root: str, object_id: str) -> tuple[float, bytes] | None:
    """The last frame of this camera's newest wake, and when it was taken."""
    for folder in wakes(root, object_id):
        meta = read_meta(folder)
        if not meta or not meta.get("offsets"):
            continue
        try:
            with open(os.path.join(folder, f"{len(meta['offsets']) - 1}.jpg"), "rb") as f:
                return wake_time(folder), f.read()
        except OSError:
            continue
    return None


def _in_window(folder: str, since: float | None, until: float | None) -> bool:
    t = wake_time(folder)
    return (since is None or t >= since) and (until is None or t <= until)


def load_wakes(root: str, object_id: str, count: int, width: int, per_wake: int | None = None,
               since: float | None = None, until: float | None = None, most: int | None = None,
               events_only: bool = False) -> list[dict]:
    """The newest `count` wakes, or every wake between `since` and `until`
    (epoch seconds), newest first, as the service answers them: when, why,
    which event, whether there's a clip, and each frame as base64 JPEG with its
    offset. `per_wake` keeps that many frames from the middle of each; `most`
    thins the wakes, evenly spread, before any picture is read."""
    folders = wakes(root, object_id)
    if since is not None or until is not None:
        folders = [f for f in folders if _in_window(f, since, until)]
    if events_only:
        folders = [f for f in folders if (read_meta(f) or {}).get("event")]
    if since is None and until is None:
        folders = folders[:count]
    if most:
        folders = spread(folders, most)
    out = []
    for folder in folders:
        meta = read_meta(folder)
        if meta is None:
            continue
        offsets = meta.get("offsets") or []
        picked = list(range(len(offsets)))
        if per_wake and per_wake < len(picked):
            mid = len(picked) // 2
            picked = picked[max(0, mid - per_wake // 2):][:per_wake]
        frames = []
        for i in picked:
            try:
                with open(os.path.join(folder, f"{i}.jpg"), "rb") as f:
                    data = f.read()
            except OSError:
                continue
            frames.append({"offset": offsets[i], "image": base64.b64encode(resized(data, width)).decode()})
        if frames:
            out.append({
                "time": meta.get("time"),
                "reason": meta.get("reason"),
                "event": meta.get("event"),
                "clip": bool(meta.get("clip")),
                "frames": frames,
            })
    return out


def wake_times(root: str, object_id: str, since: float, until: float | None = None) -> list[str]:
    """When every wake in the window was, newest first, from folder names
    alone, so a thinned answer can still say where nothing was kept."""
    return [iso(wake_time(f)) for f in wakes(root, object_id) if _in_window(f, since, until)]


def events(root: str, object_id: str, since: float) -> list[dict]:
    """The events this camera caught since `since`, newest first."""
    out = []
    for folder in wakes(root, object_id):
        if wake_time(folder) < since:
            break
        meta = read_meta(folder)
        if meta and meta.get("event"):
            out.append({"time": meta.get("time"), "clip": bool(meta.get("clip")), "repeats": len(meta.get("also") or []),
                        **meta["event"]})
    return out
