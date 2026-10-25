"""Connection to one L60: a local Tuya socket for control and status, the cloud for the map."""

from __future__ import annotations

import asyncio
import base64
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import tinytuya

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_HOST, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import codec as c
from . import pb
from . import legacy, mapdata
from .codes import ERRORS, PROMPTS, WARNINGS
from .const import (
    CONF_DEVICE_ID,
    CONF_LIVE_INTERVAL,
    CONF_LOCAL_KEY,
    CONF_MAC,
    CONF_MAP_INTERVAL,
    CONF_MODEL,
    CONF_ROOMS,
    DEFAULT_LIVE_INTERVAL,
    DEFAULT_MAP_INTERVAL,
    DOMAIN,
    LIVE_STATES,
)
from .eufy_cloud import SESSIONS, EufyAuthError, EufyCloud

_LOGGER = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 10
POLL_SECONDS = 60
SOCKET_TIMEOUT = 1
STORE_VERSION = 1
COMMAND_MAX_AGE = 30  # seconds; older queued commands are dropped after a reconnect
# Protobuf DPs the robot only pushes on change; ask for them again after connecting.
RAW_DPS = [153, 154, 157, 167, 168, 169, 173, 176, 177, 178]

ACTIVE_STATES = {"cleaning", "returning", "paused"}


def parse_rooms(text: str) -> list[dict]:
    """Parse the manual room list option: "1=Kitchen, 2=Living room" (commas or new lines)."""
    rooms = []
    for part in re.split(r"[,\n]", text or ""):
        if "=" not in part:
            continue
        room_id, name = part.split("=", 1)
        if room_id.strip().isdigit() and name.strip():
            rooms.append({"id": int(room_id), "name": name.strip()})
    return rooms


def onoff(value) -> bool | None:
    return None if value is None else bool(value)


class RobotState:
    """The robot's DPs and everything derived from them. Used only on the worker thread."""

    def __init__(self) -> None:
        self.dps: dict[str, Any] = {}
        self.decoded: dict[str, Any] = {}

    def update(self, dps: dict) -> bool:
        changed = False
        for dp, value in dps.items():
            dp = str(dp)
            if self.dps.get(dp) == value:
                continue
            self.dps[dp] = value
            try:
                self.decoded[dp] = c.decode_dp(dp, value)
            except Exception as err:
                _LOGGER.warning("Could not decode DP %s=%r: %s", dp, value, err)
                continue
            _LOGGER.debug("DP %s -> %s", dp, str(self.decoded[dp]).replace("\n", " ")[:300])
            changed = True
        return changed

    def snapshot(self) -> dict[str, Any]:
        d = self.decoded
        ws = d.get(c.DP_WORK_STATUS)
        out: dict[str, Any] = {
            "state": c.vacuum_state(ws),
            "status": c.status_text(ws),
            "battery": d.get(c.DP_BATTERY),
            "fan_speed": d.get(c.DP_SUCTION),
            "boost_iq": onoff(d.get(c.DP_BOOST_IQ)),
            "auto_return": onoff(d.get(c.DP_AUTO_RETURN)),
            "volume": d.get(c.DP_VOLUME),
            "task": c.MODE_NAMES.get(ws.mode.value) if ws is not None else None,
        }
        err = d.get(c.DP_ERRORS)
        if err is not None:
            errors, warnings = c.error_texts(err)
            out["error"] = "; ".join(errors) or "No error"
            out["warning"] = "; ".join(warnings) or "No warning"
            out["error_codes"] = list(err.error)
            out["warning_codes"] = list(err.warn)
        prompt = d.get(c.DP_PROMPT)
        if prompt is not None:
            out["prompt_codes"] = list(prompt.value)
            out["prompt_time"] = prompt.last_time
            out["prompt"] = c.prompt_text(prompt) or "None"
        cons = d.get(c.DP_CONSUMABLES)
        if cons is not None:
            for key, meta in c.CONSUMABLES.items():
                used = getattr(cons.runtime, key).duration
                out[f"{key}_hours"] = used
                out[f"{key}_left"] = c.consumable_percent(used, meta["life"])
        stats = d.get(c.DP_STATISTICS)
        if stats is not None:
            out["clean_area"] = stats.single.clean_area
            out["clean_minutes"] = round(stats.single.clean_duration / 60)
            out["total_area"] = stats.total.clean_area
            out["total_hours"] = round(stats.total.clean_duration / 3600, 1)
            out["total_count"] = stats.total.clean_count
        param = d.get(c.DP_CLEAN_PARAM)
        if param is not None:
            cp = param.clean_param
            out["clean_times"] = str(cp.clean_times or 1)
            out["water_level"] = c.WATER_LEVELS[cp.mop_mode.level] if cp.mop_mode.level < 3 else None
            out["clean_type"] = c.CLEAN_TYPES[cp.clean_type.value] if cp.clean_type.value < 4 else None
        dnd = d.get(c.DP_DND)
        if dnd is not None:
            u = dnd.undisturbed
            out["dnd"] = bool(u.sw.value)
            out["dnd_start"] = (u.begin.hour, u.begin.minute)
            out["dnd_end"] = (u.end.hour, u.end.minute)
        uni = d.get(c.DP_UNISETTING)
        if uni is not None:
            out["child_lock"] = bool(uni.children_lock.value)
            out["wifi_signal"] = uni.ap_signal_strength or None
        info = d.get(c.DP_DEVICE_INFO)
        if info is not None:
            out["firmware"] = info.software or None
            out["ip"] = info.wifi_ip or None
            out["wifi_name"] = info.wifi_name or None
            out["dust_option"] = bool(info.proto_info.collect_dust.options & 1)
        station = d.get(c.DP_STATION)
        if station is not None:
            out["station_connected"] = bool(station.status.connected)
            out["station_status"] = c.station_status(station)
            cfg = station.auto_cfg_status
            out["auto_empty"] = bool(cfg.collectdust_v2.sw.value)
            out["auto_empty_every"] = str(cfg.collectdust_v2.mode.task or 1)
            out["auto_cut_hair"] = bool(cfg.cut_hair.sw.value)
        return out


def _render_floor(raw: dict[int, bytes], rooms: list[dict]) -> tuple[bytes | None, list[dict]]:
    """A stored floor's picture and room outlines (blocking)."""
    try:
        snapshot = mapdata.MapSnapshot(raw=dict(raw))
        renderer = mapdata.MapRenderer.build(snapshot)
    except Exception as err:  # noqa: BLE001 - one bad floor shouldn't stop the others
        _LOGGER.warning("Could not draw a saved floor: %s", err)
        return None, []
    if renderer is None:
        return None, []
    names = {r["id"]: r["name"] for r in rooms}
    shapes = [{"id": room_id, "name": names.get(room_id, f"Room {room_id}"), "points": points}
              for room_id, points in sorted(renderer.shapes.items())]
    return renderer.frame(snapshot.pose(), snapshot.path()), shapes


class LegacyRobotState(RobotState):
    """Older RoboVacs: plain DP values, no protobuf."""

    def update(self, dps: dict) -> bool:
        changed = False
        for dp, value in dps.items():
            if self.dps.get(str(dp)) != value:
                self.dps[str(dp)] = value
                changed = True
        return changed

    def snapshot(self) -> dict[str, Any]:
        return legacy.snapshot(self.dps)


class EufyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the worker thread, the robot's state and the cached map."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=entry.title, config_entry=entry, update_interval=None)
        data = entry.data
        self.device_id: str = data[CONF_DEVICE_ID]
        self.local_key: str = data[CONF_LOCAL_KEY]
        self.model: str = data.get(CONF_MODEL, "T2267")
        self.mac: str | None = data.get(CONF_MAC)
        self.legacy = self.model in legacy.MODELS
        self.robot_name: str = data.get(CONF_NAME) or c.L60_MODELS.get(self.model) or legacy.MODELS.get(self.model, "eufy")
        self.host: str = entry.options.get(CONF_HOST) or data.get(CONF_HOST) or ""
        self.online = False
        self.cloud: EufyCloud | None = None
        if data.get(CONF_PASSWORD):
            # Reuse the session the config flow just opened, if any (eufy rate-limits logins).
            self.cloud = SESSIONS.pop(data[CONF_EMAIL], None) or EufyCloud(data[CONF_EMAIL], data[CONF_PASSWORD])

        self.map = mapdata.MapSnapshot()
        self.map_png: bytes | None = None
        self.map_updated: datetime | None = None
        self._map_rooms: list[dict] = []
        self.manual_rooms = parse_rooms(entry.options.get(CONF_ROOMS, ""))
        self.map_id: int | None = None
        self.last_fetch: mapdata.FetchResult | None = None
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.{self.device_id}")
        self._map_lock = threading.Lock()
        self._last_map_fetch = 0.0
        self._prev_state: str | None = None
        self._map_retry_unsub = None
        # Live view: the floor plan drawn once, then the robot (and path) redrawn every few seconds.
        self.live_png: bytes | None = None
        self.live_signal = f"{DOMAIN}_{self.device_id}_live"
        self._renderer: mapdata.MapRenderer | None = None
        self._live_task: asyncio.Task | None = None
        self._live_pose: tuple[float, ...] | None = None  # x, y (cm), heading (radians x 100)
        self._live_path: list = []
        self._live_rooms: tuple[int, ...] = ()  # rooms of the current room clean, highlighted on the live map
        self._sent_rooms: tuple[int, ...] = ()
        self.alert_signal = f"{DOMAIN}_{self.device_id}_alert"
        # Every floor the robot has cleaned on (multi-floor maps): map id -> name, map channels,
        # rooms and when it was last seen. The robot doesn't list its saved floors, so a floor
        # is learned the first time its map is uploaded, and kept from then on.
        self.floors: dict[int, dict] = {}
        self.primary_map_id: int | None = None  # the first floor; its rooms keep their original ids
        # The floor the dock is on. The robot only shows its floor by uploading a map while it
        # cleans, so when it's set back on the dock, this is how we know it's downstairs again.
        self.station_floor: int | None = None
        self._docked_after_clean = False
        self.floor_pngs: dict[int, bytes] = {}
        self.floor_shapes: dict[int, list[dict]] = {}
        self.floor_signal = f"{DOMAIN}_{self.device_id}_floors"
        self._alert_seen: dict | None = None
        self._key_failures = 0

        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.data = {}

    @property
    def rooms(self) -> list[dict]:
        """Rooms from the robot's map, or the manual list until the map has them."""
        return self._map_rooms or self.manual_rooms

    @property
    def room_shapes(self) -> list[dict]:
        """The rooms' outlines on the map picture (fractions of its size), for tapping rooms in the card."""
        if self._renderer is None:
            return []
        names = {r["id"]: r["name"] for r in self.rooms}
        return [{"id": room_id, "name": names.get(room_id, f"Room {room_id}"), "points": points}
                for room_id, points in sorted(self._renderer.shapes.items())]

    @property
    def floor_name(self) -> str | None:
        """The name of the floor the robot is on (as set in the eufy app)."""
        floor = self.floors.get(self.map_id)
        return floor["name"] if floor else None

    @property
    def floor_list(self) -> list[dict]:
        return [{"id": map_id, "name": f["name"], "active": map_id == self.map_id}
                for map_id, f in sorted(self.floors.items())]

    def floor_key(self, map_id: int | None) -> int | None:
        """How room entities tell floors apart: None for the first floor (whose rooms keep the ids
        they had before floors existed), else the map id."""
        return None if map_id is None or map_id == self.primary_map_id else map_id

    def floor_map_id(self, key: int | None) -> int | None:
        return self.primary_map_id if key is None else key

    def on_floor(self, key: int | None) -> bool:
        """Whether the robot is on that floor (or floors aren't known yet)."""
        map_id = self.floor_map_id(key)
        return map_id is None or self.map_id is None or map_id == self.map_id

    def room_label(self, key: int | None, name: str) -> str:
        """A room's name, with its floor when it isn't on the first floor."""
        floor = self.floors.get(key) if key is not None else None
        return f"{name} ({floor['name']})" if floor else name

    def floor_rooms(self, map_id: int | None) -> list[dict]:
        """A floor's rooms; the robot's current floor uses the live list."""
        if map_id is None or map_id == self.map_id:
            return self.rooms
        floor = self.floors.get(map_id)
        return floor["rooms"] if floor else []

    def floor_room_params(self, map_id: int | None):
        """A floor's RoomParams (per-room settings): the live ones for the current floor."""
        if map_id is None or map_id == self.map_id:
            return self.room_params()
        floor = self.floors.get(map_id)
        return mapdata.MapSnapshot(raw=floor["raw"]).get(mapdata.CH_ROOM_PARAMS) if floor else None

    @property
    def hybrid(self) -> bool:
        return self.model in c.HYBRID_MODELS

    @property
    def has_station(self) -> bool:
        d = self.data or {}
        return bool(d.get("dust_option") or d.get("station_connected"))

    # ------------------------------------------------------------ lifecycle
    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        for key, floor in (stored.get("floors") or {}).items():
            self.floors[int(key)] = {
                "name": floor["name"],
                "raw": {int(k): base64.b64decode(v) for k, v in floor["channels"].items()},
                "rooms": floor.get("rooms") or [],
                "updated": dt_util.parse_datetime(floor["updated"]) if floor.get("updated") else None,
            }
        self.primary_map_id = stored.get("primary")
        self.station_floor = stored.get("station")
        if stored.get("channels"):
            self.map.raw = {int(k): base64.b64decode(v) for k, v in stored["channels"].items()}
            self._map_rooms = stored.get("rooms") or self.map.rooms()
            self.map_id = self.map.map_id
            if stored.get("updated"):
                self.map_updated = dt_util.parse_datetime(stored["updated"])
            self.map_png = await self.hass.async_add_executor_job(self._safe_render)
            await self._async_rebuild_live()
            await self._async_remember_floor(save=False)
        for map_id in [m for m in self.floors if m not in self.floor_pngs]:
            floor = self.floors[map_id]
            self.floor_pngs[map_id], self.floor_shapes[map_id] = await self.hass.async_add_executor_job(
                _render_floor, floor["raw"], floor["rooms"])
        self._thread = threading.Thread(target=self._worker, name=f"eufy_clean_local_{self.device_id}", daemon=True)
        self._thread.start()
        if self.cloud is not None and not self.legacy:
            self.config_entry.async_create_background_task(self.hass, self._async_cloud_start(), "eufy cloud start")

    async def _async_cloud_start(self) -> None:
        """Log in (with backoff), seed the DPs the robot won't report locally, then load the map."""
        delay = 60
        while True:
            try:
                dps = await self.hass.async_add_executor_job(self.cloud.device_dps, self.device_id)
                break
            except EufyAuthError as err:
                _LOGGER.error("eufy rejected the stored login (%s); asking for it again", err)
                self.config_entry.async_start_reauth(self.hass)
                return
            except Exception as err:
                _LOGGER.warning("eufy cloud not reachable yet (%s); retrying in %ss", err, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1800)
        raw = {k: v for k, v in dps.items() if isinstance(v, str) and str(k) in {str(d) for d in RAW_DPS}}
        _LOGGER.debug("Cloud has %d DPs; seeding %s", len(dps), sorted(raw))
        if raw:
            self._commands.put(("seed", time.monotonic(), raw))
        await self.async_refresh_map()

    async def async_stop(self) -> None:
        self._stop.set()
        if self._live_task is not None:
            self._live_task.cancel()
        if self._map_retry_unsub:
            self._map_retry_unsub()
        if self._thread is not None:
            await self.hass.async_add_executor_job(self._thread.join, 5)

    # ------------------------------------------------------------ commands
    async def async_send(self, dps: dict | None) -> None:
        if not dps:
            return
        _LOGGER.debug("Sending %s", dps)
        self._commands.put(("send", time.monotonic(), dps))

    async def async_command(self, key: str, value=None) -> None:
        state = (self.data or {}).get("state")
        dps = None
        if self.legacy:
            dps = legacy.command(key, state, value)
            if dps is None:
                raise ValueError(f"{self.robot_name} doesn't support {key}")
        elif key == "start":
            dps = c.cmd_resume() if state == "paused" else c.cmd_start_auto()
        elif key == "pause":
            dps = c.cmd_pause()
        elif key == "stop":
            dps = c.cmd_stop_go_home() if state == "returning" else c.cmd_stop()
        elif key == "return_to_base":
            dps = c.cmd_go_home()
        elif key == "clean_spot":
            dps = c.cmd_spot()
        elif key == "locate":
            dps = {c.DP_FIND_ROBOT: True}
        elif key == "fan_speed":
            dps = c.cmd_suction(value)
        elif key == "rooms":
            if self.map_id is None:
                raise ValueError("The map has not been loaded yet, so room ids are unknown")
            room_ids, times = value
            dps = c.cmd_rooms(room_ids, self.map_id, times)
            self._sent_rooms = tuple(int(r) for r in room_ids)
        else:
            raise ValueError(f"Unknown command {key}")
        await self.async_send(dps)

    def dnd_dps(self, on: bool | None = None, start: tuple | None = None, end: tuple | None = None) -> dict:
        d = self.data or {}
        return c.cmd_dnd(
            d.get("dnd", False) if on is None else on,
            start or d.get("dnd_start") or (22, 0),
            end or d.get("dnd_end") or (8, 0),
        )

    # ------------------------------------------------------------ worker thread
    def _push(self, state: dict | None) -> None:
        """Hand a new snapshot (or an availability change) to the event loop."""
        self.hass.loop.call_soon_threadsafe(self._async_push, state)

    @callback
    def _async_push(self, state: dict | None) -> None:
        if state is not None:
            prev = self._prev_state
            self._prev_state = state.get("state")
            firmware = state.get("firmware")
            if firmware and firmware != (self.data or {}).get("firmware"):
                registry = dr.async_get(self.hass)
                device = registry.async_get_device_by_identifier((DOMAIN, self.device_id), self.config_entry.entry_id)
                if device is not None and device.sw_version != firmware:
                    registry.async_update_device(device.id, sw_version=firmware)
            self._check_alerts(state)
            self.async_set_updated_data(state)
            self._track_dock_floor(prev, self._prev_state)
            self._schedule_map_refresh(prev, self._prev_state)
        else:
            self.async_update_listeners()

    def _find_host(self) -> str:
        if self.host:
            return self.host
        _LOGGER.debug("Looking for %s on the LAN", self.device_id)
        found = tinytuya.find_device(dev_id=self.device_id)
        ip = found.get("ip") if found else None
        if not ip:
            raise ConnectionError("robot not found on the LAN; set its IP address in the integration options")
        _LOGGER.info("Found %s at %s", self.robot_name, ip)
        return ip

    def _worker(self) -> None:
        robot = LegacyRobotState() if self.legacy else RobotState()
        backoff = 5
        while not self._stop.is_set():
            device = None
            try:
                ip = self._find_host()
                device = tinytuya.Device(self.device_id, address=ip, local_key=self.local_key,
                                         version=3.3, persist=True)
                device.set_socketTimeout(SOCKET_TIMEOUT)
                device.set_socketRetryLimit(1)
                status = device.status()
                if not status or "Error" in status:
                    if status and str(status.get("Err")) == "914":  # tinytuya: wrong local key (or version)
                        self._key_failures += 1
                        if self._key_failures == 3 and self.cloud is not None:
                            self.hass.add_job(self._async_refresh_key)
                    raise ConnectionError(f"status request failed: {status}")
                self._key_failures = 0
                self.host = ip
                self.online = True
                robot.update(status.get("dps") or {})
                self._push(robot.snapshot())
                if not self.legacy:
                    device.set_multiple_values(c.cmd_keep_alive(), nowait=True)
                    device.updatedps(RAW_DPS, nowait=True)
                _LOGGER.info("Connected to %s at %s", self.robot_name, ip)
                backoff = 5
                self._run_connected(device, robot)
            except Exception as err:
                if self._stop.is_set():
                    break
                _LOGGER.warning("Connection to %s lost: %s (retrying in %ss)", self.robot_name, err, backoff)
                if self.online:
                    self.online = False
                    self._push(None)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 300)
                if backoff >= 80 and not self.config_entry.options.get(CONF_HOST):
                    self.host = ""  # rediscover in case DHCP moved it
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
        self.online = False

    def _run_connected(self, device: tinytuya.Device, robot: RobotState) -> None:
        next_beat = time.monotonic() + HEARTBEAT_SECONDS
        next_poll = time.monotonic() + POLL_SECONDS
        while not self._stop.is_set():
            while True:
                try:
                    kind, queued, dps = self._commands.get_nowait()
                except queue.Empty:
                    break
                if kind == "seed":
                    # Cloud values only fill DPs the robot hasn't reported locally.
                    fresh = {k: v for k, v in dps.items() if str(k) not in robot.dps}
                    if fresh and robot.update(fresh):
                        self._push(robot.snapshot())
                elif time.monotonic() - queued <= COMMAND_MAX_AGE:
                    device.set_multiple_values(dps, nowait=True)
                else:
                    _LOGGER.debug("Dropping stale command %s", dps)
            data = device.receive()
            if data:
                if "Error" in data and str(data.get("Err")) != "904":
                    raise ConnectionError(data.get("Error"))
                if robot.update(data.get("dps") or {}):
                    self._push(robot.snapshot())
            now = time.monotonic()
            if now >= next_beat:
                device.heartbeat(nowait=True)
                next_beat = now + HEARTBEAT_SECONDS
            if now >= next_poll:
                status = device.status()
                if status and "Error" in status:
                    raise ConnectionError(status.get("Error"))
                if status and robot.update(status.get("dps") or {}):
                    self._push(robot.snapshot())
                next_poll = now + POLL_SECONDS

    # ------------------------------------------------------------ map
    @callback
    @callback
    def _track_dock_floor(self, prev: str | None, new: str | None) -> None:
        """On docking, the robot is on the dock's floor: learn it after a clean, use it otherwise."""
        if new != "docked" or prev == "docked" or self.cloud is None or self.legacy:
            return
        if prev == "returning":
            # It drove itself home, so the dock is on the floor it just cleaned; the map
            # refresh after docking confirms which one that is. (Paused, then lifted onto
            # the dock, says nothing about where the dock is.)
            self._docked_after_clean = True
        elif len(self.floors) > 1:
            self.config_entry.async_create_background_task(self.hass, self._async_dock_floor(), "eufy dock floor")

    @property
    def dock_floor(self) -> int | None:
        """The dock's floor: learned when the robot docks after cleaning, else the first floor seen."""
        return self.station_floor if self.station_floor in self.floors else self.primary_map_id

    async def _async_dock_floor(self) -> None:
        """Show the dock's floor (its saved map) while the robot sits on the dock."""
        target = self.dock_floor
        if target is None or target == self.map_id or target not in self.floors:
            return
        _LOGGER.info("Docked: showing %s", self.floors[target]["name"])
        await self._async_use_floor(target)
        self.map_id = target
        self.map_updated = self.floors[target]["updated"]
        self.map_png = await self.hass.async_add_executor_job(self._safe_render)
        await self._async_rebuild_live()
        await self._async_remember_floor(save=False)
        await self._async_save()
        self.async_update_listeners()

    async def _async_use_floor(self, map_id: int) -> None:
        """Make another floor the current one, keeping the one we leave."""
        await self._async_remember_floor(save=False)
        known = self.floors.get(map_id)
        self.map = mapdata.MapSnapshot(raw=dict(known["raw"])) if known else mapdata.MapSnapshot()
        self._map_rooms = list(known["rooms"]) if known else []
        self._live_pose, self._live_path, self._live_rooms = None, [], ()

    def _schedule_map_refresh(self, prev: str | None, new: str | None) -> None:
        if self.cloud is None or self.legacy:
            return
        self._ensure_live(new)
        interval = self.config_entry.options.get(CONF_MAP_INTERVAL, DEFAULT_MAP_INTERVAL)
        finished = prev in ACTIVE_STATES and new == "docked"
        due = new in ("cleaning", "returning") and time.monotonic() - self._last_map_fetch > interval
        if finished or due:
            delay = 45 if finished else 0
            if self._map_retry_unsub:
                self._map_retry_unsub()
            self._map_retry_unsub = async_call_later(self.hass, delay, self._async_map_timer)

    async def _async_map_timer(self, _now) -> None:
        self._map_retry_unsub = None
        await self.async_refresh_map()

    async def async_refresh_map(self) -> None:
        """Ask the robot to upload its map, then fetch the latest snapshot from the cloud."""
        if self.cloud is None:
            return
        self._last_map_fetch = time.monotonic()
        if self.online:
            await self.async_send(c.cmd_keep_alive())
        try:
            result = await self.hass.async_add_executor_job(self._fetch_map)
        except Exception as err:
            _LOGGER.warning("Map refresh failed: %s; retrying in 5 minutes", err)
            if self._map_retry_unsub:
                self._map_retry_unsub()
            self._map_retry_unsub = async_call_later(self.hass, 300, self._async_map_timer)
            return
        self.last_fetch = result
        snapshot = result.snapshot
        if snapshot is None:
            _LOGGER.debug("The cloud holds no map data yet")
            return
        new_id = snapshot.map_id
        if self._docked_after_clean:
            self._docked_after_clean = False
            if new_id is not None:
                self.station_floor = new_id
        elif ((self.data or {}).get("state") == "docked" and new_id is not None and len(self.floors) > 1
              and new_id != self.dock_floor):
            # The cloud keeps the last floor's map until the robot cleans again; it's on the dock now.
            _LOGGER.debug("Ignoring map %s from the cloud: the robot is docked on map %s", new_id, self.dock_floor)
            return
        if new_id is not None and self.map_id is not None and new_id != self.map_id:
            # The robot is on another floor: keep this one, and start from what we know of that one.
            known = self.floors.get(new_id)
            await self._async_use_floor(new_id)
            _LOGGER.info("The robot moved from map %s to map %s (%s)", self.map_id, new_id,
                         known["name"] if known else "a new floor")
        self.map = self._merge(snapshot)
        rooms = self.map.rooms()
        if rooms:
            self._map_rooms = rooms
        if self.map.map_id is not None:
            self.map_id = self.map.map_id
        self.map_png = await self.hass.async_add_executor_job(self._safe_render) or self.map_png
        self.map_updated = dt_util.utcnow()
        await self._async_rebuild_live()
        _LOGGER.info("Map updated: map %s, %d rooms", self.map_id, len(self.rooms))
        await self._async_remember_floor(save=False)
        await self._async_save()
        self.async_update_listeners()

    def _merge(self, snapshot: mapdata.MapSnapshot) -> mapdata.MapSnapshot:
        """Channels missing from this fetch keep their last value, unless they belong to another map."""
        merged = mapdata.MapSnapshot(raw=dict(self.map.raw))
        merged.raw.update(snapshot.raw)
        new_id = snapshot.map_id
        if new_id is not None:
            for channel in list(merged.raw):
                if channel in snapshot.raw:
                    continue
                old_id = mapdata.map_id_of(merged.get(channel))
                if old_id is not None and old_id != new_id:
                    del merged.raw[channel]
        return merged

    async def _async_remember_floor(self, save: bool = True) -> None:
        """Record the current map as a floor (and tell the floor entities when the floors change)."""
        map_id = self.map.map_id
        if map_id is None or not self.map.raw:
            return
        m = self.map.get(mapdata.CH_MAP)
        name = (m.name.strip() if m is not None and m.name else "") or f"Map {map_id}"
        known = set(self.floors)
        self.floors[map_id] = {"name": name, "raw": dict(self.map.raw), "rooms": list(self._map_rooms),
                               "updated": self.map_updated}
        if self.primary_map_id is None:
            self.primary_map_id = map_id
        self.floor_pngs[map_id] = self.map_png
        self.floor_shapes[map_id] = self.room_shapes
        if save:
            await self._async_save()
        async_dispatcher_send(self.hass, self.floor_signal, set(self.floors) != known)

    async def _async_save(self) -> None:
        await self._store.async_save({
            "channels": {str(k): base64.b64encode(v).decode() for k, v in self.map.raw.items()},
            "rooms": self._map_rooms if self.map.raw else [],
            "updated": self.map_updated.isoformat() if self.map_updated else None,
            "primary": self.primary_map_id,
            "station": self.station_floor,
            "floors": {
                str(map_id): {
                    "name": f["name"],
                    "channels": {str(k): base64.b64encode(v).decode() for k, v in f["raw"].items()},
                    "rooms": f["rooms"],
                    "updated": f["updated"].isoformat() if f["updated"] else None,
                }
                for map_id, f in self.floors.items()
            },
        })

    # ------------------------------------------------------------ live view
    @property
    def live_interval(self) -> float:
        return float(self.config_entry.options.get(CONF_LIVE_INTERVAL, DEFAULT_LIVE_INTERVAL))

    @callback
    def _ensure_live(self, state: str | None) -> None:
        """Start following the robot when it starts moving; the loop ends itself afterwards."""
        if state in LIVE_STATES and self.live_interval > 0 and self._live_task is None:
            self._live_task = self.config_entry.async_create_background_task(
                self.hass, self._async_live_loop(), "eufy live position")

    async def _async_live_loop(self) -> None:
        try:
            while (self.data or {}).get("state") in LIVE_STATES and self.live_interval > 0:
                started = time.monotonic()
                try:
                    pose, path, rooms = await self.hass.async_add_executor_job(self._fetch_live)
                except Exception as err:  # keep following; the cloud has hiccups
                    _LOGGER.debug("Live position read failed: %s", err)
                    await asyncio.sleep(max(self.live_interval, 10))
                    continue
                if path is None:
                    path = self._live_path
                rooms = self._highlight_rooms(rooms)
                if pose is not None and (pose != self._live_pose or path != self._live_path
                                         or rooms != self._live_rooms):
                    self._live_pose, self._live_path, self._live_rooms = pose, path, rooms
                    if self._renderer is not None:
                        self.live_png = await self.hass.async_add_executor_job(
                            self._renderer.frame, pose, path, True, rooms)
                    async_dispatcher_send(self.hass, self.live_signal)
                await asyncio.sleep(max(0.5, self.live_interval - (time.monotonic() - started)))
        finally:
            self._live_task = None
            if self._live_rooms and self._renderer is not None and not self._stop.is_set():  # drop the highlight
                self._live_rooms = ()
                self.live_png = await self.hass.async_add_executor_job(
                    self._renderer.frame, self._live_pose, self._live_path, True)
                async_dispatcher_send(self.hass, self.live_signal)

    def _fetch_live(self) -> tuple[tuple | None, list | None, tuple | None]:
        """Read the robot's position, and about every 10 s its cleaned path and selected rooms."""
        with self._map_lock:
            pose = mapdata.read_pose(self.cloud, self.device_id)
            path = rooms = None
            if time.monotonic() - getattr(self, "_last_path_read", 0) >= 10:
                self._last_path_read = time.monotonic()
                path = mapdata.read_path(self.cloud, self.device_id)
                rooms = mapdata.read_selected_rooms(self.cloud, self.device_id)
        return pose, path, rooms

    def _highlight_rooms(self, from_cloud: tuple | None) -> tuple[int, ...]:
        """The rooms being cleaned right now: what the robot reports, else what we asked for."""
        if (self.data or {}).get("task") != c.MODE_NAMES.get(pb.WorkStatus.Mode.SELECT_ROOM):
            return ()
        if from_cloud:
            return tuple(from_cloud)
        if from_cloud is None and self._live_rooms:  # not re-read this tick
            return self._live_rooms
        return self._sent_rooms

    # ------------------------------------------------------------ alerts
    @callback
    def _check_alerts(self, state: dict) -> None:
        """Fire an alert event for each newly raised error, warning or robot message."""
        if "error_codes" not in state:
            return
        current = {
            "error": set(state.get("error_codes") or []),
            "warning": set(state.get("warning_codes") or []),
            "message": (state.get("prompt_time"), tuple(state.get("prompt_codes") or [])),
        }
        seen, self._alert_seen = self._alert_seen, current
        if seen is None:  # the first report after start-up only sets the baseline
            return
        texts = {"error": legacy.ERRORS if self.legacy else ERRORS, "warning": WARNINGS}
        for kind in ("error", "warning"):
            for code in sorted(current[kind] - seen[kind]):
                self._fire_alert(kind, code, texts[kind].get(code, f"{kind.title()} {code}"))
        if current["message"] != seen["message"] and current["message"][1]:
            for code in current["message"][1]:
                self._fire_alert("message", code, PROMPTS.get(code, f"Message {code}"))

    @callback
    def _fire_alert(self, kind: str, code: int, text: str) -> None:
        _LOGGER.debug("Alert %s %s: %s", kind, code, text)
        async_dispatcher_send(self.hass, self.alert_signal, kind, {"code": code, "message": text})

    # ------------------------------------------------------------ local key
    async def _async_refresh_key(self) -> None:
        """The robot refused our key three times: it may have been re-paired, so look it up again."""
        _LOGGER.warning("%s rejected its local key; looking it up again in the eufy cloud", self.robot_name)
        try:
            devices = await self.hass.async_add_executor_job(self.cloud.devices)
        except EufyAuthError:
            self.config_entry.async_start_reauth(self.hass)
            return
        except Exception as err:
            _LOGGER.warning("Could not refresh the local key: %s", err)
            return
        key = next((d["local_key"] for d in devices if d["id"] == self.device_id and d["local_key"]), None)
        if key and key != self.local_key:
            _LOGGER.info("Found a new local key for %s; reloading", self.robot_name)
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_LOCAL_KEY: key})

    # ------------------------------------------------------------ per-room settings
    def room_params(self):
        """The robot's per-room settings (RoomParams), from the map."""
        return self.map.get(mapdata.CH_ROOM_PARAMS)

    async def async_set_rooms_custom(self, enable: bool | None = None, room_id: int | None = None,
                                     **changes) -> None:
        """Change per-room settings (or switch them on/off) the way the app does."""
        params = self.room_params()
        if params is None or self.map_id is None:
            raise ValueError("The robot's room settings haven't been loaded from the map yet")
        if room_id is not None:
            room = next((r for r in params.rooms if r.id == room_id), None)
            if room is None:
                raise ValueError(f"Room {room_id} isn't on the map")
            if "suction" in changes:
                room.custom.fan.suction = changes["suction"]
            if "clean_times" in changes:
                room.custom.clean_times = changes["clean_times"]
            if "water_level" in changes:
                room.custom.mop_mode.level = changes["water_level"]
            if "clean_type" in changes:
                room.custom.clean_type.value = changes["clean_type"]
        if enable is not None:
            params.custom_enable = enable
        await self.async_send(c.cmd_rooms_custom(params, self.map_id))
        # Show the change straight away; the next map refresh confirms it.
        body = params.SerializeToString()
        self.map.raw[mapdata.CH_ROOM_PARAMS] = c._varint(len(body)) + body
        self.async_update_listeners()

    async def _async_rebuild_live(self) -> None:
        """Redraw the cached floor plan after a full map refresh."""
        try:
            self._renderer = await self.hass.async_add_executor_job(mapdata.MapRenderer.build, self.map)
        except Exception as err:
            _LOGGER.warning("Could not prepare the live map: %s", err)
            self._renderer = None
            return
        if self._renderer is None:
            return
        pose = self._live_pose or self.map.pose()
        path = self._live_path or self.map.path()
        self.live_png = await self.hass.async_add_executor_job(self._renderer.frame, pose, path, True)
        async_dispatcher_send(self.hass, self.live_signal)

    def _fetch_map(self) -> mapdata.FetchResult:
        with self._map_lock:
            return mapdata.fetch_snapshot(self.cloud, self.device_id)

    def _safe_render(self) -> bytes | None:
        try:
            return self.map.render_png()
        except Exception as err:
            _LOGGER.warning("Could not render the map: %s", err)
            return None


def map_age(coordinator: EufyCoordinator) -> timedelta | None:
    if coordinator.map_updated is None:
        return None
    return dt_util.utcnow() - coordinator.map_updated
