"""
Configuration Manager

Runner configuration.
"""

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .service_urls import resolve_service_url


def _default_home() -> Path:
    # AKAION_HOME lets the .dmg/.AppImage install live in its own
    # config dir (e.g. ~/.akaion-prod) without clobbering the source
    # checkout's ~/.akaion.
    override = os.getenv("AKAION_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".akaion"


class ConfigManager:
    """Loads, merges and persists the runner configuration."""

    def __init__(self, config_dir: Optional[Path] = None):
        # Not created here: read-only commands (`doctor`, `status`) build a
        # ConfigManager and must leave an empty home empty (#42).
        self.config_dir = config_dir or _default_home()

        self.config_path = self.config_dir / "config.yaml"
        self.default_config_path = Path(__file__).parent.parent / "config.yaml"

    def config_exists(self) -> bool:
        """Whether a configuration file exists."""
        return self.config_path.exists()

    def create_default_config(self):
        """Write the default configuration."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if self.default_config_path.exists():
            shutil.copy(self.default_config_path, self.config_path)
        else:
            # Fallback se il file template non esiste
            default = self._get_minimal_config()
            self.save_config(default)

    def create_config(self, config_data: Dict[str, Any]):
        """Write a configuration, merged over the defaults."""
        # Start from the defaults and merge the overrides in
        if self.default_config_path.exists():
            with open(self.default_config_path) as f:
                config = yaml.safe_load(f)
        else:
            config = self._get_minimal_config()

        # Merge ricorsivo
        self._deep_merge(config, config_data)
        self.save_config(config)

    def load_config(self) -> Dict[str, Any]:
        """Load the configuration."""
        if not self.config_path.exists():
            raise FileNotFoundError("Configuration file not found. Run 'akaion init' first.")

        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def save_config(self, config: Dict[str, Any]):
        """Persist the configuration."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    def reset_config(self):
        """Reset to the default configuration."""
        if self.config_path.exists():
            self.config_path.unlink()
        self.create_default_config()

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value from the config. Supports dot notation."""
        config = self.load_config()

        keys = key.split(".")
        value = config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def set(self, key: str, value: Any):
        """Set a value in the config. Supports dot notation."""
        config = self.load_config()

        keys = key.split(".")
        current = config

        for k in keys[:-1]:
            if k not in current:
                current[k] = {}
            current = current[k]

        current[keys[-1]] = value
        self.save_config(config)

    @staticmethod
    def _deep_merge(base: Dict, update: Dict) -> Dict:
        """Merge ricorsivo di due dict"""
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                ConfigManager._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    @staticmethod
    def _get_minimal_config() -> Dict[str, Any]:
        """The minimal fallback configuration."""
        return {
            "cloud": {
                # Local-first default: cloud sync is opt-in. The runner works
                # fully offline (Brain, notes, search) without ever touching
                # the network. Flip to True (or run `akaion cloud enable`) to
                # enable polling, heartbeat and sync.
                "enabled": False,
                "api_url": resolve_service_url("main"),
                "polling_interval": 5,
                "timeout": 30,
            },
            "ai": {"provider": "akaion", "temperature": 0.7, "max_tokens": 4000},
            "permissions": {
                "filesystem": {
                    "allowed_paths": ["~/Documents", "~/Downloads"],
                    "denied_paths": ["~/.ssh"],
                    "max_file_size_mb": 100,
                },
                "shell": {
                    "enabled": True,
                    "allowed_commands": ["ls", "cat", "grep", "find", "git"],
                },
                "network": {"enabled": True},
            },
            "tools": {"enabled": ["filesystem", "shell", "document_reader", "explorer"]},
            "logging": {"level": "INFO", "file": "logs/annona.log"},
            "runner": {
                "mode": "daemon",
                "max_concurrent_tasks": 3,
                "retry_attempts": 3,
                # When true, every executed task is also stored as a local
                # note (sync_status=local_only) in the vault. Override
                # via env var AKAION_CAPTURE_TO_BRAIN=0/1.
                "capture_to_brain": True,
            },
        }
