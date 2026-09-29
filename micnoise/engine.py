"""Audio engine: microphone in, enhanced speech out.

The input and output devices are different pieces of hardware with independent
clocks, so PortAudio cannot run them as one duplex stream. Instead we run an
input stream and one or two output streams joined by ring buffers. Each buffer
starts prefilled to a target level and is left alone until it drifts past a
watermark, at which point it is trimmed or padded back - ordinary jitter costs
nothing, and a real clock difference costs one faint seam every few minutes.

The enhancer runs inside the input callback: at 10 ms hops it costs well under
a millisecond, and keeping it there avoids an extra thread hand-off.
"""

import logging
import threading
import time

import sounddevice as sd

from . import devices, i18n
from .dsp import EnhancerParams, SpeechEnhancer, db_to_lin
from .resample import LinearResampler
from .ring import AudioRing
from .rnnoise import RNNoise

log = logging.getLogger(__name__)

PROC_RATE = 48000
PROC_HOP = 480  # 10 ms
TARGET_LATENCY_MS = 40.0
MAX_LATENCY_MS = 110.0


class EngineError(Exception):
    """A failure the user should read, kept as a message key.

    The text is rendered when it is shown rather than when it is raised, so an
    error that is already on screen follows a language switch.
    """

    def __init__(self, key, **params):
        Exception.__init__(self, key)
        self.key = key
        self.params = params

    def __str__(self):
        return i18n.t(self.key, **self.params)


class AudioEngine(object):
    """Owns every audio stream. All public methods are safe to call from the UI."""

    def __init__(self, cfg, on_state_change=None):
        self.cfg = dict(cfg)
        self._on_state_change = on_state_change
        self._lock = threading.RLock()

        self.enhancer = SpeechEnhancer(PROC_RATE, PROC_HOP, EnhancerParams.from_config(cfg))
        self._rnnoise = None

        self._in_stream = None
        self._out_stream = None
        self._mon_stream = None
        self._out_ring = None
        self._mon_ring = None
        self._in_ring = AudioRing(PROC_RATE)  # 1 s of headroom
        self._in_resampler = None
        self._out_resampler = None
        self._mon_resampler = None
        self._mon_gain = db_to_lin(float(cfg.get("monitor_gain_db", -6.0)))

        self._running = False
        self._error = None
        self._info = {}
        self._watchdog = None
        self._stop_watchdog = threading.Event()
        self._out_target = 0
        self._out_max = 0
        self._mon_target = 0

    # ------------------------------------------------------------------ state

    @property
    def running(self):
        return self._running

    @property
    def error(self):
        return str(self._error) if self._error is not None else None

    def status(self):
        """Snapshot for the tray and the settings window."""
        enh = self.enhancer
        with self._lock:
            info = dict(self._info)
        info.update(
            running=self._running,
            error=self.error,
            bypassed=not bool(self.cfg.get("enabled", True)),
            in_level_db=enh.in_level_db,
            out_level_db=enh.out_level_db,
            speech=enh.speech,
            gate_db=enh.gate_db,
            rnnoise=self._rnnoise is not None,
        )
        if self._out_ring is not None:
            info["xruns"] = (
                self._in_ring.overflows
                + self._out_ring.overflows
                + self._out_ring.underflows
            )
            info["drift_corrections"] = self._out_ring.drops + self._out_ring.pads
        return info

    def _notify(self):
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception:
                log.exception("State change callback failed")

    # ---------------------------------------------------------------- control

    def start(self):
        with self._lock:
            if self._running:
                return True
            self._error = None
            try:
                self._open_streams()
            except Exception as exc:
                self._teardown()
                self._error = exc
                log.exception("Could not start audio")
                self._notify()
                return False
            self._running = True

        self._stop_watchdog.clear()
        self._watchdog = threading.Thread(target=self._watch, name="mnc-watchdog", daemon=True)
        self._watchdog.start()
        log.info("Audio started: %s", self._info)
        self._notify()
        return True

    def stop(self):
        with self._lock:
            if not self._running and self._in_stream is None:
                return
            self._running = False
            self._teardown()
        self._stop_watchdog.set()
        log.info("Audio stopped")
        self._notify()

    def restart(self):
        was_running = self._running
        self.stop()
        if was_running:
            time.sleep(0.15)  # let the driver release the endpoint
            return self.start()
        return False

    def apply_config(self, cfg):
        """Adopt new settings, restarting the streams only if routing changed."""
        keys = ("input_device", "output_device", "monitor_device", "monitor_enabled", "samplerate")
        with self._lock:
            routing_changed = any(self.cfg.get(k) != cfg.get(k) for k in keys)
            rnnoise_changed = bool(self.cfg.get("use_rnnoise")) != bool(cfg.get("use_rnnoise"))
            self.cfg = dict(cfg)
            self.enhancer.set_params(EnhancerParams.from_config(cfg))
            self._mon_gain = db_to_lin(float(cfg.get("monitor_gain_db", -6.0)))

        if routing_changed or (rnnoise_changed and self._running):
            self.restart()
        else:
            self._notify()

    # ------------------------------------------------------------------ setup

    def _open_streams(self):
        in_dev = self._resolve("input", self.cfg.get("input_device"))
        if in_dev is None:
            raise EngineError("engine.no_input")
        out_dev = self._resolve("output", self.cfg.get("output_device"))
        if out_dev is None:
            raise EngineError("engine.no_output")

        in_rate = _pick_rate(in_dev, "input", PROC_RATE)
        out_rate = _pick_rate(out_dev, "output", PROC_RATE)

        self._in_resampler = LinearResampler(in_rate, PROC_RATE)
        self._out_resampler = LinearResampler(PROC_RATE, out_rate)

        self._in_ring.clear()
        self.enhancer.reset()
        self._setup_rnnoise()

        target = int(out_rate * TARGET_LATENCY_MS / 1000.0)
        self._out_ring = AudioRing(max(int(out_rate * 0.5), target * 4))
        self._out_ring.prefill(target)  # the jitter buffer only helps if it starts full
        self._out_target = target
        self._out_max = int(out_rate * MAX_LATENCY_MS / 1000.0)

        mon_dev = None
        if self.cfg.get("monitor_enabled"):
            mon_dev = self._resolve("output", self.cfg.get("monitor_device"))
            if mon_dev is not None:
                mon_rate = _pick_rate(mon_dev, "output", PROC_RATE)
                self._mon_resampler = LinearResampler(PROC_RATE, mon_rate)
                self._mon_target = int(mon_rate * TARGET_LATENCY_MS / 1000.0)
                self._mon_ring = AudioRing(int(mon_rate * 0.5))
                self._mon_ring.prefill(self._mon_target)
            else:
                log.warning("Monitor device not found, monitoring disabled")

        self._in_stream = self._open_input(in_dev, in_rate)
        self._out_stream, _ = self._open_output(out_dev, out_rate, self._out_callback)
        if mon_dev is not None and self._mon_ring is not None:
            self._mon_stream, _ = self._open_output(mon_dev, mon_rate, self._mon_callback)

        self._in_stream.start()
        self._out_stream.start()
        if self._mon_stream is not None:
            self._mon_stream.start()

        self._info = {
            "input_name": in_dev.name,
            "input_api": in_dev.hostapi,
            "input_rate": in_rate,
            "output_name": out_dev.name,
            "output_api": out_dev.hostapi,
            "output_rate": out_rate,
            "monitor_name": mon_dev.name if mon_dev else None,
            "virtual_cable": devices.is_virtual_sink(out_dev.name),
            "latency_ms": round(
                TARGET_LATENCY_MS + 1000.0 * (PROC_HOP * 2) / PROC_RATE, 1
            ),
            "xruns": 0,
            "drift_corrections": 0,
        }

    def _setup_rnnoise(self):
        if self._rnnoise is not None:
            self._rnnoise.close()
            self._rnnoise = None
        if not self.cfg.get("use_rnnoise", True):
            return
        try:
            if RNNoise.available():
                self._rnnoise = RNNoise()
        except Exception:
            log.exception("RNNoise could not be initialised, continuing without it")
            self._rnnoise = None

    def _resolve(self, kind, name):
        dev = devices.find_by_name(kind, name)
        if dev is None:
            dev = devices.default_device(kind)
        return dev

    def _open_input(self, dev, rate):
        last = None
        for channels in _channel_options(dev.channels):
            try:
                return sd.InputStream(
                    device=dev.index,
                    channels=channels,
                    samplerate=rate,
                    blocksize=_blocksize_for(rate),
                    dtype="float32",
                    latency="low",
                    callback=self._make_in_callback(channels),
                    extra_settings=_wasapi_settings(dev),
                )
            except Exception as exc:
                last = exc
                log.debug("Input open failed at %d ch: %s", channels, exc)
        raise EngineError("engine.open_input_failed", device=dev.name, error=last)

    def _open_output(self, dev, rate, callback):
        """Open an output stream; returns (stream, channel_count)."""
        last = None
        for ch in _channel_options(dev.channels, preferred=min(2, dev.channels)):
            try:
                stream = sd.OutputStream(
                    device=dev.index,
                    channels=ch,
                    samplerate=rate,
                    blocksize=_blocksize_for(rate),
                    dtype="float32",
                    latency="low",
                    callback=callback,
                    extra_settings=_wasapi_settings(dev),
                )
                return stream, ch
            except Exception as exc:
                last = exc
                log.debug("Output open failed at %d ch: %s", ch, exc)
        raise EngineError("engine.open_output_failed", device=dev.name, error=last)

    def _teardown(self):
        for attr in ("_in_stream", "_out_stream", "_mon_stream"):
            stream = getattr(self, attr, None)
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    log.debug("Error closing %s", attr, exc_info=True)
                setattr(self, attr, None)
        if self._rnnoise is not None:
            self._rnnoise.close()
            self._rnnoise = None
        self._out_ring = None
        self._mon_ring = None

    # -------------------------------------------------------------- callbacks

    def _make_in_callback(self, channels):
        def callback(indata, frames, time_info, status):
            if status:
                log.debug("Input status: %s", status)
            try:
                mono = indata[:, 0] if channels == 1 else indata.mean(axis=1)
                self._in_ring.write(self._in_resampler.process(mono))
                self._pump()
            except Exception:
                log.exception("Input callback failed")

        return callback

    def _pump(self):
        """Drain whole hops out of the input ring and enhance them."""
        bypass = not bool(self.cfg.get("enabled", True))
        out_rs, mon_rs = self._out_resampler, self._mon_resampler
        if out_rs is None:
            return
        while self._in_ring.available >= PROC_HOP:
            block = self._in_ring.read(PROC_HOP)
            if bypass:
                out = block
            else:
                if self._rnnoise is not None:
                    block = self._rnnoise.process(block)
                out = self.enhancer.process(block)

            ring = self._out_ring
            if ring is not None:
                ring.write(out_rs.process(out))
            mon_ring = self._mon_ring
            if mon_ring is not None and mon_rs is not None:
                mon_ring.write(mon_rs.process(out * self._mon_gain))

    def _out_callback(self, outdata, frames, time_info, status):
        if status:
            log.debug("Output status: %s", status)
        ring = self._out_ring
        if ring is None:
            outdata.fill(0.0)
            return
        try:
            ring.trim(high_water=self._out_max, target=self._out_target)
            ring.pad(low_water=frames, target=self._out_target)
            mono = ring.read(frames)
            outdata[:] = mono.reshape(-1, 1)
        except Exception:
            log.exception("Output callback failed")
            outdata.fill(0.0)

    def _mon_callback(self, outdata, frames, time_info, status):
        ring = self._mon_ring
        if ring is None:
            outdata.fill(0.0)
            return
        try:
            ring.trim(high_water=self._mon_target * 3, target=self._mon_target)
            ring.pad(low_water=frames, target=self._mon_target)
            outdata[:] = ring.read(frames).reshape(-1, 1)
        except Exception:
            log.exception("Monitor callback failed")
            outdata.fill(0.0)

    # --------------------------------------------------------------- watchdog

    def _watch(self):
        """Restart the streams if a device disappears or a driver stalls."""
        failures = 0
        while not self._stop_watchdog.wait(2.0):
            if not self._running:
                continue
            streams = [self._in_stream, self._out_stream, self._mon_stream]
            dead = any(s is not None and not s.active for s in streams)
            if not dead:
                failures = 0
                continue
            failures += 1
            log.warning("Audio stream stopped unexpectedly (attempt %d)", failures)
            devices.refresh()
            with self._lock:
                self._teardown()
                try:
                    self._open_streams()
                    self._error = None
                    failures = 0
                except Exception as exc:
                    self._error = exc
                    log.warning("Restart failed: %s", exc)
            self._notify()
            if failures > 5:
                log.error("Giving up on automatic restart")
                self.stop()
                return


def _channel_options(maximum, preferred=None):
    """Channel counts to try, most desirable first."""
    options = []
    if preferred:
        options.append(min(preferred, maximum))
    options.extend([1, 2, maximum])
    seen, out = set(), []
    for c in options:
        c = max(1, min(int(c), maximum))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _blocksize_for(rate):
    """A 10 ms block at the device's own rate."""
    return max(64, int(round(rate / 100.0)))


def _wasapi_settings(dev):
    """Let WASAPI convert rates/channels for us when the binding supports it."""
    if "wasapi" not in dev.hostapi.lower():
        return None
    try:
        return sd.WasapiSettings(auto_convert=True)
    except TypeError:
        try:
            return sd.WasapiSettings()
        except Exception:
            return None
    except Exception:
        return None


def _pick_rate(dev, kind, preferred):
    """Highest-preference sample rate the device will actually accept."""
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    candidates = [preferred, dev.samplerate, 48000, 44100, 32000, 16000]
    seen = set()
    for rate in candidates:
        rate = int(rate)
        if rate in seen:
            continue
        seen.add(rate)
        try:
            kwargs = {"device": dev.index, "samplerate": rate, "dtype": "float32"}
            kwargs["channels"] = 1 if kind == "input" else min(2, dev.channels)
            check(**kwargs)
            return rate
        except Exception:
            continue
    return int(dev.samplerate or preferred)
