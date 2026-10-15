"""eufy Clean (Local): LAN control of eufy L60 robot vacuums."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

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
