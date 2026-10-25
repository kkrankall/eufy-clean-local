"""The robot's map, rendered from the latest cloud snapshot, following the robot while it cleans.

Each live frame (every few seconds while cleaning) is a new image, which the
dashboard loads as an ordinary picture. That works everywhere, including Safari,
whose service worker breaks the live camera's endless stream. The price is a
state change per frame while the robot cleans.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.cloud is None or coordinator.legacy:
        return
    async_add_entities([EufyMapImage(hass, coordinator)])

    # With two or more floors, each also gets its own picture (its last map), so floors the
    # robot isn't on can be shown.
    added: set[int] = set()

    @callback
    def add_floors(*_args) -> None:
        if len(coordinator.floors) < 2:
            return
        new = [map_id for map_id in sorted(coordinator.floors) if map_id not in added]
        if new:
            added.update(new)
            async_add_entities(EufyFloorImage(hass, coordinator, map_id) for map_id in new)

    add_floors()
    entry.async_on_unload(async_dispatcher_connect(hass, coordinator.floor_signal, add_floors))


class EufyMapImage(EufyEntity, ImageEntity):
    _attr_content_type = "image/png"
    _attr_icon = "mdi:floor-plan"
    _unrecorded_attributes = frozenset({"room_shapes"})

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        EufyEntity.__init__(self, coordinator, "map")
        ImageEntity.__init__(self, hass)
        self._shown: datetime | None = coordinator.map_updated
        self._attr_image_last_updated = coordinator.map_updated

    @property
    def available(self) -> bool:
        return (self.coordinator.live_png or self.coordinator.map_png) is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.live_signal, self._new_frame))

    @callback
    def _new_frame(self) -> None:
        """A new live frame (the robot moved, or the map was redrawn)."""
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.map_updated != self._shown:
            self._shown = self.coordinator.map_updated
            self._attr_image_last_updated = self._shown
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        return self.coordinator.live_png or self.coordinator.map_png

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "map_id": self.coordinator.map_id,
            "floor": self.coordinator.floor_name,
            "floors": self.coordinator.floor_list,
            "rooms": [r["name"] for r in self.coordinator.rooms],
            "room_shapes": self.coordinator.room_shapes,
        }


class EufyFloorImage(EufyEntity, ImageEntity):
    """One floor's last map, for showing floors the robot isn't on."""

    _attr_content_type = "image/png"
    _attr_icon = "mdi:floor-plan"
    _unrecorded_attributes = frozenset({"room_shapes"})

    def __init__(self, hass: HomeAssistant, coordinator, map_id: int) -> None:
        EufyEntity.__init__(self, coordinator, f"floor_map_{map_id}")
        ImageEntity.__init__(self, hass)
        self._attr_translation_key = "floor_map"
        self.map_id = map_id
        self._attr_image_last_updated = coordinator.floors[map_id]["updated"]

    @property
    def _name(self) -> str:
        return self.coordinator.floors.get(self.map_id, {}).get("name", f"Map {self.map_id}")

    @property
    def translation_placeholders(self) -> dict[str, str]:
        return {"floor": self._name}

    @property
    def available(self) -> bool:
        return self.coordinator.floor_pngs.get(self.map_id) is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.floor_signal, self._floors_changed))

    @callback
    def _floors_changed(self, *_args) -> None:
        self._attr_image_last_updated = self.coordinator.floors.get(self.map_id, {}).get("updated")
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self.coordinator.floor_pngs.get(self.map_id)

    @property
    def extra_state_attributes(self) -> dict:
        co = self.coordinator
        return {"map_id": self.map_id, "floor": self._name, "active": self.map_id == co.map_id,
                "room_shapes": co.floor_shapes.get(self.map_id, [])}
