"""
Halo — web UI & REST API server for the garage door module.

Runs on the local network (e.g. http://<pi-ip>:8080). Standard library only
(ThreadingHTTPServer); paho-mqtt is optional for Wyze sensor ingest.

Efficiency notes:
  * Clients subscribe to /api/events (Server-Sent Events) and get a push only
    when state actually changes, instead of polling /api/status every second.
  * CC1101 identity registers are read once at startup and refreshed only after
    a transmission, so status requests never touch the SPI bus.
  * Static files are cached in memory, gzip-compressed once, and served with
    ETags so repeat loads are a 304.
"""

import os
import gzip
import json
import time
import socket
import hashlib
import secrets
import ssl
from datetime import datetime, timezone
import urllib.error
import urllib.request
import logging
import re
import errno
import math
import signal
import shutil
import subprocess
import sys
import threading
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from cc1101 import CC1101, RadioError
from security_plus import generate_burst_bytes, generate_burst_bits
from remote_manager import RemoteManager

logger = logging.getLogger("Halo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

APP_NAME = "Halo"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

STATE_FILE = os.path.join(BASE_DIR, "garage_state.json")
DOCKER_STATE_FILE = "/var/lib/docker/volumes/homebridge_data/_data/garage_state.json"

LIGHT_AUTO_OFF_SEC = 270          # Chamberlain courtesy light: 4.5 minutes
TRAVEL_STEP_SEC = 0.25            # door position update cadence while moving
SSE_HEARTBEAT_SEC = 15
MAX_LOG_ENTRIES = 50

radio_lock = threading.Lock()     # SPI radio hardware
state_lock = threading.RLock()    # door_info


def _now_iso() -> str:
    """UTC timestamp. Everything is stored in UTC and shown in the chosen time zone."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Time zone ────────────────────────────────────────────────────────────────
# Uses the IANA time-zone database (the same rules phones and computers use),
# so daylight saving ("spring forward", "fall back") is applied automatically.
# Timers (auto-close, light auto-off, alerts) count real elapsed seconds, so a
# DST change never makes them fire early or late.
DEFAULT_TZ = "America/New_York"
SETTINGS = {"timezone": DEFAULT_TZ, "clock_24h": False}


def _zone(name: str = None):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or SETTINGS["timezone"])
    except Exception:
        return None


def tz_database_ok() -> bool:
    return _zone(DEFAULT_TZ) is not None


def valid_timezone(name: str) -> bool:
    if not isinstance(name, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$", name) or len(name) > 64:
        return False
    if name == "UTC":
        return True
    return _zone(name) is not None if tz_database_ok() else True


def local_dt(ts: float = None) -> datetime:
    """A moment in the chosen time zone (falls back to UTC if the zone data is missing)."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, _zone() or timezone.utc)


def fmt_clock(ts: float = None) -> str:
    """e.g. '7:42 PM', or '19:42' with 24-hour time on."""
    dt = local_dt(ts)
    return dt.strftime("%H:%M") if SETTINGS["clock_24h"] else dt.strftime("%I:%M %p").lstrip("0")


def tz_public() -> dict:
    dt = local_dt()
    off = int(dt.utcoffset().total_seconds() // 60)
    return {"name": SETTINGS["timezone"], "abbr": dt.tzname(), "offset_minutes": off,
            "clock_24h": SETTINGS["clock_24h"],
            "dst": bool(dt.dst()), "database": tz_database_ok(), "local_time": dt.strftime("%Y-%m-%d %H:%M:%S")}


def set_timezone(name: str) -> dict:
    if not valid_timezone(name):
        raise ValueError(f"Unknown time zone '{name}'")
    SETTINGS["timezone"] = name
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(timezone=name)
    t = tz_public()
    add_log("Halo", f"Time zone set to {name} ({t['abbr']})", True)
    return t


door_info = {
    "state": "CLOSED",
    "percent": 0,
    "travel_time": 12.0,
    "light_on": False,
    "light_timer_expires": 0.0,
    "locked": False,
    "sensor": {"name": "Garage Door Sensor", "mac": None, "state": None, "battery": None,
               "signal_strength": None, "last_seen": None, "connected": False},
    # Car presence, estimated from a motion sensor in the bay + door cycles
    "car": {"state": "unknown", "source": None, "reason": "", "updated_at": None},
    "motion": {"name": "Car Bay Motion", "mac": None, "active": False, "last_motion": None, "battery": None,
               "signal_strength": None, "connected": False},
    "updated_at": _now_iso(),
}
hw_info = {"part_number": "0x00", "version": "0x00", "state": "0x00"}
activity_log = deque(maxlen=MAX_LOG_ENTRIES)


# ── Change notification (drives Server-Sent Events) ───────────────────────────
_change = threading.Condition()
_version = 0


def notify_change():
    global _version
    with _change:
        _version += 1
        _change.notify_all()


def add_log(action: str, details: str = "", success: bool = True):
    activity_log.appendleft({
        "ts": round(time.time(), 3),
        "timestamp": local_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "details": details,
        "success": success,
    })
    notify_change()


# ── Persistence ────────────────────────────────────────────────────────────────
def load_door_state():
    for sf in (STATE_FILE, DOCKER_STATE_FILE):
        if not os.path.exists(sf):
            continue
        try:
            with open(sf, "r") as f:
                saved = json.load(f)
            door_info["state"] = saved.get("doorState", saved.get("state", "CLOSED"))
            if door_info["state"] not in ("OPEN", "CLOSED"):
                door_info["state"] = "CLOSED"     # never resume mid-travel
            door_info["percent"] = 100 if door_info["state"] == "OPEN" else 0
            door_info["light_on"] = saved.get("lightOn", saved.get("light_on", False))
            door_info["locked"] = saved.get("locked", False)
            if saved.get("car") in ("present", "away"):
                door_info["car"].update(state=saved["car"], source="saved", reason="Restored after restart")
            return
        except Exception as e:
            logger.warning(f"Could not load state from {sf}: {e}")


def save_door_state():
    """Persist state for the Homebridge plugin's file watcher. Caller holds state_lock."""
    payload = json.dumps({
        "doorState": door_info["state"],
        "percent": door_info["percent"],
        "lightOn": door_info["light_on"],
        "locked": door_info["locked"],
        "car": door_info["car"]["state"],
        "updatedAt": _now_iso(),
    }, indent=2)
    for sf in (STATE_FILE, DOCKER_STATE_FILE):
        parent = os.path.dirname(sf)
        if not os.path.isdir(parent):
            continue
        try:
            tmp = sf + ".tmp"
            with open(tmp, "w") as f:
                f.write(payload)
            os.replace(tmp, sf)               # atomic: watchers never see half a file
        except Exception as e:
            logger.debug(f"Could not save state to {sf}: {e}")


# ── Light (courtesy light with auto-off timer) ────────────────────────────────
_light_timer = None


def set_light(on: bool, log_action: str = None, log_msg: str = None):
    """Set light state, (re)arm the auto-off timer, persist and notify."""
    global _light_timer
    with state_lock:
        door_info["light_on"] = on
        door_info["light_timer_expires"] = time.time() + LIGHT_AUTO_OFF_SEC if on else 0.0
        if _light_timer:
            _light_timer.cancel()
            _light_timer = None
        if on:
            _light_timer = threading.Timer(LIGHT_AUTO_OFF_SEC, _light_auto_off)
            _light_timer.daemon = True
            _light_timer.start()
        save_door_state()
    if log_action:
        add_log(log_action, log_msg or "", success=True)
    else:
        notify_change()


def _light_auto_off():
    with state_lock:
        if not door_info["light_on"] or time.time() < door_info["light_timer_expires"] - 1:
            return
    set_light(False, "Light", "Courtesy light auto-off after 4.5 min")
    logger.info("[Light] Auto-off timer elapsed")


def _light_on_for_motion(reason: str):
    """Chamberlain openers switch the courtesy light on whenever the door operates."""
    if not door_info["light_on"]:
        set_light(True, "Light", reason)


# ── Door ───────────────────────────────────────────────────────────────────────
_travel_id = 0


_closed_count = 0          # increments every time the door reaches CLOSED


def set_door_state(is_open: bool):
    """Ground-truth update (sensor/integration): door is fully OPEN or CLOSED."""
    global _travel_id, _closed_count
    with state_lock:
        _travel_id += 1                      # cancel any simulated travel
        if not is_open and door_info["state"] != "CLOSED":
            _closed_count += 1
        door_info["state"] = "OPEN" if is_open else "CLOSED"
        door_info["percent"] = 100 if is_open else 0
        door_info["updated_at"] = _now_iso()
        save_door_state()
    notify_change()


_correction = {"at": 0.0}


def correction_edge(now: float = None) -> bool:
    """True if a door open/close change was caused by a manual correction (not real movement)."""
    return (now or time.time()) - _correction["at"] < 3.0


def correct_door(is_open: bool):
    """The user says the sensor/Halo is wrong. Fix what Halo shows; send nothing to the opener."""
    with state_lock:
        before = door_info["state"]
        door_info["sensor"]["state"] = "open" if is_open else "closed"
    _correction["at"] = time.time()
    set_door_state(is_open)
    add_log("Door", f"Corrected in Halo from {before.lower()} to {'open' if is_open else 'closed'} (no signal sent)", True)


def start_door_travel() -> str:
    """Flip the door direction and start simulated travel. Returns the new state."""
    global _travel_id
    with state_lock:
        cur = door_info["state"]
        if cur in ("CLOSED", "CLOSING"):
            target = "OPEN"
        elif cur in ("OPEN", "OPENING"):
            target = "CLOSED"
        else:
            target = "OPEN" if door_info["percent"] < 50 else "CLOSED"
        door_info["state"] = "OPENING" if target == "OPEN" else "CLOSING"
        door_info["updated_at"] = _now_iso()
        save_door_state()
        _travel_id += 1
        my_id = _travel_id
        new_state = door_info["state"]
    notify_change()
    _light_on_for_motion("Courtesy light on with door movement")
    threading.Thread(target=_run_door_travel, args=(target, my_id), daemon=True).start()
    return new_state


def _run_door_travel(target: str, my_id: int):
    with state_lock:
        start = door_info["percent"]
        full = door_info.get("travel_time", 12.0)
    end = 100 if target == "OPEN" else 0
    duration = max(TRAVEL_STEP_SEC, full * abs(end - start) / 100)
    steps = max(1, int(duration / TRAVEL_STEP_SEC))

    for i in range(1, steps + 1):
        time.sleep(duration / steps)
        with state_lock:
            if _travel_id != my_id:
                return                       # superseded by a newer command / sensor
            door_info["percent"] = int(round(start + (end - start) * i / steps))
            if i == steps:
                if target == "CLOSED" and door_info["state"] != "CLOSED":
                    global _closed_count
                    _closed_count += 1
                door_info["state"] = target
                door_info["updated_at"] = _now_iso()
                save_door_state()
        notify_change()
    logger.info(f"Door travel finished: {target}")


# ── Sensor ingest ──────────────────────────────────────────────────────────────
def parse_sensor_payload(data):
    """
    Parse Wyze / Zigbee2MQTT / Homebridge style contact payloads.
    Returns True for OPEN, False for CLOSED, None if unrecognized.
    """
    if not isinstance(data, dict):
        return None
    if "state" in data:
        s = str(data["state"]).strip().lower()
        if s in ("open", "opened", "1", "true"):
            return True
        if s in ("closed", "close", "0", "false"):
            return False
    if "isOpen" in data:
        return bool(data["isOpen"])
    if "contact" in data:
        c = data["contact"]
        if isinstance(c, bool):
            return not c                     # contact True = magnet touching = CLOSED
        s = str(c).strip().lower()
        if s in ("true", "1", "closed"):
            return False
        if s in ("false", "0", "open"):
            return True
    return None


SENSORS = {"door": None, "car": None}       # configured MACs (upper-case), from remote_config.json


def _norm_mac(v):
    return str(v).strip().upper() if v else None


def ingest_sensor(data: dict, source: str):
    """Door contact report. Returns True (open) / False (closed) / None (unrecognized)."""
    is_open = parse_sensor_payload(data)
    if is_open is None:
        return None
    with state_lock:
        was_open = door_info["state"] != "CLOSED"
        s = door_info["sensor"]
        s["mac"] = data.get("mac") or data.get("device") or s.get("mac") or source
        s["state"] = "open" if is_open else "closed"
        if data.get("battery") is not None:
            s["battery"] = data["battery"]
        if data.get("signal_strength") is not None:
            s["signal_strength"] = data["signal_strength"]
        s["last_seen"] = _now_iso()
        s["connected"] = True
    set_door_state(is_open)
    if is_open != was_open:
        _light_on_for_motion("Courtesy light on with door movement")
        add_log("Sensor", f"{s['name']}: door {'opened' if is_open else 'closed'}", success=True)
    return is_open


MOTION_ON = {"1", "true", "on", "open", "active", "detected", "motion", "occupied"}
MOTION_OFF = {"0", "false", "off", "closed", "close", "inactive", "clear", "none", "unoccupied"}


def parse_motion_payload(data):
    """Wyze/Zigbee/HA style motion payloads. True = motion, False = clear, None = unrecognized."""
    for key in ("motion", "occupancy", "state"):
        if key in data:
            v = str(data[key]).strip().lower()
            if v in MOTION_ON:
                return True
            if v in MOTION_OFF:
                return False
    return None


# Motion history for the car estimator (epoch seconds of motion starts)
_motion_times = deque(maxlen=500)
CAR_PRE_SEC = 120      # motion this long before the door opened -> someone was inside
CAR_POST_SKIP = 5      # ignore motion right after closing (door panels settling)
CAR_POST_SEC = 120     # motion this long after the door closed -> someone got out


def ingest_motion(data: dict, source: str):
    active = parse_motion_payload(data)
    if active is None:
        return None
    now = time.time()
    with state_lock:
        m = door_info["motion"]
        was = m["active"]
        m["mac"] = data.get("mac") or m.get("mac") or source
        m["active"] = active
        m["connected"] = True
        if data.get("battery") is not None:
            m["battery"] = data["battery"]
        if data.get("signal_strength") is not None:
            m["signal_strength"] = data["signal_strength"]
        if active:
            m["last_motion"] = _now_iso()
            _motion_times.append(now)
    if active != was:
        notify_change()
    return active


SENSOR_NAME_KEYS = {"door": ("sensor", "sensor_name"), "motion": ("motion", "car_sensor_name")}


def rename_sensor(which: str, name: str):
    """Give a sensor a friendly name; saved in remote_config.json."""
    info_key, cfg_key = SENSOR_NAME_KEYS[which]
    with state_lock:
        old = door_info[info_key]["name"]
        door_info[info_key]["name"] = name
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(**{cfg_key: name})
    add_log("Sensor", f"Renamed '{old}' to '{name}'", True)


# Ceiling-mounted distance sensors pointing down at the parking spot. Each one
# measures how far away the first thing below it is; height = (distance to the
# empty floor) - (current reading). A car is tall AND wide: it sits under two
# sensors spaced ~1.2 m apart at the same time. A person (~0.5 m across) can be
# under only one, so a tall person is never mistaken for a car. A multi-zone
# sensor (e.g. VL53L5CX 8x8) can also send its whole grid to measure width directly.
# (A Wyze motion sensor can't measure height or width: it only sees movement.)
DIST = {"enabled": False, "interval": 2.0, "max_cm": 400.0, "beam_deg": 15.0,
        "min_height_cm": 50.0, "min_width_cm": 100.0, "stable_seconds": 20.0,
        "sensors": [
            {"id": "front", "driver": "hcsr04", "trigger_pin": 17, "echo_pin": 27, "x_cm": 0, "y_cm": 0, "floor_cm": None},
            {"id": "back", "driver": "hcsr04", "trigger_pin": 22, "echo_pin": 23, "x_cm": 120, "y_cm": 0, "floor_cm": None},
        ]}
DIST_INFO = {"status": "off", "present": None, "height_cm": None, "width_cm": None, "width_measurable": None,
             "readings": {}, "error": None, "updated_at": None, "since": None, "committed": None}
_dist_state = {}                     # sensor id -> {"window": deque, "median", "zones", "at"}
_dist_lock = threading.Lock()
GRID_CAR_COVERAGE = 0.6             # share of a grid's zones a car roof fills (a person: ~0.1-0.3)
MIN_VALID_CM = 15                    # closer than this = something touching the sensor (e.g. the open door)
STALE_SEC = 15


def _dist_sensor(sid):
    return next((x for x in DIST["sensors"] if x["id"] == sid), None)


def ingest_distance(sensor_id: str, distance_cm=None, zones=None):
    """A reading from one sensor: distance_cm, or a square grid of zone distances (cm)."""
    cfg = _dist_sensor(sensor_id)
    if cfg is None:
        raise ValueError(f"Unknown distance sensor '{sensor_id}'")
    now = time.time()
    with _dist_lock:
        st = _dist_state.setdefault(sensor_id, {"window": deque(maxlen=5), "median": None, "zones": None, "at": 0})
        if zones is not None:
            z = [float(v) for v in zones]
            n = int(round(math.sqrt(len(z))))
            if n * n != len(z) or n < 2:
                raise ValueError("zones must be a square grid, e.g. 16 or 64 values")
            st.update(zones=z, at=now)
        else:
            d = float(distance_cm)
            if d < MIN_VALID_CM or d > DIST["max_cm"] * 1.05:
                st.update(median=None, at=now, problem="blocked" if d < MIN_VALID_CM else "no echo")
            else:
                st["window"].append(d)
                st.update(median=sorted(st["window"])[len(st["window"]) // 2], at=now, problem=None)
    return _evaluate_distance()


CAR_MATCH_MARGIN_CM = 35.0    # with only a parked-car calibration: this much farther still counts as the car


def _is_tall(d: float, floor, car):
    """Is something car-tall under the sensor at distance d?  Returns (tall, height_or_None).
      both calibrations: tall when closer than halfway between the floor and the parked car
      empty floor only : tall when the object is taller than min_height
      parked car only  : tall when about as close as the car was (or closer)"""
    if floor and car:
        return d <= (floor + car) / 2.0, max(0.0, floor - d)
    if floor:
        h = max(0.0, floor - d)
        return h >= DIST["min_height_cm"], h
    if car:
        return d <= car + CAR_MATCH_MARGIN_CM, None
    return None, None


def _tall_points():
    """Points (x, y, footprint radius, height or None) where something car-tall is.
    Returns (points, sensors_ok, width_measurable, readings, grid_coverage)."""
    now = time.time()
    pts, readings, ok, multi, coverage = [], {}, 0, False, 0.0
    beam = math.tan(math.radians(DIST["beam_deg"]))
    for cfg in DIST["sensors"]:
        st = _dist_state.get(cfg["id"])
        if not st or now - st["at"] > STALE_SEC:
            readings[cfg["id"]] = {"status": "no data"}
            continue
        if cfg.get("driver") == "grid" or st.get("zones"):
            z = st.get("zones")
            floors, cars = cfg.get("floor_zones"), cfg.get("car_zones")
            floors = floors if floors and z and len(floors) == len(z) else None
            cars = cars if cars and z and len(cars) == len(z) else None
            if not z or not (floors or cars):
                readings[cfg["id"]] = {"status": "needs calibration"}
                continue
            n = int(round(math.sqrt(len(z))))
            fov = math.radians(cfg.get("fov_deg", 45.0))
            tall = 0
            for i, d in enumerate(z):
                if d < MIN_VALID_CM:
                    continue
                is_tall, h = _is_tall(d, floors[i] if floors else None, cars[i] if cars else None)
                if is_tall:
                    r, c = divmod(i, n)
                    ax, ay = ((c + 0.5) / n - 0.5) * fov, ((r + 0.5) / n - 0.5) * fov
                    pts.append((cfg["x_cm"] + d * math.tan(ax), cfg["y_cm"] + d * math.tan(ay),
                                d * math.tan(fov / n / 2), h))
                    tall += 1
            ok += 1
            multi = True
            coverage = max(coverage, tall / len(z))
            readings[cfg["id"]] = {"status": "ok", "tall_zones": tall, "zones": len(z)}
            continue
        if st.get("problem"):
            readings[cfg["id"]] = {"status": st["problem"]}
            continue
        d = st["median"]
        is_tall, h = _is_tall(d, cfg.get("floor_cm"), cfg.get("car_cm"))
        if is_tall is None:
            readings[cfg["id"]] = {"status": "needs calibration", "distance_cm": round(d, 1)}
            continue
        if is_tall:
            pts.append((cfg["x_cm"], cfg["y_cm"], d * beam, h))
        ok += 1
        readings[cfg["id"]] = {"status": "ok", "distance_cm": round(d, 1), "tall": is_tall,
                               "height_cm": None if h is None else round(h, 1)}
    point_sensors = sum(1 for r in readings.values() if r.get("status") == "ok" and "tall" in r)
    return pts, ok, multi or point_sensors >= 2, readings, coverage


def _object_width(pts) -> float:
    """Widest extent of the tall points, including each beam's footprint."""
    best = 0.0
    for i, (x1, y1, r1, _) in enumerate(pts):
        best = max(best, 2 * r1)
        for x2, y2, r2, _ in pts[i + 1:]:
            best = max(best, math.hypot(x2 - x1, y2 - y1) + r1 + r2)
    return best


def _evaluate_distance():
    now = time.time()
    with _dist_lock:
        pts, ok, measurable, readings, coverage = _tall_points()
        DIST_INFO.update(readings=readings, updated_at=_now_iso(), width_measurable=measurable if ok else None)
        if not ok:
            DIST_INFO.update(status="needs calibration" if any(r["status"] == "needs calibration" for r in readings.values())
                             else "no data", present=None, height_cm=None, width_cm=None)
            notify_change()
            return None
        heights = [p[3] for p in pts if p[3] is not None]
        height = max(heights) if heights else (None if pts else 0.0)
        width = _object_width(pts) if pts else 0.0
        if not pts:
            present = False
        elif measurable:
            # Tall AND wide = car; tall but narrow = person. A grid sensor's view is only
            # ~0.8 x distance wide, so for grids "fills most of the view" also counts as wide.
            present = width >= DIST["min_width_cm"] or coverage >= GRID_CAR_COVERAGE
        else:
            present = True                               # one sensor: height only (app warns)
        if present != DIST_INFO["present"]:
            DIST_INFO["since"] = now                     # candidate; must hold steady
        DIST_INFO.update(status="ok", present=present,
                         height_cm=(None if height is None else round(height, 1)) if pts else 0.0,
                         width_cm=round(width, 1) if pts else 0.0)
        commit = now - DIST_INFO["since"] >= DIST["stable_seconds"] and present != DIST_INFO["committed"]
        if commit:
            DIST_INFO["committed"] = present
    if commit:
        if present:
            set_car("present", "distance",
                    f"Object {_fmt_len(height)} tall and {_fmt_len(width)} wide under the sensors" if height
                    else f"Matches the parked car ({_fmt_len(width)} wide) under the sensors")
        elif pts:
            set_car("away", "distance", f"Only a narrow object ({_fmt_len(width)} wide), probably a person")
        else:
            set_car("away", "distance", "Sensors see the empty floor")
    else:
        notify_change()
    return present


def _fmt_len(cm: float) -> str:
    inches = cm / 2.54
    return f"{cm / 100:.2f} m ({int(inches // 12)} ft {int(round(inches % 12))} in)"


def calibrate_distance(mode: str = "empty"):
    """mode 'empty': remember the current readings as the empty floor.
    mode 'car'  : remember them as the parked car (no need to move it)."""
    if mode not in ("empty", "car"):
        raise RuntimeError("Calibration must be 'empty' or 'car'")
    point_key, grid_key = ("floor_cm", "floor_zones") if mode == "empty" else ("car_cm", "car_zones")
    done = []
    with _dist_lock:
        for cfg in DIST["sensors"]:
            st = _dist_state.get(cfg["id"])
            if not st or time.time() - st["at"] > STALE_SEC:
                continue
            if st.get("zones"):
                cfg[grid_key] = [round(v, 1) for v in st["zones"]]
                done.append(cfg["id"])
            elif st.get("median"):
                cfg[point_key] = round(st["median"], 1)
                done.append(cfg["id"])
            if mode == "car" and cfg.get("floor_cm") and cfg.get("car_cm") \
                    and cfg["floor_cm"] - cfg["car_cm"] < 20:
                cfg.pop("car_cm", None)
                raise RuntimeError(f"Sensor '{cfg['id']}' sees almost the same distance as the empty floor. "
                                   "Is the car really under it?")
        DIST_INFO.update(present=None, since=time.time(), committed=None)
    if not done:
        raise RuntimeError("No distance readings yet. Check the sensors are connected.")
    save_distance_settings()
    what = "the empty floor" if mode == "empty" else "the parked car"
    add_log("Car", f"Distance sensors calibrated on {what} ({', '.join(done)})", True)
    if mode == "car":
        set_car("present", "distance", "Calibrated with the car parked")
    return done


def save_distance_settings():
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(distance_sensor=json.loads(json.dumps(DIST)))


_dist_runner = {"thread": None, "stop": threading.Event(), "lock": threading.Lock()}
DIST_OPEN_RETRY_SEC = 30


def _open_hcsr04(cfg):
    from gpiozero import DistanceSensor
    return DistanceSensor(echo=int(cfg["echo_pin"]), trigger=int(cfg["trigger_pin"]),
                          max_distance=DIST["max_cm"] / 100.0, queue_len=1)


def gpio_holders(proc: str = "/proc"):
    """Processes holding GPIO line requests: {pid: {"cmd", "fds", "lines"}} plus whether some
    processes couldn't be inspected (e.g. started with sudo). Lines come from the kernel's
    fdinfo (Linux 6.7+); on older kernels they're unknown (empty set)."""
    holders, hidden = {}, False
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return holders, hidden
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            hidden = True
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if "gpio" not in link or not link.startswith("anon_inode:"):
                continue
            entry = holders.get(int(pid))
            if entry is None:
                try:
                    with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                        cmd = f.read().replace(bytes([0]), b" ").decode(errors="replace").strip()
                except OSError:
                    cmd = "?"
                entry = holders[int(pid)] = {"cmd": cmd, "fds": [], "lines": set()}
            entry["fds"].append(int(fd))
            try:
                with open(os.path.join(proc, pid, "fdinfo", fd)) as f:
                    for row in f:
                        if row.startswith("gpio-line:"):
                            entry["lines"].add(int(row.split(":", 1)[1].strip()))
            except (OSError, ValueError):
                pass
    return holders, hidden


def release_leftover_gpio():
    """GPIO handles this process holds before it has opened any sensor can only be leftovers
    from before a self-restart. Close them so the pins are free again."""
    holders, _ = gpio_holders()
    mine = holders.get(os.getpid())
    if not mine:
        return 0
    for fd in mine["fds"]:
        try:
            os.close(fd)
        except OSError:
            pass
    logger.warning(f"[GPIO] Released {len(mine['fds'])} GPIO handle(s) left over from before Halo restarted")
    return len(mine["fds"])


def describe_gpio_holder(pins) -> str:
    """Plain-English 'who has these pins' for the 'GPIO busy' message."""
    holders, hidden = gpio_holders()
    pins = set(pins)
    me = os.getpid()
    for pid, h in holders.items():
        if h["lines"] and not (h["lines"] & pins):
            continue                                    # holds other pins
        if pid == me:
            return ("they're still held by Halo itself from before it restarted. Restart Halo once: "
                    "sudo systemctl restart garage-door.service")
        if HALO_CMD_RE.search(h["cmd"] or ""):
            return f"another copy of Halo (PID {pid}) has them. Stop it with: sudo kill {pid}"
        return f"another program has them: PID {pid} ({(h['cmd'] or '?')[:80]})"
    if hidden:
        return ("a program Halo isn't allowed to inspect has them (probably started with sudo). "
                "Find it with: sudo gpioinfo | grep -E 'line +(17|22|23|27):'")
    return "another program has them (find it with: gpioinfo | grep -E 'line +(17|22|23|27):')"


def _pin_conflicts():
    """Same GPIO used twice in the config is a guaranteed 'GPIO busy'."""
    seen, clashes = {}, []
    for cfg in DIST["sensors"]:
        if cfg.get("driver", "hcsr04") != "hcsr04":
            continue
        for role in ("trigger_pin", "echo_pin"):
            pin = cfg.get(role)
            if pin in seen:
                clashes.append(f"GPIO {pin} is used by both '{seen[pin]}' and '{cfg['id']}'")
            seen[pin] = cfg["id"]
    return clashes


def _distance_reader(stop: threading.Event):
    """Reads HC-SR04 ultrasonic sensors on the Pi's GPIO (via gpiozero), one at a time so
    their pings don't interfere. Owns the GPIO pins while running and always releases them."""
    devices, failed, next_try = {}, {}, 0.0
    try:
        clashes = _pin_conflicts()
        if clashes:
            DIST_INFO.update(status="error", error="; ".join(clashes) + ". Give each sensor its own pins.")
            notify_change()
            return
        while not stop.is_set():
            # (Re)open any sensor that isn't open yet, e.g. after 'GPIO busy'
            if time.monotonic() >= next_try:
                for cfg in DIST["sensors"]:
                    if cfg.get("driver", "hcsr04") != "hcsr04" or cfg["id"] in devices:
                        continue
                    try:
                        devices[cfg["id"]] = _open_hcsr04(cfg)
                        failed.pop(cfg["id"], None)
                    except Exception as e:
                        why = str(e)
                        if "busy" in why.lower():
                            why = "those pins are in use: " + describe_gpio_holder(
                                (cfg.get("trigger_pin"), cfg.get("echo_pin")))
                        failed[cfg["id"]] = (f"Can't open sensor '{cfg['id']}' on GPIO {cfg.get('trigger_pin')}/"
                                             f"{cfg.get('echo_pin')}: {why}. Retrying every {DIST_OPEN_RETRY_SEC} s.")
                next_try = time.monotonic() + DIST_OPEN_RETRY_SEC
                DIST_INFO["error"] = " ".join(failed.values()) or None
                if failed and not devices:
                    DIST_INFO["status"] = "error"
                notify_change()
            for sid, dev in list(devices.items()):
                if stop.is_set():
                    break
                try:
                    ingest_distance(sid, dev.distance * 100.0)
                except Exception as e:
                    DIST_INFO.update(error=str(e)[:160])
                stop.wait(0.08)                              # let echoes die down before the next ping
            stop.wait(DIST["interval"])
    finally:
        for dev in devices.values():                         # always give the pins back
            try:
                dev.close()
            except Exception:
                pass


def start_distance_reader():
    """Start the GPIO reader if it isn't already running (safe to call repeatedly)."""
    with _dist_runner["lock"]:
        t = _dist_runner["thread"]
        if t and t.is_alive():
            return
        _dist_runner["stop"] = threading.Event()
        t = threading.Thread(target=_distance_reader, args=(_dist_runner["stop"],), daemon=True,
                             name="distance-reader")
        _dist_runner["thread"] = t
        t.start()


def stop_distance_reader(timeout: float = 3.0):
    """Stop the reader and wait for it to release the GPIO pins."""
    with _dist_runner["lock"]:
        t = _dist_runner["thread"]
        _dist_runner["stop"].set()
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout)


def distance_public() -> dict:
    return {**{k: DIST[k] for k in ("enabled", "min_height_cm", "min_width_cm", "stable_seconds")},
            "sensor_count": len(DIST["sensors"]),
            "calibrated": {"empty": any(x.get("floor_cm") or x.get("floor_zones") for x in DIST["sensors"]),
                           "car": any(x.get("car_cm") or x.get("car_zones") for x in DIST["sensors"])},
            **{k: DIST_INFO.get(k) for k in ("status", "present", "height_cm", "width_cm", "width_measurable",
                                             "readings", "error", "updated_at")}}


def set_car(state: str, source: str, reason: str):
    with state_lock:
        car = door_info["car"]
        changed = car["state"] != state
        car.update(state=state, source=source, reason=reason, updated_at=_now_iso())
        save_door_state()
    if changed:
        add_log("Car", f"{'In garage' if state == 'present' else 'Away' if state == 'away' else 'Unknown'}: {reason}", True)
    else:
        notify_change()


def _motion_between(a: float, b: float) -> bool:
    return any(a <= t <= b for t in _motion_times)


def estimate_car(opened: float, closed: float, now: float = None):
    """
    Decide car presence after one door cycle (open -> close).
      motion before opening, none after closing  -> someone drove OUT  -> away
      motion after closing, none before opening  -> someone drove IN   -> present
    Anything else (no motion, or both) means a person walked through: leave it.
    """
    if not SENSORS["car"] and not _motion_times:
        return None                                   # no motion sensor in use
    if DIST["enabled"] and DIST_INFO["status"] == "ok":
        return None                                   # the distance sensors know for sure
    end = min(now or time.time(), closed + CAR_POST_SEC)
    pre = _motion_between(opened - CAR_PRE_SEC, opened - 0.001)
    during = _motion_between(opened, closed)
    post = _motion_between(closed + CAR_POST_SKIP, end)
    if pre and not post:
        set_car("away", "estimate", "Motion before the door opened, none after it closed")
        return "away"
    if post and not pre:
        set_car("present", "estimate", "Motion after the door closed, none before it opened")
        return "present"
    logger.info(f"[Car] Cycle inconclusive (pre={pre} during={during} post={post}); unchanged")
    return None


def _car_watcher():
    """Watch door edges; evaluate each open->close cycle once the post-close window has passed."""
    last_version, prev_closed, opened_at, pending = None, True, None, None
    while True:
        with _change:
            _change.wait_for(lambda: _version != last_version, timeout=5)
            last_version = _version
        with state_lock:
            closed = door_info["state"] == "CLOSED"
        now = time.time()
        if prev_closed != closed and correction_edge(now):  # manual correction, not a real trip
            prev_closed, opened_at = closed, None
            continue
        if prev_closed and not closed:                      # door started opening
            if pending:                                     # reopened before window ended
                estimate_car(pending[0], pending[1], now)
                pending = None
            opened_at = now
        elif not prev_closed and closed and opened_at:      # door fully closed
            pending = (opened_at, now)
            opened_at = None
        prev_closed = closed
        if pending and now >= pending[1] + CAR_POST_SEC:
            estimate_car(*pending)
            pending = None


# Every Wyze sensor Halo has heard of: from state messages and the retained
# Home Assistant discovery configs wyzesense2mqtt publishes for each paired sensor.
SENSOR_INV = {}
_inv_lock = threading.Lock()
MAC_RE = re.compile(r"^[0-9A-F]{8}$")


def _kind_from_class(cls: str) -> str:
    cls = (cls or "").lower()
    if any(k in cls for k in ("motion", "occupancy", "presence")):
        return "motion"
    if any(k in cls for k in ("opening", "door", "window", "garage", "contact", "switch")):
        return "contact"
    return "unknown"


def inventory_update(mac: str, **fields):
    mac = _norm_mac(mac)
    if not mac or not MAC_RE.match(mac):
        return
    with _inv_lock:
        new = mac not in SENSOR_INV
        e = SENSOR_INV.setdefault(mac, {"mac": mac, "name": None, "kind": "unknown", "battery": None,
                                        "signal_strength": None, "last_seen": None, "found_at": time.time()})
        for k, v in fields.items():
            if v is None:
                continue
            if k == "kind" and v == "unknown":
                continue
            e[k] = v
    if new:
        add_log("Sensor", f"Found {fields.get('kind', 'new')} sensor {mac}", True)
    else:
        notify_change()


def inventory_remove(mac: str):
    with _inv_lock:
        SENSOR_INV.pop(_norm_mac(mac), None)
    notify_change()


def inventory_list():
    with _inv_lock:
        items = [dict(v) for v in SENSOR_INV.values()]
    for e in items:
        e["role"] = "door" if e["mac"] == SENSORS["door"] else "car" if e["mac"] == SENSORS["car"] else None
    return sorted(items, key=lambda e: (e["role"] is None, e["mac"]))


def ingest_discovery(topic: str, raw: str):
    """homeassistant/binary_sensor/wyzesense_<MAC>/state/config (v1-3) or
    homeassistant/device/ws2m_sensor_<MAC>/config (v4). Empty payload = sensor removed."""
    m = re.search(r"(?:wyzesense|ws2m_sensor)_([0-9A-Fa-f]{8})", topic)
    if not m:
        return
    mac = m.group(1).upper()
    if not raw:
        inventory_remove(mac)
        return
    try:
        cfg = json.loads(raw)
    except ValueError:
        return
    dev = cfg.get("dev") or cfg.get("device") or {}
    name = cfg.get("name") or dev.get("name")
    cls = cfg.get("dev_cla") or cfg.get("device_class") or ""
    for comp in (cfg.get("cmps") or cfg.get("components") or {}).values():
        cls = cls or comp.get("dev_cla") or comp.get("device_class") or ""
    kind = _kind_from_class(cls) if cls else _kind_from_class(str(dev.get("mdl") or dev.get("model") or ""))
    inventory_update(mac, name=name, kind=kind)


def route_sensor(data: dict, topic_mac: str, source: str):
    """Send a sensor message to the right handler by MAC (door contact vs. car motion)."""
    mac = _norm_mac(data.get("mac") or (topic_mac if topic_mac and MAC_RE.match(topic_mac.upper()) else None))
    if mac:
        data["mac"] = mac
    cls = str(data.get("device_class", data.get("type", ""))).lower()
    if mac and MAC_RE.match(mac):
        inventory_update(mac, kind=_kind_from_class(cls), battery=data.get("battery"),
                         signal_strength=data.get("signal_strength"), last_seen=_now_iso(),
                         name=data.get("name"))
    if SENSORS["car"] and mac == SENSORS["car"]:
        return "motion", ingest_motion(data, source)
    if not SENSORS["car"] and ("motion" in cls or "occupancy" in cls or "motion" in data or "occupancy" in data):
        return "motion", ingest_motion(data, source)
    if SENSORS["door"] and mac and mac != SENSORS["door"]:
        logger.debug(f"[Sensor] Ignoring unknown sensor {mac}")
        return None, None
    return "door", ingest_sensor(data, source)


def assign_sensor(mac: str, role: str):
    """Make a sensor the garage door contact ('door') or the car-bay motion sensor ('car'), or neither."""
    mac = _norm_mac(mac)
    if role not in ("door", "car", "none") or not mac or not MAC_RE.match(mac):
        raise ValueError("Send {'mac': '8 hex characters', 'role': 'door'|'car'|'none'}")
    with state_lock:
        for r in ("door", "car"):
            if SENSORS[r] == mac:
                SENSORS[r] = None                      # one role per sensor
        if role != "none":
            SENSORS[role] = mac
            info = door_info["sensor" if role == "door" else "motion"]
            info["mac"] = mac
            info["connected"] = False                  # wait for this sensor's first report
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(sensor_mac=SENSORS["door"], car_sensor_mac=SENSORS["car"])
    label = {"door": "garage door sensor", "car": "car motion sensor", "none": "unused"}[role]
    add_log("Sensor", f"{mac} is now the {label}", True)


PAIRING = {"until": 0.0}


def _ws_roots():
    """wyzesense2mqtt topic root(s), e.g. 'wyzesense2mqtt' from 'wyzesense2mqtt/#'."""
    cfg = CTX["remote_mgr"].config.get("mqtt", {}) if CTX["remote_mgr"] else {}
    return [str(cfg.get("topic", "wyzesense2mqtt/#")).split("/")[0] or "wyzesense2mqtt"]


def start_pairing(seconds: int = 60):
    client = _mqtt["client"]
    if not client or not _mqtt["connected"]:
        raise RuntimeError("Not connected to the MQTT broker, so the Wyze bridge can't be reached")
    for root in _ws_roots():
        client.publish(f"{root}/scan", "", qos=1)                         # wyzesense2mqtt 1.x-3.x
    for dmac in BRIDGE_INFO["dongles"]:
        client.publish(f"ws2m/dongle/{dmac}/scan", "scan", qos=1)         # wyzesense2mqtt 4.x
    PAIRING["until"] = time.time() + seconds
    add_log("Sensor", "Pairing mode: press the reset button on the new sensor", True)


def unpair_sensor(mac: str):
    mac = _norm_mac(mac)
    client = _mqtt["client"]
    if not client or not _mqtt["connected"]:
        raise RuntimeError("Not connected to the MQTT broker")
    for root in _ws_roots():
        client.publish(f"{root}/remove", mac, qos=1)
    for dmac in BRIDGE_INFO["dongles"]:
        client.publish(f"ws2m/dongle/{dmac}/remove", mac, qos=1)
    for r in ("door", "car"):
        if SENSORS[r] == mac:
            assign_sensor(mac, "none")
    inventory_remove(mac)
    add_log("Sensor", f"Removed sensor {mac}", True)


# ── Wyze bridge watchdog ───────────────────────────────────────────────────────
# wyzesense2mqtt 1.x has no online/offline signal, so Halo checks the two things
# that actually break: is the USB bridge plugged in, and is the container running?
# If the bridge is replugged or the container stops, Halo restarts it. Sensors
# stay paired inside the bridge itself, so they come back without re-pairing.
BRIDGE = {"watchdog": True, "container": "wyzesense2mqtt", "usb_id": "1a86:e024",
          "sysfs": "/sys/bus/usb/devices", "restart_command": None, "status_command": None}
BRIDGE_INFO = {"status": "unknown", "usb": None, "service": None, "dongles": {}, "restarts": 0,
               "last_restart": None, "error": None, "next_retry": None}
_BACKOFF = (30, 60, 120, 300, 900)


def _usb_bridge_present():
    base = BRIDGE["sysfs"]
    if not base or not os.path.isdir(base):
        return None                                    # not Linux / not visible: unknown
    vid, pid = BRIDGE["usb_id"].lower().split(":")
    for d in os.listdir(base):
        try:
            with open(os.path.join(base, d, "idVendor")) as f:
                if f.read().strip().lower() != vid:
                    continue
            with open(os.path.join(base, d, "idProduct")) as f:
                if f.read().strip().lower() == pid:
                    return True
        except OSError:
            continue
    return False


def _run(cmd, timeout):
    if isinstance(cmd, str):
        cmd = cmd.split()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _bridge_service_running():
    cmd = BRIDGE["status_command"]
    if not cmd:
        if not shutil.which("docker"):
            return None
        cmd = ["docker", "inspect", "-f", "{{.State.Running}}", BRIDGE["container"]]
    try:
        code, out, err = _run(cmd, 10)
    except (OSError, subprocess.SubprocessError) as e:
        BRIDGE_INFO["error"] = str(e)
        return None
    if code != 0:
        if "permission denied" in err.lower():
            BRIDGE_INFO["error"] = "Halo can't use Docker. Add its user to the docker group (see README)."
            return None
        if "no such" in err.lower():
            return None                                # bridge container not installed here
        return False
    return out.lower() in ("true", "running", "active", "1")


def restart_bridge(reason: str) -> bool:
    cmd = BRIDGE["restart_command"] or ["docker", "restart", BRIDGE["container"]]
    BRIDGE_INFO["status"] = "restarting"
    notify_change()
    try:
        code, out, err = _run(cmd, 90)
    except (OSError, subprocess.SubprocessError) as e:
        code, err = 1, str(e)
    BRIDGE_INFO["last_restart"] = _now_iso()
    BRIDGE_INFO["restarts"] += 1
    if code == 0:
        BRIDGE_INFO["error"] = None
        add_log("Wyze Bridge", f"Restarted ({reason})", True)
        return True
    BRIDGE_INFO["error"] = ("Halo can't use Docker. Add its user to the docker group (see README)."
                            if "permission denied" in err.lower() else f"Restart failed: {err[:120]}")
    add_log("Wyze Bridge", f"Restart failed ({reason}): {err[:120]}", False)
    return False


def _bridge_watchdog():
    prev_usb, last_restart_at, fails, healthy_since = None, 0.0, 0, None
    while True:
        usb = _usb_bridge_present()
        running = _bridge_service_running()
        dongle_down = any(v == "offline" for v in BRIDGE_INFO["dongles"].values())
        now = time.time()
        BRIDGE_INFO.update(usb=usb, service=None if running is None else ("running" if running else "stopped"))

        reason = None
        if prev_usb is False and usb:
            time.sleep(3)                              # let the USB device settle
            reason = "bridge plugged back in"
        elif usb is not False and running is False:
            reason = "bridge service stopped"
        elif usb and dongle_down:
            reason = "bridge reported offline"

        if usb is False:
            status = "unplugged"
            if prev_usb is not False:
                add_log("Wyze Bridge", "USB bridge unplugged. Door sensor offline until it's back", False)
        elif reason:
            status = "down"
        elif usb is None and running is None:
            status = "unknown"
        else:
            status = "ok"
        if status != BRIDGE_INFO["status"] and BRIDGE_INFO["status"] != "restarting":
            BRIDGE_INFO["status"] = status
            notify_change()

        wait_needed = _BACKOFF[min(fails, len(_BACKOFF) - 1)] if fails else 0
        plugged_in = reason == "bridge plugged back in"
        retry_at = last_restart_at + wait_needed if reason and BRIDGE["watchdog"] else None
        if retry_at != BRIDGE_INFO["next_retry"]:
            BRIDGE_INFO["next_retry"] = retry_at if retry_at and retry_at > now else None
        if reason and BRIDGE["watchdog"] and (plugged_in and now - last_restart_at > 20
                                              or now - last_restart_at >= wait_needed):
            ok = restart_bridge(reason)
            last_restart_at, fails = time.time(), (0 if ok and plugged_in else fails + 1)
            BRIDGE_INFO["status"] = "ok" if ok else "down"
            healthy_since = None
            notify_change()
        elif status == "ok":
            healthy_since = healthy_since or now
            if now - healthy_since > 300:
                fails = 0
        prev_usb = usb
        time.sleep(BRIDGE.get("interval", 15))


# ── Auto-close ─────────────────────────────────────────────────────────────────
# Closes the door after it has been open N minutes. Safety rules:
#   * Only when the contact sensor confirms the door is OPEN. The remote is a
#     toggle, so acting on a guess could OPEN a closed door.
#   * A warning goes out first (notification + app) with "Keep open".
#   * Postponed while there's motion in the garage (car motion sensor).
#   * One attempt per opening. If the door doesn't close (something in the
#     way), you get an alert and Halo does not try again.
AUTO_CLOSE = {"enabled": False, "minutes": 10, "warn_seconds": 60, "motion_hold_seconds": 120}
AC = {"deadline": None, "held": False, "blocked": None, "warned": False, "attempted": False}


def configure_auto_close(**changes) -> dict:
    if "enabled" in changes and isinstance(changes["enabled"], bool):
        AUTO_CLOSE["enabled"] = changes["enabled"]
    if "minutes" in changes:
        AUTO_CLOSE["minutes"] = max(0.05, min(240.0, float(changes["minutes"])))
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(auto_close=dict(AUTO_CLOSE))
    if not AUTO_CLOSE["enabled"]:
        AC.update(deadline=None, warned=False)
    add_log("Auto-Close", f"On: close after {AUTO_CLOSE['minutes']:g} min" if AUTO_CLOSE["enabled"] else "Off", True)
    return auto_close_public()


def hold_auto_close():
    AC.update(held=True, deadline=None, warned=False)
    add_log("Auto-Close", "Keeping the door open until it next closes", True)


def auto_close_public() -> dict:
    with state_lock:
        sensor_ok = bool(SENSORS["door"] or door_info["sensor"]["connected"])
    return {**AUTO_CLOSE, "deadline": AC["deadline"], "held": AC["held"], "blocked": AC["blocked"],
            "needs_sensor": not sensor_ok, "now": time.time()}


def _auto_close_worker():
    last_version, prev_closed, opened_at, seen_closes = None, None, None, _closed_count
    while True:
        wait = 30.0
        if AC["deadline"]:
            wait = max(0.2, min(wait, AC["deadline"] - AUTO_CLOSE["warn_seconds"] - time.time()
                                if not AC["warned"] else AC["deadline"] - time.time()))
        with _change:
            _change.wait_for(lambda: _version != last_version, timeout=wait)
            last_version = _version
        with state_lock:
            state = door_info["state"]
            sensor = dict(door_info["sensor"])
            closes = _closed_count
        closed = state == "CLOSED"
        now = time.time()
        if closes != seen_closes:                    # it closed since we last looked (maybe briefly)
            seen_closes = closes
            prev_closed = True
        if prev_closed is None or (prev_closed and not closed):          # door opened (or startup)
            opened_at = None if closed else now
            AC.update(held=False, warned=False, attempted=False, blocked=None)
        if closed:
            AC.update(deadline=None, held=False, warned=False, blocked=None)
            opened_at = None
        prev_closed = closed
        if closed or not AUTO_CLOSE["enabled"] or AC["held"] or AC["attempted"] or not opened_at:
            if not AUTO_CLOSE["enabled"]:
                AC["deadline"] = None
            continue
        if AC["deadline"] is None:
            AC["deadline"] = opened_at + AUTO_CLOSE["minutes"] * 60
            notify_change()

        # Warning shortly before closing
        if not AC["warned"] and now >= AC["deadline"] - AUTO_CLOSE["warn_seconds"]:
            AC["warned"] = True
            secs = int(AUTO_CLOSE["warn_seconds"])
            send_notification("Garage Door Closing Soon",
                              f"The garage door will close automatically in {secs} seconds. "
                              "Select Keep Open to leave it open.",
                              priority="high", keep_open_action=True)
            notify_change()
        if now < AC["deadline"]:
            continue

        # Safety checks
        blocked = None
        if not (sensor["connected"] and sensor["state"] == "open"):
            blocked = "Needs the door contact sensor to confirm the door is open"
        elif state != "OPEN":
            blocked = "Door is moving"
        elif _motion_between(now - AUTO_CLOSE["motion_hold_seconds"], now):
            blocked = "Motion in the garage, trying again shortly"
        if blocked:
            if blocked != AC["blocked"]:
                AC["blocked"] = blocked
                add_log("Auto-Close", f"Waiting: {blocked}", True)
            AC["deadline"] = now + 60                   # check again in a minute
            AC["warned"] = True
            notify_change()
            continue

        AC.update(attempted=True, deadline=None, blocked=None)
        # Send on its own thread: the radio burst takes ~2 s and this watcher must
        # keep seeing door changes meanwhile.
        threading.Thread(target=_send_auto_close, daemon=True).start()


def _send_auto_close():
    try:
        press_door(source="Auto-close", allow_locked=True)
    except Exception as e:
        add_log("Auto-Close", f"Couldn't send close: {e}", False)
        send_notification("Auto-Close Failed",
                          f"Halo could not send the close command. Reason: {e}. The garage door is still open.",
                          close_action=True)
        return
    _verify_auto_close()


def _verify_auto_close():
    with state_lock:
        travel = door_info.get("travel_time", 12.0)
    time.sleep(travel + 20)
    with state_lock:
        closed = door_info["state"] == "CLOSED"
        sensor_open = door_info["sensor"]["state"] == "open"
    if not closed or sensor_open:
        add_log("Auto-Close", "Door did not close (something in the way?). Not retrying", False)
        send_notification("Garage Door Did Not Close",
                          "Halo sent an automatic close command, but the garage door is still open. "
                          "Check the doorway for an obstruction. Auto-close will not try again until the door "
                          "is next opened.",
                          priority="urgent")


# ── Actions shared by HTTP and MQTT ────────────────────────────────────────────
CTX = {"remote_mgr": None, "radio": None, "tx_mode": "fifo", "gdo0": 24}


class LockedError(Exception):
    """Door command refused because the remote lock is on."""


def transmit(rolling: int, fixed: int, repeats: int, delay: float = 0.002):
    """Send one Security+ 1.0 press (N bursts) in the configured TX mode."""
    radio = CTX["radio"]
    with radio_lock:
        if not radio.is_mock and radio.verified is False:
            radio.verify()                              # maybe it was just fixed (wiring, power)
            if not radio.verified:
                notify_change()
                raise RadioError(radio.problem or "The CC1101 isn't working")
        if CTX["tx_mode"] == "async":
            bits = generate_burst_bits(rolling, fixed)
            radio.transmit_security_plus_async([bits] * repeats, gdo0_gpio=CTX["gdo0"], inter_burst_delay=delay)
        else:
            burst = generate_burst_bytes(rolling, fixed)
            try:
                radio.transmit_security_plus([burst] * repeats, inter_burst_delay=delay)
            except RadioError as e:
                radio.verified, radio.problem = False, str(e)
                notify_change()
                raise
        refresh_hw_state(radio)


def default_repeats(value=None, default=None) -> int:
    r = value or default or CTX["remote_mgr"].config.get("repeats_per_press", 12)
    return max(1, min(32, int(r)))


_last_door_cmd = {"source": None, "at": 0.0}


def press_door(repeats=None, source: str = None, allow_locked: bool = False) -> dict:
    """One door-button press (Security+ 1.0 is a toggle). Raises LockedError."""
    with state_lock:
        if door_info["locked"] and not allow_locked:
            raise LockedError("Remote lock is on")
    mgr = CTX["remote_mgr"]
    rolling, fixed = mgr.get_next_codes()
    # Every screen (Halo app, Apple Home) sees the door start moving now, not
    # after the ~1-2 s radio burst. If the radio fails, the change is undone.
    with state_lock:
        before = (door_info["state"], door_info["percent"])
    _last_door_cmd.update(source=source or "Halo", at=time.time())
    new_state = start_door_travel()
    try:
        transmit(rolling, fixed, default_repeats(repeats))
    except Exception:
        global _travel_id
        with state_lock:
            _travel_id += 1                                 # stop the simulated travel
            door_info["state"], door_info["percent"] = before
            save_door_state()
        notify_change()
        raise
    new_rolling, _ = mgr.peek_codes()
    add_log("Door", f"Sent open/close from {source or 'Halo'} (code {rolling}, next {new_rolling})", True)
    return {"door_state": new_state, "door_percent": door_info["percent"],
            "sent_rolling_code": rolling, "next_rolling_code": new_rolling, "fixed_code": fixed}


def command_door(cmd: str, repeats=None, source: str = None) -> dict:
    """open / close / toggle. Only transmits when the door is not already heading there."""
    cmd = cmd.strip().lower()
    if cmd not in ("open", "up", "close", "closed", "down", "toggle"):
        raise ValueError(f"Unknown door command '{cmd}'")
    with state_lock:
        st = door_info["state"]
    if cmd in ("open", "up") and st in ("OPEN", "OPENING"):
        return {"door_state": st, "skipped": True}
    if cmd in ("close", "closed", "down") and st in ("CLOSED", "CLOSING"):
        return {"door_state": st, "skipped": True}
    return press_door(repeats, source)


def press_light(repeats=None, source: str = None) -> dict:
    """
    Press the opener's light button (remote button 1). It's a toggle and Halo has
    no light sensor, so the result is only an estimate ("possibly on").
    """
    mgr = CTX["remote_mgr"]
    rolling, fixed = mgr.get_button_codes(button=1)
    likely_on = not door_info["light_on"]
    set_light(likely_on)                                    # every screen updates now
    try:
        transmit(rolling, fixed, default_repeats(repeats))
    except Exception:
        set_light(not likely_on)                            # radio failed: undo
        raise
    add_log("Light", f"Toggled from {source or 'Halo'} (now possibly {'on' if likely_on else 'off'})", True)
    new_rolling, _ = mgr.peek_codes()
    return {"light_on": likely_on, "estimated": True,
            "auto_off_remaining_sec": LIGHT_AUTO_OFF_SEC if likely_on else 0,
            "sent_rolling_code": rolling, "next_rolling_code": new_rolling, "fixed_code": fixed}


def command_light(cmd: str = "toggle", repeats=None, source: str = None) -> dict:
    """Every light command is a button press. on/off are accepted for compatibility but
    can't be honoured exactly, since Halo can't see the light."""
    return press_light(repeats, source)


def set_lock(locked=None) -> bool:
    with state_lock:
        door_info["locked"] = bool(locked) if isinstance(locked, bool) else not door_info["locked"]
        locked = door_info["locked"]
        save_door_state()
    add_log("Remote Lock", "Locked: door commands blocked" if locked else "Unlocked", True)
    return locked


# ── Local MQTT bridge ──────────────────────────────────────────────────────────
# Everything stays on the LAN broker: Wyze sensor ingest, retained state for
# Homebridge (mqttthing) / Home Assistant, and command topics. Reconnects every
# 1-5 s, so Halo recovers as soon as the broker is back, with or without internet.
_mqtt = {"client": None, "base": "halo/garage", "connected": False}


def _mqtt_publish_state():
    client, base = _mqtt["client"], _mqtt["base"]
    if not client or not _mqtt["connected"]:
        return
    with state_lock:
        d = door_info
        snap = {"door": d["state"].lower(), "percent": d["percent"], "light": "on" if d["light_on"] else "off",
                "lock": "on" if d["locked"] else "off", "car": d["car"]["state"],
                "motion": "on" if d["motion"]["active"] else "off", "updated_at": d["updated_at"]}
    client.publish(f"{base}/state", json.dumps(snap), qos=1, retain=True)
    client.publish(f"{base}/door", snap["door"], qos=1, retain=True)
    client.publish(f"{base}/light", snap["light"], qos=1, retain=True)
    client.publish(f"{base}/lock", snap["lock"], qos=1, retain=True)
    client.publish(f"{base}/car", snap["car"], qos=1, retain=True)
    client.publish(f"{base}/motion", snap["motion"], qos=1, retain=True)


def _mqtt_state_publisher():
    """Publish on meaningful changes only (not every travel tick), plus a 60 s refresh."""
    last_key, last_version, last_sent = None, None, 0.0
    while True:
        with _change:
            _change.wait_for(lambda: _version != last_version, timeout=60)
            last_version = _version
        with state_lock:
            key = (door_info["state"], door_info["light_on"], door_info["locked"],
                   door_info["car"]["state"], door_info["motion"]["active"])
        if key != last_key or time.time() - last_sent > 60:
            try:
                _mqtt_publish_state()
                last_key, last_sent = key, time.time()
            except Exception as e:
                logger.debug(f"[MQTT] publish failed: {e}")


def _mqtt_command(kind: str, payload: str):
    try:
        if kind == "door":
            r = command_door(payload, source="MQTT")
        elif kind == "light":
            r = command_light(payload, source="MQTT")
        elif kind == "car":
            v = payload.strip().lower()
            if v in ("present", "home", "in", "on", "true", "1"):
                set_car("present", "manual", "Set over MQTT")
            elif v in ("away", "out", "off", "false", "0"):
                set_car("away", "manual", "Set over MQTT")
            return
        elif kind == "lock":
            v = payload.strip().lower()
            set_lock(True if v in ("on", "lock", "locked", "true", "1") else
                     False if v in ("off", "unlock", "unlocked", "false", "0") else None)
            return
        else:
            return
        if r.get("skipped"):
            logger.info(f"[MQTT] {kind} '{payload}' ignored: already there")
    except LockedError:
        add_log("Door", "Blocked: remote lock is on (MQTT)", success=False)
    except Exception as e:
        add_log(kind.capitalize(), f"MQTT command failed: {e}", success=False)


def start_mqtt_client(broker_host="127.0.0.1", broker_port=1883, topic="wyzesense2mqtt/#",
                      base_topic="halo/garage", username=None, password=None):
    """Connect to the local broker. Works with paho-mqtt v1 and v2."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.info("[MQTT] paho-mqtt not installed (optional: sudo apt install python3-paho-mqtt)")
        return
    base = base_topic.rstrip("/")
    avail = base.split("/")[0] + "/status"
    _mqtt["base"] = base
    sensor_topics = (topic, "ws2m/sensor/#", "wyzesense/#", "home/garage/#")

    def on_connect(client, *args, **kwargs):
        rc = args[2] if len(args) > 2 else 0
        if rc != 0:
            logger.warning(f"[MQTT] Broker refused connection (rc={rc})")
            return
        _mqtt["connected"] = True
        logger.info(f"[MQTT] Connected to {broker_host}:{broker_port}")
        for t in sensor_topics:
            client.subscribe(t)
        client.subscribe(f"{base}/+/set", qos=1)
        client.subscribe("homeassistant/binary_sensor/+/state/config")   # wyzesense2mqtt 1.x-3.x
        client.subscribe("homeassistant/device/+/config")                # wyzesense2mqtt 4.x
        client.subscribe("ws2m/dongle/+/status")
        client.publish(avail, "online", qos=1, retain=True)
        _mqtt_publish_state()

    def on_disconnect(client, *args, **kwargs):
        if _mqtt["connected"]:
            logger.warning("[MQTT] Lost broker; retrying every 1-5 s")
        _mqtt["connected"] = False

    def on_message(client, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8").strip()
            if msg.topic.startswith(base + "/") and msg.topic.endswith("/set"):
                if not msg.retain:   # never replay a stale retained command
                    kind = msg.topic[len(base) + 1:-len("/set")]
                    threading.Thread(target=_mqtt_command, args=(kind, raw), daemon=True).start()
                return
            if msg.topic.startswith("homeassistant/"):
                return ingest_discovery(msg.topic, raw)
            if msg.topic.startswith("ws2m/dongle/") and msg.topic.endswith("/status"):
                BRIDGE_INFO["dongles"][msg.topic.split("/")[2]] = raw.lower()
                return notify_change()
            if msg.topic.rsplit("/", 1)[-1] in ("scan", "remove", "reload"):
                return                                   # our own pairing commands echoed back
            try:
                data = json.loads(raw)
            except ValueError:
                try:
                    import ast
                    data = ast.literal_eval(raw)      # some bridges publish Python reprs
                except (ValueError, SyntaxError):
                    data = {"state": raw}
            if not isinstance(data, dict):
                data = {"state": data}
            route_sensor(data, msg.topic.rsplit("/", 1)[-1], source="MQTT")
        except Exception as e:
            logger.error(f"[MQTT] Error processing message: {e}")

    try:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="halo_garage")
        except AttributeError:
            client = mqtt.Client(client_id="halo_garage")
        if username:
            client.username_pw_set(username, password)
        client.will_set(avail, "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.connect_async(broker_host, broker_port, keepalive=15)
        client.loop_start()
        _mqtt["client"] = client
        threading.Thread(target=_mqtt_state_publisher, daemon=True).start()
    except Exception as e:
        logger.warning(f"[MQTT] Could not start client: {e}")


# ── Notifications (garage door only) ───────────────────────────────────────────
# Sent through ntfy (https://ntfy.sh or your own ntfy server): install the free
# ntfy app, subscribe to the topic shown in Halo, and you get door alerts on your
# phone. Apple Home can't do this without a home hub. Sending happens on a
# background thread with retries, so a slow or missing internet connection never
# delays the door.
# Notification icon for ntfy on Android (iPhone always shows the ntfy app's icon).
# Must be a public address so it also works away from home: the Halo icon in the
# project's public GitHub repository.
DEFAULT_ICON_URL = "https://raw.githubusercontent.com/Izem07/Garage-Door-Opener-Cloner/main/web/icon-192.png"
NOTIFY = {"enabled": False, "server": "https://ntfy.sh", "topic": None,
          "left_open_minutes": 10, "app_url": None,
          "on_open": True, "on_close": True, "icon_url": DEFAULT_ICON_URL}
_notify_lock = threading.Lock()


def notify_public() -> dict:
    with _notify_lock:
        n = dict(NOTIFY)
    n["subscribe_url"] = f"{n['server'].rstrip('/')}/{n['topic']}" if n["topic"] else None
    n["status"] = dict(NOTIFY_STATUS)
    n["push"] = push_public()
    n["ntfy"] = n.get("ntfy", True)
    return n


def configure_notifications(**changes) -> dict:
    with _notify_lock:
        for k in ("enabled", "on_open", "on_close", "ntfy"):
            if k in changes and isinstance(changes[k], bool):
                NOTIFY[k] = changes[k]
        if "left_open_minutes" in changes:
            NOTIFY["left_open_minutes"] = max(0.0, min(240.0, float(changes["left_open_minutes"] or 0)))
        if changes.get("server"):
            srv = str(changes["server"]).strip().rstrip("/")
            if not srv.startswith(("http://", "https://")):
                raise ValueError("Server must start with http:// or https://")
            NOTIFY["server"] = srv
        if changes.get("topic"):
            t = str(changes["topic"]).strip()
            if not t.replace("-", "").replace("_", "").isalnum() or len(t) > 64:
                raise ValueError("Topic may only use letters, numbers, - and _ (max 64)")
            NOTIFY["topic"] = t
        if changes.get("app_url"):
            u = str(changes["app_url"]).strip().rstrip("/")
            if u.startswith(("http://", "https://")):
                NOTIFY["app_url"] = u
        if NOTIFY["enabled"] and not NOTIFY["topic"]:
            NOTIFY["topic"] = "halo-" + secrets.token_hex(8)   # hard to guess = private
        saved = dict(NOTIFY)
    if CTX["remote_mgr"]:
        CTX["remote_mgr"].update(notifications=saved)
    return notify_public()


NOTIFY_STATUS = {"last_ok": None, "last_error": None, "last_error_at": None, "last_title": None}
_tls = {"clean": None}


def _clean_tls_context():
    """Trust store without expired root certificates.

    Some systems (seen on Windows; possible on an old Pi image) carry an expired
    cross-signed 'ISRG Root X2'. OpenSSL picks it over the valid chain ntfy.sh
    sends and refuses the connection. Dropping expired roots changes nothing
    else: hostname checks and all other verification stay on.
    """
    if _tls["clean"] is None:
        base = ssl.create_default_context()
        now = time.time()
        pems = [ssl.DER_cert_to_PEM_cert(der) for der, info in zip(base.get_ca_certs(True), base.get_ca_certs())
                if ssl.cert_time_to_seconds(info["notAfter"]) > now]
        _tls["clean"] = ssl.create_default_context(cadata="".join(pems)) if len(pems) >= 10 else False
        if _tls["clean"] is False:
            try:
                import certifi                          # optional: pip install certifi
                _tls["clean"] = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                pass
    return _tls["clean"] or None


def _explain(err: Exception) -> str:
    msg = str(getattr(err, "reason", err))
    if isinstance(err, urllib.error.HTTPError):
        body = ""
        try:
            body = err.read().decode()[:160]
        except Exception:
            pass
        return f"ntfy rejected the alert (HTTP {err.code}) {body}".strip()
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        if time.gmtime().tm_year < 2024:
            return "This device's clock is wrong, so secure connections fail. Check the Pi's time (timedatectl)."
        return "Secure connection to the alert server failed (certificate problem). See the README, Door Alerts, Troubleshooting."
    if any(k in msg.lower() for k in ("name or service", "getaddrinfo", "nodename", "temporary failure in name")):
        return "No internet / DNS: couldn't find the alert server"
    if "refused" in msg.lower() or "unreachable" in msg.lower():
        return "Couldn't connect to the alert server (is the address right, and is it running?)"
    if "timed out" in msg.lower():
        return "The alert server didn't answer (timed out)"
    return msg[:160]


def _post_bytes(url: str, data: bytes, headers: dict):
    """POST once. Returns (http_status or None, error text or None)."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for ctx in (None, "clean"):
        try:
            c = _clean_tls_context() if ctx == "clean" else None
            if ctx == "clean" and c is None:
                break
            with urllib.request.urlopen(req, timeout=8, context=c) as r:
                return r.status, (None if r.status < 300 else f"HTTP {r.status}")
        except urllib.error.HTTPError as e:
            return e.code, _explain(e)
        except Exception as e:
            if ctx is None and "CERTIFICATE_VERIFY_FAILED" in str(getattr(e, "reason", e)) \
                    and time.gmtime().tm_year >= 2024:
                continue                                # retry with expired roots removed
            return None, _explain(e)
    return None, "Secure connection to the alert server failed (certificate problem)"


def _deliver(url: str, data: bytes, headers: dict):
    """One ntfy delivery attempt. Returns None on success or an error message."""
    return _post_bytes(url, data, headers)[1]


# ── Halo notifications (Web Push) ──────────────────────────────────────────────
# Alerts that come from the Halo app itself, with the Halo icon: iPhone (Halo on
# the Home Screen, opened over HTTPS), Android and desktop browsers. Sent next to
# ntfy, not instead of it. See webpush.py for the encryption.
PUSH_KEY_FILE = os.path.join(BASE_DIR, "halo_vapid.pem")
PUSH_SUBS_FILE = os.path.join(BASE_DIR, "push_subscriptions.json")
PUSH = {"vapid": None, "error": None, "subs": [], "subject": "mailto:halo@example.com"}
_push_lock = threading.Lock()


def init_push():
    try:
        import webpush
        PUSH["vapid"] = webpush.Vapid(PUSH_KEY_FILE)
        PUSH["error"] = None
    except ImportError:
        PUSH["error"] = ("Halo notifications need the 'cryptography' package. On the Pi run: "
                         "sudo apt install python3-cryptography")
    except Exception as e:
        PUSH["error"] = f"Couldn't set up Halo notifications: {e}"
    try:
        with open(PUSH_SUBS_FILE, encoding="utf-8") as f:
            PUSH["subs"] = [x for x in json.load(f) if isinstance(x, dict) and x.get("endpoint")]
    except (OSError, ValueError):
        PUSH["subs"] = []


def _save_push_subs():
    tmp = PUSH_SUBS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(PUSH["subs"], f, indent=2)
    os.replace(tmp, PUSH_SUBS_FILE)


def add_push_subscription(sub: dict, device: str):
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    u = urlparse(endpoint)
    local_test = u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or local_test) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Not a valid push subscription")
    with _push_lock:
        PUSH["subs"] = [x for x in PUSH["subs"] if x["endpoint"] != endpoint]
        PUSH["subs"].append({"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                             "device": (device or "Device")[:40], "added": _now_iso()})
        _save_push_subs()
    add_log("Notifications", f"Halo notifications turned on for {device or 'a device'}", True)


def remove_push_subscription(endpoint: str, reason: str = None):
    with _push_lock:
        before = len(PUSH["subs"])
        gone = [x for x in PUSH["subs"] if x["endpoint"] == endpoint]
        PUSH["subs"] = [x for x in PUSH["subs"] if x["endpoint"] != endpoint]
        if len(PUSH["subs"]) != before:
            _save_push_subs()
    if gone:
        add_log("Notifications", f"Halo notifications removed for {gone[0]['device']}"
                + (f" ({reason})" if reason else ""), True)
    notify_change()


def push_public() -> dict:
    with _push_lock:
        devices = [{"device": x["device"], "added": x["added"]} for x in PUSH["subs"]]
    return {"available": PUSH["vapid"] is not None, "error": PUSH["error"],
            "public_key": PUSH["vapid"].public_key if PUSH["vapid"] else None, "devices": devices}


def _send_push_one(sub: dict, payload: dict, urgency: str):
    """Returns None on success or an error text. Expired subscriptions are removed."""
    import webpush
    try:
        url, body, headers = webpush.build_request(sub, payload, PUSH["vapid"], PUSH["subject"], urgency=urgency)
    except Exception as e:
        return f"Couldn't encrypt: {e}"
    status, err = _post_bytes(url, body, headers)
    if status in (404, 410):                            # the phone unsubscribed or the app was removed
        remove_push_subscription(sub["endpoint"], "no longer subscribed")
        return "Device is no longer subscribed"
    return err


def send_notification(title: str, message: str, priority: str = "default",
                      tags: str = None, close_action: bool = False, force: bool = False,
                      keep_open_action: bool = False, wait: bool = False):
    """Push an alert to ntfy.

    wait=False: background thread, retried for about a minute if the internet is
    down, so it never delays the door. wait=True: send once now and return the
    error text (or None), used by "Send Test" so the app shows the real result.
    """
    with _notify_lock:
        n = dict(NOTIFY)
    if not (n["enabled"] or force):
        return "Door alerts are off"
    with _push_lock:
        push_subs = list(PUSH["subs"]) if PUSH["vapid"] else []
    use_ntfy = bool(n["topic"]) and n.get("ntfy", True)
    if not use_ntfy and not push_subs:
        return "No way to deliver alerts yet: subscribe in ntfy, or turn on Halo notifications on a device"
    # Plain text only: no Tags (ntfy shows them as emoji), no Markdown, and no
    # Priority header (ntfy marks high/urgent alerts with icons). ASCII only.
    title = title.encode("ascii", "replace").decode()
    message = message.encode("ascii", "replace").decode()
    headers = {"Title": title, "User-Agent": "Halo/2.0"}
    if n.get("icon_url"):
        headers["Icon"] = n["icon_url"]                 # Halo icon on Android
    if n["app_url"]:
        headers["Click"] = n["app_url"]
        if close_action:
            headers["Actions"] = f"http, Close Door, {n['app_url']}/api/close?source=Notification, method=POST, clear=true"
        if keep_open_action:
            headers["Actions"] = f"http, Keep Open, {n['app_url']}/api/auto-close/hold, method=POST, clear=true"
    url = f"{n['server']}/{n['topic']}"
    data = message.encode("utf-8")
    payload = {"title": title, "body": message, "url": "/", "tag": "halo-door", "actions": []}
    if close_action:
        payload["actions"].append({"action": "close", "title": "Close Door", "url": "/api/close?source=Notification"})
    if keep_open_action:
        payload["actions"].append({"action": "keep", "title": "Keep Open", "url": "/api/auto-close/hold"})
    urgency = "high" if (close_action or keep_open_action) else "normal"

    def send_all():
        """Every channel once. Returns (results, first error or None if anything got through)."""
        results = {}
        if use_ntfy:
            results["ntfy"] = _deliver(url, data, headers)
        for sub in push_subs:
            results[sub.get("device", "Device")] = _send_push_one(sub, payload, urgency)
        ok = any(v is None for v in results.values())
        return results, (None if ok else next(iter(results.values()), "No channel"))

    def record(err):
        if err is None:
            NOTIFY_STATUS.update(last_ok=_now_iso(), last_title=title, last_error=None)
        else:
            NOTIFY_STATUS.update(last_error=err, last_error_at=_now_iso(), last_title=title)
        notify_change()

    if wait:
        results, err = send_all()
        NOTIFY_STATUS["last_results"] = results
        record(err)
        if err:
            logger.warning(f"[Notify] {title}: {err}")
        return err

    def worker():
        err = None
        for delay in (0, 5, 15, 40):
            time.sleep(delay)
            results, err = send_all()
            NOTIFY_STATUS["last_results"] = results
            if err is None or "rejected" in err:       # delivered somewhere, or a permanent error
                break
            logger.info(f"[Notify] {title}: {err}; retrying")
        record(err)
        if err:
            logger.warning(f"[Notify] Gave up on '{title}': {err}")
            add_log("Notifications", f"Couldn't send '{title}': {err}", False)

    threading.Thread(target=worker, daemon=True).start()
    return None


def _fmt_minutes(sec: float) -> str:
    """Elapsed time in words, e.g. '12 minutes', '1 hour 5 minutes'."""
    m = int(round(sec / 60))
    if m < 1:
        return "less than a minute"
    unit = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    if m < 60:
        return unit(m, "minute")
    h, r = divmod(m, 60)
    return unit(h, "hour") + (f" {unit(r, 'minute')}" if r else "")


_SOURCE_PHRASE = {"Halo app": "the Halo app", "HomeKit": "Apple Home", "MQTT": "an MQTT command", "API": "the Halo API",
                  "Notification": "a notification action", "Auto-close": "auto-close",
                  "remote or wall button": "a handheld remote or the wall button"}


def _by(src: str) -> str:
    return f" using {_SOURCE_PHRASE.get(src, src)}" if src else ""


def _door_notifier():
    """Notify on door opened / closed / left open. Never for the light."""
    last_version, prev_closed, opened_at, reminders = None, None, None, 0
    while True:
        # Sleep until something changes or the next left-open reminder is due.
        wait = 60.0
        limit = NOTIFY["left_open_minutes"] * 60
        if opened_at and limit and reminders < 3:
            wait = max(0.2, min(wait, opened_at + limit * (reminders + 1) - time.time()))
        with _change:
            _change.wait_for(lambda: _version != last_version, timeout=wait)
            last_version = _version
        with state_lock:
            closed = door_info["state"] == "CLOSED"
            sensor_live = door_info["sensor"]["connected"]
        now = time.time()
        if prev_closed is None:                                   # startup: no alert
            prev_closed, opened_at = closed, (None if closed else now)
            continue
        if prev_closed != closed and correction_edge(now):        # manual correction: no alert
            prev_closed, reminders = closed, 0
            opened_at = None if closed else now
            continue
        if prev_closed and not closed:
            opened_at, reminders = now, 0
            src = _last_door_cmd["source"] if now - _last_door_cmd["at"] < 30 else (
                "remote or wall button" if sensor_live else None)
            if NOTIFY["on_open"]:
                send_notification("Garage Door Opened",
                                  f"The garage door was opened at {fmt_clock(now)}{_by(src)}.")
        elif not prev_closed and closed:
            if NOTIFY["on_close"]:
                how_long = f" It was open for {_fmt_minutes(now - opened_at)}." if opened_at else ""
                auto = _last_door_cmd["source"] == "Auto-close" and now - _last_door_cmd["at"] < 120
                if auto:
                    send_notification("Garage Door Closed Automatically",
                                      f"Halo closed the garage door at {fmt_clock(now)} because it was left open.{how_long}")
                else:
                    send_notification("Garage Door Closed", f"The garage door was closed at {fmt_clock(now)}.{how_long}")
            opened_at = None
        prev_closed = closed
        limit = NOTIFY["left_open_minutes"] * 60
        if opened_at and limit and reminders < 3 and now - opened_at >= limit * (reminders + 1):
            reminders += 1
            send_notification("Garage Door Left Open",
                              f"The garage door has been open since {fmt_clock(opened_at)} "
                              f"({_fmt_minutes(now - opened_at)}). Select Close Door to close it.",
                              priority="high", close_action=True)


# ── Static file cache ──────────────────────────────────────────────────────────
MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon",
}
COMPRESSIBLE = {".html", ".css", ".js", ".json", ".svg", ".webmanifest"}
_static_cache = {}
_static_lock = threading.Lock()


def get_static(file_path: str):
    """Return (body, gzipped_body_or_None, content_type, etag), cached by mtime."""
    mtime = os.path.getmtime(file_path)
    with _static_lock:
        hit = _static_cache.get(file_path)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(file_path, "rb") as f:
        body = f.read()
    ext = os.path.splitext(file_path)[1].lower()
    gz = gzip.compress(body, 9) if ext in COMPRESSIBLE and len(body) > 512 else None
    etag = '"' + hashlib.md5(body).hexdigest()[:16] + '"'
    entry = (body, gz, MIME.get(ext, "application/octet-stream"), etag)
    with _static_lock:
        _static_cache[file_path] = (mtime, entry)
    return entry


# ── HTTP handler ───────────────────────────────────────────────────────────────
class HaloRequestHandler(BaseHTTPRequestHandler):
    remote_mgr: RemoteManager = None
    radio: CC1101 = None
    tx_mode: str = "fifo"
    gdo0_gpio: int = 24
    server_version = "Halo/2.0"

    def log_message(self, fmt, *args):
        # Static and status traffic is noise; log only API writes and errors
        if self.command == "POST" or (args and str(args[1])[:1] in "45"):
            logger.info("%s %s" % (self.client_address[0], fmt % args))

    # helpers
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, data: dict, status_code: int = 200):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, action: str, e: Exception):
        msg = f"{action} failed: {e}"
        logger.error(msg)
        add_log(action, msg, success=False)
        self.send_json({"success": False, "error": msg}, status_code=500)

    def transmit(self, rolling: int, fixed: int, repeats: int, delay: float = 0.002):
        transmit(rolling, fixed, repeats, delay)

    def _repeats(self, data, default=None):
        return default_repeats(data.get("repeats"), default)

    # routing
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            return self.send_json(build_status(self.remote_mgr, self.radio))
        if path == "/api/events":
            return self.handle_events()
        if path == "/api/push/key":
            return self.send_json(push_public())
        if path == "/api/logs":
            return self.send_json({"logs": list(activity_log)})
        self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = {}
        if length:
            try:
                data = json.loads(self.rfile.read(min(length, 65536)).decode("utf-8")) or {}
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        for k, v in parse_qs(urlparse(self.path).query).items():   # e.g. ?source=Notification
            data.setdefault(k, v[-1])

        route = {
            "/api/toggle": self.handle_toggle,
            "/api/open": lambda d: self.handle_toggle(d, "open"),
            "/api/close": lambda d: self.handle_toggle(d, "close"),
            "/api/sensor": self.handle_sensor, "/api/state": self.handle_sensor,
            "/api/motion": self.handle_motion, "/api/car": self.handle_car,
            "/api/sensor-name": self.handle_sensor_name,
            "/api/notifications": self.handle_notifications,
            "/api/notifications/test": self.handle_notifications_test,
            "/api/settings": self.handle_settings,
            "/api/logs/clear": self.handle_logs_clear,
            "/api/radio/check": self.handle_radio_check,
            "/api/door/correct": self.handle_door_correct,
            "/api/distance": self.handle_distance,
            "/api/distance/calibrate": self.handle_distance_calibrate,
            "/api/distance/settings": self.handle_distance_settings,
            "/api/sensors/pair": self.handle_sensors_pair,
            "/api/push/subscribe": self.handle_push_subscribe,
            "/api/push/unsubscribe": self.handle_push_unsubscribe,
            "/api/sensors/assign": self.handle_sensors_assign,
            "/api/sensors/remove": self.handle_sensors_remove,
            "/api/bridge/restart": self.handle_bridge_restart,
            "/api/bridge": self.handle_bridge_settings,
            "/api/auto-close": self.handle_auto_close,
            "/api/auto-close/hold": self.handle_auto_close_hold,
            "/api/light": self.handle_light, "/api/light/state": self.handle_light_state,
            "/api/lock": self.handle_lock,
            "/api/pair": self.handle_pair, "/api/pair-light": self.handle_pair_light,
            "/api/set-counter": self.handle_set_counter,
            "/api/reset-remote": self.handle_reset_remote,
            "/api/test-rf": self.handle_test_rf,
        }.get(path)
        if route:
            route(data)
        else:
            self.send_json({"success": False, "code": "unknown_endpoint",
                            "error": f"Unknown API endpoint {path}. If you just updated Halo, it restarts itself "
                                     "within a few seconds; otherwise run: sudo systemctl restart halo"}, 404)

    # static
    def serve_static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        file_path = os.path.abspath(os.path.join(WEB_DIR, rel))
        if not file_path.startswith(WEB_DIR + os.sep) or not os.path.isfile(file_path):
            self.send_error(404, "File Not Found")
            return
        try:
            body, gz, ctype, etag = get_static(file_path)
        except OSError as e:
            self.send_error(500, f"Error reading file: {e}")
            return

        cache = "no-cache" if ctype.startswith(("text/html", "application/manifest", "application/json")) \
            or rel == "sw.js" else "public, max-age=86400"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        use_gz = gz is not None and "gzip" in self.headers.get("Accept-Encoding", "")
        payload = gz if use_gz else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache)
        if gz is not None:
            self.send_header("Vary", "Accept-Encoding")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(payload)

    # Server-Sent Events: push full status on every change, heartbeat otherwise
    def handle_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        last = None
        try:
            self.wfile.write(b"retry: 1000\n\n")      # browsers reconnect within 1 s
            while True:
                with _change:
                    _change.wait_for(lambda: _version != last, timeout=SSE_HEARTBEAT_SEC)
                    v = _version
                if v != last:
                    last = v
                    msg = "data: " + json.dumps(build_status(self.remote_mgr, self.radio),
                                                separators=(",", ":")) + "\n\n"
                else:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    # API handlers
    def handle_toggle(self, data: dict, cmd: str = "toggle"):
        try:
            r = command_door(cmd, data.get("repeats"), source=str(data.get("source") or "API")[:24])
        except LockedError as e:
            add_log("Door", "Blocked: remote lock is on", success=False)
            return self.send_json({"success": False, "error": str(e)}, 423)
        except Exception as e:
            return self._fail("Door", e)
        self.send_json({"success": True, **r})

    def handle_sensor(self, data: dict):
        kind, result = route_sensor(data, None, source="Contact sensor")
        if kind == "motion":
            return self.send_json({"success": result is not None, "motion": result})
        if result is None:
            return self.send_json({"success": False, "error":
                                   "Unrecognized payload. Send {'state':'open'|'closed'} or {'contact':true|false}"}, 400)
        self.send_json({"success": True, "door_state": door_info["state"],
                        "door_percent": door_info["percent"]})

    def handle_motion(self, data: dict):
        result = ingest_motion(data, source=data.get("mac") or "Motion sensor")
        if result is None:
            return self.send_json({"success": False, "error":
                                   "Unrecognized payload. Send {'motion': true|false} or {'state':'active'|'inactive'}"}, 400)
        self.send_json({"success": True, "motion": result})

    def handle_sensor_name(self, data: dict):
        which = data.get("sensor")
        name = " ".join(str(data.get("name", "")).split())[:32]
        if which not in SENSOR_NAME_KEYS or not name:
            return self.send_json({"success": False, "error":
                                   "Send {'sensor': 'door'|'motion', 'name': '...'} (1-32 characters)"}, 400)
        rename_sensor(which, name)
        self.send_json({"success": True, "sensor": which, "name": name})

    def handle_notifications(self, data: dict):
        try:
            n = configure_notifications(**{k: v for k, v in data.items() if k in (
                "enabled", "server", "topic", "left_open_minutes", "app_url", "on_open", "on_close", "ntfy")})
        except (ValueError, TypeError) as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        add_log("Notifications", "Door alerts on" if n["enabled"] else "Door alerts off", True)
        self.send_json({"success": True, "notifications": n})

    def handle_notifications_test(self, data: dict):
        if data.get("app_url"):
            configure_notifications(app_url=data["app_url"])
        if not NOTIFY["enabled"]:
            return self.send_json({"success": False, "error": "Turn on door alerts first"}, 400)
        err = send_notification("Halo Test Notification",
                                f"Garage door alerts are set up correctly. Sent at {fmt_clock()}.", wait=True)
        results = NOTIFY_STATUS.get("last_results") or {}
        if err:
            return self.send_json({"success": False, "error": err, "results": results}, 502)
        got = [k for k, v in results.items() if v is None]
        failed = {k: v for k, v in results.items() if v is not None}
        msg = "Delivered to " + ", ".join(got) + (". Failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items())
                                                  if failed else ".")
        self.send_json({"success": True, "message": msg, "results": results})

    def handle_logs_clear(self, data: dict):
        activity_log.clear()
        notify_change()
        self.send_json({"success": True})

    def handle_radio_check(self, data: dict):
        self.send_json({"success": True, "radio": check_radio()})

    def handle_door_correct(self, data: dict):
        st = str(data.get("state", "")).lower()
        if st not in ("open", "closed"):
            return self.send_json({"success": False, "error": "Send {'state': 'open'|'closed'}"}, 400)
        correct_door(st == "open")
        self.send_json({"success": True, "door_state": door_info["state"]})

    def handle_distance(self, data: dict):
        """Readings from any distance sensor: {'id':'front','distance_cm':180} or a grid
        {'id':'grid','zones':[...64 values in cm...]}."""
        sid = str(data.get("id") or (DIST["sensors"][0]["id"] if DIST["sensors"] else ""))
        try:
            if data.get("zones") is not None:
                present = ingest_distance(sid, zones=data["zones"])
            else:
                val = data.get("distance_cm")
                if val is None and data.get("distance_m") is not None:
                    val = float(data["distance_m"]) * 100
                if val is None:
                    raise ValueError("Send {'id': 'front', 'distance_cm': 123} or {'id': 'grid', 'zones': [...]}")
                present = ingest_distance(sid, distance_cm=val)
        except (TypeError, ValueError) as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        self.send_json({"success": True, "present": present, "distance": distance_public()})

    def handle_distance_calibrate(self, data: dict):
        try:
            done = calibrate_distance(str(data.get("mode") or "empty"))
        except RuntimeError as e:
            return self.send_json({"success": False, "error": str(e)}, 409)
        self.send_json({"success": True, "calibrated": done})

    def handle_distance_settings(self, data: dict):
        try:
            if isinstance(data.get("enabled"), bool):
                DIST["enabled"] = data["enabled"]
                if DIST["enabled"]:
                    start_distance_reader()                       # no-op if already running
                else:
                    stop_distance_reader()                        # releases the GPIO pins
                    DIST_INFO.update(status="off", present=None, committed=None, error=None)
            for k, lo, hi in (("min_height_cm", 10, 250), ("min_width_cm", 30, 400), ("stable_seconds", 0, 600)):
                if data.get(k) is not None:
                    DIST[k] = max(lo, min(hi, float(data[k])))
                    DIST_INFO.update(present=None, since=time.time())
            if isinstance(data.get("sensors"), list):
                DIST["sensors"] = [dict(x) for x in data["sensors"] if isinstance(x, dict) and x.get("id")]
                if DIST["enabled"]:                               # reopen with the new pins
                    stop_distance_reader()
                    start_distance_reader()
        except (TypeError, ValueError) as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        save_distance_settings()
        notify_change()
        self.send_json({"success": True, "distance": distance_public()})

    def handle_settings(self, data: dict):
        if isinstance(data.get("clock_24h"), bool):
            SETTINGS["clock_24h"] = data["clock_24h"]
            if CTX["remote_mgr"]:
                CTX["remote_mgr"].update(clock_24h=SETTINGS["clock_24h"])
            add_log("Halo", "24-hour time on" if SETTINGS["clock_24h"] else "24-hour time off", True)
            notify_change()
        if "timezone" in data:
            try:
                set_timezone(str(data["timezone"]))
            except ValueError as e:
                return self.send_json({"success": False, "error": str(e)}, 400)
            notify_change()
        self.send_json({"success": True, "timezone": tz_public()})

    def handle_push_subscribe(self, data: dict):
        if not PUSH["vapid"]:
            return self.send_json({"success": False, "error": PUSH["error"] or "Not available"}, 503)
        try:
            add_push_subscription(data.get("subscription") or {}, str(data.get("device") or "Device"))
        except ValueError as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        with _notify_lock:
            needs_on = not NOTIFY["enabled"]
        if needs_on:
            configure_notifications(enabled=True)
        notify_change()
        self.send_json({"success": True, "push": push_public()})

    def handle_push_unsubscribe(self, data: dict):
        remove_push_subscription(str(data.get("endpoint") or ""))
        self.send_json({"success": True, "push": push_public()})

    def handle_sensors_pair(self, data: dict):
        try:
            start_pairing(int(data.get("seconds", 60)))
        except RuntimeError as e:
            return self.send_json({"success": False, "error": str(e)}, 503)
        self.send_json({"success": True, "until": PAIRING["until"]})

    def handle_sensors_assign(self, data: dict):
        try:
            assign_sensor(data.get("mac"), data.get("role", "none"))
        except ValueError as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        self.send_json({"success": True, "sensors": inventory_list()})

    def handle_sensors_remove(self, data: dict):
        try:
            unpair_sensor(data.get("mac"))
        except RuntimeError as e:
            return self.send_json({"success": False, "error": str(e)}, 503)
        self.send_json({"success": True})

    def handle_bridge_restart(self, data: dict):
        ok = restart_bridge("restarted from Halo")
        BRIDGE_INFO["status"] = "ok" if ok else "down"
        notify_change()
        if not ok:
            return self.send_json({"success": False, "error": BRIDGE_INFO["error"]}, 500)
        self.send_json({"success": True})

    def handle_bridge_settings(self, data: dict):
        if isinstance(data.get("watchdog"), bool):
            BRIDGE["watchdog"] = data["watchdog"]
            if CTX["remote_mgr"]:
                saved = dict(CTX["remote_mgr"].config.get("bridge") or {})
                saved["watchdog"] = BRIDGE["watchdog"]
                CTX["remote_mgr"].update(bridge=saved)
            add_log("Wyze Bridge", "Auto-reconnect on" if BRIDGE["watchdog"] else "Auto-reconnect off", True)
        self.send_json({"success": True, "bridge": {**BRIDGE_INFO, "watchdog": BRIDGE["watchdog"]}})

    def handle_auto_close(self, data: dict):
        try:
            r = configure_auto_close(**{k: v for k, v in data.items() if k in ("enabled", "minutes")})
        except (TypeError, ValueError) as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        notify_change()
        self.send_json({"success": True, "auto_close": r})

    def handle_auto_close_hold(self, data: dict):
        hold_auto_close()
        self.send_json({"success": True})

    def handle_car(self, data: dict):
        present = data.get("present")
        if not isinstance(present, bool):
            return self.send_json({"success": False, "error": "Send {'present': true|false}"}, 400)
        set_car("present" if present else "away", "manual", "Set in Halo")
        self.send_json({"success": True, "car": door_info["car"]["state"]})

    def handle_light(self, data: dict):
        try:
            r = command_light(data.get("command", "toggle"), data.get("repeats"),
                              source=str(data.get("source") or "API")[:24])
        except Exception as e:
            return self._fail("Light", e)
        self.send_json({"success": True, **r})

    def handle_light_state(self, data: dict):
        on = bool(data.get("on", False))
        set_light(on, "Light", f"Reported {'on' if on else 'off'} by integration")
        self.send_json({"success": True, "light_on": on})

    def handle_lock(self, data: dict):
        self.send_json({"success": True, "locked": set_lock(data.get("locked"))})

    def handle_pair(self, data: dict):
        rolling, fixed = self.remote_mgr.get_next_codes()
        try:
            self.transmit(rolling, fixed, self._repeats(data, 20), delay=0.002)
        except Exception as e:
            return self._fail("Pair Door", e)
        new_rolling, _ = self.remote_mgr.peek_codes()
        add_log("Pair Door", f"Sent pairing bursts (ID {fixed:X}, code {rolling})", True)
        self.send_json({"success": True, "fixed_code": fixed, "next_rolling_code": new_rolling})

    def handle_pair_light(self, data: dict):
        rolling, fixed = self.remote_mgr.get_button_codes(button=1)
        try:
            self.transmit(rolling, fixed, self._repeats(data, 24), delay=0.002)
        except Exception as e:
            return self._fail("Pair Light", e)
        new_rolling, _ = self.remote_mgr.peek_codes()
        add_log("Pair Light", f"Sent light pairing bursts (code {rolling})", True)
        self.send_json({"success": True, "fixed_code": fixed, "next_rolling_code": new_rolling})

    def handle_set_counter(self, data: dict):
        try:
            val = int(data["counter"])
            if val <= 0:
                raise ValueError("counter must be positive")
            self.remote_mgr.set_rolling_code(val)
        except (KeyError, TypeError, ValueError) as e:
            return self.send_json({"success": False, "error": f"Invalid counter: {e}"}, 400)
        add_log("Rolling Code", f"Counter set to {val}", True)
        self.send_json({"success": True, "rolling_code": val})

    def handle_reset_remote(self, data: dict):
        start = int(data.get("start_counter", 1000))
        new_fixed, new_rolling = self.remote_mgr.reset_remote(start_rolling=start)
        add_log("Remote ID", f"New virtual remote {new_fixed:X} (counter {new_rolling})", True)
        self.send_json({"success": True, "fixed_code": new_fixed, "rolling_code": new_rolling})

    def handle_test_rf(self, data: dict):
        duration = max(0.5, min(10.0, float(data.get("duration", 3.0))))
        burst = generate_burst_bytes(12345, 67890)
        count = 0
        with radio_lock:
            end = time.time() + duration
            while time.time() < end:
                self.radio.transmit_burst(burst)
                time.sleep(0.02)
                count += 1
        msg = f"Sent {count} test bursts over {duration:g}s"
        add_log("RF Test", msg, True)
        self.send_json({"success": True, "message": msg, "bursts_sent": count})


# Kept for backwards compatibility with anything importing the old name
GarageRequestHandler = HaloRequestHandler


def refresh_hw_state(radio: CC1101, full: bool = False):
    """Read CC1101 registers. Caller holds radio_lock (or is single-threaded startup)."""
    if radio.is_mock:
        return
    try:
        if full:
            hw_info["part_number"] = f"0x{radio.get_partnum():02X}"
            hw_info["version"] = f"0x{radio.get_version():02X}"
        hw_info["state"] = f"0x{radio.get_state():02X}"
    except Exception as e:
        logger.error(f"Error reading radio registers: {e}")


MARC_STATES = {
    0x00: "Sleep", 0x01: "Idle", 0x02: "Crystal off", 0x03: "Starting (VCOON_MC)", 0x04: "Starting (REGON_MC)",
    0x05: "Calibrating", 0x06: "Starting (VCOON)", 0x07: "Starting (REGON)", 0x08: "Calibrating (STARTCAL)",
    0x09: "Settling (BWBOOST)", 0x0A: "Settling (FS_LOCK)", 0x0B: "Settling (IFADCON)", 0x0C: "Calibrating (ENDCAL)",
    0x0D: "Receiving", 0x0E: "Receive ending", 0x0F: "RX to TX", 0x10: "RX to TX settling",
    0x11: "Receive overflow", 0x12: "Frequency synth on", 0x13: "Transmitting", 0x14: "Transmit ending",
    0x15: "TX to RX settling", 0x16: "Transmit underflow",
}
RADIO_CHECK = {"checked_at": None, "ok": None, "checks": [], "summary": "Not checked yet"}


def check_radio() -> dict:
    """Read the CC1101's identity, state and frequency over SPI and say plainly if it's healthy.
    Read-only: nothing on the chip is changed."""
    radio = CTX["radio"]
    checks = []
    if radio is None or radio.is_mock:
        RADIO_CHECK.update(checked_at=_now_iso(), ok=False, summary="No CC1101 found: running in simulation",
                           checks=[{"name": "SPI connection", "ok": False,
                                    "detail": "Couldn't open /dev/spidev0.0. Enable SPI (raspi-config) and check the wiring."}])
        notify_change()
        return dict(RADIO_CHECK)
    try:
        with radio_lock:
            part, ver = radio.get_partnum(), radio.get_version()
            state = radio.get_state()
            from cc1101 import REG_FREQ2, REG_FREQ1, REG_FREQ0
            f2, f1, f0 = radio.read_reg(REG_FREQ2), radio.read_reg(REG_FREQ1), radio.read_reg(REG_FREQ0)
    except Exception as e:
        RADIO_CHECK.update(checked_at=_now_iso(), ok=False, summary=f"SPI error: {e}",
                           checks=[{"name": "SPI connection", "ok": False, "detail": str(e)[:160]}])
        notify_change()
        return dict(RADIO_CHECK)
    hw_info.update(part_number=f"0x{part:02X}", version=f"0x{ver:02X}", state=f"0x{state:02X}")
    wiring_dead = ver in (0x00, 0xFF) and part in (0x00, 0xFF)
    checks.append({"name": "SPI connection", "ok": not wiring_dead,
                   "detail": "Chip is answering" if not wiring_dead else
                   "No answer (reads all 0x00 or 0xFF). Check VCC 3.3 V, GND, MISO/MOSI/SCK/CSN wiring."})
    checks.append({"name": "Chip identity", "ok": part == 0x00 and ver in (0x04, 0x14),
                   "detail": f"Part 0x{part:02X}, version 0x{ver:02X}"
                             + (" (genuine CC1101)" if ver == 0x14 else " (older CC1101 silicon)" if ver == 0x04
                                else " (not a recognised CC1101)")})
    freq = 26.0 * ((f2 << 16) | (f1 << 8) | f0) / 65536.0
    target = float(CTX["remote_mgr"].config.get("frequency_mhz", 315.0)) if CTX["remote_mgr"] else 315.0
    checks.append({"name": "Frequency", "ok": abs(freq - target) < 0.05,
                   "detail": f"Tuned to {freq:.3f} MHz (expected {target:.2f} MHz, 26 MHz crystal)"})
    state_name = MARC_STATES.get(state, f"Unknown (0x{state:02X})")
    checks.append({"name": "Radio state", "ok": state in (0x01, 0x0D, 0x13, 0x14, 0x12),
                   "detail": state_name + ("" if state in (0x01, 0x0D, 0x13, 0x14, 0x12) else
                                           ". Unusual: try restarting Halo")})
    ok = all(c["ok"] for c in checks)
    if ok:
        radio.verified, radio.problem = True, None
    else:
        radio.verified = False
        radio.problem = next(c["name"] + ": " + c["detail"] for c in checks if not c["ok"])
    RADIO_CHECK.update(checked_at=_now_iso(), ok=ok, checks=checks, state_name=state_name,
                       frequency_mhz=round(freq, 3),
                       summary="CC1101 is healthy" if ok else next(c["name"] + ": " + c["detail"] for c in checks if not c["ok"]))
    add_log("Radio", RADIO_CHECK["summary"], ok)
    return dict(RADIO_CHECK)


def build_status(remote_mgr: RemoteManager, radio: CC1101) -> dict:
    rolling, fixed = remote_mgr.peek_codes()
    cfg = remote_mgr.config
    with state_lock:
        d = dict(door_info)
        sensor = dict(door_info["sensor"])
    remaining = max(0, int(d["light_timer_expires"] - time.time())) if d["light_on"] and d["light_timer_expires"] else 0
    moving = d["state"] in ("OPENING", "CLOSING")
    return {
        "app": APP_NAME,
        "timezone": tz_public(),
        "update": dict(UPDATE),
        "duplicates": {"others": list(DUPLICATES["others"]), "self_unit": DUPLICATES["self_unit"]},
        "https": dict(HTTPS_INFO),
        "server": {"pid": os.getpid(), "started_at": STARTED_AT, "uptime_s": int(time.monotonic() - _STARTED_MONO)},
        "app_build": _app_build(),
        "name": cfg.get("name", "Virtual Remote"),
        "frequency_mhz": cfg.get("frequency_mhz", 315.0),
        "protocol": "Security+ 1.0",
        "fixed_code": fixed,
        "fixed_code_hex": f"0x{fixed:X}",
        "rolling_code": rolling,
        "rolling_code_hex": f"0x{rolling:X}",
        "repeats": cfg.get("repeats_per_press", 8),
        "door": {
            "state": d["state"],
            "percent": d["percent"],
            "travel_time": d["travel_time"],
            "motor": "Moving" if moving else "Idle",
            "updated_at": d["updated_at"],
        },
        "light": {"on": d["light_on"], "estimated": True, "auto_off_remaining_sec": remaining},
        "notifications": notify_public(),
        "auto_close": auto_close_public(),
        "distance": distance_public(),
        "sensors": inventory_list(),
        "pairing": {"active": PAIRING["until"] > time.time(), "until": PAIRING["until"]},
        "bridge": {**BRIDGE_INFO, "dongles": dict(BRIDGE_INFO["dongles"]), "watchdog": BRIDGE["watchdog"]},
        "sensor": sensor,
        "car": dict(d["car"]),
        "motion": dict(d["motion"]),
        "security": {"remote_lock": d["locked"]},
        "hardware": {
            "connected": (not radio.is_mock) and radio.verified is not False,
            "verified": radio.verified,
            "problem": radio.problem,
            "is_simulation": radio.is_mock,
            **hw_info,
            "state_name": MARC_STATES.get(int(hw_info["state"], 16), "Unknown"),
            "check": dict(RADIO_CHECK),
        },
        "recent_logs": list(activity_log)[:20],
    }


def get_local_ip() -> str:
    """LAN address for the startup banner. Works with no internet (no packet is sent)."""
    for probe in ("192.168.0.1", "10.0.0.1", "172.16.0.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    return ip
        except OSError:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


# When new code is copied onto the Pi, the page updates at once but the running
# Python process doesn't. The app would then call endpoints the old process
# doesn't have ("Unknown API endpoint"). So Halo watches its own code and, once
# a copy has finished and the new code compiles, restarts itself in place when
# the door isn't moving and the radio is idle.
CODE_FILES = [os.path.join(BASE_DIR, f) for f in
              ("web_server.py", "cc1101.py", "security_plus.py", "remote_manager.py", "garage_door.py")]
UPDATE = {"restart_needed": False, "error": None, "auto_restart": True}


def _code_signature():
    sig = []
    for f in CODE_FILES:
        try:
            st = os.stat(f)
            sig.append((f, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((f, None, None))
    return sig


_START_SIG = _code_signature()
STARTED_AT = time.time()           # identifies this run of Halo
_STARTED_MONO = time.monotonic()   # uptime: immune to the clock jump at boot (Pi has no RTC)


def _app_build():
    """Fingerprint of the app page, so open screens reload themselves after an update."""
    try:
        return get_static(os.path.join(WEB_DIR, "index.html"))[3]
    except OSError:
        return None


def _code_watcher(server):
    last_bad = None
    while True:
        time.sleep(3)
        sig = _code_signature()
        if sig == _START_SIG:
            continue
        time.sleep(2)
        if _code_signature() != sig:
            continue                                   # still being copied
        try:
            for f, mt, _ in sig:
                if mt is not None:
                    with open(f, encoding="utf-8") as fh:
                        compile(fh.read(), f, "exec")      # syntax check only; writes nothing
        except (SyntaxError, ValueError, OSError) as e:
            msg = f"The updated code has an error, so Halo is still running the previous version: {str(e)[:200]}"
            if msg != last_bad:
                last_bad = msg
                UPDATE.update(error=msg, restart_needed=True)
                add_log("Halo", msg, False)
            continue
        UPDATE.update(restart_needed=True, error=None)
        notify_change()
        if not UPDATE["auto_restart"] or os.name != "posix":
            continue                                   # e.g. Windows dev machine: tell the app instead
        with state_lock:
            moving = door_info["state"] in ("OPENING", "CLOSING")
        if moving or not radio_lock.acquire(timeout=1):
            continue                                   # try again in a few seconds
        try:
            logger.info("[Update] New code installed; restarting Halo")
            with state_lock:
                save_door_state()
            stop_distance_reader()                       # hand the GPIO pins back first
            try:
                CTX["radio"].close()                     # and the SPI bus
            except Exception:
                pass
            # Close every other handle (GPIO lines, sockets, files) so nothing carries over
            # into the new version. Don't close the server first: that makes the main thread
            # exit and race the restart.
            os.closerange(3, 65536)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        finally:
            radio_lock.release()                        # only reached if execv failed


# ── Port already in use ───────────────────────────────────────────────────────
# If an older copy of Halo is still running (started by hand, from cron, or an
# old service), it keeps port 8080 and the app keeps talking to the old code.
# The new Halo stops that older copy and takes over. Any other program is left
# alone, with a clear message saying what it is.
HALO_CMD_RE = re.compile(r"(garage_door\.py\b.*\bweb\b|web_server\.py)")


def _port_owners(port: int, proc: str = "/proc"):
    """[(pid, command line)] of processes listening on a TCP port (Linux /proc).
    Processes Halo isn't allowed to inspect (e.g. started with sudo) are counted as None."""
    inodes = set()
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, "net", name)) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) > 9 and cols[3] == "0A" and int(cols[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(cols[9])                        # state 0A = LISTEN
    if not inodes:
        return []
    owners, hidden = [], False
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit() and int(d) != os.getpid()]
    except OSError:
        return []
    for pid in pids:
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            hidden = True
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link.startswith("socket:[") and link[8:-1] in inodes:
                try:
                    with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                        cmd = f.read().replace(bytes([0]), b" ").decode(errors="replace").strip()
                except OSError:
                    cmd = "?"
                owners.append((int(pid), cmd))
                break
    if not owners and hidden:
        owners.append((None, None))                        # held by a process we can't see
    return owners


def _stop_old_halo(pid: int, cmd: str, port: int) -> bool:
    logger.warning(f"Port {port} is held by an older copy of Halo (PID {pid}: {cmd}). Stopping it so this "
                   f"version can take over.")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):                                # up to 8 s to exit cleanly
            time.sleep(0.2)
            os.kill(pid, 0)
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        time.sleep(0.5)
        return True
    except ProcessLookupError:
        return True                                        # it exited
    except PermissionError:
        logger.error(f"Not allowed to stop PID {pid} (it was probably started with sudo). Stop it with: sudo kill {pid}")
        return False


# ── One Halo at a time ─────────────────────────────────────────────────────────
# Two copies of Halo (e.g. two services, or a service plus a cron job) would fight:
# each resets the CC1101, grabs the GPIO pins and the port. So the first copy holds
# a lock file; any other copy waits quietly WITHOUT touching the radio, pins or port,
# and only takes over if the first one stops. The app shows a banner naming both.
LOCK_FILE = os.path.join(BASE_DIR, "halo.lock")
_instance = {"lock": None}
DUPLICATES = {"others": [], "self_unit": None}


def unit_of(pid) -> str:
    """What started a process: its systemd service, cron, or a terminal."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            path = f.read()
    except OSError:
        return "unknown"
    for part in reversed(path.replace("/", " ").split()):
        if part.endswith(".service"):
            return "cron (crontab)" if part == "cron.service" else part
        if part.startswith("session-") and part.endswith(".scope"):
            return "a terminal or login session (started by hand)"
    return "unknown"


def other_halos(proc: str = "/proc"):
    """Other running Halo processes: [(pid, command, what started it)]."""
    me, out = os.getpid(), []
    try:
        pids = [int(d) for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == me:
            continue
        try:
            with open(os.path.join(proc, str(pid), "cmdline"), "rb") as f:
                cmd = f.read().replace(bytes([0]), b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if HALO_CMD_RE.search(cmd):
            out.append((pid, cmd, unit_of(pid)))
    return out


def acquire_single_instance():
    """Take the Halo lock, or wait (idle) until the copy holding it stops."""
    if os.name != "posix":
        return
    import fcntl
    f = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        holder = f.read().strip()
        who = f"PID {holder}, started by {unit_of(holder)}" if holder.isdigit() else "another process"
        msg = (f"Another copy of Halo is already running ({who}). This copy ({unit_of(os.getpid())}) will "
               f"wait without touching the radio, pins or port, and take over only if that one stops. "
               f"Only one thing should start Halo: see README, 'Two copies of Halo'.")
        logger.warning(msg)
        print(msg, file=sys.stderr, flush=True)
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)          # wait here
        logger.info("The other copy of Halo stopped; this copy is taking over")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _instance["lock"] = f
    DUPLICATES["self_unit"] = unit_of(os.getpid())


def _duplicate_watch():
    """Keep the app's 'two copies' banner current."""
    while True:
        others = [{"pid": p, "unit": u} for p, _, u in other_halos()]
        if others != DUPLICATES["others"]:
            DUPLICATES["others"] = others
            if others:
                logger.warning(f"[Halo] Another copy of Halo is waiting: {others}")
            notify_change()
        time.sleep(30)


def _bind_http(host: str, port: int):
    for attempt in range(3):
        try:
            return ThreadingHTTPServer((host, port), HaloRequestHandler)
        except OSError as e:
            in_use = e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) in (10048, 10013)
            if not in_use or attempt == 2:
                raise
            owners = _port_owners(port)
            # We hold the Halo lock, so any Halo still on the port is an older version
            # that doesn't know about the lock: safe to stop it.
            halos = [(p, c) for p, c in owners if p and c and HALO_CMD_RE.search(c)]
            if owners and len(halos) == len(owners):
                if all(_stop_old_halo(p, c, port) for p, c in halos):
                    continue
            if owners and owners[0][0] is None:
                who = "a program Halo isn't allowed to inspect (probably started with sudo)"
            elif owners:
                who = "; ".join(f"PID {p}: {c}" for p, c in owners)
            else:
                who = "another program"
            msg = (f"Port {port} is already in use by {who}. Halo can't start until it's stopped. "
                   f"Find it with: sudo ss -ltnp 'sport = :{port}'  then: sudo kill <PID>. "
                   f"Or run Halo on another port with --port.")
            logger.error(msg)
            print(msg, file=sys.stderr)
            raise SystemExit(1)


# Halo notifications on iPhone only work on an HTTPS page. Halo serves the same app
# over HTTPS on a second port, using a certificate from Tailscale (free, trusted by
# iPhones): sudo tailscale cert ... (see README). Plain HTTP on 8080 stays as is.
HTTPS = {"enabled": True, "port": 8443, "cert_file": os.path.join(BASE_DIR, "halo.crt"),
         "key_file": os.path.join(BASE_DIR, "halo.key")}
HTTPS_INFO = {"running": False, "url": None, "expires": None, "error": None}


class _TLSServer(ThreadingHTTPServer):
    """HTTPS server; the TLS handshake runs in each request's own thread, so one slow
    or broken client can't hold up the others."""
    ssl_ctx = None

    def get_request(self):
        sock, addr = super().get_request()
        sock.settimeout(15)
        return self.ssl_ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass                                            # failed handshakes (e.g. http:// on the https port)


def _cert_details(path: str):
    """(hostname, expiry text) from a certificate file, or (None, None)."""
    try:
        info = ssl._ssl._test_decode_cert(path)
        names = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
        return (names[0] if names else None), info.get("notAfter")
    except Exception:
        return None, None


def start_https(handler):
    cert, key = HTTPS["cert_file"], HTTPS["key_file"]
    if not HTTPS["enabled"]:
        return
    if not (os.path.exists(cert) and os.path.exists(key)):
        HTTPS_INFO.update(running=False, error=None)

        def wait_for_cert():                            # certificate created later: start HTTPS then
            while not (os.path.exists(cert) and os.path.exists(key)):
                time.sleep(30)
            time.sleep(2)                               # let both files finish writing
            start_https(handler)
        threading.Thread(target=wait_for_cert, daemon=True).start()
        return
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        _TLSServer.allow_reuse_address = True
        _TLSServer.daemon_threads = True
        _TLSServer.ssl_ctx = ctx
        srv = _TLSServer(("0.0.0.0", int(HTTPS["port"])), handler)
    except Exception as e:
        HTTPS_INFO.update(running=False, error=f"HTTPS couldn't start: {e}")
        logger.error(HTTPS_INFO["error"])
        return
    host, expires = _cert_details(cert)
    HTTPS_INFO.update(running=True, error=None, expires=expires,
                      url=f"https://{host}:{HTTPS['port']}" if host else None)
    threading.Thread(target=srv.serve_forever, daemon=True, name="https").start()
    logger.info(f"[HTTPS] Serving on port {HTTPS['port']} ({host or 'certificate has no hostname'})")

    def watch_cert():                                   # renewed certificate: use it without a restart
        stamp = (os.path.getmtime(cert), os.path.getmtime(key))
        while True:
            time.sleep(60)
            try:
                now = (os.path.getmtime(cert), os.path.getmtime(key))
                if now != stamp:
                    time.sleep(2)
                    ctx.load_cert_chain(cert, key)
                    stamp = now
                    HTTPS_INFO["expires"] = _cert_details(cert)[1]
                    logger.info("[HTTPS] Reloaded renewed certificate")
            except Exception as e:
                HTTPS_INFO["error"] = f"Couldn't load the renewed certificate: {e}"
    threading.Thread(target=watch_cert, daemon=True).start()


def run_server(host: str = "0.0.0.0", port: int = 8080, config_path: str = None,
               spi_bus: int = 0, spi_device: int = 0,
               tx_mode: str = "fifo", gdo0_gpio: int = 24):
    os.makedirs(WEB_DIR, exist_ok=True)
    acquire_single_instance()                           # before touching radio, pins or port
    load_door_state()

    remote_mgr = RemoteManager(config_path) if config_path else RemoteManager()
    radio = CC1101(bus=spi_bus, device=spi_device)
    radio.init_spi()
    if tx_mode == "async":
        radio.setup_async_ook_315mhz()
    else:
        radio.setup_security_plus_315mhz(packet_len=50)
    refresh_hw_state(radio, full=True)

    SETTINGS["clock_24h"] = bool(remote_mgr.config.get("clock_24h", False))
    tzname = remote_mgr.config.get("timezone") or DEFAULT_TZ
    if valid_timezone(tzname):
        SETTINGS["timezone"] = tzname
    if not tz_database_ok():
        logger.warning("[Time] No time-zone database found; install it with: sudo apt install tzdata "
                       "(or pip install tzdata). Times fall back to UTC until then.")
    init_push()
    PUSH["subject"] = str(remote_mgr.config.get("push_subject") or PUSH["subject"])
    saved_n = remote_mgr.config.get("notifications") or {}
    with _notify_lock:
        NOTIFY.update({k: v for k, v in saved_n.items() if k in NOTIFY})
    threading.Thread(target=_door_notifier, daemon=True).start()
    BRIDGE.update({k: v for k, v in (remote_mgr.config.get("bridge") or {}).items() if k in BRIDGE or k == "interval"})
    AUTO_CLOSE.update({k: v for k, v in (remote_mgr.config.get("auto_close") or {}).items() if k in AUTO_CLOSE})
    threading.Thread(target=_bridge_watchdog, daemon=True).start()
    DIST.update({k: v for k, v in (remote_mgr.config.get("distance_sensor") or {}).items() if k in DIST})
    if os.name == "posix":
        release_leftover_gpio()                         # e.g. from an older self-restart
    if DIST["enabled"]:
        start_distance_reader()
    threading.Thread(target=_auto_close_worker, daemon=True).start()
    CTX.update(remote_mgr=remote_mgr, radio=radio)
    threading.Thread(target=check_radio, daemon=True).start()          # health check at startup
    SENSORS["door"] = _norm_mac(remote_mgr.config.get("sensor_mac"))
    SENSORS["car"] = _norm_mac(remote_mgr.config.get("car_sensor_mac"))
    for which, (info_key, cfg_key) in SENSOR_NAME_KEYS.items():
        if remote_mgr.config.get(cfg_key):
            door_info[info_key]["name"] = str(remote_mgr.config[cfg_key])[:32]
    for mac in (SENSORS["door"], SENSORS["car"]):
        if mac and MAC_RE.match(mac):
            SENSOR_INV.setdefault(mac, {"mac": mac, "name": None, "kind": "contact" if mac == SENSORS["door"] else "motion",
                                        "battery": None, "signal_strength": None, "last_seen": None, "found_at": 0})
    if SENSORS["door"]:
        door_info["sensor"]["mac"] = SENSORS["door"]
    if SENSORS["car"]:
        door_info["motion"]["mac"] = SENSORS["car"]
    global CAR_PRE_SEC, CAR_POST_SEC, CAR_POST_SKIP
    cd = remote_mgr.config.get("car_detection", {})
    CAR_PRE_SEC = float(cd.get("before_open_sec", CAR_PRE_SEC))
    CAR_POST_SEC = float(cd.get("after_close_sec", CAR_POST_SEC))
    CAR_POST_SKIP = float(cd.get("ignore_after_close_sec", CAR_POST_SKIP))
    threading.Thread(target=_car_watcher, daemon=True).start()
    if door_info["light_on"]:
        set_light(True)                       # re-arm auto-off after a restart

    CTX.update(remote_mgr=remote_mgr, radio=radio, tx_mode=tx_mode, gdo0=gdo0_gpio)
    HaloRequestHandler.remote_mgr = remote_mgr
    HaloRequestHandler.radio = radio
    HaloRequestHandler.tx_mode = tx_mode
    HaloRequestHandler.gdo0_gpio = gdo0_gpio

    mqtt_cfg = remote_mgr.config.get("mqtt", {})
    if mqtt_cfg.get("enabled", True):
        start_mqtt_client(
            broker_host=mqtt_cfg.get("broker", "127.0.0.1"),
            broker_port=int(mqtt_cfg.get("port", 1883)),
            topic=mqtt_cfg.get("topic", "wyzesense2mqtt/#"),
            base_topic=mqtt_cfg.get("base_topic", "halo/garage"),
            username=mqtt_cfg.get("username"),
            password=mqtt_cfg.get("password"),
        )

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True   # SSE streams never block shutdown
    server = _bind_http(host, port)

    local_ip = get_local_ip()
    print("=" * 56)
    print(f"  {APP_NAME} - smart garage")
    print("=" * 56)
    print(f"  Local   : http://localhost:{port}/")
    print(f"  Network : http://{local_ip}:{port}/  or  http://{socket.gethostname()}.local:{port}/")
    print(f"  Radio   : {'SIMULATION (no CC1101 found)' if radio.is_mock else 'CC1101 on SPI'}")
    print("=" * 56)
    print("  Press Ctrl+C to stop.\n")
    add_log("Halo", f"Server started on port {port}", success=True)

    UPDATE["auto_restart"] = bool(remote_mgr.config.get("auto_restart_on_update", True))
    if os.name == "posix":
        threading.Thread(target=_duplicate_watch, daemon=True).start()
    HTTPS.update({k: v for k, v in (remote_mgr.config.get("https") or {}).items() if k in HTTPS})
    start_https(HaloRequestHandler)
    threading.Thread(target=_code_watcher, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Halo...")
    finally:
        stop_distance_reader()
        server.server_close()
        radio.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Halo web UI & REST API server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", default=None, help="Path to remote_config.json")
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=0)
    parser.add_argument("--mode", choices=["fifo", "async"], default="fifo")
    parser.add_argument("--gdo0", type=int, default=24)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, config_path=args.config,
               spi_bus=args.spi_bus, spi_device=args.spi_device,
               tx_mode=args.mode, gdo0_gpio=args.gdo0)
