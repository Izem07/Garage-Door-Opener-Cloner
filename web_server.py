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
import urllib.request
import logging
import threading
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from cc1101 import CC1101
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
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
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


def set_door_state(is_open: bool):
    """Ground-truth update (sensor/integration): door is fully OPEN or CLOSED."""
    global _travel_id
    with state_lock:
        _travel_id += 1                      # cancel any simulated travel
        door_info["state"] = "OPEN" if is_open else "CLOSED"
        door_info["percent"] = 100 if is_open else 0
        door_info["updated_at"] = _now_iso()
        save_door_state()
    notify_change()


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
        s["last_seen"] = time.strftime("%H:%M:%S")
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
    add_log("Sensor", f"Renamed “{old}” to “{name}”", True)


def set_car(state: str, source: str, reason: str):
    with state_lock:
        car = door_info["car"]
        changed = car["state"] != state
        car.update(state=state, source=source, reason=reason, updated_at=_now_iso())
        save_door_state()
    if changed:
        add_log("Car", f"{'In garage' if state == 'present' else 'Away' if state == 'away' else 'Unknown'} — {reason}", True)
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


def route_sensor(data: dict, topic_mac: str, source: str):
    """Send a sensor message to the right handler by MAC (door contact vs. car motion)."""
    mac = _norm_mac(data.get("mac") or topic_mac)
    if mac:
        data["mac"] = mac
    cls = str(data.get("device_class", data.get("type", ""))).lower()
    if SENSORS["car"] and mac == SENSORS["car"]:
        return "motion", ingest_motion(data, source)
    if not SENSORS["car"] and ("motion" in cls or "occupancy" in cls or "motion" in data or "occupancy" in data):
        return "motion", ingest_motion(data, source)
    if SENSORS["door"] and mac and mac != SENSORS["door"]:
        logger.debug(f"[Sensor] Ignoring unknown sensor {mac}")
        return None, None
    return "door", ingest_sensor(data, source)


# ── Actions shared by HTTP and MQTT ────────────────────────────────────────────
CTX = {"remote_mgr": None, "radio": None, "tx_mode": "fifo", "gdo0": 24}


class LockedError(Exception):
    """Door command refused because the remote lock is on."""


def transmit(rolling: int, fixed: int, repeats: int, delay: float = 0.025):
    """Send one Security+ 1.0 press (N bursts) in the configured TX mode."""
    radio = CTX["radio"]
    with radio_lock:
        if CTX["tx_mode"] == "async":
            bits = generate_burst_bits(rolling, fixed)
            radio.transmit_security_plus_async([bits] * repeats, gdo0_gpio=CTX["gdo0"], inter_burst_delay=delay)
        else:
            burst = generate_burst_bytes(rolling, fixed)
            radio.transmit_security_plus([burst] * repeats, inter_burst_delay=delay)
        refresh_hw_state(radio)


def default_repeats(value=None, default=None) -> int:
    r = value or default or CTX["remote_mgr"].config.get("repeats_per_press", 8)
    return max(1, min(32, int(r)))


_last_door_cmd = {"source": None, "at": 0.0}


def press_door(repeats=None, source: str = None) -> dict:
    """One door-button press (Security+ 1.0 is a toggle). Raises LockedError."""
    with state_lock:
        if door_info["locked"]:
            raise LockedError("Remote lock is on")
    mgr = CTX["remote_mgr"]
    rolling, fixed = mgr.get_next_codes()
    transmit(rolling, fixed, default_repeats(repeats))
    _last_door_cmd.update(source=source or "Halo", at=time.time())
    new_state = start_door_travel()
    new_rolling, _ = mgr.peek_codes()
    add_log("Door", f"Sent open/close from {source or 'Halo'} (code {rolling} → {new_rolling})", True)
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
    transmit(rolling, fixed, default_repeats(repeats))
    likely_on = not door_info["light_on"]
    set_light(likely_on, "Light", f"Toggled from {source or 'Halo'} (now possibly {'on' if likely_on else 'off'})")
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
    add_log("Remote Lock", "Locked — door commands blocked" if locked else "Unlocked", True)
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
NOTIFY = {"enabled": False, "server": "https://ntfy.sh", "topic": None,
          "left_open_minutes": 10, "app_url": None,
          "on_open": True, "on_close": True}
_notify_lock = threading.Lock()


def notify_public() -> dict:
    with _notify_lock:
        n = dict(NOTIFY)
    n["subscribe_url"] = f"{n['server'].rstrip('/')}/{n['topic']}" if n["topic"] else None
    return n


def configure_notifications(**changes) -> dict:
    with _notify_lock:
        for k in ("enabled", "on_open", "on_close"):
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


def send_notification(title: str, message: str, priority: str = "default",
                      tags: str = "house", close_action: bool = False, force: bool = False):
    """Fire-and-forget push to ntfy. Retries for ~1 minute if the internet is down."""
    with _notify_lock:
        n = dict(NOTIFY)
    if not n["topic"] or not (n["enabled"] or force):
        return False
    headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"),
               "Priority": priority, "Tags": tags}
    if n["app_url"]:
        headers["Click"] = n["app_url"]
        if close_action:
            headers["Actions"] = f"http, Close door, {n['app_url']}/api/close?source=Notification, method=POST, clear=true"
    url = f"{n['server']}/{n['topic']}"

    def worker():
        for delay in (0, 5, 15, 40):
            time.sleep(delay)
            try:
                req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=8) as r:
                    if r.status < 300:
                        return
            except Exception as e:
                logger.info(f"[Notify] Send failed ({e}); retrying")
        logger.warning(f"[Notify] Gave up sending: {title}")

    threading.Thread(target=worker, daemon=True).start()
    return True


def _fmt_minutes(sec: float) -> str:
    m = int(round(sec / 60))
    return "under a minute" if m < 1 else f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


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
        if prev_closed and not closed:
            opened_at, reminders = now, 0
            src = _last_door_cmd["source"] if now - _last_door_cmd["at"] < 30 else (
                "remote or wall button" if sensor_live else None)
            if NOTIFY["on_open"]:
                send_notification("Garage door opened", f"Opened{' from ' + src if src else ''}.",
                                  tags="door,warning")
        elif not prev_closed and closed:
            if NOTIFY["on_close"]:
                how_long = f" after {_fmt_minutes(now - opened_at)}" if opened_at else ""
                send_notification("Garage door closed", f"Closed{how_long}.", tags="door,white_check_mark")
            opened_at = None
        prev_closed = closed
        limit = NOTIFY["left_open_minutes"] * 60
        if opened_at and limit and reminders < 3 and now - opened_at >= limit * (reminders + 1):
            reminders += 1
            send_notification("Garage door left open",
                              f"The garage door has been open for {_fmt_minutes(now - opened_at)}.",
                              priority="high", tags="rotating_light", close_action=True)


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

    def transmit(self, rolling: int, fixed: int, repeats: int, delay: float = 0.025):
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
            self.send_json({"success": False, "error": "Unknown API endpoint"}, 404)

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
            else "public, max-age=86400"
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
                "enabled", "server", "topic", "left_open_minutes", "app_url", "on_open", "on_close")})
        except (ValueError, TypeError) as e:
            return self.send_json({"success": False, "error": str(e)}, 400)
        add_log("Notifications", "Door alerts on" if n["enabled"] else "Door alerts off", True)
        self.send_json({"success": True, "notifications": n})

    def handle_notifications_test(self, data: dict):
        if data.get("app_url"):
            configure_notifications(app_url=data["app_url"])
        ok = send_notification("Halo test", "Door alerts are working. You'll be notified when the garage door "
                               "opens, closes or is left open.", tags="tada", force=True)
        if not ok:
            return self.send_json({"success": False, "error": "Turn on door alerts first"}, 400)
        self.send_json({"success": True, "message": "Test notification sent"})

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
            self.transmit(rolling, fixed, self._repeats(data, 16), delay=0.03)
        except Exception as e:
            return self._fail("Pair Door", e)
        new_rolling, _ = self.remote_mgr.peek_codes()
        add_log("Pair Door", f"Sent pairing bursts (ID {fixed:X}, code {rolling})", True)
        self.send_json({"success": True, "fixed_code": fixed, "next_rolling_code": new_rolling})

    def handle_pair_light(self, data: dict):
        rolling, fixed = self.remote_mgr.get_button_codes(button=1)
        try:
            self.transmit(rolling, fixed, self._repeats(data, 24), delay=0.03)
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
        "sensor": sensor,
        "car": dict(d["car"]),
        "motion": dict(d["motion"]),
        "security": {"remote_lock": d["locked"]},
        "hardware": {
            "connected": not radio.is_mock,
            "is_simulation": radio.is_mock,
            **hw_info,
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


def run_server(host: str = "0.0.0.0", port: int = 8080, config_path: str = None,
               spi_bus: int = 0, spi_device: int = 0,
               tx_mode: str = "fifo", gdo0_gpio: int = 24):
    os.makedirs(WEB_DIR, exist_ok=True)
    load_door_state()

    remote_mgr = RemoteManager(config_path) if config_path else RemoteManager()
    radio = CC1101(bus=spi_bus, device=spi_device)
    radio.init_spi()
    if tx_mode == "async":
        radio.setup_async_ook_315mhz()
    else:
        radio.setup_security_plus_315mhz(packet_len=50)
    refresh_hw_state(radio, full=True)

    saved_n = remote_mgr.config.get("notifications") or {}
    with _notify_lock:
        NOTIFY.update({k: v for k, v in saved_n.items() if k in NOTIFY})
    threading.Thread(target=_door_notifier, daemon=True).start()
    SENSORS["door"] = _norm_mac(remote_mgr.config.get("sensor_mac"))
    SENSORS["car"] = _norm_mac(remote_mgr.config.get("car_sensor_mac"))
    for which, (info_key, cfg_key) in SENSOR_NAME_KEYS.items():
        if remote_mgr.config.get(cfg_key):
            door_info[info_key]["name"] = str(remote_mgr.config[cfg_key])[:32]
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
    server = ThreadingHTTPServer((host, port), HaloRequestHandler)

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

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Halo...")
    finally:
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
