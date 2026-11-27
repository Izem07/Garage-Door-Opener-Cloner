"""
Lightweight Web UI & REST API Server for Garage Door Opener.
Runs on localhost / local network (e.g. http://<pi-ip>:8080).
Zero external dependencies (uses standard library http.server.ThreadingHTTPServer).
"""

import os
import json
import time
import socket
import logging
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from cc1101 import CC1101
from security_plus import generate_burst_bytes, generate_burst_bits
from remote_manager import RemoteManager

logger = logging.getLogger("GarageWeb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# Thread-safe lock for SPI radio hardware
radio_lock = threading.Lock()

# State file and thread safety
STATE_FILE = os.path.join(BASE_DIR, "garage_state.json")
DOCKER_STATE_FILE = "/var/lib/docker/volumes/homebridge_data/_data/garage_state.json"
state_lock = threading.Lock()

door_info = {
    "state": "CLOSED",
    "percent": 0,
    "travel_time": 12.0,
    "light_on": False,
    "light_timer_expires": 0,
    "locked": False,
    "sensor": {
        "mac": "77CB8338",
        "state": "closed",
        "battery": 98,
        "signal_strength": -32,
        "last_seen": "Startup",
        "connected": True,
    },
    "last_action": "Initialized",
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
}

def _background_light_timer():
    """Background checker for Chamberlain 4.5m courtesy light auto-off."""
    while True:
        time.sleep(2)
        with state_lock:
            expires = door_info.get("light_timer_expires", 0)
            if expires > 0 and time.time() >= expires and door_info.get("light_on", False):
                door_info["light_on"] = False
                door_info["light_timer_expires"] = 0
                save_door_state()
                add_log("Opener Light", "Courtesy light auto-off after 4.5m timer", success=True)
                logger.info("[Light] Auto-off timer elapsed, light turned off")

threading.Thread(target=_background_light_timer, daemon=True).start()

def load_door_state():
    global door_info
    # Try local state file first, then docker state file
    target_files = [STATE_FILE, DOCKER_STATE_FILE]
    for sf in target_files:
        if os.path.exists(sf):
            try:
                with open(sf, "r") as f:
                    saved = json.load(f)
                    door_info["state"] = saved.get("doorState", saved.get("state", "CLOSED"))
                    door_info["percent"] = 100 if door_info["state"] == "OPEN" else 0
                    door_info["light_on"] = saved.get("lightOn", saved.get("light_on", False))
                    door_info["locked"] = saved.get("locked", False)
                    break
            except Exception as e:
                logger.warning(f"Could not load state from {sf}: {e}")

def save_door_state():
    state_payload = {
        "doorState": door_info["state"],
        "percent": door_info["percent"],
        "lightOn": door_info["light_on"],
        "locked": door_info["locked"],
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for sf in (STATE_FILE, DOCKER_STATE_FILE):
        try:
            parent = os.path.dirname(sf)
            if os.path.exists(parent):
                with open(sf, "w") as f:
                    json.dump(state_payload, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save state to {sf}: {e}")

load_door_state()

def parse_sensor_payload(data):
    """
    Parses various contact/door sensor payloads (Wyze, Zigbee2MQTT, Homebridge, etc.)
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
            return not c  # contact True = magnet touching = door CLOSED
        s = str(c).strip().lower()
        if s in ("true", "1", "closed"):
            return False
        if s in ("false", "0", "open"):
            return True
    return None

def start_mqtt_client(broker_host: str = "127.0.0.1", broker_port: int = 1883, topic: str = "wyzesense2mqtt/#"):
    """
    Subscribes to local Mosquitto MQTT broker for Wyze Sense sensor events.
    Compatible with both paho-mqtt v1 and v2.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.info("[MQTT] paho-mqtt not installed. Optional: run 'sudo apt install -y python3-paho-mqtt' on Pi.")
        return

    def on_connect(*args, **kwargs):
        # Compatible with paho-mqtt v1 (4 args) and v2 (5 args)
        rc = args[3] if len(args) > 3 else 0
        if rc == 0:
            logger.info(f"[MQTT] Connected to broker {broker_host}:{broker_port}. Subscribing to {topic}")
            client.subscribe(topic)
            client.subscribe("wyzesense/#")
            client.subscribe("home/garage/#")
        else:
            logger.warning(f"[MQTT] Broker connection failed with result code {rc}")

    def on_message(client, userdata, msg):
        try:
            payload_str = msg.payload.decode("utf-8").strip()
            data = {}
            try:
                data = json.loads(payload_str)
            except Exception:
                try:
                    import ast
                    data = ast.literal_eval(payload_str)
                except Exception:
                    data = {"state": payload_str}

            dev_id = data.get("mac", msg.topic.split("/")[-1])
            battery = data.get("battery")
            sig = data.get("signal_strength")

            is_open = parse_sensor_payload(data)
            if is_open is not None:
                with state_lock:
                    door_info["state"] = "OPEN" if is_open else "CLOSED"
                    door_info["percent"] = 100 if is_open else 0
                    door_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if "sensor" not in door_info:
                        door_info["sensor"] = {}
                    door_info["sensor"]["mac"] = dev_id
                    door_info["sensor"]["state"] = "open" if is_open else "closed"
                    if battery is not None:
                        door_info["sensor"]["battery"] = battery
                    if sig is not None:
                        door_info["sensor"]["signal_strength"] = sig
                    door_info["sensor"]["last_seen"] = time.strftime("%H:%M:%S")
                    door_info["sensor"]["connected"] = True

                    # Real Chamberlain openers turn light ON whenever the door operates
                    if not door_info.get("light_on", False):
                        door_info["light_on"] = True
                        door_info["light_timer_expires"] = time.time() + 270
                    save_door_state()

                st_str = "OPEN" if is_open else "CLOSED"
                msg_txt = f"Wyze Ground-Truth Sensor ({dev_id}) reported: {st_str} (RSSI: {sig} dBm, Batt: {battery}%)"
                add_log("Sensor Update", msg_txt, success=True)
                logger.info(f"[MQTT] {msg_txt}")
        except Exception as e:
            logger.error(f"[MQTT] Error processing message: {e}")

    try:
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gdo_wyze_sync")
        except AttributeError:
            client = mqtt.Client(client_id="gdo_wyze_sync")
        client.on_connect = on_connect
        client.on_message = on_message
        client.connect_async(broker_host, broker_port, 60)
        client.loop_start()
        logger.info(f"[MQTT] Background listener started for {broker_host}:{broker_port}")
    except Exception as e:
        logger.warning(f"[MQTT] Could not connect to MQTT broker: {e}")

def _run_door_travel(target_state: str, duration: float):
    global door_info
    with state_lock:
        # Chamberlain openers illuminate courtesy light during door operation
        if not door_info.get("light_on", False):
            door_info["light_on"] = True
            door_info["light_timer_expires"] = time.time() + 270
            save_door_state()
            add_log("Opener Light", "Courtesy light illuminated by door movement (4.5m timer)", success=True)

    start_percent = door_info["percent"]
    end_percent = 100 if target_state == "OPEN" else 0
    steps = int(duration * 10)
    step_duration = duration / steps if steps > 0 else 0.1
    delta = (end_percent - start_percent) / steps if steps > 0 else 0

    for i in range(steps):
        time.sleep(step_duration)
        with state_lock:
            if target_state == "OPEN" and door_info["state"] != "OPENING":
                return
            if target_state == "CLOSED" and door_info["state"] != "CLOSING":
                return
            new_pct = int(start_percent + delta * (i + 1))
            door_info["percent"] = max(0, min(100, new_pct))

    with state_lock:
        door_info["state"] = target_state
        door_info["percent"] = end_percent
        door_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_door_state()
        logger.info(f"Door travel finished: {target_state}")

# Rolling activity log
activity_log = []
MAX_LOG_ENTRIES = 50


def add_log(action: str, details: str = "", success: bool = True):
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "details": details,
        "success": success,
    }
    activity_log.insert(0, entry)
    if len(activity_log) > MAX_LOG_ENTRIES:
        activity_log.pop()


class GarageRequestHandler(BaseHTTPRequestHandler):
    remote_mgr: RemoteManager = None
    radio: CC1101 = None
    tx_mode: str = "fifo"
    gdo0_gpio: int = 24

    def log_message(self, format, *args):
        # Override to keep server output clean
        logger.info("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))

    def send_json(self, data: dict, status_code: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        if path == "/api/status":
            self.handle_api_status()
            return
        elif path == "/api/logs":
            self.send_json({"logs": activity_log})
            return

        # Serve static web files
        if path in ("", "/"):
            file_path = os.path.join(WEB_DIR, "index.html")
        else:
            rel_path = path.lstrip("/")
            file_path = os.path.join(WEB_DIR, rel_path)

        # Sanitize path against directory traversal
        file_path = os.path.abspath(file_path)
        if not file_path.startswith(WEB_DIR) or not os.path.isfile(file_path):
            self.send_error(404, "File Not Found")
            return

        # Content types
        ext = os.path.splitext(file_path)[1].lower()
        mime_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        content_type = mime_types.get(ext, "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path

        # Parse request body
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        data = {}
        if body:
            try:
                data = json.loads(body)
            except Exception:
                pass

        if path in ("/api/toggle", "/api/open", "/api/close"):
            self.handle_api_toggle(data)
        elif path in ("/api/sensor", "/api/state"):
            self.handle_api_sensor(data)
        elif path == "/api/light":
            self.handle_api_light(data)
        elif path == "/api/light/state":
            self.handle_api_light_state(data)
        elif path == "/api/lock":
            self.handle_api_lock(data)
        elif path == "/api/pair-light":
            self.handle_api_pair_light(data)
        elif path == "/api/pair":
            self.handle_api_pair(data)
        elif path == "/api/set-counter":
            self.handle_api_set_counter(data)
        elif path == "/api/reset-remote":
            self.handle_api_reset_remote(data)
        elif path == "/api/test-rf":
            self.handle_api_test_rf(data)
        else:
            self.send_error(404, "Unknown API endpoint")

    def handle_api_status(self):
        rolling, fixed = self.remote_mgr.peek_codes()
        is_mock = self.radio.is_mock
        part = 0
        ver = 0
        state = 0
        if not is_mock:
            with radio_lock:
                try:
                    part = self.radio.get_partnum()
                    ver = self.radio.get_version()
                    state = self.radio.get_state()
                except Exception as e:
                    logger.error(f"Error checking radio status: {e}")

        with state_lock:
            cur_door_state = door_info["state"]
            cur_door_percent = door_info["percent"]
            cur_light_on = door_info["light_on"]
            cur_expires = door_info.get("light_timer_expires", 0)
            remaining_sec = max(0, int(cur_expires - time.time())) if cur_light_on and cur_expires > 0 else 0
            cur_locked = door_info["locked"]
            sensor_data = dict(door_info.get("sensor", {}))

        payload = {
            "name": self.remote_mgr.config.get("name", "Chamberlain Purple 315MHz Remote"),
            "frequency_mhz": self.remote_mgr.config.get("frequency_mhz", 315.0),
            "protocol": "Chamberlain Security+ 1.0 (Purple Learn Button)",
            "fixed_code": fixed,
            "fixed_code_hex": f"0x{fixed:X}",
            "rolling_code": rolling,
            "rolling_code_hex": f"0x{rolling:X}",
            "repeats": self.remote_mgr.config.get("repeats_per_press", 8),
            "door": {
                "state": cur_door_state,
                "percent": cur_door_percent,
                "travel_time": door_info.get("travel_time", 12.0),
                "motor": "Moving" if cur_door_state in ("OPENING", "CLOSING") else "Idle",
                "obstruction": "Clear",
                "wall_button": "Released",
                "synced": True,
            },
            "light": {
                "on": cur_light_on,
                "auto_off_remaining_sec": remaining_sec,
            },
            "sensor": sensor_data,
            "security": {
                "remote_lock": cur_locked,
            },
            "hardware": {
                "connected": not is_mock,
                "is_simulation": is_mock,
                "part_number": f"0x{part:02X}",
                "version": f"0x{ver:02X}",
                "state": f"0x{state:02X}",
                "rssi_dbm": -51,
                "link_quality": 98,
            },
            "recent_logs": activity_log[:20],
        }
        self.send_json(payload)

    def handle_api_toggle(self, data: dict):
        repeats = data.get("repeats") or self.remote_mgr.config.get("repeats_per_press", 8)
        rolling, fixed = self.remote_mgr.get_next_codes()

        try:
            with radio_lock:
                if self.tx_mode == "async":
                    bits = generate_burst_bits(rolling, fixed)
                    self.radio.transmit_security_plus_async(
                        [bits] * repeats, gdo0_gpio=self.gdo0_gpio,
                        inter_burst_delay=0.025
                    )
                else:
                    burst = generate_burst_bytes(rolling, fixed)
                    self.radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.025)

            new_rolling, _ = self.remote_mgr.peek_codes()

            with state_lock:
                cur_st = door_info["state"]
                if cur_st in ("CLOSED", "CLOSING"):
                    target_st = "OPEN"
                    door_info["state"] = "OPENING"
                elif cur_st in ("OPEN", "OPENING"):
                    target_st = "CLOSED"
                    door_info["state"] = "CLOSING"
                else:
                    target_st = "OPEN" if door_info["percent"] < 50 else "CLOSED"
                    door_info["state"] = "OPENING" if target_st == "OPEN" else "CLOSING"
                door_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
                save_door_state()

            # Start asynchronous motion thread
            t = threading.Thread(
                target=_run_door_travel,
                args=(target_st, door_info.get("travel_time", 12.0)),
                daemon=True
            )
            t.start()

            msg = f"Transmitted Open/Close (Rolling Code: {rolling} -> Next: {new_rolling})"
            add_log("Toggle Door", msg, success=True)
            self.send_json({
                "success": True,
                "message": msg,
                "door_state": door_info["state"],
                "door_percent": door_info["percent"],
                "sent_rolling_code": rolling,
                "next_rolling_code": new_rolling,
                "fixed_code": fixed,
            })
        except Exception as e:
            err_msg = f"Failed to transmit: {e}"
            logger.error(err_msg)
            add_log("Toggle Door", err_msg, success=False)
            self.send_json({"success": False, "error": err_msg}, status_code=500)

    def handle_api_sensor(self, data: dict):
        is_open = parse_sensor_payload(data)
        if is_open is None:
            self.send_json({
                "success": False,
                "error": "Unrecognized sensor payload. Provide {'state': 'open'|'closed'} or {'contact': true|false}"
            }, status_code=400)
            return

        with state_lock:
            door_info["state"] = "OPEN" if is_open else "CLOSED"
            door_info["percent"] = 100 if is_open else 0
            door_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            save_door_state()

        st_str = "OPEN" if is_open else "CLOSED"
        dev_name = data.get("device", data.get("mac", "Wyze Contact Sensor"))
        msg = f"Wyze sensor ({dev_name}) ground-truth: {st_str}"
        add_log("Sensor Update", msg, success=True)
        logger.info(f"[Sensor API] {msg}")
        self.send_json({
            "success": True,
            "door_state": door_info["state"],
            "door_percent": door_info["percent"],
            "message": msg
        })

    def handle_api_light(self, data: dict):
        repeats = data.get("repeats") or self.remote_mgr.config.get("repeats_per_press", 8)
        rolling, fixed = self.remote_mgr.get_button_codes(button=1)

        try:
            with radio_lock:
                if self.tx_mode == "async":
                    bits = generate_burst_bits(rolling, fixed)
                    self.radio.transmit_security_plus_async(
                        [bits] * repeats, gdo0_gpio=self.gdo0_gpio,
                        inter_burst_delay=0.025
                    )
                else:
                    burst = generate_burst_bytes(rolling, fixed)
                    self.radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.025)

            with state_lock:
                door_info["light_on"] = not door_info["light_on"]
                if door_info["light_on"]:
                    door_info["light_timer_expires"] = time.time() + 270 # 4.5 minutes auto-off
                else:
                    door_info["light_timer_expires"] = 0
                save_door_state()
                cur_light = door_info["light_on"]

            new_rolling, _ = self.remote_mgr.peek_codes()
            msg = f"Opener Light turned {'ON' if cur_light else 'OFF'} (Rolling {rolling})"
            add_log("Toggle Light", msg, success=True)
            self.send_json({
                "success": True,
                "message": msg,
                "light_on": cur_light,
                "auto_off_remaining_sec": 270 if cur_light else 0,
                "sent_rolling_code": rolling,
                "next_rolling_code": new_rolling,
                "fixed_code": fixed,
            })
        except Exception as e:
            err_msg = f"Failed to toggle light: {e}"
            logger.error(err_msg)
            add_log("Toggle Light", err_msg, success=False)
            self.send_json({"success": False, "error": err_msg}, status_code=500)

    def handle_api_light_state(self, data: dict):
        new_on = bool(data.get("on", False))
        with state_lock:
            door_info["light_on"] = new_on
            if new_on:
                door_info["light_timer_expires"] = time.time() + 270
            else:
                door_info["light_timer_expires"] = 0
            save_door_state()
        msg = f"Opener Light state updated to {'ON' if new_on else 'OFF'} by sensor/integration"
        add_log("Light Sensor", msg, success=True)
        self.send_json({"success": True, "light_on": new_on, "message": msg})

    def handle_api_lock(self, data: dict):
        with state_lock:
            door_info["locked"] = not door_info["locked"]
            save_door_state()
            locked_st = door_info["locked"]

        status_text = "Locked" if locked_st else "Unlocked"
        msg = f"Remote Lock toggled to {status_text}"
        add_log("Remote Lock", msg, success=True)
        self.send_json({
            "success": True,
            "message": msg,
            "locked": locked_st,
        })

    def handle_api_pair_light(self, data: dict):
        repeats = data.get("repeats") or 8
        rolling, fixed = self.remote_mgr.get_button_codes(button=1)

        try:
            with radio_lock:
                if self.tx_mode == "async":
                    bits = generate_burst_bits(rolling, fixed)
                    self.radio.transmit_security_plus_async(
                        [bits] * repeats, gdo0_gpio=self.gdo0_gpio,
                        inter_burst_delay=0.03
                    )
                else:
                    burst = generate_burst_bytes(rolling, fixed)
                    self.radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.03)
            new_rolling, _ = self.remote_mgr.peek_codes()
            msg = f"Sent Light Pairing (Button 1 Fixed ID: {fixed}, Rolling Counter: {rolling})"
            add_log("Pair Light", msg, success=True)
            self.send_json({
                "success": True,
                "message": msg,
                "fixed_code": fixed,
                "next_rolling_code": new_rolling,
            })
        except Exception as e:
            err_msg = f"Light pairing failed: {e}"
            logger.error(err_msg)
            add_log("Pair Light", err_msg, success=False)
            self.send_json({"success": False, "error": err_msg}, status_code=500)

    def handle_api_pair(self, data: dict):
        repeats = data.get("repeats") or 8
        rolling, fixed = self.remote_mgr.get_next_codes()

        try:
            burst = generate_burst_bytes(rolling, fixed)
            with radio_lock:
                self.radio.transmit_security_plus([burst] * repeats, inter_burst_delay=0.03)
            new_rolling, _ = self.remote_mgr.peek_codes()
            msg = f"Sent Pairing Bursts (Fixed ID: {fixed}, Rolling Counter: {rolling})"
            add_log("Pair Remote", msg, success=True)
            self.send_json({
                "success": True,
                "message": msg,
                "fixed_code": fixed,
                "next_rolling_code": new_rolling,
            })
        except Exception as e:
            err_msg = f"Pairing failed: {e}"
            logger.error(err_msg)
            add_log("Pair Remote", err_msg, success=False)
            self.send_json({"success": False, "error": err_msg}, status_code=500)

    def handle_api_set_counter(self, data: dict):
        new_val = data.get("counter")
        if new_val is None:
            self.send_json({"success": False, "error": "Missing 'counter' field"}, status_code=400)
            return

        try:
            val_int = int(new_val)
            self.remote_mgr.set_rolling_code(val_int)
            msg = f"Rolling counter manually set to {val_int}"
            add_log("Set Counter", msg, success=True)
            self.send_json({"success": True, "message": msg, "rolling_code": val_int})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)}, status_code=400)

    def handle_api_reset_remote(self, data: dict):
        start = data.get("start_counter", 1000)
        new_fixed, new_rolling = self.remote_mgr.reset_remote(start_rolling=start)
        msg = f"Generated new virtual remote ID {new_fixed} (Starting counter: {new_rolling})"
        add_log("Reset Remote", msg, success=True)
        self.send_json({
            "success": True,
            "message": msg,
            "fixed_code": new_fixed,
            "rolling_code": new_rolling,
        })

    def handle_api_test_rf(self, data: dict):
        duration = float(data.get("duration", 3.0))
        burst = generate_burst_bytes(12345, 67890)
        start_time = time.time()
        count = 0
        with radio_lock:
            while time.time() - start_time < duration:
                self.radio.transmit_burst(burst)
                time.sleep(0.02)
                count += 1
        msg = f"Sent {count} test bursts over {duration}s on 315.00 MHz"
        add_log("RF Test", msg, success=True)
        self.send_json({"success": True, "message": msg, "bursts_sent": count})


def get_local_ip() -> str:
    """Detect local IP address for convenient display."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_server(host: str = "0.0.0.0", port: int = 8080, config_path: str = None,
               spi_bus: int = 0, spi_device: int = 0,
               tx_mode: str = "fifo", gdo0_gpio: int = 24):
    if not os.path.exists(WEB_DIR):
        os.makedirs(WEB_DIR, exist_ok=True)

    remote_mgr = RemoteManager(config_path) if config_path else RemoteManager()

    radio = CC1101(bus=spi_bus, device=spi_device)
    radio.init_spi()
    if tx_mode == "async":
        radio.setup_async_ook_315mhz()
    else:
        radio.setup_security_plus_315mhz(packet_len=50)

    GarageRequestHandler.remote_mgr = remote_mgr
    GarageRequestHandler.radio = radio
    GarageRequestHandler.tx_mode = tx_mode
    GarageRequestHandler.gdo0_gpio = gdo0_gpio

    # Optional MQTT subscriber for Wyze contact sensor
    mqtt_cfg = remote_mgr.config.get("mqtt", {})
    if mqtt_cfg.get("enabled", True):
        start_mqtt_client(
            broker_host=mqtt_cfg.get("broker", "127.0.0.1"),
            broker_port=int(mqtt_cfg.get("port", 1883)),
            topic=mqtt_cfg.get("topic", "wyzesense2mqtt/#")
        )

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), GarageRequestHandler)

    local_ip = get_local_ip()
    print("=" * 65)
    print("  GARAGE DOOR OPENER - LOCALHOST WEB UI & REST API")
    print("=" * 65)
    print(f"  Local Access  : http://localhost:{port}/")
    print(f"  Network Access: http://{local_ip}:{port}/")
    print("=" * 65)
    print("  Open the Network URL from your phone or PC on the same Wi-Fi!")
    print("  Press Ctrl+C to stop the server.\n")

    add_log("Server Start", f"Web UI started on port {port}", success=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        server.server_close()
        radio.close()
        print("Server stopped.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Garage Door Opener Web UI Server")
    parser.add_argument("--host", default="0.0.0.0", help="Binding host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--config", default=None, help="Path to remote_config.json")
    parser.add_argument("--spi-bus", type=int, default=0, help="SPI bus (default: 0)")
    parser.add_argument("--spi-device", type=int, default=0, help="SPI device (default: 0)")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, config_path=args.config, spi_bus=args.spi_bus, spi_device=args.spi_device)
