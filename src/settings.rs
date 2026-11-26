//! Settings window, built from plain Win32 controls.
//!
//! The window exists only while it is open: closing it destroys every control
//! and frees its memory, and opening it again builds a fresh one. That is
//! also how a language switch works - every label is set at build time.

use std::cell::Cell;

use windows_sys::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM};
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::Controls::*;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::EnableWindow;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

use crate::app::{neural_label, preset_label, trim_working_set, with_app, App};
use crate::config::{self, Config, Preset, APP_TITLE, VERSION};
use crate::devices::{self, Kind};
use crate::i18n::{self, t, tf, Lang, S};
use crate::neural::NeuralModel;
use crate::win::{self, wide, MessageKind};
use crate::{autostart, shortcut};

const VB_CABLE_URL: &str = "https://vb-audio.com/Cable/";
const METER_FLOOR_DB: f32 = -60.0;

const TIMER_METERS: usize = 1;
const TIMER_APPLY: usize = 2;

// Control ids.
const ID_MUTED: i32 = 900; // hint text in grey; several statics share it
const ID_STATUS: i32 = 901;
const ID_CABLE_HINT: i32 = 902;
const ID_RESCAN: i32 = 1001;
const ID_CABLE: i32 = 1002;
const ID_SHORTCUT: i32 = 1003;
const ID_OPEN_LOGS: i32 = 1004;
const ID_RESET: i32 = 1005;
const ID_RESTART: i32 = 1006;
const ID_CLOSE: i32 = 1007;
const ID_LANG: i32 = 1101;
const ID_IN: i32 = 1102;
const ID_OUT: i32 = 1103;
const ID_MON: i32 = 1104;
const ID_NEURAL: i32 = 1105;
const ID_MONITOR: i32 = 1201;
const ID_GATE: i32 = 1202;
const ID_AUTOSTART: i32 = 1204;
const ID_PRESET: i32 = 1301;
const ID_TAB: i32 = 1401;

// Static-control styles and one trackbar message that windows-sys files
// elsewhere or leaves out.
const SS_LEFT: u32 = 0x0;
const SS_RIGHT: u32 = 0x2;
const SS_NOTIFY: u32 = 0x100;
const TBM_GETPOS: u32 = WM_USER;

const ALWAYS: u8 = 255;
const GENERAL: u8 = 0;
const ADVANCED: u8 = 1;

const fn rgb(r: u8, g: u8, b: u8) -> COLORREF {
    r as u32 | (g as u32) << 8 | (b as u32) << 16
}
const COL_OK: COLORREF = rgb(0x1f, 0x9d, 0x5f);
const COL_WARN: COLORREF = rgb(0xc2, 0x76, 0x10);
const COL_ERR: COLORREF = rgb(0xd0, 0x3a, 0x4b);
const COL_MUTED: COLORREF = rgb(0x6b, 0x72, 0x80);
const COL_METER_BG: COLORREF = rgb(0xe3, 0xe6, 0xea);
const COL_METER_OK: COLORREF = rgb(0x2e, 0xc4, 0x7b);
const COL_METER_IDLE: COLORREF = rgb(0x9a, 0xa3, 0xaf);
const COL_METER_HOT: COLORREF = rgb(0xe0, 0x4f, 0x5f);

thread_local! {
    // Read while painting, which can happen while the App is borrowed, so
    // these live outside it.
    static HWND_NOW: Cell<HWND> = const { Cell::new(std::ptr::null_mut()) };
    static STATUS_COLOR: Cell<COLORREF> = const { Cell::new(COL_MUTED) };
    static CABLE_COLOR: Cell<COLORREF> = const { Cell::new(COL_WARN) };
}

pub fn current_hwnd() -> HWND {
    HWND_NOW.with(Cell::get)
}

// ------------------------------------------------------------------ sliders

struct SliderDef {
    label: S,
    help: S,
    lo: f32,
    hi: f32,
    unit: &'static str,
    integer: bool,
    page: u8,
    get: fn(&Config) -> f32,
    set: fn(&mut Config, f32),
}

macro_rules! slider {
    ($field:ident, $label:ident, $help:ident, $lo:expr, $hi:expr, $unit:expr, $int:expr, $page:expr) => {
        SliderDef {
            label: S::$label,
            help: S::$help,
            lo: $lo,
            hi: $hi,
            unit: $unit,
            integer: $int,
            page: $page,
            get: |c| c.$field,
            set: |c, v| c.$field = v,
        }
    };
}

const SLIDERS: [SliderDef; 11] = [
    slider!(monitor_gain_db, SettingsMonitorVolume, HelpMonitorGain, -40.0, 6.0, "dB", false, GENERAL),
    slider!(reduction_db, SettingsReduction, HelpReduction, 6.0, 45.0, "dB", false, ADVANCED),
    slider!(highpass_hz, SettingsHighpass, HelpHighpass, 20.0, 200.0, "Hz", true, ADVANCED),
    slider!(input_gain_db, SettingsInputGain, HelpInputGain, -20.0, 20.0, "dB", false, ADVANCED),
    slider!(makeup_gain_db, SettingsOutputGain, HelpMakeupGain, -12.0, 18.0, "dB", false, ADVANCED),
    slider!(gate_floor_db, SettingsGateFloor, HelpGateFloor, -80.0, -6.0, "dB", false, ADVANCED),
    slider!(vad_threshold_db, SettingsVadThreshold, HelpVadThreshold, 0.3, 5.0, "dB", false, ADVANCED),
    slider!(vad_hangover_ms, SettingsHangover, HelpHangover, 50.0, 700.0, "ms", true, ADVANCED),
    slider!(gate_attack_ms, SettingsAttack, HelpAttack, 1.0, 60.0, "ms", true, ADVANCED),
    slider!(gate_release_ms, SettingsRelease, HelpRelease, 30.0, 800.0, "ms", true, ADVANCED),
    slider!(deepfilter_limit_db, SettingsDeepFilterLimit, HelpDeepFilterLimit, 6.0, 100.0, "dB", true, ADVANCED),
];
const SLIDER_DEEPFILTER: usize = 10;

impl SliderDef {
    fn scale(&self) -> f32 {
        if self.integer {
            1.0
        } else {
            10.0
        }
    }
    fn to_pos(&self, v: f32) -> isize {
        (v.clamp(self.lo, self.hi) * self.scale()).round() as isize
    }
    fn value_at(&self, pos: isize) -> f32 {
        ((pos as f32 / self.scale()) * 100.0).round() / 100.0
    }
    fn format(&self, v: f32) -> String {
        if self.integer {
            format!("{} {}", v.round() as i32, self.unit)
        } else {
            format!("{:.1} {}", v, self.unit)
        }
    }
}

struct Slider {
    def: &'static SliderDef,
    track: HWND,
    value: HWND,
}

// ------------------------------------------------------------------- window

pub struct SettingsWindow {
    pub hwnd: HWND,
    font: HFONT,
    bold: HFONT,
    tooltip: HWND,
    groups: Vec<HWND>,
    apply_pending: bool,
    scale: f32,
    tab: HWND,
    // One child window per tab, holding that tab's controls. Each paints its
    // own background, so switching tabs never leaves the other tab's pixels
    // behind (inside a group box nothing else would repaint them).
    page_windows: [HWND; 2],
    page_origin: [(i32, i32); 2], // in pixels, relative to the window
    status: HWND,
    status_hint: HWND,
    status_text: (String, String),
    in_combo: HWND,
    out_combo: HWND,
    mon_combo: HWND,
    in_list: Vec<String>,
    out_list: Vec<String>,
    cable_hint: HWND,
    cable_button: HWND,
    cable_missing: bool,
    presets: [HWND; 3],
    meter_in: HWND,
    meter_out: HWND,
    speech: HWND,
    speech_state: Option<bool>,
    monitor_check: HWND,
    gate_check: HWND,
    neural_combo: HWND,
    deepfilter_track: HWND,
    autostart_check: HWND,
    shortcut_button: HWND,
    sliders: Vec<Slider>,
}

impl Drop for SettingsWindow {
    fn drop(&mut self) {
        HWND_NOW.with(|c| c.set(std::ptr::null_mut()));
    }
}

/// Layout helper: positions are in 96-DPI pixels and scaled on creation.
struct Builder<'a> {
    w: &'a mut SettingsWindow,
    page: u8,
}

impl Builder<'_> {
    fn s(&self, v: i32) -> i32 {
        (v as f32 * self.w.scale).round() as i32
    }

    /// Create a control. Coordinates are relative to the window; controls on
    /// a tab are placed on that tab's page window, offset to match.
    #[allow(clippy::too_many_arguments)]
    fn make(&mut self, class: &str, text: &str, style: u32, x: i32, y: i32, w: i32, h: i32, id: i32, tip: Option<S>) -> HWND {
        let (class_w, text_w) = (wide(class), wide(text));
        let (parent, (ox, oy)) = match self.page {
            ALWAYS => (self.w.hwnd, (0, 0)),
            p => (self.w.page_windows[p as usize], self.w.page_origin[p as usize]),
        };
        let hwnd = unsafe {
            CreateWindowExW(
                0,
                class_w.as_ptr(),
                text_w.as_ptr(),
                WS_CHILD | WS_VISIBLE | style,
                self.s(x) - ox,
                self.s(y) - oy,
                self.s(w),
                self.s(h),
                parent,
                id as isize as HMENU,
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            )
        };
        unsafe { SendMessageW(hwnd, WM_SETFONT, self.w.font as usize, 0) };
        if let Some(tip) = tip {
            self.tip(hwnd, t(tip));
        }
        hwnd
    }

    fn tip(&mut self, ctl: HWND, text: &str) {
        let mut text = wide(text);
        let mut ti: TTTOOLINFOW = unsafe { std::mem::zeroed() };
        ti.cbSize = std::mem::size_of::<TTTOOLINFOW>() as u32;
        ti.uFlags = TTF_IDISHWND | TTF_SUBCLASS;
        ti.hwnd = unsafe { GetParent(ctl) };
        ti.uId = ctl as usize;
        ti.lpszText = text.as_mut_ptr(); // copied by the tooltip control
        unsafe { SendMessageW(self.w.tooltip, TTM_ADDTOOLW, 0, &ti as *const _ as LPARAM) };
    }

    fn label(&mut self, text: &str, x: i32, y: i32, w: i32, h: i32, tip: Option<S>) -> HWND {
        self.make("STATIC", text, SS_NOTIFY | SS_LEFT, x, y, w, h, 0, tip)
    }

    fn hint(&mut self, text: &str, x: i32, y: i32, w: i32, h: i32) -> HWND {
        self.make("STATIC", text, SS_LEFT | WS_GROUP, x, y, w, h, ID_MUTED, None)
    }

    fn group(&mut self, text: &str, x: i32, y: i32, w: i32, h: i32) {
        let g = self.make("BUTTON", text, WS_CLIPSIBLINGS | BS_GROUPBOX as u32, x, y, w, h, 0, None);
        self.w.groups.push(g);
    }

    fn button(&mut self, text: &str, x: i32, y: i32, w: i32, id: i32, tip: S) -> HWND {
        self.make("BUTTON", text, WS_TABSTOP | BS_PUSHBUTTON as u32, x, y, w, 25, id, Some(tip))
    }

    fn check(&mut self, text: &str, x: i32, y: i32, w: i32, id: i32, tip: S) -> HWND {
        self.make("BUTTON", text, WS_TABSTOP | BS_AUTOCHECKBOX as u32, x, y, w, 20, id, Some(tip))
    }

    fn combo(&mut self, x: i32, y: i32, w: i32, id: i32, tip: S) -> HWND {
        let style = WS_TABSTOP | WS_VSCROLL | CBS_DROPDOWNLIST as u32 | CBS_HASSTRINGS as u32;
        self.make("COMBOBOX", "", style, x, y, w, 300, id, Some(tip))
    }

    fn meter(&mut self, x: i32, y: i32, w: i32, tip: S) -> HWND {
        self.make(METER_CLASS, "", SS_NOTIFY, x, y, w, 14, 0, Some(tip))
    }

    /// Name, track and readout on one row; the help hangs off all three.
    fn slider(&mut self, def: &'static SliderDef, x: i32, y: i32, w: i32) {
        self.page = def.page;
        self.label(t(def.label), x, y + 3, 150, 18, Some(def.help));
        let style = WS_TABSTOP | TBS_HORZ | TBS_NOTICKS;
        let track = self.make("msctls_trackbar32", "", style, x + 156, y, w - 156 - 72, 26, 0, Some(def.help));
        let value = self.make("STATIC", "", SS_RIGHT | SS_NOTIFY, x + w - 70, y + 3, 70, 18, ID_MUTED, Some(def.help));
        unsafe {
            SendMessageW(track, TBM_SETRANGEMIN, 0, def.to_pos(def.lo));
            SendMessageW(track, TBM_SETRANGEMAX, 1, def.to_pos(def.hi));
            SendMessageW(track, TBM_SETLINESIZE, 0, 1);
            SendMessageW(track, TBM_SETPAGESIZE, 0, ((def.hi - def.lo) * def.scale() / 10.0).max(1.0) as isize);
        }
        self.w.sliders.push(Slider { def, track, value });
    }
}

// --------------------------------------------------------------- level meter

const METER_CLASS: &str = "MicNoiseCancelMeter";

/// Pack a level and a colour into the meter's user data.
fn meter_value(level_db: f32, active: bool) -> isize {
    let fraction = ((level_db - METER_FLOOR_DB) / -METER_FLOOR_DB).clamp(0.0, 1.0);
    let colour = if level_db > -1.5 { 2 } else if active { 1 } else { 0 };
    (fraction * 1000.0) as isize | colour << 16
}

fn set_meter(hwnd: HWND, level_db: f32, active: bool) {
    let v = meter_value(level_db, active);
    unsafe {
        if GetWindowLongPtrW(hwnd, GWLP_USERDATA) != v {
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, v);
            InvalidateRect(hwnd, std::ptr::null(), 0);
        }
    }
}

unsafe extern "system" fn meter_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_ERASEBKGND => 1,
        WM_PAINT => {
            let mut ps: PAINTSTRUCT = std::mem::zeroed();
            let hdc = BeginPaint(hwnd, &mut ps);
            let mut rc: RECT = std::mem::zeroed();
            GetClientRect(hwnd, &mut rc);
            let v = GetWindowLongPtrW(hwnd, GWLP_USERDATA);
            let fraction = (v & 0xFFFF) as f32 / 1000.0;
            let colour = match v >> 16 {
                2 => COL_METER_HOT,
                1 => COL_METER_OK,
                _ => COL_METER_IDLE,
            };
            let split = rc.left + ((rc.right - rc.left) as f32 * fraction) as i32;
            let bar = RECT { right: split, ..rc };
            let rest = RECT { left: split, ..rc };
            let (b1, b2) = (CreateSolidBrush(colour), CreateSolidBrush(COL_METER_BG));
            FillRect(hdc, &bar, b1);
            FillRect(hdc, &rest, b2);
            DeleteObject(b1);
            DeleteObject(b2);
            EndPaint(hwnd, &ps);
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

// ------------------------------------------------------------- construction

fn register_classes() {
    use std::sync::Once;
    static ONCE: Once = Once::new();
    ONCE.call_once(|| unsafe {
        let hinst = GetModuleHandleW(std::ptr::null());
        let class = wide(SETTINGS_CLASS);
        let wc = WNDCLASSW {
            lpfnWndProc: Some(settings_proc),
            hInstance: hinst,
            hIcon: LoadIconW(hinst, 1 as _),
            hCursor: LoadCursorW(std::ptr::null_mut(), IDC_ARROW),
            hbrBackground: GetSysColorBrush(COLOR_WINDOW),
            lpszClassName: class.as_ptr(),
            ..std::mem::zeroed()
        };
        RegisterClassW(&wc);
        let page = wide(PAGE_CLASS);
        let wc = WNDCLASSW {
            lpfnWndProc: Some(page_proc),
            hInstance: hinst,
            hCursor: LoadCursorW(std::ptr::null_mut(), IDC_ARROW),
            hbrBackground: GetSysColorBrush(COLOR_WINDOW),
            lpszClassName: page.as_ptr(),
            ..std::mem::zeroed()
        };
        RegisterClassW(&wc);
        let meter = wide(METER_CLASS);
        let wc = WNDCLASSW {
            lpfnWndProc: Some(meter_proc),
            hInstance: hinst,
            hCursor: LoadCursorW(std::ptr::null_mut(), IDC_ARROW),
            lpszClassName: meter.as_ptr(),
            ..std::mem::zeroed()
        };
        RegisterClassW(&wc);
    });
}

const SETTINGS_CLASS: &str = "MicNoiseCancelSettings";
const PAGE_CLASS: &str = "MicNoiseCancelPage";

/// A tab page: paints its background and hands everything its controls
/// report on to the settings window, which handles all of it in one place.
unsafe extern "system" fn page_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_COMMAND | WM_HSCROLL | WM_NOTIFY | WM_CTLCOLORSTATIC | WM_CTLCOLORBTN => {
            SendMessageW(GetParent(hwnd), msg, wparam, lparam)
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

/// The window's two fonts. Made once and kept for the life of the process:
/// two small GDI objects, and no risk of freeing one a control still uses.
fn ui_fonts(scale: f32) -> (HFONT, HFONT) {
    thread_local! {
        static FONTS: Cell<(HFONT, HFONT)> = const { Cell::new((std::ptr::null_mut(), std::ptr::null_mut())) };
    }
    let cached = FONTS.with(Cell::get);
    if !cached.0.is_null() {
        return cached;
    }
    let fonts = make_fonts(scale);
    FONTS.with(|c| c.set(fonts));
    fonts
}

fn make_fonts(scale: f32) -> (HFONT, HFONT) {
    unsafe {
        let mut ncm: NONCLIENTMETRICSW = std::mem::zeroed();
        ncm.cbSize = std::mem::size_of::<NONCLIENTMETRICSW>() as u32;
        SystemParametersInfoW(SPI_GETNONCLIENTMETRICS, ncm.cbSize, &mut ncm as *mut _ as *mut _, 0);
        let mut lf = ncm.lfMessageFont;
        if lf.lfHeight == 0 {
            lf.lfHeight = -(12.0 * scale) as i32;
        }
        let font = CreateFontIndirectW(&lf);
        lf.lfWeight = FW_BOLD as i32;
        lf.lfHeight = (lf.lfHeight as f32 * 1.15) as i32;
        let bold = CreateFontIndirectW(&lf);
        (font, bold)
    }
}

fn dpi_scale() -> f32 {
    unsafe {
        let hdc = GetDC(std::ptr::null_mut());
        let dpi = GetDeviceCaps(hdc, LOGPIXELSY as _);
        ReleaseDC(std::ptr::null_mut(), hdc);
        if dpi > 0 {
            dpi as f32 / 96.0
        } else {
            1.0
        }
    }
}

const CLIENT_W: i32 = 560;
const CLIENT_H: i32 = 630;

impl App {
    pub fn open_settings(&mut self) {
        self.open_settings_on(0);
    }

    fn open_settings_on(&mut self, tab: usize) {
        if let Some(s) = &self.settings {
            let hwnd = s.hwnd;
            unsafe {
                ShowWindow(hwnd, SW_RESTORE);
                SetForegroundWindow(hwnd);
            }
            self.reload_devices();
            self.load_values();
            return;
        }
        register_classes();
        let scale = dpi_scale();
        let (font, bold) = ui_fonts(scale);

        let style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_CLIPCHILDREN;
        let mut rc = RECT { left: 0, top: 0, right: (CLIENT_W as f32 * scale) as i32, bottom: (CLIENT_H as f32 * scale) as i32 };
        let (w, h, x, y) = unsafe {
            AdjustWindowRectEx(&mut rc, style, 0, WS_EX_APPWINDOW);
            let (w, h) = (rc.right - rc.left, rc.bottom - rc.top);
            let mut work: RECT = std::mem::zeroed();
            SystemParametersInfoW(SPI_GETWORKAREA, 0, &mut work as *mut _ as *mut _, 0);
            let x = work.left + ((work.right - work.left) - w).max(0) / 2;
            let y = work.top + ((work.bottom - work.top) - h).max(0) / 3;
            (w, h, x, y)
        };

        let class = wide(SETTINGS_CLASS);
        let title = wide(&tf(S::SettingsTitle, &[("app", APP_TITLE)]));
        let hwnd = unsafe {
            CreateWindowExW(
                WS_EX_APPWINDOW,
                class.as_ptr(),
                title.as_ptr(),
                style,
                x,
                y,
                w,
                h,
                self.hwnd, // owned by the hidden main window: destroyed with it
                std::ptr::null_mut(),
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            )
        };
        if hwnd.is_null() {
            crate::log_warn!("Could not create the settings window");
            return;
        }
        let tooltip = unsafe {
            let tip = CreateWindowExW(
                WS_EX_TOPMOST,
                wide("tooltips_class32").as_ptr(),
                std::ptr::null(),
                WS_POPUP | TTS_ALWAYSTIP | TTS_NOPREFIX,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                hwnd,
                std::ptr::null_mut(),
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            );
            SendMessageW(tip, TTM_SETMAXTIPWIDTH, 0, (340.0 * scale) as isize);
            SendMessageW(tip, TTM_SETDELAYTIME, TTDT_INITIAL as usize, 450);
            SendMessageW(tip, TTM_SETDELAYTIME, TTDT_AUTOPOP as usize, 30000);
            tip
        };

        let mut win = SettingsWindow {
            hwnd,
            font,
            bold,
            tooltip,
            groups: Vec::new(),
            apply_pending: false,
            scale,
            tab: std::ptr::null_mut(),
            page_windows: [std::ptr::null_mut(); 2],
            page_origin: [(0, 0); 2],
            status: std::ptr::null_mut(),
            status_hint: std::ptr::null_mut(),
            status_text: Default::default(),
            in_combo: std::ptr::null_mut(),
            out_combo: std::ptr::null_mut(),
            mon_combo: std::ptr::null_mut(),
            in_list: Vec::new(),
            out_list: Vec::new(),
            cable_hint: std::ptr::null_mut(),
            cable_button: std::ptr::null_mut(),
            cable_missing: false,
            presets: [std::ptr::null_mut(); 3],
            meter_in: std::ptr::null_mut(),
            meter_out: std::ptr::null_mut(),
            speech: std::ptr::null_mut(),
            speech_state: None,
            monitor_check: std::ptr::null_mut(),
            gate_check: std::ptr::null_mut(),
            neural_combo: std::ptr::null_mut(),
            deepfilter_track: std::ptr::null_mut(),
            autostart_check: std::ptr::null_mut(),
            shortcut_button: std::ptr::null_mut(),
            sliders: Vec::new(),
        };
        build_controls(&mut win);
        HWND_NOW.with(|c| c.set(hwnd));
        self.settings = Some(win);

        self.reload_devices();
        self.load_values();
        self.show_tab(tab);
        self.tick_meters();
        unsafe {
            SetTimer(hwnd, TIMER_METERS, 60, None);
            ShowWindow(hwnd, SW_SHOW);
            SetForegroundWindow(hwnd);
        }
    }

    fn show_tab(&mut self, tab: usize) {
        let Some(s) = &self.settings else { return };
        unsafe {
            SendMessageW(s.tab, TCM_SETCURSEL, tab, 0);
            // Show the new page before hiding the old one, so the area is
            // never briefly empty.
            ShowWindow(s.page_windows[tab.min(1)], SW_SHOW);
            ShowWindow(s.page_windows[1 - tab.min(1)], SW_HIDE);
        }
    }

    fn current_tab(&self) -> usize {
        self.settings.as_ref().map(|s| unsafe { SendMessageW(s.tab, TCM_GETCURSEL, 0, 0) }.max(0) as usize).unwrap_or(0)
    }

    // ------------------------------------------------------------- values

    fn reload_devices(&mut self) {
        let Some(s) = &mut self.settings else { return };
        s.in_list = devices::names(Kind::Input);
        s.out_list = devices::names(Kind::Output);
        fill_combo(s.in_combo, &s.in_list);
        fill_combo(s.out_combo, &s.out_list);
        fill_combo(s.mon_combo, &s.out_list);
    }

    /// Push the config into the controls. Setting a control's state by
    /// message sends no notification, so nothing here re-triggers an apply.
    pub(crate) fn load_values(&mut self) {
        let cfg = self.cfg.clone();
        let Some(s) = &mut self.settings else { return };
        select_combo(s.in_combo, &s.in_list, cfg.input_device.as_deref());
        select_combo(s.out_combo, &s.out_list, cfg.output_device.as_deref());
        select_combo(s.mon_combo, &s.out_list, cfg.monitor_device.as_deref());
        set_check(s.monitor_check, cfg.monitor_enabled);
        set_check(s.gate_check, cfg.gate_enabled);
        let neural = NeuralModel::ALL.iter().position(|&m| m == cfg.neural_model()).unwrap_or(0);
        unsafe { SendMessageW(s.neural_combo, CB_SETCURSEL, neural, 0) };
        set_check(s.autostart_check, autostart::is_enabled());
        for slider in &s.sliders {
            let v = (slider.def.get)(&cfg);
            unsafe { SendMessageW(slider.track, TBM_SETPOS, 1, slider.def.to_pos(v)) };
            set_text(slider.value, &slider.def.format(v));
        }
        self.sync_presets();
        self.sync_neural();
        self.update_cable_hint();
        self.refresh_shortcut_button();
    }

    fn selected_neural(&self) -> Option<NeuralModel> {
        let s = self.settings.as_ref()?;
        let index = unsafe { SendMessageW(s.neural_combo, CB_GETCURSEL, 0, 0) };
        usize::try_from(index).ok().and_then(|i| NeuralModel::ALL.get(i).copied())
    }

    /// The DeepFilterNet limit only means something while it is selected.
    fn sync_neural(&mut self) {
        let on = self.selected_neural() == Some(NeuralModel::DeepFilterNet);
        let Some(s) = &self.settings else { return };
        unsafe { EnableWindow(s.deepfilter_track, on as i32) };
    }

    fn sync_presets(&mut self) {
        let preset = self.cfg.detect_preset();
        let Some(s) = &self.settings else { return };
        for (i, p) in Preset::ALL.into_iter().enumerate() {
            set_check(s.presets[i], preset == Some(p));
        }
    }

    /// The config as the controls currently show it.
    fn collect(&self) -> Config {
        let mut cfg = self.cfg.clone();
        let Some(s) = &self.settings else { return cfg };
        // An empty list says nothing about what the user wants; keep the
        // stored name so the device is picked up again when it returns.
        cfg.input_device = combo_value(s.in_combo, &s.in_list).or(cfg.input_device);
        cfg.output_device = combo_value(s.out_combo, &s.out_list).or(cfg.output_device);
        cfg.monitor_device = combo_value(s.mon_combo, &s.out_list).or(cfg.monitor_device);
        cfg.monitor_enabled = get_check(s.monitor_check);
        cfg.gate_enabled = get_check(s.gate_check);
        if let Some(model) = self.selected_neural() {
            cfg.set_neural_model(model);
        }
        for slider in &s.sliders {
            let pos = unsafe { SendMessageW(slider.track, TBM_GETPOS, 0, 0) };
            (slider.def.set)(&mut cfg, slider.def.value_at(pos));
        }
        cfg.preset = cfg.detect_preset().map(|p| p.id()).unwrap_or("custom").into();
        cfg
    }

    fn apply(&mut self) {
        if let Some(s) = &mut self.settings {
            s.apply_pending = false;
        }
        let cfg = self.collect();
        self.update_config(cfg);
        self.sync_presets();
        self.update_cable_hint();
    }

    fn schedule_apply(&mut self, delay_ms: u32) {
        if let Some(s) = &mut self.settings {
            s.apply_pending = true;
            unsafe { SetTimer(s.hwnd, TIMER_APPLY, delay_ms, None) }; // replaces a pending one
        }
    }

    fn update_cable_hint(&mut self) {
        let Some(s) = &mut self.settings else { return };
        let out = combo_value(s.out_combo, &s.out_list).unwrap_or_default();
        s.cable_missing = !devices::is_virtual_sink(&out);
        let (text, colour) = if s.cable_missing {
            (t(S::SettingsCableMissing).to_string(), COL_WARN)
        } else {
            let source = devices::find_virtual_source().unwrap_or_else(|| "CABLE Output".into());
            (tf(S::SettingsCableOk, &[("device", &source)]), COL_OK)
        };
        CABLE_COLOR.with(|c| c.set(colour));
        set_text(s.cable_hint, &text);
        unsafe { ShowWindow(s.cable_button, if s.cable_missing { SW_SHOW } else { SW_HIDE }) };
    }

    fn refresh_shortcut_button(&mut self) {
        let Some(s) = &self.settings else { return };
        let text = if shortcut::exists(shortcut::Where::Desktop) { S::SettingsShortcutUpdate } else { S::SettingsShortcutCreate };
        set_text(s.shortcut_button, t(text));
    }

    fn tick_meters(&mut self) {
        let status = self.engine.status();
        let Some(s) = &mut self.settings else { return };
        set_meter(s.meter_in, status.in_level_db, true);
        set_meter(s.meter_out, status.out_level_db, status.speech);

        let (head, colour, hint) = if let Some(err) = &status.error {
            (t(S::StatusError), COL_ERR, err.clone())
        } else if !status.running {
            (t(S::StatusStopped), COL_MUTED, t(S::SettingsStatusStopped).to_string())
        } else if status.bypassed {
            (t(S::StatusBypassed), COL_WARN, t(S::SettingsStatusBypassed).to_string())
        } else {
            let info = &status.info;
            let neural = match status.neural {
                NeuralModel::Off => String::new(),
                m => format!(", {m}"),
            };
            (t(S::StatusActive), COL_OK, format!("{}  ->  {}   (~{:.0} ms{neural})", info.input_name, info.output_name, info.latency_ms))
        };
        if s.status_text.0 != head || s.status_text.1 != hint || STATUS_COLOR.with(Cell::get) != colour {
            STATUS_COLOR.with(|c| c.set(colour));
            set_text(s.status, head);
            set_text(s.status_hint, &hint);
            s.status_text = (head.to_string(), hint);
        }
        if s.speech_state != Some(status.speech) {
            s.speech_state = Some(status.speech);
            set_text(s.speech, t(if status.speech { S::SettingsVoiceDetected } else { S::SettingsQuiet }));
        }
    }

    // ------------------------------------------------------------ actions

    fn on_preset(&mut self, index: usize) {
        let mut cfg = self.collect();
        cfg.apply_preset(Preset::ALL[index]);
        self.update_config(cfg);
        self.load_values();
    }

    fn reset_to_defaults(&mut self) {
        let mut cfg = Config::default();
        cfg.input_device = self.cfg.input_device.clone();
        cfg.output_device = self.cfg.output_device.clone();
        cfg.monitor_device = self.cfg.monitor_device.clone();
        cfg.language = self.cfg.language.clone(); // a reset is about audio, not the UI
        cfg.autostart = self.cfg.autostart;
        self.update_config(cfg);
        self.load_values();
    }

    /// Returns false if the registry refused.
    fn on_autostart(&mut self) -> bool {
        let Some(s) = &self.settings else { return true };
        let wanted = get_check(s.autostart_check);
        let ok = autostart::set_enabled(wanted);
        let now = autostart::is_enabled();
        set_check(s.autostart_check, now);
        let mut cfg = self.cfg.clone();
        cfg.autostart = now;
        self.update_config(cfg);
        ok
    }
}

fn build_controls(w: &mut SettingsWindow) {
    let mut b = Builder { w, page: ALWAYS };
    let (cw, cx) = (CLIENT_W, 16);

    // Header: status on the left, language on the right.
    let status = b.make("STATIC", "", SS_LEFT, cx, 10, 290, 24, ID_STATUS, None);
    unsafe { SendMessageW(status, WM_SETFONT, b.w.bold as usize, 0) };
    b.w.status = status;
    b.label(i18n::LANGUAGE_LABEL, cw - 16 - 110 - 118, 14, 112, 18, Some(S::HelpLanguage));
    let lang = b.combo(cw - 16 - 110, 10, 110, ID_LANG, S::HelpLanguage);
    for l in Lang::ALL {
        unsafe { SendMessageW(lang, CB_ADDSTRING, 0, wide(l.name()).as_ptr() as LPARAM) };
    }
    let current = Lang::ALL.iter().position(|&l| l == i18n::language()).unwrap_or(0);
    unsafe { SendMessageW(lang, CB_SETCURSEL, current, 0) };
    b.w.status_hint = b.hint("", cx, 36, cw - 32, 32);

    // Tabs.
    let tab_y = 72;
    let tab_bottom = CLIENT_H - 46;
    let tab_style = WS_TABSTOP | WS_CLIPSIBLINGS;
    let tab = b.make("SysTabControl32", "", tab_style, 10, tab_y, cw - 20, tab_bottom - tab_y, ID_TAB, None);
    for (i, label) in [S::SettingsTabGeneral, S::SettingsTabAdvanced].into_iter().enumerate() {
        let mut text = wide(&format!("  {}  ", t(label)));
        let mut item: TCITEMW = unsafe { std::mem::zeroed() };
        item.mask = TCIF_TEXT;
        item.pszText = text.as_mut_ptr();
        unsafe { SendMessageW(tab, TCM_INSERTITEMW, i, &item as *const _ as LPARAM) };
    }
    b.w.tab = tab;

    // The page windows fill the tab's display area exactly.
    unsafe {
        let mut rc: RECT = std::mem::zeroed();
        GetClientRect(tab, &mut rc);
        SendMessageW(tab, TCM_ADJUSTRECT, 0, &mut rc as *mut _ as LPARAM);
        let (tx, ty) = (b.s(10), b.s(tab_y));
        let (px, py) = (tx + rc.left, ty + rc.top);
        let class = wide(PAGE_CLASS);
        for i in 0..2 {
            b.w.page_windows[i] = CreateWindowExW(
                WS_EX_CONTROLPARENT, // Tab moves into its controls
                class.as_ptr(),
                std::ptr::null(),
                WS_CHILD | WS_CLIPSIBLINGS | if i == 0 { WS_VISIBLE } else { 0 },
                px,
                py,
                rc.right - rc.left,
                rc.bottom - rc.top,
                b.w.hwnd,
                std::ptr::null_mut(),
                GetModuleHandleW(std::ptr::null()),
                std::ptr::null(),
            );
            b.w.page_origin[i] = (px, py);
        }
    }

    let x0 = 24;
    let gw = cw - 48; // group width
    let ix = x0 + 12; // inside a group
    let iw = gw - 24;
    let gap = 6;
    let top = tab_y + 34;

    // ---------------------------------------------------------- General
    b.page = GENERAL;
    let mut y = top;

    b.group(t(S::SettingsDevices), x0, y, gw, 144);
    b.label(t(S::SettingsMicrophone), ix, y + 25, 150, 18, Some(S::HelpInputDevice));
    b.w.in_combo = b.combo(ix + 156, y + 21, iw - 156, ID_IN, S::HelpInputDevice);
    b.label(t(S::SettingsOutput), ix, y + 54, 150, 18, Some(S::HelpOutputDevice));
    b.w.out_combo = b.combo(ix + 156, y + 50, iw - 156, ID_OUT, S::HelpOutputDevice);
    b.w.cable_hint = b.make("STATIC", "", SS_LEFT, ix, y + 80, iw, 30, ID_CABLE_HINT, None);
    b.button(t(S::SettingsRescanDevices), ix, y + 112, 170, ID_RESCAN, S::HelpRescan);
    b.w.cable_button = b.button(t(S::SettingsDownloadCable), ix + 178, y + 112, 170, ID_CABLE, S::HelpCableDownload);
    y += 144 + gap;

    b.group(t(S::SettingsNoiseReduction), x0, y, gw, 66);
    let widths = [110, 130, 220];
    let helps = [S::HelpPresetLight, S::HelpPresetBalanced, S::HelpPresetAggressive];
    let mut rx = ix;
    for (i, p) in Preset::ALL.into_iter().enumerate() {
        let style = WS_TABSTOP | BS_AUTORADIOBUTTON as u32 | if i == 0 { WS_GROUP } else { 0 };
        let id = ID_PRESET + i as i32;
        b.w.presets[i] = b.make("BUTTON", t(preset_label(p)), style, rx, y + 20, widths[i], 20, id, Some(helps[i]));
        rx += widths[i] + 8;
    }
    b.hint(t(S::SettingsHoverPresets), ix, y + 43, iw, 18);
    y += 66 + gap;

    b.group(t(S::SettingsLevels), x0, y, gw, 80);
    b.label(t(S::SettingsMeterIn), ix, y + 19, 40, 18, Some(S::HelpMeterIn));
    b.w.meter_in = b.meter(ix + 48, y + 21, iw - 48, S::HelpMeterIn);
    b.label(t(S::SettingsMeterOut), ix, y + 38, 40, 18, Some(S::HelpMeterOut));
    b.w.meter_out = b.meter(ix + 48, y + 40, iw - 48, S::HelpMeterOut);
    b.w.speech = b.hint("", ix, y + 58, iw, 18);
    y += 80 + gap;

    b.group(t(S::SettingsMonitorAndStartup), x0, y, gw, 158);
    b.w.monitor_check = b.check(t(S::SettingsMonitorEnabled), ix, y + 20, iw, ID_MONITOR, S::HelpMonitorEnabled);
    b.label(t(S::SettingsHeadphones), ix, y + 48, 150, 18, Some(S::HelpMonitorDevice));
    b.w.mon_combo = b.combo(ix + 156, y + 44, iw - 156, ID_MON, S::HelpMonitorDevice);
    b.slider(&SLIDERS[0], ix, y + 72, iw);
    b.page = GENERAL;
    b.w.autostart_check = b.check(t(S::SettingsAutostart), ix, y + 102, iw, ID_AUTOSTART, S::HelpAutostart);
    b.w.shortcut_button = b.button(t(S::SettingsShortcutCreate), ix, y + 126, 240, ID_SHORTCUT, S::HelpShortcut);
    debug_assert!(y + 158 <= tab_bottom - 8);

    // --------------------------------------------------------- Advanced
    b.page = ADVANCED;
    let mut y = top;
    b.group(t(S::SettingsNoiseReduction), x0, y, gw, 132);
    for (i, def) in SLIDERS[1..5].iter().enumerate() {
        b.slider(def, ix, y + 20 + i as i32 * 27, iw);
    }
    y += 132 + gap;

    b.page = ADVANCED;
    b.group(t(S::SettingsGate), x0, y, gw, 214);
    b.w.gate_check = b.check(t(S::SettingsGateEnabled), ix, y + 20, iw, ID_GATE, S::HelpGateEnabled);
    for (i, def) in SLIDERS[5..SLIDER_DEEPFILTER].iter().enumerate() {
        b.slider(def, ix, y + 44 + i as i32 * 27, iw);
    }
    b.page = ADVANCED;
    b.hint(t(S::SettingsHoverSliders), ix, y + 180, iw, 30);
    y += 214 + gap;

    b.group(t(S::SettingsOther), x0, y, gw, 106);
    b.label(t(S::SettingsNeural), ix, y + 23, 150, 18, Some(S::HelpNeural));
    let neural = b.combo(ix + 156, y + 19, iw - 156, ID_NEURAL, S::HelpNeural);
    for m in NeuralModel::ALL {
        unsafe { SendMessageW(neural, CB_ADDSTRING, 0, wide(t(neural_label(m))).as_ptr() as LPARAM) };
    }
    b.w.neural_combo = neural;
    b.slider(&SLIDERS[SLIDER_DEEPFILTER], ix, y + 46, iw);
    b.w.deepfilter_track = b.w.sliders.last().map(|sl| sl.track).unwrap_or(std::ptr::null_mut());
    b.page = ADVANCED;
    b.button(t(S::SettingsOpenLogs), ix, y + 74, 150, ID_OPEN_LOGS, S::HelpOpenLogs);
    b.button(t(S::SettingsReset), ix + 158, y + 74, 150, ID_RESET, S::HelpReset);
    let version = format!("{APP_TITLE} {VERSION}");
    b.make("STATIC", &version, SS_RIGHT, ix + 316, y + 79, iw - 316, 18, ID_MUTED, None);
    debug_assert!(y + 106 <= tab_bottom - 8);

    // ------------------------------------------------------------ Bottom
    b.page = ALWAYS;
    let by = CLIENT_H - 36;
    b.button(t(S::ActionRestartAudio), cw - 16 - 110 - 8 - 170, by, 170, ID_RESTART, S::HelpRestartAudio);
    b.button(t(S::ActionClose), cw - 16 - 110, by, 110, ID_CLOSE, S::HelpClose);

    // Children created first sit on top of the z-order. Push the frames -
    // group boxes within their page, and the tab control under the pages -
    // to the bottom, so with WS_CLIPSIBLINGS they never paint over what they
    // frame. The pages themselves do not clip their children: they erase
    // their whole area, group-box interiors included, before the controls
    // draw on top.
    let flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE;
    for &g in &b.w.groups {
        unsafe { SetWindowPos(g, HWND_BOTTOM, 0, 0, 0, 0, flags) };
    }
    unsafe { SetWindowPos(tab, HWND_BOTTOM, 0, 0, 0, 0, flags) };
}

// --------------------------------------------------------- control helpers

fn set_text(hwnd: HWND, text: &str) {
    let w = wide(text);
    unsafe { SetWindowTextW(hwnd, w.as_ptr()) };
}

fn set_check(hwnd: HWND, on: bool) {
    unsafe { SendMessageW(hwnd, BM_SETCHECK, if on { BST_CHECKED as usize } else { BST_UNCHECKED as usize }, 0) };
}

fn get_check(hwnd: HWND) -> bool {
    unsafe { SendMessageW(hwnd, BM_GETCHECK, 0, 0) == BST_CHECKED as isize }
}

fn fill_combo(combo: HWND, items: &[String]) {
    unsafe {
        SendMessageW(combo, CB_RESETCONTENT, 0, 0);
        if items.is_empty() {
            SendMessageW(combo, CB_ADDSTRING, 0, wide(t(S::SettingsNoneFound)).as_ptr() as LPARAM);
        }
        for item in items {
            SendMessageW(combo, CB_ADDSTRING, 0, wide(item).as_ptr() as LPARAM);
        }
        SendMessageW(combo, CB_SETMINVISIBLE, 12, 0);
    }
}

/// Point a combobox at the device with the given name, else the first one.
fn select_combo(combo: HWND, items: &[String], name: Option<&str>) {
    let wanted = name.map(|n| n.trim().to_lowercase());
    let index = wanted.and_then(|w| items.iter().position(|i| i.to_lowercase() == w)).unwrap_or(0);
    unsafe { SendMessageW(combo, CB_SETCURSEL, index, 0) };
}

fn combo_value(combo: HWND, items: &[String]) -> Option<String> {
    let index = unsafe { SendMessageW(combo, CB_GETCURSEL, 0, 0) };
    usize::try_from(index).ok().and_then(|i| items.get(i).cloned())
}

// ----------------------------------------------------------------- messages

/// Recreate the window in the current language, on the same tab. Runs from
/// a posted message, because a window cannot tear itself down from inside
/// one of its own control notifications.
pub fn rebuild() {
    let Some(Some((hwnd, tab))) = with_app(|app| app.settings.as_ref().map(|s| (s.hwnd, app.current_tab()))) else {
        return;
    };
    unsafe { DestroyWindow(hwnd) }; // WM_DESTROY clears app.settings
    with_app(|app| app.open_settings_on(tab));
}

unsafe extern "system" fn settings_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_CTLCOLORSTATIC | WM_CTLCOLORBTN => {
            let hdc = wparam as HDC;
            let colour = match GetDlgCtrlID(lparam as HWND) {
                ID_STATUS => STATUS_COLOR.with(Cell::get),
                ID_CABLE_HINT => CABLE_COLOR.with(Cell::get),
                ID_MUTED => COL_MUTED,
                _ => GetSysColor(COLOR_WINDOWTEXT),
            };
            SetTextColor(hdc, colour);
            SetBkColor(hdc, GetSysColor(COLOR_WINDOW));
            GetSysColorBrush(COLOR_WINDOW) as LRESULT
        }
        WM_COMMAND => {
            let id = (wparam & 0xFFFF) as i32;
            let code = ((wparam >> 16) & 0xFFFF) as u32;
            on_command(hwnd, id, code);
            0
        }
        WM_HSCROLL if lparam != 0 => {
            let track = lparam as HWND;
            with_app(|app| {
                if let Some(s) = &app.settings {
                    if let Some(sl) = s.sliders.iter().find(|sl| sl.track == track) {
                        let pos = SendMessageW(track, TBM_GETPOS, 0, 0);
                        set_text(sl.value, &sl.def.format(sl.def.value_at(pos)));
                    }
                }
                app.schedule_apply(180); // sliders fire on every pixel of movement
            });
            0
        }
        WM_NOTIFY => {
            let hdr = &*(lparam as *const NMHDR);
            if hdr.idFrom == ID_TAB as usize && hdr.code == TCN_SELCHANGE {
                with_app(|app| {
                    let tab = app.current_tab();
                    app.show_tab(tab);
                });
            }
            0
        }
        WM_TIMER => {
            match wparam {
                TIMER_METERS => {
                    with_app(|app| app.tick_meters());
                }
                TIMER_APPLY => {
                    KillTimer(hwnd, TIMER_APPLY);
                    with_app(|app| app.apply());
                }
                _ => {}
            }
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_DESTROY => {
            KillTimer(hwnd, TIMER_METERS);
            KillTimer(hwnd, TIMER_APPLY);
            HWND_NOW.with(|c| c.set(std::ptr::null_mut()));
            let flush = with_app(|app| {
                // An apply still waiting on its timer would be lost otherwise.
                if app.settings.as_ref().is_some_and(|s| s.apply_pending) {
                    app.apply();
                }
                app.settings = None;
            });
            if flush.is_some() {
                trim_working_set();
            }
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

fn on_command(hwnd: HWND, id: i32, code: u32) {
    const BN_CLICKED_CODE: u32 = BN_CLICKED;
    const CBN_SELCHANGE_CODE: u32 = CBN_SELCHANGE;
    match (id, code) {
        // IDCANCEL comes from IsDialogMessage when Esc is pressed.
        (ID_CLOSE, BN_CLICKED_CODE) | (IDCANCEL, _) => unsafe {
            DestroyWindow(hwnd);
        },
        (ID_RESTART, BN_CLICKED_CODE) => {
            with_app(|app| app.restart_audio());
        }
        (ID_RESCAN, BN_CLICKED_CODE) => {
            with_app(|app| {
                app.reload_devices();
                app.load_values();
            });
        }
        (ID_CABLE, BN_CLICKED_CODE) => {
            win::shell_open(VB_CABLE_URL);
        }
        (ID_OPEN_LOGS, BN_CLICKED_CODE) => {
            let dir = config::config_dir();
            if !win::shell_open(&dir.display().to_string()) {
                win::message(hwnd, &dir.display().to_string(), MessageKind::Info);
            }
        }
        (ID_RESET, BN_CLICKED_CODE) => {
            if win::confirm(hwnd, t(S::SettingsResetConfirm)) {
                with_app(|app| app.reset_to_defaults());
            }
        }
        (ID_SHORTCUT, BN_CLICKED_CODE) => {
            let ok = shortcut::create(shortcut::Where::Desktop);
            shortcut::create(shortcut::Where::StartMenu); // a bonus; not reported on its own
            let (text, kind) = if ok {
                (S::SettingsShortcutDone, MessageKind::Info)
            } else {
                (S::SettingsShortcutFailed, MessageKind::Warning)
            };
            win::message(hwnd, t(text), kind);
            with_app(|app| app.refresh_shortcut_button());
        }
        (ID_AUTOSTART, BN_CLICKED_CODE) => {
            if with_app(|app| app.on_autostart()) == Some(false) {
                win::message(hwnd, t(S::SettingsAutostartFailed), MessageKind::Warning);
            }
        }
        (ID_MONITOR, BN_CLICKED_CODE) => {
            with_app(|app| app.schedule_apply(50));
        }
        (ID_NEURAL, CBN_SELCHANGE_CODE) => {
            with_app(|app| {
                app.sync_neural();
                app.schedule_apply(50);
            });
        }
        (ID_GATE, BN_CLICKED_CODE) => {
            with_app(|app| app.schedule_apply(180));
        }
        (p, BN_CLICKED_CODE) if (ID_PRESET..ID_PRESET + 3).contains(&p) => {
            with_app(|app| app.on_preset((p - ID_PRESET) as usize));
        }
        (ID_IN | ID_OUT | ID_MON, CBN_SELCHANGE_CODE) => {
            with_app(|app| {
                app.update_cable_hint();
                app.schedule_apply(50);
            });
        }
        (ID_LANG, CBN_SELCHANGE_CODE) => {
            with_app(|app| {
                let Some(s) = &app.settings else { return };
                let combo = unsafe { GetDlgItem(s.hwnd, ID_LANG) };
                let index = unsafe { SendMessageW(combo, CB_GETCURSEL, 0, 0) };
                if let Some(&lang) = usize::try_from(index).ok().and_then(|i| Lang::ALL.get(i)) {
                    if lang != i18n::language() {
                        app.set_language(lang); // posts the rebuild
                    }
                }
            });
        }
        _ => {}
    }
}
