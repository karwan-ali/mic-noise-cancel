//! Audio engine: microphone in, enhanced speech out.
//!
//! The input and output devices have independent clocks, so they run as
//! separate WASAPI streams joined by ring buffers. Each buffer starts
//! prefilled to a target level and is left alone until it drifts past a
//! watermark, at which point it is trimmed or padded back - ordinary jitter
//! costs nothing, a real clock difference costs one faint seam every few
//! minutes.
//!
//! The enhancer runs inside the input callback: a 10 ms hop costs well under a
//! millisecond, and keeping it there avoids an extra thread. Everything the
//! callbacks touch is allocated when the streams open, never while they run.
//!
//! The engine itself lives on the UI thread. The callbacks talk to it only
//! through atomics in `Shared`.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

use cpal::traits::{DeviceTrait, StreamTrait};
use cpal::{FromSample, Sample, SampleFormat, SizedSample, StreamConfig};
use nnnoiseless::DenoiseState;

use crate::config::Config;
use crate::devices::{self, Kind};
use crate::dsp::{db_to_lin, rms_db, SpeechEnhancer};
use crate::i18n::{t, tf, S};
use crate::resample::LinearResampler;
use crate::ring::AudioRing;
use crate::{log_error, log_info, log_warn};

pub const PROC_RATE: u32 = 48000;
pub const PROC_HOP: usize = 480; // 10 ms
const TARGET_LATENCY_MS: f32 = 40.0;
const MAX_LATENCY_MS: f32 = 110.0;
const RNNOISE_SCALE: f32 = 32768.0; // RNNoise works on int16-ranged floats
const MAX_RESTARTS: u32 = 5;

/// A failure the user should read. Rendered when shown, not when raised, so
/// an error already on screen follows a language switch.
#[derive(Clone, Debug)]
pub enum EngineError {
    NoInput,
    NoOutput,
    OpenInput { device: String, error: String },
    OpenOutput { device: String, error: String },
}

impl EngineError {
    pub fn message(&self) -> String {
        match self {
            EngineError::NoInput => t(S::EngineNoInput).into(),
            EngineError::NoOutput => t(S::EngineNoOutput).into(),
            EngineError::OpenInput { device, error } => {
                tf(S::EngineOpenInputFailed, &[("device", device), ("error", error)])
            }
            EngineError::OpenOutput { device, error } => {
                tf(S::EngineOpenOutputFailed, &[("device", device), ("error", error)])
            }
        }
    }
}

/// f32 in an atomic, for meters the UI polls.
#[derive(Default)]
struct AtomicF32(AtomicU32);

impl AtomicF32 {
    fn new(v: f32) -> Self {
        AtomicF32(AtomicU32::new(v.to_bits()))
    }
    fn get(&self) -> f32 {
        f32::from_bits(self.0.load(Ordering::Relaxed))
    }
    fn set(&self, v: f32) {
        self.0.store(v.to_bits(), Ordering::Relaxed)
    }
}

/// State shared between the UI thread and the audio callbacks.
struct Shared {
    in_level_db: AtomicF32,
    out_level_db: AtomicF32,
    speech: AtomicBool,
    bypass: AtomicBool,
    monitor_gain: AtomicF32,
    failed: AtomicBool,
    params_dirty: AtomicBool,
    params: Mutex<crate::dsp::EnhancerParams>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Clone, Default)]
pub struct Info {
    pub input_name: String,
    pub output_name: String,
    pub monitor_name: Option<String>,
    pub input_rate: u32,
    pub output_rate: u32,
    pub virtual_cable: bool,
    pub latency_ms: f32,
}

/// Snapshot for the tray and the settings window.
pub struct Status {
    pub running: bool,
    pub error: Option<String>,
    pub bypassed: bool,
    pub in_level_db: f32,
    pub out_level_db: f32,
    pub speech: bool,
    pub rnnoise: bool,
    pub info: Info,
}

struct Streams {
    // Dropping a cpal stream stops it and joins its thread.
    _input: cpal::Stream,
    _output: cpal::Stream,
    _monitor: Option<cpal::Stream>,
    out_ring: Arc<Mutex<AudioRing>>,
}

pub struct Engine {
    cfg: Config,
    shared: Arc<Shared>,
    streams: Option<Streams>,
    running: bool,
    error: Option<EngineError>,
    info: Info,
    rnnoise: bool,
    restarts: u32,
    wake: Arc<dyn Fn() + Send + Sync>,
}

impl Engine {
    /// `wake` is called from an audio thread when a stream fails, so the UI
    /// thread can react without polling.
    pub fn new(cfg: &Config, wake: Arc<dyn Fn() + Send + Sync>) -> Self {
        let shared = Arc::new(Shared {
            in_level_db: AtomicF32::new(-120.0),
            out_level_db: AtomicF32::new(-120.0),
            speech: AtomicBool::new(false),
            bypass: AtomicBool::new(!cfg.enabled),
            monitor_gain: AtomicF32::new(db_to_lin(cfg.monitor_gain_db)),
            failed: AtomicBool::new(false),
            params_dirty: AtomicBool::new(false),
            params: Mutex::new(cfg.enhancer_params()),
        });
        Engine {
            cfg: cfg.clone(),
            shared,
            streams: None,
            running: false,
            error: None,
            info: Info::default(),
            rnnoise: false,
            restarts: 0,
            wake,
        }
    }

    pub fn running(&self) -> bool {
        self.running
    }

    pub fn error(&self) -> Option<String> {
        self.error.as_ref().map(EngineError::message)
    }

    pub fn status(&self) -> Status {
        Status {
            running: self.running,
            error: self.error(),
            bypassed: !self.cfg.enabled,
            in_level_db: self.shared.in_level_db.get(),
            out_level_db: self.shared.out_level_db.get(),
            speech: self.shared.speech.load(Ordering::Relaxed),
            rnnoise: self.rnnoise,
            info: self.info.clone(),
        }
    }

    pub fn start(&mut self) -> bool {
        if self.running {
            return true;
        }
        self.error = None;
        match self.open_streams() {
            Ok(()) => {
                self.running = true;
                log_info!(
                    "Audio started: {} ({} Hz) -> {} ({} Hz), monitor {:?}, RNNoise {}",
                    self.info.input_name,
                    self.info.input_rate,
                    self.info.output_name,
                    self.info.output_rate,
                    self.info.monitor_name,
                    self.rnnoise
                );
                true
            }
            Err(e) => {
                self.teardown();
                log_error!("Could not start audio: {e:?}");
                self.error = Some(e);
                false
            }
        }
    }

    pub fn stop(&mut self) {
        if !self.running && self.streams.is_none() {
            return;
        }
        self.running = false;
        self.teardown();
        log_info!("Audio stopped");
    }

    pub fn restart(&mut self) -> bool {
        let was_running = self.running || self.error.is_some();
        self.stop();
        self.restarts = 0;
        if was_running {
            std::thread::sleep(std::time::Duration::from_millis(150)); // let the driver let go
            return self.start();
        }
        false
    }

    /// Adopt new settings, reopening the streams only if routing changed.
    pub fn apply_config(&mut self, cfg: &Config) {
        let routing_changed = self.cfg.input_device != cfg.input_device
            || self.cfg.output_device != cfg.output_device
            || self.cfg.monitor_device != cfg.monitor_device
            || self.cfg.monitor_enabled != cfg.monitor_enabled;
        let rnnoise_changed = self.cfg.use_rnnoise != cfg.use_rnnoise;
        self.cfg = cfg.clone();

        *lock(&self.shared.params) = cfg.enhancer_params();
        self.shared.params_dirty.store(true, Ordering::Release);
        self.shared.bypass.store(!cfg.enabled, Ordering::Relaxed);
        self.shared.monitor_gain.set(db_to_lin(cfg.monitor_gain_db));

        if routing_changed || (rnnoise_changed && self.running) {
            self.restart();
        }
    }

    /// Called on a timer from the UI thread: reopen the streams if a device
    /// disappeared or a driver stalled.
    pub fn watchdog(&mut self) -> bool {
        if !self.running || !self.shared.failed.load(Ordering::Acquire) {
            return false;
        }
        self.restarts += 1;
        log_warn!("Audio stream failed, reopening (attempt {})", self.restarts);
        self.teardown();
        match self.open_streams() {
            Ok(()) => {
                self.error = None;
                self.restarts = 0;
            }
            Err(e) => {
                log_warn!("Restart failed: {e:?}");
                self.error = Some(e);
                self.shared.failed.store(true, Ordering::Release); // try again next tick
                if self.restarts > MAX_RESTARTS {
                    log_error!("Giving up on automatic restart");
                    self.running = false;
                    self.teardown();
                }
            }
        }
        true
    }

    // ------------------------------------------------------------------ setup

    fn resolve(kind: Kind, name: &Option<String>) -> Option<(String, cpal::Device)> {
        name.as_deref()
            .and_then(|n| devices::find_by_name(kind, n))
            .or_else(|| devices::default_device(kind))
    }

    fn open_streams(&mut self) -> Result<(), EngineError> {
        self.shared.failed.store(false, Ordering::Release);
        let (in_name, in_dev) = Self::resolve(Kind::Input, &self.cfg.input_device).ok_or(EngineError::NoInput)?;
        let (out_name, out_dev) =
            Self::resolve(Kind::Output, &self.cfg.output_device).ok_or(EngineError::NoOutput)?;

        let mut monitor = None;
        if self.cfg.monitor_enabled {
            match Self::resolve(Kind::Output, &self.cfg.monitor_device) {
                Some(m) => monitor = Some(m),
                None => log_warn!("Monitor device not found, monitoring disabled"),
            }
        }

        // Outputs first: their rings must exist before the input starts
        // writing into them.
        let out = open_output(&out_dev, &out_name, self.shared.clone(), self.wake.clone(), 1.0)?;
        let mon = match &monitor {
            Some((name, dev)) => match open_output(dev, name, self.shared.clone(), self.wake.clone(), 3.0) {
                Ok(m) => Some(m),
                Err(e) => {
                    log_warn!("Monitor could not be opened, continuing without: {e:?}");
                    None
                }
            },
            None => None,
        };

        let rnnoise = self.cfg.use_rnnoise.then(DenoiseState::new);
        self.rnnoise = rnnoise.is_some();

        let mut sinks = vec![Sink::new(out.ring.clone(), out.rate, false)];
        if let Some(m) = &mon {
            sinks.push(Sink::new(m.ring.clone(), m.rate, true));
        }

        let params = *lock(&self.shared.params);
        let proc = InputProc {
            channels: 1,
            mono: Vec::with_capacity(PROC_RATE as usize / 10),
            resampler: LinearResampler::new(PROC_RATE, PROC_RATE),
            pending: Vec::with_capacity(PROC_RATE as usize / 5),
            enhancer: SpeechEnhancer::new(PROC_RATE, PROC_HOP, params),
            rnnoise,
            rn_buf: [0.0; PROC_HOP],
            hop: [0.0; PROC_HOP],
            sinks,
            shared: self.shared.clone(),
        };
        let (input, in_rate) = open_input(&in_dev, &in_name, proc, self.shared.clone(), self.wake.clone())?;

        for stream in [Some(&out.stream), mon.as_ref().map(|m| &m.stream), Some(&input)].into_iter().flatten() {
            stream.play().map_err(|e| EngineError::OpenOutput { device: out_name.clone(), error: e.to_string() })?;
        }

        self.info = Info {
            virtual_cable: devices::is_virtual_sink(&out_name),
            input_name: in_name,
            output_name: out_name,
            monitor_name: mon.as_ref().and(monitor.map(|(n, _)| n)),
            input_rate: in_rate,
            output_rate: out.rate,
            latency_ms: TARGET_LATENCY_MS + 1000.0 * (PROC_HOP * 2) as f32 / PROC_RATE as f32,
        };
        self.streams = Some(Streams {
            _input: input,
            _output: out.stream,
            _monitor: mon.map(|m| m.stream),
            out_ring: out.ring,
        });
        Ok(())
    }

    fn teardown(&mut self) {
        if let Some(s) = &self.streams {
            let r = lock(&s.out_ring);
            log_info!(
                "Output buffer: {} overflows, {} underflows, {} drift trims, {} drift pads",
                r.overflows,
                r.underflows,
                r.drops,
                r.pads
            );
        }
        // The input stream is declared first in Streams, so it stops before
        // the outputs it feeds.
        self.streams = None;
        self.rnnoise = false;
        self.shared.in_level_db.set(-120.0);
        self.shared.out_level_db.set(-120.0);
        self.shared.speech.store(false, Ordering::Relaxed);
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.teardown();
    }
}

// ---------------------------------------------------------------- input side

/// One destination for processed audio: a ring an output callback drains.
struct Sink {
    ring: Arc<Mutex<AudioRing>>,
    resampler: LinearResampler,
    scratch: Vec<f32>,
    monitor: bool,
}

impl Sink {
    fn new(ring: Arc<Mutex<AudioRing>>, rate: u32, monitor: bool) -> Self {
        Sink {
            ring,
            resampler: LinearResampler::new(PROC_RATE, rate),
            scratch: Vec::with_capacity(PROC_HOP * 5),
            monitor,
        }
    }
}

/// Everything the input callback owns.
struct InputProc {
    channels: usize,
    mono: Vec<f32>,
    resampler: LinearResampler,
    pending: Vec<f32>,
    enhancer: SpeechEnhancer,
    rnnoise: Option<Box<DenoiseState<'static>>>,
    rn_buf: [f32; PROC_HOP],
    hop: [f32; PROC_HOP],
    sinks: Vec<Sink>,
    shared: Arc<Shared>,
}

impl InputProc {
    fn push<T>(&mut self, data: &[T])
    where
        T: Sample,
        f32: FromSample<T>,
    {
        let ch = self.channels;
        let frames = data.len() / ch;
        self.mono.clear();
        if ch == 1 {
            self.mono.extend(data.iter().map(|s| f32::from_sample(*s)));
        } else {
            let scale = 1.0 / ch as f32;
            self.mono.extend(
                data.chunks_exact(ch)
                    .take(frames)
                    .map(|f| f.iter().map(|s| f32::from_sample(*s)).sum::<f32>() * scale),
            );
        }
        self.resampler.process(&self.mono, &mut self.pending);

        let mut offset = 0;
        while self.pending.len() - offset >= PROC_HOP {
            self.hop.copy_from_slice(&self.pending[offset..offset + PROC_HOP]);
            offset += PROC_HOP;
            self.process_hop();
        }
        self.pending.drain(..offset);
    }

    fn process_hop(&mut self) {
        let shared = &*self.shared;
        if shared.params_dirty.swap(false, Ordering::Acquire) {
            // try_lock: the UI holds it for a single copy, and if we miss it
            // now the flag is set again so the next hop picks it up.
            match shared.params.try_lock() {
                Ok(p) => self.enhancer.set_params(*p),
                Err(_) => shared.params_dirty.store(true, Ordering::Release),
            }
        }

        if shared.bypass.load(Ordering::Relaxed) {
            let level = rms_db(&self.hop);
            shared.in_level_db.set(level);
            shared.out_level_db.set(level);
            shared.speech.store(false, Ordering::Relaxed);
        } else {
            if let Some(rn) = self.rnnoise.as_mut() {
                for s in self.hop.iter_mut() {
                    *s *= RNNOISE_SCALE;
                }
                rn.process_frame(&mut self.rn_buf, &self.hop);
                for (d, s) in self.hop.iter_mut().zip(&self.rn_buf) {
                    *d = s / RNNOISE_SCALE;
                }
            }
            self.enhancer.process(&mut self.hop);
            shared.in_level_db.set(self.enhancer.in_level_db);
            shared.out_level_db.set(self.enhancer.out_level_db);
            shared.speech.store(self.enhancer.speech, Ordering::Relaxed);
        }

        let mon_gain = shared.monitor_gain.get();
        for sink in &mut self.sinks {
            sink.scratch.clear();
            sink.resampler.process(&self.hop, &mut sink.scratch);
            if sink.monitor {
                sink.scratch.iter_mut().for_each(|s| *s *= mon_gain);
            }
            lock(&sink.ring).write(&sink.scratch);
        }
    }
}

fn error_callback(shared: Arc<Shared>, wake: Arc<dyn Fn() + Send + Sync>, what: &'static str) -> impl FnMut(cpal::Error) + Send + 'static {
    move |e: cpal::Error| {
        if e.kind() == cpal::ErrorKind::DeviceChanged {
            return; // the stream rerouted itself and keeps running
        }
        log_warn!("{what} stream error: {e}");
        if !shared.failed.swap(true, Ordering::AcqRel) {
            wake();
        }
    }
}

fn open_input(
    dev: &cpal::Device,
    name: &str,
    mut proc: InputProc,
    shared: Arc<Shared>,
    wake: Arc<dyn Fn() + Send + Sync>,
) -> Result<(cpal::Stream, u32), EngineError> {
    let fail = |e: &dyn std::fmt::Display| EngineError::OpenInput { device: name.to_string(), error: e.to_string() };
    // Capture streams only accept the device's own format, so use it and
    // convert ourselves.
    let def = dev.default_input_config().map_err(|e| fail(&e))?;
    let config = StreamConfig { channels: def.channels(), sample_rate: def.sample_rate(), buffer_size: cpal::BufferSize::Default };
    proc.channels = config.channels.max(1) as usize;
    proc.resampler = LinearResampler::new(config.sample_rate, PROC_RATE);
    let rate = config.sample_rate;

    fn build<T>(
        dev: &cpal::Device,
        config: StreamConfig,
        mut proc: InputProc,
        err: impl FnMut(cpal::Error) + Send + 'static,
    ) -> Result<cpal::Stream, cpal::Error>
    where
        T: SizedSample + Send + 'static,
        f32: FromSample<T>,
    {
        dev.build_input_stream::<T, _, _>(config, move |data: &[T], _| proc.push(data), err, None)
    }

    let err = error_callback(shared, wake, "Input");
    let stream = match def.sample_format() {
        SampleFormat::F32 => build::<f32>(dev, config, proc, err),
        SampleFormat::I16 => build::<i16>(dev, config, proc, err),
        SampleFormat::I32 => build::<i32>(dev, config, proc, err),
        SampleFormat::U8 => build::<u8>(dev, config, proc, err),
        SampleFormat::F64 => build::<f64>(dev, config, proc, err),
        other => return Err(fail(&format!("unsupported sample format {other:?}"))),
    }
    .map_err(|e| fail(&e))?;
    Ok((stream, rate))
}

// --------------------------------------------------------------- output side

struct OpenedOutput {
    stream: cpal::Stream,
    ring: Arc<Mutex<AudioRing>>,
    rate: u32,
}

/// Open an output stream that drains a fresh ring buffer.
///
/// WASAPI converts output formats for us, so 48 kHz float is asked for first
/// and the producer never has to resample. `high_water_factor` sets how far
/// past the target latency the buffer may drift before it is trimmed.
fn open_output(
    dev: &cpal::Device,
    name: &str,
    shared: Arc<Shared>,
    wake: Arc<dyn Fn() + Send + Sync>,
    high_water_factor: f32,
) -> Result<OpenedOutput, EngineError> {
    let fail = |e: &dyn std::fmt::Display| EngineError::OpenOutput { device: name.to_string(), error: e.to_string() };
    let def = dev.default_output_config().map_err(|e| fail(&e))?;
    let channels = def.channels().max(1);

    let mut last_err = None;
    for rate in [PROC_RATE, def.sample_rate()] {
        let target = (rate as f32 * TARGET_LATENCY_MS / 1000.0) as usize;
        let high_water = if high_water_factor > 1.0 {
            (target as f32 * high_water_factor) as usize
        } else {
            (rate as f32 * MAX_LATENCY_MS / 1000.0) as usize
        };
        let mut ring = AudioRing::new((rate as usize / 2).max(target * 4));
        ring.prefill(target); // the jitter buffer only helps if it starts full
        let ring = Arc::new(Mutex::new(ring));

        let config = StreamConfig { channels, sample_rate: rate, buffer_size: cpal::BufferSize::Default };
        let cb_ring = ring.clone();
        let ch = channels as usize;
        let mut mono: Vec<f32> = Vec::with_capacity(rate as usize / 10);
        let result = dev.build_output_stream::<f32, _, _>(
            config,
            move |data: &mut [f32], _| {
                let frames = data.len() / ch;
                if mono.len() < frames {
                    mono.resize(frames, 0.0);
                }
                {
                    let mut r = lock(&cb_ring);
                    r.trim(high_water, target);
                    r.pad(frames, target);
                    r.read_into(&mut mono[..frames]);
                }
                for (frame, &s) in data.chunks_exact_mut(ch).zip(&mono[..frames]) {
                    frame.fill(s);
                }
            },
            error_callback(shared.clone(), wake.clone(), "Output"),
            None,
        );
        match result {
            Ok(stream) => return Ok(OpenedOutput { stream, ring, rate }),
            Err(e) => {
                log_warn!("Output {name} refused {rate} Hz: {e}");
                last_err = Some(e);
            }
        }
        if def.sample_rate() == PROC_RATE {
            break;
        }
    }
    Err(fail(&last_err.map(|e| e.to_string()).unwrap_or_default()))
}
