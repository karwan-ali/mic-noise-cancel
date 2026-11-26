//! Real-time single-channel speech enhancement.
//!
//! Per 10 ms hop at 48 kHz, each frame goes through:
//!
//! ```text
//!     high-pass shaping -> STFT (WOLA, sqrt-Hann, 50% overlap)
//!     -> log-SNR VAD with hysteresis and hangover
//!     -> noise PSD tracking (bias-corrected sliding-window minimum)
//!     -> decision-directed a priori SNR -> floored Wiener gain
//!     -> spectral + temporal gain smoothing
//!     -> noise gate with attack/release, smoothed in dB
//!     -> ISTFT / overlap-add -> makeup gain -> peak limiter
//! ```
//!
//! Every buffer is allocated once in `new()`; `process()` never allocates, so it
//! is safe to call from an audio callback.

use std::sync::Arc;

use realfft::num_complex::Complex32;
use realfft::{ComplexToReal, RealFftPlanner, RealToComplex};

// Noise tracking, deliberately independent of the VAD.
//
// Updating the noise estimate only on frames the VAD calls silent collapses in
// a noisy room: a missed syllable folds speech into the estimate, which lowers
// the next frame's SNR, which makes the VAD miss more. Tracking a per-bin
// minimum over a sliding window needs no decision at all, because the dips
// between syllables expose the floor on their own. Martin's sub-window trick
// keeps it to one comparison per bin per frame.
const ALPHA_POWER: f32 = 0.8; // smoothing of the periodogram used for noise tracking
// The VAD gets its own, much faster smoother: noise tracking does not care
// about a 40 ms lag, the gate does, because that lag lands on every word onset.
const ALPHA_VAD: f32 = 0.3;
const MIN_SUBWINDOWS: usize = 8; // window = 8 x 20 frames = 1.6 s
const MIN_SUBWINDOW_LEN: usize = 20;
const MIN_BIAS: f32 = 2.5; // measured mean/min ratio for this window

// Decision-directed a priori SNR smoothing (Ephraim & Malah).
const ALPHA_DD: f32 = 0.97;

const PRIME_FRAMES: u64 = 10; // frames of pure observation before the gate may close

const VAD_HYSTERESIS_DB: f32 = 0.5;
const VAD_BAND_HZ: (f32, f32) = (250.0, 4500.0);

const LIMITER_CEILING: f32 = 0.98;

pub fn db_to_lin(db: f32) -> f32 {
    10f32.powf(db / 20.0)
}

/// Tunables, all in units a human can reason about.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct EnhancerParams {
    pub reduction_db: f32,
    pub gate_floor_db: f32,
    pub vad_threshold_db: f32,
    pub vad_hangover_ms: f32,
    pub gate_attack_ms: f32,
    pub gate_release_ms: f32,
    pub highpass_hz: f32,
    pub input_gain_db: f32,
    pub makeup_gain_db: f32,
    pub gate_enabled: bool,
}

impl Default for EnhancerParams {
    fn default() -> Self {
        EnhancerParams {
            reduction_db: 30.0,
            gate_floor_db: -60.0,
            vad_threshold_db: 1.5,
            vad_hangover_ms: 180.0,
            gate_attack_ms: 8.0,
            gate_release_ms: 220.0,
            highpass_hz: 90.0,
            input_gain_db: 0.0,
            makeup_gain_db: 0.0,
            gate_enabled: true,
        }
    }
}

/// Stateful frame-by-frame enhancer. Not thread-safe by itself.
pub struct SpeechEnhancer {
    sr: f32,
    hop: usize,
    nfft: usize,
    nbins: usize,

    fft: Arc<dyn RealToComplex<f32>>,
    ifft: Arc<dyn ComplexToReal<f32>>,
    fft_scratch: Vec<Complex32>,
    time_buf: Vec<f32>,
    spec: Vec<Complex32>,

    win: Vec<f32>,
    freqs: Vec<f32>,
    vad_lo: usize,
    vad_hi: usize, // exclusive
    hp: Vec<f32>,

    input: Vec<f32>,
    ola: Vec<f32>,
    mag: Vec<f32>,
    power: Vec<f32>,
    gain: Vec<f32>,
    smooth: Vec<f32>,

    p: Vec<f32>,
    p_vad: Vec<f32>,
    noise: Vec<f32>,
    sub_min: Vec<f32>,
    sub_history: Vec<f32>, // MIN_SUBWINDOWS rows of nbins
    history_min: Vec<f32>,
    sub_index: usize,
    sub_count: usize,
    prev_clean: Vec<f32>,
    prev_gain: Vec<f32>,

    gate_db_state: f32,
    hang: i32,
    limiter: f32,
    frames: u64,

    params: EnhancerParams,
    gain_floor: f32,
    gate_floor_db: f32,
    in_gain: f32,
    makeup: f32,
    vad_on_db: f32,
    vad_off_db: f32,
    hang_frames: i32,
    attack: f32,
    release: f32,

    // Live telemetry for the UI.
    pub in_level_db: f32,
    pub out_level_db: f32,
    pub speech: bool,
    pub gate_db: f32,
    pub vad_score_db: f32,
}

impl SpeechEnhancer {
    pub fn new(samplerate: u32, hop: usize, params: EnhancerParams) -> Self {
        let nfft = hop * 2;
        let nbins = nfft / 2 + 1;
        let sr = samplerate as f32;

        let mut planner = RealFftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(nfft);
        let ifft = planner.plan_fft_inverse(nfft);
        let scratch_len = fft.get_scratch_len().max(ifft.get_scratch_len());

        // sqrt of a periodic Hann window, used for analysis and synthesis: at
        // 50% overlap the squared windows sum to exactly 1.0.
        let win: Vec<f32> = (0..nfft)
            .map(|k| {
                let w = 0.5 - 0.5 * (2.0 * std::f64::consts::PI * k as f64 / nfft as f64).cos();
                w.max(0.0).sqrt() as f32
            })
            .collect();
        let freqs: Vec<f32> = (0..nbins).map(|k| k as f32 * sr / nfft as f32).collect();
        let in_band = |f: f32| f >= VAD_BAND_HZ.0 && f <= VAD_BAND_HZ.1;
        let (vad_lo, vad_hi) = match freqs.iter().position(|&f| in_band(f)) {
            Some(lo) => (lo, lo + freqs[lo..].iter().take_while(|&&f| in_band(f)).count()),
            None => (0, nbins),
        };

        let mut enh = SpeechEnhancer {
            sr,
            hop,
            nfft,
            nbins,
            fft,
            ifft,
            fft_scratch: vec![Complex32::new(0.0, 0.0); scratch_len],
            time_buf: vec![0.0; nfft],
            spec: vec![Complex32::new(0.0, 0.0); nbins],
            win,
            freqs,
            vad_lo,
            vad_hi,
            hp: vec![1.0; nbins],
            input: vec![0.0; nfft],
            ola: vec![0.0; nfft],
            mag: vec![0.0; nbins],
            power: vec![0.0; nbins],
            gain: vec![0.0; nbins],
            smooth: vec![0.0; nbins],
            p: vec![1e-8; nbins],
            p_vad: vec![1e-8; nbins],
            noise: vec![1e-8; nbins],
            sub_min: vec![f32::INFINITY; nbins],
            sub_history: vec![f32::INFINITY; nbins * MIN_SUBWINDOWS],
            history_min: vec![f32::INFINITY; nbins],
            sub_index: 0,
            sub_count: 0,
            prev_clean: vec![0.0; nbins],
            prev_gain: vec![1.0; nbins],
            gate_db_state: -90.0,
            hang: 0,
            limiter: 1.0,
            frames: 0,
            params,
            gain_floor: 0.0,
            gate_floor_db: -60.0,
            in_gain: 1.0,
            makeup: 1.0,
            vad_on_db: 0.0,
            vad_off_db: 0.0,
            hang_frames: 1,
            attack: 0.0,
            release: 0.0,
            in_level_db: -120.0,
            out_level_db: -120.0,
            speech: false,
            gate_db: -120.0,
            vad_score_db: 0.0,
        };
        enh.set_params(params);
        enh
    }

    /// Apply new tunables. Safe between any two calls to `process()`.
    pub fn set_params(&mut self, params: EnhancerParams) {
        self.params = params;
        let frame_s = self.hop as f32 / self.sr;

        self.gain_floor = db_to_lin(-params.reduction_db.abs());
        self.gate_floor_db = -params.gate_floor_db.abs();
        self.gate_db_state = self.gate_db_state.max(self.gate_floor_db);
        self.in_gain = db_to_lin(params.input_gain_db);
        self.makeup = db_to_lin(params.makeup_gain_db);

        self.vad_on_db = params.vad_threshold_db;
        self.vad_off_db = params.vad_threshold_db - VAD_HYSTERESIS_DB;
        self.hang_frames = ((params.vad_hangover_ms / 1000.0 / frame_s).round() as i32).max(1);

        self.attack = (-frame_s / (params.gate_attack_ms / 1000.0).max(1e-4)).exp();
        self.release = (-frame_s / (params.gate_release_ms / 1000.0).max(1e-4)).exp();

        if params.highpass_hz <= 0.0 {
            self.hp.fill(1.0);
        } else {
            // Magnitude response of a 2nd-order Butterworth high-pass: -3 dB at fc.
            for (h, &f) in self.hp.iter_mut().zip(&self.freqs) {
                let r = f / params.highpass_hz;
                *h = r * r / (1.0 + r * r * r * r).sqrt();
            }
        }
    }

    /// Forget all adaptive state.
    #[cfg(test)]
    pub fn reset(&mut self) {
        self.input.fill(0.0);
        self.ola.fill(0.0);
        self.p.fill(1e-8);
        self.p_vad.fill(1e-8);
        self.noise.fill(1e-8);
        self.sub_min.fill(f32::INFINITY);
        self.sub_history.fill(f32::INFINITY);
        self.history_min.fill(f32::INFINITY);
        self.sub_index = 0;
        self.sub_count = 0;
        self.prev_clean.fill(0.0);
        self.prev_gain.fill(1.0);
        self.gate_db_state = self.gate_floor_db;
        self.hang = 0;
        self.limiter = 1.0;
        self.frames = 0;
        self.speech = false;
    }

    /// Enhance one hop of mono audio in place. `block.len()` must equal the hop.
    pub fn process(&mut self, block: &mut [f32]) {
        debug_assert_eq!(block.len(), self.hop);
        let hop = self.hop;

        if self.in_gain != 1.0 {
            block.iter_mut().for_each(|s| *s *= self.in_gain);
        }
        self.in_level_db = rms_db(block);

        self.input.copy_within(hop.., 0);
        self.input[self.nfft - hop..].copy_from_slice(block);

        for ((t, &x), &w) in self.time_buf.iter_mut().zip(&self.input).zip(&self.win) {
            *t = x * w;
        }
        // The planned sizes always match, so this cannot fail.
        let _ = self
            .fft
            .process_with_scratch(&mut self.time_buf, &mut self.spec, &mut self.fft_scratch);

        for k in 0..self.nbins {
            let c = self.spec[k] * self.hp[k];
            self.spec[k] = c;
            let m = c.norm();
            self.mag[k] = m;
            self.power[k] = m * m;
        }

        // The very first frame seeds the smoothers outright - ramping up from
        // zero would prime the noise floor several dB too low.
        if self.frames == 0 {
            self.p.copy_from_slice(&self.power);
            self.p_vad.copy_from_slice(&self.power);
        } else {
            for k in 0..self.nbins {
                let pw = self.power[k];
                self.p[k] = ALPHA_POWER * self.p[k] + (1.0 - ALPHA_POWER) * pw;
                self.p_vad[k] = ALPHA_VAD * self.p_vad[k] + (1.0 - ALPHA_VAD) * pw;
            }
        }
        self.track_noise();
        let speech = self.detect_speech();
        self.spectral_gain();
        let gate = self.apply_gate(speech);

        for k in 0..self.nbins {
            let g = self.gain[k] * gate;
            self.gain[k] = g;
            self.prev_gain[k] = g;
            self.prev_clean[k] = g * self.mag[k];
            self.spec[k] *= g;
        }
        // A real signal's DC and Nyquist bins have no imaginary part; make that
        // exact so the inverse transform accepts the spectrum.
        self.spec[0].im = 0.0;
        self.spec[self.nbins - 1].im = 0.0;
        let _ = self
            .ifft
            .process_with_scratch(&mut self.spec, &mut self.time_buf, &mut self.fft_scratch);

        let norm = 1.0 / self.nfft as f32;
        for ((o, &t), &w) in self.ola.iter_mut().zip(&self.time_buf).zip(&self.win) {
            *o += t * norm * w;
        }
        block.copy_from_slice(&self.ola[..hop]);
        self.ola.copy_within(hop.., 0);
        let n = self.nfft;
        self.ola[n - hop..].fill(0.0);

        if self.makeup != 1.0 {
            block.iter_mut().for_each(|s| *s *= self.makeup);
        }
        self.limit(block);

        self.out_level_db = rms_db(block);
        self.frames += 1;
    }

    /// Speech/silence decision from the per-bin SNR excess.
    ///
    /// Averaging log(1 + SNR excess) across the band lets the handful of bins
    /// that carry a formant speak up loudly while noise-only bins contribute
    /// almost nothing, which separates speech from silence far more sharply
    /// than comparing total band energy.
    fn detect_speech(&mut self) -> bool {
        let (lo, hi) = (self.vad_lo, self.vad_hi);
        let mut sum = 0.0f32;
        for k in lo..hi {
            let excess = (self.p_vad[k] / self.noise[k] - 1.0).max(0.0);
            sum += (1.0 + excess).log10();
        }
        let score = 10.0 * sum / (hi - lo).max(1) as f32;
        self.vad_score_db = score;

        if self.frames < PRIME_FRAMES {
            self.hang = 0;
            return false;
        }

        if score >= self.vad_on_db {
            self.hang = self.hang_frames; // clearly speech: hold wide open
        } else if score >= self.vad_off_db {
            self.hang = (self.hang - 1).max(0); // ambiguous: start closing
        } else {
            self.hang = (self.hang - 2).max(0); // clearly silent: close sooner
        }
        // Only crossing the upper threshold reopens the gate, which is what
        // keeps it from chattering around the decision point.
        let speech = self.hang > 0;
        self.speech = speech;
        speech
    }

    /// Bias-corrected sliding-window minimum of the smoothed periodogram.
    fn track_noise(&mut self) {
        let nb = self.nbins;
        for k in 0..nb {
            self.sub_min[k] = self.sub_min[k].min(self.p[k]);
        }
        self.sub_count += 1;
        if self.sub_count >= MIN_SUBWINDOW_LEN {
            let row = self.sub_index * nb;
            self.sub_history[row..row + nb].copy_from_slice(&self.sub_min);
            self.sub_index = (self.sub_index + 1) % MIN_SUBWINDOWS;
            self.sub_count = 0;
            self.sub_min.copy_from_slice(&self.p);
            self.history_min.fill(f32::INFINITY);
            for r in 0..MIN_SUBWINDOWS {
                let hist = &self.sub_history[r * nb..(r + 1) * nb];
                for (m, &h) in self.history_min.iter_mut().zip(hist) {
                    *m = m.min(h);
                }
            }
        }

        if self.frames < PRIME_FRAMES {
            for (n, &p) in self.noise.iter_mut().zip(&self.p) {
                *n = p.max(1e-10);
            }
            return;
        }

        for k in 0..nb {
            let pmin = self.history_min[k].min(self.sub_min[k]).max(1e-12);
            let target = MIN_BIAS * pmin;
            self.noise[k] = (0.9 * self.noise[k] + 0.1 * target).max(1e-10);
        }
    }

    /// Floored Wiener gain from a decision-directed a priori SNR, into `gain`.
    fn spectral_gain(&mut self) {
        for k in 0..self.nbins {
            let noise = self.noise[k];
            let post = self.power[k] / noise;
            let pc = self.prev_clean[k];
            let prio =
                (ALPHA_DD * (pc * pc) / noise + (1.0 - ALPHA_DD) * (post - 1.0).max(0.0)).max(1e-4);
            self.gain[k] = (prio / (1.0 + prio)).max(self.gain_floor);
        }

        // 3-tap smoothing across frequency kills most of the musical noise.
        let n = self.nbins;
        self.smooth.copy_from_slice(&self.gain);
        for k in 1..n - 1 {
            self.smooth[k] = 0.25 * self.gain[k - 1] + 0.5 * self.gain[k] + 0.25 * self.gain[k + 1];
        }
        // A touch of temporal smoothing without smearing transients.
        for k in 0..n {
            self.gain[k] = 0.7 * self.smooth[k] + 0.3 * self.prev_gain[k];
        }
    }

    /// Smoothed noise gate driven by the VAD; returns a scalar gain.
    ///
    /// The smoothing runs on decibels rather than linear gain: a linear ramp
    /// spends almost all of its time in the top few dB, so "220 ms release"
    /// would not describe how long the background takes to disappear.
    fn apply_gate(&mut self, speech: bool) -> f32 {
        if !self.params.gate_enabled {
            self.gate_db = 0.0;
            return 1.0;
        }
        let target = if speech { 0.0 } else { self.gate_floor_db };
        let coef = if target > self.gate_db_state { self.attack } else { self.release };
        self.gate_db_state = coef * self.gate_db_state + (1.0 - coef) * target;
        self.gate_db = self.gate_db_state;
        db_to_lin(self.gate_db_state)
    }

    /// Peak limiter.
    ///
    /// Reductions apply to the whole block at once - ramping into them would
    /// let the first samples through above the ceiling. Recovery is ramped, so
    /// a steady loud signal produces no zipper noise.
    fn limit(&mut self, out: &mut [f32]) {
        let peak = out.iter().fold(0.0f32, |m, &s| m.max(s.abs()));
        let needed = if peak > LIMITER_CEILING { (LIMITER_CEILING / peak).min(1.0) } else { 1.0 };

        let start = self.limiter;
        let end;
        if needed < start {
            end = needed;
            out.iter_mut().for_each(|s| *s *= end);
        } else {
            end = needed.min(start * 1.08 + 1e-3); // ~ 60 ms release
            if !(start == 1.0 && end == 1.0) {
                let n = out.len();
                let step = if n > 1 { (end - start) / (n - 1) as f32 } else { 0.0 };
                for (i, s) in out.iter_mut().enumerate() {
                    *s *= start + step * i as f32;
                }
            }
        }
        self.limiter = end;

        // Absolute guarantee, whatever the gain settings upstream were.
        for s in out.iter_mut() {
            *s = if s.is_finite() { s.clamp(-1.0, 1.0) } else { 0.0 };
        }
    }
}

pub fn rms_db(x: &[f32]) -> f32 {
    if x.is_empty() {
        return -120.0;
    }
    let sum: f64 = x.iter().map(|&s| (s as f64) * (s as f64)).sum();
    let rms = (sum / x.len() as f64).sqrt();
    if rms > 1e-6 {
        (20.0 * rms.log10()) as f32
    } else {
        -120.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::*;

    const SR: u32 = 48000;
    const HOP: usize = 480;
    const FPS: f32 = SR as f32 / HOP as f32;

    fn run(enh: &mut SpeechEnhancer, x: &[f32]) -> (Vec<f32>, Vec<f32>, Vec<bool>) {
        let mut out = Vec::with_capacity(x.len());
        let (mut gates, mut speech) = (Vec::new(), Vec::new());
        let mut buf = [0.0f32; HOP];
        for chunk in x.chunks_exact(HOP) {
            buf.copy_from_slice(chunk);
            enh.process(&mut buf);
            out.extend_from_slice(&buf);
            gates.push(enh.gate_db);
            speech.push(enh.speech);
        }
        (out, gates, speech)
    }

    fn params(reduction: f32, gate: bool, floor: f32, vad: f32, hp: f32) -> EnhancerParams {
        EnhancerParams {
            reduction_db: reduction,
            gate_enabled: gate,
            gate_floor_db: floor,
            vad_threshold_db: vad,
            highpass_hz: hp,
            ..EnhancerParams::default()
        }
    }

    fn median(mut v: Vec<f32>) -> f32 {
        v.sort_by(|a, b| a.partial_cmp(b).unwrap());
        if v.is_empty() {
            f32::NAN
        } else {
            v[v.len() / 2]
        }
    }

    fn select(x: &[f32], mask: &[bool]) -> Vec<f32> {
        x.iter().zip(mask).filter(|(_, &m)| m).map(|(&s, _)| s).collect()
    }

    #[test]
    fn perfect_reconstruction_when_gain_is_one() {
        let mut rng = Rng::new(7);
        let p = EnhancerParams { reduction_db: 0.0, gate_enabled: false, highpass_hz: 0.0, ..Default::default() };
        let mut enh = SpeechEnhancer::new(SR, HOP, p);
        let x: Vec<f32> = (0..SR as usize).map(|_| rng.normal() * 0.2).collect();
        let (y, _, _) = run(&mut enh, &x);
        let err = (0..20000).map(|i| (y[HOP + i] - x[i]).abs()).fold(0.0f32, f32::max);
        assert!(err < 1e-3, "max err {err}");
    }

    #[test]
    fn noise_reduction_against_clean_reference() {
        for (room, amp) in [("headset", 0.008f32), ("noisy room", 0.05)] {
            let scene = make_scene(amp, 7);
            let (voiced, silence) = settled_masks(&scene.clean);
            let a = frames_to_samples(&voiced);
            let b = frames_to_samples(&silence);
            for (name, p, floor) in [
                ("light", params(14.0, false, -60.0, 1.5, 70.0), 6.0),
                ("balanced", params(22.0, true, -28.0, 1.5, 80.0), 18.0),
                ("aggressive", params(30.0, true, -60.0, 1.3, 90.0), 30.0),
            ] {
                let mut enh = SpeechEnhancer::new(SR, HOP, p);
                let (y, _, _) = run(&mut enh, &scene.mixed);
                let n = y.len().min(a.len());
                let in_n = db(&select(&scene.mixed[..n], &b[..n]));
                let out_n = db(&select(&y[..n], &b[..n]));
                let clean_s = db(&select(&scene.clean[..n], &a[..n]));
                let out_s = db(&select(&y[..n], &a[..n]));
                eprintln!("{room:>10}/{name:<10} background -{:.1} dB, speech {:+.1} dB vs clean", in_n - out_n, out_s - clean_s);
                assert!(in_n - out_n >= floor, "{room}/{name}: background down {:.1} dB", in_n - out_n);
                assert!((out_s - clean_s).abs() < 6.0, "{room}/{name}: speech {:+.1} dB", out_s - clean_s);
                assert!(y.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
            }
        }
    }

    #[test]
    fn gate_behaviour_and_onset_latency() {
        for (room, amp) in [("headset", 0.008f32), ("noisy room", 0.05)] {
            let scene = make_scene(amp, 11);
            let (v, sl) = settled_masks(&scene.clean);
            let mut enh = SpeechEnhancer::new(SR, HOP, EnhancerParams::default());
            let (_, gates, flags) = run(&mut enh, &scene.mixed);
            let n = gates.len().min(v.len());
            let g_v = median(select(&gates[..n], &v[..n]));
            let g_s = median(select(&gates[..n], &sl[..n]));
            assert!(g_v > -2.0, "{room}: gate while speaking {g_v:.1}");
            assert!(g_s < -35.0, "{room}: gate in silence {g_s:.1}");
            let frac = |mask: &[bool]| {
                let total = mask[..n].iter().filter(|&&m| m).count() as f32;
                let on = (0..n).filter(|&i| mask[i] && flags[i]).count() as f32;
                on / total
            };
            assert!(frac(&v) > 0.90, "{room}: VAD on voiced {:.2}", frac(&v));
            assert!(frac(&sl) < 0.08, "{room}: VAD on silence {:.2}", frac(&sl));

            let mut worst = 0.0f32;
            for i in 1..n {
                if v[i] && !v[i - 1] {
                    let j = (i..n.min(i + 40)).find(|&j| gates[j] > -3.0).expect("gate never opened");
                    worst = worst.max((j - i) as f32 * 1000.0 / FPS);
                }
            }
            eprintln!("{room:>10}: gate opens within {worst:.0} ms of speech");
            assert!(worst <= 50.0, "{room}: onset {worst:.0} ms");
        }
    }

    #[test]
    fn limiter_holds_the_ceiling() {
        let p = EnhancerParams { reduction_db: 6.0, gate_enabled: false, makeup_gain_db: 18.0, highpass_hz: 20.0, ..Default::default() };
        let mut enh = SpeechEnhancer::new(SR, HOP, p);
        let loud: Vec<f32> = (0..SR)
            .map(|i| 0.9 * (2.0 * std::f32::consts::PI * 300.0 * i as f32 / SR as f32).sin())
            .collect();
        let (y, _, _) = run(&mut enh, &loud);
        assert!(y.iter().all(|s| s.abs() <= 1.0));
    }

    #[test]
    fn robust_to_live_changes_and_edge_input() {
        let scene = make_scene(0.05, 3);
        let mut enh = SpeechEnhancer::new(SR, HOP, EnhancerParams::default());
        let mut buf = [0.0f32; HOP];
        for (i, chunk) in scene.mixed.chunks_exact(HOP).enumerate() {
            if i % 20 == 0 {
                enh.set_params(EnhancerParams {
                    reduction_db: 6.0 + (i % 35) as f32,
                    gate_floor_db: -20.0 - (i % 50) as f32,
                    highpass_hz: (i % 150) as f32,
                    ..Default::default()
                });
            }
            buf.copy_from_slice(chunk);
            enh.process(&mut buf);
            assert!(buf.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
        }
        enh.reset();

        let mut rng = Rng::new(5);
        let edges: [Box<dyn Fn() -> [f32; HOP]>; 5] = [
            Box::new(|| [0.0; HOP]),
            Box::new(|| [1.0; HOP]),
            Box::new(|| [-1.0; HOP]),
            Box::new(|| [1e-9; HOP]),
            Box::new(|| [5.0; HOP]),
        ];
        for _ in 0..6 {
            for e in &edges {
                let mut b = e();
                b[0] += rng.normal();
                enh.process(&mut b);
                assert!(b.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
            }
        }
        for _ in 0..(30.0 * FPS) as usize {
            let mut b = [0.0; HOP];
            enh.process(&mut b);
            assert!(b.iter().all(|s| s.is_finite()));
        }
    }

    #[test]
    fn faster_than_real_time() {
        let scene = make_scene(0.05, 9);
        let mut enh = SpeechEnhancer::new(SR, HOP, EnhancerParams::default());
        let start = std::time::Instant::now();
        let (y, _, _) = run(&mut enh, &scene.mixed);
        let rtf = y.len() as f64 / SR as f64 / start.elapsed().as_secs_f64();
        eprintln!("{:.3} ms per 10 ms hop ({rtf:.0}x real time)", 10.0 / rtf);
        // Debug builds are much slower than release; this only guards against
        // something pathological.
        assert!(rtf > 5.0, "{rtf:.0}x real time");
    }
}
