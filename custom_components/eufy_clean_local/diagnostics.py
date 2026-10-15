"""Diagnostics: the robot's state plus the raw map channels, so map decoding can be checked offline."""

from __future__ import annotations

from typing import Any

from google.protobuf.json_format import MessageToDict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import EufyConfigEntry
from . import mapdata
from .const import CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_MAC

TO_REDACT = {CONF_EMAIL, CONF_PASSWORD, CONF_LOCAL_KEY, CONF_MAC, CONF_DEVICE_ID, "devId", "ip", "mac"}


def _summary(channel: int, msg) -> dict:
    """A channel's message with its pixel blobs replaced by their sizes."""
    data = MessageToDict(msg, preserving_proto_field_name=True)
    for key in ("pixels", "carpet_pixels"):
        if key in data:
            data[key] = f"<{len(getattr(msg, key))} bytes>"
    return data


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EufyConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    snapshot = coordinator.map
    channels = {}
    for channel, blob in sorted(snapshot.raw.items()):
        msg = snapshot.get(channel)
        channels[channel] = {
            "hex": blob.hex(),
            "decoded": _summary(channel, msg) if msg is not None else None,
        }
    fetch = coordinator.last_fetch
    return {
        "entry": async_redact_data({"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT),
        "online": coordinator.online,
        "state": async_redact_data(dict(coordinator.data or {}), TO_REDACT),
        "map": {
            "map_id": coordinator.map_id,
            "rooms": coordinator.rooms,
            "updated": coordinator.map_updated.isoformat() if coordinator.map_updated else None,
            "channel_names": {ch: cls.DESCRIPTOR.name for ch, cls in mapdata.CHANNEL_TYPES.items()},
            "channels": channels,
        },
        "last_fetch": None if fetch is None else {
            "latest": async_redact_data(fetch.latest, TO_REDACT),
            "channels": fetch.channels,
            "raw_entries": fetch.raw,
        },
    }
