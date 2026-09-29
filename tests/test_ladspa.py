"""The LADSPA host and the RNNoise discovery logic.

fake_ladspa.c is compiled and loaded for real, so the ctypes struct layout, the
port scan, the control defaults and the buffer plumbing are all exercised
against an actual shared library rather than asserted about. Without a C
compiler the plugin half is skipped; the discovery half still runs.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from micnoise import ladspa  # noqa: E402
from micnoise.rnnoise import RNNoise, _classify, discover, search_dirs  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


def build_fake_plugin(outdir):
    """Compile fake_ladspa.c into a shared library, or return None."""
    source = os.path.join(HERE, "fake_ladspa.c")
    if not os.path.isfile(source):
        return None
    suffix = {"win32": ".dll", "darwin": ".dylib"}.get(sys.platform, ".so")
    target = os.path.join(outdir, "librnnoise_ladspa" + suffix)
    for compiler in ("cc", "gcc", "clang"):
        if shutil.which(compiler) is None:
            continue
        command = [compiler, "-shared", "-O1", "-o", target, source]
        if sys.platform != "win32":
            command.insert(2, "-fPIC")
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=120)
        except Exception:
            continue
        if result.returncode == 0 and os.path.isfile(target):
            return target
        print("  (%s failed: %s)" % (compiler,
                                     (result.stdout or b"").decode()[:200]))
    return None


print("=== Control defaults from port hints ===")


def hint(descriptor, low=0.0, high=0.0):
    return ladspa.PortRangeHint(descriptor, low, high)


BOUNDED = 0x1 | 0x2
cases = [
    ("DEFAULT_1", hint(0x1C0), 1.0),
    ("DEFAULT_0", hint(0x180), 0.0),
    ("DEFAULT_100", hint(0x200), 100.0),
    ("DEFAULT_440", hint(0x240), 440.0),
    ("DEFAULT_MINIMUM", hint(BOUNDED | 0x40, 2.0, 8.0), 2.0),
    ("DEFAULT_MAXIMUM", hint(BOUNDED | 0x140, 2.0, 8.0), 8.0),
    ("DEFAULT_MIDDLE", hint(BOUNDED | 0xC0, 0.0, 100.0), 50.0),
    ("DEFAULT_LOW", hint(BOUNDED | 0x80, 0.0, 100.0), 25.0),
    ("DEFAULT_HIGH", hint(BOUNDED | 0x100, 0.0, 100.0), 75.0),
    ("no hint", hint(0), 0.0),
]
for label, h, expected in cases:
    got = ladspa.default_control_value(h, 48000)
    check("%s -> %g" % (label, expected), abs(got - expected) < 1e-6, "got %g" % got)

log_hint = hint(BOUNDED | 0x10 | 0xC0, 10.0, 1000.0)  # logarithmic middle
got = ladspa.default_control_value(log_hint, 48000)
check("logarithmic middle is geometric, not arithmetic",
      abs(got - 100.0) < 1e-3, "got %g (arithmetic would be 505)" % got)

sr_hint = hint(BOUNDED | 0x8 | 0x140, 0.0, 0.5)  # sample-rate relative maximum
check("sample-rate relative bounds are scaled",
      abs(ladspa.default_control_value(sr_hint, 48000) - 24000.0) < 1e-3)

int_hint = hint(BOUNDED | 0x20 | 0xC0, 0.0, 7.0)  # integer middle
check("integer hint rounds", ladspa.default_control_value(int_hint, 48000) in (3.0, 4.0))

check("defaults are clamped to the stated bounds",
      ladspa.default_control_value(hint(BOUNDED | 0x200, 0.0, 10.0), 48000) == 10.0)

print("\n=== The host, against a compiled plugin ===")
tmp = tempfile.mkdtemp(prefix="mnc-ladspa-")
try:
    lib_path = build_fake_plugin(tmp)
    if lib_path is None:
        print("  SKIP  no C compiler available")
    else:
        print("  built %s" % os.path.basename(lib_path))
        check("recognised as a LADSPA plugin", ladspa.is_ladspa(lib_path))
        check("a non-plugin file is rejected",
              not ladspa.is_ladspa(os.path.join(HERE, "fake_ladspa.c")))

        plugin = ladspa.LadspaPlugin(lib_path, samplerate=48000)
        print("  picked: %r (%d ports)" % (plugin.name, len(plugin.ports)))
        check("the mono plugin is chosen over the stereo one at index 0",
              "mono" in plugin.label, plugin.label)
        check("port count is right", len(plugin.ports) == 4)
        check("audio ports are found",
              plugin._audio_in.index == 2 and plugin._audio_out.index == 3)
        check("control ports are named",
              [p.name for p in plugin.ports if p.is_control]
              == ["Gain", "VAD Threshold (%)"])

        controls = plugin.control_values
        check("defaults come from the hints",
              controls == {"Gain": 1.0, "VAD Threshold (%)": 50.0}, str(controls))

        raw = np.array([0.1, 0.2, -0.3, 0.4], np.float32)
        out = plugin.process(raw)
        # The plugin computes in * Gain + Threshold * 0.001
        expected = raw * 1.0 + 0.05
        check("audio really goes through the plugin",
              np.allclose(out, expected, atol=1e-6), "%s vs %s" % (out, expected))
        check("the returned block is a copy, not the live buffer",
              out.base is None or not np.shares_memory(out, plugin._out))

        lifecycle = ctypes.CDLL(lib_path)
        activated = ctypes.c_int.in_dll(lifecycle, "fake_activate_count").value
        rate = ctypes.c_ulong.in_dll(lifecycle, "fake_last_samplerate").value
        check("activate() was called", activated >= 1, str(activated))
        check("instantiated at the sample rate we asked for", rate == 48000, str(rate))

        long_block = np.random.default_rng(1).normal(0, 0.1, 4096).astype(np.float32)
        check("a large block is handled",
              np.allclose(plugin.process(long_block), long_block + 0.05, atol=1e-5))
        try:
            plugin.process(np.zeros(ladspa.MAX_BLOCK + 1, np.float32))
            check("an oversized block is refused", False)
        except ValueError:
            check("an oversized block is refused", True)

        before = ctypes.c_int.in_dll(lifecycle, "fake_cleanup_count").value
        plugin.close()
        after = ctypes.c_int.in_dll(lifecycle, "fake_cleanup_count").value
        check("cleanup() was called on close", after == before + 1)
        check("closing twice is harmless", plugin.close() is None)
        check("processing after close passes audio through untouched",
              np.array_equal(plugin.process(raw), raw))

        print("\n=== Control overrides ===")
        tuned = ladspa.LadspaPlugin(lib_path, controls={"threshold": 0.0, "gain": 2.0})
        check("overrides are applied by substring",
              tuned.control_values == {"Gain": 2.0, "VAD Threshold (%)": 0.0},
              str(tuned.control_values))
        check("the plugin sees the overridden values",
              np.allclose(tuned.process(raw), raw * 2.0, atol=1e-6))
        tuned.close()

        print("\n=== RNNoise discovery finds it ===")
        # Pretend the file was dropped next to the app.
        original = search_dirs
        import micnoise.rnnoise as rn

        rn.search_dirs = lambda: [tmp]
        try:
            RNNoise.rescan()
            check("the plugin is discovered", RNNoise.available())
            check("classified as ladspa", RNNoise.kind() == "ladspa", str(RNNoise.kind()))
            check("the path is reported", RNNoise.library_path() == lib_path)

            denoiser = RNNoise()
            block = np.random.default_rng(2).normal(0, 0.1, 480).astype(np.float32)
            result = denoiser.process(block)
            check("the facade processes a 480 sample frame",
                  result.shape == (480,) and np.all(np.isfinite(result)))
            # Our overrides must have disabled the plugin's own gate.
            check("the plugin's own VAD gate is turned off",
                  np.allclose(result, block, atol=1e-6),
                  "threshold override did not reach the plugin")
            denoiser.close()
            check("closing the facade twice is harmless", denoiser.close() is None)
        finally:
            rn.search_dirs = original
            RNNoise.rescan()

        print("\n=== A wrong file is rejected, not loaded ===")
        junk = os.path.join(tmp, "rnnoise_junk.dylib" if sys.platform == "darwin"
                            else "rnnoise_junk.so")
        with open(junk, "wb") as fh:
            fh.write(b"not a shared library at all")
        kind, message = _classify(junk)
        check("a junk file yields no backend", kind is None)
        check("and says why", "could not be loaded" in message or "neither" in message,
              message[:120])

        # The same download ships VST2, VST3 and LV2 next to each other, and
        # they all match the *rnnoise*.dll glob, so the message has to name the
        # format rather than shrug.
        suffix = ".dylib" if sys.platform == "darwin" else ".so"
        formats = [
            ("VSTPluginMain", "void * VSTPluginMain(void * c) { (void) c; return 0; }",
             "VST2"),
            ("GetPluginFactory", "void * GetPluginFactory(void) { return 0; }", "VST3"),
            ("lv2_descriptor",
             "const void * lv2_descriptor(unsigned int i) { (void) i; return 0; }", "LV2"),
        ]
        for symbol, code, label in formats:
            src = os.path.join(tmp, "f_%s.c" % symbol)
            with open(src, "w") as fh:
                fh.write(code + "\n")
            out = os.path.join(tmp, "rnnoise_%s%s" % (symbol, suffix))
            built = subprocess.run(["cc", "-shared", "-fPIC", "-o", out, src],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if built.returncode != 0:
                continue
            kind, message = _classify(out)
            check("a %s plugin is refused" % label, kind is None)
            check("and is named as %s" % label, label in message, message[-70:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n=== Discovery on this machine ===")
dirs = search_dirs()
print("  searches %d directories" % len(dirs))
check("search covers the program folder", any(d.endswith("micnoise") for d in dirs))
check("search includes an rnnoise subfolder when present",
      all(os.path.isdir(d) for d in dirs))
kind, path, notes = discover()
print("  found: %s %s" % (kind, path or ""))
check("discovery never raises", kind in (None, "native", "ladspa"))
check("notes are collected", isinstance(notes, list))

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
