"""Offline checks for the enhancer, ring buffer and resampler."""
import importlib.util
import math
import os
import sys
import time

import numpy as np

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "micnoise")


def load(name):
    spec = importlib.util.spec_from_file_location("m_" + name, os.path.join(PKG, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dsp = load("dsp")
ring = load("ring")
resample = load("resample")

SR, HOP = 48000, 480
FPS = SR / HOP
rng = np.random.default_rng(7)

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def db(x):
    r = float(np.sqrt(np.mean(np.square(np.asarray(x, np.float64))))) if len(x) else 0.0
    return 20 * math.log10(r) if r > 1e-12 else -200.0


def synth_speech(seconds, sr=SR):
    """Harmonic stack with a wandering F0, syllable envelope and fricative bursts."""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    f0 = 130 + 30 * np.sin(2 * np.pi * 1.1 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    sig = np.zeros(n)
    for k, amp in enumerate([1.0, 0.6, 0.45, 0.3, 0.18, 0.1], start=1):
        sig += amp * np.sin(k * phase)
    # Syllables: never fully silent between them, the way real speech isn't.
    env = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t - 1.0)) ** 0.7
    sig *= env
    sig /= np.max(np.abs(sig))
    # A couple of /s/-like bursts so the band is not purely harmonic.
    hiss = rng.normal(0, 1, n) * (np.abs(np.sin(2 * np.pi * 2.0 * t)) > 0.97)
    sig = sig + 0.25 * hiss
    return (sig / np.max(np.abs(sig)) * 0.35).astype(np.float32)


def make_scene(seconds=11.0, bursts=((2.5, 1.1), (4.6, 1.3), (6.9, 1.0), (9.0, 1.2)),
               noise_amp=0.05):
    """Speech bursts separated by real pauses, all under steady broadband noise."""
    n = int(seconds * SR)
    clean = np.zeros(n, np.float32)
    for start, length in bursts:
        seg = synth_speech(length)
        i = int(start * SR)
        clean[i:i + seg.size] = seg[: max(0, n - i)]
    t = np.arange(n) / SR
    noise = rng.normal(0, noise_amp, n).astype(np.float32)
    noise += (0.4 * noise_amp * np.sin(2 * np.pi * 50 * t)).astype(np.float32)  # mains hum
    noise += (0.3 * noise_amp * np.sin(2 * np.pi * 210 * t)).astype(np.float32)  # fan whine
    return clean, noise, (clean + noise).astype(np.float32)


# Two rooms: a headset (quiet) and a noisy laptop-in-a-cafe situation.
SCENES = {"headset": 0.008, "noisy room": 0.05}


def frame_rms(x):
    return np.array([db(x[i:i + HOP]) for i in range(0, len(x) - HOP + 1, HOP)])


def run(enh, x, trace=False):
    out, gates, speech = [], [], []
    for i in range(0, len(x) - HOP + 1, HOP):
        out.append(enh.process(x[i:i + HOP]))
        if trace:
            gates.append(enh.gate_db)
            speech.append(enh.speech)
    y = np.concatenate(out)
    return (y, np.array(gates), np.array(speech)) if trace else y


def settled_masks(clean, warmup_s=2.2, settle_s=0.35):
    """Frame masks for 'clearly speaking' and 'settled silence'."""
    lvl = frame_rms(clean)
    voiced = lvl > -40.0
    settle = int(settle_s * FPS)
    silence = ~voiced.copy()
    for i in range(len(silence)):  # drop the tail right after each burst
        if silence[i] and np.any(voiced[max(0, i - settle):i]):
            silence[i] = False
    warm = int(warmup_s * FPS)
    voiced[:warm] = False
    silence[:warm] = False
    return voiced, silence


def frames_to_samples(mask):
    out = np.zeros(len(mask) * HOP, bool)
    for i, v in enumerate(mask):
        out[i * HOP:(i + 1) * HOP] = v
    return out


print("=== 1. Perfect reconstruction (WOLA identity) ===")
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams(
    reduction_db=0.0, gate_enabled=False, highpass_hz=0.0, makeup_gain_db=0.0))
x = rng.normal(0, 0.2, SR).astype(np.float32)
y = run(enh, x)
err = np.max(np.abs(y[HOP:HOP + 20000] - x[:20000]))  # one hop of algorithmic delay
check("output matches input when the gain is 1", err < 1e-3, "max err %.1e" % err)

print("\n=== 2. Noise reduction, measured against the clean reference ===")
clean, noise, mixed = make_scene()
voiced, silence = settled_masks(clean)
vs, ss = frames_to_samples(voiced), frames_to_samples(silence)
print("  scene: %.1f s, %d voiced frames, %d settled-silence frames"
      % (len(clean) / SR, voiced.sum(), silence.sum()))

PRESETS = [
    ("light", dsp.EnhancerParams(reduction_db=14, gate_enabled=False, highpass_hz=70,
                                 vad_threshold_db=1.5)),
    ("balanced", dsp.EnhancerParams(reduction_db=22, gate_enabled=True, gate_floor_db=-28,
                                    vad_threshold_db=1.5, highpass_hz=80)),
    ("aggressive", dsp.EnhancerParams(reduction_db=30, gate_enabled=True, gate_floor_db=-60,
                                      vad_threshold_db=1.3, highpass_hz=90)),
]

for room, amp in SCENES.items():
    ck, nz, mx = make_scene(noise_amp=amp)
    v, sl = settled_masks(ck)
    a, b = frames_to_samples(v), frames_to_samples(sl)
    print("  -- %s (input SNR %.0f dB) --" % (room, db(ck[a]) - db(nz[b])))
    for preset, params in PRESETS:
        enh = dsp.SpeechEnhancer(SR, HOP, params)
        y = run(enh, mx)
        n = min(len(y), len(a))
        yy, mm, cc = y[:n], mx[:n], ck[:n]
        av, bv = a[:n], b[:n]
        in_n, out_n = db(mm[bv]), db(yy[bv])
        # Against the clean signal: removing the noise that sat on top of the
        # speech is not "losing speech", so mixed[] is the wrong reference.
        clean_s, out_s = db(cc[av]), db(yy[av])
        print("     %-11s silence %6.1f -> %6.1f (-%4.1f)   speech vs clean %+5.1f dB"
              % (preset, in_n, out_n, in_n - out_n, out_s - clean_s))
        floor = {"light": 6.0, "balanced": 18.0, "aggressive": 30.0}[preset]
        check("%s/%s: background down >= %.0f dB" % (room, preset, floor),
              in_n - out_n >= floor, "%.1f dB" % (in_n - out_n))
        check("%s/%s: speech within 6 dB of clean" % (room, preset),
              abs(out_s - clean_s) < 6.0, "%+.1f dB" % (out_s - clean_s))
        check("%s/%s: finite, no clipping" % (room, preset),
              np.all(np.isfinite(y)) and np.max(np.abs(y)) <= 1.0)

print("\n=== 3. Gate behaviour and onset latency ===")
for room, amp in SCENES.items():
    ck, _, mx = make_scene(noise_amp=amp)
    v, sl = settled_masks(ck)
    enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
    y, gates, flags = run(enh, mx, trace=True)
    n = min(len(gates), len(v))
    gates, flags, v, sl = gates[:n], flags[:n], v[:n], sl[:n]
    print("  -- %s: gate %.1f dB speaking, %.1f dB in silence; VAD %.0f%% / %.0f%%"
          % (room, np.median(gates[v]), np.median(gates[sl]),
             100 * flags[v].mean(), 100 * flags[sl].mean()))
    check("%s: gate open while speaking" % room, np.median(gates[v]) > -2.0,
          "%.1f dB" % np.median(gates[v]))
    check("%s: gate shut in silence" % room, np.median(gates[sl]) < -35.0,
          "%.1f dB" % np.median(gates[sl]))
    check("%s: VAD tracks voiced frames" % room, flags[v].mean() > 0.90,
          "%.0f%%" % (100 * flags[v].mean()))
    check("%s: VAD quiet in silence" % room, flags[sl].mean() < 0.08,
          "%.0f%%" % (100 * flags[sl].mean()))

    onsets = []
    for i in range(1, n):
        if v[i] and not v[i - 1]:
            for j in range(i, min(n, i + 40)):
                if gates[j] > -3.0:
                    onsets.append((j - i) * 1000.0 / FPS)
                    break
    print("     onset delay: %s ms" % ", ".join("%.0f" % o for o in onsets))
    check("%s: gate opens within 50 ms" % room, bool(onsets) and max(onsets) <= 50.0,
          "max %.0f ms" % (max(onsets) if onsets else -1))

print("\n=== 4. Adapts when the noise level changes ===")
clean2, _, _ = make_scene()
t2 = np.arange(len(clean2)) / SR
loud_noise = rng.normal(0, 0.02, len(clean2)).astype(np.float32)
loud_noise[int(5.5 * SR):] *= 4.0  # +12 dB of new background midway
mixed2 = (clean2 + loud_noise).astype(np.float32)
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
y2, gates2, _ = run(enh, mixed2, trace=True)
v2, s2 = settled_masks(clean2)
n = min(len(gates2), len(v2))
late = np.zeros(n, bool)
late[int(7.5 * FPS):] = True  # two seconds after the change
check("gate still shuts after the noise jumps",
      np.median(gates2[:n][s2[:n] & late]) < -35.0,
      "%.1f dB" % np.median(gates2[:n][s2[:n] & late]))
check("gate still opens after the noise jumps",
      np.median(gates2[:n][v2[:n] & late]) > -2.0,
      "%.1f dB" % np.median(gates2[:n][v2[:n] & late]))

print("\n=== 5. Non-stationary noise (keyboard clicks in the gaps) ===")
clicks = np.zeros(len(clean), np.float32)
for pos in np.arange(0.2, 6.0, 0.31):
    i = int(pos * SR)
    if i + 600 < len(clicks) and not voiced[min(len(voiced) - 1, i // HOP)]:
        clicks[i:i + 600] += (rng.normal(0, 0.35, 600)
                              * np.exp(-np.arange(600) / 120.0)).astype(np.float32)
noisy_clicks = (clean + noise + clicks).astype(np.float32)
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
y3 = run(enh, noisy_clicks)
n = min(len(y3), len(ss))
in_c, out_c = db(noisy_clicks[:n][ss[:n]]), db(y3[:n][ss[:n]])
print("  clicks in gaps: %.1f -> %.1f dB" % (in_c, out_c))
check("transients in the gaps are suppressed >= 25 dB", in_c - out_c >= 25.0,
      "%.1f dB" % (in_c - out_c))

print("\n=== 6. Limiter ===")
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams(
    reduction_db=6, gate_enabled=False, makeup_gain_db=18, highpass_hz=20))
loud = (0.9 * np.sin(2 * np.pi * 300 * np.arange(SR) / SR)).astype(np.float32)
y = run(enh, loud)
check("peaks stay <= 1.0 with +18 dB makeup", np.max(np.abs(y)) <= 1.0,
      "peak %.4f" % np.max(np.abs(y)))
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams(gate_enabled=False, makeup_gain_db=0))
y = run(enh, (0.5 * rng.normal(0, 1, SR)).astype(np.float32).clip(-1, 1))
check("no limiting when it is not needed", np.all(np.isfinite(y)) and np.max(np.abs(y)) <= 1.0)

print("\n=== 7. Ring buffer ===")
r = ring.AudioRing(1000)
r.write(np.arange(600, dtype=np.float32))
check("round-trip", np.array_equal(r.read(600), np.arange(600, dtype=np.float32)))
check("empty after read", r.available == 0)
r.write(np.ones(900, np.float32))
r.write(np.full(300, 2.0, np.float32))
check("overflow counted and capped", r.overflows == 1 and r.available == 1000)
out = r.read(2000)
check("underflow zero-pads", out.size == 2000 and out[-1] == 0.0 and r.underflows == 1)
r = ring.AudioRing(1000)
r.write(np.zeros(800, np.float32))
check("trim drops back to target when quiet", r.trim(400, 200) == 600 and r.available == 200)
r = ring.AudioRing(1000)
r.write(np.ones(400, np.float32))
check("trim defers while loud", r.trim(300, 200) == 0)
r = ring.AudioRing(1000)
r.write(np.ones(900, np.float32))
check("trim forces through when far over", r.trim(300, 300) == 600)
r = ring.AudioRing(1000)
r.write(np.ones(500, np.float32))
check("trim ignores a buffer under the watermark", r.trim(800, 200) == 0)
r = ring.AudioRing(1000)
r.prefill(300)
check("prefill seeds silence", r.available == 300)
r.read(280)
check("pad tops back up to target", r.pad(low_water=100, target=300) == 280
      and r.available == 300)
check("pad does nothing above the low water mark", r.pad(low_water=100, target=300) == 0)

print("\n=== 8. Resampler ===")
for src, dst in [(44100, 48000), (48000, 44100), (48000, 48000), (16000, 48000), (48000, 32000)]:
    rs = resample.LinearResampler(src, dst)
    ti = to = 0
    for _ in range(300):
        block = np.zeros(max(1, src // 100), np.float32)
        ti += block.size
        to += rs.process(block).size
    check("%d->%d keeps the rate" % (src, dst),
          abs(to / float(ti) - dst / float(src)) < 0.005,
          "%.4f vs %.4f" % (to / float(ti), dst / float(src)))
rs = resample.LinearResampler(48000, 24000)
tone = np.sin(2 * np.pi * 400 * np.arange(4800) / 48000.0).astype(np.float32)
out = np.concatenate([rs.process(tone[i:i + 480]) for i in range(0, 4800, 480)])
check("resampled tone stays bounded", np.max(np.abs(out)) < 1.05 and np.all(np.isfinite(out)))

print("\n=== 9. Timing ===")
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
blocks = [mixed[i:i + HOP] for i in range(0, len(mixed) - HOP + 1, HOP)]
start = time.perf_counter()
for _ in range(4):
    for b in blocks:
        enh.process(b)
elapsed = time.perf_counter() - start
rtf = 4 * len(blocks) * HOP / SR / elapsed
print("  %.3f ms per 10 ms hop (%.0fx real time)" % (elapsed / (4 * len(blocks)) * 1000, rtf))
check("comfortably faster than real time", rtf > 20, "%.0fx" % rtf)

print("\n=== 10. Robustness ===")
enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
ok = True
for i in range(0, len(mixed) - HOP + 1, HOP):
    if (i // HOP) % 20 == 0:
        enh.set_params(dsp.EnhancerParams(reduction_db=6 + (i // HOP) % 35,
                                          gate_floor_db=-20 - (i // HOP) % 50,
                                          highpass_hz=(i // HOP) % 150))
    o = enh.process(mixed[i:i + HOP])
    ok = ok and np.all(np.isfinite(o)) and np.max(np.abs(o)) <= 1.0
check("live parameter changes stay stable", ok)

enh.reset()
check("reset works", np.all(np.isfinite(enh.process(mixed[:HOP]))))

enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
edge = [np.zeros(HOP, np.float32), np.ones(HOP, np.float32), -np.ones(HOP, np.float32),
        np.full(HOP, 1e-9, np.float32), rng.normal(0, 5, HOP).astype(np.float32)]
ok = True
for blk in edge * 6:
    o = enh.process(blk)
    ok = ok and np.all(np.isfinite(o)) and np.max(np.abs(o)) <= 1.0
check("survives silence, DC, full-scale and overdriven input", ok)

enh = dsp.SpeechEnhancer(SR, HOP, dsp.EnhancerParams())
long_silence = np.zeros(HOP, np.float32)
ok = True
for _ in range(int(30 * FPS)):  # 30 s of digital silence
    o = enh.process(long_silence)
    ok = ok and np.all(np.isfinite(o))
o = enh.process(synth_speech(0.01)[:HOP])
check("30 s of digital silence then speech", ok and np.all(np.isfinite(o)))

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
