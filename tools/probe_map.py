"""Probe the eufy/Tuya cloud map API for an L60 and save what comes back.

Asks for the eufy password interactively (never stored or printed). Prints only
the structure of the responses; raw map channels go to local/probe/ for analysis.

    python tools/probe_map.py you@example.com
"""

from __future__ import annotations

import base64
import binascii
import getpass
import json
import pathlib
import string
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "eufy_clean_local"
OUT = ROOT / "local" / "probe"

stub = types.ModuleType("eufy_clean_local")
stub.__path__ = [str(PKG)]
sys.modules["eufy_clean_local"] = stub
from eufy_clean_local import mapdata  # noqa: E402
from eufy_clean_local.eufy_cloud import EufyCloud  # noqa: E402

SECRET_KEYS = {"localKey", "sid", "token", "secKey", "ecode", "uid", "password"}


def describe(obj, indent=0, key=""):
    pad = "  " * indent
    if key in SECRET_KEYS:
        print(f"{pad}{key}: <redacted>")
    elif isinstance(obj, dict):
        print(f"{pad}{key + ': ' if key else ''}{{{len(obj)} keys}}")
        for k, v in obj.items():
            describe(v, indent + 1, k)
    elif isinstance(obj, list):
        print(f"{pad}{key + ': ' if key else ''}[{len(obj)} items]")
        for v in obj[:3]:
            describe(v, indent + 1)
    elif isinstance(obj, str) and len(obj) > 40:
        kind = "hex" if all(ch in string.hexdigits for ch in obj) else "text/base64"
        print(f"{pad}{key}: str len={len(obj)} ({kind}) starts {obj[:16]!r}")
    else:
        print(f"{pad}{key}: {obj!r}")


def find(obj, names):
    """Depth-first search for the first value under any of the given keys."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            hit = find(v, names)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, names)
            if hit is not None:
                return hit
    return None


def to_bytes(content: str) -> bytes:
    if all(ch in string.hexdigits for ch in content) and len(content) % 2 == 0:
        return binascii.unhexlify(content)
    return base64.b64decode(content)


def main() -> None:
    email = sys.argv[1] if len(sys.argv) > 1 else input("eufy email: ")
    password = getpass.getpass("eufy password (not shown): ")
    OUT.mkdir(parents=True, exist_ok=True)
    cloud = EufyCloud(email, password)
    devices = cloud.devices()
    for d in devices:
        print(f"device: {d['name']} model={d['model']} key={'yes' if d['local_key'] else 'no'}")
    l60 = next((d for d in devices if d["model"] in ("T2267", "T2268", "T2277", "T2278")), None)
    if not l60:
        sys.exit("No L60 on this account")
    dev_id = l60["id"]

    # Ask the robot to sync first is done by the integration; here we just look at the API.
    tuya = cloud.tuya

    def call(action, data, version):
        try:
            res = tuya.request(action, data, version=version)
        except Exception as err:
            print(f"  {action} v{version} {data}: FAILED {err}")
            return None
        short = {k: v for k, v in res.items() if k != "devId"} if isinstance(res, dict) else res
        print(f"  {action} v{version} {dict((k, v) for k, v in data.items() if k != 'devId')}:")
        print("   ", json.dumps(short)[:600])
        return res

    print("\n== media.latest variants")
    results = {}
    for version in ("3.0", "2.0"):
        for size in (10, 500):
            results[(version, size)] = call("tuya.m.device.media.latest", {"devId": dev_id, "start": 0, "size": size}, version)
    (OUT / "latest_variants.json").write_text(json.dumps({f"{k[0]}/{k[1]}": v for k, v in results.items()}, indent=1))
    latest = next((r for r in results.values() if r), None)
    if latest is None:
        sys.exit("media.latest failed")
    sub_record = find(latest, {"subRecordId", "subRecordID"})
    map_id = find(latest, {"mapId", "mapID", "map_id"})
    print(f"\nsubRecordId={sub_record!r} mapId={map_id!r}")

    print("\n== media.detail variants (channel 2 = map, 5 = rooms)")
    snapshot = mapdata.MapSnapshot()
    details = {}
    for channel in (2, 5):
        for version in ("3.0", "2.0"):
            for start, size in ((0, 500), ("0", 500), (0, 50)):
                data = {"devId": dev_id, "subRecordId": sub_record, "mapId": map_id,
                        "start": start, "size": size, "datatype": channel}
                res = call("tuya.m.device.media.detail", data, version)
                details[f"ch{channel}/v{version}/{start!r}/{size}"] = res
                content = find(res, {"content"}) if res else None
                if isinstance(content, str) and content:
                    raw = to_bytes(content)
                    (OUT / f"ch{channel}.bin").write_bytes(raw)
                    try:
                        mapdata.parse_channel(channel, raw)
                        snapshot.raw[channel] = raw
                        print(f"    -> channel {channel}: {len(raw)} bytes, parsed OK")
                    except Exception as err:
                        print(f"    -> channel {channel}: {len(raw)} bytes, parse failed: {err}")
    (OUT / "detail_variants.json").write_text(json.dumps(details, indent=1))

    print("\nrooms:", snapshot.rooms())
    print("map_id:", snapshot.map_id)
    try:
        png = snapshot.render_png()
        if png:
            (OUT / "map.png").write_bytes(png)
            print(f"rendered {OUT / 'map.png'} ({len(png)} bytes)")
    except Exception as err:
        print(f"render failed: {err!r}")


if __name__ == "__main__":
    main()
