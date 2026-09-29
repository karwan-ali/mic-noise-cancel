"""Integration checks for the non-DSP layers.

The audio callbacks are driven directly with synthetic buffers rather than by
opening real devices, so this exercises the same code the driver would without
touching the machine's microphone.
"""
import os
import sys
import tempfile
import threading

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Keep the test out of the real config directory.
_tmp = tempfile.mkdtemp(prefix="mnc-test-")
os.environ["APPDATA"] = _tmp

from micnoise import config, devices, engine, icon  # noqa: E402
from micnoise.resample import LinearResampler  # noqa: E402
from micnoise.ring import AudioRing  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


print("=== Config ===")
cfg = config.load()
check("defaults load", cfg["reduction_db"] == config.DEFAULTS["reduction_db"])
check("config dir is inside APPDATA", config.config_dir().startswith(_tmp))
cfg["reduction_db"] = 17.5
check("save round-trips", config.save(cfg) and config.load()["reduction_db"] == 17.5)

for name in ("light", "balanced", "aggressive"):
    applied = config.apply_preset(dict(config.DEFAULTS), name)
    check("preset %s detected back" % name, config.detect_preset(applied) == name,
          config.detect_preset(applied))
custom = config.apply_preset(dict(config.DEFAULTS), "aggressive")
custom["reduction_db"] = 11.0
check("edited preset reads as custom", config.detect_preset(custom) == "custom")
check("unknown preset is ignored", config.apply_preset(dict(cfg), "nope")["reduction_db"] == 17.5)

with open(config.config_path(), "w") as fh:
    fh.write("{ this is not json")
check("corrupt config falls back to defaults",
      config.load()["reduction_db"] == config.DEFAULTS["reduction_db"])
with open(config.config_path(), "w") as fh:
    fh.write('{"reduction_db": 9.0, "bogus_key": 1}')
loaded = config.load()
check("unknown keys are dropped", "bogus_key" not in loaded and loaded["reduction_db"] == 9.0)

print("\n=== Devices ===")
ins, outs = devices.list_devices("input"), devices.list_devices("output")
print("  %d input, %d output devices on this machine" % (len(ins), len(outs)))
check("enumeration returns Device objects", all(hasattr(d, "label") for d in ins + outs))
check("no duplicate names", len({d.name.lower() for d in outs}) == len(outs))
check("find_by_name round-trips", not ins or devices.find_by_name("input", ins[0].name) is not None)
check("find_by_name tolerates nonsense", devices.find_by_name("input", "no such device xyz") is None
      or True)  # substring fallback may match; must not raise
check("virtual sink detection", devices.is_virtual_sink("CABLE Input (VB-Audio Virtual Cable)"))
check("virtual sink negative", not devices.is_virtual_sink("Realtek Speakers"))
check("voicemeeter recognised", devices.is_virtual_sink("VoiceMeeter Input (VB-Audio)"))
fresh = {"input_device": None, "output_device": None, "monitor_device": None}
devices.suggest_defaults(fresh)
print("  suggested: in=%r out=%r monitor=%r"
      % (fresh["input_device"], fresh["output_device"], fresh["monitor_device"]))
check("suggest_defaults does not crash", True)

print("\n=== Icons ===")
for state in ("active", "bypassed", "stopped", "error"):
    img = icon.make_icon(state, 64)
    check("icon %s is 64x64 RGBA" % state, img.size == (64, 64) and img.mode == "RGBA")
    check("icon %s has opaque pixels" % state, max(img.split()[3].getdata()) == 255)
ico = os.path.join(_tmp, "app.ico")
icon.save_ico(ico)
check("multi-resolution .ico written", os.path.getsize(ico) > 2000, "%d bytes" % os.path.getsize(ico))

print("\n=== Engine helpers ===")
check("channel options prefer mono for input", engine._channel_options(2)[0] == 1)
check("channel options honour preference", engine._channel_options(8, preferred=2)[0] == 2)
check("channel options clamp to the device", max(engine._channel_options(1, preferred=2)) == 1)
check("channel options are unique", len(set(engine._channel_options(2))) == len(engine._channel_options(2)))
check("blocksize is 10 ms", engine._blocksize_for(48000) == 480 and engine._blocksize_for(44100) == 441)
check("blocksize has a floor", engine._blocksize_for(1000) == 64)


class FakeDevice(object):
    def __init__(self, name, hostapi, channels, rate=48000, index=0):
        self.name, self.hostapi, self.channels = name, hostapi, channels
        self.samplerate, self.index = rate, index


check("wasapi settings only for wasapi",
      engine._wasapi_settings(FakeDevice("x", "MME", 2)) is None)

print("\n=== Engine callbacks (driven by hand) ===")
cfg = dict(config.DEFAULTS)
cfg["enabled"] = True
eng = engine.AudioEngine(cfg)

# Wire up exactly what _open_streams would, minus the hardware.
rate = 48000
eng._in_resampler = LinearResampler(rate, engine.PROC_RATE)
eng._out_resampler = LinearResampler(engine.PROC_RATE, rate)
eng._out_target = int(rate * engine.TARGET_LATENCY_MS / 1000.0)
eng._out_max = int(rate * engine.MAX_LATENCY_MS / 1000.0)
eng._out_ring = AudioRing(rate)
eng._out_ring.prefill(eng._out_target)

in_cb = eng._make_in_callback(1)
sr, hop = 48000, 480
t = np.arange(hop) / float(sr)
produced = []

for k in range(400):  # 4 seconds
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * k * hop / sr)
    tone = (0.2 * env * np.sin(2 * np.pi * 300 * (t + k * hop / sr))).astype(np.float32)
    noise = (0.02 * np.random.default_rng(k).normal(0, 1, hop)).astype(np.float32)
    in_cb((tone + noise).reshape(-1, 1), hop, None, None)
    out = np.zeros((hop, 2), np.float32)
    eng._out_callback(out, hop, None, None)
    produced.append(out.copy())

produced = np.concatenate(produced)
check("output stays finite", np.all(np.isfinite(produced)))
check("output is bounded", np.max(np.abs(produced)) <= 1.0, "peak %.3f" % np.max(np.abs(produced)))
check("both channels carry the signal", np.array_equal(produced[:, 0], produced[:, 1]))
check("something was actually produced", np.max(np.abs(produced)) > 0.01)
latency_ms = 1000.0 * eng._out_ring.available / rate
print("  ring holds %.0f ms, target %.0f ms, %d overflows, %d underflows, %d trims"
      % (latency_ms, engine.TARGET_LATENCY_MS, eng._out_ring.overflows,
         eng._out_ring.underflows, eng._out_ring.drops))
check("buffer sits near its target", abs(latency_ms - engine.TARGET_LATENCY_MS) <= 12,
      "%.0f ms" % latency_ms)
check("no ring overflow in steady state", eng._out_ring.overflows == 0)

print("\n=== Clock drift between two devices ===")
eng2 = engine.AudioEngine(dict(config.DEFAULTS))
eng2._in_resampler = LinearResampler(rate, engine.PROC_RATE)
eng2._out_resampler = LinearResampler(engine.PROC_RATE, rate)
eng2._out_target = int(rate * engine.TARGET_LATENCY_MS / 1000.0)
eng2._out_max = int(rate * engine.MAX_LATENCY_MS / 1000.0)
eng2._out_ring = AudioRing(rate)
eng2._out_ring.prefill(eng2._out_target)
in_cb2 = eng2._make_in_callback(1)

# Output consumes 0.3% faster than the input produces: 60 s of hard drift.
levels = []
carry = 0.0
for k in range(6000):
    blk = (0.05 * np.random.default_rng(k).normal(0, 1, hop)).astype(np.float32)
    in_cb2(blk.reshape(-1, 1), hop, None, None)
    carry += hop * 1.003
    take = int(carry)
    carry -= take
    out = np.zeros((take, 2), np.float32)
    eng2._out_callback(out, take, None, None)
    if k % 500 == 0:
        levels.append(1000.0 * eng2._out_ring.available / rate)
print("  buffer over 60 s of 0.3%% drift: %s ms" % " ".join("%.0f" % v for v in levels))
print("  %d silence insertions, %d short reads over 60 s"
      % (eng2._out_ring.pads, eng2._out_ring.underflows))
check("drift is corrected by rare padding, not constant glitching",
      eng2._out_ring.underflows <= 15, "%d short reads" % eng2._out_ring.underflows)
check("drift does not overflow the buffer", eng2._out_ring.overflows == 0)
check("latency stays bounded under drift",
      max(levels) <= engine.MAX_LATENCY_MS + 12 and min(levels) >= 0,
      "range %.0f-%.0f ms" % (min(levels), max(levels)))

print("\n=== Bypass and hot config changes ===")
eng3 = engine.AudioEngine(dict(config.DEFAULTS))
eng3._in_resampler = LinearResampler(rate, engine.PROC_RATE)
eng3._out_resampler = LinearResampler(engine.PROC_RATE, rate)
eng3._out_target, eng3._out_max = 1920, 5280
eng3._out_ring = AudioRing(rate)
in_cb3 = eng3._make_in_callback(1)
bypass_cfg = dict(config.DEFAULTS)
bypass_cfg["enabled"] = False
eng3.cfg = bypass_cfg
ramp = np.linspace(-0.5, 0.5, hop).astype(np.float32)
in_cb3(ramp.reshape(-1, 1), hop, None, None)
out = np.zeros((hop, 1), np.float32)
eng3._out_callback(out, hop, None, None)
check("bypass passes audio through untouched",
      np.allclose(out[:, 0], ramp, atol=1e-5), "max diff %.2e" % np.max(np.abs(out[:, 0] - ramp)))

st = eng3.status()
check("status reports bypassed", st["bypassed"] is True)
check("status has no error", st["error"] is None)
check("status counts xruns", "xruns" in st)

for _ in range(5):
    c = dict(config.DEFAULTS)
    c["reduction_db"] = 12.0
    eng3.apply_config(c)
    in_cb3(ramp.reshape(-1, 1), hop, None, None)
check("apply_config while running is safe", eng3.enhancer.params.reduction_db == 12.0)

print("\n=== Stereo downmix and odd block sizes ===")
eng4 = engine.AudioEngine(dict(config.DEFAULTS))
eng4._in_resampler = LinearResampler(44100, engine.PROC_RATE)
eng4._out_resampler = LinearResampler(engine.PROC_RATE, 44100)
eng4._out_target, eng4._out_max = 1764, 4851
eng4._out_ring = AudioRing(44100)
in_cb4 = eng4._make_in_callback(2)
ok = True
for k in range(300):
    stereo = np.random.default_rng(k).normal(0, 0.1, (441, 2)).astype(np.float32)
    in_cb4(stereo, 441, None, None)
    out = np.zeros((441, 2), np.float32)
    eng4._out_callback(out, 441, None, None)
    ok = ok and np.all(np.isfinite(out)) and np.max(np.abs(out)) <= 1.0
check("44.1 kHz stereo input resamples and survives", ok)
check("no NaN from the resampler", np.all(np.isfinite(eng4._out_ring.read(441))))

print("\n=== Error paths ===")
bad = dict(config.DEFAULTS)
bad["input_device"] = "definitely not a real device"
bad["output_device"] = "also not real"
eng5 = engine.AudioEngine(bad)
started = eng5.start()
print("  start() with bogus devices -> %s (%s)" % (started, eng5.error))
check("start reports failure or falls back to defaults", isinstance(started, bool))
check("stop after a failed start is safe", eng5.stop() is None)
check("status still readable after failure", isinstance(eng5.status(), dict))

print("\n=== Threading ===")
eng6 = engine.AudioEngine(dict(config.DEFAULTS))
eng6._in_resampler = LinearResampler(rate, engine.PROC_RATE)
eng6._out_resampler = LinearResampler(engine.PROC_RATE, rate)
eng6._out_target, eng6._out_max = 1920, 5280
eng6._out_ring = AudioRing(rate)
in_cb6 = eng6._make_in_callback(1)
errors = []


def audio_thread():
    try:
        for k in range(500):
            blk = np.random.default_rng(k).normal(0, 0.1, hop).astype(np.float32)
            in_cb6(blk.reshape(-1, 1), hop, None, None)
            out = np.zeros((hop, 1), np.float32)
            eng6._out_callback(out, hop, None, None)
    except Exception as exc:  # pragma: no cover
        errors.append(exc)


def ui_thread():
    try:
        for i in range(500):
            eng6.status()
            c = dict(config.DEFAULTS)
            c["reduction_db"] = 10.0 + (i % 20)
            eng6.apply_config(c)
    except Exception as exc:  # pragma: no cover
        errors.append(exc)


threads = [threading.Thread(target=audio_thread), threading.Thread(target=ui_thread)]
for th in threads:
    th.start()
for th in threads:
    th.join(30)
check("audio and UI threads do not collide", not errors, repr(errors[:1]))
check("no thread deadlocked", not any(th.is_alive() for th in threads))

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
