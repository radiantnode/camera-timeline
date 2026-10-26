"""The timeline in the media browser: each camera, its wakes newest first
(events named), and each wake's frames and recording."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import store
from .const import DOMAIN

#: How long an address the browser hands out stays good.
SIGNED_FOR = timedelta(hours=1)
#: Wakes listed per camera, newest first.
LISTED = 300


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    hass.http.register_view(TimelineFileView(hass))
    return TimelineMediaSource(hass)


def _path(object_id: str, wake: str, name: str) -> str:
    return f"/api/{DOMAIN}/{object_id}/{wake}/{name}"


def _label(woken: float) -> str:
    at = dt_util.as_local(datetime.fromtimestamp(woken, UTC))
    clock = at.strftime("%I:%M:%S %p").lstrip("0")
    return clock if at.date() == dt_util.now().date() else f"{at.strftime('%b %d')}, {clock}"


class TimelineMediaSource(MediaSource):
    name = "Camera Timeline"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _cameras(self) -> dict[str, str]:
        """Object id to name, for every camera on the timeline."""
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            return {}
        out = {}
        for camera in entries[0].runtime_data.cameras.values():
            state = self.hass.states.get(camera.entity_id)
            out[camera.object_id] = state.name if state else camera.entity_id
        return out

    def _sign(self, path: str) -> str:
        return async_sign_path(self.hass, path, SIGNED_FOR)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 3 or parts[0] not in self._cameras() or not parts[1].isdigit():
            raise Unresolvable(f"Not on the timeline: {item.identifier}")
        object_id, wake, name = parts
        if name == "clip.mp4":
            return PlayMedia(_path(object_id, wake, name), "video/mp4")
        if not name.endswith(".jpg") or not name[:-4].isdigit():
            raise Unresolvable(f"Not on the timeline: {item.identifier}")
        return PlayMedia(_path(object_id, wake, name), "image/jpeg")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        cameras = self._cameras()
        parts = [p for p in (item.identifier or "").split("/") if p]
        if not parts:
            children = [self._node(oid, name, MediaClass.DIRECTORY, can_expand=True) for oid, name in cameras.items()]
            return self._node(None, self.name, MediaClass.DIRECTORY, children=children, can_expand=True)
        object_id = parts[0]
        if object_id not in cameras:
            raise BrowseError(f"No camera {object_id} on the timeline")
        root = self.hass.config.path()
        if len(parts) == 1:
            folders = (await self.hass.async_add_executor_job(store.wakes, root, object_id))[:LISTED]
            children = []
            for folder in folders:
                meta = await self.hass.async_add_executor_job(store.read_meta, folder)
                if not meta or not meta.get("offsets"):
                    continue
                wake = os.path.basename(folder)
                event = meta.get("event") or {}
                title = _label(int(wake) / 1000) + (f" · {event['type']}" if event else "")
                if meta.get("clip"):
                    title += " · recording"
                last = len(meta["offsets"]) - 1
                children.append(self._node(f"{object_id}/{wake}", title, MediaClass.DIRECTORY, can_expand=True,
                                           thumbnail=self._sign(_path(object_id, wake, f"{last}.jpg"))))
            return self._node(object_id, cameras[object_id], MediaClass.DIRECTORY, children=children, can_expand=True)
        wake = parts[1]
        if not wake.isdigit():
            raise BrowseError(f"Not a wake: {item.identifier}")
        folder = os.path.join(store.camera_folder(root, object_id), wake)
        meta = await self.hass.async_add_executor_job(store.read_meta, folder)
        if not meta:
            raise BrowseError(f"No wake {wake} kept for {cameras[object_id]}")
        children = []
        if meta.get("clip"):
            children.append(self._node(f"{object_id}/{wake}/clip.mp4", "Recording", MediaClass.VIDEO,
                                       media_type="video/mp4", can_play=True))
        for i in range(len(meta.get("offsets") or [])):
            children.append(self._node(f"{object_id}/{wake}/{i}.jpg", f"Frame {i + 1}", MediaClass.IMAGE,
                                       media_type="image/jpeg", can_play=True,
                                       thumbnail=self._sign(_path(object_id, wake, f"{i}.jpg"))))
        return self._node(f"{object_id}/{wake}", f"{cameras[object_id]}, {_label(int(wake) / 1000)}",
                          MediaClass.DIRECTORY, children=children, can_expand=True)

    def _node(self, identifier: str | None, title: str, media_class: str, *, children=None, can_expand=False,
              can_play=False, media_type: str = "", thumbnail: str | None = None) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN, identifier=identifier, media_class=media_class, media_content_type=media_type,
            title=title, can_play=can_play, can_expand=can_expand, children=children, thumbnail=thumbnail,
        )


class TimelineFileView(HomeAssistantView):
    """A kept frame or recording, to someone signed in or holding a signed address."""

    url = f"/api/{DOMAIN}/{{object_id:[a-z0-9_]+}}/{{wake:\\d+}}/{{name:(\\d+\\.jpg|clip\\.mp4)}}"
    name = f"api:{DOMAIN}:file"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, object_id: str, wake: str, name: str) -> web.StreamResponse:
        path = os.path.join(store.camera_folder(self.hass.config.path(), object_id), wake, name)
        if not await self.hass.async_add_executor_job(os.path.isfile, path):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
