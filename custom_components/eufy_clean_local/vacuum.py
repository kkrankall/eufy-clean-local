"""The robot itself, including room cleaning through Home Assistant's vacuum segments."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from . import codec as c
from . import legacy
from .entity import EufyEntity

ACTIVITY = {
    "cleaning": VacuumActivity.CLEANING,
    "docked": VacuumActivity.DOCKED,
    "idle": VacuumActivity.IDLE,
    "paused": VacuumActivity.PAUSED,
    "returning": VacuumActivity.RETURNING,
    "error": VacuumActivity.ERROR,
}


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EufyVacuum(entry.runtime_data)])


class EufyVacuum(EufyEntity, StateVacuumEntity):
    _attr_name = None
    _attr_fan_speed_list = c.SUCTIONS
    _attr_supported_features = (
        VacuumEntityFeature.STATE
        | VacuumEntityFeature.START
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.LOCATE
        | VacuumEntityFeature.CLEAN_SPOT
        | VacuumEntityFeature.SEND_COMMAND
        | VacuumEntityFeature.MAP
        | VacuumEntityFeature.CLEAN_AREA
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "vacuum")
        self._last_segments: list[tuple] = []
        if coordinator.legacy:  # no map: no rooms, segments or map feature
            self._attr_fan_speed_list = list(legacy.SUCTIONS)
            self._attr_supported_features = self._attr_supported_features & ~(
                VacuumEntityFeature.MAP | VacuumEntityFeature.CLEAN_AREA)

    @property
    def activity(self) -> VacuumActivity | None:
        return ACTIVITY.get((self.coordinator.data or {}).get("state"))

    @property
    def fan_speed(self) -> str | None:
        return (self.coordinator.data or {}).get("fan_speed")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data or {}
        return {
            "status": d.get("status"),
            "task": d.get("task"),
            "error": d.get("error"),
            "warning": d.get("warning"),
            "map_id": self.coordinator.map_id,
            "floor": self.coordinator.floor_name,
            "rooms": self.coordinator.rooms,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        segments = [(s.id, s.name) for s in self._segments()]
        if segments != self._last_segments:
            self._last_segments = segments
            self._check_segments()
        super()._handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._last_segments = [(s.id, s.name) for s in self._segments()]
        self._check_segments()

    def _segments(self) -> list[Segment]:
        """Every floor's rooms. First-floor rooms keep their plain ids (and their area mapping);
        other floors' are "<map id>_<room id>". With two or more floors each is grouped by floor."""
        co = self.coordinator
        multi = len(co.floors) > 1
        out = []
        for map_id in sorted(co.floors) or [co.map_id]:
            key = co.floor_key(map_id)
            group = co.floors[map_id]["name"] if multi and map_id in co.floors else None
            for room in co.floor_rooms(map_id):
                out.append(Segment(id=str(room["id"]) if key is None else f"{key}_{room['id']}",
                                   name=room["name"], group=group))
        return out

    @callback
    def _check_segments(self) -> None:
        """Raise HA's repair prompt when the robot's rooms no longer match the saved area mapping."""
        segments = self._segments()
        if self.registry_entry is None or not segments:
            return
        seen = self.last_seen_segments
        if seen is not None and sorted((s.id, s.name) for s in seen) != sorted((s.id, s.name) for s in segments):
            self.async_create_segments_issue()

    # ------------------------------------------------------------ actions
    async def async_start(self) -> None:
        await self.coordinator.async_command("start")

    async def async_pause(self) -> None:
        await self.coordinator.async_command("pause")

    async def async_stop(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("stop")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("return_to_base")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("clean_spot")

    async def async_locate(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("locate")

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        await self.coordinator.async_command("fan_speed", fan_speed)

    async def async_get_segments(self) -> list[Segment]:
        return self._segments()

    async def async_clean_segments(self, segment_ids: list[str], **kwargs: Any) -> None:
        co = self.coordinator
        names = {s.id: (s.name, s.group) for s in self._segments()}
        room_ids = []
        for segment in segment_ids:
            key, _, room = segment.rpartition("_")
            floor = int(key) if key else None
            if not co.on_floor(floor):
                name, group = names.get(segment, (f"room {room}", None))
                where = group or co.floors.get(co.floor_map_id(floor), {}).get("name", "another floor")
                raise HomeAssistantError(
                    f"{name} is on {where}, but the robot is on {co.floor_name or 'another floor'}. "
                    f"Carry it to {where} first.")
            room_ids.append(int(room))
        await self._clean_rooms(room_ids, int(kwargs.get("clean_times", 1)))

    async def _clean_rooms(self, room_ids: list[int], times: int = 1) -> None:
        try:
            await self.coordinator.async_command("rooms", (room_ids, times))
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_send_command(self, command: str, params: dict[str, Any] | list[Any] | None = None,
                                 **kwargs: Any) -> None:
        """Extra commands: room_clean (rooms by id or name), zone_clean, quick_mapping, remote, raw.

        Older robots (30C): auto_clean, edge_clean, small_room_clean, spot_clean, remote, raw.
        """
        params = params if isinstance(params, dict) else {}
        if self.coordinator.legacy:
            await self._legacy_command(command, params)
            return
        times = int(params.get("clean_times", 1))
        if command == "room_clean":
            names = {r["name"].lower(): r["id"] for r in self.coordinator.rooms}
            ids = []
            for room in params.get("rooms", []):
                if isinstance(room, int) or str(room).isdigit():
                    ids.append(int(room))
                elif str(room).lower() in names:
                    ids.append(names[str(room).lower()])
                else:
                    raise HomeAssistantError(f"Unknown room {room!r}; known rooms: {', '.join(names)}")
            await self._clean_rooms(ids, times)
        elif command == "zone_clean":
            if self.coordinator.map_id is None:
                raise HomeAssistantError("The map has not been loaded yet")
            await self.coordinator.async_send(c.cmd_zones(params["zones"], self.coordinator.map_id, times))
        elif command == "auto_clean":
            await self.coordinator.async_send(c.cmd_start_auto(times))
        elif command == "quick_mapping":
            await self.coordinator.async_send(c.cmd_quick_mapping())
        elif command == "remote":
            direction = str(params.get("direction", "brake")).capitalize()
            if direction not in c.REMOTE_DIRECTIONS:
                raise HomeAssistantError(f"direction must be one of {c.REMOTE_DIRECTIONS}")
            await self.coordinator.async_send({c.DP_REMOTE: direction})
        elif command == "raw":
            await self.coordinator.async_send({str(k): v for k, v in params.items()})
        else:
            raise HomeAssistantError(f"Unknown command {command!r}: " + json.dumps(params))

    async def _legacy_command(self, command: str, params: dict) -> None:
        modes = {"auto_clean": "auto", "edge_clean": "edge", "small_room_clean": "small_room", "spot_clean": "spot"}
        if command in modes:
            await self.coordinator.async_command(modes[command])
        elif command == "remote":
            direction = str(params.get("direction", "")).capitalize()
            if direction not in ("Forward", "Back", "Left", "Right"):
                raise HomeAssistantError("direction must be forward, back, left or right")
            await self.coordinator.async_send({legacy.DP_DIRECTION: direction})
        elif command == "raw":
            await self.coordinator.async_send({str(k): v for k, v in params.items()})
        else:
            raise HomeAssistantError(
                f"Unknown command {command!r}; this robot supports {', '.join([*modes, 'remote', 'raw'])}")
