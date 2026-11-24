"""
Remote Configuration and Rolling Code State Manager.
Persists the virtual remote ID and rolling code counter to disk.
"""

import json
import os
import random
from typing import Tuple, Dict, Any

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "remote_config.json")
MAX_FIXED_CODE = 3**20 - 1  # 3,486,784,400
MAX_ROLLING_CODE = 2**32 - 1


def generate_handheld_remote_id() -> int:
    """
    Generate a valid Security+ 1.0 Handheld Remote Fixed Code.
    
    Structure in Security+ 1.0:
      button = fixed % 3          (1 = Primary / Left button)
      id0    = (fixed // 3) % 3   (Sub-identifier)
      id1    = (fixed // 9) % 3   (Device Type: 0 = Keypad, 1 or 2 = Handheld Remote)
      remote = fixed // 27
    
    CRITICAL: id1 MUST NOT be 0! If id1 == 0, Chamberlain openers classify the device
    as a wireless keypad (requiring a 4-digit PIN), which causes the opener to reject
    the command and flash 10 times! Setting id1 = 1 guarantees it is recognized as a
    standard handheld remote.
    """
    remote_base = random.randint(10000, 100000000)
    button = 1  # Standard primary remote button
    id0 = 0
    id1 = 1     # Handheld remote (NOT a keypad!)
    return (remote_base * 27) + (id1 * 9) + (id0 * 3) + button


class RemoteManager:
    """Manages persistent remote settings and rolling code counter."""

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.config_path = config_path
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        """Load remote configuration from disk or initialize defaults."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if "fixed_code" in cfg and "rolling_code" in cfg:
                        fixed = int(cfg["fixed_code"])
                        # Validate that id1 != 0 so it's a remote and not a keypad
                        if (fixed // 9) % 3 == 0:
                            # Fix it by adding 9 so id1 becomes 1
                            fixed += 9
                            cfg["fixed_code"] = fixed
                        if cfg.get("step", 2) < 2:
                            cfg["step"] = 2
                        if cfg.get("repeats_per_press", 8) < 8:
                            cfg["repeats_per_press"] = 8
                        self.save_config(cfg)
                        return cfg
            except Exception:
                pass

        # Generate a new unique remote configuration
        cfg = {
            "name": "Chamberlain Purple 315MHz Virtual Remote",
            "frequency_mhz": 315.00,
            "fixed_code": generate_handheld_remote_id(),
            "rolling_code": 1000,
            "repeats_per_press": 8,
            "step": 2,
        }
        self.save_config(cfg)
        return cfg

    def save_config(self, cfg: Dict[str, Any] = None):
        """Save configuration to JSON file."""
        if cfg is None:
            cfg = self.config
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
            f.flush()
            os.fsync(f.fileno())

    def reload(self):
        """Reload configuration from disk to stay in sync with other processes."""
        self.config = self.load_config()

    def peek_codes(self) -> Tuple[int, int]:
        """View the current (rolling_code, fixed_code) without incrementing."""
        self.reload()
        return int(self.config["rolling_code"]), int(self.config["fixed_code"])

    def get_next_codes(self) -> Tuple[int, int]:
        """
        Get the current (rolling_code, fixed_code) and increment the rolling counter.
        The incremented counter is persisted to disk immediately.
        """
        self.reload()
        rolling = int(self.config["rolling_code"])
        fixed = int(self.config["fixed_code"])
        step = int(self.config.get("step", 2))

        # Advance rolling code
        next_rolling = (rolling + step) % (MAX_ROLLING_CODE + 1)
        self.config["rolling_code"] = next_rolling
        self.save_config()

        return rolling, fixed

    def reset_remote(self, fixed_code: int = None, start_rolling: int = 1000):
        """Reset the virtual remote with a new fixed ID and initial rolling counter."""
        if fixed_code is None:
            fixed_code = generate_handheld_remote_id()
        self.config["fixed_code"] = fixed_code % (MAX_FIXED_CODE + 1)
        self.config["rolling_code"] = start_rolling % (MAX_ROLLING_CODE + 1)
        self.save_config()
        return self.config["fixed_code"], self.config["rolling_code"]

    def set_rolling_code(self, new_val: int):
        """Manually adjust rolling code."""
        self.reload()
        self.config["rolling_code"] = int(new_val) % (MAX_ROLLING_CODE + 1)
        self.save_config()

    def set_fixed_code(self, fixed_val: int):
        """Manually set remote fixed ID."""
        self.reload()
        self.config["fixed_code"] = int(fixed_val) % (MAX_FIXED_CODE + 1)
        self.save_config()
