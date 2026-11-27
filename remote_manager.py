"""
Remote Configuration and Rolling Code State Manager.
Persists the virtual remote ID and rolling code counter to disk.
"""

import json
import os
import random
import threading
import time
from typing import Tuple, Dict, Any

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "remote_config.json")
MAX_FIXED_CODE = 3**20 - 1  # 3,486,784,400
MAX_ROLLING_CODE = 2**32 - 1


def generate_handheld_remote_id(button: int = 0) -> int:
    """
    Generate a valid Security+ 1.0 Handheld Remote Fixed Code.
    
    Structure in Security+ 1.0:
      button = fixed % 3          (0 = Large Main Door Button, 1 = Left, 2 = Right)
      id0    = (fixed // 3) % 3   (Sub-identifier)
      id1    = (fixed // 9) % 3   (Device Type: 0 = Keypad, 1 or 2 = Handheld Remote)
      remote = fixed // 27
    
    CRITICAL:
    1. button MUST be 0 (Large button). On models like 248739, button 1 is the
       small accessory/light button, while button 0 is the primary door opener.
    2. id1 MUST be 1 (Handheld remote, NOT a keypad).
    """
    remote_base = random.randint(10000, 100000000)
    btn = button % 3
    id0 = 0
    id1 = 1     # Handheld remote (NOT a keypad!)
    return (remote_base * 27) + (id1 * 9) + (id0 * 3) + btn


class RemoteManager:
    """
    Manages persistent remote settings and rolling code counter.

    Safety rules (the remote ID and counter are what the opener is paired to):
      * Writes are atomic (temp file + rename), so a reader never sees half a file.
      * An existing but unreadable config is NEVER replaced with a new remote ID.
      * The file is only re-read when it changed on disk, and only re-written
        when something actually changed, which spares the Pi's SD card.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.lock = threading.RLock()
        self._mtime = None
        self.config = self.load_config()

    def _read_file(self) -> Dict[str, Any]:
        last_err = None
        for _ in range(5):                  # tolerate a write from another process in flight
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (ValueError, OSError) as e:
                last_err = e
                time.sleep(0.05)
        raise RuntimeError(f"Cannot read {self.config_path}: {last_err}. "
                           "Fix or restore the file; Halo will not overwrite it with a new remote ID.")

    def load_config(self) -> Dict[str, Any]:
        """Load remote configuration from disk, or create one only if none exists."""
        with self.lock:
            if os.path.exists(self.config_path):
                cfg = self._read_file()
                if "fixed_code" not in cfg or "rolling_code" not in cfg:
                    raise RuntimeError(f"{self.config_path} is missing fixed_code/rolling_code; not overwriting it.")
                before = json.dumps(cfg, sort_keys=True)
                fixed = int(cfg["fixed_code"])
                # Enforce id1 != 0 (remote not keypad) and button = 0 (large door button)
                if (fixed // 9) % 3 == 0 or (fixed % 3) != 0:
                    cfg["fixed_code"] = ((fixed // 27) * 27) + (1 * 9) + (0 * 3) + 0
                if cfg.get("step", 2) < 2:
                    cfg["step"] = 2
                if cfg.get("repeats_per_press", 8) < 8:
                    cfg["repeats_per_press"] = 8
                if json.dumps(cfg, sort_keys=True) != before:
                    self.save_config(cfg)
                else:
                    self._mtime = self._stat()
                return cfg

            # First run: generate a new unique remote configuration
            cfg = {
                "name": "Halo Virtual Remote",
                "frequency_mhz": 315.00,
                "fixed_code": generate_handheld_remote_id(button=0),
                "rolling_code": 1000,
                "repeats_per_press": 8,
                "step": 2,
            }
            self.save_config(cfg)
            return cfg

    def _stat(self):
        try:
            st = os.stat(self.config_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def save_config(self, cfg: Dict[str, Any] = None):
        """Atomically save configuration (temp file + rename, fsynced)."""
        with self.lock:
            if cfg is None:
                cfg = self.config
            tmp = self.config_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)
            self._mtime = self._stat()

    def reload(self):
        """Re-read the config only if another process (e.g. the CLI) changed it."""
        with self.lock:
            if self._stat() != self._mtime:
                self.config = self.load_config()

    def update(self, **values):
        """Change top-level settings and save them (thread-safe)."""
        with self.lock:
            self.reload()
            self.config.update(values)
            self.save_config()

    def peek_codes(self) -> Tuple[int, int]:
        """View the current (rolling_code, fixed_code) without incrementing."""
        with self.lock:
            self.reload()
            return int(self.config["rolling_code"]), int(self.config["fixed_code"])

    def get_next_codes(self) -> Tuple[int, int]:
        """
        Get the current (rolling_code, fixed_code) and increment the rolling counter.
        The incremented counter is persisted to disk immediately.
        """
        return self.get_button_codes(button=0)

    def get_button_codes(self, button: int = 0) -> Tuple[int, int]:
        """
        Get the current (rolling_code, fixed_code) for a specific button
        (0 = Door, 1 = Light / Accessory, 2 = Right), and advance the shared rolling counter.
        """
        with self.lock:
            self.reload()
            rolling = int(self.config["rolling_code"])
            fixed = int(self.config["fixed_code"])
            step = int(self.config.get("step", 2))

            # Derive button-specific fixed ID using the same remote serial base
            remote_base = fixed // 27
            btn_fixed = (remote_base * 27) + (1 * 9) + (0 * 3) + (button % 3)

            # Advance rolling code (persisted before transmitting, so a crash never reuses a code)
            next_rolling = (rolling + step) % (MAX_ROLLING_CODE + 1)
            self.config["rolling_code"] = next_rolling
            self.save_config()

            return rolling, btn_fixed

    def reset_remote(self, fixed_code: int = None, start_rolling: int = 1000, button: int = 0):
        """Reset the virtual remote with a new fixed ID and initial rolling counter."""
        with self.lock:
            if fixed_code is None:
                fixed_code = generate_handheld_remote_id(button=button)
            self.config["fixed_code"] = fixed_code % (MAX_FIXED_CODE + 1)
            self.config["rolling_code"] = start_rolling % (MAX_ROLLING_CODE + 1)
            self.save_config()
            return self.config["fixed_code"], self.config["rolling_code"]

    def set_button(self, button_num: int):
        """Switch active button (0 = Large Main Door, 1 = Left, 2 = Right)."""
        with self.lock:
            self.reload()
            fixed = int(self.config["fixed_code"])
            remote_base = fixed // 27
            new_fixed = (remote_base * 27) + (1 * 9) + (0 * 3) + (button_num % 3)
            self.config["fixed_code"] = new_fixed
            self.save_config()
            return new_fixed

    def set_rolling_code(self, new_val: int):
        """Manually adjust rolling code."""
        with self.lock:
            self.reload()
            self.config["rolling_code"] = int(new_val) % (MAX_ROLLING_CODE + 1)
            self.save_config()

    def set_fixed_code(self, fixed_val: int):
        """Manually set remote fixed ID."""
        with self.lock:
            self.reload()
            self.config["fixed_code"] = int(fixed_val) % (MAX_FIXED_CODE + 1)
            self.save_config()
