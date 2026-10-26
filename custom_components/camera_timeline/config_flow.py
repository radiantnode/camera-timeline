"""Set up from the UI: which cameras, how often, how long to keep."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CAMERAS,
    CONF_INTERVAL,
    CONF_KEEP_HOURS,
    CONF_STREAM_CAMERAS,
    DEFAULT_INTERVAL,
    DEFAULT_KEEP_HOURS,
    DOMAIN,
)


def _schema(options: dict[str, Any]) -> vol.Schema:
    cameras = selector.EntitySelector(selector.EntitySelectorConfig(domain="camera", multiple=True))
    return vol.Schema({
        vol.Required(CONF_CAMERAS, default=options.get(CONF_CAMERAS, [])): cameras,
        vol.Optional(CONF_STREAM_CAMERAS, default=options.get(CONF_STREAM_CAMERAS, [])): cameras,
        vol.Required(CONF_INTERVAL, default=options.get(CONF_INTERVAL, DEFAULT_INTERVAL)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=3600, step=1, unit_of_measurement="s",
                                          mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_KEEP_HOURS, default=options.get(CONF_KEEP_HOURS, DEFAULT_KEEP_HOURS)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=168, step=1, unit_of_measurement="h",
                                          mode=selector.NumberSelectorMode.BOX)
        ),
    })


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_CAMERAS: list(user_input.get(CONF_CAMERAS, [])),
        CONF_STREAM_CAMERAS: list(user_input.get(CONF_STREAM_CAMERAS, [])),
        CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        CONF_KEEP_HOURS: int(user_input.get(CONF_KEEP_HOURS, DEFAULT_KEEP_HOURS)),
    }


class CameraTimelineConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Camera Timeline", data={}, options=_clean(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CameraTimelineOptionsFlow()


class CameraTimelineOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(step_id="init", data_schema=_schema(dict(self.config_entry.options)))
