"""Set up from the UI: which cameras, how often, how long to keep."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CAMERAS,
    CONF_INTERVAL,
    CONF_KEEP_HOURS,
    CONF_QUIET_CAMERAS,
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
        vol.Optional(CONF_QUIET_CAMERAS, default=options.get(CONF_QUIET_CAMERAS, [])): cameras,
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
        CONF_QUIET_CAMERAS: list(user_input.get(CONF_QUIET_CAMERAS, [])),
        CONF_INTERVAL: int(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        CONF_KEEP_HOURS: int(user_input.get(CONF_KEEP_HOURS, DEFAULT_KEEP_HOURS)),
    }


def _errors(user_input: dict[str, Any] | None) -> dict[str, str]:
    if user_input is not None and not user_input.get(CONF_CAMERAS):
        return {CONF_CAMERAS: "no_cameras"}
    return {}


class CameraTimelineConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = _errors(user_input)
        if user_input is not None and not errors:
            return self.async_create_entry(title="Camera Timeline", data={}, options=_clean(user_input))
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return CameraTimelineOptionsFlow()


class CameraTimelineOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = _errors(user_input)
        if user_input is not None and not errors:
            return self.async_create_entry(data=_clean(user_input))
        return self.async_show_form(step_id="init", data_schema=_schema(user_input or dict(self.config_entry.options)),
                                    errors=errors)
