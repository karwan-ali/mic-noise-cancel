"""Settings window (Tkinter). Lives on the main thread, like all Tk code."""

import logging
import os
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import APP_TITLE, VERSION, autostart, config, devices, i18n, shortcut
from .rnnoise import RNNoise
from .rnnoise import search_dirs as rnnoise_search_dirs

log = logging.getLogger(__name__)

VB_CABLE_URL = "https://vb-audio.com/Cable/"

METER_FLOOR_DB = -60.0

_COL_BG = "#f5f6f8"
_COL_OK = "#2ec47b"
_COL_WARN = "#d98a1f"
_COL_ERR = "#e04f5f"
_COL_MUTED = "#6b7280"
_COL_TIP_BG = "#2b3038"
_COL_TIP_FG = "#f2f4f7"

class SettingsWindow(object):
    """One reusable Toplevel; opening it again just raises the existing one."""

    def __init__(self, root, app):
        self.root = root
        self.app = app  # provides .cfg, .engine, .update_config(), .restart_audio()
        self.win = None
        self._vars = {}
        self._apply_job = None
        self._meter_job = None
        self._loading = False
        self._in_devices = []
        self._out_devices = []
        self._notebook = None
        self._language_combo = None

    # ------------------------------------------------------------ open/close

    def show(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            _raise(self.win)
            self._reload_devices()
            self._load_values()
            return
        self._build()

    def close(self):
        if self._meter_job is not None:
            try:
                self.root.after_cancel(self._meter_job)
            except Exception:
                pass
            self._meter_job = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None

    # ---------------------------------------------------------------- layout

    def _build(self):
        win = tk.Toplevel(self.root)
        self.win = win
        win.title(i18n.t("settings.title", app=APP_TITLE))
        win.configure(bg=_COL_BG)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style(win)
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=_COL_BG)
        style.configure("TLabelframe", background=_COL_BG)
        style.configure("TLabelframe.Label", background=_COL_BG, font=("Segoe UI", 9, "bold"))
        style.configure("TLabel", background=_COL_BG)
        style.configure("TCheckbutton", background=_COL_BG)
        style.configure("TRadiobutton", background=_COL_BG)
        style.configure("Hint.TLabel", background=_COL_BG, foreground=_COL_MUTED)
        style.configure("Status.TLabel", background=_COL_BG, font=("Segoe UI", 10, "bold"))

        outer = ttk.Frame(win, padding=(14, 12))
        outer.grid(row=0, column=0, sticky="nsew")

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self._status_label = ttk.Label(header, text="", style="Status.TLabel")
        self._status_label.grid(row=0, column=0, sticky="w")
        self._build_language_picker(header).grid(row=0, column=1, rowspan=2, sticky="ne")
        self._status_hint = ttk.Label(outer, text="", style="Hint.TLabel")
        self._status_hint.grid(row=1, column=0, sticky="w", pady=(0, 10))

        notebook = ttk.Notebook(outer)
        notebook.grid(row=2, column=0, sticky="nsew")
        self._notebook = notebook
        general = ttk.Frame(notebook, padding=12)
        advanced = ttk.Frame(notebook, padding=12)
        notebook.add(general, text="  %s  " % i18n.t("settings.tab.general"))
        notebook.add(advanced, text="  %s  " % i18n.t("settings.tab.advanced"))

        self._build_general(general)
        self._build_advanced(advanced)

        buttons = ttk.Frame(outer)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        _tip(ttk.Button(buttons, text=i18n.t("action.restart_audio"), command=self._restart),
             _help("restart_audio")).grid(row=0, column=1, padx=(0, 6))
        _tip(ttk.Button(buttons, text=i18n.t("action.close"), command=self.close),
             _help("close")).grid(row=0, column=2)

        self._reload_devices()
        self._load_values()
        self._tick_meters()

        win.update_idletasks()
        _center(win)
        _raise(win)

    def _build_language_picker(self, parent):
        frame = ttk.Frame(parent)
        _tip(ttk.Label(frame, text=i18n.LANGUAGE_LABEL, style="Hint.TLabel"),
             _help("language")).grid(row=0, column=0, padx=(0, 6))
        codes = list(i18n.SUPPORTED)
        combo = ttk.Combobox(frame, state="readonly", width=9,
                             values=[i18n.NAMES[c] for c in codes])
        combo.set(i18n.NAMES[i18n.get_language()])
        combo.grid(row=0, column=1)
        _tip(combo, _help("language"))

        def picked(_event=None):
            code = codes[combo.current()]
            if code != i18n.get_language():
                self.app.set_language(code)

        combo.bind("<<ComboboxSelected>>", picked)
        self._language_combo = combo
        return frame

    def rebuild(self):
        """Recreate the window in the current language, on the same tab.

        Every label is set once at build time, so a new language means new
        widgets. Called via after_idle, because the window cannot tear itself
        down from inside one of its own widget callbacks.
        """
        if self.win is None or not self.win.winfo_exists():
            return
        try:
            tab = self._notebook.index(self._notebook.select())
        except Exception:
            tab = 0
        self.close()
        self._build()
        try:
            self._notebook.select(tab)
        except Exception:
            pass

    def _build_general(self, parent):
        parent.columnconfigure(0, weight=1)
        row = 0

        box = ttk.Labelframe(parent, text=i18n.t("settings.devices"), padding=10)
        box.grid(row=row, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        row += 1

        _tip(ttk.Label(box, text=i18n.t("settings.microphone")), _help("input_device")).grid(
            row=0, column=0, sticky="w", pady=4)
        self._in_combo = ttk.Combobox(box, state="readonly", width=44)
        self._in_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        self._in_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())
        _tip(self._in_combo, _help("input_device"))

        _tip(ttk.Label(box, text=i18n.t("settings.output")), _help("output_device")).grid(
            row=1, column=0, sticky="w", pady=4)
        self._out_combo = ttk.Combobox(box, state="readonly", width=44)
        self._out_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        self._out_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())
        _tip(self._out_combo, _help("output_device"))

        self._cable_hint = ttk.Label(box, text="", style="Hint.TLabel", wraplength=430, justify="left")
        self._cable_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.Frame(box)
        actions.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        _tip(ttk.Button(actions, text=i18n.t("settings.rescan_devices"), command=self._rescan),
             _help("rescan")).grid(row=0, column=0)
        self._cable_button = ttk.Button(
            actions, text=i18n.t("settings.download_cable"), command=lambda: webbrowser.open(VB_CABLE_URL)
        )
        self._cable_button.grid(row=0, column=1, padx=(6, 0))
        _tip(self._cable_button, _help("cable_download"))

        box = ttk.Labelframe(parent, text=i18n.t("settings.noise_reduction"), padding=10)
        box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        row += 1
        self._vars["preset"] = tk.StringVar(value="aggressive")
        for i, name in enumerate(config.PRESET_NAMES):
            _tip(
                ttk.Radiobutton(
                    box,
                    text=config.preset_label(name),
                    value=name,
                    variable=self._vars["preset"],
                    command=self._on_preset,
                ),
                _help("preset_%s" % name),
            ).grid(row=0, column=i, sticky="w", padx=(0, 14))
        ttk.Label(
            box,
            text=i18n.t("settings.hover_presets"),
            style="Hint.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        box = ttk.Labelframe(parent, text=i18n.t("settings.levels"), padding=10)
        box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        row += 1
        _tip(ttk.Label(box, text=i18n.t("settings.meter_in")), _help("meter_in")).grid(row=0, column=0, sticky="w")
        self._meter_in = _tip(_Meter(box), _help("meter_in"))
        self._meter_in.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        _tip(ttk.Label(box, text=i18n.t("settings.meter_out")), _help("meter_out")).grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self._meter_out = _tip(_Meter(box), _help("meter_out"))
        self._meter_out.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        self._speech_label = ttk.Label(box, text="", style="Hint.TLabel")
        self._speech_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        box = ttk.Labelframe(parent, text=i18n.t("settings.monitor_and_startup"), padding=10)
        box.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        row += 1

        self._vars["monitor_enabled"] = tk.BooleanVar()
        _tip(
            ttk.Checkbutton(
                box,
                text=i18n.t("settings.monitor_enabled"),
                variable=self._vars["monitor_enabled"],
                command=self._on_device_change,
            ),
            _help("monitor_enabled"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        _tip(ttk.Label(box, text=i18n.t("settings.headphones")), _help("monitor_device")).grid(
            row=1, column=0, sticky="w", pady=4)
        self._mon_combo = ttk.Combobox(box, state="readonly", width=40)
        self._mon_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        self._mon_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())
        _tip(self._mon_combo, _help("monitor_device"))

        self._slider(box, 2, "monitor_gain_db", i18n.t("settings.monitor_volume"), -40, 6, "dB")

        self._vars["autostart"] = tk.BooleanVar()
        self._autostart_check = ttk.Checkbutton(
            box,
            text=i18n.t("settings.autostart"),
            variable=self._vars["autostart"],
            command=self._on_autostart,
        )
        self._autostart_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        _tip(self._autostart_check, _help("autostart"))
        if not autostart.supported():
            self._autostart_check.state(["disabled"])

        self._shortcut_button = ttk.Button(
            box, text=i18n.t("settings.shortcut_create"), command=self._make_shortcut
        )
        self._shortcut_button.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        _tip(self._shortcut_button, _help("shortcut"))
        if not shortcut.supported():
            self._shortcut_button.state(["disabled"])

    def _build_advanced(self, parent):
        parent.columnconfigure(0, weight=1)

        box = ttk.Labelframe(parent, text=i18n.t("settings.noise_reduction"), padding=10)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self._slider(box, 0, "reduction_db", i18n.t("settings.reduction"), 6, 45, "dB")
        self._slider(box, 1, "highpass_hz", i18n.t("settings.highpass"), 20, 200, "Hz", integer=True)
        self._slider(box, 2, "input_gain_db", i18n.t("settings.input_gain"), -20, 20, "dB")
        self._slider(box, 3, "makeup_gain_db", i18n.t("settings.output_gain"), -12, 18, "dB")

        box = ttk.Labelframe(parent, text=i18n.t("settings.gate"), padding=10)
        box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        box.columnconfigure(1, weight=1)
        self._vars["gate_enabled"] = tk.BooleanVar()
        _tip(
            ttk.Checkbutton(
                box,
                text=i18n.t("settings.gate_enabled"),
                variable=self._vars["gate_enabled"],
                command=self._schedule_apply,
            ),
            _help("gate_enabled"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self._slider(box, 1, "gate_floor_db", i18n.t("settings.gate_floor"), -80, -6, "dB")
        self._slider(box, 2, "vad_threshold_db", i18n.t("settings.vad_threshold"), 0.3, 5, "dB")
        self._slider(box, 3, "vad_hangover_ms", i18n.t("settings.hangover"), 50, 700, "ms", integer=True)
        self._slider(box, 4, "gate_attack_ms", i18n.t("settings.attack"), 1, 60, "ms", integer=True)
        self._slider(box, 5, "gate_release_ms", i18n.t("settings.release"), 30, 800, "ms", integer=True)
        ttk.Label(
            box,
            text=i18n.t("settings.hover_sliders"),
            style="Hint.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        box = ttk.Labelframe(parent, text=i18n.t("settings.other"), padding=10)
        box.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        box.columnconfigure(0, weight=1)
        self._vars["use_rnnoise"] = tk.BooleanVar()
        self._rnnoise_check = ttk.Checkbutton(
            box,
            text=i18n.t("settings.use_rnnoise"),
            variable=self._vars["use_rnnoise"],
            command=self._on_device_change,
        )
        self._rnnoise_check.grid(row=0, column=0, sticky="w")
        _tip(self._rnnoise_check, _help("use_rnnoise"))
        self._rnnoise_hint = ttk.Label(box, text="", style="Hint.TLabel", wraplength=430, justify="left")
        self._rnnoise_hint.grid(row=1, column=0, sticky="w", pady=(2, 8))

        links = ttk.Frame(box)
        links.grid(row=2, column=0, sticky="w")
        _tip(ttk.Button(links, text=i18n.t("settings.rnnoise_rescan"),
                        command=self._rescan_rnnoise),
             _help("rnnoise_rescan")).grid(row=0, column=2, padx=(6, 0))
        _tip(ttk.Button(links, text=i18n.t("settings.open_logs"), command=self._open_logs),
             _help("open_logs")).grid(row=0, column=0)
        _tip(ttk.Button(links, text=i18n.t("settings.reset"), command=self._reset),
             _help("reset")).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(
            parent, text="%s %s" % (APP_TITLE, VERSION), style="Hint.TLabel"
        ).grid(row=3, column=0, sticky="w", pady=(12, 0))

    def _slider(self, parent, row, key, label, lo, hi, unit, integer=False):
        # The same help hangs off the name, the slider and the readout, so it
        # appears wherever the pointer happens to rest on that row.
        help_text = _help(key)
        _tip(ttk.Label(parent, text=label), help_text).grid(
            row=row, column=0, sticky="w", pady=3)
        var = tk.DoubleVar()
        self._vars[key] = var
        value_label = ttk.Label(parent, text="", width=8, anchor="e", style="Hint.TLabel")

        def on_move(_value):
            value_label.configure(text=_format(var.get(), unit, integer))
            self._schedule_apply()

        scale = ttk.Scale(parent, from_=lo, to=hi, variable=var, command=on_move)
        scale.grid(row=row, column=1, sticky="ew", padx=(10, 6))
        value_label.grid(row=row, column=2, sticky="e")
        _tip(scale, help_text)
        _tip(value_label, help_text)
        self._vars[key + "__label"] = (value_label, unit, integer)

    # ----------------------------------------------------------------- values

    def _reload_devices(self):
        self._in_devices = devices.list_devices("input")
        self._out_devices = devices.list_devices("output")
        if self.win is None:
            return
        self._in_combo["values"] = [d.label for d in self._in_devices] or [i18n.t("settings.none_found")]
        out_labels = [d.label for d in self._out_devices] or [i18n.t("settings.none_found")]
        self._out_combo["values"] = out_labels
        self._mon_combo["values"] = out_labels

    def _load_values(self):
        """Push cfg into the widgets without triggering apply callbacks."""
        self._loading = True
        try:
            cfg = self.app.cfg
            _select(self._in_combo, self._in_devices, cfg.get("input_device"))
            _select(self._out_combo, self._out_devices, cfg.get("output_device"))
            _select(self._mon_combo, self._out_devices, cfg.get("monitor_device"))

            for key, var in self._vars.items():
                if key.endswith("__label"):
                    continue
                if key == "preset":
                    var.set(config.detect_preset(cfg))
                elif isinstance(var, tk.BooleanVar):
                    var.set(bool(cfg.get(key, False)))
                elif isinstance(var, tk.DoubleVar):
                    var.set(float(cfg.get(key, 0.0)))

            if autostart.supported():
                self._vars["autostart"].set(autostart.is_enabled())

            for key, meta in list(self._vars.items()):
                if key.endswith("__label"):
                    widget, unit, integer = meta
                    source = self._vars[key[: -len("__label")]]
                    widget.configure(text=_format(source.get(), unit, integer))

            self._rnnoise_hint.configure(text=_rnnoise_status())
            self._update_cable_hint()
            self._refresh_shortcut_button()
        finally:
            self._loading = False

    def _collect(self):
        cfg = dict(self.app.cfg)
        cfg["input_device"] = _device_name(self._in_combo, self._in_devices)
        cfg["output_device"] = _device_name(self._out_combo, self._out_devices)
        cfg["monitor_device"] = _device_name(self._mon_combo, self._out_devices)
        for key, var in self._vars.items():
            if key.endswith("__label") or key in ("preset", "autostart"):
                continue
            if isinstance(var, tk.BooleanVar):
                cfg[key] = bool(var.get())
            elif isinstance(var, tk.DoubleVar):
                cfg[key] = round(float(var.get()), 2)
        cfg["preset"] = config.detect_preset(cfg)
        return cfg

    # ---------------------------------------------------------------- actions

    def _schedule_apply(self, delay=180):
        """Debounce: sliders fire on every pixel of movement."""
        if self._loading or self.win is None:
            return
        if self._apply_job is not None:
            try:
                self.root.after_cancel(self._apply_job)
            except Exception:
                pass
        self._apply_job = self.root.after(delay, self._apply)

    def _apply(self):
        self._apply_job = None
        if self._loading:
            return
        cfg = self._collect()
        self.app.update_config(cfg)
        if self.win is not None and self.win.winfo_exists():
            self._vars["preset"].set(config.detect_preset(cfg))
            self._update_cable_hint()

    def _on_device_change(self):
        self._schedule_apply(delay=50)

    def _on_preset(self):
        if self._loading:
            return
        cfg = config.apply_preset(self._collect(), self._vars["preset"].get())
        self.app.update_config(cfg)
        self._load_values()

    def _on_autostart(self):
        if self._loading:
            return
        wanted = bool(self._vars["autostart"].get())
        if not autostart.set_enabled(wanted):
            messagebox.showwarning(
                APP_TITLE, i18n.t("settings.autostart_failed"), parent=self.win
            )
        self._vars["autostart"].set(autostart.is_enabled())
        cfg = dict(self.app.cfg)
        cfg["autostart"] = self._vars["autostart"].get()
        self.app.update_config(cfg)

    def _make_shortcut(self):
        """Put the app on the desktop and in the Start Menu."""
        desktop = shortcut.create("desktop")
        shortcut.create("start_menu")  # a bonus; not worth reporting on its own
        if desktop:
            messagebox.showinfo(
                APP_TITLE,
                i18n.t("settings.shortcut_done"),
                parent=self.win,
            )
        else:
            messagebox.showwarning(
                APP_TITLE,
                i18n.t("settings.shortcut_failed"),
                parent=self.win,
            )
        self._refresh_shortcut_button()

    def _refresh_shortcut_button(self):
        if not shortcut.supported():
            return
        self._shortcut_button.configure(
            text=i18n.t("settings.shortcut_update") if shortcut.exists("desktop")
            else i18n.t("settings.shortcut_create")
        )

    def _rescan_rnnoise(self):
        """Look again after the user has copied a library into place."""
        RNNoise.rescan()
        self._rnnoise_hint.configure(text=_rnnoise_status())
        for note in RNNoise.notes():
            log.info("RNNoise: %s", note)
        if RNNoise.available():
            self.app.update_config(self._collect())  # restarts with it enabled
        else:
            messagebox.showinfo(
                APP_TITLE,
                i18n.t("settings.rnnoise_not_found", dirs="\n".join(rnnoise_search_dirs())),
                parent=self.win,
            )

    def _rescan(self):
        devices.refresh()
        self._reload_devices()
        self._load_values()

    def _restart(self):
        self.app.restart_audio()

    def _reset(self):
        if not messagebox.askyesno(
            APP_TITLE, i18n.t("settings.reset_confirm"), parent=self.win
        ):
            return
        cfg = dict(config.DEFAULTS)
        cfg["input_device"] = self.app.cfg.get("input_device")
        cfg["output_device"] = self.app.cfg.get("output_device")
        cfg["monitor_device"] = self.app.cfg.get("monitor_device")
        cfg["language"] = self.app.cfg.get("language")  # a reset is about audio, not UI
        self.app.update_config(cfg)
        self._load_values()

    def _open_logs(self):
        path = config.config_dir()
        try:
            if os.name == "nt":
                os.startfile(path)  # noqa: S606 - intentional shell open
            else:
                webbrowser.open("file://" + path)
        except Exception:
            messagebox.showinfo(APP_TITLE, path, parent=self.win)

    # ----------------------------------------------------------------- meters

    def _update_cable_hint(self):
        name = _device_name(self._out_combo, self._out_devices)
        if devices.is_virtual_sink(name):
            source = devices.find_virtual_source()
            target = source.name if source else "CABLE Output"
            self._cable_hint.configure(
                text=i18n.t("settings.cable_ok", device=target),
                foreground=_COL_OK,
            )
            self._cable_button.grid_remove()
        else:
            self._cable_hint.configure(
                text=i18n.t("settings.cable_missing"),
                foreground=_COL_WARN,
            )
            self._cable_button.grid()

    def _tick_meters(self):
        if self.win is None or not self.win.winfo_exists():
            self._meter_job = None
            return
        status = self.app.engine.status()
        self._meter_in.set(status.get("in_level_db", METER_FLOOR_DB), active=True)
        self._meter_out.set(
            status.get("out_level_db", METER_FLOOR_DB), active=status.get("speech", False)
        )

        if status.get("error"):
            self._status_label.configure(text=i18n.t("status.error"), foreground=_COL_ERR)
            self._status_hint.configure(text=str(status["error"]))
        elif not status.get("running"):
            self._status_label.configure(text=i18n.t("status.stopped"), foreground=_COL_MUTED)
            self._status_hint.configure(text=i18n.t("settings.status.stopped"))
        elif status.get("bypassed"):
            self._status_label.configure(text=i18n.t("status.bypassed"), foreground=_COL_WARN)
            self._status_hint.configure(text=i18n.t("settings.status.bypassed"))
        else:
            self._status_label.configure(text=i18n.t("status.active"), foreground=_COL_OK)
            self._status_hint.configure(
                text="%s  ->  %s   (%s, ~%s ms%s)"
                % (
                    status.get("input_name", "?"),
                    status.get("output_name", "?"),
                    status.get("output_api", "?"),
                    status.get("latency_ms", "?"),
                    ", RNNoise" if status.get("rnnoise") else "",
                )
            )

        self._speech_label.configure(
            text=i18n.t("settings.voice_detected")
            if status.get("speech")
            else i18n.t("settings.quiet")
        )
        self._meter_job = self.root.after(60, self._tick_meters)


class _Meter(tk.Canvas):
    """A simple horizontal dB level bar."""

    def __init__(self, parent, width=300, height=14):
        tk.Canvas.__init__(
            self, parent, width=width, height=height, highlightthickness=0, bg="#e3e6ea"
        )
        # Not _w / _h: tkinter.Misc stores the widget's Tcl path name in _w.
        self._px_width = width
        self._px_height = height
        self._bar = self.create_rectangle(0, 0, 0, height, fill=_COL_OK, width=0)

    def set(self, level_db, active=True):
        fraction = max(0.0, min(1.0, (level_db - METER_FLOOR_DB) / (0.0 - METER_FLOOR_DB)))
        self.coords(self._bar, 0, 0, self._px_width * fraction, self._px_height)
        if level_db > -1.5:
            colour = _COL_ERR
        elif active:
            colour = _COL_OK
        else:
            colour = "#9aa3af"
        self.itemconfigure(self._bar, fill=colour)


def _format(value, unit, integer):
    return "%d %s" % (round(value), unit) if integer else "%.1f %s" % (value, unit)


def _select(combo, device_list, name):
    """Point a combobox at the device with the given name, if present."""
    if not device_list:
        combo.set("")
        return
    labels = [d.label for d in device_list]
    if name:
        lowered = name.strip().lower()
        for dev, label in zip(device_list, labels):
            if dev.name.lower() == lowered:
                combo.set(label)
                return
    combo.set(labels[0])


def _device_name(combo, device_list):
    label = combo.get()
    for dev in device_list:
        if dev.label == label:
            return dev.name
    return None


class _Tooltip(object):
    """Hover help.

    Tk ships no tooltip widget, so this is a borderless Toplevel that appears
    after a short dwell. The delay matters: without it every pass of the mouse
    across the panel would flash a box. It hides on leave and on any click, so
    it never sits on top of a slider being dragged.
    """

    _visible = None  # only ever one on screen, whatever the mouse did

    def __init__(self, widget, text, delay=450, wrap=330):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.wrap = wrap
        self._job = None
        self._win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event=None):
        self.hide()
        try:
            self._job = self.widget.after(self.delay, self.show)
        except tk.TclError:
            self._job = None

    def show(self):
        self._job = None
        if self._win is not None or not self.text:
            return
        try:
            if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
                return  # the window closed while we were waiting
        except tk.TclError:
            return

        if _Tooltip._visible is not None and _Tooltip._visible is not self:
            _Tooltip._visible.hide()

        try:
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
            tk.Label(
                win,
                text=self.text,
                justify="left",
                wraplength=self.wrap,
                background=_COL_TIP_BG,
                foreground=_COL_TIP_FG,
                font=("Segoe UI", 9),
                padx=10,
                pady=8,
                borderwidth=0,
            ).pack()
            win.update_idletasks()
            win.wm_geometry("+%d+%d" % self._position(win))
        except tk.TclError:
            return
        self._win = win
        _Tooltip._visible = self

    def _position(self, win):
        """Below the widget, pushed back on screen if it would fall off."""
        width, height = win.winfo_width(), win.winfo_height()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        x = max(4, min(x, self.widget.winfo_screenwidth() - width - 4))
        if y + height > self.widget.winfo_screenheight() - 4:
            y = self.widget.winfo_rooty() - height - 6  # flip above instead
        return x, max(4, y)

    def hide(self, _event=None):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None
        if _Tooltip._visible is self:
            _Tooltip._visible = None


def _rnnoise_status():
    """One line describing what the RNNoise scan turned up."""
    if not RNNoise.available():
        return i18n.t("settings.rnnoise_none")
    kind = i18n.t("settings.rnnoise_kind.%s" % RNNoise.kind())
    return i18n.t("settings.rnnoise_active", kind=kind, path=RNNoise.library_path())


def _help(key):
    """Hover help for a control, in the current language."""
    return i18n.t("help." + key) if i18n.has("help." + key) else None


def _tip(widget, text):
    """Attach hover help and return the widget, so calls can wrap creation."""
    if text:
        _Tooltip(widget, text)
    return widget


def _raise(win):
    """Bring the window to the front. A window manager may refuse; that is fine."""
    try:
        win.lift()
        win.focus_force()
    except Exception:
        log.debug("Could not raise the settings window", exc_info=True)


def _center(win):
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    x = (win.winfo_screenwidth() - w) // 2
    y = max(0, (win.winfo_screenheight() - h) // 3)
    win.geometry("+%d+%d" % (x, y))
