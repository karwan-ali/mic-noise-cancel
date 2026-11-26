//! The neural first stage: a network that removes noise before the app's own
//! noise reduction and gate get the audio.
//!
//! Two models are built in:
//!
//! - **RNNoise** (via nnnoiseless): tiny and nearly free. Runs inside the
//!   capture callback like the rest of the chain.
//! - **DeepFilterNet 3** (Rikorose/DeepFilterNet, run with tract): a much
//!   larger network that also handles barking dogs, dishes and chatter, at
//!   about a millisecond of CPU per 10 ms hop and 20 ms more latency.
//!
//! tract's model state is not `Send` (it holds `Rc`s), so DeepFilterNet cannot
//! move into an audio callback. Instead it gets a thread of its own,
//! [`DeepFilterWorker`], which builds the model there and keeps it for as long
//! as DeepFilterNet stays selected. The capture callback only hands samples
//! over; the worker runs the whole chain on them. That also keeps the heavier
//! work, and tract's allocations, out of the WASAPI callback.
//!
//! Both work on the same 10 ms hop at 48 kHz as the rest of the pipeline, so
//! one hop in gives one hop out. Everything here is platform independent, so
//! the tests run on any OS.

use std::fmt;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::JoinHandle;

use df::tract::{DfParams, DfTract, RuntimeParams};
use ndarray::{ArrayView2, ArrayViewMut2};
use nnnoiseless::DenoiseState;

pub const HOP: usize = 480; // 10 ms at 48 kHz
const SAMPLE_RATE: usize = 48000;
const RNNOISE_SCALE: f32 = 32768.0; // RNNoise works on int16-ranged floats
/// How much audio may wait for the worker before new samples are dropped.
/// Far more than it ever needs; it only bounds memory if the thread stalls.
const FEED_CAPACITY: usize = SAMPLE_RATE / 2;

/// Which network runs first. Stored in the config as two booleans, so files
/// written before DeepFilterNet existed keep their meaning.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NeuralModel {
    Off,
    RNNoise,
    DeepFilterNet,
}

impl NeuralModel {
    pub const ALL: [NeuralModel; 3] = [NeuralModel::Off, NeuralModel::RNNoise, NeuralModel::DeepFilterNet];
}

impl fmt::Display for NeuralModel {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            NeuralModel::Off => "off",
            NeuralModel::RNNoise => "RNNoise",
            NeuralModel::DeepFilterNet => "DeepFilterNet",
        })
    }
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

// ------------------------------------------------------------------ RNNoise

/// The stages light enough to run inside the capture callback.
pub enum Denoiser {
    Off,
    RNNoise { state: Box<DenoiseState<'static>>, out: Box<[f32; HOP]> },
}

impl Denoiser {
    pub fn off() -> Self {
        Denoiser::Off
    }

    pub fn rnnoise() -> Self {
        Denoiser::RNNoise { state: DenoiseState::new(), out: Box::new([0.0; HOP]) }
    }

    pub fn model(&self) -> NeuralModel {
        match self {
            Denoiser::Off => NeuralModel::Off,
            Denoiser::RNNoise { .. } => NeuralModel::RNNoise,
        }
    }

    /// Clean one 10 ms hop in place.
    pub fn process(&mut self, hop: &mut [f32; HOP]) {
        if let Denoiser::RNNoise { state, out } = self {
            for s in hop.iter_mut() {
                *s *= RNNOISE_SCALE;
            }
            state.process_frame(&mut out[..], hop);
            for (d, s) in hop.iter_mut().zip(out.iter()) {
                *d = s / RNNOISE_SCALE;
            }
        }
    }
}

// ------------------------------------------------------------ DeepFilterNet

/// One running DeepFilterNet stream. Lives and dies on the thread that made
/// it; the worker hands a `&mut` to the engine's chain for each batch.
pub struct DeepFilter {
    model: DfTract,
    out: [f32; HOP],
    atten_lim_db: f32,
    failed: bool,
}

impl DeepFilter {
    /// Build the DeepFilterNet 3 model embedded in the program. Parsing and
    /// optimising its three ONNX graphs takes a few hundred milliseconds.
    pub fn load() -> Result<Self, String> {
        // The archive is compiled in (the crate's "default-model" feature), so
        // unpacking it either always works or fails every test run.
        let params = DfParams::default();
        let model = DfTract::new(params, &RuntimeParams::default_with_ch(1)).map_err(|e| format!("{e:#}"))?;
        if model.hop_size != HOP || model.sr != SAMPLE_RATE {
            return Err(format!("model runs at {} Hz with a {}-sample hop, expected 48000 Hz and {HOP}", model.sr, model.hop_size));
        }
        Ok(DeepFilter { model, out: [0.0; HOP], atten_lim_db: 100.0, failed: false })
    }

    /// A fresh stream from an already loaded model: the compiled plans are
    /// shared, only the small recurrent state is copied.
    fn fresh(&self) -> Self {
        DeepFilter { model: self.model.clone(), out: [0.0; HOP], atten_lim_db: 100.0, failed: false }
    }

    /// How far the model delays the audio: the STFT overlap plus its lookahead.
    pub fn delay_ms(&self) -> f32 {
        let m = &self.model;
        let samples = m.fft_size - m.hop_size + m.lookahead * m.hop_size;
        1000.0 * samples as f32 / m.sr as f32
    }

    /// Limit how far noise is pushed down; 100 dB or more means no limit.
    pub fn set_atten_lim(&mut self, db: f32) {
        if (self.atten_lim_db - db).abs() > 1e-3 {
            self.atten_lim_db = db;
            self.model.set_atten_lim(db);
        }
    }

    /// Clean one 10 ms hop in place.
    pub fn process(&mut self, hop: &mut [f32; HOP]) {
        if self.failed {
            return; // pass through rather than go silent
        }
        let noisy = ArrayView2::from_shape((1, HOP), &hop[..]).expect("hop shape");
        let enhanced = ArrayViewMut2::from_shape((1, HOP), &mut self.out[..]).expect("hop shape");
        match self.model.process(noisy, enhanced) {
            Ok(_lsnr) => {
                for (d, &s) in hop.iter_mut().zip(self.out.iter()) {
                    // The network can overshoot a little on loud input; the
                    // limiter at the end of the chain catches the rest.
                    *d = if s.is_finite() { s.clamp(-1.0, 1.0) } else { 0.0 };
                }
            }
            Err(e) => {
                crate::log_error!("DeepFilterNet failed, passing audio through: {e:#}");
                self.failed = true;
            }
        }
    }
}

/// The engine's processing chain as the worker runs it: called with each
/// batch of 48 kHz mono samples and the DeepFilterNet stage to apply per hop.
pub type Job = Box<dyn FnMut(&[f32], &mut DeepFilter) + Send>;

/// Samples on their way from the capture callback to the worker.
struct Feed {
    state: Mutex<FeedState>,
    wake: Condvar,
    atten_lim_db: AtomicU32,
    dropped: AtomicBool,
}

struct FeedState {
    samples: Vec<f32>,
    detach: bool,
    stop: bool,
}

/// The capture callback's end of the feed. Never allocates: the buffer is
/// reserved up front, and if the worker ever falls that far behind, new
/// samples are dropped instead.
pub struct FeedHandle(Arc<Feed>);

impl FeedHandle {
    pub fn push(&self, samples: &[f32]) {
        {
            let mut st = lock(&self.0.state);
            let room = FEED_CAPACITY - st.samples.len().min(FEED_CAPACITY);
            if samples.len() > room {
                self.0.dropped.store(true, Ordering::Relaxed);
            }
            st.samples.extend_from_slice(&samples[..samples.len().min(room)]);
        }
        self.0.wake.notify_one();
    }
}

/// A thread that owns a loaded DeepFilterNet model and runs one processing
/// chain at a time on it.
pub struct DeepFilterWorker {
    feed: Arc<Feed>,
    jobs: Option<mpsc::Sender<Job>>,
    done: mpsc::Receiver<()>,
    thread: Option<JoinHandle<()>>,
    attached: bool,
    delay_ms: f32,
}

impl DeepFilterWorker {
    /// Start the thread and wait until the model has loaded on it.
    pub fn start() -> Result<Self, String> {
        let feed = Arc::new(Feed {
            state: Mutex::new(FeedState { samples: Vec::with_capacity(FEED_CAPACITY), detach: false, stop: false }),
            wake: Condvar::new(),
            atten_lim_db: AtomicU32::new(100f32.to_bits()),
            dropped: AtomicBool::new(false),
        });
        let (ready_tx, ready_rx) = mpsc::channel::<Result<f32, String>>();
        let (jobs_tx, jobs_rx) = mpsc::channel::<Job>();
        let (done_tx, done_rx) = mpsc::channel::<()>();
        let thread_feed = feed.clone();
        let thread = std::thread::Builder::new()
            .name("DeepFilterNet".into())
            .spawn(move || {
                raise_priority();
                let template = match DeepFilter::load() {
                    Ok(m) => {
                        let _ = ready_tx.send(Ok(m.delay_ms()));
                        m
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        return;
                    }
                };
                while let Ok(job) = jobs_rx.recv() {
                    run_session(&thread_feed, &template, job);
                    let _ = done_tx.send(());
                }
            })
            .map_err(|e| e.to_string())?;
        let delay_ms = match ready_rx.recv() {
            Ok(Ok(delay)) => delay,
            Ok(Err(e)) => {
                let _ = thread.join();
                return Err(e);
            }
            Err(_) => {
                let _ = thread.join();
                return Err("the DeepFilterNet thread ended while loading".into());
            }
        };
        Ok(DeepFilterWorker { feed, jobs: Some(jobs_tx), done: done_rx, thread: Some(thread), attached: false, delay_ms })
    }

    pub fn delay_ms(&self) -> f32 {
        self.delay_ms
    }

    /// Takes effect from the next batch the worker picks up.
    pub fn set_atten_lim(&self, db: f32) {
        self.feed.atten_lim_db.store(db.to_bits(), Ordering::Relaxed);
    }

    /// Start running `job` on a fresh DeepFilterNet stream, and get the
    /// handle the capture callback feeds it through.
    pub fn attach(&mut self, job: Job) -> FeedHandle {
        self.detach();
        {
            let mut st = lock(&self.feed.state);
            st.samples.clear();
            st.detach = false;
        }
        if let Some(jobs) = &self.jobs {
            self.attached = jobs.send(job).is_ok();
        }
        FeedHandle(self.feed.clone())
    }

    /// End the current job and wait until the worker has dropped it. Call
    /// after the capture stream has stopped, so nothing feeds it any more.
    pub fn detach(&mut self) {
        if !std::mem::take(&mut self.attached) {
            return;
        }
        lock(&self.feed.state).detach = true;
        self.feed.wake.notify_one();
        let _ = self.done.recv();
        if self.feed.dropped.swap(false, Ordering::Relaxed) {
            crate::log_warn!("DeepFilterNet fell behind and some audio was dropped");
        }
    }
}

impl Drop for DeepFilterWorker {
    fn drop(&mut self) {
        self.detach();
        lock(&self.feed.state).stop = true;
        self.feed.wake.notify_one();
        self.jobs = None; // ends the thread's wait for the next job
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

fn run_session(feed: &Feed, template: &DeepFilter, mut job: Job) {
    let mut stage = template.fresh();
    let mut batch: Vec<f32> = Vec::with_capacity(FEED_CAPACITY);
    loop {
        {
            let mut st = lock(&feed.state);
            while st.samples.is_empty() && !st.detach && !st.stop {
                st = feed.wake.wait(st).unwrap_or_else(|e| e.into_inner());
            }
            if st.detach || st.stop {
                st.detach = false;
                st.samples.clear();
                return;
            }
            // Swapping keeps both buffers' capacity: nothing is allocated.
            std::mem::swap(&mut st.samples, &mut batch);
        }
        stage.set_atten_lim(f32::from_bits(feed.atten_lim_db.load(Ordering::Relaxed)));
        job(&batch, &mut stage);
        batch.clear();
    }
}

/// The worker produces audio someone is waiting to hear, like the WASAPI
/// threads it sits between.
fn raise_priority() {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Threading::{GetCurrentThread, SetThreadPriority, THREAD_PRIORITY_TIME_CRITICAL};
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::*;

    fn run(f: &mut dyn FnMut(&mut [f32; HOP]), x: &[f32]) -> Vec<f32> {
        let mut out = Vec::with_capacity(x.len());
        let mut buf = [0.0f32; HOP];
        for chunk in x.chunks_exact(HOP) {
            buf.copy_from_slice(chunk);
            f(&mut buf);
            out.extend_from_slice(&buf);
        }
        out
    }

    fn select(x: &[f32], mask: &[bool]) -> Vec<f32> {
        x.iter().zip(mask).filter(|(_, &m)| m).map(|(&s, _)| s).collect()
    }

    /// (background reduction, speech level vs clean) in dB, with `y`
    /// already lined up with the input.
    fn score(scene: &Scene, y: &[f32]) -> (f32, f32) {
        let (voiced, silence) = settled_masks(&scene.clean);
        let a = frames_to_samples(&voiced);
        let b = frames_to_samples(&silence);
        let n = y.len().min(a.len());
        let in_n = db(&select(&scene.mixed[..n], &b[..n]));
        let out_n = db(&select(&y[..n], &b[..n]));
        let clean_s = db(&select(&scene.clean[..n], &a[..n]));
        let out_s = db(&select(&y[..n], &a[..n]));
        (in_n - out_n, out_s - clean_s)
    }

    #[test]
    fn deep_filter_loads_with_the_expected_shape() {
        let start = std::time::Instant::now();
        let model = DeepFilter::load().expect("model loads");
        let loaded = start.elapsed();
        let start = std::time::Instant::now();
        let _stream = model.fresh();
        let delay = model.delay_ms();
        eprintln!("DeepFilterNet loads in {loaded:.0?}, a stream clones in {:.1?}, delay {delay:.0} ms", start.elapsed());
        assert!((10.0..=50.0).contains(&delay), "delay {delay} ms");
    }

    #[test]
    fn deep_filter_removes_noise_and_keeps_speech() {
        let model = DeepFilter::load().unwrap();
        let delay = (model.delay_ms() * 48.0).round() as usize;
        for (room, amp) in [("headset", 0.008f32), ("noisy room", 0.05)] {
            let scene = make_scene(amp, 7);
            let mut d = model.fresh();
            let y = run(&mut |h| d.process(h), &scene.mixed);
            let y = &y[delay..];
            let (noise, speech) = score(&scene, y);
            eprintln!("{room:>10}: background -{noise:.1} dB, speech {speech:+.1} dB vs clean");
            assert!(noise >= 15.0, "{room}: background down only {noise:.1} dB");
            assert!(speech.abs() < 8.0, "{room}: speech {speech:+.1} dB");
            assert!(y.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
        }
    }

    #[test]
    fn deep_filter_attenuation_limit_is_respected() {
        let model = DeepFilter::load().unwrap();
        let mut rng = Rng::new(3);
        let noise: Vec<f32> = (0..48000 * 4).map(|_| rng.normal() * 0.03).collect();
        let tail = 48000 * 2; // judge the last two seconds, once settled
        let in_db = db(&noise[tail..]);
        let reduction = |lim: f32| {
            let mut d = model.fresh();
            d.set_atten_lim(lim);
            in_db - db(&run(&mut |h| d.process(h), &noise)[tail..])
        };
        let (limited, full) = (reduction(12.0), reduction(100.0));
        eprintln!("pure noise: -{limited:.1} dB at a 12 dB limit, -{full:.1} dB unlimited");
        assert!(limited < 14.0, "limit ignored: -{limited:.1} dB");
        assert!(full > limited + 6.0, "unlimited -{full:.1} dB vs limited -{limited:.1} dB");

        // Moving the limit while running takes effect.
        let mut d = model.fresh();
        run(&mut |h| d.process(h), &noise[..tail]);
        d.set_atten_lim(12.0);
        let after = in_db - db(&run(&mut |h| d.process(h), &noise[tail..])[48000..]);
        assert!(after < 14.0, "live change ignored: -{after:.1} dB");
    }

    #[test]
    fn deep_filter_survives_edge_input() {
        let mut d = DeepFilter::load().unwrap();
        let mut rng = Rng::new(5);
        for block in [[0.0f32; HOP], [1.0; HOP], [-1.0; HOP], [1e-9; HOP], [0.0; HOP]] {
            for _ in 0..20 {
                let mut b = block;
                b[0] += rng.normal() * 0.1;
                d.process(&mut b);
                assert!(b.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
            }
        }
        assert!(!d.failed);
    }

    #[test]
    fn rnnoise_and_off_keep_their_behaviour() {
        let scene = make_scene(0.05, 9);
        let mut off = Denoiser::off();
        assert_eq!(run(&mut |h| off.process(h), &scene.mixed[..HOP * 10]), scene.mixed[..HOP * 10]);
        let mut rn = Denoiser::rnnoise();
        let y = run(&mut |h| rn.process(h), &scene.mixed);
        assert!(y.iter().all(|s| s.is_finite()));
        assert!(db(&y) < db(&scene.mixed), "RNNoise removed nothing");
    }

    #[test]
    fn deep_filter_is_fast_enough_for_real_time() {
        let mut d = DeepFilter::load().unwrap();
        let scene = make_scene(0.05, 9);
        let start = std::time::Instant::now();
        let y = run(&mut |h| d.process(h), &scene.mixed);
        let rtf = y.len() as f64 / 48000.0 / start.elapsed().as_secs_f64();
        eprintln!("DeepFilterNet: {:.3} ms per 10 ms hop ({rtf:.1}x real time)", 10.0 / rtf);
        // Debug builds are far slower; this only guards against something
        // pathological in a release test run.
        if !cfg!(debug_assertions) {
            assert!(rtf > 2.0, "{rtf:.1}x real time");
        }
    }

    /// A job that does what the engine's does: cut batches into hops, run
    /// DeepFilterNet on each, and collect the result.
    fn collecting_job(out: Arc<Mutex<Vec<f32>>>) -> Job {
        let mut pending: Vec<f32> = Vec::new();
        Box::new(move |samples, df| {
            pending.extend_from_slice(samples);
            let mut hop = [0.0f32; HOP];
            let mut offset = 0;
            while pending.len() - offset >= HOP {
                hop.copy_from_slice(&pending[offset..offset + HOP]);
                offset += HOP;
                df.process(&mut hop);
                lock(&out).extend_from_slice(&hop);
            }
            pending.drain(..offset);
        })
    }

    /// Feed `x` in `chunk`-sized pieces, far faster than real time but
    /// without outrunning the feed's capacity, and wait for all of it.
    fn feed_all(feed: &FeedHandle, out: &Mutex<Vec<f32>>, x: &[f32], chunk: usize) {
        for piece in x.chunks(chunk) {
            while lock(&feed.0.state).samples.len() > FEED_CAPACITY / 2 {
                std::thread::sleep(std::time::Duration::from_millis(2)); // let it catch up
            }
            feed.push(piece);
        }
        let full = x.len() / HOP * HOP;
        let start = std::time::Instant::now();
        while lock(out).len() < full && start.elapsed().as_secs() < 30 {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(lock(out).len(), full, "every hop came back");
    }

    #[test]
    fn worker_runs_sessions_on_its_own_thread() {
        let mut worker = DeepFilterWorker::start().expect("worker starts");
        let delay = (worker.delay_ms() * 48.0).round() as usize;
        let scene = make_scene(0.05, 7);

        for session in 0..2 {
            let out = Arc::new(Mutex::new(Vec::new()));
            let feed = worker.attach(collecting_job(out.clone()));
            // Odd-sized pushes, like a capture callback at 44.1 kHz would make.
            feed_all(&feed, &out, &scene.mixed, 441);
            drop(feed);
            worker.detach();

            let y = lock(&out).clone();
            let (noise, speech) = score(&scene, &y[delay..]);
            eprintln!("worker session {session}: background -{noise:.1} dB, speech {speech:+.1} dB");
            assert!(noise >= 15.0 && speech.abs() < 8.0, "session {session}");
        }

        // A limit set from outside reaches the worker's stream.
        worker.set_atten_lim(12.0);
        let mut rng = Rng::new(3);
        let noise: Vec<f32> = (0..48000 * 3).map(|_| rng.normal() * 0.03).collect();
        let out = Arc::new(Mutex::new(Vec::new()));
        let feed = worker.attach(collecting_job(out.clone()));
        feed_all(&feed, &out, &noise, 480);
        worker.detach();
        let y = lock(&out).clone();
        let reduction = db(&noise[48000..]) - db(&y[48000..]);
        assert!(reduction < 14.0, "limit not applied: -{reduction:.1} dB");

        drop(worker); // joins the thread; hangs here if shutdown is broken
    }

    #[test]
    fn worker_drops_rather_than_grows_when_it_falls_behind() {
        let mut worker = DeepFilterWorker::start().unwrap();
        // A job that stalls, as if the thread were starved.
        let gate = Arc::new(Mutex::new(()));
        let held = lock(&gate);
        let job_gate = gate.clone();
        let feed = worker.attach(Box::new(move |_, _| drop(lock(&job_gate))));
        let second = [0.0f32; SAMPLE_RATE];
        for _ in 0..3 {
            feed.push(&second);
        }
        {
            let st = lock(&feed.0.state);
            assert!(st.samples.len() <= FEED_CAPACITY);
            assert!(st.samples.capacity() <= FEED_CAPACITY, "the feed reallocated");
        }
        assert!(worker.feed.dropped.load(Ordering::Relaxed));
        drop(held);
        drop(feed);
        worker.detach(); // must not hang on a job that was stalled
    }
}
