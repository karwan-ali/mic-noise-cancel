"""Real-time single-channel speech enhancement.

Per 10 ms hop at 48 kHz, each frame goes through:

    high-pass shaping -> STFT (WOLA, sqrt-Hann, 50% overlap)
    -> log-SNR VAD with hysteresis and hangover
    -> noise PSD tracking (bias-corrected sliding-window minimum)
    -> decision-directed a priori SNR -> floored Wiener gain
    -> spectral + temporal gain smoothing
    -> noise gate with attack/release, smoothed in dB
    -> ISTFT / overlap-add -> makeup gain -> peak limiter

Everything is vectorised over frequency bins; a frame costs well under 1 ms,
which leaves plenty of headroom inside the audio callback.
"""

import math

import numpy as np

EPS = 1e-12

# Noise tracking, deliberately independent of the VAD.
#
# Updating the noise estimate only on frames the VAD calls silent looks
# appealing but collapses in a noisy room: a missed syllable folds speech into
# the estimate, which lowers the next frame's SNR, which makes the VAD miss
# more. Tracking a per-bin minimum instead needs no decision at all, because
# the dips between syllables expose the floor on their own.
#
# The minimum is taken over a sliding window rather than with a leaky
# integrator. A leaky minimum has to choose between settling quickly at startup
# and holding still while someone talks - one time constant cannot do both -
# whereas a window of a fixed length is fully converged after exactly that long
# and is completely unmoved by anything older. Martin's sub-window trick keeps
# it to one comparison per bin per frame.
_ALPHA_POWER = 0.8  # smoothing of the periodogram used for noise tracking
# The VAD gets its own, much faster smoother. Noise tracking wants low variance
# and does not care about a 40 ms lag; the gate does, because that lag lands on
# the front of every word. Measured on the test scenes, this cuts the worst-case
# onset from 60 ms to 30 ms with no increase in false openings.
_ALPHA_VAD = 0.3
_MIN_SUBWINDOWS = 8  # window = 8 x 20 frames = 1.6 s
_MIN_SUBWINDOW_LEN = 20
_MIN_BIAS = 2.5  # measured mean/min ratio for this window, on white
#                  and on room-like noise alike (spread 2.33-2.70)

# Decision-directed a priori SNR smoothing (Ephraim & Malah).
_ALPHA_DD = 0.97

_PRIME_FRAMES = 10  # frames of pure observation before the gate may close

_VAD_HYSTERESIS_DB = 0.5
_VAD_BAND_HZ = (250.0, 4500.0)


def _periodic_sqrt_hann(n):
    """sqrt of a periodic Hann window.

    Used for both analysis and synthesis: at 50% overlap the squared windows
    sum to exactly 1.0, so overlap-add reconstructs the signal perfectly when
    the spectral gain is 1.
    """
    return np.sqrt(np.hanning(n + 1)[:-1]).astype(np.float32)


def db_to_lin(db):
    return float(10.0 ** (db / 20.0))


class EnhancerParams(object):
    """Tunables, all in units a human can reason about."""

    def __init__(
        self,
        reduction_db=30.0,
        gate_floor_db=-60.0,
        vad_threshold_db=1.5,
        vad_hangover_ms=180.0,
        gate_attack_ms=8.0,
        gate_release_ms=220.0,
        highpass_hz=90.0,
        input_gain_db=0.0,
        makeup_gain_db=0.0,
        gate_enabled=True,
    ):
        self.reduction_db = reduction_db
        self.gate_floor_db = gate_floor_db
        self.vad_threshold_db = vad_threshold_db
        self.vad_hangover_ms = vad_hangover_ms
        self.gate_attack_ms = gate_attack_ms
        self.gate_release_ms = gate_release_ms
        self.highpass_hz = highpass_hz
        self.input_gain_db = input_gain_db
        self.makeup_gain_db = makeup_gain_db
        self.gate_enabled = gate_enabled

    @classmethod
    def from_config(cls, cfg):
        return cls(
            reduction_db=float(cfg.get("reduction_db", 30.0)),
            gate_floor_db=float(cfg.get("gate_floor_db", -60.0)),
            vad_threshold_db=float(cfg.get("vad_threshold_db", 1.5)),
            vad_hangover_ms=float(cfg.get("vad_hangover_ms", 180.0)),
            gate_attack_ms=float(cfg.get("gate_attack_ms", 8.0)),
            gate_release_ms=float(cfg.get("gate_release_ms", 220.0)),
            highpass_hz=float(cfg.get("highpass_hz", 90.0)),
            input_gain_db=float(cfg.get("input_gain_db", 0.0)),
            makeup_gain_db=float(cfg.get("makeup_gain_db", 0.0)),
            gate_enabled=bool(cfg.get("gate_enabled", True)),
        )


class SpeechEnhancer(object):
    """Stateful frame-by-frame enhancer. Not thread-safe by itself."""

    def __init__(self, samplerate=48000, hop=480, params=None):
        self.sr = int(samplerate)
        self.hop = int(hop)
        self.nfft = self.hop * 2
        self.nbins = self.nfft // 2 + 1

        self.win = _periodic_sqrt_hann(self.nfft)
        self.freqs = np.fft.rfftfreq(self.nfft, 1.0 / self.sr).astype(np.float32)
        band = (self.freqs >= _VAD_BAND_HZ[0]) & (self.freqs <= _VAD_BAND_HZ[1])
        self._vad_band = band if band.any() else np.ones(self.nbins, bool)

        self._in = np.zeros(self.nfft, np.float32)
        self._ola = np.zeros(self.nfft, np.float32)

        self._p = np.full(self.nbins, 1e-8, np.float32)
        self._p_vad = np.full(self.nbins, 1e-8, np.float32)
        self._pmin = np.full(self.nbins, 1e-8, np.float32)
        self._noise = np.full(self.nbins, 1e-8, np.float32)
        self._sub_min = np.full(self.nbins, np.inf, np.float32)
        self._sub_history = np.full((_MIN_SUBWINDOWS, self.nbins), np.inf, np.float32)
        self._history_min = np.full(self.nbins, np.inf, np.float32)
        self._sub_index = 0
        self._sub_count = 0
        self._prev_clean = np.zeros(self.nbins, np.float32)
        self._prev_gain = np.ones(self.nbins, np.float32)

        self._gate_db_state = -90.0  # gate gain, smoothed in dB so the
        self._gate_floor_db = -60.0  # attack/release times mean what they say
        self._hang = 0
        self._limiter = 1.0
        self._frames = 0

        # Live telemetry for the UI. Plain floats/bools: reads are atomic
        # enough for a meter, and we never want the audio thread to block.
        self.in_level_db = -120.0
        self.out_level_db = -120.0
        self.speech = False
        self.gate_db = -120.0
        self.vad_score_db = 0.0

        self.set_params(params or EnhancerParams())

    # ------------------------------------------------------------------ setup

    def set_params(self, params):
        """Apply new tunables. Safe to call while audio is running."""
        self.params = params
        frame_s = self.hop / float(self.sr)

        self._gain_floor = db_to_lin(-abs(params.reduction_db))
        self._gate_floor_db = -abs(params.gate_floor_db)
        self._gate_db_state = max(self._gate_db_state, self._gate_floor_db)
        self._in_gain = db_to_lin(params.input_gain_db)
        self._makeup = db_to_lin(params.makeup_gain_db)

        self._vad_on_db = params.vad_threshold_db
        self._vad_off_db = params.vad_threshold_db - _VAD_HYSTERESIS_DB
        self._hang_frames = max(1, int(round(params.vad_hangover_ms / 1000.0 / frame_s)))

        self._attack = math.exp(-frame_s / max(1e-4, params.gate_attack_ms / 1000.0))
        self._release = math.exp(-frame_s / max(1e-4, params.gate_release_ms / 1000.0))

        if params.highpass_hz <= 0.0:
            self._hp = np.ones(self.nbins, np.float32)
        else:
            r = self.freqs / float(params.highpass_hz)
            # Magnitude response of a 2nd-order Butterworth high-pass: -3 dB at fc.
            self._hp = (r ** 2 / np.sqrt(1.0 + r ** 4)).astype(np.float32)

    def reset(self):
        """Forget all adaptive state (used when a stream restarts)."""
        self._in[:] = 0.0
        self._ola[:] = 0.0
        self._p[:] = 1e-8
        self._p_vad[:] = 1e-8
        self._pmin[:] = 1e-8
        self._noise[:] = 1e-8
        self._sub_min[:] = np.inf
        self._sub_history[:] = np.inf
        self._history_min[:] = np.inf
        self._sub_index = 0
        self._sub_count = 0
        self._prev_clean[:] = 0.0
        self._prev_gain[:] = 1.0
        self._gate_db_state = self._gate_floor_db
        self._hang = 0
        self._limiter = 1.0
        self._frames = 0

    # ------------------------------------------------------------- processing

    def process(self, block):
        """Enhance one hop of mono float32 audio; returns a new hop."""
        x = np.asarray(block, dtype=np.float32)
        if self._in_gain != 1.0:
            x = x * self._in_gain

        self.in_level_db = _rms_db(x)

        self._in[: -self.hop] = self._in[self.hop:]
        self._in[-self.hop:] = x

        spec = np.fft.rfft(self._in * self.win)
        spec *= self._hp
        mag = np.abs(spec).astype(np.float32)
        power = mag * mag

        # The very first frame seeds the smoothers outright - letting them ramp
        # up from zero would prime the noise floor several dB too low.
        if self._frames == 0:
            self._p = power.copy()
            self._p_vad = power.copy()
        else:
            self._p = (_ALPHA_POWER * self._p + (1.0 - _ALPHA_POWER) * power).astype(np.float32)
            self._p_vad = (_ALPHA_VAD * self._p_vad + (1.0 - _ALPHA_VAD) * power).astype(np.float32)
        self._track_noise()
        speech = self._detect_speech()
        gain = self._spectral_gain(mag, power)
        gain *= self._apply_gate(speech)

        self._prev_gain = gain
        self._prev_clean = gain * mag

        frame = np.fft.irfft(spec * gain).astype(np.float32) * self.win
        self._ola += frame
        out = self._ola[: self.hop].copy()
        self._ola[: -self.hop] = self._ola[self.hop:]
        self._ola[-self.hop:] = 0.0

        if self._makeup != 1.0:
            out *= self._makeup
        out = self._limit(out)

        self.out_level_db = _rms_db(out)
        self._frames += 1
        return out

    # ------------------------------------------------------------------ inner

    def _detect_speech(self):
        """Speech/silence decision from the per-bin SNR excess.

        Comparing total band energy against a noise floor fails at low SNR: a
        voice only 8 dB above the room barely moves the sum. Averaging
        log(1 + SNR excess) across the band instead lets the handful of bins
        that carry a formant speak up loudly while noise-only bins contribute
        almost nothing, which separates speech from silence far more sharply.
        """
        band = self._vad_band
        excess = self._p_vad[band] / self._noise[band] - 1.0
        np.maximum(excess, 0.0, out=excess)
        score = 10.0 * float(np.mean(np.log10(1.0 + excess)))
        self.vad_score_db = score

        if self._frames < _PRIME_FRAMES:
            self._hang = 0
            return False

        if score >= self._vad_on_db:
            self._hang = self._hang_frames  # clearly speech: hold wide open
        elif score >= self._vad_off_db:
            self._hang = max(0, self._hang - 1)  # ambiguous: start closing
        else:
            self._hang = max(0, self._hang - 2)  # clearly silent: close sooner
        # Only crossing the upper threshold reopens the gate, which is what
        # keeps it from chattering around the decision point.
        speech = self._hang > 0
        self.speech = speech
        return speech

    def _track_noise(self):
        """Bias-corrected sliding-window minimum of the smoothed periodogram."""
        p = self._p

        np.minimum(self._sub_min, p, out=self._sub_min)
        self._sub_count += 1
        if self._sub_count >= _MIN_SUBWINDOW_LEN:
            self._sub_history[self._sub_index] = self._sub_min.copy()
            self._sub_index = (self._sub_index + 1) % _MIN_SUBWINDOWS
            self._sub_count = 0
            self._sub_min = p.copy()
            self._history_min = np.min(self._sub_history, axis=0)

        self._pmin = np.minimum(self._history_min, self._sub_min)
        np.maximum(self._pmin, 1e-12, out=self._pmin)

        if self._frames < _PRIME_FRAMES:
            self._noise = np.maximum(p, 1e-10).astype(np.float32)
            return

        target = _MIN_BIAS * self._pmin
        self._noise = (0.9 * self._noise + 0.1 * target).astype(np.float32)
        np.maximum(self._noise, 1e-10, out=self._noise)

    def _spectral_gain(self, mag, power):
        """Floored Wiener gain from a decision-directed a priori SNR."""
        noise = self._noise
        post = power / noise
        prio = _ALPHA_DD * (self._prev_clean * self._prev_clean) / noise + (
            1.0 - _ALPHA_DD
        ) * np.maximum(post - 1.0, 0.0)
        np.maximum(prio, 1e-4, out=prio)

        gain = prio / (1.0 + prio)
        np.maximum(gain, self._gain_floor, out=gain)

        # 3-tap smoothing across frequency kills most of the musical noise.
        smooth = gain.copy()
        smooth[1:-1] = 0.25 * gain[:-2] + 0.5 * gain[1:-1] + 0.25 * gain[2:]
        # A touch of temporal smoothing without smearing transients.
        gain = (0.7 * smooth + 0.3 * self._prev_gain).astype(np.float32)
        return gain

    def _apply_gate(self, speech):
        """Smoothed noise gate driven by the VAD; returns a scalar gain.

        The smoothing runs on decibels rather than on linear gain. A linear
        ramp spends almost all of its time in the top few dB and crawls towards
        deep attenuation, so "220 ms release" would not remotely describe how
        long the background actually takes to disappear.
        """
        if not self.params.gate_enabled:
            self.gate_db = 0.0
            return np.float32(1.0)

        target = 0.0 if speech else self._gate_floor_db
        coef = self._attack if target > self._gate_db_state else self._release
        self._gate_db_state = coef * self._gate_db_state + (1.0 - coef) * target
        self.gate_db = self._gate_db_state
        return np.float32(10.0 ** (self._gate_db_state / 20.0))

    def _limit(self, out):
        """Peak limiter.

        Reductions apply to the whole block at once - ramping into them would
        let the first samples through above the ceiling, which is the one thing
        a limiter must never do. Recovery is ramped, so the common case of a
        steady loud signal produces no zipper noise.
        """
        ceiling = 0.98
        peak = float(np.max(np.abs(out))) if out.size else 0.0
        needed = min(1.0, ceiling / peak) if peak > ceiling else 1.0

        start = self._limiter
        if needed < start:
            end = needed
            out *= np.float32(end)
        else:
            end = min(needed, start * 1.08 + 1e-3)  # ~ 60 ms release
            if not (start == 1.0 and end == 1.0):
                out *= np.linspace(start, end, out.size, dtype=np.float32)
        self._limiter = end

        # Absolute guarantee, whatever the gain settings upstream were.
        return np.clip(out, -1.0, 1.0, out=out)


def _rms_db(x):
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20.0 * math.log10(rms) if rms > 1e-6 else -120.0
