"""Persistent settings, stored as JSON under %APPDATA%\\MicNoiseCancel."""

import copy
import json
import logging
import os
import tempfile

from . import APP_NAME, i18n

log = logging.getLogger(__name__)

DEFAULTS = {
    # Devices are stored by name, not index: indices shuffle when hardware
    # is plugged in or out, names survive a reboot.
    "input_device": None,
    "output_device": None,
    "monitor_device": None,
    "monitor_enabled": False,
    "monitor_gain_db": -6.0,
    # None follows the Windows display language until the user picks one.
    "language": None,
    "enabled": True,
    "autostart": False,
    "preset": "aggressive",
    "samplerate": 48000,
    # DSP
    "reduction_db": 30.0,
    "gate_enabled": True,
    "gate_floor_db": -60.0,
    "vad_threshold_db": 1.5,
    "vad_hangover_ms": 180.0,
    "gate_attack_ms": 8.0,
    "gate_release_ms": 220.0,
    "highpass_hz": 90.0,
    "input_gain_db": 0.0,
    "makeup_gain_db": 0.0,
    "use_rnnoise": True,
}

PRESETS = {
    "light": {
        "reduction_db": 14.0,
        "gate_enabled": False,
        "gate_floor_db": -20.0,
        "vad_threshold_db": 1.5,
        "highpass_hz": 70.0,
    },
    "balanced": {
        "reduction_db": 22.0,
        "gate_enabled": True,
        "gate_floor_db": -28.0,
        "vad_threshold_db": 1.5,
        "highpass_hz": 80.0,
    },
    "aggressive": {
        "reduction_db": 30.0,
        "gate_enabled": True,
        "gate_floor_db": -60.0,
        "vad_threshold_db": 1.5,
        "highpass_hz": 90.0,
    },
}

PRESET_NAMES = ("light", "balanced", "aggressive")


def preset_label(name):
    """The preset's display name in the current UI language."""
    return i18n.t("preset." + name)


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(config_dir(), "config.json")


def load():
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except FileNotFoundError:
        log.info("No config yet, using defaults (%s)", path)
    except Exception:
        log.exception("Could not read %s, falling back to defaults", path)
    return cfg


def save(cfg):
    """Write atomically so a crash mid-write cannot leave a truncated file."""
    path = config_path()
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            _silent_unlink(tmp)
            raise
    except Exception:
        log.exception("Could not save config to %s", path)
        return False
    return True


def apply_preset(cfg, name):
    """Copy a preset's values into cfg. Unknown names are ignored."""
    values = PRESETS.get(name)
    if not values:
        return cfg
    cfg.update(values)
    cfg["preset"] = name
    return cfg


def detect_preset(cfg):
    """Which preset (if any) the current DSP values correspond to."""
    for name, values in PRESETS.items():
        if all(_close(cfg.get(k), v) for k, v in values.items()):
            return name
    return "custom"


def _close(a, b):
    if isinstance(b, bool):
        return bool(a) == b
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return a == b


def _silent_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass
