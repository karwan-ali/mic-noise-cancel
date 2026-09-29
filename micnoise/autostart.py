"""Start-with-Windows support via the per-user Run key.

Nothing here needs administrator rights, and everything degrades to a no-op on
non-Windows platforms so the module can be imported anywhere.
"""

import logging
import os
import sys

from . import APP_NAME

log = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def supported():
    return os.name == "nt"


def _command():
    """The command line Windows should run at logon."""
    if getattr(sys, "frozen", False):
        return '"%s" --startup' % os.path.abspath(sys.executable)

    # Prefer pythonw.exe so a source checkout starts without a console window.
    exe = sys.executable
    quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(quiet):
        exe = quiet
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(root, "MicNoiseCancel.pyw")
    if not os.path.isfile(launcher):
        launcher = os.path.join(root, "run.py")
    return '"%s" "%s" --startup' % (exe, launcher)


def is_enabled():
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(enabled):
    """Add or remove the Run entry. Returns True on success."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command())
                log.info("Autostart enabled: %s", _command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    log.info("Autostart disabled")
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        log.exception("Could not update the autostart entry")
        return False
