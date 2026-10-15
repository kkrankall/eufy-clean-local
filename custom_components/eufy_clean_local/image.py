"""The robot's map, rendered from the latest cloud snapshot."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.cloud is not None and not coordinator.legacy:
        async_add_entities([EufyMapImage(hass, coordinator)])


class EufyMapImage(EufyEntity, ImageEntity):
    _attr_content_type = "image/png"
    _attr_icon = "mdi:floor-plan"

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        EufyEntity.__init__(self, coordinator, "map")
        ImageEntity.__init__(self, hass)
        self._shown: datetime | None = coordinator.map_updated
        self._attr_image_last_updated = coordinator.map_updated

    @property
    def available(self) -> bool:
        return self.coordinator.map_png is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.map_updated != self._shown:
            self._shown = self.coordinator.map_updated
            self._attr_image_last_updated = self._shown
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return self.coordinator.map_png

    @property
    def extra_state_attributes(self) -> dict:
        return {"map_id": self.coordinator.map_id, "rooms": [r["name"] for r in self.coordinator.rooms]}
