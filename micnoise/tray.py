"""System tray icon and menu (pystray).

pystray owns a thread of its own and calls menu handlers on it. Nothing here
touches Tk or the config directly - every action is handed to the main thread
through the app's task queue.
"""

import logging

import pystray
from pystray import Menu, MenuItem

from . import APP_TITLE, VERSION, config, devices, i18n
from .icon import make_icon

log = logging.getLogger(__name__)

_MAX_DEVICES_IN_MENU = 12


class TrayIcon(object):
    def __init__(self, app):
        self.app = app
        self._icons = {state: make_icon(state) for state in ("active", "bypassed", "stopped", "error")}
        self._state = None
        self._signature_cache = None
        self.icon = pystray.Icon(
            APP_TITLE.replace(" ", ""),
            icon=self._icons["stopped"],
            title=APP_TITLE,
            menu=self._menu(),
        )

    # ------------------------------------------------------------------- api

    def run(self):
        """Blocks until stop(); call it on a dedicated thread."""
        self.icon.run()

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            log.debug("Tray stop failed", exc_info=True)

    def notify(self, message, title=None):
        try:
            self.icon.notify(message, title or APP_TITLE)
        except Exception:
            log.debug("Notification not supported", exc_info=True)

    def refresh(self):
        """Repaint the icon and tooltip from the current engine status."""
        status = self.app.engine.status()
        if status.get("error"):
            state = "error"
        elif not status.get("running"):
            state = "stopped"
        elif status.get("bypassed"):
            state = "bypassed"
        else:
            state = "active"

        if state != self._state:
            self._state = state
            try:
                self.icon.icon = self._icons[state]
            except Exception:
                log.debug("Could not update the tray icon", exc_info=True)

        try:
            self.icon.title = self._tooltip(status, state)
        except Exception:
            log.debug("Could not update the tray tooltip", exc_info=True)

        # This runs on a 2 s timer, and rebuilding means re-enumerating every
        # audio device, so only do it when something a menu item shows changed.
        signature = self._signature(status, state)
        if signature == self._signature_cache:
            return
        self._signature_cache = signature
        try:
            self.icon.menu = self._menu()
            self.icon.update_menu()
        except Exception:
            log.debug("Could not update the tray menu", exc_info=True)

    def _signature(self, status, state):
        cfg = self.app.cfg
        return (
            state,
            status.get("error"),
            status.get("virtual_cable"),
            bool(cfg.get("enabled", True)),
            bool(cfg.get("monitor_enabled", False)),
            config.detect_preset(cfg),
            cfg.get("input_device"),
            cfg.get("output_device"),
            i18n.get_language(),
        )

    def _tooltip(self, status, state):
        lines = ["%s %s" % (APP_TITLE, VERSION)]
        if state == "error":
            lines.append(i18n.t("tray.tip.error", error=status.get("error")))
        elif state == "stopped":
            lines.append(i18n.t("status.stopped"))
        else:
            lines.append(i18n.t("status.bypassed" if state == "bypassed" else "status.active"))
            lines.append(i18n.t("tray.tip.input", device=_short(status.get("input_name"))))
            lines.append(i18n.t("tray.tip.output", device=_short(status.get("output_name"))))
        return "\n".join(lines)

    # ------------------------------------------------------------------ menu

    def _menu(self):
        app = self.app
        status = app.engine.status()

        return Menu(
            MenuItem(_headline(status), None, enabled=False),
            Menu.SEPARATOR,
            MenuItem(
                i18n.t("tray.enabled"),
                lambda _i, _it: app.post(app.toggle_enabled),
                checked=lambda _i: bool(app.cfg.get("enabled", True)),
            ),
            MenuItem(i18n.t("tray.level"), self._preset_menu()),
            Menu.SEPARATOR,
            MenuItem(i18n.t("tray.microphone"), self._device_menu("input", "input_device")),
            MenuItem(i18n.t("tray.send_to"), self._device_menu("output", "output_device")),
            MenuItem(
                i18n.t("tray.monitor"),
                lambda _i, _it: app.post(app.toggle_monitor),
                checked=lambda _i: bool(app.cfg.get("monitor_enabled", False)),
            ),
            Menu.SEPARATOR,
            MenuItem(
                i18n.t("tray.settings"),
                lambda _i, _it: app.post(app.open_settings),
                default=True,
            ),
            MenuItem(i18n.LANGUAGE_LABEL, self._language_menu()),
            MenuItem(i18n.t("action.restart_audio"), lambda _i, _it: app.post(app.restart_audio)),
            Menu.SEPARATOR,
            MenuItem(i18n.t("tray.quit"), lambda _i, _it: app.post(app.quit)),
        )

    def _language_menu(self):
        app = self.app
        return Menu(*[
            MenuItem(
                i18n.NAMES[code],
                _language_handler(app, code),
                checked=_language_checker(code),
                radio=True,
            )
            for code in i18n.SUPPORTED
        ])

    def _preset_menu(self):
        app = self.app
        items = []
        for name in config.PRESET_NAMES:
            items.append(
                MenuItem(
                    config.preset_label(name),
                    _preset_handler(app, name),
                    checked=_preset_checker(app, name),
                    radio=True,
                )
            )
        return Menu(*items)

    def _device_menu(self, kind, key):
        app = self.app
        try:
            found = devices.list_devices(kind)
        except Exception:
            log.exception("Device listing failed")
            found = []

        if not found:
            return Menu(MenuItem(i18n.t("tray.no_devices"), None, enabled=False))

        current = app.cfg.get(key)
        shown = found[:_MAX_DEVICES_IN_MENU]
        if current and all(d.name != current for d in shown):
            match = next((d for d in found if d.name == current), None)
            if match is not None:
                shown = [match] + shown[: _MAX_DEVICES_IN_MENU - 1]

        items = [
            MenuItem(
                _short(dev.name, 44),
                _device_handler(app, key, dev.name),
                checked=_device_checker(app, key, dev.name),
                radio=True,
            )
            for dev in shown
        ]
        if len(found) > len(shown):
            items.append(Menu.SEPARATOR)
            items.append(
                MenuItem(i18n.t("tray.more_in_settings"),
                         lambda _i, _it: app.post(app.open_settings))
            )
        return Menu(*items)


# Handlers are built by these helpers so each closure captures its own value
# rather than the last one from the loop.


def _preset_handler(app, name):
    return lambda _icon, _item: app.post(lambda: app.set_preset(name))


def _preset_checker(app, name):
    return lambda _item: config.detect_preset(app.cfg) == name


def _device_handler(app, key, value):
    return lambda _icon, _item: app.post(lambda: app.set_device(key, value))


def _device_checker(app, key, value):
    return lambda _item: app.cfg.get(key) == value


def _language_handler(app, code):
    return lambda _icon, _item: app.post(lambda: app.set_language(code))


def _language_checker(code):
    return lambda _item: i18n.get_language() == code


def _headline(status):
    if status.get("error"):
        return i18n.t("tray.headline.error")
    if not status.get("running"):
        return i18n.t("status.stopped")
    if status.get("bypassed"):
        return i18n.t("status.bypassed")
    if not status.get("virtual_cable"):
        return i18n.t("tray.headline.no_cable")
    return i18n.t("tray.headline.active")


def _short(name, limit=34):
    if not name:
        return "-"
    name = str(name)
    return name if len(name) <= limit else name[: limit - 1] + "…"
