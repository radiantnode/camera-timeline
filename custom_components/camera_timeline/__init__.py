"""Camera Timeline: what your cameras saw, kept for a while.

A picture of each camera on a schedule, a burst at every motion, person or
doorbell event (and the camera's own recording, from an eufy-sdk bridge), and
services that take a picture now or look back through what's kept.
"""
from __future__ import annotations

from datetime import datetime
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .timeline import Timeline

_LOGGER = logging.getLogger(__name__)

type TimelineConfigEntry = ConfigEntry[Timeline]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SNAPSHOT_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("burst", default=False): cv.boolean,
    vol.Optional("width", default=1280): vol.All(vol.Coerce(int), vol.Range(min=160, max=1920)),
})
GET_FRAMES_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("wakes", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    vol.Optional("frames", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=5)),
    vol.Optional("width", default=960): vol.All(vol.Coerce(int), vol.Range(min=160, max=1920)),
    vol.Optional("minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=7 * 24 * 60)),
    vol.Optional("since"): cv.datetime,
    vol.Optional("until"): cv.datetime,
    vol.Optional("most", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=500)),
    vol.Optional("events_only", default=False): cv.boolean,
    vol.Optional("wait", default=True): cv.boolean,
})
GET_EVENTS_SCHEMA = vol.Schema({
    vol.Optional("entity_id"): cv.entity_id,
    vol.Optional("hours", default=24): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=24 * 7)),
})


def _epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    return dt_util.as_utc(value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())).timestamp()


def _timeline(hass: HomeAssistant) -> Timeline:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("Camera Timeline isn't set up")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config) -> bool:
    from .media_source import TimelineFileView  # noqa: PLC0415 — needs http, a dependency

    hass.http.register_view(TimelineFileView(hass))

    async def snapshot(call: ServiceCall) -> ServiceResponse:
        d = call.data
        return await _timeline(hass).snapshot(d["entity_id"], d["burst"], d["width"])

    async def get_frames(call: ServiceCall) -> ServiceResponse:
        d = call.data
        return await _timeline(hass).get_frames(
            d["entity_id"], d["wakes"], d["frames"], d["width"], d["minutes"], d["most"],
            _epoch(d.get("since")), _epoch(d.get("until")), d["events_only"], d["wait"],
        )

    async def get_events(call: ServiceCall) -> ServiceResponse:
        return await _timeline(hass).get_events(call.data.get("entity_id"), call.data["hours"])

    hass.services.async_register(DOMAIN, "snapshot", snapshot, SNAPSHOT_SCHEMA, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "get_frames", get_frames, GET_FRAMES_SCHEMA, SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "get_events", get_events, GET_EVENTS_SCHEMA, SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TimelineConfigEntry) -> bool:
    timeline = Timeline(hass, entry)
    entry.runtime_data = timeline
    await timeline.async_start()
    entry.async_on_unload(entry.add_update_listener(_options_changed))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TimelineConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True


async def _options_changed(hass: HomeAssistant, entry: TimelineConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
