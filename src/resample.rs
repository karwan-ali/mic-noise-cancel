//! Streaming linear resampler.
//!
//! Only used when the microphone does not run at 48 kHz, the rate the enhancer
//! and RNNoise work at. Output streams never need it: WASAPI converts those.
//! Linear interpolation is modest but perfectly adequate for speech.

pub struct LinearResampler {
    step: f64, // input samples per output sample
    pos: f64,  // fractional read position; 0 is the sample carried over
    last: f32,
    passthrough: bool,
}

impl LinearResampler {
    pub fn new(src_rate: u32, dst_rate: u32) -> Self {
        LinearResampler {
            step: src_rate as f64 / dst_rate as f64,
            pos: 0.0,
            last: 0.0,
            passthrough: src_rate == dst_rate,
        }
    }

    /// Resample `input`, appending to `out`. Allocation-free once `out` has
    /// grown to the largest block it will ever see.
    pub fn process(&mut self, input: &[f32], out: &mut Vec<f32>) {
        if self.passthrough {
            out.extend_from_slice(input);
            return;
        }
        let n = input.len();
        if n == 0 {
            return;
        }
        // Conceptually buf = [last, input...], so index i is input[i - 1].
        let at = |i: usize| if i == 0 { self.last } else { input[i - 1] };
        let end = n as f64;
        while self.pos <= end {
            let i = self.pos as usize;
            let frac = (self.pos - i as f64) as f32;
            let a = at(i);
            let s = if i >= n { a } else { a + (at(i + 1) - a) * frac };
            out.push(s);
            self.pos += self.step;
        }
        self.pos -= end;
        self.last = input[n - 1];
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_the_rate() {
        for (src, dst) in [(44100, 48000), (48000, 44100), (48000, 48000), (16000, 48000), (48000, 32000)] {
            let mut rs = LinearResampler::new(src, dst);
            let block = vec![0.0f32; (src / 100) as usize];
            let (mut ti, mut to) = (0usize, 0usize);
            let mut out = Vec::new();
            for _ in 0..300 {
                out.clear();
                rs.process(&block, &mut out);
                ti += block.len();
                to += out.len();
            }
            let ratio = to as f64 / ti as f64;
            assert!((ratio - dst as f64 / src as f64).abs() < 0.005, "{src}->{dst}: {ratio}");
        }
    }

    #[test]
    fn tone_stays_bounded_and_continuous() {
        let mut rs = LinearResampler::new(48000, 24000);
        let tone: Vec<f32> =
            (0..4800).map(|i| (2.0 * std::f32::consts::PI * 400.0 * i as f32 / 48000.0).sin()).collect();
        let mut out = Vec::new();
        for c in tone.chunks(480) {
            rs.process(c, &mut out);
        }
        assert!(out.iter().all(|s| s.is_finite() && s.abs() < 1.05));
        // 400 Hz at 24 kHz moves at most ~0.105 per sample; a seam would jump.
        assert!(out.windows(2).all(|w| (w[1] - w[0]).abs() < 0.12));
    }
}
