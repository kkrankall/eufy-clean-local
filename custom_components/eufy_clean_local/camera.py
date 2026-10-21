"""A live view of the map: the robot (and its cleaned path) redrawn every few seconds while it cleans.

A camera rather than an image entity: the dashboard pulls frames itself and the
camera's state never changes per frame, so following the robot adds nothing to
the recorder.
"""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.const import CONTENT_TYPE_MULTIPART
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity

# Longest gap between frames in the live stream. Safari and the Home Assistant
# iOS/macOS apps fail an image whose stream sends nothing for about 60 s.
KEEPALIVE = 20.0
BOUNDARY = "--frameboundary"


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

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        """The live view, resending the current frame at least every KEEPALIVE seconds.

        Home Assistant's own still-image stream only writes a frame when the
        picture changes, so a docked robot leaves it silent and WebKit shows a
        broken image a minute later. Browsers also show a stream's frames one
        behind, so the resend is what puts the newest frame on screen.
        """
        response = web.StreamResponse()
        response.content_type = CONTENT_TYPE_MULTIPART.format(BOUNDARY)
        await response.prepare(request)
        last, sent_at = None, 0.0
        while True:
            image = await self.async_camera_image()
            if not image:
                break
            if image != last or time.monotonic() - sent_at >= KEEPALIVE:
                part = (f"{BOUNDARY}\r\nContent-Type: {self.content_type}\r\n"
                        f"Content-Length: {len(image)}\r\n\r\n").encode() + image + b"\r\n"
                await response.write(part)
                if last is None:
                    await response.write(part)  # the first frame twice, so it shows straight away
                last, sent_at = image, time.monotonic()
            await asyncio.sleep(self.frame_interval)
        return response
