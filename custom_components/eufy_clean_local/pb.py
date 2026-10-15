"""Protobuf message classes built from the embedded schema.

The schema is loaded into a private descriptor pool, so it works with whatever
protobuf version Home Assistant ships and cannot clash with other integrations
that register the same proto file names.
"""

from __future__ import annotations

import base64
import zlib

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from .schema import DESCRIPTOR_SET

_files = descriptor_pb2.FileDescriptorSet.FromString(
    zlib.decompress(base64.b64decode("".join(DESCRIPTOR_SET)))
).file
_pool = descriptor_pool.DescriptorPool()

try:
    _classes = message_factory.GetMessages(list(_files), pool=_pool)
except (AttributeError, TypeError):  # protobuf < 4.21
    for _f in _files:
        _pool.Add(_f)
    _factory = message_factory.MessageFactory(_pool)
    _classes = {}
    for _f in _files:
        _fd = _pool.FindFileByName(_f.name)
        for _name in _fd.message_types_by_name:
            _full = f"{_fd.package}.{_name}"
            _classes[_full] = _factory.GetPrototype(_pool.FindMessageTypeByName(_full))


def message(name: str):
    """Return the class for proto.cloud.<name> (or a fully qualified name)."""
    return _classes[name if "." in name else f"proto.cloud.{name}"]


ModeCtrlRequest = message("ModeCtrlRequest")
ModeCtrlResponse = message("ModeCtrlResponse")
AutoClean = message("AutoClean")
SpotClean = message("SpotClean")
SelectRoomsClean = message("SelectRoomsClean")
SelectZonesClean = message("SelectZonesClean")
Point = message("Point")
WorkStatus = message("WorkStatus")
CleanParam = message("CleanParam")
CleanParamRequest = message("CleanParamRequest")
CleanParamResponse = message("CleanParamResponse")
UndisturbedRequest = message("UndisturbedRequest")
UndisturbedResponse = message("UndisturbedResponse")
CleanStatistics = message("CleanStatistics")
ConsumableRequest = message("ConsumableRequest")
ConsumableResponse = message("ConsumableResponse")
DeviceInfo = message("DeviceInfo")
StationRequest = message("StationRequest")
StationResponse = message("StationResponse")
UnisettingRequest = message("UnisettingRequest")
UnisettingResponse = message("UnisettingResponse")
ErrorCode = message("ErrorCode")
PromptCode = message("PromptCode")
KeepAliveRequest = message("KeepAliveRequest")
MapEditRequest = message("MapEditRequest")
Map = message("proto.cloud.stream.Map")
RoomOutline = message("proto.cloud.stream.RoomOutline")
RoomParams = message("proto.cloud.stream.RoomParams")
RestrictedZone = message("proto.cloud.stream.RestrictedZone")
DynamicData = message("proto.cloud.stream.DynamicData")
TemporaryData = message("proto.cloud.stream.TemporaryData")
