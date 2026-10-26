"""The timeline of the cameras it's given: a picture on a schedule, a burst at
every event, and what's asked for in between, kept for a while.

Each camera is captured one way at a time (a lock per camera), so a look that
comes during a scheduled picture waits for it and then finds the camera awake.
"""
from __future__ import annotations

import asyncio
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import os
import time
from typing import Any

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import capture, store
from .const import (
    BURST_FRAMES,
    BURST_GAP_S,
    CAPTURE_TIMEOUT_S,
    CLIP_DELAY_S,
    CLIP_TIMEOUT_S,
    CONF_CAMERAS,
    CONF_INTERVAL,
    CONF_KEEP_HOURS,
    CONF_QUIET_CAMERAS,
    CONF_STREAM_CAMERAS,
    DEFAULT_INTERVAL,
    EVENT_COOLDOWN_S,
    DEFAULT_KEEP_HOURS,
    EVENT_DEVICE_CLASSES,
    SELF_TRIGGER_S,
    STALE_EVENT_S,
    STREAM_PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)

#: A doorbell's press events, all of which are its ring.
PRESSES = {"single_press", "double_press", "long_press", "press", "pressed", "ring", "ding"}


@dataclass
class Camera:
    entity_id: str
    object_id: str
    stream: bool
    platform: str | None
    #: Pictured only at events and looks, never on the schedule.
    quiet: bool = False
    #: Entities whose change is an event on this camera, with the kind of event.
    triggers: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_capture: float = 0.0
    #: The wake being taken right now, so an event during it is marked on it.
    taking: str | None = None
    #: Its stream, for a camera captured from one.
    reader: capture.StreamReader | None = None
    #: Its last event burst, and when, for the cooldown.
    last_event: tuple[float, str] | None = None
    #: When the timeline last began taking a picture of it, to tell its own
    #: doing from an event.
    capture_started: float = 0.0


class Timeline:
    """Every camera on the timeline, and the work for each."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        options = entry.options
        self.interval = int(options.get(CONF_INTERVAL, DEFAULT_INTERVAL))
        self.keep_s = float(options.get(CONF_KEEP_HOURS, DEFAULT_KEEP_HOURS)) * 3600
        self.root = hass.config.path()
        self.ffmpeg = get_ffmpeg_manager(hass).binary
        self.cameras: dict[str, Camera] = {}
        self._unsubs: list = []
        self._tasks: set[asyncio.Task] = set()
        forced = set(options.get(CONF_STREAM_CAMERAS, []))
        quiet = set(options.get(CONF_QUIET_CAMERAS, []))
        for entity_id in options.get(CONF_CAMERAS, []):
            self.cameras[entity_id] = self._describe(entity_id, forced)
            self.cameras[entity_id].quiet = entity_id in quiet

    # ---- set-up ------------------------------------------------------------

    def _describe(self, entity_id: str, forced: set[str]) -> Camera:
        """The camera, how it's captured, and the entities on its device whose
        changes count as events."""
        entities = er.async_get(self.hass)
        entry = entities.async_get(entity_id)
        platform = entry.platform if entry else None
        camera = Camera(entity_id, entity_id.split(".", 1)[1], entity_id in forced or platform in STREAM_PLATFORMS,
                        platform)
        if entry and entry.device_id:
            for other in er.async_entries_for_device(entities, entry.device_id):
                if other.disabled_by:
                    continue
                domain = other.entity_id.split(".", 1)[0]
                if domain == "event":
                    camera.triggers[other.entity_id] = "event"
                elif domain == "binary_sensor":
                    device_class = other.device_class or other.original_device_class
                    if device_class in EVENT_DEVICE_CLASSES:
                        camera.triggers[other.entity_id] = _kind(other.entity_id, device_class, camera.object_id)
        return camera

    async def async_start(self) -> None:
        cameras = list(self.cameras.values())
        scheduled = [c for c in cameras if not c.quiet]
        if self.interval > 0 and scheduled:
            # Spread over the interval, so the cameras don't all wake at once.
            for i, camera in enumerate(scheduled):
                delay = self.interval * i / len(scheduled)
                self._unsubs.append(self.hass.loop.call_later(delay, self._schedule, camera).cancel)
        triggers = {t: c for c in cameras for t in c.triggers}
        if triggers:
            self._unsubs.append(async_track_state_change_event(self.hass, list(triggers), self._on_trigger))
        self._unsubs.append(async_track_time_interval(self.hass, self._prune, timedelta(minutes=10)))
        await self._prune()

    @callback
    def _schedule(self, camera: Camera) -> None:
        @callback
        def tick(_now) -> None:
            self._spawn(self._sample(camera))

        self._unsubs.append(async_track_time_interval(self.hass, tick, timedelta(seconds=self.interval)))
        tick(None)

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for camera in self.cameras.values():
            if camera.reader is not None:
                await camera.reader.close()

    def _spawn(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, "camera_timeline")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- capturing ---------------------------------------------------------

    async def _capture(self, camera: Camera, frames: int, reason: str, event: dict | None = None
                       ) -> tuple[float, list[tuple[float, bytes]], str]:
        """Take `frames` pictures and keep them as a wake. Returns when it was
        taken, the frames and its folder."""
        async with camera.lock:
            woken = time.time()
            camera.capture_started = woken
            folder_name = f"{int(woken * 1000)}"
            camera.taking = folder_name
            try:
                if camera.stream:
                    if camera.reader is None:
                        camera.reader = capture.StreamReader(self.hass, self.ffmpeg, camera.entity_id)
                    taken = await camera.reader.frames(frames, BURST_GAP_S, CAPTURE_TIMEOUT_S)
                else:
                    taken = await capture.from_snapshot(self.hass, camera.entity_id, frames, BURST_GAP_S)
            finally:
                camera.taking = None
            folder = await self.hass.async_add_executor_job(
                store.save_wake, self.root, camera.object_id, woken, taken, reason, event
            )
            camera.last_capture = time.time()
            return woken, taken, folder

    async def _sample(self, camera: Camera) -> None:
        """The schedule's picture, unless the camera was captured since the
        last one anyway."""
        if camera.lock.locked() or time.time() - camera.last_capture < self.interval * 0.8:
            return
        try:
            await self._capture(camera, 1, "sample")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: scheduled picture failed: %s", camera.entity_id, err)

    @callback
    def _on_trigger(self, change: Event[EventStateChangedData]) -> None:
        entity_id = change.data["entity_id"]
        new, old = change.data.get("new_state"), change.data.get("old_state")
        camera = next((c for c in self.cameras.values() if entity_id in c.triggers), None)
        if camera is None or new is None or new.state in ("unavailable", "unknown"):
            return
        # Coming back from unavailable is Home Assistant (or the integration)
        # starting and restoring the state, not anything happening.
        if old is None or old.state in ("unavailable", "unknown"):
            return
        kind = camera.triggers[entity_id]
        if kind == "event":
            # An event entity's state is the time of its newest event.
            if old.state == new.state:
                return
            fired = dt_util.parse_datetime(new.state)
            if fired is None or time.time() - fired.timestamp() > STALE_EVENT_S:
                return
            kind = str(new.attributes.get("event_type") or "event")
            if "doorbell" in entity_id and kind in PRESSES:
                kind = "doorbell"
        elif not (new.state == "on" and old.state != "on"):
            return
        elif time.time() - camera.capture_started < SELF_TRIGGER_S:
            # The picture being taken tripped it.
            _LOGGER.debug("%s: %s tripped by its own picture; not an event", camera.entity_id, entity_id)
            return
        self._spawn(self._event(camera, {"type": kind, "entity_id": entity_id}))

    async def _event(self, camera: Camera, event: dict) -> None:
        """A burst for an event, or the event marked on the wake being taken
        right now; then, from a Eufy HomeBase, its own recording."""
        _LOGGER.debug("%s: %s from %s", camera.entity_id, event["type"], event["entity_id"])
        folder = None
        if camera.taking:
            folder = os.path.join(store.camera_folder(self.root, camera.object_id), camera.taking)
        elif camera.last_event and time.time() - camera.last_event[0] < EVENT_COOLDOWN_S:
            # Still the same goings-on: say so on the burst already taken.
            await self.hass.async_add_executor_job(store.mark_event, camera.last_event[1], event)
            return
        try:
            if folder is None:
                _, _, folder = await self._capture(camera, BURST_FRAMES, "event", event)
                camera.last_event = (time.time(), folder)
            else:
                async with camera.lock:  # until the wake being taken is written
                    pass
                await self.hass.async_add_executor_job(store.mark_event, folder, event)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: event burst failed: %s", camera.entity_id, err)
            return
        if camera.platform == "eufy_sdk":
            await self._clip(camera, folder)

    async def _clip(self, camera: Camera, folder: str) -> None:
        """The detection's recording from an eufy-sdk bridge (HomeBase 2)."""
        bridge = self._eufy_bridge(camera)
        if bridge is None:
            return
        await asyncio.sleep(CLIP_DELAY_S)
        url = f"http://{bridge[0]}:{bridge[1]}/clip/{bridge[2]}"
        try:
            async with async_get_clientsession(self.hass).get(url, timeout=CLIP_TIMEOUT_S) as response:
                if response.status != 200 or "video" not in response.content_type:
                    _LOGGER.debug("%s: no clip (%s)", camera.entity_id, response.status)
                    return
                data = await response.read()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s: clip fetch failed: %s", camera.entity_id, err)
            return
        await self.hass.async_add_executor_job(store.attach_clip, folder, data)

    def _eufy_bridge(self, camera: Camera) -> tuple[str, int, str] | None:
        """The bridge's host, port and the camera's serial, from the eufy_sdk
        entry and the camera's stream address (which ends in its serial)."""
        entries = self.hass.config_entries.async_entries("eufy_sdk")
        state = self.hass.states.get(camera.entity_id)
        if not entries or state is None:
            return None
        data = entries[0].data
        entity = er.async_get(self.hass).async_get(camera.entity_id)
        serial = None
        if entity and entity.device_id:
            device = dr.async_get(self.hass).async_get(entity.device_id)
            serial = next((i[1] for i in (device.identifiers if device else ()) if i[0] == "eufy_sdk"), None)
        if not serial or "host" not in data or "port" not in data:
            return None
        return str(data["host"]), int(data["port"]), str(serial)

    async def _prune(self, _now=None) -> None:
        now = time.time()
        for camera in self.cameras.values():
            await self.hass.async_add_executor_job(store.prune, self.root, camera.object_id, self.keep_s, now)

    # ---- services ----------------------------------------------------------

    def camera(self, entity_id: str) -> Camera:
        if entity_id not in self.cameras:
            raise ServiceValidationError(f"{entity_id} isn't on the camera timeline; add it in the integration's options")
        return self.cameras[entity_id]

    async def snapshot(self, entity_id: str, burst: bool, width: int) -> dict[str, Any]:
        """A picture taken now, or a burst: kept on the timeline and answered."""
        camera = self.camera(entity_id)
        try:
            woken, taken, _ = await self._capture(camera, BURST_FRAMES if burst else 1, "look")
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"{entity_id} didn't give a picture: {err}") from err
        frames = []
        for offset, picture in taken:
            sized = await self.hass.async_add_executor_job(store.resized, picture, width)
            frames.append({"offset": offset, "image": b64encode(sized).decode()})
        return {"time": store.iso(woken), "frames": frames}

    async def get_frames(self, entity_id: str, wakes: int, frames: int, width: int, minutes: int, most: int,
                         since: float | None, until: float | None, events_only: bool, wait: bool) -> dict[str, Any]:
        camera = self.camera(entity_id)
        if wait and camera.lock.locked():
            async with camera.lock:
                pass
        if minutes and since is None:
            since = time.time() - minutes * 60
        kept = await self.hass.async_add_executor_job(
            store.load_wakes, self.root, camera.object_id, wakes, width, frames or None, since, until, most or None,
            events_only,
        )
        answer: dict[str, Any] = {"wakes": kept}
        if since is not None:
            answer["times"] = await self.hass.async_add_executor_job(
                store.wake_times, self.root, camera.object_id, since, until
            )
        return answer

    async def get_events(self, entity_id: str | None, hours: float) -> dict[str, Any]:
        since = time.time() - hours * 3600
        cameras = [self.camera(entity_id)] if entity_id else list(self.cameras.values())
        out = []
        for camera in cameras:
            for event in await self.hass.async_add_executor_job(store.events, self.root, camera.object_id, since):
                out.append({"camera": camera.entity_id, **event})
        out.sort(key=lambda e: e["time"] or "", reverse=True)
        return {"events": out}


def _kind(entity_id: str, device_class: str, camera_object_id: str) -> str:
    """What an event sensor detects, read off its name where it says
    ("patio_person" is a person) and its device class where it doesn't."""
    tail = entity_id.split(".", 1)[1]
    for prefix in (camera_object_id + "_", camera_object_id.rsplit("_", 1)[0] + "_"):
        if tail.startswith(prefix):
            tail = tail[len(prefix):]
            break
    for word in ("person", "pet", "dog", "vehicle", "package", "doorbell", "ring", "sound", "motion"):
        if word in tail:
            return word
    return device_class
