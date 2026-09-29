"""Desktop and Start Menu shortcuts.

A .pyw file can be double-clicked, but it looks like a Python file and sits
wherever the source happens to live. A shortcut gives the app its own icon and
a normal place to start from.

Windows has no public API for writing a .lnk, so the file itself is created
through the WScript.Shell COM object via PowerShell. The folder paths are
resolved with SHGetFolderPath rather than guessed from the home directory,
because a redirected (OneDrive) Desktop is common and guessing gets it wrong.
"""

import logging
import os
import subprocess
import sys

from . import APP_TITLE, config, i18n
from .icon import save_ico

log = logging.getLogger(__name__)

_CSIDL_DESKTOPDIRECTORY = 0x0010
_CSIDL_PROGRAMS = 0x0002
_SHORTCUT_NAME = "%s.lnk" % APP_TITLE
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def supported():
    return os.name == "nt"


def _known_folder(csidl):
    import ctypes
    from ctypes import wintypes

    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return buf.value if result == 0 and buf.value else None


def locations():
    """{'desktop': path, 'start_menu': path} for the shortcuts we manage."""
    if not supported():
        return {}
    found = {}
    for key, csidl in (("desktop", _CSIDL_DESKTOPDIRECTORY), ("start_menu", _CSIDL_PROGRAMS)):
        folder = _known_folder(csidl)
        if folder:
            found[key] = os.path.join(folder, _SHORTCUT_NAME)
    return found


def exists(where="desktop"):
    path = locations().get(where)
    return bool(path) and os.path.isfile(path)


def _launch_target():
    """(target, arguments) that the shortcut should run."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), ""

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(root, "MicNoiseCancel.pyw")
    if not os.path.isfile(launcher):
        launcher = os.path.join(root, "run.py")

    # sys.executable is already the environment that has the dependencies;
    # its pythonw sibling starts without a console window.
    exe = sys.executable
    quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(quiet):
        exe = quiet
    return exe, '"%s"' % launcher


def _icon_path():
    """A .ico for the shortcut, written next to the config."""
    path = os.path.join(config.config_dir(), "app.ico")
    try:
        if not os.path.isfile(path):
            save_ico(path)
    except Exception:
        log.exception("Could not write the shortcut icon")
        return None
    return path


def _ps_quote(text):
    return "'%s'" % str(text).replace("'", "''")


def create(where="desktop"):
    """Create (or overwrite) a shortcut. Returns True on success."""
    if not supported():
        return False
    path = locations().get(where)
    if not path:
        log.warning("Could not resolve the %s folder", where)
        return False

    target, arguments = _launch_target()
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon = _icon_path()

    script = [
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(%s)" % _ps_quote(path),
        "$s.TargetPath = %s" % _ps_quote(target),
        "$s.Arguments = %s" % _ps_quote(arguments),
        "$s.WorkingDirectory = %s" % _ps_quote(workdir),
        "$s.Description = %s" % _ps_quote(i18n.t("shortcut.description")),
    ]
    if icon:
        script.append("$s.IconLocation = %s" % _ps_quote(icon))
    script.append("$s.Save()")

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", "; ".join(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
            timeout=30,
        )
    except Exception:
        log.exception("Creating the %s shortcut failed", where)
        return False

    if proc.returncode != 0:
        log.warning("PowerShell refused to create the shortcut: %s",
                    (proc.stdout or b"").decode("utf-8", "replace").strip())
        return False
    log.info("Created shortcut at %s", path)
    return os.path.isfile(path)


def remove(where="desktop"):
    path = locations().get(where)
    if not path or not os.path.isfile(path):
        return True
    try:
        os.unlink(path)
        return True
    except OSError:
        log.exception("Could not remove %s", path)
        return False
