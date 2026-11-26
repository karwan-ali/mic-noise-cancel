//! Application controller and the UI thread.
//!
//! Threading model: one thread owns every window, the tray icon, the config
//! and the audio engine. WASAPI runs its callbacks on threads of its own, and
//! those only ever touch atomics and ring buffers inside the engine.
//!
//! All state lives in one `App`, reached through `with_app`. Window procedures
//! can be re-entered while a modal loop (a message box, a popup menu) runs, so
//! nothing modal is ever started while the App is borrowed - handlers borrow,
//! decide, release, and only then show the dialog.

use std::cell::{Cell, RefCell};
use std::sync::Arc;

use windows_sys::Win32::Foundation::{GetLastError, ERROR_ALREADY_EXISTS, HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::{CreateMutexW, GetCurrentProcess, SetProcessWorkingSetSize};
use windows_sys::Win32::UI::Controls::{
    InitCommonControlsEx, ICC_BAR_CLASSES, ICC_STANDARD_CLASSES, ICC_TAB_CLASSES, ICC_WIN95_CLASSES,
    INITCOMMONCONTROLSEX,
};
use windows_sys::Win32::UI::WindowsAndMessaging::*;

use crate::config::{self, Config, Preset, APP_NAME, APP_TITLE, VERSION};
use crate::devices::{self, Kind};
use crate::engine::Engine;
use crate::i18n::{self, t, tf, Lang, S};
use crate::icon_draw::IconState;
use crate::settings::{self, SettingsWindow};
use crate::tray::{Tray, WM_TRAY};
use crate::win::{self, wide};
use crate::{log_info, log_warn};

const WM_WAKE: u32 = WM_APP + 2;
pub const WM_REBUILD_SETTINGS: u32 = WM_APP + 3;
const WM_STARTUP: u32 = WM_APP + 4;
const TIMER_TICK: usize = 1;

const MAX_DEVICES_IN_MENU: usize = 12;

const CMD_TOGGLE: u32 = 1;
const CMD_MONITOR: u32 = 2;
const CMD_SETTINGS: u32 = 3;
const CMD_RESTART: u32 = 4;
const CMD_QUIT: u32 = 5;
const CMD_MORE: u32 = 6;
const CMD_PRESET: u32 = 10;
const CMD_LANG: u32 = 20;
const CMD_INPUT: u32 = 100;
const CMD_OUTPUT: u32 = 200;

pub struct App {
    pub cfg: Config,
    pub engine: Engine,
    pub hwnd: HWND,
    pub settings: Option<SettingsWindow>,
    tray: Tray,
    menu_inputs: Vec<String>,
    menu_outputs: Vec<String>,
    first_run: bool,
    from_startup: bool,
}

thread_local! {
    static APP: RefCell<Option<App>> = const { RefCell::new(None) };
    static TASKBAR_CREATED: Cell<u32> = const { Cell::new(0) };
}

/// Run `f` on the App, unless it is already borrowed further up the stack.
pub fn with_app<R>(f: impl FnOnce(&mut App) -> R) -> Option<R> {
    APP.with(|cell| match cell.try_borrow_mut() {
        Ok(mut guard) => guard.as_mut().map(f),
        Err(_) => None,
    })
}

/// Give back memory Windows is holding for us but we are not using.
pub fn trim_working_set() {
    unsafe { SetProcessWorkingSetSize(GetCurrentProcess(), usize::MAX, usize::MAX) };
}

impl App {
    // ------------------------------------------------------------- status

    pub fn icon_state(&self) -> IconState {
        if self.engine.error().is_some() {
            IconState::Error
        } else if !self.engine.running() {
            IconState::Stopped
        } else if !self.cfg.enabled {
            IconState::Bypassed
        } else {
            IconState::Active
        }
    }

    pub fn refresh_tray(&mut self) {
        let state = self.icon_state();
        let status = self.engine.status();
        let mut lines = vec![format!("{APP_TITLE} {VERSION}")];
        match state {
            IconState::Error => {
                lines.push(tf(S::TrayTipError, &[("error", &status.error.unwrap_or_default())]))
            }
            IconState::Stopped => lines.push(t(S::StatusStopped).into()),
            _ => {
                lines.push(t(if state == IconState::Bypassed { S::StatusBypassed } else { S::StatusActive }).into());
                lines.push(tf(S::TrayTipInput, &[("device", &short(&status.info.input_name, 34))]));
                lines.push(tf(S::TrayTipOutput, &[("device", &short(&status.info.output_name, 34))]));
            }
        }
        self.tray.update(state, &lines.join("\n"));
    }

    pub fn notify(&self, message: &str) {
        self.tray.notify(APP_TITLE, message);
    }

    // ------------------------------------------------------------- startup

    fn startup(&mut self) {
        let started = self.engine.start();
        self.refresh_tray();

        if !started {
            if let Some(err) = self.engine.error() {
                self.tray.notify(&tf(S::AppStartFailedTitle, &[("app", APP_TITLE)]), &err);
                self.open_settings();
                return;
            }
        }
        if !self.engine.status().info.virtual_cable {
            self.notify(t(S::AppNoCable));
            if !self.from_startup {
                self.open_settings();
            }
        } else if self.first_run && !self.from_startup {
            let source = devices::find_virtual_source().unwrap_or_else(|| "CABLE Output".into());
            self.notify(&tf(S::AppReady, &[("device", &source)]));
        }
    }

    fn shutdown(&mut self) {
        self.engine.stop();
        self.tray.remove();
        config::save(&self.cfg);
        log_info!("Exited cleanly");
    }

    // ------------------------------------------------------------- actions

    pub fn update_config(&mut self, cfg: Config) {
        self.cfg = cfg;
        config::save(&self.cfg);
        self.engine.apply_config(&self.cfg);
        self.refresh_tray();
    }

    fn toggle_enabled(&mut self) {
        let mut cfg = self.cfg.clone();
        cfg.enabled = !cfg.enabled;
        self.update_config(cfg);
    }

    fn toggle_monitor(&mut self) {
        let mut cfg = self.cfg.clone();
        cfg.monitor_enabled = !cfg.monitor_enabled;
        if cfg.monitor_enabled && cfg.monitor_device.is_none() {
            if let Some((name, _)) = devices::default_device(Kind::Output) {
                if !devices::is_virtual_sink(&name) {
                    cfg.monitor_device = Some(name);
                }
            }
        }
        self.update_config(cfg);
    }

    fn set_preset(&mut self, preset: Preset) {
        let mut cfg = self.cfg.clone();
        cfg.apply_preset(preset);
        self.update_config(cfg);
    }

    fn set_device(&mut self, kind: Kind, name: String) {
        let mut cfg = self.cfg.clone();
        match kind {
            Kind::Input => cfg.input_device = Some(name),
            Kind::Output => cfg.output_device = Some(name),
        }
        self.update_config(cfg);
    }

    pub fn restart_audio(&mut self) {
        self.engine.restart();
        if !self.engine.running() && self.engine.error().is_none() {
            self.engine.start();
        }
        self.refresh_tray();
        if let Some(err) = self.engine.error() {
            self.notify(&err);
        }
    }

    /// Switch the UI language and remember the choice. The caller rebuilds
    /// the settings window, which cannot happen from inside its own handler.
    pub fn set_language(&mut self, lang: Lang) {
        i18n::set_language(Some(lang.code()));
        self.cfg.language = Some(lang.code().into());
        config::save(&self.cfg);
        log_info!("UI language set to {}", lang.code());
        self.refresh_tray();
        if self.settings.is_some() {
            unsafe { PostMessageW(self.hwnd, WM_REBUILD_SETTINGS, 0, 0) };
        }
    }

    // ---------------------------------------------------------------- menu

    fn build_menu(&mut self) -> HMENU {
        let status = self.engine.status();
        let headline = if status.error.is_some() {
            S::TrayHeadlineError
        } else if !status.running {
            S::StatusStopped
        } else if status.bypassed {
            S::StatusBypassed
        } else if !status.info.virtual_cable {
            S::TrayHeadlineNoCable
        } else {
            S::TrayHeadlineActive
        };

        unsafe {
            let menu = CreatePopupMenu();
            append(menu, MF_GRAYED, 0, t(headline));
            append(menu, MF_SEPARATOR, 0, "");
            append(menu, if self.cfg.enabled { MF_CHECKED } else { 0 }, CMD_TOGGLE, t(S::TrayEnabled));

            let presets = CreatePopupMenu();
            for (i, p) in Preset::ALL.into_iter().enumerate() {
                append(presets, 0, CMD_PRESET + i as u32, t(preset_label(p)));
            }
            if let Some(i) = self.cfg.detect_preset().and_then(|p| Preset::ALL.iter().position(|&q| q == p)) {
                let id = CMD_PRESET + i as u32;
                CheckMenuRadioItem(presets, CMD_PRESET, CMD_PRESET + 2, id, MF_BYCOMMAND);
            }
            append_popup(menu, presets, t(S::TrayLevel));
            append(menu, MF_SEPARATOR, 0, "");

            self.menu_inputs = devices::names(Kind::Input);
            self.menu_outputs = devices::names(Kind::Output);
            let inputs = device_menu(&mut self.menu_inputs, self.cfg.input_device.as_deref(), CMD_INPUT);
            let outputs = device_menu(&mut self.menu_outputs, self.cfg.output_device.as_deref(), CMD_OUTPUT);
            append_popup(menu, inputs, t(S::TrayMicrophone));
            append_popup(menu, outputs, t(S::TraySendTo));
            append(menu, if self.cfg.monitor_enabled { MF_CHECKED } else { 0 }, CMD_MONITOR, t(S::TrayMonitor));
            append(menu, MF_SEPARATOR, 0, "");

            append(menu, 0, CMD_SETTINGS, t(S::TraySettings));
            SetMenuDefaultItem(menu, CMD_SETTINGS, 0);
            let langs = CreatePopupMenu();
            for (i, lang) in Lang::ALL.into_iter().enumerate() {
                append(langs, 0, CMD_LANG + i as u32, lang.name());
            }
            let current = Lang::ALL.iter().position(|&l| l == i18n::language()).unwrap_or(0) as u32;
            CheckMenuRadioItem(langs, CMD_LANG, CMD_LANG + 1, CMD_LANG + current, MF_BYCOMMAND);
            append_popup(menu, langs, i18n::LANGUAGE_LABEL);
            append(menu, 0, CMD_RESTART, t(S::ActionRestartAudio));
            append(menu, MF_SEPARATOR, 0, "");
            append(menu, 0, CMD_QUIT, t(S::TrayQuit));
            menu
        }
    }

    /// Returns true when the app should quit.
    fn handle_command(&mut self, cmd: u32) -> bool {
        match cmd {
            CMD_TOGGLE => self.toggle_enabled(),
            CMD_MONITOR => self.toggle_monitor(),
            CMD_SETTINGS | CMD_MORE => self.open_settings(),
            CMD_RESTART => self.restart_audio(),
            CMD_QUIT => return true,
            c if (CMD_PRESET..CMD_PRESET + 3).contains(&c) => self.set_preset(Preset::ALL[(c - CMD_PRESET) as usize]),
            c if (CMD_LANG..CMD_LANG + 2).contains(&c) => self.set_language(Lang::ALL[(c - CMD_LANG) as usize]),
            c if (CMD_INPUT..CMD_OUTPUT).contains(&c) => {
                if let Some(name) = self.menu_inputs.get((c - CMD_INPUT) as usize).cloned() {
                    self.set_device(Kind::Input, name);
                }
            }
            c if (CMD_OUTPUT..CMD_OUTPUT + 100).contains(&c) => {
                if let Some(name) = self.menu_outputs.get((c - CMD_OUTPUT) as usize).cloned() {
                    self.set_device(Kind::Output, name);
                }
            }
            _ => {}
        }
        false
    }

    fn tick(&mut self) {
        self.engine.watchdog();
        self.refresh_tray();
        // Safety net: if the settings window died while the App was busy,
        // its WM_DESTROY could not clear it.
        if let Some(s) = &self.settings {
            if unsafe { IsWindow(s.hwnd) } == 0 {
                self.settings = None;
            }
        }
    }
}

pub fn preset_label(p: Preset) -> S {
    match p {
        Preset::Light => S::PresetLight,
        Preset::Balanced => S::PresetBalanced,
        Preset::Aggressive => S::PresetAggressive,
    }
}

fn short(name: &str, limit: usize) -> String {
    if name.is_empty() {
        return "-".into();
    }
    if name.chars().count() <= limit {
        return name.into();
    }
    let mut s: String = name.chars().take(limit - 1).collect();
    s.push('…');
    s
}

unsafe fn append(menu: HMENU, flags: u32, id: u32, text: &str) {
    // A single & would underline the next letter; device names can contain one.
    let text = wide(&text.replace('&', "&&"));
    AppendMenuW(menu, MF_STRING | flags, id as usize, if flags & MF_SEPARATOR != 0 { std::ptr::null() } else { text.as_ptr() });
}

unsafe fn append_popup(menu: HMENU, sub: HMENU, text: &str) {
    let text = wide(text);
    AppendMenuW(menu, MF_POPUP, sub as usize, text.as_ptr());
}

/// A device submenu. Trims `names` to what the menu shows, so command ids
/// index straight into it.
unsafe fn device_menu(names: &mut Vec<String>, current: Option<&str>, base: u32) -> HMENU {
    let menu = CreatePopupMenu();
    if names.is_empty() {
        append(menu, MF_GRAYED, 0, t(S::TrayNoDevices));
        return menu;
    }
    let total = names.len();
    if total > MAX_DEVICES_IN_MENU {
        // Keep the current device visible even when it sorts past the cut.
        if let Some(pos) = current.and_then(|c| names.iter().position(|n| n == c)) {
            if pos >= MAX_DEVICES_IN_MENU {
                let cur = names.remove(pos);
                names.insert(0, cur);
            }
        }
        names.truncate(MAX_DEVICES_IN_MENU);
    }
    for (i, name) in names.iter().enumerate() {
        append(menu, 0, base + i as u32, &short(name, 44));
    }
    if let Some(pos) = current.and_then(|c| names.iter().position(|n| n == c)) {
        let last = base + names.len() as u32 - 1;
        CheckMenuRadioItem(menu, base, last, base + pos as u32, MF_BYCOMMAND);
    }
    if total > names.len() {
        append(menu, MF_SEPARATOR, 0, "");
        append(menu, 0, CMD_MORE, t(S::TrayMoreInSettings));
    }
    menu
}

fn show_menu(hwnd: HWND, x: i32, y: i32) {
    let Some(menu) = with_app(|app| app.build_menu()) else { return };
    // Released before the modal menu loop starts.
    let cmd = unsafe {
        SetForegroundWindow(hwnd); // otherwise the menu will not close on an outside click
        let flags = TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY;
        let cmd = TrackPopupMenu(menu, flags, x, y, 0, hwnd, std::ptr::null());
        PostMessageW(hwnd, WM_NULL, 0, 0);
        DestroyMenu(menu);
        cmd as u32
    };
    if cmd != 0 && with_app(|app| app.handle_command(cmd)) == Some(true) {
        quit(hwnd);
    }
}

fn quit(hwnd: HWND) {
    log_info!("Quit requested");
    // Destroying the hidden window destroys the settings window it owns first.
    unsafe { DestroyWindow(hwnd) };
}

unsafe extern "system" fn main_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_TRAY => {
            let event = (lparam & 0xFFFF) as u32;
            match event {
                WM_CONTEXTMENU => {
                    let (x, y) = ((wparam & 0xFFFF) as i16 as i32, ((wparam >> 16) & 0xFFFF) as i16 as i32);
                    show_menu(hwnd, x, y);
                }
                // NIN_SELECT / NIN_KEYSELECT: a click or Enter on the icon.
                e if e == WM_USER || e == WM_USER + 1 => {
                    with_app(|app| app.open_settings());
                }
                _ => {}
            }
            0
        }
        WM_TIMER if wparam == TIMER_TICK => {
            with_app(|app| app.tick());
            0
        }
        WM_WAKE => {
            with_app(|app| {
                app.engine.watchdog();
                app.refresh_tray();
            });
            0
        }
        WM_STARTUP => {
            with_app(|app| app.startup());
            trim_working_set();
            0
        }
        WM_REBUILD_SETTINGS => {
            settings::rebuild();
            0
        }
        WM_DESTROY => {
            with_app(|app| app.shutdown());
            PostQuitMessage(0);
            0
        }
        m if m != 0 && m == TASKBAR_CREATED.with(Cell::get) => {
            // Explorer restarted and took the tray with it.
            with_app(|app| {
                app.tray.add();
                app.refresh_tray();
            });
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

fn single_instance() -> bool {
    let name = wide(&format!("Global\\{APP_NAME}.singleton"));
    // Held for the lifetime of the process; Windows releases it on exit.
    let handle = unsafe { CreateMutexW(std::ptr::null(), 0, name.as_ptr()) };
    !handle.is_null() && unsafe { GetLastError() } != ERROR_ALREADY_EXISTS
}

pub fn main() -> i32 {
    let from_startup = std::env::args().any(|a| a == "--startup");
    let first_run = !config::config_path().exists();
    let log_path = config::config_dir().join("app.log");
    crate::log::init(log_path.clone());
    log_info!("Starting {APP_NAME} {VERSION} (startup={from_startup}, first_run={first_run})");

    let mut cfg = config::load();
    // Before anything can show a message box, so even startup errors are in
    // the user's language.
    let lang = i18n::set_language(cfg.language.as_deref());
    log_info!("UI language: {}", lang.code());

    if !single_instance() {
        log_info!("Another instance is already running, exiting");
        win::message(std::ptr::null_mut(), &tf(S::AppAlreadyRunning, &[("app", APP_TITLE)]), win::MessageKind::Info);
        return 0;
    }

    unsafe {
        CoInitializeEx(std::ptr::null(), COINIT_APARTMENTTHREADED as u32);
        let icc = INITCOMMONCONTROLSEX {
            dwSize: std::mem::size_of::<INITCOMMONCONTROLSEX>() as u32,
            dwICC: ICC_BAR_CLASSES | ICC_TAB_CLASSES | ICC_STANDARD_CLASSES | ICC_WIN95_CLASSES,
        };
        InitCommonControlsEx(&icc);
    }

    if devices::suggest_defaults(&mut cfg) {
        config::save(&cfg);
    }

    let hwnd = match create_main_window() {
        Some(h) => h,
        None => {
            let err = std::io::Error::last_os_error().to_string();
            log_warn!("Could not create the main window: {err}");
            let text = tf(
                S::AppStartFailed,
                &[("app", APP_TITLE), ("error", &err), ("log", &log_path.display().to_string())],
            );
            win::message(std::ptr::null_mut(), &text, win::MessageKind::Warning);
            return 1;
        }
    };

    let wake_hwnd = hwnd as usize;
    let wake: Arc<dyn Fn() + Send + Sync> = Arc::new(move || unsafe {
        PostMessageW(wake_hwnd as HWND, WM_WAKE, 0, 0);
    });
    let engine = Engine::new(&cfg, wake);
    let tray = Tray::new(hwnd);
    APP.with(|cell| {
        *cell.borrow_mut() = Some(App {
            cfg,
            engine,
            hwnd,
            settings: None,
            tray,
            menu_inputs: Vec::new(),
            menu_outputs: Vec::new(),
            first_run,
            from_startup,
        })
    });

    unsafe {
        SetTimer(hwnd, TIMER_TICK, 2000, None);
        PostMessageW(hwnd, WM_STARTUP, 0, 0);

        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            let dlg = settings::current_hwnd();
            if !dlg.is_null() && IsDialogMessageW(dlg, &msg) != 0 {
                continue; // Tab / arrow-key navigation in the settings window
            }
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
    APP.with(|cell| cell.borrow_mut().take()); // drops the engine and the tray icon
    0
}

fn create_main_window() -> Option<HWND> {
    unsafe {
        let hinst = GetModuleHandleW(std::ptr::null());
        let class = wide("MicNoiseCancelMain");
        let wc = WNDCLASSW {
            lpfnWndProc: Some(main_proc),
            hInstance: hinst,
            lpszClassName: class.as_ptr(),
            ..std::mem::zeroed()
        };
        RegisterClassW(&wc);
        TASKBAR_CREATED.with(|c| c.set(RegisterWindowMessageW(wide("TaskbarCreated").as_ptr())));
        let title = wide(APP_TITLE);
        // A real (hidden) top-level window rather than a message-only one:
        // only those receive the TaskbarCreated broadcast.
        let hwnd = CreateWindowExW(
            0,
            class.as_ptr(),
            title.as_ptr(),
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            hinst,
            std::ptr::null(),
        );
        (!hwnd.is_null()).then_some(hwnd)
    }
}
