"""Streaming linear resampler.

Only used when a device refuses to run at 48 kHz, which is the rate the
enhancer (and RNNoise) works at. Linear interpolation is modest but perfectly
adequate for speech on a fallback path, and it keeps the dependency list to
NumPy alone.
"""

import numpy as np


class LinearResampler(object):
    def __init__(self, src_rate, dst_rate):
        self.src_rate = float(src_rate)
        self.dst_rate = float(dst_rate)
        self.ratio = self.dst_rate / self.src_rate
        self._tail = np.zeros(1, np.float32)
        self._pos = 0.0  # fractional read position inside the working buffer

    @property
    def passthrough(self):
        return abs(self.ratio - 1.0) < 1e-9

    def reset(self):
        self._tail = np.zeros(1, np.float32)
        self._pos = 0.0

    def process(self, x):
        if self.passthrough:
            return np.asarray(x, np.float32)

        buf = np.concatenate((self._tail, np.asarray(x, np.float32)))
        last = buf.size - 1
        if last <= 0:
            return np.zeros(0, np.float32)

        span = last - self._pos
        if span <= 0:
            self._tail = buf[-1:].copy()
            self._pos = max(0.0, self._pos - last)
            return np.zeros(0, np.float32)

        count = int(np.floor(span * self.ratio)) + 1
        times = self._pos + np.arange(count, dtype=np.float64) / self.ratio
        times = times[times <= last]
        if times.size == 0:
            self._tail = buf[-1:].copy()
            self._pos = max(0.0, self._pos - last)
            return np.zeros(0, np.float32)

        out = np.interp(times, np.arange(buf.size), buf).astype(np.float32)
        self._pos = (times[-1] + 1.0 / self.ratio) - last
        self._tail = buf[-1:].copy()
        return out
