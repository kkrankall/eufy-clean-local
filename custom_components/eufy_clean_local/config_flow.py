"""Config flow: log in to eufy once to get the robot's local key, then pick the robot."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_NAME, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import codec as c
from . import legacy
from .const import (
    CONF_DEVICE_ID,
    CONF_LIVE_INTERVAL,
    CONF_LOCAL_KEY,
    CONF_MAC,
    CONF_MAP_INTERVAL,
    CONF_MODEL,
    CONF_ROOMS,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_MAP_INTERVAL,
    DOMAIN,
)
from .eufy_cloud import SESSIONS, EufyAuthError, EufyCloud

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
    vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
})


class EufyCleanLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._devices: list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                cloud = EufyCloud(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                devices = await self.hass.async_add_executor_job(cloud.devices)
                SESSIONS[user_input[CONF_EMAIL]] = cloud
            except EufyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("eufy login failed")
                errors["base"] = "cannot_connect"
            else:
                self._credentials = user_input
                configured = {e.unique_id for e in self._async_current_entries()}
                self._devices = [d for d in devices if d["local_key"] and d["id"] not in configured
                                 and (d["model"] in c.L60_MODELS or d["model"] in legacy.MODELS)]
                if not self._devices:
                    others = ", ".join(f"{d['name']} ({d['model']})" for d in devices) or "none"
                    _LOGGER.warning("No supported robot found; robots on the account: %s", others)
                    errors["base"] = "no_devices"
                elif len(self._devices) == 1:
                    return await self._async_create(self._devices[0])
                else:
                    return await self.async_step_pick()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            device = next(d for d in self._devices if d["id"] == user_input[CONF_DEVICE_ID])
            return await self._async_create(device)
        options = [
            SelectOptionDict(value=d["id"], label=f"{d['name']} "
                             f"({c.L60_MODELS.get(d['model']) or legacy.MODELS.get(d['model'], d['model'])})")
            for d in self._devices
        ]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_ID): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def _async_create(self, device: dict) -> ConfigFlowResult:
        await self.async_set_unique_id(device["id"])
        self._abort_if_unique_id_configured(updates={CONF_LOCAL_KEY: device["local_key"]})
        return self.async_create_entry(
            title=device["name"],
            data={
                **self._credentials,
                CONF_DEVICE_ID: device["id"],
                CONF_LOCAL_KEY: device["local_key"],
                CONF_MODEL: device["model"],
                CONF_NAME: device["name"],
                CONF_MAC: device.get("mac"),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """eufy rejected the stored login (e.g. the password changed)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = entry.data[CONF_EMAIL]
            try:
                devices = await self.hass.async_add_executor_job(
                    lambda: EufyCloud(email, user_input[CONF_PASSWORD]).devices())
            except EufyAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("eufy login failed")
                errors["base"] = "cannot_connect"
            else:
                updates = {CONF_PASSWORD: user_input[CONF_PASSWORD]}
                device = next((d for d in devices if d["id"] == entry.data[CONF_DEVICE_ID]), None)
                if device and device["local_key"]:
                    updates[CONF_LOCAL_KEY] = device["local_key"]
                return self.async_update_reload_and_abort(entry, data_updates=updates)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD))}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EufyCleanLocalOptionsFlow()


class EufyCleanLocalOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_HOST, description={"suggested_value": opts.get(CONF_HOST, "")}): str,
                vol.Required(CONF_MAP_INTERVAL, default=opts.get(CONF_MAP_INTERVAL, DEFAULT_MAP_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                vol.Required(CONF_LIVE_INTERVAL, default=opts.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Any(0, vol.Range(min=2, max=10))),
                vol.Optional(CONF_ROOMS, description={"suggested_value": opts.get(CONF_ROOMS, "")}):
                    TextSelector(TextSelectorConfig(multiline=True)),
            }),
        )
