"""Application controller.

Threading model: Tk owns the main thread, pystray runs on its own thread, and
PortAudio calls back on its own. Anything that touches Tk or mutates config
goes through `post()`, which is drained on the main thread.
"""

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from logging.handlers import RotatingFileHandler

from . import APP_NAME, APP_TITLE, autostart, config, devices, i18n
from .engine import AudioEngine
from .settings_ui import SettingsWindow
from .tray import TrayIcon

log = logging.getLogger(__name__)


class Application(object):
    def __init__(self, first_run=False, from_startup=False):
        self.cfg = config.load()
        i18n.set_language(self.cfg.get("language"))
        self.first_run = first_run
        self.from_startup = from_startup

        if devices.suggest_defaults(self.cfg):
            config.save(self.cfg)

        self._tasks = queue.Queue()
        self._quitting = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)

        self.engine = AudioEngine(self.cfg, on_state_change=self._on_engine_state)
        self.settings = SettingsWindow(self.root, self)
        self.tray = TrayIcon(self)
        self._tray_thread = None

    # ------------------------------------------------------------- lifecycle

    def run(self):
        self._tray_thread = threading.Thread(target=self.tray.run, name="mnc-tray", daemon=True)
        self._tray_thread.start()

        self.root.after(120, self._startup)
        self.root.after(50, self._drain)
        self.root.after(1000, self._refresh_tray_periodically)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    def _startup(self):
        started = self.engine.start()
        self.tray.refresh()

        if not started and self.engine.error:
            self.tray.notify(str(self.engine.error),
                             i18n.t("app.start_failed_title", app=APP_TITLE))
            self.open_settings()
            return

        if not self.engine.status().get("virtual_cable"):
            self.tray.notify(
                i18n.t("app.no_cable"),
                APP_TITLE,
            )
            if not self.from_startup:
                self.open_settings()
        elif self.first_run and not self.from_startup:
            source = devices.find_virtual_source()
            self.tray.notify(
                i18n.t("app.ready", device=source.name if source else "CABLE Output"),
                APP_TITLE,
            )

    def _shutdown(self):
        try:
            self.engine.stop()
        except Exception:
            log.exception("Engine shutdown failed")
        try:
            self.tray.stop()
        except Exception:
            log.exception("Tray shutdown failed")
        config.save(self.cfg)
        log.info("Exited cleanly")

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        log.info("Quit requested")
        self.settings.close()
        self.root.quit()

    # ------------------------------------------------------- task marshalling

    def post(self, fn):
        """Run fn on the Tk thread. Safe to call from any thread."""
        self._tasks.put(fn)

    def _drain(self):
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception:
                log.exception("Queued task failed")
        if not self._quitting:
            self.root.after(50, self._drain)

    def _on_engine_state(self):
        self.post(self.tray.refresh)

    def _refresh_tray_periodically(self):
        if self._quitting:
            return
        self.tray.refresh()
        self.root.after(2000, self._refresh_tray_periodically)

    # ---------------------------------------------------------------- actions

    def update_config(self, cfg):
        self.cfg = dict(cfg)
        config.save(self.cfg)
        self.engine.apply_config(self.cfg)
        self.tray.refresh()

    def toggle_enabled(self):
        cfg = dict(self.cfg)
        cfg["enabled"] = not bool(cfg.get("enabled", True))
        self.update_config(cfg)

    def toggle_monitor(self):
        cfg = dict(self.cfg)
        cfg["monitor_enabled"] = not bool(cfg.get("monitor_enabled", False))
        if cfg["monitor_enabled"] and not cfg.get("monitor_device"):
            dev = devices.default_device("output")
            if dev and not devices.is_virtual_sink(dev.name):
                cfg["monitor_device"] = dev.name
        self.update_config(cfg)

    def set_preset(self, name):
        self.update_config(config.apply_preset(dict(self.cfg), name))

    def set_device(self, key, name):
        cfg = dict(self.cfg)
        cfg[key] = name
        self.update_config(cfg)

    def restart_audio(self):
        devices.refresh()
        self.engine.restart()
        self.tray.refresh()
        if self.engine.error:
            self.tray.notify(str(self.engine.error), APP_TITLE)

    def open_settings(self):
        self.settings.show()

    def set_language(self, code):
        """Switch the UI language and remember the choice."""
        code = i18n.set_language(code)
        self.cfg = dict(self.cfg, language=code)
        config.save(self.cfg)
        log.info("UI language set to %s", code)
        self.tray.refresh()
        if self.settings.win is not None:
            self.root.after_idle(self.settings.rebuild)


# --------------------------------------------------------------------- boot


def setup_logging():
    path = os.path.join(config.config_dir(), "app.log")
    handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None and sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(console)
    return path


def enable_dpi_awareness():
    if os.name != "nt":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# Held for the lifetime of the process; Windows releases it on exit.
_INSTANCE_MUTEX = None


def acquire_single_instance():
    """Return a handle if we are the only instance, else None."""
    if os.name != "nt":
        return object()
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, "Global\\%s.singleton" % APP_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from_startup = "--startup" in argv

    first_run = not os.path.exists(config.config_path())
    log_path = setup_logging()
    log.info("Starting %s (startup=%s, first_run=%s)", APP_NAME, from_startup, first_run)
    # Before anything can show a message box, so even startup errors are in
    # the user's language.
    log.info("UI language: %s", i18n.set_language(config.load().get("language")))

    global _INSTANCE_MUTEX
    _INSTANCE_MUTEX = acquire_single_instance()
    if _INSTANCE_MUTEX is None:
        log.info("Another instance is already running, exiting")
        _message(i18n.t("app.already_running", app=APP_TITLE))
        return 0

    enable_dpi_awareness()

    try:
        import sounddevice  # noqa: F401
    except Exception as exc:
        log.exception("sounddevice import failed")
        _message(i18n.t("app.audio_library_failed", error=exc))
        return 1

    try:
        app = Application(first_run=first_run, from_startup=from_startup)
    except Exception as exc:
        log.exception("Startup failed")
        _message(i18n.t("app.start_failed", app=APP_TITLE, error=exc, log=log_path))
        return 1

    if from_startup and not autostart.is_enabled():
        log.info("Launched with --startup but the registry entry is gone")

    app.run()
    return 0


def _message(text):
    """Show an error without assuming a console or a running Tk."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x40)
            return
        except Exception:
            pass
    sys.stderr.write(text + "\n")
