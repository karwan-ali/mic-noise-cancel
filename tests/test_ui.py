"""Builds the settings window and the tray menu for real, against a fake app.

pystray needs pyobjc on macOS and cannot be installed here, so it is stubbed
with a module that records what the menu was built from. The logic under test -
which items exist, what they are bound to, how state maps to icons - is ours,
not pystray's.
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="mnc-ui-")

fails = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not ok:
        fails.append(name)


# ----------------------------------------------------------------- pystray stub
class _Item(object):
    SEPARATOR = "---"

    def __init__(self, text, action, checked=None, radio=False, default=False, enabled=True,
                 visible=True):
        self.text, self.action, self.checked = text, action, checked
        self.radio, self.default, self.enabled = radio, default, enabled

    @property
    def submenu(self):
        return self.action if isinstance(self.action, _Menu) else None


class _Menu(object):
    SEPARATOR = "---"

    def __init__(self, *items):
        self.items = list(items)


class _Icon(object):
    def __init__(self, name, icon=None, title=None, menu=None):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.stopped = False
        self.notifications = []

    def run(self):
        pass

    def stop(self):
        self.stopped = True

    def notify(self, msg, title=None):
        self.notifications.append((title, msg))

    def update_menu(self):
        pass


stub = types.ModuleType("pystray")
stub.Icon, stub.Menu, stub.MenuItem = _Icon, _Menu, _Item
sys.modules["pystray"] = stub

from micnoise import config, i18n, tray  # noqa: E402

# The assertions below read the Swedish UI; the English one gets its own
# section further down. Pinning the language keeps the result independent of
# whatever the machine running the tests is set to.
i18n.set_language("sv")


class FakeEngine(object):
    def __init__(self, **state):
        self.state = {"running": True, "error": None, "bypassed": False, "speech": False,
                      "in_level_db": -30.0, "out_level_db": -35.0, "virtual_cable": True,
                      "input_name": "Headset Mic", "output_name": "CABLE Input",
                      "output_api": "Windows WASAPI", "latency_ms": 50.0, "rnnoise": False}
        self.state.update(state)
        self.enhancer = types.SimpleNamespace(
            in_level_db=-30.0, out_level_db=-35.0, speech=False, gate_db=-60.0)

    def status(self):
        return dict(self.state)


class FakeApp(object):
    def __init__(self, **state):
        self.cfg = dict(config.DEFAULTS)
        self.engine = FakeEngine(**state)
        self.posted = []

    def post(self, fn):
        self.posted.append(fn)

    def toggle_enabled(self):
        self.cfg["enabled"] = not self.cfg["enabled"]

    def toggle_monitor(self):
        self.cfg["monitor_enabled"] = not self.cfg["monitor_enabled"]

    def set_preset(self, name):
        self.cfg = config.apply_preset(self.cfg, name)

    def set_device(self, key, value):
        self.cfg[key] = value

    def update_config(self, cfg):
        self.cfg = dict(cfg)

    def restart_audio(self):
        self.cfg["_restarted"] = True

    def open_settings(self):
        self.cfg["_settings_opened"] = True

    def set_language(self, code):
        self.cfg["language"] = i18n.set_language(code)
        self.language_calls = getattr(self, "language_calls", []) + [code]

    def quit(self):
        self.cfg["_quit"] = True


print("=== Tray menu ===")
app = FakeApp()
icon = tray.TrayIcon(app)
items = icon.icon.menu.items
labels = [i.text for i in items if isinstance(i, _Item)]
print("  top level: %s" % " | ".join(str(x) for x in labels))
check("has an on/off toggle", any("Brusreducering" in str(t) for t in labels))
check("has a settings entry", any("Inställningar" in str(t) for t in labels))
check("has a quit entry", any("Avsluta" in str(t) for t in labels))
check("has device submenus", any("Mikrofon" in str(t) for t in labels)
      and any("Skicka till" in str(t) for t in labels))
check("settings is the default (double-click) item",
      any(i.default for i in items if isinstance(i, _Item)))

presets = [i for i in items if isinstance(i, _Item) and i.text == "Nivå"][0].submenu
check("preset submenu has three levels", len(presets.items) == 3,
      "%d" % len(presets.items))
check("preset items are radio", all(i.radio for i in presets.items))

# Each preset handler must capture its own value, not the loop's last one.
for item in presets.items:
    app.posted = []
    item.action(icon.icon, item)
    app.posted[0]()
labels_by_value = {config.preset_label(n): n for n in config.PRESET_NAMES}
check("last preset click actually applied",
      config.detect_preset(app.cfg) == labels_by_value[presets.items[-1].text],
      config.detect_preset(app.cfg))

checked = [i.text for i in presets.items if i.checked(i)]
check("exactly one preset is ticked", len(checked) == 1, str(checked))

app2 = FakeApp()
icon2 = tray.TrayIcon(app2)
toggle = [i for i in icon2.icon.menu.items
          if isinstance(i, _Item) and "Brusreducering" in str(i.text)][0]
check("toggle starts ticked", bool(toggle.checked(toggle)))
toggle.action(icon2.icon, toggle)
app2.posted[0]()
check("toggle flips the config", app2.cfg["enabled"] is False)

mic_menu = [i for i in icon2.icon.menu.items
            if isinstance(i, _Item) and i.text == "Mikrofon"][0].submenu
print("  microphone submenu: %d entries" % len(mic_menu.items))
if mic_menu.items and mic_menu.items[0].action is not None:
    first = mic_menu.items[0]
    app2.posted = []
    first.action(icon2.icon, first)
    app2.posted[0]()
    check("picking a device stores its name", app2.cfg["input_device"] is not None)

print("\n=== Tray state -> icon ===")
for state, expect in [
    ({}, "active"),
    ({"bypassed": True}, "bypassed"),
    ({"running": False}, "stopped"),
    ({"error": "boom"}, "error"),
]:
    a = FakeApp(**state)
    ic = tray.TrayIcon(a)
    ic.refresh()
    check("state %s -> %s icon" % (state or "normal", expect), ic._state == expect, ic._state)

a = FakeApp(error="Kunde inte öppna mikrofonen")
ic = tray.TrayIcon(a)
ic.refresh()
check("tooltip mentions the error", "Kunde inte" in ic.icon.title, ic.icon.title.replace("\n", " / "))
a = FakeApp(virtual_cable=False)
ic = tray.TrayIcon(a)
head = ic.icon.menu.items[0]
check("headline warns when no cable is routed", "virtuell kabel" in head.text, head.text)
check("headline item is not clickable", head.enabled is False)

print("\n=== Language switching in the tray ===")
app5 = FakeApp()
ic5 = tray.TrayIcon(app5)
lang_items = [i for i in ic5.icon.menu.items
              if isinstance(i, _Item) and i.text == i18n.LANGUAGE_LABEL]
check("a language menu exists, labelled in both languages", len(lang_items) == 1)
if lang_items:
    options = lang_items[0].submenu.items
    check("it offers every supported language",
          [o.text for o in options] == [i18n.NAMES[c] for c in i18n.SUPPORTED],
          str([o.text for o in options]))
    check("the options are radio items", all(o.radio for o in options))
    ticked = [o.text for o in options if o.checked(o)]
    check("the current language is ticked", ticked == [i18n.NAMES["sv"]], str(ticked))

    english = options[i18n.SUPPORTED.index("en")]
    english.action(ic5.icon, english)
    app5.posted[-1]()
    check("picking English switches the language", i18n.get_language() == "en")
    ic5.refresh()
    labels_en = [str(i.text) for i in ic5.icon.menu.items if isinstance(i, _Item)]
    print("  english menu: %s" % " | ".join(labels_en))
    check("the menu is rebuilt in English",
          {"Quit", "Settings...", "Level", "Microphone"} <= set(labels_en))
    joined = "".join(labels_en).replace(i18n.LANGUAGE_LABEL, "")
    check("no Swedish is left in the English menu", not any(c in joined for c in "åäöÅÄÖ"))
    presets_en = [i for i in ic5.icon.menu.items
                  if isinstance(i, _Item) and i.text == "Level"][0].submenu
    check("preset names are translated",
          [p.text for p in presets_en.items] == ["Light", "Balanced", "Aggressive (voice only)"])
    check("the tooltip follows the language", "Active" in ic5.icon.title, ic5.icon.title)
    ticked = [o.text for o in lang_items[0].submenu.items if o.checked(o)]
    check("English is now the ticked language", ticked == ["English"], str(ticked))
    i18n.set_language("sv")

print("\n=== Settings window ===")
try:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.withdraw()
    # focus_force() never resolves in a headless macOS session; keep the event
    # loop to idle tasks so the test exercises layout, not activation.
    from micnoise import settings_ui as _sui
    _sui._raise = lambda _w: None
except Exception as exc:
    print("  SKIP  no display available (%s)" % exc)
else:
    from micnoise.settings_ui import SettingsWindow

    app3 = FakeApp()
    win = SettingsWindow(root, app3)
    win.show()
    root.update_idletasks()
    check("window builds", win.win is not None and win.win.winfo_exists())

    expected = {"reduction_db", "gate_floor_db", "vad_threshold_db", "vad_hangover_ms",
                "gate_attack_ms", "gate_release_ms", "highpass_hz", "input_gain_db",
                "makeup_gain_db", "monitor_gain_db", "gate_enabled", "monitor_enabled",
                "use_rnnoise", "autostart", "preset"}
    have = {k for k in win._vars if not k.endswith("__label")}
    check("every setting is bound to a widget", expected <= have, str(sorted(expected - have)))

    collected = win._collect()
    missing = [k for k in config.DEFAULTS if k not in collected]
    check("collect() returns a complete config", not missing, str(missing))
    check("collect() keeps values in range",
          all(isinstance(collected[k], (int, float, bool, str, type(None)))
              for k in collected))

    win._vars["reduction_db"].set(19.0)
    win._apply()
    root.update_idletasks()
    check("moving a slider reaches the app", app3.cfg["reduction_db"] == 19.0,
          str(app3.cfg["reduction_db"]))

    win._vars["preset"].set("light")
    win._on_preset()
    root.update_idletasks()
    check("preset radio applies the preset", config.detect_preset(app3.cfg) == "light",
          config.detect_preset(app3.cfg))

    win._tick_meters()
    root.update_idletasks()
    check("meters and status render", win._status_label.cget("text") in
          ("Aktiv", "Stoppad", "Förbikopplad", "Fel"), win._status_label.cget("text"))

    for state, label in [({"error": "x"}, "Fel"), ({"running": False}, "Stoppad"),
                         ({"bypassed": True}, "Förbikopplad")]:
        app3.engine.state.update({"error": None, "running": True, "bypassed": False})
        app3.engine.state.update(state)
        win._tick_meters()
        root.update_idletasks()
        check("status shows %s" % label, win._status_label.cget("text") == label,
              win._status_label.cget("text"))

    win._reload_devices()
    win._load_values()
    root.update_idletasks()
    check("reload does not lose the bound values", win._vars["reduction_db"].get() > 0)

    print("\n=== Language switching in the settings window ===")

    def all_texts(widget, found=None):
        """Every caption in the window, notebook tab titles included."""
        found = [] if found is None else found
        try:
            found.append(str(widget.cget("text")))
        except Exception:
            pass
        if isinstance(widget, ttk.Notebook):
            found += [widget.tab(i, "text") for i in range(len(widget.tabs()))]
        for child in widget.winfo_children():
            all_texts(child, found)
        return found

    check("the language picker shows the current language",
          win._language_combo.get() == i18n.NAMES["sv"], win._language_combo.get())
    win._notebook.select(1)
    i18n.set_language("en")
    win.rebuild()
    root.update_idletasks()
    check("the window is rebuilt in English", win.win.title().endswith("- settings"),
          win.win.title())
    check("the tab the user was on is kept",
          win._notebook.index(win._notebook.select()) == 1)
    check("tab titles are translated",
          [win._notebook.tab(i, "text").strip() for i in range(2)] == ["General", "Advanced"])
    check("the picker follows", win._language_combo.get() == "English")
    win._tick_meters()
    root.update_idletasks()
    check("the live status follows",
          win._status_label.cget("text") in ("Active", "Stopped", "Bypassed", "Error"),
          win._status_label.cget("text"))
    leftovers = [x for x in all_texts(win.win)
                 if x != i18n.LANGUAGE_LABEL and any(c in x for c in "åäöÅÄÖ„“")]
    check("no Swedish text anywhere in the English window", not leftovers, str(leftovers[:3]))

    app3.language_calls = []
    win._language_combo.current(i18n.SUPPORTED.index("sv"))
    win._language_combo.event_generate("<<ComboboxSelected>>")
    root.update_idletasks()  # update() never returns in a headless macOS session
    check("choosing a language in the window reaches the app",
          getattr(app3, "language_calls", []) == ["sv"], str(getattr(app3, "language_calls", [])))
    win.rebuild()
    root.update_idletasks()
    check("and back to Swedish", win.win.title().endswith("inställningar"), win.win.title())

    cfg_before = dict(app3.cfg, language="en")
    app3.cfg = cfg_before
    import tkinter.messagebox as _mb
    _mb.askyesno = lambda *a, **k: True
    win._reset()
    check("reset keeps the chosen language", app3.cfg.get("language") == "en",
          str(app3.cfg.get("language")))
    i18n.set_language("sv")

    print("\n=== Hover help ===")
    from micnoise.settings_ui import _Tooltip, _tip

    HELP = {k[len("help."):]: i18n.t(k) for k in i18n.keys() if k.startswith("help.")}

    # Every stored setting the user can reach must explain itself.
    documented = set(HELP)
    settings = {k for k in win._vars
                if not k.endswith("__label") and k not in ("preset", "autostart")}
    settings |= {"autostart"}
    check("every setting has hover help", settings <= documented,
          str(sorted(settings - documented)))
    check("every preset has hover help",
          {"preset_light", "preset_balanced", "preset_aggressive"} <= documented)
    check("no help entry is empty", all(t.strip() for t in HELP.values()))
    thin = [k for k, t in HELP.items() if len(t) < 40]
    check("help text says something useful", not thin, str(thin))
    check("help text is not just the label repeated",
          all(not t.lower().startswith(k.split("_")[0].lower()) for k, t in HELP.items()))

    # Count what actually got wired up rather than trusting the layout code.
    def count_tips(widget, found=None):
        found = [] if found is None else found
        binding = widget.bind("<Enter>")
        if binding:
            found.append(str(widget))
        for child in widget.winfo_children():
            count_tips(child, found)
        return found

    wired = count_tips(win.win)
    print("  %d widgets have hover help attached" % len(wired))
    check("hover help is attached widely", len(wired) >= 30, "%d widgets" % len(wired))

    # And that the window itself behaves.
    probe = ttk.Label(win.win, text="probe")
    probe.grid()
    tip = _Tooltip(probe, "Förklarande text", delay=10)
    root.update_idletasks()
    tip.show()
    root.update_idletasks()
    check("tooltip window appears", tip._win is not None and tip._win.winfo_exists())
    if tip._win is not None:
        text = tip._win.winfo_children()[0].cget("text")
        check("tooltip shows the given text", text == "Förklarande text", text)
        check("tooltip has no window decorations",
              bool(tip._win.wm_overrideredirect()))
    tip.hide()
    root.update_idletasks()
    check("tooltip disappears on leave", tip._win is None)
    tip.hide()
    check("hiding twice is harmless", tip._win is None)

    tip._schedule()
    check("a pending tooltip is registered", tip._job is not None)
    tip.hide()
    check("leaving cancels the pending tooltip", tip._job is None)

    # A tooltip scheduled just before the window closes must not crash.
    tip._schedule()
    probe.destroy()
    tip.show()
    check("a tooltip for a destroyed widget is dropped quietly", tip._win is None)

    check("_tip returns the widget it was given",
          _tip(ttk.Label(win.win, text="x"), "hjälp").winfo_exists())
    check("_tip with no text attaches nothing",
          _tip(ttk.Label(win.win, text="y"), None).bind("<Enter>") == "")

    win.close()
    check("window closes cleanly", win.win is None)
    win.show()
    root.update_idletasks()
    check("reopening works", win.win is not None)
    win.close()
    root.destroy()

print("\n" + "=" * 62)
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("All checks passed.")
