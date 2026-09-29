"""Double-click launcher for Mic Noise Cancel.

Windows binds .pyw to pythonw.exe, so opening this file starts the app with no
console window - no batch file involved. If the dependencies are not installed
yet it builds a local .venv first, showing a small progress window, then hands
over to it. Nothing here imports the app's own dependencies, because the whole
point is that they may not exist yet.
"""

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")

# Import names, which are not always the package names on PyPI.
REQUIRED_MODULES = ("numpy", "sounddevice", "pystray", "PIL")

APP_TITLE = "Mic Noise Cancel"
PYTHON_URL = "https://www.python.org/downloads/windows/"

# Where the micnoise package lives. Captured once: tests repoint HERE at a
# scratch directory, and the translations must still come from the checkout.
_PKG_ROOT = HERE
_translate = None

# Keep pip from flashing a console window when we are running under pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def venv_python(windowless=False):
    """Path to the interpreter inside our virtual environment."""
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "pythonw.exe" if windowless else "python.exe")
    return os.path.join(VENV, "bin", "python")


def tr(key, **params):
    """UI text from micnoise.i18n, which is standard library only.

    Imported lazily so this file still needs nothing beyond the standard
    library at module scope, and so the language comes from the saved config
    when there is one.
    """
    global _translate
    if _translate is None:
        try:
            if _PKG_ROOT not in sys.path:
                sys.path.insert(0, _PKG_ROOT)
            from micnoise import config, i18n

            i18n.set_language(config.load().get("language"))
            _translate = i18n.t
        except Exception:
            _translate = lambda key, **params: key  # never block startup over wording
    return _translate(key, **params)


def missing_modules():
    import importlib.util

    absent = []
    for name in REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                absent.append(name)
        except (ImportError, ValueError):
            absent.append(name)
    return absent


# --------------------------------------------------------------- progress UI


class _Progress(object):
    """A minimal Tk window, so a 60 second install is not a silent 60 seconds.

    tkinter ships with Python itself, so this works before anything has been
    installed - which is exactly when it is needed.
    """

    def __init__(self, title, message):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.title(title)
        self.root.resizable(False, False)
        frame = ttk.Frame(self.root, padding=(20, 16))
        frame.grid()
        ttk.Label(frame, text=message, font=("Segoe UI", 10)).grid(sticky="w")
        self.status = ttk.Label(frame, text="", foreground="#6b7280")
        self.status.grid(sticky="w", pady=(6, 10))
        self.bar = ttk.Progressbar(frame, mode="indeterminate", length=340)
        self.bar.grid(sticky="ew")
        self.bar.start(12)
        self.root.update_idletasks()
        self._center()

    def _center(self):
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry("+%d+%d" % (x, y))

    def set_status(self, text):
        self.status.configure(text=text)

    def pump(self):
        self.root.update()

    def close(self):
        try:
            self.bar.stop()
            self.root.destroy()
        except Exception:
            pass


def message(text, error=True):
    """Report to the user without assuming a console exists."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)(APP_TITLE, text)
        root.destroy()
        return
    except Exception:
        pass
    sys.stderr.write(text + "\n")


# ---------------------------------------------------------------- bootstrap


def _run(command, report):
    """Run a subprocess quietly; returns (ok, combined output)."""
    report(command[-1] if len(command) > 3 else " ".join(command[-2:]))
    try:
        proc = subprocess.run(
            command,
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        return False, str(exc)
    output = (proc.stdout or b"").decode("utf-8", "replace")
    return proc.returncode == 0, output


def _install_everything(report):
    """Create the venv if needed and install the requirements into it."""
    if not os.path.isfile(venv_python()):
        report(tr("launcher.creating_env"))
        ok, out = _run([sys.executable, "-m", "venv", VENV], report)
        if not ok:
            return False, tr("launcher.venv_failed", output=out.strip()[-800:], url=PYTHON_URL)

    python = venv_python()
    report(tr("launcher.updating_pip"))
    _run([python, "-m", "pip", "install", "--upgrade", "pip"], report)  # best effort

    report(tr("launcher.installing"))
    ok, out = _run([python, "-m", "pip", "install", "-r", REQUIREMENTS], report)
    if not ok:
        return False, tr("launcher.install_failed", output=out.strip()[-800:])
    return True, ""


def bootstrap():
    """Build the environment with a progress window. Returns True on success."""
    ui = None
    try:
        ui = _Progress(APP_TITLE, tr("launcher.preparing"))
    except Exception:
        pass  # no display: fall through and install silently

    outcome = {}
    updates = []

    def report(text):
        updates.append(text)

    def work():
        try:
            outcome["ok"], outcome["error"] = _install_everything(report)
        except Exception as exc:
            outcome["ok"], outcome["error"] = False, str(exc)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive():
        if ui is not None:
            while updates:
                ui.set_status(updates.pop(0))
            try:
                ui.pump()
            except Exception:
                ui = None
        time.sleep(0.05)
    worker.join(5)

    if ui is not None:
        ui.close()
    if not outcome.get("ok"):
        message(outcome.get("error") or tr("launcher.failed"))
        return False
    return True


def relaunch(argv):
    """Hand over to the interpreter that actually has the dependencies."""
    python = venv_python(windowless=True)
    if not os.path.isfile(python):
        python = venv_python()
    env = dict(os.environ, MICNOISE_BOOTSTRAPPED="1")
    try:
        subprocess.Popen([python, os.path.abspath(__file__)] + argv, cwd=HERE, env=env)
    except Exception as exc:
        message(tr("launcher.start_failed", error=exc))
        return 1
    return 0


def start_app(argv):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from micnoise.app import main

    return main(argv)


def main():
    argv = sys.argv[1:]

    if not missing_modules():
        return start_app(argv)

    if os.environ.get("MICNOISE_BOOTSTRAPPED") == "1":
        # We already handed over once, so installing again would just loop.
        message(tr("launcher.still_missing", modules=", ".join(missing_modules())))
        return 1

    if not bootstrap():
        return 1
    return relaunch(argv)


if __name__ == "__main__":
    sys.exit(main())
