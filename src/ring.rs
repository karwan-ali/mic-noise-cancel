//! A fixed-capacity sample ring buffer with drift correction.
//!
//! Shared between the processing side (input callback) and an output callback
//! behind a mutex. Every critical section is a handful of memcpys, and the
//! buffer itself is allocated once.

pub struct AudioRing {
    buf: Box<[f32]>,
    read: usize,
    write: usize,
    count: usize,
    pub overflows: u32,
    pub underflows: u32,
    pub drops: u32,
    pub pads: u32,
}

impl AudioRing {
    pub fn new(capacity: usize) -> Self {
        AudioRing {
            buf: vec![0.0; capacity.max(1)].into_boxed_slice(),
            read: 0,
            write: 0,
            count: 0,
            overflows: 0,
            underflows: 0,
            drops: 0,
            pads: 0,
        }
    }

    pub fn capacity(&self) -> usize {
        self.buf.len()
    }

    #[cfg(test)]
    pub fn available(&self) -> usize {
        self.count
    }

    /// Append samples. On overflow the oldest samples are dropped.
    pub fn write(&mut self, mut data: &[f32]) {
        let cap = self.capacity();
        if data.len() > cap {
            data = &data[data.len() - cap..]; // pathological block; keep the tail
        }
        let n = data.len();
        if n == 0 {
            return;
        }
        let free = cap - self.count;
        if n > free {
            self.advance_read(n - free);
            self.overflows += 1;
        }
        let first = n.min(cap - self.write);
        self.buf[self.write..self.write + first].copy_from_slice(&data[..first]);
        self.buf[..n - first].copy_from_slice(&data[first..]);
        self.write = (self.write + n) % cap;
        self.count += n;
    }

    /// Fill `out` completely, zero-padding if the buffer ran dry.
    pub fn read_into(&mut self, out: &mut [f32]) {
        let n = out.len();
        let take = n.min(self.count);
        if take < n {
            self.underflows += 1;
        }
        let cap = self.capacity();
        let first = take.min(cap - self.read);
        out[..first].copy_from_slice(&self.buf[self.read..self.read + first]);
        out[first..take].copy_from_slice(&self.buf[..take - first]);
        out[take..].fill(0.0);
        self.advance_read(take);
    }

    /// Seed the buffer with silence so it starts at its target level.
    pub fn prefill(&mut self, samples: usize) {
        self.insert_silence(samples.min(self.capacity() - self.count));
    }

    /// Top the buffer back up with silence once clock drift has drained it.
    ///
    /// The two clocks differ by a fixed fraction, so with no correction the
    /// consumer eventually outruns the producer for good. One deliberate gap,
    /// inserted when the buffer is already empty, restores the margin.
    pub fn pad(&mut self, low_water: usize, target: usize) -> usize {
        if self.count > low_water || target <= self.count {
            return 0;
        }
        let n = (target - self.count).min(self.capacity() - self.count);
        self.insert_silence(n);
        self.pads += 1;
        n
    }

    /// Drop old samples once the buffer has drifted past `high_water`.
    ///
    /// Nothing happens between the watermarks, so ordinary jitter is absorbed.
    /// Cutting during a quiet stretch hides the seam, but a buffer far past the
    /// mark is trimmed regardless: growing latency beats one faint click.
    pub fn trim(&mut self, high_water: usize, target: usize) -> usize {
        const QUIET: f32 = 0.02;
        if self.count <= high_water || self.count <= target {
            return 0;
        }
        let n = self.count - target;
        if (self.count as f32) < high_water as f32 * 1.5 && self.peek_rms(n) > QUIET {
            return 0; // wait for a gap in the speech
        }
        self.advance_read(n);
        self.drops += 1;
        n
    }

    fn insert_silence(&mut self, n: usize) {
        let cap = self.capacity();
        let first = n.min(cap - self.write);
        self.buf[self.write..self.write + first].fill(0.0);
        self.buf[..n - first].fill(0.0);
        self.write = (self.write + n) % cap;
        self.count += n;
    }

    fn advance_read(&mut self, n: usize) {
        self.read = (self.read + n) % self.capacity();
        self.count -= n;
    }

    fn peek_rms(&self, n: usize) -> f32 {
        if n == 0 {
            return 0.0;
        }
        let cap = self.capacity();
        let first = n.min(cap - self.read);
        let sq = |s: &[f32]| s.iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>();
        let total = sq(&self.buf[self.read..self.read + first]) + sq(&self.buf[..n - first]);
        (total / n as f64).sqrt() as f32
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(r: &mut AudioRing, n: usize) -> Vec<f32> {
        let mut v = vec![9.0; n];
        r.read_into(&mut v);
        v
    }

    #[test]
    fn round_trip_overflow_underflow() {
        let mut r = AudioRing::new(1000);
        let data: Vec<f32> = (0..600).map(|i| i as f32).collect();
        r.write(&data);
        assert_eq!(read(&mut r, 600), data);
        assert_eq!(r.available(), 0);
        r.write(&[1.0; 900]);
        r.write(&[2.0; 300]);
        assert_eq!((r.overflows, r.available()), (1, 1000));
        let out = read(&mut r, 2000);
        assert_eq!((out.len(), out[1999], r.underflows), (2000, 0.0, 1));
    }

    #[test]
    fn trim_and_pad() {
        let mut r = AudioRing::new(1000);
        r.write(&[0.0; 800]);
        assert_eq!(r.trim(400, 200), 600);
        assert_eq!(r.available(), 200);

        let mut r = AudioRing::new(1000);
        r.write(&[1.0; 400]);
        assert_eq!(r.trim(300, 200), 0, "defers while loud");

        let mut r = AudioRing::new(1000);
        r.write(&[1.0; 900]);
        assert_eq!(r.trim(300, 300), 600, "forces through when far over");

        let mut r = AudioRing::new(1000);
        r.write(&[1.0; 500]);
        assert_eq!(r.trim(800, 200), 0);

        let mut r = AudioRing::new(1000);
        r.prefill(300);
        assert_eq!(r.available(), 300);
        read(&mut r, 280);
        assert_eq!(r.pad(100, 300), 280);
        assert_eq!(r.available(), 300);
        assert_eq!(r.pad(100, 300), 0);
    }

    #[test]
    fn wraps_around() {
        let mut r = AudioRing::new(10);
        for round in 0..50 {
            let data: Vec<f32> = (0..7).map(|i| (round * 7 + i) as f32).collect();
            r.write(&data);
            assert_eq!(read(&mut r, 7), data);
        }
    }
}
