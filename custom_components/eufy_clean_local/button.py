"""Consumable resets, station actions, map refresh and one button per room."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from . import codec as c
from .entity import EufyEntity, add_when_station


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.legacy:
        return
    entities: list[ButtonEntity] = []

    def reset(key: str) -> "EufyButton":
        return EufyButton(coordinator, f"reset_{key}", "mdi:restore", EntityCategory.CONFIG,
                          lambda: coordinator.async_send(c.cmd_reset_consumable(key)))

    entities.extend(reset(key) for key in c.CONSUMABLES if key != "dustbag")
    add_when_station(entry, coordinator, async_add_entities, lambda: [
        reset("dustbag"),
        EufyButton(coordinator, "empty_dust", "mdi:delete-empty", None,
                   lambda: coordinator.async_send(c.cmd_empty_dust())),
        EufyButton(coordinator, "cut_hair", "mdi:content-cut", None,
                   lambda: coordinator.async_send(c.cmd_cut_hair())),
    ])
    if coordinator.cloud is not None:
        entities.append(EufyButton(coordinator, "refresh_map", "mdi:map-search", EntityCategory.DIAGNOSTIC,
                                   coordinator.async_refresh_map, needs_robot=False))
    async_add_entities(entities)

    added: set[tuple[int | None, int]] = set()

    @callback
    def add_rooms() -> None:
        new = []
        for map_id in sorted(coordinator.floors) or [coordinator.map_id]:
            key = coordinator.floor_key(map_id)
            for room in coordinator.floor_rooms(map_id):
                if (key, room["id"]) not in added:
                    added.add((key, room["id"]))
                    new.append(EufyRoomButton(coordinator, room["id"], key))
        if new:
            async_add_entities(new)

    add_rooms()
    entry.async_on_unload(coordinator.async_add_listener(add_rooms))


class EufyButton(EufyEntity, ButtonEntity):
    def __init__(self, coordinator, key, icon, category, action, needs_robot: bool = True) -> None:
        super().__init__(coordinator, key)
        self._attr_icon = icon
        self._attr_entity_category = category
        self._action = action
        self._needs_robot = needs_robot

    @property
    def available(self) -> bool:
        return super().available if self._needs_robot else True

    async def async_press(self) -> None:
        await self._action()


class EufyRoomButton(EufyEntity, ButtonEntity):
    """Clean one room. Rooms that disappear from the map, or are on another floor, become unavailable."""

    _attr_icon = "mdi:broom"

    def __init__(self, coordinator, room_id: int, floor: int | None = None) -> None:
        super().__init__(coordinator, f"clean_room_{room_id}" if floor is None else f"clean_room_{floor}_{room_id}")
        self._attr_translation_key = "clean_room"
        self.room_id, self.floor = room_id, floor

    @property
    def _room(self) -> dict | None:
        rooms = self.coordinator.floor_rooms(self.coordinator.floor_map_id(self.floor))
        return next((r for r in rooms if r["id"] == self.room_id), None)

    @property
    def translation_placeholders(self) -> dict[str, str]:
        room = self._room
        return {"room": self.coordinator.room_label(self.floor, room["name"] if room else f"room {self.room_id}")}

    @property
    def available(self) -> bool:
        return super().available and self._room is not None and self.coordinator.on_floor(self.floor)

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_command("rooms", ([self.room_id], 1))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
