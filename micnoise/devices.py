"""Audio device discovery on top of PortAudio (via sounddevice)."""

import logging
import time

import sounddevice as sd

log = logging.getLogger(__name__)

# Host APIs worth using on Windows, best first. WASAPI gives the lowest
# latency; the others are fallbacks for odd drivers.
_HOSTAPI_PREFERENCE = ("Windows WASAPI", "Windows DirectSound", "MME", "Windows WDM-KS")

# Substrings that identify the input side of a virtual audio cable, i.e. the
# device we should *write* the cleaned microphone signal to.
_VIRTUAL_SINK_HINTS = (
    "cable input",
    "voicemeeter input",
    "voicemeeter aux input",
    "voicemeeter vaio3 input",
    "line 1 (virtual audio cable)",
    "virtual cable",
)

# The matching capture side, which other apps pick as their "microphone".
_VIRTUAL_SOURCE_HINTS = (
    "cable output",
    "voicemeeter output",
    "line 1 (virtual audio cable)",
)


class Device(object):
    __slots__ = ("index", "name", "hostapi", "channels", "samplerate", "is_input")

    def __init__(self, index, name, hostapi, channels, samplerate, is_input):
        self.index = index
        self.name = name
        self.hostapi = hostapi
        self.channels = channels
        self.samplerate = samplerate
        self.is_input = is_input

    @property
    def label(self):
        return "%s  [%s]" % (self.name, self.hostapi)

    def __repr__(self):
        return "<Device %d %r %s>" % (self.index, self.name, self.hostapi)


# Enumeration is not free - WASAPI can take tens of milliseconds - and the tray
# rebuilds its menu on a timer, so results are cached briefly.
_CACHE_TTL = 5.0
_cache = {}


def refresh():
    """Force PortAudio to re-enumerate. Call after a device change."""
    _cache.clear()
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        log.exception("Could not reinitialise PortAudio")


def _hostapi_rank(name):
    try:
        return _HOSTAPI_PREFERENCE.index(name)
    except ValueError:
        return len(_HOSTAPI_PREFERENCE)


def list_devices(kind):
    """List input or output devices, best host API first, duplicates removed.

    PortAudio reports the same physical device once per host API. We keep the
    entry from the most preferred API so the menus stay readable.
    """
    assert kind in ("input", "output")
    cached = _cache.get(kind)
    if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]

    want_input = kind == "input"
    try:
        raw = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception:
        log.exception("Device enumeration failed")
        return []

    best = {}
    for idx, info in enumerate(raw):
        channels = info["max_input_channels"] if want_input else info["max_output_channels"]
        if channels < 1:
            continue
        api_name = apis[info["hostapi"]]["name"] if info["hostapi"] < len(apis) else "?"
        dev = Device(
            index=idx,
            name=info["name"].strip(),
            hostapi=api_name,
            channels=channels,
            samplerate=int(info.get("default_samplerate") or 48000),
            is_input=want_input,
        )
        key = dev.name.lower()
        current = best.get(key)
        if current is None or _hostapi_rank(api_name) < _hostapi_rank(current.hostapi):
            best[key] = dev

    found = sorted(best.values(), key=lambda d: (_hostapi_rank(d.hostapi), d.name.lower()))
    _cache[kind] = (time.monotonic(), found)
    return found


def find_by_name(kind, name):
    """Resolve a stored device name back to a live device, or None."""
    if not name:
        return None
    devices = list_devices(kind)
    lowered = name.strip().lower()
    for dev in devices:
        if dev.name.lower() == lowered:
            return dev
    # Drivers sometimes append or trim a suffix between reboots.
    for dev in devices:
        if lowered in dev.name.lower() or dev.name.lower() in lowered:
            return dev
    return None


def default_device(kind):
    """The system default device, as a Device, or None."""
    try:
        idx = sd.default.device[0 if kind == "input" else 1]
        if idx is None or idx < 0:
            return None
        info = sd.query_devices(idx)
    except Exception:
        return None
    name = info["name"].strip()
    return find_by_name(kind, name)


def _matches(name, hints):
    lowered = name.lower()
    return any(hint in lowered for hint in hints)


def find_virtual_sink():
    """The virtual cable device to send cleaned audio into, or None."""
    for dev in list_devices("output"):
        if _matches(dev.name, _VIRTUAL_SINK_HINTS):
            return dev
    return None


def find_virtual_source():
    """The capture side of the cable - what other apps should select."""
    for dev in list_devices("input"):
        if _matches(dev.name, _VIRTUAL_SOURCE_HINTS):
            return dev
    return None


def is_virtual_sink(name):
    return bool(name) and _matches(name, _VIRTUAL_SINK_HINTS)


def suggest_defaults(cfg):
    """Fill in any unset device so a fresh install just works.

    Input becomes the system microphone, output the virtual cable if one is
    installed, and the monitor the real speakers.
    """
    changed = False

    if not cfg.get("input_device"):
        dev = default_device("input")
        if dev is None:
            candidates = [d for d in list_devices("input") if not _matches(d.name, _VIRTUAL_SOURCE_HINTS)]
            dev = candidates[0] if candidates else None
        if dev:
            cfg["input_device"] = dev.name
            changed = True

    if not cfg.get("output_device"):
        dev = find_virtual_sink()
        if dev:
            cfg["output_device"] = dev.name
            changed = True

    if not cfg.get("monitor_device"):
        dev = default_device("output")
        if dev and not is_virtual_sink(dev.name):
            cfg["monitor_device"] = dev.name
            changed = True

    return changed
