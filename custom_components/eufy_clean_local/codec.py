"""Encode commands and decode status for the eufy L60 family over Tuya DPs.

The DP layout and message shapes come from the eufy app's own per-model codec
for the L60 (T2267), cross-checked against the protobuf schema in the app's
CleanWidgetProtobuf framework. Protobuf DPs travel as base64 of a varint length
prefix followed by the message.
"""

from __future__ import annotations

import base64
import time

from . import pb
from .codes import ERRORS, PROMPTS, WARNINGS

L60_MODELS = {"T2267": "L60", "T2268": "L60 Hybrid", "T2277": "L60 SES", "T2278": "L60 Hybrid SES"}
HYBRID_MODELS = {"T2268", "T2278"}

DP_MODE_CTRL = "152"
DP_WORK_STATUS = "153"
DP_CLEAN_PARAM = "154"
DP_REMOTE = "155"
DP_AUTO_RETURN = "156"
DP_DND = "157"
DP_SUCTION = "158"
DP_BOOST_IQ = "159"
DP_FIND_ROBOT = "160"
DP_VOLUME = "161"
DP_BATTERY = "163"
DP_STATISTICS = "167"
DP_CONSUMABLES = "168"
DP_DEVICE_INFO = "169"
DP_MAP_EDIT = "170"
DP_MULTI_MAP = "172"
DP_STATION = "173"
DP_UNISETTING = "176"
DP_ERRORS = "177"
DP_PROMPT = "178"

SUCTIONS = ["Quiet", "Standard", "Turbo", "Max"]
WATER_LEVELS = ["Low", "Medium", "High"]
CLEAN_TYPES = ["Vacuum", "Mop", "Vacuum and mop", "Vacuum then mop"]
REMOTE_DIRECTIONS = ["Brake", "Forward", "Back", "Left", "Right"]

# Consumable life in hours, from the app's T2267 accessory config.
CONSUMABLES = {
    "side_brush": {"name": "Side brush", "life": 250, "reset": pb.ConsumableRequest.SIDE_BRUSH},
    "rolling_brush": {"name": "Rolling brush", "life": 360, "reset": pb.ConsumableRequest.ROLLING_BRUSH},
    "filter_mesh": {"name": "Filter", "life": 200, "reset": pb.ConsumableRequest.FILTER_MESH},
    "dustbag": {"name": "Dust bag", "life": 1440, "reset": pb.ConsumableRequest.DUSTBAG},
}

Method = pb.ModeCtrlRequest
WS = pb.WorkStatus


# ---------------------------------------------------------------- wire format

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_encode(msg) -> str:
    body = msg.SerializeToString()
    return base64.b64encode(_varint(len(body)) + body).decode()


def pb_decode(value: str, cls):
    raw = base64.b64decode(value)
    length, shift, pos = 0, 0, 0
    while True:
        b = raw[pos]
        length |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            break
        shift += 7
    msg = cls()
    msg.ParseFromString(raw[pos:pos + length])
    return msg


# ---------------------------------------------------------------- commands
# Each builder returns a {dp: value} dict ready for tinytuya set_multiple_values.

def mode(method: int, **param) -> dict:
    req = pb.ModeCtrlRequest(method=method, seq=int(time.time()) & 0xFFFF)
    for field, value in param.items():
        getattr(req, field).CopyFrom(value)
    return {DP_MODE_CTRL: pb_encode(req)}


def cmd_start_auto(clean_times: int = 1) -> dict:
    return mode(Method.START_AUTO_CLEAN, auto_clean=pb.AutoClean(clean_times=clean_times))


def cmd_pause() -> dict:
    return mode(Method.PAUSE_TASK)


def cmd_resume() -> dict:
    return mode(Method.RESUME_TASK)


def cmd_stop() -> dict:
    return mode(Method.STOP_TASK)


def cmd_go_home() -> dict:
    return mode(Method.START_GOHOME)


def cmd_stop_go_home() -> dict:
    return mode(Method.STOP_GOHOME)


def cmd_spot(clean_times: int = 1) -> dict:
    return mode(Method.START_SPOT_CLEAN, spot_clean=pb.SpotClean(clean_times=clean_times))


def cmd_quick_mapping() -> dict:
    return mode(Method.START_FAST_MAPPING)


def cmd_rooms(room_ids: list[int], map_id: int, clean_times: int = 1) -> dict:
    rooms = pb.SelectRoomsClean(clean_times=clean_times, map_id=map_id)
    for order, room_id in enumerate(room_ids, start=1):
        rooms.rooms.add(id=int(room_id), order=order)
    return mode(Method.START_SELECT_ROOMS_CLEAN, select_rooms_clean=rooms)


def cmd_zones(zones: list[list[list[int]]], map_id: int, clean_times: int = 1) -> dict:
    """zones: list of four [x, y] corners each, in robot map units (cm)."""
    req = pb.SelectZonesClean(map_id=map_id)
    for corners in zones:
        zone = req.zones.add(clean_times=clean_times)
        for name, (x, y) in zip(("p0", "p1", "p2", "p3"), corners):
            getattr(zone.quadrangle, name).CopyFrom(pb.Point(x=int(x), y=int(y)))
    return mode(Method.START_SELECT_ZONES_CLEAN, select_zones_clean=req)


def cmd_suction(name: str) -> dict:
    return {DP_SUCTION: SUCTIONS[[s.lower() for s in SUCTIONS].index(name.lower())]}


def cmd_clean_param(**fields) -> dict:
    """Send one or more clean-param sub-messages, like the app does (only what changed)."""
    param = pb.CleanParam()
    if "clean_type" in fields:
        param.clean_type.value = fields["clean_type"]
    if "water_level" in fields:
        param.mop_mode.level = fields["water_level"]
    if "clean_extent" in fields:
        param.clean_extent.value = fields["clean_extent"]
    if "clean_times" in fields:
        param.clean_times = fields["clean_times"]
    return {DP_CLEAN_PARAM: pb_encode(pb.CleanParamRequest(clean_param=param))}


def cmd_dnd(on: bool, begin: tuple[int, int], end: tuple[int, int]) -> dict:
    req = pb.UndisturbedRequest()
    req.undisturbed.sw.SetInParent()
    req.undisturbed.sw.value = on
    req.undisturbed.begin.SetInParent()
    req.undisturbed.begin.hour, req.undisturbed.begin.minute = begin
    req.undisturbed.end.SetInParent()
    req.undisturbed.end.hour, req.undisturbed.end.minute = end
    return {DP_DND: pb_encode(req)}


def cmd_child_lock(on: bool) -> dict:
    req = pb.UnisettingRequest()
    req.children_lock.SetInParent()
    req.children_lock.value = on
    return {DP_UNISETTING: pb_encode(req)}


def cmd_reset_consumable(key: str) -> dict:
    req = pb.ConsumableRequest(reset_types=[CONSUMABLES[key]["reset"]])
    return {DP_CONSUMABLES: pb_encode(req)}


def cmd_empty_dust() -> dict:
    req = pb.StationRequest()
    req.manual_cmd.go_collect_dust = True
    return {DP_STATION: pb_encode(req)}


def cmd_cut_hair() -> dict:
    req = pb.StationRequest()
    req.manual_cmd.go_cut_hair = True
    return {DP_STATION: pb_encode(req)}


def cmd_auto_empty(on: bool, every_tasks: int = 1) -> dict:
    """Self-empty station: empty the robot's bin after every N cleanings."""
    req = pb.StationRequest()
    cfg = req.auto_cfg.collectdust_v2
    cfg.sw.SetInParent()
    cfg.sw.value = on
    cfg.mode.SetInParent()
    cfg.mode.task = every_tasks
    return {DP_STATION: pb_encode(req)}


def cmd_auto_cut_hair(on: bool) -> dict:
    req = pb.StationRequest()
    req.auto_cfg.cut_hair.sw.SetInParent()
    req.auto_cfg.cut_hair.sw.value = on
    return {DP_STATION: pb_encode(req)}


def station_status(resp) -> str:
    st = resp.status
    if not st.connected:  # the station reports this whenever the robot is off the dock
        return "Robot away"
    if st.collecting_dust:
        return "Emptying dust"
    if st.cutting_hair:
        return "Cutting hair"
    return "Idle"


def cmd_rooms_custom(params, map_id: int) -> dict:
    """Per-room settings (DP 170, MapEditRequest SET_ROOMS_CUSTOM), built from the map's RoomParams.

    Like the app, every room's settings are sent each time, plus the room order
    while custom settings are on.
    """
    req = pb.MapEditRequest(method=pb.MapEditRequest.SET_ROOMS_CUSTOM, map_id=map_id,
                            seq=int(time.time()) & 0xFFFF)
    custom = req.rooms_custom
    custom.custom_enable.SetInParent()
    custom.custom_enable.value = params.custom_enable
    rooms = sorted(params.rooms, key=lambda r: (r.order.value or 255, r.id))
    if params.custom_enable:
        for order, room in enumerate(rooms, start=1):
            custom.rooms_order.rooms.add(id=room.id, order=order)
    for room in rooms:
        entry = custom.rooms_parm.rooms.add(id=room.id)
        entry.custom.CopyFrom(_room_custom(room.custom))
    if params.HasField("smart_mode_sw"):
        custom.smart_mode_sw.CopyFrom(params.smart_mode_sw)
    return {DP_MAP_EDIT: pb_encode(req)}


def _room_custom(src):
    """RoomParams.Room.Custom -> MapEditRequest.RoomsCustom.Parm.Room.Custom (same fields)."""
    dst = pb.MapEditRequest.RoomsCustom.Parm.Room.Custom()
    dst.ParseFromString(src.SerializeToString())
    for field in ("clean_type", "fan", "mop_mode", "clean_extent"):
        getattr(dst, field).SetInParent()  # the robot expects each part present, even at its default
    return dst


def cmd_keep_alive(force_sync: bool = True) -> dict:
    req = pb.KeepAliveRequest(timestamp=int(time.time() * 1_000_000), force_sync=force_sync)
    return {DP_WORK_STATUS: pb_encode(req)}


# ---------------------------------------------------------------- status

DECODERS = {
    DP_MODE_CTRL: pb.ModeCtrlResponse,
    DP_WORK_STATUS: pb.WorkStatus,
    DP_CLEAN_PARAM: pb.CleanParamResponse,
    DP_DND: pb.UndisturbedResponse,
    DP_STATISTICS: pb.CleanStatistics,
    DP_CONSUMABLES: pb.ConsumableResponse,
    DP_DEVICE_INFO: pb.DeviceInfo,
    DP_STATION: pb.StationResponse,
    DP_UNISETTING: pb.UnisettingResponse,
    DP_ERRORS: pb.ErrorCode,
    DP_PROMPT: pb.PromptCode,
}


def decode_dp(dp: str, value):
    """Return a protobuf message for protobuf DPs, or the plain value otherwise."""
    cls = DECODERS.get(dp)
    if cls is None or not isinstance(value, str):
        return value
    return pb_decode(value, cls)


def vacuum_state(ws) -> str:
    """Map WorkStatus to Home Assistant's vacuum states."""
    if ws is None:
        return "idle"
    state = ws.state
    if state == WS.FAULT:
        return "error"
    if state == WS.CHARGING:
        return "docked"
    if state == WS.GO_HOME:
        return "paused" if ws.go_home.state == WS.GoHome.PAUSED else "returning"
    if state == WS.CLEANING:
        return "paused" if ws.cleaning.state == WS.Cleaning.PAUSED else "cleaning"
    if state == WS.FAST_MAPPING:
        return "paused" if ws.mapping.state == WS.Mapping.PAUSED else "cleaning"
    if state in (WS.REMOTE_CTRL, WS.CRUISIING):
        return "cleaning"
    if ws.HasField("cleaning") and ws.cleaning.state == WS.Cleaning.PAUSED:
        return "paused"
    return "idle"


MODE_NAMES = {
    WS.Mode.AUTO: "full clean",
    WS.Mode.SELECT_ROOM: "room clean",
    WS.Mode.SELECT_ZONE: "zone clean",
    WS.Mode.SPOT: "spot clean",
    WS.Mode.FAST_MAPPING: "mapping",
    WS.Mode.SCENE: "scene",
}


def status_text(ws) -> str:
    """A human description close to what the eufy app shows."""
    if ws is None:
        return "Unknown"
    s = ws.state
    task = MODE_NAMES.get(ws.mode.value, "clean")
    station = ws.station
    if station.HasField("dust_collection_system"):
        return "Emptying dust"
    if ws.HasField("roller_brush_cleaning"):
        return "Cutting hair from roller brush"
    if s == WS.SLEEP:
        return "Sleeping"
    if s == WS.FAULT:
        return "Error"
    if s == WS.CHARGING:
        if ws.HasField("breakpoint"):
            return "Recharging, will resume cleaning"
        if ws.charging.state == WS.Charging.DONE:
            return "Fully charged"
        if ws.charging.state == WS.Charging.ABNORMAL:
            return "Charging problem"
        return "Charging"
    if s == WS.FAST_MAPPING:
        if ws.mapping.mode == WS.Mapping.RELOCATING:
            return "Positioning"
        return "Mapping paused" if ws.mapping.state == WS.Mapping.PAUSED else "Mapping"
    if s == WS.CLEANING:
        if ws.HasField("relocating") or ws.cleaning.mode == WS.Cleaning.RELOCATING:
            return "Positioning"
        if ws.cleaning.state == WS.Cleaning.PAUSED:
            return f"Paused ({task})"
        return f"Cleaning ({task})"
    if s == WS.REMOTE_CTRL:
        return "Remote control"
    if s == WS.GO_HOME:
        if ws.go_home.state == WS.GoHome.PAUSED:
            return "Return paused"
        if ws.go_home.mode == WS.GoHome.COLLECT_DUST:
            return "Returning to empty dust"
        if ws.HasField("breakpoint"):
            return "Returning to recharge, will resume"
        return "Returning to dock"
    if ws.HasField("cleaning") and ws.cleaning.state == WS.Cleaning.PAUSED:
        return f"Paused ({task})"
    return "Standby"


def consumable_percent(hours_used: int, life: int) -> int:
    """The app's remaining-life formula."""
    if life <= 0:
        return 100
    p = 100 * (life - max(hours_used, 0)) / life
    if p > 100:
        return 100
    if p > 1:
        return int(p)
    if p > 0:
        return 1
    return 0


def error_texts(err) -> tuple[list[str], list[str]]:
    errors = [ERRORS.get(c, f"Error {c}") for c in err.error]
    warnings = [WARNINGS.get(c, f"Warning {c}") for c in err.warn]
    return errors, warnings


def prompt_text(prompt) -> str:
    if not prompt.value:
        return ""
    return "; ".join(PROMPTS.get(c, f"Prompt {c}") for c in prompt.value)
