"""Show what the eufy cloud knows about an L60's saved floors (multi-floor maps).

Reads the cloud's copy of the robot's settings (DP 176: is multi-floor on?) and
its last map-manager reply (DP 172: the saved maps, when the app last listed
them), and prints a summary. Raw DP values go to local/probe/ (git-ignored).
Asks for the eufy password interactively; it is never stored or printed.

    python tools/probe_floors.py you@example.com
"""

from __future__ import annotations

import base64
import getpass
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "eufy_clean_local"
OUT = ROOT / "local" / "probe"

stub = types.ModuleType("eufy_clean_local")
stub.__path__ = [str(PKG)]
sys.modules["eufy_clean_local"] = stub
from eufy_clean_local import codec as c  # noqa: E402
from eufy_clean_local import pb  # noqa: E402
from eufy_clean_local.eufy_cloud import EufyCloud  # noqa: E402

L60_MODELS = ("T2267", "T2268", "T2277", "T2278")


def size_of(pixels) -> str:
    return f"{len(pixels.pixels)} bytes" if pixels is not None and pixels.pixels else "none"


def describe_map(m) -> None:
    rooms = [r.name or f"room {r.id}" for r in m.room_params.rooms] if m.HasField("room_params") else []
    print(f"    map id {m.map_id}: name {m.name!r}, {m.map_width}x{m.map_height}, stable {m.map_stable}, "
          f"new {m.is_new_map}, releases {m.releases}")
    if hasattr(m, "room_outline"):
        print(f"      grid {size_of(m.map)}, room outline {size_of(m.room_outline)}")
    print(f"      rooms: {', '.join(rooms) or 'none listed'}")


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else input("eufy email: ")
    password = getpass.getpass("eufy password (not shown): ")
    cloud = EufyCloud(email, password)
    robot = next((d for d in cloud.devices() if d["model"] in L60_MODELS), None)
    if robot is None:
        sys.exit("No L60 on this account")
    info = cloud.tuya.get_device(robot["id"])
    print(f"{robot['name']} ({robot['model']}): cloud says online={info.get('isOnline')}")
    dps = info.get("dps") or {}
    print(f"cloud holds DPs: {sorted(dps, key=int)}")
    OUT.mkdir(parents=True, exist_ok=True)

    uni = dps.get(c.DP_UNISETTING)
    if uni:
        u = c.pb_decode(uni, pb.UnisettingResponse)
        print(f"\nDP 176 settings: multi-floor maps switch = {bool(u.multi_map_sw.value)}")
    else:
        print("\nDP 176 settings: not in the cloud")

    raw = dps.get(c.DP_MULTI_MAP)
    if not raw:
        print("DP 172 map manager: nothing stored (the app hasn't listed the maps since the cloud reset)")
        return
    (OUT / "dp172.b64").write_text(raw)
    body = base64.b64decode(raw)
    print(f"DP 172 map manager: {len(body)} bytes (saved to {OUT / 'dp172.b64'})")
    Resp = pb.message("MultiMapsManageResponse")
    try:
        r = c.pb_decode(raw, Resp)
    except Exception as err:
        sys.exit(f"  could not decode as MultiMapsManageResponse: {err}")
    print(f"  method {Resp.DESCRIPTOR.fields_by_name['method'].enum_type.values_by_number[r.method].name}, "
          f"result {Resp.Result.Name(r.result)}, seq {r.seq}, carries {r.WhichOneof('param')}")
    if r.HasField("complete_maps"):
        print(f"  {len(r.complete_maps.complete_map)} saved map(s):")
        for m in r.complete_maps.complete_map:
            describe_map(m)
    elif r.HasField("map_infos"):
        describe_map(r.map_infos)


if __name__ == "__main__":
    main()
