"""Measure how often the L60's position updates in the cloud while it cleans.

Read-only: it never sends anything to the robot. It logs in to eufy once
(the password is asked for and not stored), waits until the robot is cleaning,
then every few seconds reads the robot-position channel (7) and the cloud's
"latest upload" record, and logs every change with a timestamp. It stops once
the robot is back on the dock, or after --max-minutes.

    python tools/watch_position.py you@example.com            # wait for a clean
    python tools/watch_position.py you@example.com --now      # robot already cleaning

Results go to local/position/ (git-ignored) and a summary is printed at the end.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import pathlib
import statistics
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
stub = types.ModuleType("eufy_clean_local")
stub.__path__ = [str(ROOT / "custom_components" / "eufy_clean_local")]
sys.modules["eufy_clean_local"] = stub
from eufy_clean_local import codec, mapdata  # noqa: E402
from eufy_clean_local.eufy_cloud import EufyCloud  # noqa: E402

OUT = ROOT / "local" / "position"
L60 = ("T2267", "T2268", "T2277", "T2278")


def now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def robot_state(cloud: EufyCloud, dev_id: str) -> str:
    dps = cloud.device_dps(dev_id)
    ws = codec.decode_dp(codec.DP_WORK_STATUS, dps.get("153")) if dps.get("153") else None
    return codec.vacuum_state(ws) if ws is not None else "unknown"


def read_pose(cloud: EufyCloud, dev_id: str, sub_record) -> tuple | None:
    page = cloud.media_detail(dev_id, sub_record, mapdata.CH_POSE, "", 1000)
    blob = mapdata.pick_blob(mapdata.CH_POSE, mapdata._entries(page))
    if not blob:
        return None
    pose = mapdata.parse_channel(mapdata.CH_POSE, blob).cur_pose
    return pose.x, pose.y, pose.theta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--now", action="store_true", help="start measuring immediately")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between reads while cleaning")
    ap.add_argument("--max-minutes", type=float, default=45)
    args = ap.parse_args()

    cloud = EufyCloud(args.email, getpass.getpass("eufy password (not shown): "))
    dev = next((d for d in cloud.devices() if d["model"] in L60), None)
    if dev is None:
        sys.exit("No L60 on this account")
    dev_id = dev["id"]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"position-{dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    print(f"{now()} logged in; found {dev['name']}. Log: {path}")

    if not args.now:
        print(f"{now()} waiting for the robot to start cleaning (checking every 30 s; Ctrl-C to stop)")
        last_note = 0.0
        while True:
            try:
                state = robot_state(cloud, dev_id)
            except Exception as err:  # a network blip shouldn't end a long wait
                print(f"{now()} state check failed, retrying: {err}")
                time.sleep(60)
                continue
            if state in ("cleaning", "returning"):
                break
            if time.monotonic() - last_note > 600:
                print(f"{now()} still waiting (robot is {state})")
                last_note = time.monotonic()
            time.sleep(30)

    print(f"{now()} robot is cleaning; reading the cloud every {args.interval:g} s")
    pose_changes: list[float] = []  # monotonic times when the position changed
    uploads: dict = {}  # channel -> list of upload times seen in media.latest
    last_pose, last_upload, errors = None, None, 0
    started = time.monotonic()
    docked_since = None
    next_state = time.monotonic() + 60
    with path.open("w", newline="") as f:
        log = csv.writer(f)
        log.writerow(["time", "kind", "a", "b", "c", "d"])
        while time.monotonic() - started < args.max_minutes * 60:
            t0 = time.monotonic()
            try:
                latest = cloud.media_latest(dev_id, 0, 1000)
                upload = (latest.get("mapId"), latest.get("startTime"), latest.get("endTime"))
                if upload != last_upload:
                    last_upload = upload
                    uploads.setdefault(upload[0], []).append(upload[1])
                    log.writerow([now(), "upload", *upload, len(latest.get("dataList") or [])])
                sub = latest.get("subRecordId") or 1
                pose = read_pose(cloud, dev_id, sub)
                if pose is not None and pose != last_pose:
                    pose_changes.append(t0)
                    gap = f"{pose_changes[-1] - pose_changes[-2]:.1f} s" if len(pose_changes) > 1 else "first"
                    print(f"{now()} position x={pose[0]} y={pose[1]} theta={pose[2]}  ({gap} since last change)")
                    log.writerow([now(), "pose", *pose, gap])
                    last_pose = pose
                errors = 0
            except Exception as err:  # keep measuring through transient errors
                errors += 1
                print(f"{now()} read failed ({errors}): {err}")
                log.writerow([now(), "error", str(err)[:200], "", "", ""])
                if errors >= 5:
                    print(f"{now()} backing off 60 s")
                    time.sleep(60)
            if time.monotonic() >= next_state:
                try:
                    state = robot_state(cloud, dev_id)
                except Exception as err:  # keep measuring; try again next minute
                    print(f"{now()} state check failed: {err}")
                    state = "unknown"
                log.writerow([now(), "state", state, "", "", ""])
                next_state = time.monotonic() + 60
                if state == "docked":
                    docked_since = docked_since or time.monotonic()
                    if time.monotonic() - docked_since > 60:
                        print(f"{now()} robot docked; stopping")
                        break
                else:
                    docked_since = None
            f.flush()
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))

    print("\n== summary")
    gaps = [b - a for a, b in zip(pose_changes, pose_changes[1:])]
    if gaps:
        print(f"position changed {len(pose_changes)} times; seconds between changes: "
              f"median {statistics.median(gaps):.1f}, min {min(gaps):.1f}, max {max(gaps):.1f}")
    else:
        print(f"position changed {len(pose_changes)} time(s); not enough to measure a rate")
    for channel, times in sorted(uploads.items(), key=lambda kv: str(kv[0])):
        ts = sorted(t for t in times if isinstance(t, int))
        g = [b - a for a, b in zip(ts, ts[1:])]
        rate = f"median {statistics.median(g)} s apart" if g else "once"
        print(f"cloud uploads seen for channel {channel}: {len(times)} ({rate})")
    print(f"log: {path}")


if __name__ == "__main__":
    main()
