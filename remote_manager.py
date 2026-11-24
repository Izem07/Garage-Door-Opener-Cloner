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
                        return cfg
            except Exception:
                pass

        # Generate a new unique remote configuration
        cfg = {
            "name": "Chamberlain Purple 315MHz Virtual Remote",
            "frequency_mhz": 315.00,
            "fixed_code": random.randint(100000, MAX_FIXED_CODE),
            "rolling_code": 1000,
            "repeats_per_press": 4,
            "step": 1,
        }
        self.save_config(cfg)
        return cfg

    def save_config(self, cfg: Dict[str, Any] = None):
        """Save configuration to JSON file."""
        if cfg is None:
            cfg = self.config
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)

    def peek_codes(self) -> Tuple[int, int]:
        """View the current (rolling_code, fixed_code) without incrementing."""
        return int(self.config["rolling_code"]), int(self.config["fixed_code"])

    def get_next_codes(self) -> Tuple[int, int]:
        """
        Get the current (rolling_code, fixed_code) and increment the rolling counter.
        The incremented counter is persisted to disk immediately.
        """
        rolling = int(self.config["rolling_code"])
        fixed = int(self.config["fixed_code"])
        step = int(self.config.get("step", 1))

        # Advance rolling code
        next_rolling = (rolling + step) % (MAX_ROLLING_CODE + 1)
        self.config["rolling_code"] = next_rolling
        self.save_config()

        return rolling, fixed

    def reset_remote(self, fixed_code: int = None, start_rolling: int = 1000):
        """Reset the virtual remote with a new fixed ID and initial rolling counter."""
        if fixed_code is None:
            fixed_code = random.randint(100000, MAX_FIXED_CODE)
        self.config["fixed_code"] = fixed_code % (MAX_FIXED_CODE + 1)
        self.config["rolling_code"] = start_rolling % (MAX_ROLLING_CODE + 1)
        self.save_config()
        return self.config["fixed_code"], self.config["rolling_code"]

    def set_rolling_code(self, new_val: int):
        """Manually adjust rolling code (e.g. if desynchronized)."""
        self.config["rolling_code"] = int(new_val) % (MAX_ROLLING_CODE + 1)
        self.save_config()

    def set_fixed_code(self, fixed_val: int):
        """Manually set remote fixed ID."""
        self.config["fixed_code"] = int(fixed_val) % (MAX_FIXED_CODE + 1)
        self.save_config()
