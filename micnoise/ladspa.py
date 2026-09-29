"""A minimal LADSPA host, enough to run one mono effect plugin.

Why this exists: RNNoise is distributed far more often as an audio plugin than
as a library, and LADSPA is the one plugin format worth hosting from Python. It
is a single entry point returning a struct of function pointers, unchanged since
2002, with no callbacks back into the host and no memory ownership questions -
the whole API is the struct below. VST2 needs the host to answer calls from the
plugin, VST3 is COM; both are far easier to get subtly and fatally wrong.

Note that werman/noise-suppression-for-voice ships LADSPA only in its Linux
builds; the Windows archive is VST2, LV2 and VST3.
"""

import ctypes
import logging
import math

import numpy as np

log = logging.getLogger(__name__)

PORT_INPUT = 0x1
PORT_OUTPUT = 0x2
PORT_CONTROL = 0x4
PORT_AUDIO = 0x8

_HINT_BOUNDED_BELOW = 0x1
_HINT_BOUNDED_ABOVE = 0x2
_HINT_SAMPLE_RATE = 0x8
_HINT_LOGARITHMIC = 0x10
_HINT_INTEGER = 0x20
_HINT_DEFAULT_MASK = 0x3C0
_HINT_DEFAULT_MINIMUM = 0x40
_HINT_DEFAULT_LOW = 0x80
_HINT_DEFAULT_MIDDLE = 0xC0
_HINT_DEFAULT_HIGH = 0x100
_HINT_DEFAULT_MAXIMUM = 0x140
_HINT_DEFAULT_0 = 0x180
_HINT_DEFAULT_1 = 0x1C0
_HINT_DEFAULT_100 = 0x200
_HINT_DEFAULT_440 = 0x240

MAX_BLOCK = 8192


class PortRangeHint(ctypes.Structure):
    _fields_ = [
        ("HintDescriptor", ctypes.c_int),
        ("LowerBound", ctypes.c_float),
        ("UpperBound", ctypes.c_float),
    ]


class Descriptor(ctypes.Structure):
    pass


# c_ulong deliberately: LADSPA uses `unsigned long`, which is 32-bit under the
# Windows ABI and 64-bit elsewhere. ctypes tracks the platform, a fixed-width
# type would not.
Descriptor._fields_ = [
    ("UniqueID", ctypes.c_ulong),
    ("Label", ctypes.c_char_p),
    ("Properties", ctypes.c_int),
    ("Name", ctypes.c_char_p),
    ("Maker", ctypes.c_char_p),
    ("Copyright", ctypes.c_char_p),
    ("PortCount", ctypes.c_ulong),
    ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
    ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
    ("PortRangeHints", ctypes.POINTER(PortRangeHint)),
    ("ImplementationData", ctypes.c_void_p),
    ("instantiate", ctypes.CFUNCTYPE(
        ctypes.c_void_p, ctypes.POINTER(Descriptor), ctypes.c_ulong)),
    ("connect_port", ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_float))),
    ("activate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
    ("run", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)),
    ("run_adding", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)),
    ("set_run_adding_gain", ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_float)),
    ("deactivate", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
    ("cleanup", ctypes.CFUNCTYPE(None, ctypes.c_void_p)),
]


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")


def default_control_value(hint, samplerate):
    """The value the plugin author intends when the host has no opinion."""
    flags = hint.HintDescriptor
    low, high = float(hint.LowerBound), float(hint.UpperBound)
    if flags & _HINT_SAMPLE_RATE:
        low *= samplerate
        high *= samplerate

    which = flags & _HINT_DEFAULT_MASK
    if which == _HINT_DEFAULT_MINIMUM:
        value = low
    elif which == _HINT_DEFAULT_MAXIMUM:
        value = high
    elif which == _HINT_DEFAULT_0:
        value = 0.0
    elif which == _HINT_DEFAULT_1:
        value = 1.0
    elif which == _HINT_DEFAULT_100:
        value = 100.0
    elif which == _HINT_DEFAULT_440:
        value = 440.0
    elif which in (_HINT_DEFAULT_LOW, _HINT_DEFAULT_MIDDLE, _HINT_DEFAULT_HIGH):
        weight = {_HINT_DEFAULT_LOW: 0.75, _HINT_DEFAULT_MIDDLE: 0.5,
                  _HINT_DEFAULT_HIGH: 0.25}[which]
        if (flags & _HINT_LOGARITHMIC) and low > 0.0 and high > 0.0:
            value = math.exp(math.log(low) * weight + math.log(high) * (1.0 - weight))
        else:
            value = low * weight + high * (1.0 - weight)
    else:
        value = 0.0

    if flags & _HINT_INTEGER:
        value = float(round(value))
    if flags & _HINT_BOUNDED_BELOW:
        value = max(value, low)
    if flags & _HINT_BOUNDED_ABOVE:
        value = min(value, high)
    return value


class Port(object):
    __slots__ = ("index", "name", "flags", "hint")

    def __init__(self, index, name, flags, hint):
        self.index = index
        self.name = name
        self.flags = flags
        self.hint = hint

    @property
    def is_audio(self):
        return bool(self.flags & PORT_AUDIO)

    @property
    def is_control(self):
        return bool(self.flags & PORT_CONTROL)

    @property
    def is_input(self):
        return bool(self.flags & PORT_INPUT)

    @property
    def is_output(self):
        return bool(self.flags & PORT_OUTPUT)

    def __repr__(self):
        kind = "audio" if self.is_audio else "control"
        way = "in" if self.is_input else "out"
        return "<Port %d %r %s-%s>" % (self.index, self.name, kind, way)


class LadspaPlugin(object):
    """One instantiated mono plugin: feed it blocks, get blocks back."""

    def __init__(self, path, samplerate=48000, controls=None):
        self.path = path
        self.samplerate = int(samplerate)
        self._lib = ctypes.CDLL(path)
        self._lib.ladspa_descriptor.restype = ctypes.POINTER(Descriptor)
        self._lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]

        self._desc_ptr = self._find_mono_descriptor()
        if self._desc_ptr is None:
            raise RuntimeError("%s has no mono LADSPA plugin" % path)
        desc = self._desc_ptr.contents

        self.name = _text(desc.Name)
        self.label = _text(desc.Label)
        self.ports = self._read_ports(desc)
        self._audio_in = next(p for p in self.ports if p.is_audio and p.is_input)
        self._audio_out = next(p for p in self.ports if p.is_audio and p.is_output)

        self._handle = desc.instantiate(self._desc_ptr, ctypes.c_ulong(self.samplerate))
        if not self._handle:
            raise RuntimeError("instantiate() failed for %s" % path)

        # Buffers are connected once and reused, so they must not be
        # reallocated for the lifetime of the plugin - hence the attributes.
        self._in = np.zeros(MAX_BLOCK, np.float32)
        self._out = np.zeros(MAX_BLOCK, np.float32)
        self._controls = {}
        self._connect(desc, controls or {})

        if desc.activate:
            desc.activate(self._handle)
        log.info("LADSPA plugin ready: %r from %s", self.name, path)

    # ---------------------------------------------------------------- setup

    def _find_mono_descriptor(self):
        """First plugin with exactly one audio in and one audio out.

        The archive ships mono and stereo variants side by side and the stereo
        one often comes first, so taking index 0 blindly gets the wrong plugin.
        """
        for index in range(32):
            ptr = self._lib.ladspa_descriptor(ctypes.c_ulong(index))
            if not ptr:
                break
            desc = ptr.contents
            if not desc.instantiate or not desc.connect_port or not desc.run:
                continue
            ins = outs = 0
            for i in range(int(desc.PortCount)):
                flags = desc.PortDescriptors[i]
                if flags & PORT_AUDIO:
                    if flags & PORT_INPUT:
                        ins += 1
                    elif flags & PORT_OUTPUT:
                        outs += 1
            if ins == 1 and outs == 1:
                return ptr
        return None

    def _read_ports(self, desc):
        ports = []
        for i in range(int(desc.PortCount)):
            ports.append(Port(
                index=i,
                name=_text(desc.PortNames[i]),
                flags=desc.PortDescriptors[i],
                hint=desc.PortRangeHints[i],
            ))
        return ports

    def _connect(self, desc, overrides):
        in_ptr = self._in.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        out_ptr = self._out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        desc.connect_port(self._handle, ctypes.c_ulong(self._audio_in.index), in_ptr)
        desc.connect_port(self._handle, ctypes.c_ulong(self._audio_out.index), out_ptr)

        for port in self.ports:
            if not port.is_control:
                continue
            value = default_control_value(port.hint, self.samplerate)
            lowered = port.name.lower()
            for needle, override in overrides.items():
                if needle.lower() in lowered:
                    value = float(override)
                    break
            holder = ctypes.c_float(value)
            self._controls[port.name] = holder
            desc.connect_port(self._handle, ctypes.c_ulong(port.index),
                              ctypes.byref(holder))
            log.debug("LADSPA control %r = %s", port.name, value)

    # ------------------------------------------------------------ processing

    @property
    def control_values(self):
        return {name: holder.value for name, holder in self._controls.items()}

    def process(self, block):
        """Run one block of mono float32 audio through the plugin."""
        if self._handle is None:
            return np.asarray(block, np.float32)
        data = np.asarray(block, np.float32)
        count = data.size
        if count > MAX_BLOCK:
            raise ValueError("block of %d exceeds MAX_BLOCK" % count)
        self._in[:count] = data
        self._desc_ptr.contents.run(self._handle, ctypes.c_ulong(count))
        return self._out[:count].copy()

    def close(self):
        if getattr(self, "_handle", None) is None:
            return
        desc = self._desc_ptr.contents
        try:
            if desc.deactivate:
                desc.deactivate(self._handle)
            if desc.cleanup:
                desc.cleanup(self._handle)
        finally:
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def is_ladspa(path):
    """True if the file exports the LADSPA entry point."""
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return False
    return hasattr(lib, "ladspa_descriptor")
