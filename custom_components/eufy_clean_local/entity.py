"""Shared base for eufy Clean (Local) entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import codec as c
from .const import DOMAIN
from .coordinator import EufyCoordinator


class EufyEntity(CoordinatorEntity[EufyCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EufyCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_translation_key = key
        info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.robot_name,
            manufacturer="eufy",
            model=c.L60_MODELS.get(coordinator.model, coordinator.model),
            model_id=coordinator.model,
        )
        firmware = (coordinator.data or {}).get("firmware")
        if firmware:
            info["sw_version"] = firmware
        if coordinator.mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, coordinator.mac.lower())}
        self._attr_device_info = info

    @property
    def available(self) -> bool:
        return self.coordinator.online and bool(self.coordinator.data)

    @property
    def value(self):
        return (self.coordinator.data or {}).get(self.key)


def add_when_station(entry, coordinator, async_add_entities, factory) -> None:
    """Add station-only entities as soon as the robot reports a self-empty station."""
    added = False

    @callback
    def check() -> None:
        nonlocal added
        if not added and coordinator.has_station:
            added = True
            async_add_entities(factory())

    check()
    entry.async_on_unload(coordinator.async_add_listener(check))
