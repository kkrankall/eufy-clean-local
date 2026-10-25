"""Status, consumable and statistics sensors."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from . import codec as c
from .entity import EufyEntity, add_when_station


@dataclass(frozen=True, kw_only=True)
class EufySensorDescription(SensorEntityDescription):
    station_only: bool = False


SENSORS = [
    EufySensorDescription(key="battery", device_class=SensorDeviceClass.BATTERY,
                          native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    EufySensorDescription(key="status", icon="mdi:robot-vacuum"),
    EufySensorDescription(key="error", icon="mdi:alert-circle-outline"),
    EufySensorDescription(key="warning", icon="mdi:alert-outline"),
    EufySensorDescription(key="prompt", icon="mdi:message-text-outline"),
    EufySensorDescription(key="clean_area", native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
                          device_class=SensorDeviceClass.AREA, state_class=SensorStateClass.MEASUREMENT,
                          suggested_display_precision=0),
    EufySensorDescription(key="clean_minutes", native_unit_of_measurement=UnitOfTime.MINUTES,
                          device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.MEASUREMENT),
    EufySensorDescription(key="total_area", native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
                          device_class=SensorDeviceClass.AREA, state_class=SensorStateClass.TOTAL_INCREASING,
                          suggested_display_precision=0),
    EufySensorDescription(key="total_hours", native_unit_of_measurement=UnitOfTime.HOURS,
                          device_class=SensorDeviceClass.DURATION, state_class=SensorStateClass.TOTAL_INCREASING,
                          suggested_display_precision=1),
    EufySensorDescription(key="total_count", icon="mdi:counter", state_class=SensorStateClass.TOTAL_INCREASING),
    EufySensorDescription(key="firmware", icon="mdi:chip", entity_category=EntityCategory.DIAGNOSTIC),
    EufySensorDescription(key="ip", icon="mdi:ip-network", entity_category=EntityCategory.DIAGNOSTIC),
    EufySensorDescription(key="wifi_signal", icon="mdi:wifi", native_unit_of_measurement=PERCENTAGE,
                          state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC),
]
SENSORS.append(EufySensorDescription(key="station_status", icon="mdi:home-import-outline", station_only=True))
for _key in c.CONSUMABLES:
    SENSORS.append(EufySensorDescription(
        key=f"{_key}_left", native_unit_of_measurement=PERCENTAGE, icon="mdi:progress-wrench",
        state_class=SensorStateClass.MEASUREMENT, station_only=_key == "dustbag"))
    SENSORS.append(EufySensorDescription(
        key=f"{_key}_hours", native_unit_of_measurement=UnitOfTime.HOURS, device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
        station_only=_key == "dustbag"))


LEGACY_SENSORS = {"battery", "status", "error", "ip"}


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    if coordinator.legacy:
        async_add_entities(EufySensor(coordinator, d) for d in SENSORS if d.key in LEGACY_SENSORS)
        return
    async_add_entities(EufySensor(coordinator, desc) for desc in SENSORS if not desc.station_only)
    if coordinator.cloud is not None:
        async_add_entities([EufyFloorSensor(coordinator)])
    add_when_station(entry, coordinator, async_add_entities,
                     lambda: [EufySensor(coordinator, d) for d in SENSORS if d.station_only])


class EufySensor(EufyEntity, SensorEntity):
    entity_description: EufySensorDescription

    def __init__(self, coordinator, description: EufySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        if self.key == "ip":  # the L60 leaves its own address out of DP 169
            return self.value or self.coordinator.host or None
        return self.value


class EufyFloorSensor(EufyEntity, SensorEntity):
    """The floor the robot is on (multi-floor maps), named as in the eufy app.

    It changes once the robot uploads a map from another floor, which it does when it starts
    cleaning there; the robot doesn't report its floor otherwise.
    """

    _attr_icon = "mdi:layers-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "floor")

    @property
    def available(self) -> bool:
        return self.coordinator.floor_name is not None

    @property
    def native_value(self) -> str | None:
        return self.coordinator.floor_name

    @property
    def extra_state_attributes(self) -> dict:
        return {"floors": self.coordinator.floor_list}
