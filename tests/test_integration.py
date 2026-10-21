"""End-to-end tests against a simulated L60."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from homeassistant.components.vacuum import DATA_COMPONENT
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eufy_clean_local import codec as c
from custom_components.eufy_clean_local import mapdata, pb
from custom_components.eufy_clean_local.const import CONF_DEVICE_ID, CONF_LOCAL_KEY, CONF_MODEL, DOMAIN

from .conftest import legacy_snapshot, robot_dps, synthetic_snapshot


def entry_data(cloud: bool) -> dict:
    data = {
        CONF_DEVICE_ID: "bf0123456789abcdefxyzw",
        CONF_LOCAL_KEY: "0123456789abcdef",
        CONF_MODEL: "T2267",
        CONF_NAME: "L60",
        CONF_HOST: "192.168.1.50",
    }
    if cloud:
        data.update({CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"})
    return data


async def setup(hass: HomeAssistant, cloud: bool = False) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(cloud), unique_id="bf0123456789abcdefxyzw", title="L60")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def wait_for_sent(device, count: int) -> list[dict]:
    for _ in range(300):  # up to 6 s, so a busy machine doesn't fail the test
        if len(device.sent) >= count:
            return device.sent
        await asyncio.sleep(0.02)
    return device.sent


async def test_states_and_commands(hass: HomeAssistant, fake_tuya) -> None:
    entry = await setup(hass)
    device = fake_tuya.instances[0]
    assert device.address == "192.168.1.50" and device.version == 3.3

    state = hass.states.get("vacuum.l60")
    assert state.state == "docked"
    assert state.attributes["fan_speed"] == "Standard"
    assert state.attributes["status"] == "Fully charged"
    assert hass.states.get("sensor.l60_battery").state == "100"
    assert hass.states.get("sensor.l60_status").state == "Fully charged"
    assert hass.states.get("sensor.l60_warning").state == "Please clean the dustbin and filter for the best results."
    assert hass.states.get("sensor.l60_side_brush_remaining").state == "60"
    assert hass.states.get("sensor.l60_rolling_brush_remaining").state == "90"
    assert hass.states.get("sensor.l60_filter_remaining").state == "1"
    assert hass.states.get("sensor.l60_dust_bag_remaining") is None  # no self-empty station
    assert hass.states.get("sensor.l60_cleaning_time").state == "50"
    assert hass.states.get("sensor.l60_firmware").state == "2.0.4"
    assert hass.states.get("switch.l60_boostiq").state == "on"
    assert hass.states.get("switch.l60_resume_after_recharge").state == "off"
    assert hass.states.get("switch.l60_child_lock").state == "off"
    assert hass.states.get("switch.l60_do_not_disturb").state == "on"
    assert hass.states.get("time.l60_do_not_disturb_start").state == "22:00:00"
    assert hass.states.get("number.l60_volume").state == "60"
    assert hass.states.get("select.l60_cleaning_passes").state == "1"
    assert hass.states.get("image.l60_map") is None  # no cloud login, no map

    # The keep-alive goes out on connect; then each action adds one write.
    await wait_for_sent(device, 1)
    sent_before = len(device.sent)
    await hass.services.async_call("vacuum", "start", {"entity_id": "vacuum.l60"}, blocking=True)
    await hass.services.async_call("vacuum", "set_fan_speed", {"entity_id": "vacuum.l60", "fan_speed": "Max"},
                                   blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.l60_child_lock"}, blocking=True)
    await hass.services.async_call("time", "set_value",
                                   {"entity_id": "time.l60_do_not_disturb_end", "time": "07:30"}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.l60_reset_side_brush"}, blocking=True)
    sent = (await wait_for_sent(device, sent_before + 5))[sent_before:]
    start = c.pb_decode(sent[0]["152"], pb.ModeCtrlRequest)
    assert start.method == pb.ModeCtrlRequest.START_AUTO_CLEAN and start.auto_clean.clean_times == 1
    assert sent[1] == {"158": "Max"}
    assert sent[2] == {"176": "BAoCCAE="}
    dnd = c.pb_decode(sent[3]["157"], pb.UndisturbedRequest)
    assert dnd.undisturbed.sw.value and dnd.undisturbed.begin.hour == 22
    assert (dnd.undisturbed.end.hour, dnd.undisturbed.end.minute) == (7, 30)
    assert sent[4] == {"168": "AwoBAA=="}

    # A pushed status change shows up without polling.
    device.push({"153": "BgoAEAUyAA==", "163": 97})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if hass.states.get("vacuum.l60").state == "cleaning":
            break
    assert hass.states.get("vacuum.l60").state == "cleaning"
    assert hass.states.get("sensor.l60_status").state == "Cleaning (full clean)"
    assert hass.states.get("sensor.l60_battery").state == "97"

    # Pausing while cleaning, then start resumes rather than restarting.
    await hass.services.async_call("vacuum", "pause", {"entity_id": "vacuum.l60"}, blocking=True)
    device.push({"153": "CAoAEAUyAggB"})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if hass.states.get("vacuum.l60").state == "paused":
            break
    n = len(device.sent)
    await hass.services.async_call("vacuum", "start", {"entity_id": "vacuum.l60"}, blocking=True)
    sent = await wait_for_sent(device, n + 1)
    assert c.pb_decode(sent[-1]["152"], pb.ModeCtrlRequest).method == pb.ModeCtrlRequest.RESUME_TASK

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_map_rooms_and_segments(hass: HomeAssistant, fake_tuya) -> None:
    snapshot = synthetic_snapshot()
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot", return_value=mapdata.FetchResult(snapshot=snapshot)), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}):
        entry = await setup(hass, cloud=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if hass.states.get("button.l60_clean_bedroom_1"):
                break
    coordinator = entry.runtime_data
    assert coordinator.rooms == [{"id": 1, "name": "Kitchen"}, {"id": 2, "name": "Bedroom 1"}]
    assert coordinator.map_id == 7
    assert coordinator.map_png and coordinator.map_png[:8] == b"\x89PNG\r\n\x1a\n"
    assert hass.states.get("image.l60_map").state != "unavailable"
    assert hass.states.get("button.l60_clean_kitchen") is not None

    vacuum = hass.data[DATA_COMPONENT].get_entity("vacuum.l60")
    segments = await vacuum.async_get_segments()
    assert [(s.id, s.name) for s in segments] == [("1", "Kitchen"), ("2", "Bedroom 1")]

    device = fake_tuya.instances[0]
    n = len(device.sent)
    await vacuum.async_clean_segments(["2", "1"])
    await hass.services.async_call("button", "press", {"entity_id": "button.l60_clean_kitchen"}, blocking=True)
    await hass.services.async_call("vacuum", "send_command",
                                   {"entity_id": "vacuum.l60", "command": "room_clean",
                                    "params": {"rooms": ["kitchen", 2], "clean_times": 2}}, blocking=True)
    sent = (await wait_for_sent(device, n + 3))[n:]
    msgs = [c.pb_decode(s["152"], pb.ModeCtrlRequest) for s in sent if "152" in s]
    rooms = [[(r.id, r.order) for r in m.select_rooms_clean.rooms] for m in msgs]
    assert rooms == [[(2, 1), (1, 2)], [(1, 1)], [(1, 1), (2, 2)]]
    assert all(m.method == pb.ModeCtrlRequest.START_SELECT_ROOMS_CLEAN and m.select_rooms_clean.map_id == 7
               for m in msgs)
    assert msgs[2].select_rooms_clean.clean_times == 2

    from custom_components.eufy_clean_local.diagnostics import async_get_config_entry_diagnostics
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_LOCAL_KEY] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["map"]["map_id"] == 7
    assert diag["map"]["channels"][2]["decoded"]["info"]["width"] == 40
    assert diag["map"]["channels"][2]["decoded"]["id"] == 7
    assert diag["map"]["channels"][2]["decoded"]["pixels"].endswith("bytes>")
    assert bytes.fromhex(diag["map"]["channels"][4]["hex"]) == snapshot.raw[4]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cloud_seeds_dps_the_robot_omits(hass: HomeAssistant, fake_tuya) -> None:
    """Like the real L60: status() has only plain DPs, so protobuf ones come from the cloud."""
    fake_tuya.plain_status_only = True
    cloud_dps = {k: v for k, v in robot_dps().items() if isinstance(v, str)}
    cloud_dps["153"] = "BBADGgA="  # charging
    station = pb.StationResponse()
    station.status.connected = True
    station.auto_cfg_status.collectdust_v2.sw.value = True
    station.auto_cfg_status.collectdust_v2.mode.task = 2
    station.auto_cfg_status.cut_hair.sw.value = True
    cloud_dps["173"] = c.pb_encode(station)
    cons = pb.ConsumableResponse()
    cons.runtime.dustbag.duration = 720
    cloud_dps["168"] = c.pb_encode(cons)
    with patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value=cloud_dps), \
         patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot", return_value=mapdata.FetchResult()):
        entry = await setup(hass, cloud=True)
        for _ in range(200):
            await asyncio.sleep(0.02)
            if hass.states.get("sensor.l60_status").state == "Charging":
                break
    assert hass.states.get("vacuum.l60").state == "docked"
    assert hass.states.get("sensor.l60_status").state == "Charging"
    assert hass.states.get("sensor.l60_firmware").state == "2.0.4"
    assert hass.states.get("sensor.l60_ip_address").state == "192.168.1.50"  # from the connection

    # The station only shows up in the seeded DPs; its entities are added then.
    assert hass.states.get("sensor.l60_station").state == "Idle"
    assert hass.states.get("sensor.l60_dust_bag_remaining").state == "50"
    assert hass.states.get("switch.l60_auto_empty").state == "on"
    assert hass.states.get("switch.l60_auto_hair_cutting").state == "on"
    assert hass.states.get("select.l60_auto_empty_every_n_cleanings").state == "2"
    assert hass.states.get("button.l60_empty_dust") is not None
    device = fake_tuya.instances[0]
    n = len(device.sent)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.l60_auto_empty_every_n_cleanings", "option": "3"},
                                   blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": "button.l60_empty_dust"}, blocking=True)
    sent = (await wait_for_sent(device, n + 2))[n:]
    cfg = c.pb_decode(sent[0]["173"], pb.StationRequest).auto_cfg.collectdust_v2
    assert cfg.sw.value and cfg.mode.task == 3
    assert sent[1] == {"173": "BBICGAE="}

    from homeassistant.helpers import device_registry as dr
    dev = dr.async_get(hass).async_get_device_by_identifier(("eufy_clean_local", entry.unique_id), entry.entry_id)
    assert dev.sw_version == "2.0.4"
    assert hass.states.get("switch.l60_do_not_disturb").state == "on"
    assert fake_tuya.instances[0].update_requests == [153, 154, 157, 167, 168, 169, 173, 176, 177, 178]

    # A later local push wins over the seeded cloud value.
    fake_tuya.instances[0].push({"153": "BgoAEAUyAA=="})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if hass.states.get("vacuum.l60").state == "cleaning":
            break
    assert hass.states.get("vacuum.l60").state == "cleaning"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_manual_rooms_until_the_map_has_them(hass: HomeAssistant, fake_tuya) -> None:
    """With no map yet the manual room list gives the buttons; cleaning waits for the map's id."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(cloud=True), unique_id="bf0123456789abcdefxyzw",
                            title="L60", options={"rooms": "1=Kitchen\n3=Living room, bad, 4=", "map_interval": 120})
    entry.add_to_hass(hass)
    fetch = patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot", return_value=mapdata.FetchResult())
    with fetch as fetched, \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if fetched.called:
                break
    coordinator = entry.runtime_data
    assert coordinator.rooms == [{"id": 1, "name": "Kitchen"}, {"id": 3, "name": "Living room"}]
    assert coordinator.map_id is None
    assert hass.states.get("button.l60_clean_living_room") is not None
    with pytest.raises(HomeAssistantError, match="map has not been loaded"):
        await hass.services.async_call("button", "press", {"entity_id": "button.l60_clean_living_room"},
                                       blocking=True)

    # Once the map arrives its id is used, and the map's own rooms replace the manual list.
    snapshot = synthetic_snapshot()
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=snapshot)):
        await coordinator.async_refresh_map()
    assert coordinator.map_id == 7
    assert coordinator.rooms == [{"id": 1, "name": "Kitchen"}, {"id": 2, "name": "Bedroom 1"}]

    # A later fetch with only the robot's pose keeps the map; one from another map drops it.
    pose = mapdata.MapSnapshot(raw={7: snapshot.raw[7]})
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=pose)):
        await coordinator.async_refresh_map()
    assert set(coordinator.map.raw) == {2, 4, 5, 7} and coordinator.map_png is not None
    other = pb.RoomParams(map_id=9)
    other.rooms.add(id=1, name="Hall")
    body = other.SerializeToString()
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=mapdata.MapSnapshot(raw={5: bytes([len(body)]) + body}))):
        await coordinator.async_refresh_map()
    assert set(coordinator.map.raw) == {5, 7} and coordinator.map_id == 9
    assert coordinator.rooms == [{"id": 1, "name": "Hall"}]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


class FakeMediaCloud:
    """The Tuya media store as the L60 fills it: one whole channel blob per entry, mapId = channel."""

    def __init__(self, raw: dict[int, list[bytes]], latest_channel: int = 4) -> None:
        self.raw = raw
        self.latest_channel = latest_channel
        self.detail_calls = []

    def media_latest(self, dev_id, start=0, size=1000):
        rows = self.raw.get(self.latest_channel, [])[-1:]
        return {"dataList": [r.hex() for r in rows], "datatype": 0, "devId": dev_id, "hasNext": False,
                "mapId": self.latest_channel, "startRow": "0", "status": 1, "subRecordId": 1}

    def media_detail(self, dev_id, sub_record_id, channel, start="", size=1000):
        self.detail_calls.append((sub_record_id, channel, start))
        rows = self.raw.get(channel, [])
        page = int(start or 0)
        chunk = rows[page:page + 2]
        return {"dataList": [r.hex() for r in chunk], "hasNext": page + 2 < len(rows), "startRow": str(page + 2),
                "mapId": channel, "subRecordId": sub_record_id}


def test_fetch_reads_each_channel_from_the_media_store() -> None:
    """Each channel comes from media.detail; the newest whole entry wins and odd entries are ignored."""
    snap = synthetic_snapshot()
    old_params = pb.RoomParams(map_id=7, releases=2)
    old_params.rooms.add(id=9, name="Old room")
    old = old_params.SerializeToString()
    raw = {ch: [blob] for ch, blob in snap.raw.items()}
    raw[5] = [bytes([len(old)]) + old, snap.raw[5]]  # an older release first
    raw[6] = [bytes.fromhex("00")]  # no zones: an empty message
    # Bytes that look like a frame header with a huge offset must not be taken literally.
    raw[7].append(bytes([0x08, 0x00, 0x08, 0, 0, 0, 0, 0, 0, 0x20, 0xC4, 0x00, 0x00, 0xFF, 0xFF, 1, 2, 3]))
    cloud = FakeMediaCloud(raw)

    result = mapdata.fetch_snapshot(cloud, "dev")
    snapshot = result.snapshot
    assert snapshot.map_id == 7
    assert snapshot.rooms() == [{"id": 1, "name": "Kitchen"}, {"id": 2, "name": "Bedroom 1"}]
    assert snapshot.raw[7] == snap.raw[7]
    assert snapshot.render_png()[:4] == b"\x89PNG"
    assert {ch for _, ch, _ in cloud.detail_calls} == set(mapdata.CHANNELS) | {mapdata.CH_PATH}
    assert all(sub == 1 for sub, _, _ in cloud.detail_calls)
    assert result.channels[5] == {"entries": [len(raw[5][0]), len(raw[5][1])], "used": len(snap.raw[5])}
    assert result.latest["mapId"] == 4 and "dataList" not in result.latest


def test_fetch_with_an_empty_media_store() -> None:
    result = mapdata.fetch_snapshot(FakeMediaCloud({8: [b"\x00"]}, latest_channel=8), "dev")
    assert result.snapshot is None


def test_l60_grid_and_outline_decode() -> None:
    """The L60's zlib-in-LZ4 grid and traced room outlines decode onto the same cells."""
    snap = synthetic_snapshot()
    m = snap.get(mapdata.CH_MAP)
    grid = mapdata.map_grid(m)
    assert (grid.width, grid.height, grid.resolution) == (50, 40, 5)
    assert (grid.origin_x, grid.origin_y) == (-125, -100)
    assert grid.cells[10 * 50 + 10] == mapdata.FLOOR_CELL
    assert grid.cells[10 * 50 + 25] == mapdata.WALL
    assert grid.cells[0] == mapdata.UNKNOWN
    rooms = mapdata.room_cells(snap.get(mapdata.CH_ROOM_OUTLINE), grid, legacy_ok=False, room_ids={1, 2})
    assert rooms[10 * 50 + 10] == 1 and rooms[30 * 50 + 40] == 2 and rooms[0] == 0
    colours = mapdata.room_colours(rooms, grid.width, grid.height)
    assert colours[1] != colours[2]
    assert snap.map_id == 7

    png = snap.render_png()
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    found = {img.getpixel((x, y))[:3] for x in range(0, img.width, 3) for y in range(0, img.height, 3)}
    assert colours[1] in found and colours[2] in found
    def near(colour):  # the robot icon is anti-aliased, so match within a small tolerance
        return any(all(abs(a - b) <= 12 for a, b in zip(colour, f)) for f in found)
    assert near(mapdata.DOCK) and near(mapdata.ROBOT)


def test_legacy_pixels_still_render() -> None:
    snap = legacy_snapshot()
    assert snap.map_id == 7
    grid = mapdata.map_grid(snap.get(mapdata.CH_MAP))
    assert (grid.width, grid.height) == (40, 30)
    png = snap.render_png()
    assert png[:4] == b"\x89PNG"


async def test_live_map_follows_the_robot_while_cleaning(hass: HomeAssistant, fake_tuya) -> None:
    """While cleaning, the live map camera redraws the robot every few seconds, then stops when it docks."""
    from homeassistant.components.camera import async_get_image

    snapshot = synthetic_snapshot()
    poses = iter([(0.0, 0.0), (40.0, 10.0), (80.0, 20.0)] + [(80.0, 20.0)] * 50)
    reads = []

    def fake_pose(cloud, dev_id):
        reads.append(dev_id)
        return next(poses)

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(cloud=True), unique_id="bf0123456789abcdefxyzw",
                            title="L60", options={"live_interval": 2, "map_interval": 120})
    entry.add_to_hass(hass)
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=snapshot)), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}), \
         patch("custom_components.eufy_clean_local.mapdata.read_pose", side_effect=fake_pose), \
         patch("custom_components.eufy_clean_local.mapdata.read_path", return_value=[]), \
         patch("custom_components.eufy_clean_local.coordinator.asyncio.sleep", new=_fast_sleep):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        for _ in range(100):
            await asyncio.sleep(0.02)
            if coordinator.live_png:
                break
        docked_frame = coordinator.live_png
        assert docked_frame and not reads  # docked: nothing is read

        device = fake_tuya.instances[0]
        device.push({"153": "BgoAEAUyAA=="})  # cleaning
        frames = set()
        for _ in range(200):
            await asyncio.sleep(0.02)
            frames.add(coordinator.live_png)
            if coordinator._live_pose == (80.0, 20.0):
                break
        assert coordinator._live_pose == (80.0, 20.0)
        assert len(frames) >= 2  # the robot moved on the map
        image = await async_get_image(hass, "camera.l60_live_map")
        assert image.content_type == "image/png" and image.content == coordinator.live_png

        device.push({"153": "BBADGgA="})  # docked again
        for _ in range(100):
            await asyncio.sleep(0.02)
            if coordinator._live_task is None:
                break
        assert coordinator._live_task is None
        n = len(reads)
        await asyncio.sleep(0.2)
        assert len(reads) == n  # stopped reading once docked

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_live_map_stream_keeps_sending_while_docked(hass: HomeAssistant, fake_tuya, hass_client) -> None:
    """The live stream resends an unchanged frame, so Safari and the iOS app don't time it out."""
    from homeassistant.components.camera import DATA_COMPONENT as CAMERA

    from custom_components.eufy_clean_local import camera

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(cloud=True), unique_id="bf0123456789abcdefxyzw",
                            title="L60", options={"live_interval": 2, "map_interval": 120})
    entry.add_to_hass(hass)
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=synthetic_snapshot())), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}), \
         patch.object(camera, "KEEPALIVE", 0.2):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        for _ in range(100):
            if coordinator.live_png or coordinator.map_png:
                break
            await asyncio.sleep(0.02)
        hass.data[CAMERA].get_entity("camera.l60_live_map")._attr_frame_interval = 0.05

        client = await hass_client()
        response = await client.get("/api/camera_proxy_stream/camera.l60_live_map")
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        data = b""
        end = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < end:
            try:
                data += await asyncio.wait_for(response.content.readany(), 0.3)
            except TimeoutError:
                pass
        response.close()
        # The docked robot's picture never changes: two copies up front, then a resend every KEEPALIVE.
        assert data.count(b"--frameboundary\r\nContent-Type: image/png\r\n") >= 5

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_live_map_stream_ends_when_its_token_is_replaced(hass: HomeAssistant, fake_tuya,
                                                               hass_client_no_auth) -> None:
    """A stream opened with a token ends once the token rotates, so Safari can't pile up leaked streams."""
    from homeassistant.components.camera import DATA_COMPONENT as CAMERA

    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(cloud=True), unique_id="bf0123456789abcdefxyzw",
                            title="L60", options={"live_interval": 2, "map_interval": 120})
    entry.add_to_hass(hass)
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=synthetic_snapshot())), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        for _ in range(100):
            if coordinator.live_png or coordinator.map_png:
                break
            await asyncio.sleep(0.02)
        camera = hass.data[CAMERA].get_entity("camera.l60_live_map")
        camera._attr_frame_interval = 0.05

        client = await hass_client_no_auth()
        token = camera.access_tokens[-1]
        response = await client.get(f"/api/camera_proxy_stream/camera.l60_live_map?token={token}")
        assert response.status == 200
        assert b"--frameboundary" in await asyncio.wait_for(response.content.readany(), 1)
        await asyncio.sleep(0.2)
        assert not response.content.at_eof()  # still streaming while its token is current

        camera.async_update_token()  # what Home Assistant does every 5 minutes
        await asyncio.wait_for(response.content.read(), 1)  # the server ends the stream
        assert response.content.at_eof()
        response.close()

        # The dashboard rebuilds the card (new view, resized window) and opens another stream
        # with the same token; Safari keeps the old one open, so the server closes it.
        token = camera.access_tokens[-1]
        url = f"/api/camera_proxy_stream/camera.l60_live_map?token={token}"
        first = await client.get(url)
        assert b"--frameboundary" in await asyncio.wait_for(first.content.readany(), 1)
        second = await client.get(url)
        assert b"--frameboundary" in await asyncio.wait_for(second.content.readany(), 1)
        await asyncio.wait_for(first.content.read(), 1)
        assert first.content.at_eof()  # the replaced stream ended
        await asyncio.sleep(0.2)
        assert not second.content.at_eof()  # the newest one keeps going
        other = await client.get(url, headers={"User-Agent": "another browser"})
        assert b"--frameboundary" in await asyncio.wait_for(other.content.readany(), 1)
        await asyncio.sleep(0.2)
        assert not second.content.at_eof()  # a different viewer doesn't end it
        for response in (first, second, other):
            response.close()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


_real_sleep = asyncio.sleep


async def _fast_sleep(delay, *args, **kwargs):
    await _real_sleep(min(delay, 0.01), *args, **kwargs)


def test_path_records_decode() -> None:
    """The cleaned path is 5-byte records: sign-magnitude int16 x, y (cm, big-endian) and a flag byte."""
    def record(x: int, y: int, flag: int = 0) -> bytes:
        enc = lambda v: (0x8000 | -v) if v < 0 else v  # noqa: E731
        return enc(x).to_bytes(2, "big") + enc(y).to_bytes(2, "big") + bytes([flag])

    raw = record(-35, -14, 0x1C) + record(-26, -12) + record(0, 61) + record(212, 727)
    assert mapdata.decode_path(raw) == [(-35.0, -14.0), (-26.0, -12.0), (0.0, 61.0), (212.0, 727.0)]
    assert mapdata.decode_path(raw + b"\x80") == mapdata.decode_path(raw)  # a partial record is ignored
    assert mapdata.decode_path(b"") == [] and mapdata.decode_path(None) == []

    snap = synthetic_snapshot()
    snap.raw[mapdata.CH_PATH] = b"".join(record(x, -40) for x in range(-80, 80, 10))  # clear of the labels
    plain = synthetic_snapshot().render_png()
    assert snap.path() and snap.render_png() != plain  # the trail is drawn

    import io

    from PIL import Image

    with_path = Image.open(io.BytesIO(snap.render_png())).convert("RGBA")
    without = Image.open(io.BytesIO(plain)).convert("RGBA")
    changed = [(x, y) for x in range(with_path.width) for y in range(with_path.height)
               if with_path.getpixel((x, y)) != without.getpixel((x, y))]
    # Blended over the map, so the dashboard's background never shows through the line.
    assert all(with_path.getpixel(p)[3] >= without.getpixel(p)[3] for p in changed)
    mid = sorted(changed)[len(changed) // 2][0]
    assert sum(1 for x, _ in changed if x == mid) >= mapdata.PATH_WIDTH  # drawn PATH_WIDTH px thick


def test_robot_icon_turns_with_the_heading() -> None:
    """The robot is drawn as a vacuum icon facing its heading (radians x 100, counter-clockwise from east)."""
    import math

    east = mapdata.robot_icon(0.0)
    north = mapdata.robot_icon(math.pi / 2)
    assert east.size == north.size == (mapdata.ROBOT_ICON_PX, mapdata.ROBOT_ICON_PX)
    # The blue front bumper is on the right when facing east, and on top when facing north.
    w = mapdata.ROBOT_ICON_PX
    right, top = east.getpixel((w - 3, w // 2)), north.getpixel((w // 2, 2))
    assert right[2] > right[0] + 60 and top[2] > top[0] + 60

    renderer = mapdata.MapRenderer.build(synthetic_snapshot())
    assert renderer.frame((20.0, 10.0, 0.0)) != renderer.frame((20.0, 10.0, 157.0))  # turning redraws
    assert renderer.frame((20.0, 10.0)) == renderer.frame((20.0, 10.0, 0.0))  # no heading means east


# ---------------------------------------------------------------- v0.2.0 features

async def _wait(predicate, tries: int = 150) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_alert_events_and_problem_sensor(hass: HomeAssistant, fake_tuya) -> None:
    """New errors, warnings and robot messages fire the alert event; the problem sensor follows them."""
    entry = await setup(hass)
    device = fake_tuya.instances[0]
    assert hass.states.get("binary_sensor.l60_problem").state == "on"  # the start-up warning 2311
    events = []
    hass.bus.async_listen("state_changed", lambda e: e.data["entity_id"] == "event.l60_alert" and events.append(
        e.data["new_state"]))

    # The warning that was already there at start-up doesn't fire; a new one does.
    device.push({"177": c.pb_encode(pb.ErrorCode(warn=[2311, 7000]))})
    assert await _wait(lambda: events)
    state = events[-1]
    assert state.attributes["event_type"] == "warning" and state.attributes["code"] == 7000
    assert "trapped" in state.attributes["message"].lower()

    device.push({"177": c.pb_encode(pb.ErrorCode(error=[2010], warn=[7000]))})
    assert await _wait(lambda: events[-1].attributes["event_type"] == "error")
    assert events[-1].attributes["code"] == 2010

    device.push({"178": c.pb_encode(pb.PromptCode(last_time=5, value=[40]))})
    assert await _wait(lambda: events[-1].attributes["event_type"] == "message")
    assert events[-1].attributes["message"] == "Task finished, heading home"

    device.push({"177": c.pb_encode(pb.ErrorCode())})
    assert await _wait(lambda: hass.states.get("binary_sensor.l60_problem").state == "off")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_redact_secrets(hass: HomeAssistant, fake_tuya) -> None:
    from custom_components.eufy_clean_local.diagnostics import async_get_config_entry_diagnostics

    entry = await setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "0123456789abcdef" not in text  # the local key
    assert diag["state"]["state"] == "docked"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reauth_when_eufy_rejects_the_login(hass: HomeAssistant, fake_tuya) -> None:
    from custom_components.eufy_clean_local.eufy_cloud import EufyAuthError

    with patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", side_effect=EufyAuthError("bad")):
        entry = await setup(hass, cloud=True)
        await hass.async_block_till_done()
    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    assert len(flows) == 1 and flows[0]["step_id"] == "reauth_confirm"

    new_dev = {"id": entry.unique_id, "model": "T2267", "name": "L60", "mac": None, "local_key": "fedcba9876543210"}
    with patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.devices", return_value=[new_dev]), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}), \
         patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot", return_value=mapdata.FetchResult()):
        result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {CONF_PASSWORD: "new-secret"})
        await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret" and entry.data[CONF_LOCAL_KEY] == "fedcba9876543210"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_local_key_is_looked_up_again_when_rejected(hass: HomeAssistant, fake_tuya) -> None:
    """Three 'wrong key' replies make the integration fetch the key again and reload with it."""
    fake_tuya.status_error = {"Error": "Check device key or version", "Err": "914"}
    new_dev = {"id": "bf0123456789abcdefxyzw", "model": "T2267", "name": "L60", "mac": None,
               "local_key": "fedcba9876543210"}
    with patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.devices", return_value=[new_dev]), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}), \
         patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot", return_value=mapdata.FetchResult()), \
         patch("custom_components.eufy_clean_local.coordinator.tinytuya.find_device",
               return_value={"ip": "192.168.1.50"}), \
         patch("custom_components.eufy_clean_local.coordinator.threading.Event.wait", return_value=False):
        entry = await setup(hass, cloud=True)
        assert await _wait(lambda: entry.data[CONF_LOCAL_KEY] == "fedcba9876543210", tries=300)
        fake_tuya.status_error = None
        await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_per_room_settings(hass: HomeAssistant, fake_tuya) -> None:
    snapshot = synthetic_snapshot()
    with patch("custom_components.eufy_clean_local.mapdata.fetch_snapshot",
               return_value=mapdata.FetchResult(snapshot=snapshot)), \
         patch("custom_components.eufy_clean_local.eufy_cloud.EufyCloud.device_dps", return_value={}):
        entry = await setup(hass, cloud=True)
        assert await _wait(lambda: hass.states.get("select.l60_kitchen_suction") is not None)
    assert hass.states.get("select.l60_kitchen_suction").state == "Turbo"
    assert hass.states.get("select.l60_bedroom_1_passes").state == "1"
    assert hass.states.get("switch.l60_per_room_settings").state == "off"

    device = fake_tuya.instances[0]
    n = len(device.sent)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.l60_bedroom_1_suction", "option": "Max"}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.l60_per_room_settings"}, blocking=True)
    sent = (await wait_for_sent(device, n + 2))[n:]
    first, second = (c.pb_decode(s["170"], pb.MapEditRequest) for s in sent)
    assert first.method == pb.MapEditRequest.SET_ROOMS_CUSTOM and first.map_id == 7
    suctions = {r.id: r.custom.fan.suction for r in first.rooms_custom.rooms_parm.rooms}
    assert suctions == {1: 2, 2: 3}  # Kitchen stays Turbo, Bedroom 1 becomes Max
    assert not first.rooms_custom.custom_enable.value
    assert second.rooms_custom.custom_enable.value
    assert [(r.id, r.order) for r in second.rooms_custom.rooms_order.rooms] == [(1, 1), (2, 2)]
    assert hass.states.get("switch.l60_per_room_settings").state == "on"
    assert hass.states.get("select.l60_bedroom_1_suction").state == "Max"
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_rooms_being_cleaned_are_highlighted() -> None:
    renderer = mapdata.MapRenderer.build(synthetic_snapshot())
    plain = renderer.frame((20.0, 10.0, 0.0))
    kitchen = renderer.frame((20.0, 10.0, 0.0), rooms=(1,))
    both = renderer.frame((20.0, 10.0, 0.0), rooms=(1, 2))
    assert len({plain, kitchen, both}) == 3
    assert renderer.frame((20.0, 10.0, 0.0), rooms=(1,)) == kitchen  # cached overlay, same picture

    sel = pb.TemporaryData()
    sel.select_rooms_clean.rooms.add(id=2, order=1)
    body = sel.SerializeToString()

    class Cloud:
        def media_detail(self, dev_id, sub, channel, start, size):
            assert channel == mapdata.CH_TEMP
            return {"dataList": [(bytes([len(body)]) + body).hex()], "hasNext": False}

    assert mapdata.read_selected_rooms(Cloud(), "dev") == (2,)


async def test_robovac_30c(hass: HomeAssistant, fake_tuya) -> None:
    """An older RoboVac (plain Tuya DPs): vacuum, battery, status, error and alerts, no map."""
    fake_tuya.status_dps = {"1": True, "2": False, "5": "Auto", "15": "Charging", "102": "Standard",
                            "104": 88, "106": 0}
    data = entry_data(cloud=False)
    data.update({CONF_MODEL: "T2118", CONF_NAME: "RoboVac", CONF_DEVICE_ID: "11778811b4e6aaaaaaaa"})
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=data[CONF_DEVICE_ID], title="RoboVac")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    vac = hass.states.get("vacuum.robovac")
    assert vac.state == "docked" and vac.attributes["fan_speed"] == "Standard"
    assert vac.attributes["fan_speed_list"] == ["No suction", "Standard", "Boost IQ", "Max"]
    assert hass.states.get("sensor.robovac_battery").state == "88"
    assert hass.states.get("sensor.robovac_status").state == "Charging"
    assert hass.states.get("sensor.robovac_error").state == "No error"
    for absent in ("camera.robovac_live_map", "image.robovac_map", "switch.robovac_boostiq",
                   "sensor.robovac_side_brush_remaining", "select.robovac_cleaning_passes"):
        assert hass.states.get(absent) is None, absent

    device = fake_tuya.instances[0]
    n = len(device.sent)
    for service, data_ in (("start", {}), ("set_fan_speed", {"fan_speed": "Max"}), ("return_to_base", {}),
                           ("send_command", {"command": "edge_clean"})):
        await hass.services.async_call("vacuum", service, {"entity_id": "vacuum.robovac", **data_}, blocking=True)
    sent = (await wait_for_sent(device, n + 4))[n:]
    assert sent == [{"5": "Auto"}, {"102": "Max"}, {"101": True}, {"5": "Edge"}]

    device.push({"15": "Running", "2": True})
    assert await _wait(lambda: hass.states.get("vacuum.robovac").state == "cleaning")
    device.push({"2": False})
    assert await _wait(lambda: hass.states.get("vacuum.robovac").state == "paused")
    events = []
    hass.bus.async_listen("state_changed", lambda e: e.data["entity_id"] == "event.robovac_alert" and events.append(
        e.data["new_state"]))
    device.push({"106": 2})
    assert await _wait(lambda: hass.states.get("vacuum.robovac").state == "error")
    assert await _wait(lambda: events)
    assert events[-1].attributes["message"] == "Wheel stuck"
    assert hass.states.get("binary_sensor.robovac_problem").state == "on"
    assert await hass.config_entries.async_unload(entry.entry_id)
