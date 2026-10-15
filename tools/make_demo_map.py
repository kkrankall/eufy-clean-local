"""Render the README's demo map images from a made-up home.

The home is encoded in the L60's real map format (an LZ4 block wrapping zlib,
with the same header, cell values and room outlines the robot uploads) and then
decoded and drawn by the integration's own renderer, so the pictures look exactly
like the map entity in Home Assistant. No real floor plan is involved.

    python tools/make_demo_map.py      # writes docs/images/*.png

Needs Pillow, protobuf and lz4 (lz4 only here, to compress the demo data).
"""

from __future__ import annotations

import pathlib
import random
import struct
import sys
import types
import zlib

import lz4.block

ROOT = pathlib.Path(__file__).resolve().parent.parent
stub = types.ModuleType("eufy_clean_local")
stub.__path__ = [str(ROOT / "custom_components" / "eufy_clean_local")]
sys.modules["eufy_clean_local"] = stub
from eufy_clean_local import mapdata, pb  # noqa: E402

OUT = ROOT / "docs" / "images"
RES = 5  # cm per cell
W, H = 250, 190  # 12.5 m x 9.5 m
MAP_ID = 2

# Rooms as (id, name, x0, y0, x1, y1) in cells, y up. Walls are 2 cells thick.
ROOMS = [
    (1, "Kitchen", 10, 110, 90, 180),
    (2, "Living Room", 92, 80, 200, 180),
    (3, "Hallway", 10, 80, 90, 108),
    (4, "Bedroom", 10, 10, 100, 78),
    (5, "Office", 102, 10, 170, 78),
    (6, "Bathroom", 172, 30, 240, 78),
]
# Openings in the walls: (x0, y0, x1, y1) cells cleared to floor.
DOORS = [
    (40, 107, 56, 111),   # kitchen - hallway
    (89, 88, 93, 104),    # hallway - living room
    (60, 77, 76, 81),     # hallway - bedroom
    (130, 77, 146, 81),   # living room - office
    (188, 77, 198, 81),   # living room - bathroom
    (89, 140, 93, 170),   # kitchen - living room (wide)
]
# Furniture and other obstacles the lidar sees inside rooms.
FURNITURE = [
    (20, 160, 80, 168),   # kitchen counter
    (40, 128, 62, 140),   # kitchen island
    (120, 150, 170, 162), # sofa
    (135, 115, 155, 128), # coffee table
    (195, 95, 198, 175),  # TV stand
    (20, 30, 55, 60),     # bed
    (110, 60, 140, 70),   # desk
    (210, 40, 236, 50),   # bath tub
]
DOCK = (172, 84)  # cells, against the living-room wall
ROBOT = (140, 145)


def to_cm(cell_x: float, cell_y: float) -> tuple[int, int]:
    return int(cell_x * RES - W * RES / 2), int(cell_y * RES - H * RES / 2)


def build_cells() -> bytearray:
    rnd = random.Random(7)
    floor = bytearray(W * H)
    for _, _, x0, y0, x1, y1 in ROOMS:
        for y in range(y0, y1):
            for x in range(x0, x1):
                floor[y * W + x] = 1
    for x0, y0, x1, y1 in DOORS:
        for y in range(y0, y1):
            for x in range(x0, x1):
                floor[y * W + x] = 1
    cells = bytearray(W * H)  # 0 unknown
    for i, f in enumerate(floor):
        if f:
            cells[i] = 127
    # Walls: unknown cells touching the floor, two cells thick, with a little lidar noise.
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            i = y * W + x
            if floor[i]:
                continue
            near = any(floor[(y + dy) * W + (x + dx)] for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2)
                       if 0 <= y + dy < H and 0 <= x + dx < W)
            if near and rnd.random() > 0.04:
                cells[i] = 128
    for x0, y0, x1, y1 in FURNITURE:
        for y in range(y0, y1):
            for x in range(x0, x1):
                edge = x in (x0, x1 - 1) or y in (y0, y1 - 1)
                if edge and rnd.random() > 0.15:
                    cells[y * W + x] = 128
                elif not edge:
                    cells[y * W + x] = 0  # the lidar can't see under furniture
    # Scattered small obstacles (chair legs and the like).
    for _ in range(90):
        _, _, x0, y0, x1, y1 = rnd.choice(ROOMS)
        x, y = rnd.randrange(x0 + 3, x1 - 3), rnd.randrange(y0 + 3, y1 - 3)
        if cells[y * W + x] == 127:
            cells[y * W + x] = 128
    return cells


def lz4_zlib(raw: bytes) -> tuple[bytes, int]:
    inner = zlib.compress(raw)
    return lz4.block.compress(inner, store_size=False), len(inner)


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def demo_path() -> bytes:
    """A cleaned-path trail in the robot's 5-byte record format: from the dock, then back-and-forth rows."""
    def record(cx: float, cy: float) -> bytes:
        x, y = to_cm(cx, cy)
        enc = lambda v: (0x8000 | -v) if v < 0 else v  # noqa: E731 - sign-magnitude int16
        return enc(x).to_bytes(2, "big") + enc(y).to_bytes(2, "big") + b"\x00"

    pts = [(DOCK[0], DOCK[1] + 4), (DOCK[0], 96), (178, 96)]
    y, left = 96, False
    while y <= 140:
        xs = (100, 178) if left else (178, 100)
        pts += [(xs[0], y), (xs[1], y)]
        y += 5
        left = not left
    pts.append(ROBOT)
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):  # a record every ~10 cm, like the robot's
        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) / 2))
        out += [record(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(steps)]
    return b"".join(out)


def build_snapshot(with_zones: bool) -> mapdata.MapSnapshot:
    cells = build_cells()
    header = bytearray(84)
    x0, y0 = to_cm(0, 0)
    x1, y1 = to_cm(W, H)
    struct.pack_into("<II", header, 52, W, H)
    struct.pack_into("<4f", header, 60, x0 / 100, y0 / 100, x1 / 100, y1 / 100)
    struct.pack_into("<I", header, 80, W * H)
    m = pb.Map(id=MAP_ID)
    m.pixels, m.pixel_size = lz4_zlib(bytes(header) + bytes(cells))
    dock = m.info.docks_v2.add()
    dock.pose.x, dock.pose.y = to_cm(*DOCK)

    outline = bytearray(struct.pack("<II", 1792000000, len(ROOMS)))
    for room_id, _, rx0, ry0, rx1, ry1 in ROOMS:
        pts = [(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1)]
        outline += struct.pack("<II", room_id + mapdata.ROOM_LABEL_OFFSET, len(pts))
        for px, py in pts:
            outline += struct.pack("<III", px, py, 0)
    o = pb.RoomOutline(map_id=MAP_ID)
    o.pixels, o.pixel_size = lz4_zlib(bytes(outline))

    params = pb.RoomParams(map_id=MAP_ID)
    for order, (room_id, name, *_ignored) in enumerate(ROOMS, start=1):
        room = params.rooms.add(id=room_id, name=name)
        room.order.value = order

    pose = pb.DynamicData()
    pose.cur_pose.x, pose.cur_pose.y = to_cm(*ROBOT)
    pose.cur_pose.theta = 157  # heading in radians x 100: facing west-north-west, away from its last row

    snap = mapdata.MapSnapshot()
    snap.raw = {mapdata.CH_MAP: _blob(m), mapdata.CH_ROOM_OUTLINE: _blob(o),
                mapdata.CH_ROOM_PARAMS: _blob(params), mapdata.CH_POSE: _blob(pose)}
    if not with_zones:
        snap.raw[mapdata.CH_PATH] = demo_path()
    if with_zones:
        zones = pb.RestrictedZone(map_id=MAP_ID)
        q = zones.forbidden_zones.add()  # keep out from under the dining area
        for name, (cx, cy) in zip(("p0", "p1", "p2", "p3"), ((22, 118), (70, 118), (70, 150), (22, 150))):
            getattr(q, name).x, getattr(q, name).y = to_cm(cx, cy)
        wall = zones.virtual_walls.add()  # block the bathroom doorway
        wall.p0.x, wall.p0.y = to_cm(186, 82)
        wall.p1.x, wall.p1.y = to_cm(200, 82)
        snap.raw[mapdata.CH_RESTRICTED] = _blob(zones)
    return snap


def _blob(msg) -> bytes:
    body = msg.SerializeToString()
    return _varint(len(body)) + body


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, zones in (("demo-map.png", False), ("demo-map-zones.png", True)):
        snap = build_snapshot(zones)
        png = snap.render_png()
        (OUT / name).write_bytes(png)
        print(f"{OUT / name}: {len(png)} bytes, rooms {[r['name'] for r in snap.rooms()]}, map id {snap.map_id}")


if __name__ == "__main__":
    main()
