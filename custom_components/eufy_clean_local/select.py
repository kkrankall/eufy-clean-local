"""Cleaning passes, and mop settings on the Hybrid models."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from . import codec as c
from .entity import EufyEntity, add_when_station

SELECTS = {
    "clean_times": ("mdi:repeat", ["1", "2"], lambda v: c.cmd_clean_param(clean_times=int(v)), False),
    "water_level": ("mdi:water", c.WATER_LEVELS,
                    lambda v: c.cmd_clean_param(water_level=c.WATER_LEVELS.index(v)), True),
    "clean_type": ("mdi:broom", c.CLEAN_TYPES,
                   lambda v: c.cmd_clean_param(clean_type=c.CLEAN_TYPES.index(v)), True),
}


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.legacy:
        return
    async_add_entities(
        EufySelect(coordinator, key) for key, (_, _, _, hybrid_only) in SELECTS.items()
        if not hybrid_only or coordinator.hybrid
    )
    add_when_station(entry, coordinator, async_add_entities, lambda: [EufyAutoEmptyEvery(coordinator)])

    added: set[int] = set()

    @callback
    def add_rooms() -> None:
        params = coordinator.room_params()
        if params is None:
            return
        new = [r.id for r in params.rooms if r.id not in added]
        if new:
            added.update(new)
            fields = ["suction", "clean_times"] + (["water_level"] if coordinator.hybrid else [])
            async_add_entities(EufyRoomSelect(coordinator, room_id, f) for room_id in new for f in fields)

    add_rooms()
    entry.async_on_unload(coordinator.async_add_listener(add_rooms))


class EufySelect(EufyEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_icon, self._attr_options, self._build, _ = SELECTS[key]

    @property
    def current_option(self) -> str | None:
        return self.value

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_send(self._build(option))


class EufyAutoEmptyEvery(EufySelect):
    """How many cleanings between automatic dust emptying (the app offers 1-3)."""

    def __init__(self, coordinator) -> None:
        EufyEntity.__init__(self, coordinator, "auto_empty_every")
        self._attr_icon = "mdi:delete-clock"
        self._attr_options = ["1", "2", "3"]

    async def async_select_option(self, option: str) -> None:
        on = bool((self.coordinator.data or {}).get("auto_empty", True))
        await self.coordinator.async_send(c.cmd_auto_empty(on, int(option)))


ROOM_FIELDS = {
    "suction": ("mdi:fan", c.SUCTIONS),
    "clean_times": ("mdi:repeat", ["1", "2"]),
    "water_level": ("mdi:water", c.WATER_LEVELS),
}


class EufyRoomSelect(EufyEntity, SelectEntity):
    """One room's suction, passes or mop water level, used while per-room settings are on."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, room_id: int, field: str) -> None:
        super().__init__(coordinator, f"room_{room_id}_{field}")
        self._attr_translation_key = f"room_{field}"
        self.room_id, self.field = room_id, field
        self._attr_icon, self._attr_options = ROOM_FIELDS[field]

    def _room(self):
        params = self.coordinator.room_params()
        return next((r for r in params.rooms if r.id == self.room_id), None) if params is not None else None

    @property
    def translation_placeholders(self) -> dict[str, str]:
        room = next((r for r in self.coordinator.rooms if r["id"] == self.room_id), None)
        return {"room": room["name"] if room else f"Room {self.room_id}"}

    @property
    def available(self) -> bool:
        return super().available and self._room() is not None

    @property
    def current_option(self) -> str | None:
        room = self._room()
        if room is None:
            return None
        custom = room.custom
        if self.field == "suction":
            return c.SUCTIONS[custom.fan.suction] if custom.fan.suction < len(c.SUCTIONS) else None
        if self.field == "clean_times":
            return str(custom.clean_times or 1)
        return c.WATER_LEVELS[custom.mop_mode.level] if custom.mop_mode.level < len(c.WATER_LEVELS) else None

    async def async_select_option(self, option: str) -> None:
        value = int(option) if self.field == "clean_times" else self.options.index(option)
        await self.coordinator.async_set_rooms_custom(room_id=self.room_id, **{self.field: value})
