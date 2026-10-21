"""eufy Clean (Local): LAN control of eufy L60 robot vacuums."""

from __future__ import annotations

import asyncio
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .coordinator import EufyCoordinator

PLATFORMS = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.TIME,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.BINARY_SENSOR,
]

type EufyConfigEntry = ConfigEntry[EufyCoordinator]

FIRST_DATA_TIMEOUT = 15

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_URL = f"/{DOMAIN}/eufy-clean-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard card and have every dashboard load it (custom:eufy-clean-card)."""
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        return True  # no web interface (tests)
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = (await async_get_integration(hass, DOMAIN)).version
    card = Path(__file__).parent / "frontend" / "eufy-clean-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(card), True)])
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")  # the version makes browsers fetch each release
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry) -> bool:
    coordinator = EufyCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_start()

    # Wait briefly for the robot's first report so entities that depend on its
    # hardware (station, mop) are created correctly. If it is offline we carry on.
    first = asyncio.Event()
    unsub = coordinator.async_add_listener(lambda: first.set() if coordinator.data else None)
    try:
        await asyncio.wait_for(first.wait(), FIRST_DATA_TIMEOUT)
    except TimeoutError:
        pass
    finally:
        unsub()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EufyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: EufyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
