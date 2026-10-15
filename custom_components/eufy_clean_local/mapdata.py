"""Decode the L60's map channels and render them as a PNG.

The robot publishes its map as numbered channels, each a varint-length-prefixed
protobuf from proto.cloud.stream: 2 = Map, 4 = RoomOutline, 5 = RoomParams,
6 = RestrictedZone, 7 = DynamicData (robot pose).

On the L60 the Map and RoomOutline pixel fields are a raw LZ4 block wrapping a
zlib stream:

* Map: an 84-byte little-endian header (width and height at bytes 52 and 56,
  the grid's bounds in metres as four floats at 60, the data size at 80), then
  one byte per cell: 0 unknown, 127 floor, 128 wall. Row 0 is the lowest y.
* RoomOutline: a timestamp and a room count, then per room a label (room id + 9)
  and its traced boundary as (column, row, neighbour) uint32 triples in the
  Map's grid.

Older firmware sends plain LZ4 pixels instead (2 bits per map cell, one byte per
outline cell with the room id in the high 6 bits); that layout is still read.
"""

from __future__ import annotations

import functools
import io
import logging
import math
import struct
import zlib
from dataclasses import dataclass, field

from . import pb

_LOGGER = logging.getLogger(__name__)

CH_MAP = 2
CH_ROOM_OUTLINE = 4
CH_ROOM_PARAMS = 5
CH_RESTRICTED = 6
CH_POSE = 7
CH_PATH = 3
CH_TEMP = 8  # the current task's selection (rooms or zones)
CHANNELS = (CH_MAP, CH_ROOM_OUTLINE, CH_ROOM_PARAMS, CH_RESTRICTED, CH_POSE)
CHANNEL_TYPES = {
    CH_MAP: pb.Map,
    CH_ROOM_OUTLINE: pb.RoomOutline,
    CH_ROOM_PARAMS: pb.RoomParams,
    CH_RESTRICTED: pb.RestrictedZone,
    CH_POSE: pb.DynamicData,
    CH_TEMP: pb.TemporaryData,
}

UNKNOWN, WALL, FLOOR_CELL, CARPET_CELL = 0, 1, 2, 3
GRID_HEADER = 84
ROOM_LABEL_OFFSET = 9

# Colours from the eufy app's light map style.
FLOOR = (0xDC, 0xDF, 0xE5, 255)
WALL_COLOUR = (0x8F, 0x9A, 0xB5, 255)
CARPET = (0xB8, 0xC4, 0xA8, 255)
ROOM_COLOURS = [(0xF6, 0xD4, 0xD7), (0xCC, 0xF1, 0xD0), (0xF5, 0xE2, 0xC8), (0xC4, 0xE7, 0xF9)]
NO_GO = (0xFF, 0x4C, 0x4C)
DOCK = (0x4C, 0xA2, 0xFF)
ROBOT = (0x00, 0x5D, 0x8E)
HIGHLIGHT = (0x1E, 0x88, 0xE5)  # rooms being cleaned on the live map
PATH = (0x4A, 0x68, 0x9C)  # the cleaned path; darker than the app's white so it shows on pastel rooms
PATH_WIDTH = 4  # px on the SCALE-enlarged map
LABEL = (0x3A, 0x3F, 0x4D, 255)
SCALE = 3
ROBOT_ICON_PX = 26  # drawn size of the robot icon on the map
BUMPER = (0x4C, 0xA2, 0xFF)

SCENE_NAMES = {
    1: "Study", 2: "Bedroom", 3: "Bathroom", 4: "Kitchen", 5: "Living room",
    6: "Dining room", 7: "Corridor", 8: "Guest room", 9: "Powder room",
}


def strip_length(raw: bytes) -> bytes:
    length, shift, pos = 0, 0, 0
    while True:
        b = raw[pos]
        length |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            break
        shift += 7
    if length > len(raw) - pos:
        raise ValueError(f"length prefix {length} exceeds blob size {len(raw) - pos}")
    return raw[pos:pos + length]


def parse_channel(channel: int, raw: bytes):
    msg = CHANNEL_TYPES[channel]()
    msg.ParseFromString(strip_length(raw))
    return msg


def map_id_of(msg) -> int | None:
    """The map id a channel's message belongs to (the L60 leaves Map.map_id at 0 and sets Map.id)."""
    if isinstance(msg, pb.Map):
        return msg.map_id or msg.id or msg.info.map_id
    return getattr(msg, "map_id", None) if msg is not None and "map_id" in msg.DESCRIPTOR.fields_by_name else None


def _lz4(data: bytes, size: int) -> bytes:
    """Decompress a raw LZ4 block (no frame header).

    Pure Python so the integration needs no compiled package: the lz4 wheel is
    not available for every Home Assistant OS platform.
    """
    out = bytearray()
    src = memoryview(data)
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literal = token >> 4
        if literal == 15:
            while True:
                b = src[i]
                i += 1
                literal += b
                if b != 255:
                    break
        out += src[i:i + literal]
        i += literal
        if i >= n:  # the last sequence has literals only
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if not offset or offset > len(out):
            raise ValueError("corrupt LZ4 block")
        match = (token & 15) + 4
        if match == 19:
            while True:
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        start = len(out) - offset
        if match <= offset:
            out += out[start:start + match]
        else:  # overlapping copy repeats the last `offset` bytes
            for k in range(match):
                out.append(out[start + k])
    if size and len(out) != size:
        raise ValueError(f"LZ4 block decoded to {len(out)} bytes, expected {size}")
    return bytes(out)


def _unpack(data: bytes, size: int) -> tuple[bytes, bool]:
    """LZ4-decode a pixel field; the bool says whether the result was a zlib stream (and is now inflated)."""
    raw = _lz4(data, size)
    if raw[:1] == b"\x78":
        try:
            return zlib.decompress(raw), True
        except zlib.error:
            pass
    return raw, False


# The L60's cell values (0 unknown, 127 floor, 128 wall) mapped to ours; anything else counts as floor.
_CELL_TABLE = bytes(UNKNOWN if v == 0 else WALL if v == 128 else FLOOR_CELL for v in range(256))


@dataclass
class Grid:
    """The occupancy grid: one byte per cell, row 0 at the lowest y, positions in cm."""

    width: int
    height: int
    cells: bytes
    origin_x: float
    origin_y: float
    resolution: float


def map_grid(m) -> Grid | None:
    info = m.info
    data, inflated = _unpack(m.pixels, m.pixel_size)
    if inflated:
        if len(data) < GRID_HEADER:
            raise ValueError(f"map grid of {len(data)} bytes has no header")
        w, h = struct.unpack_from("<II", data, 52)
        x0, y0, x1, _ = struct.unpack_from("<4f", data, 60)
        size = struct.unpack_from("<I", data, 80)[0]
        if not w or not h or len(data) < w * h:
            raise ValueError(f"map grid {w}x{h} does not fit {len(data)} bytes")
        start = GRID_HEADER if size == w * h and len(data) >= GRID_HEADER + size else len(data) - w * h
        res = (x1 - x0) * 100 / w if x1 > x0 else info.resolution or 5
        return Grid(w, h, data[start:start + w * h].translate(_CELL_TABLE), x0 * 100, y0 * 100, res)
    w, h = info.width, info.height
    if not w or not h:
        return None
    cells = bytes((data[i >> 2] >> ((i & 3) * 2)) & 3 if (i >> 2) < len(data) else 0 for i in range(w * h))
    return Grid(w, h, cells, info.origin.x, info.origin.y, info.resolution or 5)


def room_cells(outline, grid: Grid, legacy_ok: bool, room_ids: set[int]) -> bytes | None:
    """One byte per grid cell holding its room id (0 = none)."""
    from PIL import Image, ImageDraw

    data, inflated = _unpack(outline.pixels, outline.pixel_size)
    if not inflated:
        if not legacy_ok or outline.width != grid.width or outline.height != grid.height:
            return None
        return bytes(v >> 2 for v in data[:grid.width * grid.height])
    count = struct.unpack_from("<I", data, 4)[0]
    pos, rooms = 8, []
    for _ in range(count):
        label, n = struct.unpack_from("<II", data, pos)
        pos += 8
        pts = [struct.unpack_from("<II", data, pos + 12 * i) for i in range(n)]
        pos += 12 * n
        rooms.append((label, pts))
    by_label = all(label - ROOM_LABEL_OFFSET in room_ids for label, _ in rooms) or not room_ids
    img = Image.new("L", (grid.width, grid.height), 0)
    draw = ImageDraw.Draw(img)
    for index, (label, pts) in enumerate(rooms):
        room_id = label - ROOM_LABEL_OFFSET if by_label else sorted(room_ids)[index] if index < len(room_ids) else 0
        if len(pts) >= 3 and 0 < room_id < 256:
            draw.polygon(pts, fill=room_id, outline=room_id)
    return img.tobytes()


def room_colours(room_px: bytes, w: int, h: int) -> dict[int, tuple[int, int, int]]:
    """Give neighbouring rooms different colours (greedy, rooms up to 3 cells apart count as neighbours)."""
    near: dict[int, set[int]] = {}
    for i, a in enumerate(room_px):
        if not a:
            continue
        near.setdefault(a, set())
        x = i % w
        for d in (1, 2, 3):
            for j in ((i + d) if x + d < w else -1, i + d * w):
                if 0 <= j < len(room_px):
                    b = room_px[j]
                    if b and b != a:
                        near[a].add(b)
                        near.setdefault(b, set()).add(a)
    colours: dict[int, tuple[int, int, int]] = {}
    for room in sorted(near, key=lambda r: (-len(near[r]), r)):
        used = {colours[n] for n in near[room] if n in colours}
        colours[room] = next((c for c in ROOM_COLOURS if c not in used), ROOM_COLOURS[room % len(ROOM_COLOURS)])
    return colours


@dataclass
class MapSnapshot:
    """The decoded channels of one map, as fetched from the cloud."""

    raw: dict[int, bytes] = field(default_factory=dict)

    def get(self, channel: int):
        blob = self.raw.get(channel)
        if not blob:
            return None
        try:
            return parse_channel(channel, blob)
        except Exception as err:
            _LOGGER.debug("Could not parse map channel %s: %s", channel, err)
            return None

    @property
    def map_id(self) -> int | None:
        """The robot's id for this map, which room and zone cleaning must quote."""
        ids = [map_id_of(self.get(ch)) for ch in (CH_ROOM_PARAMS, CH_ROOM_OUTLINE, CH_MAP) if ch in self.raw]
        ids = [i for i in ids if i is not None]
        return next((i for i in ids if i), ids[0] if ids else None)

    def rooms(self) -> list[dict]:
        params = self.get(CH_ROOM_PARAMS)
        if params is None:
            return []
        out, seen = [], {}
        for room in sorted(params.rooms, key=lambda r: (r.order.value or 255, r.id)):
            name = room.name.strip()
            if not name:
                base = SCENE_NAMES.get(room.scene.type, "Room")
                seen[base] = seen.get(base, 0) + 1
                name = f"{base} {room.scene.index.value or seen[base]}" if base != "Room" else f"Room {room.id}"
            out.append({"id": room.id, "name": name})
        return out

    def pose(self) -> tuple[float, float, float] | None:
        """The robot's position (cm) and heading (radians x 100) from the pose channel, if present."""
        pose = self.get(CH_POSE)
        if pose is not None and pose.HasField("cur_pose"):
            return float(pose.cur_pose.x), float(pose.cur_pose.y), float(pose.cur_pose.theta)
        return None

    def path(self) -> list[tuple[float, float]]:
        """The cleaned path (cm) from the path channel, oldest first."""
        return decode_path(self.raw.get(CH_PATH))

    def render_png(self) -> bytes | None:
        renderer = MapRenderer.build(self)
        return renderer.frame(self.pose(), self.path()) if renderer is not None else None


class MapRenderer:
    """The floor plan drawn once; the path, labels, dock and robot are added per frame.

    Live updates only move the robot and extend the path, so the expensive part
    (cells, rooms, zones) is cached and each frame is a copy plus a few shapes.
    """

    def __init__(self, base, grid: Grid, docks, labels, box, angle: int, room_px: bytes | None = None) -> None:
        self.base = base
        self.room_px = room_px
        self._highlights: dict = {}
        self.grid = grid
        self.docks = docks
        self.labels = labels
        self.box = box
        self.angle = angle

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        g = self.grid
        return ((x - g.origin_x) / g.resolution * SCALE, (g.height - (y - g.origin_y) / g.resolution) * SCALE)

    @classmethod
    def build(cls, snapshot: "MapSnapshot") -> "MapRenderer | None":
        from PIL import Image, ImageDraw

        m = snapshot.get(CH_MAP)
        if m is None or not m.pixels:
            return None
        grid = map_grid(m)
        if grid is None:
            return None
        w, h = grid.width, grid.height
        rooms = {r["id"]: r["name"] for r in snapshot.rooms()}

        room_px = None
        outline = snapshot.get(CH_ROOM_OUTLINE)
        if outline is not None and outline.pixels and map_id_of(outline) == map_id_of(m):
            try:
                room_px = room_cells(outline, grid, legacy_ok=not m.info.width or m.info.width == w, room_ids=set(rooms))
            except (struct.error, ValueError) as err:
                _LOGGER.debug("Could not decode the room outline: %s", err)

        colours = room_colours(room_px, w, h) if room_px is not None else {}
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        put = img.load()
        sums: dict[int, list[int]] = {}
        for i, value in enumerate(grid.cells):
            if not value:
                continue
            x, row = i % w, i // w
            y = h - 1 - row
            if value == WALL:
                put[x, y] = WALL_COLOUR
                continue
            colour = FLOOR
            room_id = room_px[i] if room_px is not None else 0
            if room_id:
                colour = colours.get(room_id, ROOM_COLOURS[0]) + (255,)
                acc = sums.setdefault(room_id, [0, 0, 0])
                acc[0] += x
                acc[1] += y
                acc[2] += 1
            if value == CARPET_CELL:
                colour = CARPET if colour == FLOOR else tuple(int(a * 0.85) for a in colour[:3]) + (255,)
            put[x, y] = colour

        img = img.resize((w * SCALE, h * SCALE), Image.NEAREST)
        renderer = cls(img, grid, [], [], None, m.info.angle, room_px)
        draw = ImageDraw.Draw(img)
        zones = snapshot.get(CH_RESTRICTED)
        if zones is not None:
            for quad in list(zones.forbidden_zones) + list(zones.ban_mop_zones):
                pts = [renderer.to_px(p.x, p.y) for p in (quad.p0, quad.p1, quad.p2, quad.p3)]
                draw.polygon(pts, fill=NO_GO + (60,), outline=NO_GO + (255,))
            for line in zones.virtual_walls:
                draw.line([renderer.to_px(line.p0.x, line.p0.y), renderer.to_px(line.p1.x, line.p1.y)],
                          fill=NO_GO + (255,), width=3)
        renderer.labels = [(((sx / n + 0.5) * SCALE, (sy / n + 0.5) * SCALE), rooms[room_id])
                           for room_id, (sx, sy, n) in sums.items() if room_id in rooms and n]
        renderer.docks = [renderer.to_px(d.pose.x, d.pose.y) for d in m.info.docks_v2] or \
            [renderer.to_px(d.x, d.y) for d in m.info.docks]
        box = img.getbbox()
        if box:
            pad = 6 * SCALE
            renderer.box = (max(box[0] - pad, 0), max(box[1] - pad, 0),
                            min(box[2] + pad, img.width), min(box[3] + pad, img.height))
        return renderer

    def highlight(self, rooms: tuple[int, ...]):
        """A see-through overlay with an outline over the given rooms (cached per selection)."""
        from PIL import Image, ImageChops, ImageFilter

        if rooms in self._highlights:
            return self._highlights[rooms]
        g = self.grid
        wanted = set(rooms)
        mask = Image.frombytes("L", (g.width, g.height),
                               bytes(255 if v in wanted else 0 for v in self.room_px[:g.width * g.height]))
        mask = mask.transpose(Image.FLIP_TOP_BOTTOM).resize((g.width * SCALE, g.height * SCALE), Image.NEAREST)
        edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(5)))
        overlay = Image.new("RGBA", mask.size, HIGHLIGHT + (0,))
        overlay.putalpha(ImageChops.lighter(mask.point(lambda v: 70 if v else 0), edge.point(lambda v: 230 if v else 0)))
        if len(self._highlights) > 8:
            self._highlights.clear()
        self._highlights[rooms] = overlay
        return overlay

    def frame(self, pose: tuple[float, float] | None = None, path: list | None = None,
              fast: bool = False, rooms: tuple[int, ...] = ()) -> bytes:
        """One PNG: the cached floor plan plus the rooms being cleaned, path, room names, dock and robot."""
        from PIL import Image, ImageDraw, ImageFont

        img = self.base.copy()
        if rooms and self.room_px is not None:
            img.alpha_composite(self.highlight(tuple(sorted(rooms))))
        if path and len(path) > 1:
            # Drawn on its own layer and blended, so overlaps stay one even tint and the
            # dashboard's background never shows through the line.
            trail = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ImageDraw.Draw(trail).line([self.to_px(x, y) for x, y in path], fill=PATH + (210,),
                                       width=PATH_WIDTH, joint="curve")
            img.alpha_composite(trail)
        draw = ImageDraw.Draw(img)
        if self.labels:
            try:
                font = ImageFont.load_default(size=13)
            except TypeError:  # Pillow without FreeType
                font = ImageFont.load_default()
            for centre, name in self.labels:
                draw.text(centre, name, fill=LABEL, font=font, anchor="mm",
                          stroke_width=2, stroke_fill=(255, 255, 255, 255))
        for cx, cy in self.docks:
            draw.rectangle([cx - 5, cy - 5, cx + 5, cy + 5], fill=DOCK + (255,), outline=(255, 255, 255, 255))
        if pose is not None:
            cx, cy = self.to_px(pose[0], pose[1])
            heading = pose[2] / 100 if len(pose) > 2 else 0.0  # the robot reports radians x 100
            icon = robot_icon(heading)
            img.alpha_composite(icon, (round(cx - icon.width / 2), round(cy - icon.height / 2)))
        if self.box:
            img = img.crop(self.box)
        if self.angle in (90, 180, 270):
            img = img.rotate(-self.angle, expand=True)
        out = io.BytesIO()
        if fast:
            img.save(out, format="PNG", compress_level=1)
        else:
            img.save(out, format="PNG", optimize=True)
        return out.getvalue()


def read_pose(cloud, dev_id: str) -> tuple[float, float, float] | None:
    """The robot's current position (cm) and heading (radians x 100) from the cloud's pose channel (blocking)."""
    blob = pick_blob(CH_POSE, _entries(cloud.media_detail(dev_id, 1, CH_POSE, "", PAGE_SIZE)))
    if not blob:
        return None
    pose = parse_channel(CH_POSE, blob).cur_pose
    return float(pose.x), float(pose.y), float(pose.theta)


@functools.lru_cache(maxsize=1)
def _robot_icon_master(size: int):
    """A top-down robot vacuum facing +x (east), drawn at 4x for smooth edges."""
    from PIL import Image, ImageDraw

    s = size * 4
    c = s / 2
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    edge = s * 0.04
    d.ellipse([edge, edge + s * 0.03, s - edge, s - edge + s * 0.03], fill=(0, 0, 0, 60))  # soft shadow
    d.ellipse([edge, edge, s - edge, s - edge], fill=(255, 255, 255, 255), outline=ROBOT + (255,),
              width=round(s * 0.06))
    d.arc([edge, edge, s - edge, s - edge], start=-60, end=60, fill=BUMPER + (255,), width=round(s * 0.13))
    ring = s * 0.31
    d.ellipse([c - ring, c - ring, c + ring, c + ring], outline=(0xC9, 0xD4, 0xE1, 255), width=round(s * 0.035))
    turret = s * 0.15
    tx = c - s * 0.06  # the lidar tower sits just behind the centre
    d.ellipse([tx - turret, c - turret, tx + turret, c + turret], fill=ROBOT + (255,))
    d.ellipse([tx - turret * 0.45, c - turret * 0.45, tx + turret * 0.45, c + turret * 0.45],
              fill=(0x8F, 0xC3, 0xFF, 255))
    return img


def robot_icon(heading: float, size: int = ROBOT_ICON_PX):
    """The robot icon turned to `heading` (radians, counter-clockwise from east, as on the map)."""
    from PIL import Image

    master = _robot_icon_master(size).rotate(math.degrees(heading), resample=Image.BICUBIC)
    return master.resize((size, size), Image.LANCZOS)


def read_selected_rooms(cloud, dev_id: str) -> tuple[int, ...]:
    """The room ids of the current room clean (empty when it isn't one), from channel 8 (blocking)."""
    blob = pick_blob(CH_TEMP, _entries(cloud.media_detail(dev_id, 1, CH_TEMP, "", PAGE_SIZE)))
    if not blob:
        return ()
    selection = parse_channel(CH_TEMP, blob)
    return tuple(r.id for r in selection.select_rooms_clean.rooms)


def read_path(cloud, dev_id: str) -> list[tuple[float, float]]:
    """The cleaned path (cm) from the cloud's path channel (blocking)."""
    first = cloud.media_detail(dev_id, 1, CH_PATH, "", PAGE_SIZE)
    entries = _paged(lambda start: cloud.media_detail(dev_id, 1, CH_PATH, start, PAGE_SIZE), first)
    return decode_path(b"".join(entries)) if entries else []


PATH_RECORD = 5


def _sign_magnitude(value: int) -> float:
    return float(-(value & 0x7FFF) if value & 0x8000 else value)


def decode_path(raw: bytes | None) -> list[tuple[float, float]]:
    """The cleaned path (cm), oldest first.

    The L60 uploads its path as an append-only run of 5-byte records, a few per
    media entry: x and y as big-endian 16-bit sign-magnitude numbers in cm (the
    same frame as the robot pose), then a flag byte.
    """
    if not raw:
        return []
    return [(_sign_magnitude(int.from_bytes(raw[i:i + 2], "big")),
             _sign_magnitude(int.from_bytes(raw[i + 2:i + 4], "big")))
            for i in range(0, len(raw) - PATH_RECORD + 1, PATH_RECORD)]


# ---------------------------------------------------------------- cloud fetch
#
# The robot uploads each channel to the Tuya cloud's media store. Every dataList
# entry is one whole channel blob (varint length + protobuf), hex encoded, and
# the record's "mapId" is the channel number, not the robot's map id. The app
# asks for one channel at a time with media.detail(mapId=channel, datatype=0).

def _content_bytes(content: str) -> bytes:
    import base64
    import binascii
    import string

    if len(content) % 2 == 0 and all(ch in string.hexdigits for ch in content):
        return binascii.unhexlify(content)
    return base64.b64decode(content)


def _shape(obj, depth: int = 0) -> str:
    """Describe a JSON value's structure (keys, types, lengths) without its contents."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        return "{" + ", ".join(f"{k}: {_shape(v, depth + 1)}" for k, v in list(obj.items())[:20]) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)} x {_shape(obj[0], depth + 1) if obj else '-'}]"
    if isinstance(obj, str):
        return f"str({len(obj)})"
    return repr(obj) if isinstance(obj, (int, float, bool)) or obj is None else type(obj).__name__


MAX_PAGES = 20
PAGE_SIZE = 1000
DIAG_BYTES = 2_000_000  # cap on the raw entries kept for diagnostics


def _entries(page) -> list[bytes]:
    """The decoded entries of one media page: dataList strings (or their content), or a lone content."""
    if not isinstance(page, dict):
        return []
    items = page.get("dataList")
    if items is None and page.get("content"):
        items = [page["content"]]
    out = []
    for item in items or []:
        if isinstance(item, dict):
            item = item.get("content") or item.get("data")
        if isinstance(item, str) and item:
            try:
                out.append(_content_bytes(item))
            except ValueError:
                _LOGGER.debug("Skipping a media entry that is neither hex nor base64")
    return out


def _paged(call, first) -> list[bytes]:
    """Collect entries across pages (hasNext / startRow)."""
    entries = _entries(first)
    page, pages = first, 1
    while isinstance(page, dict) and page.get("hasNext") and pages < MAX_PAGES:
        page = call(page.get("startRow") or str(len(entries)))
        entries.extend(_entries(page))
        pages += 1
    return entries


def _whole(raw: bytes) -> bool:
    """True when the bytes are exactly one varint-length-prefixed message."""
    try:
        return len(strip_length(raw)) == len(raw) - _varint_size(raw)
    except (IndexError, ValueError):
        return False


def _varint_size(raw: bytes) -> int:
    pos = 0
    while raw[pos] & 0x80:
        pos += 1
    return pos + 1


def pick_blob(channel: int, entries: list[bytes]) -> bytes | None:
    """Choose a channel's current blob from its media entries.

    Each entry is normally a whole message; when there are several, the newest
    one (highest releases / seq, else the last) wins. Entries that only make
    sense together are tried concatenated.
    """
    msg_type = CHANNEL_TYPES.get(channel)
    if msg_type is None or not entries:
        return None
    candidates = []
    for index, raw in enumerate(entries):
        if not _whole(raw):
            continue
        try:
            msg = parse_channel(channel, raw)
        except Exception:  # noqa: BLE001 - a corrupt entry is skipped
            continue
        frame_p = channel == CH_MAP and msg.frame == pb.Map.P
        key = (not frame_p, getattr(msg, "releases", 0), getattr(msg, "seq", 0), index)
        candidates.append((key, raw))
    if candidates:
        return max(candidates)[1]
    joined = b"".join(entries)
    if _whole(joined):
        try:
            parse_channel(channel, joined)
            return joined
        except Exception:  # noqa: BLE001
            pass
    _LOGGER.debug("Channel %s: none of %d entries (%s bytes, starting %s) decoded", channel, len(entries),
                  [len(e) for e in entries[:10]], entries[0][:24].hex())
    return None


@dataclass
class FetchResult:
    """What one map refresh found, kept for the integration's diagnostics."""

    snapshot: MapSnapshot | None = None
    latest: dict = field(default_factory=dict)
    channels: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def fetch_snapshot(cloud, dev_id: str) -> FetchResult:
    """Fetch the robot's map channels from the Tuya cloud (blocking)."""
    result = FetchResult()
    latest = cloud.media_latest(dev_id, 0, PAGE_SIZE)
    _LOGGER.debug("media.latest -> %s", _shape(latest))
    if isinstance(latest, dict):
        result.latest = {k: v for k, v in latest.items() if k != "dataList"}
    sub_record = (latest or {}).get("subRecordId") or 1
    latest_channel = (latest or {}).get("mapId")
    budget = DIAG_BYTES

    def fetch(channel: int) -> list[bytes]:
        first = cloud.media_detail(dev_id, sub_record, channel, "", PAGE_SIZE)
        return _paged(lambda start: cloud.media_detail(dev_id, sub_record, channel, start, PAGE_SIZE), first)

    snapshot = MapSnapshot()
    for channel in CHANNELS:
        try:
            entries = fetch(channel)
        except Exception as err:  # noqa: BLE001 - one channel failing should not lose the others
            _LOGGER.debug("media.detail channel %s failed: %s", channel, err)
            result.channels[channel] = {"error": str(err)}
            continue
        if not entries and channel == latest_channel:
            entries = _paged(lambda start: cloud.media_latest(dev_id, start, PAGE_SIZE), latest)
        blob = pick_blob(channel, entries)
        result.channels[channel] = {"entries": [len(e) for e in entries[:50]], "used": len(blob) if blob else None}
        kept = []
        for raw in entries:
            if len(raw) <= budget:
                kept.append(raw.hex())
                budget -= len(raw)
        result.raw[channel] = kept
        if blob:
            snapshot.raw[channel] = blob

    try:  # the cleaned path is raw records, not a protobuf, so it is kept as-is
        path_entries = fetch(CH_PATH)
        if path_entries:
            snapshot.raw[CH_PATH] = b"".join(path_entries)
            result.channels[CH_PATH] = {"entries": len(path_entries), "points": len(snapshot.raw[CH_PATH]) // PATH_RECORD}
    except Exception as err:  # noqa: BLE001 - the map is still useful without its path
        _LOGGER.debug("media.detail channel %s failed: %s", CH_PATH, err)
        result.channels[CH_PATH] = {"error": str(err)}

    _LOGGER.debug("Map channels (sub record %s): %s", sub_record, result.channels)
    result.snapshot = snapshot if snapshot.raw else None
    return result
