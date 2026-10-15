"""A live view of the map: the robot (and its cleaned path) redrawn every few seconds while it cleans.

A camera rather than an image entity: the dashboard pulls frames itself and the
camera's state never changes per frame, so following the robot adds nothing to
the recorder.
"""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.cloud is not None and not coordinator.legacy:
        async_add_entities([EufyLiveMap(coordinator)])


class EufyLiveMap(EufyEntity, Camera):
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, coordinator) -> None:
        EufyEntity.__init__(self, coordinator, "live_map")
        Camera.__init__(self)
        self.content_type = "image/png"
        self._attr_frame_interval = max(coordinator.live_interval, 1) if coordinator.live_interval else 10

    @property
    def available(self) -> bool:
        return (self.coordinator.live_png or self.coordinator.map_png) is not None

    @property
    def is_recording(self) -> bool:
        return False

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self.coordinator.live_png or self.coordinator.map_png
