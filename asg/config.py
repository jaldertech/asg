"""
Configuration loader for ASG (Aldertech Storage Governor).

Loads settings from a YAML config file with sensible defaults.
Config is searched in order:
  1. Path passed via --config CLI flag
  2. /etc/asg/config.yaml
  3. ./config.yaml (current directory)
"""

import os
import sys

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── Defaults ─────────────────────────────────────────────────────────

_DEFAULTS = {
    "pool": {
        "mount": "/mnt/media_pool",
        "uuid": "",
    },
    "scrub": {
        "load_threshold": 3.0,
        "io_threshold_percent": 40.0,
        "load_threshold_running": 7.0,
        "write_iops_threshold": 600.0,
        "poll_interval_seconds": 30,
        "grace_period_seconds": 60,
    },
    "capacity": {
        "chunk_fullness_warn_percent": 90.0,
        "free_space_warn_percent": 10.0,
        "metadata_min_device_count": 3,
    },
    "snapshots": {
        "directory": ".snapshots",
        "retention_days": 3,
    },
    "notifications": {},
    "state_dir": "",
}

_CONFIG_SEARCH_PATHS = [
    "/etc/asg/config.yaml",
    os.path.join(os.getcwd(), "config.yaml"),
]


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    """Merge overrides into defaults, preserving nested structure."""
    result = defaults.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: str) -> dict:
    """Load a YAML file. Returns empty dict on failure."""
    if not _HAS_YAML:
        return {}
    try:
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def load_config(config_path: str | None = None) -> dict:
    """
    Load configuration from YAML, merged with defaults.

    Parameters
    ----------
    config_path : str or None
        Explicit path to config file. If None, searches standard locations.

    Returns
    -------
    dict
        Merged configuration.
    """
    overrides = {}

    if config_path:
        if not os.path.isfile(config_path):
            print(f"WARNING: Config file not found: {config_path}", file=sys.stderr)
        else:
            overrides = _load_yaml(config_path)
    else:
        for path in _CONFIG_SEARCH_PATHS:
            if os.path.isfile(path):
                overrides = _load_yaml(path)
                if overrides:
                    break

    cfg = _deep_merge(_DEFAULTS, overrides)

    # Resolve state directory
    if not cfg["state_dir"]:
        cfg["state_dir"] = "/var/lib/asg"
        # Fallback for non-root / local run
        if not os.access(os.path.dirname(cfg["state_dir"]), os.W_OK):
            cfg["state_dir"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")

    # Resolve snapshot directory (relative to mount point)
    snap_dir = cfg["snapshots"]["directory"]
    if not os.path.isabs(snap_dir):
        cfg["snapshots"]["directory"] = os.path.join(cfg["pool"]["mount"], snap_dir)

    return cfg


# ── Module-level singleton ───────────────────────────────────────────
# Populated by cli.py at startup. Modules import this.

_active_config: dict = {}


def get() -> dict:
    """Return the active configuration. Must be initialised first."""
    if not _active_config:
        raise RuntimeError("Config not initialised. Call config.init() first.")
    return _active_config


def init(config_path: str | None = None) -> dict:
    """Load config and set it as the active singleton."""
    global _active_config
    _active_config = load_config(config_path)
    return _active_config
