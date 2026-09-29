"""Optional neural denoiser front-end.

RNNoise is a small recurrent network that removes non-stationary noise
(keyboards, fans, traffic, paper) far better than a classical estimator. It is
not required: without it the app runs on its own DSP alone.

Two shapes of binary are accepted:

  native  a library exporting RNNoise's own C API (rnnoise_create,
          rnnoise_process_frame), which is what building Xiph's rnnoise gives.

  ladspa  a LADSPA plugin exporting ladspa_descriptor, as shipped in the Linux
          builds of werman/noise-suppression-for-voice.

Neither is available prebuilt for Windows. That project's win-rnnoise.zip holds
VST2, LV2 and VST3 wrappers, none of which this can host - so the scan names the
format it found rather than reporting a vague failure, because being sent to
look for a file that does not exist is worse than being told no.

Which shape a file is gets decided by the symbols it exports, not by its name,
so a renamed file still works. Drop it next to the executable, or in an
`rnnoise` folder beside it.
"""

import ctypes
import glob
import logging
import os
import sys

import numpy as np

from . import ladspa

log = logging.getLogger(__name__)

FRAME_SIZE = 480
SAMPLERATE = 48000

# RNNoise's C API works on int16-ranged floats; LADSPA plugins use [-1, 1].
_INT16_SCALE = 32768.0

_EXPLICIT_NAMES = (
    "rnnoise.dll", "librnnoise.dll",
    "librnnoise_ladspa.dll", "rnnoise_ladspa.dll",
    "librnnoise.so", "librnnoise_ladspa.so",
    "librnnoise.dylib", "librnnoise_ladspa.dylib",
)
_PATTERNS = ("*rnnoise*.dll", "*rnnoise*.so", "*rnnoise*.dylib")

# werman's plugin has its own voice gate. Ours is measured and tuned, and two
# gates fighting is worse than either alone, so the plugin is asked to denoise
# only and leave every gating decision to us.
_LADSPA_CONTROLS = {"threshold": 0.0, "grace": 0.0}


def search_dirs():
    """Where a dropped-in library is looked for, nearest first."""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)
    dirs.append(os.path.dirname(here))
    dirs.append(os.getcwd())

    out, seen = [], set()
    for base in dirs:
        for candidate in (base, os.path.join(base, "rnnoise")):
            if candidate and candidate not in seen and os.path.isdir(candidate):
                seen.add(candidate)
                out.append(candidate)
    return out


def _candidate_files():
    files, seen = [], set()
    for directory in search_dirs():
        found = [os.path.join(directory, name) for name in _EXPLICIT_NAMES]
        for pattern in _PATTERNS:
            found += sorted(glob.glob(os.path.join(directory, pattern)))
        for path in found:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen and os.path.isfile(path):
                seen.add(key)
                files.append(path)
    return files


# Plugin formats we can recognise but not host. Naming them beats "unknown
# file": the same download ships several of these side by side, and knowing
# which one is in your hand is most of the answer.
_FOREIGN_FORMATS = (
    ("lv2_descriptor", "an LV2 plugin"),
    ("VSTPluginMain", "a VST2 plugin"),
    ("main_plugin", "a VST2 plugin"),
    ("GetPluginFactory", "a VST3 plugin"),
    ("bundleEntry", "a VST3 plugin"),
)


def _classify(path):
    """('native' | 'ladspa' | None, message) for one candidate file."""
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        # By far the most common cause: a 32-bit DLL under 64-bit Python.
        return None, "%s could not be loaded (wrong architecture? 64-bit is required): %s" % (
            path, exc)
    if hasattr(lib, "rnnoise_create"):
        return "native", path
    if hasattr(lib, "ladspa_descriptor"):
        return "ladspa", path
    for symbol, description in _FOREIGN_FORMATS:
        if hasattr(lib, symbol):
            return None, "%s is %s, which the app cannot host" % (path, description)
    return None, "%s is neither an RNNoise library nor a LADSPA plugin" % path


def discover():
    """Find the best usable library. Returns (kind, path, notes)."""
    notes = []
    best = None
    for path in _candidate_files():
        kind, detail = _classify(path)
        if kind is None:
            notes.append(detail)
            continue
        notes.append("%s: %s" % (os.path.basename(path), kind))
        if kind == "native":  # the direct API wins if both are present
            return kind, path, notes
        if best is None:
            best = (kind, path)
    if best:
        return best[0], best[1], notes
    return None, None, notes


class _NativeBackend(object):
    """Thin ctypes binding around one RNNoise denoiser state."""

    def __init__(self, path):
        self._lib = ctypes.CDLL(path)
        self._lib.rnnoise_create.restype = ctypes.c_void_p
        self._lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        self._lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        self._lib.rnnoise_process_frame.restype = ctypes.c_float
        self._lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        self._state = self._lib.rnnoise_create(None)
        if not self._state:
            raise RuntimeError("rnnoise_create failed")
        self._scratch = np.zeros(FRAME_SIZE, np.float32)
        self.vad_probability = 0.0

    def process(self, block):
        data = np.asarray(block, np.float32)
        if data.size != FRAME_SIZE:  # the C API only accepts exact frames
            return data
        np.multiply(data, _INT16_SCALE, out=self._scratch, casting="unsafe")
        ptr = self._scratch.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self.vad_probability = float(
            self._lib.rnnoise_process_frame(self._state, ptr, ptr))
        return (self._scratch * (1.0 / _INT16_SCALE)).astype(np.float32)

    def close(self):
        if self._state is not None:
            try:
                self._lib.rnnoise_destroy(self._state)
            finally:
                self._state = None


class _LadspaBackend(object):
    """Runs the plugin build, with its own gate turned off."""

    def __init__(self, path):
        self._plugin = ladspa.LadspaPlugin(
            path, samplerate=SAMPLERATE, controls=_LADSPA_CONTROLS)
        self.vad_probability = 0.0

    @property
    def plugin_name(self):
        return self._plugin.name

    def process(self, block):
        return self._plugin.process(block)

    def close(self):
        self._plugin.close()


class RNNoise(object):
    """Facade over whichever backend was found."""

    _probed = False
    _kind = None
    _path = None
    _notes = ()

    @classmethod
    def _probe(cls):
        if not cls._probed:
            cls._probed = True
            cls._kind, cls._path, notes = discover()
            cls._notes = tuple(notes)
            for note in notes:
                log.info("RNNoise scan: %s", note)
        return cls._kind

    @classmethod
    def rescan(cls):
        """Forget the previous scan, so a newly dropped file is picked up."""
        cls._probed = False
        cls._kind = cls._path = None
        cls._notes = ()
        return cls._probe()

    @classmethod
    def available(cls):
        return cls._probe() is not None

    @classmethod
    def library_path(cls):
        return cls._path if cls.available() else None

    @classmethod
    def kind(cls):
        return cls._probe()

    @classmethod
    def notes(cls):
        cls._probe()
        return list(cls._notes)

    def __init__(self):
        kind = self._probe()
        if kind is None:
            raise RuntimeError("No RNNoise library or LADSPA plugin found")
        self.kind = kind
        self._backend = (
            _NativeBackend(self._path) if kind == "native" else _LadspaBackend(self._path)
        )
        log.info("RNNoise active: %s backend from %s", kind, self._path)

    @property
    def vad_probability(self):
        return getattr(self._backend, "vad_probability", 0.0)

    def process(self, block):
        return self._backend.process(block)

    def close(self):
        backend = getattr(self, "_backend", None)
        if backend is not None:
            backend.close()
            self._backend = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
