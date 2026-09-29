"""Startup and shutdown of the application controller.

The tray and the audio engine are stubbed - the point here is the wiring:
that the Tk main loop starts, that work posted from another thread reaches it,
that the first-run guidance fires, and that quitting tears everything down in
the right order and actually returns.
"""
import os
import sys
import tempfile
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mnc-app-")

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


# --------------------------------------------------------------- pystray stub
class _Item(object):
    SEPARATOR = "---"

    def __init__(self, text, action, checked=None, radio=False, default=False, enabled=True):
        self.text, self.action, self.checked = text, action, checked
        self.radio, self.default, self.enabled = radio, default, enabled


class _Menu(object):
    SEPARATOR = "---"

    def __init__(self, *items):
        self.items = list(items)


class _Icon(object):
    def __init__(self, name, icon=None, title=None, menu=None):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.notifications = []
        self.running = threading.Event()
        self._stop = threading.Event()

    def run(self):
        self.running.set()
        self._stop.wait(20)

    def stop(self):
        self._stop.set()

    def notify(self, msg, title=None):
        self.notifications.append((title, msg))

    def update_menu(self):
        pass


stub = types.ModuleType("pystray")
stub.Icon, stub.Menu, stub.MenuItem = _Icon, _Menu, _Item
sys.modules["pystray"] = stub

try:
    import tkinter as tk

    _root_probe = tk.Tk()
    _root_probe.destroy()
except Exception as exc:
    print("SKIP  no display available (%s)" % exc)
    sys.exit(0)

from micnoise import app as appmod  # noqa: E402
from micnoise import config, i18n, settings_ui  # noqa: E402

settings_ui._raise = lambda _w: None  # focus_force never resolves headless


class FakeEngine(object):
    """Stands in for AudioEngine: no devices are opened."""

    instances = []

    def __init__(self, cfg, on_state_change=None):
        self.cfg = dict(cfg)
        self.on_state_change = on_state_change
        self.started = 0
        self.stopped = 0
        self.applied = []
        self.error = None
        self.fail_start = False
        self.virtual_cable = True
        self.enhancer = types.SimpleNamespace(
            in_level_db=-30.0, out_level_db=-40.0, speech=False, gate_db=-60.0)
        FakeEngine.instances.append(self)

    def start(self):
        self.started += 1
        if self.fail_start:
            self.error = "microphone could not be opened"
            return False
        return True

    def stop(self):
        self.stopped += 1

    def restart(self):
        self.stop()
        return self.start()

    def apply_config(self, cfg):
        self.cfg = dict(cfg)
        self.applied.append(dict(cfg))

    def status(self):
        return {"running": self.started > 0 and self.stopped == 0, "error": self.error,
                "bypassed": not self.cfg.get("enabled", True), "speech": False,
                "in_level_db": -30.0, "out_level_db": -40.0,
                "virtual_cable": self.virtual_cable, "input_name": "Mic",
                "output_name": "CABLE Input", "output_api": "Windows WASAPI",
                "latency_ms": 50.0, "rnnoise": False}


appmod.AudioEngine = FakeEngine


def build(**engine_state):
    FakeEngine.instances = []
    application = appmod.Application(**engine_state)
    return application


print("=== Startup ===")
app = build(first_run=True)
check("engine constructed", isinstance(app.engine, FakeEngine))
check("config loaded", isinstance(app.cfg, dict) and "reduction_db" in app.cfg)
check("root window is hidden", app.root.state() == "withdrawn")

app.engine.virtual_cable = True
app._startup()
check("engine started", app.engine.started == 1)
check("first run tells the user which device to pick",
      any(m == i18n.t("app.ready", device="CABLE Output") for _t, m in app.tray.icon.notifications),
      str(app.tray.icon.notifications))

print("\n=== Startup with no virtual cable ===")
app2 = build(first_run=True)
app2.engine.virtual_cable = False
app2._startup()
check("warns that no cable was found",
      any(m == i18n.t("app.no_cable") for _t, m in app2.tray.icon.notifications),
      str(app2.tray.icon.notifications))
check("opens settings so it can be fixed", app2.settings.win is not None)
app2.settings.close()
app2.root.destroy()

print("\n=== Startup when audio fails ===")
app3 = build()
app3.engine.fail_start = True
app3._startup()
check("failure is surfaced to the user",
      any(m == "microphone could not be opened" and
          t == i18n.t("app.start_failed_title", app="Mic Noise Cancel")
          for t, m in app3.tray.icon.notifications),
      str(app3.tray.icon.notifications))
check("settings window is opened on failure", app3.settings.win is not None)
app3.settings.close()
app3.root.destroy()

print("\n=== Actions ===")
app.toggle_enabled()
check("toggle disables processing", app.cfg["enabled"] is False)
check("engine was told", app.engine.cfg["enabled"] is False)
check("change was persisted", config.load()["enabled"] is False)
app.toggle_enabled()
check("toggle restores processing", app.cfg["enabled"] is True)

app.set_preset("balanced")
check("preset applied", config.detect_preset(app.cfg) == "balanced",
      config.detect_preset(app.cfg))
app.set_device("input_device", "Some Mic")
check("device stored", app.cfg["input_device"] == "Some Mic")
app.toggle_monitor()
check("monitor toggled", app.cfg["monitor_enabled"] is True)

before = app.engine.stopped
app.restart_audio()
check("restart cycles the engine", app.engine.stopped > before)

print("\n=== Cross-thread posting ===")
seen = []
done = threading.Event()


def worker():
    app.post(lambda: seen.append(threading.current_thread().name))
    app.post(lambda: done.set())


threading.Thread(target=worker, name="not-main").start()
deadline = 0
while not done.is_set() and deadline < 200:
    app._drain()
    app.root.update_idletasks()
    deadline += 1
check("work posted from another thread runs on the main thread",
      seen and seen[0] == "MainThread", str(seen))

boom = []
import logging
logging.getLogger("micnoise.app").setLevel(logging.CRITICAL)  # the traceback is the point
app.post(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
app.post(lambda: boom.append("survived"))
app._drain()
logging.getLogger("micnoise.app").setLevel(logging.INFO)
check("a failing task does not stop the queue", boom == ["survived"])

print("\n=== Full run and quit ===")
# Tk objects belong to the thread that created them, so the loop has to run
# here on the main thread - exactly as the real app does.
app4 = build()
app4.root.after(400, app4.quit)
app4.run()
check("run() returns after quit", True)
check("tray thread was started", app4._tray_thread is not None)
check("engine was stopped on the way out", app4.engine.stopped >= 1)
check("tray was stopped", app4.tray.icon._stop.is_set())
check("quit is idempotent", app4.quit() is None)

print("\n=== Language ===")
langapp = build()
check("the language follows the system until one is chosen",
      langapp.cfg.get("language") is None and i18n.get_language() == i18n.detect())
langapp.open_settings()
langapp.root.update_idletasks()
start = "sv" if i18n.get_language() == "en" else "en"
other = "en" if start == "sv" else "sv"
langapp.set_language(start)
langapp.root.update_idletasks()
check("choosing a language is stored in the config", langapp.cfg["language"] == start)
check("and persisted to disk", config.load()["language"] == start)
check("and switches the UI", i18n.get_language() == start)
check("the tray menu is rebuilt", langapp.tray._signature_cache[-1] == start,
      str(langapp.tray._signature_cache))
before = langapp.settings.win
langapp.set_language(other)
for _ in range(20):  # the rebuild is scheduled with after_idle
    langapp.root.update_idletasks()
    if langapp.settings.win is not before:
        break
check("an open settings window is rebuilt in the new language",
      langapp.settings.win is not None and langapp.settings.win is not before)
check("with a translated title",
      langapp.settings.win.title() == i18n.t("settings.title", app="Mic Noise Cancel"),
      langapp.settings.win.title())
check("an unknown language falls back to the system one",
      langapp.set_language("xx") is None and i18n.get_language() == i18n.detect())
restarted = appmod.Application()
check("the choice survives a restart", restarted.cfg["language"] == i18n.get_language())
langapp.settings.close()
langapp.root.destroy()
restarted.root.destroy()

print("\n=== Boot helpers ===")
check("dpi awareness is a no-op off Windows", appmod.enable_dpi_awareness() is None)
check("single instance available", appmod.acquire_single_instance() is not None)
check("logging goes to the config dir",
      os.path.dirname(appmod.setup_logging()) == config.config_dir())

app.root.destroy()

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
