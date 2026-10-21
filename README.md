# eufy Clean (Local)

<img src="https://raw.githubusercontent.com/kkrankall/eufy-clean-local/main/custom_components/eufy_clean_local/brand/icon.png" alt="eufy Clean (Local) icon" width="96" align="right">

A Home Assistant integration for the **eufy L60** robot vacuum family
(L60 `T2267`, L60 Hybrid `T2268`, L60 SES `T2277`, L60 Hybrid SES `T2278`), plus
the older **RoboVac 30C** (`T2118`) with basic control.

Control and status run **locally** over the robot's Tuya LAN protocol, with
push updates and no polling of the cloud. Your eufy login is used once to look up the
robot's local key, and again to download the map, which the robot only
uploads to the cloud.

> **Disclaimer:** this is an independent project. I'm not affiliated with,
> endorsed by or connected to eufy or Anker in any way; eufy and RoboVac are
> their trademarks. Use it at your own risk. It's provided as is, without any
> warranty (see [LICENSE](LICENSE)).

**Tested robots:** only the L60 (`T2267`) and the RoboVac 30C (`T2118`). The
other L60 models listed above, and other eufy RoboVacs that use the same
protocols, may work too, but they're untested. Setup only offers the models
listed above; if yours isn't offered, open an issue with the model number that
Home Assistant's log shows.

## What you get

- **Vacuum:** start, pause and resume, stop, return to dock, spot clean, locate,
  suction (Quiet / Standard / Turbo / Max).
- **Room cleaning:** each room appears as a vacuum *segment*, so you can map
  rooms to Home Assistant areas and use `vacuum.clean_area`. There's also a
  "Clean <room>" button per room and a `room_clean` command (below). Rooms come
  from the robot's map; until the cloud has one, list them yourself in the
  integration's options (`1=Kitchen`, one per line).
- **Map:** an image entity rendered from the map the robot uploads, with rooms,
  walls, no-go zones, the dock, the robot and the path it cleaned. It refreshes
  during and after each clean, and on the Refresh map button. The robot uploads
  its map to the cloud while it cleans, so a robot that has never cleaned since
  setup has no map yet.
- **Live map:** while the robot cleans, the map image moves the robot and
  extends its cleaned path every 3 seconds (2-10 s, or off, in the options).
  Nothing is read from the cloud while the robot is docked. The same view is
  also a camera entity, for cards that want a camera stream.
- **Sensors:** battery, detailed status, errors and warnings (with eufy's text),
  last message, current and total cleaning area and time, cleanings, consumable
  life (side brush, rolling brush, filter, dust bag on SES), firmware, Wi-Fi.
- **Settings:** BoostIQ, resume after recharge, child lock, do-not-disturb (on/off,
  start and end), volume, cleaning passes. Mop water level and cleaning type are added
  on Hybrid models.
- **Self-empty station** (added when the robot reports one): station status, dust
  bag life, auto-empty on/off and "every N cleanings", auto hair cutting, and
  Empty dust / Cut hair buttons.
- **Buttons:** reset each consumable, refresh map.
- **Alerts:** an event entity fires `error`, `warning` or `message` with eufy's
  text ("Wheel stuck", "Dust bag almost full", "Task finished, heading home"),
  and a problem sensor is on while any error or warning is active. Use them for
  notifications (example below).
- **Per-room settings:** a "Per-room settings" switch plus a suction and passes
  select for every room (and mop water level on Hybrids), like the app's custom
  room settings. With it on, room cleans use each room's own settings.
- **Self-healing:** if eufy stops accepting the saved password, Home Assistant
  asks you to sign in again. If the robot is re-paired and its local key changes,
  the new key is fetched automatically. Diagnostics (with your credentials, key
  and MAC redacted) can be downloaded from the device page.

### RoboVac 30C

The 30C uses eufy's older protocol and has no map. It gets the vacuum (start,
pause, stop, dock, spot, locate, suction No suction / Standard / Boost IQ / Max),
battery, status, error, the alert event and the problem sensor. Extra
`vacuum.send_command` commands: `auto_clean`, `edge_clean`, `small_room_clean`,
`spot_clean`, `remote` (`direction`: forward, back, left, right) and `raw`. Add it
by adding the integration again and picking the 30C.

The 30C accepts only one local connection. While another integration (or a
tool) is connected to it, it can't be found or reached, so disable it in any
other integration first.

## The map

| Rooms, the cleaned path, the dock and the robot | With a no-go zone and a virtual wall |
|---|---|
| ![Demo map: six coloured, labelled rooms with walls, furniture, the dock and the robot](https://raw.githubusercontent.com/kkrankall/eufy-clean-local/main/docs/images/demo-map.png) | ![The same demo map with a red no-go zone in the kitchen and a virtual wall at the bathroom door](https://raw.githubusercontent.com/kkrankall/eufy-clean-local/main/docs/images/demo-map-zones.png) |

These show a made-up home. Its data is written in the robot's real map format and
drawn by the integration's own renderer (`tools/make_demo_map.py`), so the map
entity looks like this with your rooms. Neighbouring rooms get different
colours, room names come from the eufy app, the blue square is the dock, the
robot icon turns to face the way the robot is heading, and the thin blue line is where it has cleaned. No-go zones
are red boxes, and virtual walls are red lines.

To watch the robot move, show the map image on a dashboard, for example with a
picture-entity card:

```yaml
type: picture-entity
entity: image.l60_map
show_name: false
show_state: false
```

During a room clean, the rooms being cleaned are highlighted on the live map.

The camera `camera.<name>_live_map` shows the same view as a stream
(`camera_view: live`), which keeps the per-frame updates out of the recorder.
It works in Chrome and Edge, but not in Safari: Home Assistant's service worker
breaks endless camera streams there, and the picture turns into a broken image
within a minute or so. The image entity works in every browser. The
[vacuum-card](https://github.com/denysdovhan/vacuum-card)'s `map:` option only
refreshes the picture if the map entity already exists when the card first
draws, so the picture-entity card is the reliable choice.

### Notifications

```yaml
triggers:
  - trigger: state
    entity_id: event.l60_alert
    not_to: unavailable
actions:
  - action: notify.notify
    data:
      title: "L60 {{ trigger.to_state.attributes.event_type }}"
      message: "{{ trigger.to_state.attributes.message }}"
```

## Install

1. In HACS, add this repository as a custom repository (type *Integration*).
2. Install **eufy Clean (Local)** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → eufy Clean (Local)**, and
   sign in with your eufy app account.

The robot is found on your LAN by its UDP broadcast. If that's blocked, set its
IP address in the integration's options.

Only one program can hold the robot's local connection reliably. If the robot
is also set up in another local integration (for example *Eufy RoboVac*),
disable it there.

## Extra commands

`vacuum.send_command` accepts:

| Command | Params |
|---|---|
| `room_clean` | `rooms`: list of room names or ids; optional `clean_times` (1 or 2) |
| `zone_clean` | `zones`: list of rectangles, each 4 `[x, y]` corners in map units (cm) |
| `auto_clean` | optional `clean_times` |
| `quick_mapping` | — |
| `remote` | `direction`: brake, forward, back, left, right |
| `raw` | DP number → value, sent as-is |

```yaml
action: vacuum.send_command
target:
  entity_id: vacuum.l60
data:
  command: room_clean
  params:
    rooms: [Kitchen, Bedroom 1]
```

## How it works

The DP layout, protobuf messages and status mapping come from the eufy app's
own per-model codec for the L60 and its protobuf schema. See `tools/protos`
and `NOTICE`. Tests in `tests/` run against Home Assistant's test harness with
a simulated robot.
