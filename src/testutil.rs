//! Synthetic test signals shared by the unit tests.

pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    pub fn uniform(&mut self) -> f32 {
        // xorshift64*
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        let v = self.0.wrapping_mul(0x2545_F491_4F6C_DD1D);
        ((v >> 40) as f32 + 0.5) / (1u64 << 24) as f32
    }

    pub fn normal(&mut self) -> f32 {
        let (u1, u2) = (self.uniform(), self.uniform());
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f32::consts::PI * u2).cos()
    }
}

const SR: f32 = 48000.0;
const HOP: usize = 480;

pub fn db(x: &[f32]) -> f32 {
    if x.is_empty() {
        return -200.0;
    }
    let r = (x.iter().map(|&s| (s as f64).powi(2)).sum::<f64>() / x.len() as f64).sqrt();
    if r > 1e-12 {
        (20.0 * r.log10()) as f32
    } else {
        -200.0
    }
}

/// Harmonic stack with a wandering F0, syllable envelope and fricative bursts.
pub fn synth_speech(seconds: f32, rng: &mut Rng) -> Vec<f32> {
    let n = (seconds * SR) as usize;
    let mut phase = 0.0f64;
    let mut sig: Vec<f32> = (0..n)
        .map(|i| {
            let t = i as f32 / SR;
            let f0 = 130.0 + 30.0 * (2.0 * std::f32::consts::PI * 1.1 * t).sin();
            phase += 2.0 * std::f64::consts::PI * f0 as f64 / SR as f64;
            let mut s = 0.0f32;
            for (k, amp) in [1.0f32, 0.6, 0.45, 0.3, 0.18, 0.1].iter().enumerate() {
                s += amp * ((k as f64 + 1.0) * phase).sin() as f32;
            }
            let env = 0.35
                + 0.65 * (0.5 + 0.5 * (2.0 * std::f32::consts::PI * 4.0 * t - 1.0).sin()).powf(0.7);
            s * env
        })
        .collect();
    let peak = sig.iter().fold(0.0f32, |m, s| m.max(s.abs()));
    for (i, s) in sig.iter_mut().enumerate() {
        let t = i as f32 / SR;
        let hiss = if (2.0 * std::f32::consts::PI * 2.0 * t).sin().abs() > 0.97 { rng.normal() } else { 0.0 };
        *s = *s / peak + 0.25 * hiss;
    }
    let peak = sig.iter().fold(0.0f32, |m, s| m.max(s.abs()));
    sig.iter_mut().for_each(|s| *s = *s / peak * 0.35);
    sig
}

pub struct Scene {
    pub clean: Vec<f32>,
    pub mixed: Vec<f32>,
}

/// Speech bursts separated by real pauses, all under steady broadband noise.
pub fn make_scene(noise_amp: f32, seed: u64) -> Scene {
    let mut rng = Rng::new(seed);
    let n = (11.0 * SR) as usize;
    let mut clean = vec![0.0f32; n];
    for (start, len) in [(2.5f32, 1.1f32), (4.6, 1.3), (6.9, 1.0), (9.0, 1.2)] {
        let seg = synth_speech(len, &mut rng);
        let i = (start * SR) as usize;
        let end = (i + seg.len()).min(n);
        clean[i..end].copy_from_slice(&seg[..end - i]);
    }
    let mixed = clean
        .iter()
        .enumerate()
        .map(|(i, &c)| {
            let t = i as f32 / SR;
            let mut nz = rng.normal() * noise_amp;
            nz += 0.4 * noise_amp * (2.0 * std::f32::consts::PI * 50.0 * t).sin();
            nz += 0.3 * noise_amp * (2.0 * std::f32::consts::PI * 210.0 * t).sin();
            c + nz
        })
        .collect();
    Scene { clean, mixed }
}

/// Frame masks for "clearly speaking" and "settled silence".
pub fn settled_masks(clean: &[f32]) -> (Vec<bool>, Vec<bool>) {
    let fps = SR / HOP as f32;
    let mut voiced: Vec<bool> = clean.chunks_exact(HOP).map(|c| db(c) > -40.0).collect();
    let settle = (0.35 * fps) as usize;
    let mut silence: Vec<bool> = voiced.iter().map(|v| !v).collect();
    for i in 0..silence.len() {
        if silence[i] && voiced[i.saturating_sub(settle)..i].iter().any(|&v| v) {
            silence[i] = false;
        }
    }
    let warm = (2.2 * fps) as usize;
    for i in 0..warm.min(voiced.len()) {
        voiced[i] = false;
        silence[i] = false;
    }
    (voiced, silence)
}

pub fn frames_to_samples(mask: &[bool]) -> Vec<bool> {
    mask.iter().flat_map(|&m| std::iter::repeat(m).take(HOP)).collect()
}
