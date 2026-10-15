"""Test fixtures: a simulated L60 on the other end of the Tuya socket."""

from __future__ import annotations

import threading
import time

import lz4.block
import pytest

from custom_components.eufy_clean_local import codec as c
from custom_components.eufy_clean_local import mapdata, pb

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def robot_dps() -> dict:
    cons = pb.ConsumableResponse()
    cons.runtime.side_brush.duration = 100
    cons.runtime.rolling_brush.duration = 36
    cons.runtime.filter_mesh.duration = 199
    stats = pb.CleanStatistics()
    stats.single.clean_area = 42
    stats.single.clean_duration = 3000
    stats.total.clean_area = 5000
    stats.total.clean_duration = 360000
    stats.total.clean_count = 120
    dnd = pb.UndisturbedResponse()
    dnd.undisturbed.sw.value = True
    dnd.undisturbed.begin.hour = 22
    dnd.undisturbed.end.hour = 8
    uni = pb.UnisettingResponse(ap_signal_strength=71)
    uni.children_lock.SetInParent()
    info = pb.DeviceInfo(software="2.0.4", wifi_ip="192.168.1.50", wifi_name="home")
    param = pb.CleanParamResponse()
    param.clean_param.clean_times = 1
    return {
        "153": "BhADGgIIAQ==",  # charging, done
        "163": 100, "158": "Standard", "159": True, "156": False, "161": 60,
        "168": c.pb_encode(cons), "167": c.pb_encode(stats), "177": c.pb_encode(pb.ErrorCode(warn=[2311])),
        "157": c.pb_encode(dnd), "176": c.pb_encode(uni), "169": c.pb_encode(info), "154": c.pb_encode(param),
    }


class FakeDevice:
    """Stands in for tinytuya.Device; records what the integration sends."""

    instances: list["FakeDevice"] = []
    plain_status_only = False  # the real L60 leaves protobuf DPs out of status()
    status_dps: dict | None = None  # replaces the L60's DPs (e.g. for a RoboVac 30C)
    status_error: dict | None = None  # e.g. {"Error": "...", "Err": "914"} for a wrong key

    def __init__(self, dev_id, address=None, local_key=None, version=None, persist=False):
        self.dev_id, self.address, self.local_key, self.version = dev_id, address, local_key, version
        self.sent: list[dict] = []
        self.pending: list[dict] = []
        self.lock = threading.Lock()
        FakeDevice.instances.append(self)

    def set_socketTimeout(self, s): pass
    def set_socketRetryLimit(self, n): pass
    def heartbeat(self, nowait=True): pass
    def close(self): pass

    def status(self):
        if FakeDevice.status_error is not None:
            return dict(FakeDevice.status_error)
        if FakeDevice.status_dps is not None:
            return {"dps": dict(FakeDevice.status_dps)}
        dps = robot_dps()
        if FakeDevice.plain_status_only:
            dps = {k: v for k, v in dps.items() if not isinstance(v, str) or k == "158"}
        return {"dps": dps}

    def updatedps(self, index=None, nowait=False):
        self.update_requests = list(index or [])

    def set_multiple_values(self, dps, nowait=False):
        with self.lock:
            self.sent.append(dps)

    def push(self, dps: dict):
        with self.lock:
            self.pending.append({"dps": dps})

    def receive(self):
        time.sleep(0.02)
        with self.lock:
            return self.pending.pop(0) if self.pending else None


@pytest.fixture
def fake_tuya(monkeypatch):
    FakeDevice.instances.clear()
    FakeDevice.plain_status_only = False
    FakeDevice.status_dps = None
    FakeDevice.status_error = None
    monkeypatch.setattr("custom_components.eufy_clean_local.coordinator.tinytuya.Device", FakeDevice)
    yield FakeDevice


def _varint(n: int) -> bytes:
    return c._varint(n)


def _blob(msg) -> bytes:
    body = msg.SerializeToString()
    return _varint(len(body)) + body


def _room_params_and_pose():
    params = pb.RoomParams(map_id=7, releases=3)
    r1 = params.rooms.add(id=1, name="Kitchen")
    r1.order.value = 1
    r1.custom.fan.suction = 2  # Turbo
    r1.custom.clean_times = 1
    r2 = params.rooms.add(id=2)
    r2.scene.type = 2  # bedroom, no custom name
    r2.order.value = 2
    pose = pb.DynamicData()
    pose.cur_pose.x, pose.cur_pose.y = 20, 10
    return params, pose


def _boundary(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """A rectangle's boundary cells, traced in order like the robot's room outlines."""
    return ([(x, y0) for x in range(x0, x1)] + [(x1, y) for y in range(y0, y1)]
            + [(x, y1) for x in range(x1, x0, -1)] + [(x0, y) for y in range(y1, y0, -1)])


def synthetic_snapshot() -> mapdata.MapSnapshot:
    """A map in the L60's format: a 50x40 zlib grid inside LZ4, two traced rooms, a dock and the robot.

    The grid spans -1.25..1.25 m by -1.0..1.0 m at 5 cm; walls ring the grid with a dividing wall at
    column 25 (door at rows 19-20). Room 1 (label 10) is the left half, room 2 (label 11) the right.
    """
    import struct
    import zlib

    w, h = 50, 40
    cells = bytearray(w * h)
    for i in range(w * h):
        x, y = i % w, i // w
        if 5 <= x <= 44 and 5 <= y <= 34:
            edge = x in (5, 44) or y in (5, 34) or (x == 25 and y not in (19, 20))
            cells[i] = 128 if edge else 127
    header = struct.pack("<6I4I3I2I5fI", 1792077037, 1792077486, 1, 0, 0, 0, 5, 44, 5, 34,
                         2021041401, 0, 2, w, h, -1.25, -1.0, 1.25, 1.0, 0.1, w * h)
    assert len(header) == mapdata.GRID_HEADER
    grid = zlib.compress(header + bytes(cells))
    m = pb.Map(id=7, releases=3)  # the L60 leaves map_id unset and fills id
    m.info.width, m.info.height, m.info.resolution = 40, 30, 5
    m.info.origin.x, m.info.origin.y = -100, -75
    dock = m.info.docks_v2.add()
    dock.pose.x, dock.pose.y = 0, -60
    m.pixels = lz4.block.compress(grid, store_size=False)
    m.pixel_size = len(grid)

    rooms = [(10, _boundary(6, 6, 24, 33)), (11, _boundary(26, 6, 43, 33))]
    body = struct.pack("<II", 1792077486, len(rooms))
    for label, pts in rooms:
        body += struct.pack("<II", label, len(pts)) + b"".join(struct.pack("<III", x, y, 0) for x, y in pts)
    outline_z = zlib.compress(body)
    o = pb.RoomOutline(map_id=7, releases=3, width=40, height=30, resolution=5)
    o.origin.x, o.origin.y = -100, -75
    o.pixels = lz4.block.compress(outline_z, store_size=False)
    o.pixel_size = len(outline_z)

    params, pose = _room_params_and_pose()
    snap = mapdata.MapSnapshot()
    snap.raw = {2: _blob(m), 4: _blob(o), 5: _blob(params), 7: _blob(pose)}
    return snap


def legacy_snapshot() -> mapdata.MapSnapshot:
    """The older layout: plain LZ4 pixels, 2 bits per map cell and a byte per outline cell."""
    w, h = 40, 30
    m = pb.Map(map_id=7, releases=3)
    m.info.map_id, m.info.width, m.info.height, m.info.resolution = 7, w, h, 5
    m.info.origin.x, m.info.origin.y = -100, -75
    dock = m.info.docks_v2.add()
    dock.pose.x, dock.pose.y = 0, -60
    packed = bytearray((w * h + 3) // 4)
    outline = bytearray(w * h)
    for i in range(w * h):
        x, y = i % w, i // w
        edge = x in (0, w - 1) or y in (0, h - 1) or (x == 20 and y not in (14, 15))
        value = 1 if edge else 2
        packed[i >> 2] |= value << ((i & 3) * 2)
        if not edge:
            outline[i] = ((1 if x < 20 else 2) << 2) | 1
    m.pixels = lz4.block.compress(bytes(packed), store_size=False)
    m.pixel_size = len(packed)
    o = pb.RoomOutline(map_id=7, releases=3, width=w, height=h, resolution=5)
    o.origin.x, o.origin.y = -100, -75
    o.pixels = lz4.block.compress(bytes(outline), store_size=False)
    o.pixel_size = len(outline)
    params, pose = _room_params_and_pose()
    snap = mapdata.MapSnapshot()
    snap.raw = {2: _blob(m), 4: _blob(o), 5: _blob(params), 7: _blob(pose)}
    return snap
