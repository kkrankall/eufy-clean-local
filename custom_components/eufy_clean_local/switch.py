"""Robot settings that are on/off."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from . import codec as c
from .entity import EufyEntity, add_when_station

SWITCHES = {
    "boost_iq": ("mdi:rocket-launch", lambda co, on: {c.DP_BOOST_IQ: on}),
    "auto_return": ("mdi:battery-sync", lambda co, on: {c.DP_AUTO_RETURN: on}),
    "child_lock": ("mdi:lock", lambda co, on: c.cmd_child_lock(on)),
    "dnd": ("mdi:moon-waning-crescent", lambda co, on: co.dnd_dps(on=on)),
}
STATION_SWITCHES = {
    "auto_empty": ("mdi:delete-restore",
                   lambda co, on: c.cmd_auto_empty(on, int((co.data or {}).get("auto_empty_every") or 1))),
    "auto_cut_hair": ("mdi:content-cut", lambda co, on: c.cmd_auto_cut_hair(on)),
}


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.legacy:
        return
    async_add_entities(EufySwitch(coordinator, key) for key in SWITCHES)
    if coordinator.cloud is not None:
        async_add_entities([EufyRoomCustomSwitch(coordinator)])
    add_when_station(entry, coordinator, async_add_entities,
                     lambda: [EufySwitch(coordinator, key) for key in STATION_SWITCHES])


class EufySwitch(EufyEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_icon, self._build = SWITCHES.get(key) or STATION_SWITCHES[key]

    @property
    def is_on(self) -> bool | None:
        return self.value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send(self._build(self.coordinator, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send(self._build(self.coordinator, False))


class EufyRoomCustomSwitch(EufyEntity, SwitchEntity):
    """Use each room's own suction and passes (set on the per-room selects) instead of the global ones."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:home-edit-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "room_custom")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.room_params() is not None

    @property
    def is_on(self) -> bool | None:
        params = self.coordinator.room_params()
        return None if params is None else params.custom_enable

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_rooms_custom(enable=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_rooms_custom(enable=False)
