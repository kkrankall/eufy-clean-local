"""An event for every new error, warning or robot message, for notification automations."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EufyConfigEntry
from .entity import EufyEntity


async def async_setup_entry(hass: HomeAssistant, entry: EufyConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EufyAlertEvent(entry.runtime_data)])


class EufyAlertEvent(EufyEntity, EventEntity):
    """Fires `error`, `warning` or `message` with the code and eufy's text, e.g. "Wheel stuck"."""

    _attr_icon = "mdi:bell-alert-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "alert")
        self._attr_event_types = ["error", "warning"] if coordinator.legacy else ["error", "warning", "message"]

    @property
    def available(self) -> bool:
        return True  # events can still arrive while the robot reconnects

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self.coordinator.alert_signal, self._alert))

    @callback
    def _alert(self, kind: str, data: dict) -> None:
        self._trigger_event(kind, data)
        self.async_write_ha_state()
