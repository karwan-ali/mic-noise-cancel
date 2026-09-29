"""A small single-producer/single-consumer sample ring buffer."""

import threading

import numpy as np


class AudioRing(object):
    """Fixed-capacity circular buffer of float32 samples.

    Writes drop the oldest samples on overflow, reads zero-fill on underflow;
    both are logged as x-runs rather than raised, because neither side may
    block inside an audio callback.
    """

    def __init__(self, capacity):
        self.capacity = int(capacity)
        self._buf = np.zeros(self.capacity, np.float32)
        self._read = 0
        self._write = 0
        self._count = 0
        self._lock = threading.Lock()
        self.overflows = 0
        self.underflows = 0
        self.drops = 0
        self.pads = 0

    def __len__(self):
        with self._lock:
            return self._count

    @property
    def available(self):
        return len(self)

    def clear(self):
        with self._lock:
            self._read = self._write = self._count = 0
            self._buf[:] = 0.0

    def write(self, data):
        n = data.size
        if n == 0:
            return
        if n > self.capacity:  # pathological block; keep only the tail
            data = data[-self.capacity:]
            n = data.size

        with self._lock:
            free = self.capacity - self._count
            if n > free:
                self._advance_read(n - free)
                self.overflows += 1

            first = min(n, self.capacity - self._write)
            self._buf[self._write:self._write + first] = data[:first]
            rest = n - first
            if rest:
                self._buf[:rest] = data[first:]
            self._write = (self._write + n) % self.capacity
            self._count += n

    def read(self, n):
        """Return exactly n samples, zero-padded if the buffer ran dry."""
        out = np.zeros(n, np.float32)
        with self._lock:
            take = min(n, self._count)
            if take < n:
                self.underflows += 1
            if take:
                first = min(take, self.capacity - self._read)
                out[:first] = self._buf[self._read:self._read + first]
                rest = take - first
                if rest:
                    out[first:first + rest] = self._buf[:rest]
                self._advance_read(take)
        return out

    def prefill(self, samples):
        """Seed the buffer with silence so it starts at its target level."""
        self.write(np.zeros(int(samples), np.float32))

    def pad(self, low_water, target):
        """Top the buffer back up with silence when drift has drained it.

        The two clocks differ by a fixed fraction, so with no correction the
        consumer eventually outruns the producer for good and every read comes
        up short. Inserting one deliberate gap - at the moment the buffer is
        already empty, so nothing is interrupted - restores the margin and buys
        another few minutes. Typical hardware drift makes this fire once every
        few minutes; the alternative is a glitch on every callback.
        """
        with self._lock:
            if self._count > low_water:
                return 0
            n = int(target - self._count)
            if n <= 0:
                return 0
            free = self.capacity - self._count
            n = min(n, free)
            first = min(n, self.capacity - self._write)
            self._buf[self._write:self._write + first] = 0.0
            rest = n - first
            if rest:
                self._buf[:rest] = 0.0
            self._write = (self._write + n) % self.capacity
            self._count += n
            self.pads += 1
            return n

    def trim(self, high_water, target, quiet_threshold=0.02):
        """Drop old samples once the buffer has drifted past high_water.

        Drift in the other direction: the producer outruns the consumer and
        latency grows without bound. Nothing happens between the two
        watermarks, so ordinary jitter is simply absorbed - only a buffer that
        has genuinely run long gets cut, and then all the way back to target.
        Cutting during a quiet stretch hides the seam, but a buffer far past
        the mark is trimmed regardless: growing latency beats one faint click.
        """
        with self._lock:
            if self._count <= high_water:
                return 0
            n = int(self._count - target)
            if n <= 0:
                return 0
            if self._count < high_water * 1.5 and self._peek_rms(n) > quiet_threshold:
                return 0  # wait for a gap in the speech
            self._advance_read(n)
            self.drops += 1
            return n

    # -- helpers below assume the caller holds the lock -----------------------

    def _advance_read(self, n):
        self._read = (self._read + n) % self.capacity
        self._count -= n

    def _peek_rms(self, n):
        first = min(n, self.capacity - self._read)
        head = self._buf[self._read:self._read + first]
        rest = n - first
        if rest:
            total = float(np.dot(head, head)) + float(
                np.dot(self._buf[:rest], self._buf[:rest])
            )
        else:
            total = float(np.dot(head, head))
        return (total / n) ** 0.5 if n else 0.0
