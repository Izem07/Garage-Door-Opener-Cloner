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

        payload = {
            "name": self.remote_mgr.config.get("name", "Chamberlain Purple 315MHz Remote"),
            "frequency_mhz": self.remote_mgr.config.get("frequency_mhz", 315.0),
            "protocol": "Chamberlain Security+ 1.0 (Purple Learn Button)",
            "fixed_code": fixed,
            "fixed_code_hex": f"0x{fixed:X}",
            "rolling_code": rolling,
            "rolling_code_hex": f"0x{rolling:X}",
            "repeats": self.remote_mgr.config.get("repeats_per_press", 8),
            "hardware": {
                "connected": not is_mock,
                "is_simulation": is_mock,
                "part_number": f"0x{part:02X}",
                "version": f"0x{ver:02X}",
                "state": f"0x{state:02X}",
            },
            "recent_logs": activity_log[:10],
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
            msg = f"Transmitted Open/Close (Rolling Code: {rolling} -> Next: {new_rolling})"
            add_log("Toggle Door", msg, success=True)
            self.send_json({
                "success": True,
                "message": msg,
                "sent_rolling_code": rolling,
                "next_rolling_code": new_rolling,
                "fixed_code": fixed,
            })
        except Exception as e:
            err_msg = f"Failed to transmit: {e}"
            logger.error(err_msg)
            add_log("Toggle Door", err_msg, success=False)
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
