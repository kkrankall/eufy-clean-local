"""Older eufy RoboVacs (e.g. the 30C) that speak plain Tuya DPs instead of protobuf.

These robots have no map: they report a status string, a battery level and an
error number, and take a handful of simple commands. The DP layout follows
damacus/robovac's T2118 definition (Apache License 2.0; see NOTICE).
"""

from __future__ import annotations

from typing import Any

MODELS = {"T2118": "RoboVac 30C"}

DP_START_PAUSE = "2"
DP_DIRECTION = "3"
DP_MODE = "5"
DP_STATUS = "15"
DP_GO_HOME = "101"
DP_SUCTION = "102"
DP_LOCATE = "103"
DP_BATTERY = "104"
DP_ERROR = "106"

# HA-facing name -> value the robot uses.
SUCTIONS = {"No suction": "No_suction", "Standard": "Standard", "Boost IQ": "Boost_IQ", "Max": "Max"}
MODES = {"auto": "Auto", "small_room": "SmallRoom", "spot": "Spot", "edge": "Edge"}

STATUS_TEXT = {
    "Running": "Cleaning",
    "standby": "Standby",
    "Sleeping": "Sleeping",
    "Charging": "Charging",
    "completed": "Finished",
    "Recharge": "Returning to dock",
    "recharge_needed": "Low battery, returning",
}
STATUS_STATE = {
    "Running": "cleaning",
    "standby": "idle",
    "Sleeping": "idle",
    "Charging": "docked",
    "completed": "docked",
    "Recharge": "returning",
    "recharge_needed": "returning",
}
ERRORS = {
    1: "Front bumper stuck",
    2: "Wheel stuck",
    3: "Side brush stuck",
    4: "Rolling brush stuck",
    5: "Robot trapped",
    6: "Robot trapped",
    7: "Wheel suspended",
    8: "Low battery",
    9: "Magnetic boundary detected",
    12: "Right wall sensor problem",
    13: "Robot tilted",
    14: "Insert the dust collector",
    17: "Restricted area detected",
    18: "Laser cover stuck",
    19: "Laser sensor stuck",
    20: "Laser sensor blocked",
    21: "Base blocked",
}


def error_code(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def snapshot(dps: dict[str, Any]) -> dict[str, Any]:
    """The same state keys the L60 path produces, for the subset this robot has."""
    status = dps.get(DP_STATUS)
    state = STATUS_STATE.get(status, "idle")
    if state == "cleaning" and dps.get(DP_START_PAUSE) is False:
        state = "paused"
    code = error_code(dps.get(DP_ERROR))
    if code:
        state = "error"
    suction = next((name for name, v in SUCTIONS.items() if v == dps.get(DP_SUCTION)), None)
    errors = [ERRORS.get(code, f"Error {code}")] if code else []
    return {
        "state": state,
        "status": "Paused" if state == "paused" else STATUS_TEXT.get(status, status or "Unknown"),
        "battery": dps.get(DP_BATTERY),
        "fan_speed": suction,
        "error": errors[0] if errors else "No error",
        "error_codes": [code] if code else [],
        "warning_codes": [],
        "warning": "No warning",
    }


def command(key: str, state: str | None, value=None) -> dict | None:
    """DPs for a vacuum action."""
    if key == "start":
        return {DP_START_PAUSE: True} if state == "paused" else {DP_MODE: MODES["auto"]}
    if key == "pause":
        return {DP_START_PAUSE: False}
    if key == "stop":
        return {DP_START_PAUSE: False}
    if key == "return_to_base":
        return {DP_GO_HOME: True}
    if key == "clean_spot":
        return {DP_MODE: MODES["spot"]}
    if key == "locate":
        return {DP_LOCATE: True}
    if key == "fan_speed":
        return {DP_SUCTION: SUCTIONS[value]}
    if key in MODES:
        return {DP_MODE: MODES[key]}
    return None
