"""Do-not-disturb start and end times."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.legacy:
        return
    async_add_entities([EufyDndTime(coordinator, "dnd_start"), EufyDndTime(coordinator, "dnd_end")])


class EufyDndTime(EufyEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> time | None:
        value = self.value
        return time(*value) if value else None

    async def async_set_value(self, value: time) -> None:
        hm = (value.hour, value.minute)
        dps = self.coordinator.dnd_dps(start=hm) if self.key == "dnd_start" else self.coordinator.dnd_dps(end=hm)
        await self.coordinator.async_send(dps)
