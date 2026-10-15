"""A problem sensor: on while the robot reports any error or warning."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EufyProblem(entry.runtime_data)])


class EufyProblem(EufyEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "problem")

    @property
    def is_on(self) -> bool | None:
        d = self.coordinator.data or {}
        if "error_codes" not in d:
            return None
        return bool(d.get("error_codes") or d.get("warning_codes"))

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data or {}
        return {"error": d.get("error"), "warning": d.get("warning")}
