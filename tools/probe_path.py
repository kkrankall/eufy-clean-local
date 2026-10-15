"""Capture the L60's cleaned-path channel (3) from the cloud while it cleans.

Read-only. Asks for the eufy password (not stored), then takes a few samples of
channel 3 (path) and channel 7 (robot position) a few seconds apart, saves the
raw bytes to local/probe/path/ and prints how they decode under the plausible
layouts, so the integration can draw the trail.

    python tools/probe_path.py you@example.com
"""

from __future__ import annotations

import getpass
import json
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
stub = types.ModuleType("eufy_clean_local")
stub.__path__ = [str(ROOT / "custom_components" / "eufy_clean_local")]
sys.modules["eufy_clean_local"] = stub
from eufy_clean_local import mapdata, pb  # noqa: E402
from eufy_clean_local.eufy_cloud import EufyCloud  # noqa: E402

OUT = ROOT / "local" / "probe" / "path"
PathData = pb.message("CleanRecordData").PathData
PathPoint = pb.message("proto.cloud.stream.PathPoint")


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def layouts(xy: int) -> dict:
    hi, lo = s16(xy >> 16 & 0xFFFF), s16(xy & 0xFFFF)
    return {"hi=x lo=y": (hi, lo), "lo=x hi=y": (lo, hi)}


def try_decode(raw: bytes) -> list[str]:
    notes = []
    try:
        body = mapdata.strip_length(raw)
        whole = len(body) == len(raw) - mapdata._varint_size(raw)
    except Exception:  # noqa: BLE001
        body, whole = raw, False
    for name, cls in (("PathData", PathData), ("PathPoint", PathPoint)):
        try:
            msg = cls()
            msg.ParseFromString(body)
        except Exception as err:  # noqa: BLE001
            notes.append(f"  as {name}: no ({err})")
            continue
        pts = list(msg.points) if name == "PathData" else [msg]
        if pts and any(p.xy for p in pts):
            first = pts[:3]
            notes.append(f"  as {name} (length prefix ok={whole}): {len(pts)} points; first xy/flags "
                         f"{[(p.xy, p.flags) for p in first]}")
            for lay in ("hi=x lo=y", "lo=x hi=y"):
                notes.append(f"    {lay}: {[layouts(p.xy)[lay] for p in first]} ... last {layouts(pts[-1].xy)[lay]}")
    return notes


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else input("eufy email: ")
    cloud = EufyCloud(email, getpass.getpass("eufy password (not shown): "))
    dev = next(d for d in cloud.devices() if d["model"] in ("T2267", "T2268", "T2277", "T2278"))
    OUT.mkdir(parents=True, exist_ok=True)
    for sample in range(3):
        latest = cloud.media_latest(dev["id"], 0, 1000)
        sub = latest.get("subRecordId") or 1
        page = cloud.media_detail(dev["id"], sub, 3, "", 1000)
        entries = mapdata._paged(lambda start: cloud.media_detail(dev["id"], sub, 3, start, 1000), page)
        pose_blob = mapdata.pick_blob(7, mapdata._entries(cloud.media_detail(dev["id"], sub, 7, "", 1000)))
        pose = mapdata.parse_channel(7, pose_blob).cur_pose if pose_blob else None
        print(f"\n== sample {sample + 1}: {len(entries)} path entries, sizes {[len(e) for e in entries[:12]]}"
              f"{'...' if len(entries) > 12 else ''}; robot at {(pose.x, pose.y) if pose else '?'}")
        meta = {k: v for k, v in page.items() if k not in ("dataList", "devId")} if isinstance(page, dict) else {}
        print("  record:", json.dumps(meta)[:300])
        for i, raw in enumerate(entries):
            (OUT / f"s{sample + 1}_e{i}.bin").write_bytes(raw)
        for i, raw in enumerate(entries[:3]):
            print(f" entry {i}: {len(raw)} bytes, starts {raw[:24].hex()}")
            for line in try_decode(raw):
                print(line)
        if len(entries) > 3:
            print(f" last entry: {len(entries[-1])} bytes, starts {entries[-1][:24].hex()}")
            for line in try_decode(entries[-1]):
                print(line)
        time.sleep(5)
    print(f"\nraw entries saved in {OUT}")


if __name__ == "__main__":
    main()
