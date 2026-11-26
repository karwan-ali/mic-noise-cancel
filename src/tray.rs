//! System tray icon: Shell_NotifyIcon with icons drawn at the exact size the
//! taskbar wants, so nothing is scaled and no image files are shipped.

use windows_sys::Win32::Foundation::HWND;
use windows_sys::Win32::Graphics::Gdi::{
    CreateBitmap, CreateDIBSection, DeleteObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS,
};
use windows_sys::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_INFO, NIF_MESSAGE, NIF_TIP, NIIF_INFO, NIM_ADD, NIM_DELETE, NIM_MODIFY,
    NIM_SETVERSION, NOTIFYICONDATAW, NOTIFYICON_VERSION_4,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CreateIconIndirect, DestroyIcon, GetSystemMetrics, HICON, ICONINFO, SM_CXSMICON,
};

use crate::icon_draw::{render_rgba, IconState};
use crate::win::copy_to_field;

pub const WM_TRAY: u32 = windows_sys::Win32::UI::WindowsAndMessaging::WM_APP + 1;
const ICON_ID: u32 = 1;

const STATES: [IconState; 4] = [IconState::Active, IconState::Bypassed, IconState::Stopped, IconState::Error];

pub struct Tray {
    hwnd: HWND,
    icons: [HICON; 4],
    state: IconState,
    tip: String,
}

impl Tray {
    pub fn new(hwnd: HWND) -> Self {
        let size = unsafe { GetSystemMetrics(SM_CXSMICON) }.max(16) as u32;
        let icons = STATES.map(|s| make_icon(s, size));
        let mut tray = Tray { hwnd, icons, state: IconState::Stopped, tip: crate::config::APP_TITLE.into() };
        tray.add();
        tray
    }

    fn data(&self) -> NOTIFYICONDATAW {
        let mut nid: NOTIFYICONDATAW = unsafe { std::mem::zeroed() };
        nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
        nid.hWnd = self.hwnd;
        nid.uID = ICON_ID;
        nid
    }

    fn icon(&self, state: IconState) -> HICON {
        self.icons[STATES.iter().position(|&s| s == state).unwrap_or(0)]
    }

    /// Add the icon. Also called again when Explorer restarts.
    pub fn add(&mut self) {
        let mut nid = self.data();
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
        nid.uCallbackMessage = WM_TRAY;
        nid.hIcon = self.icon(self.state);
        copy_to_field(&mut nid.szTip, &self.tip);
        unsafe {
            Shell_NotifyIconW(NIM_ADD, &nid);
            nid.Anonymous.uVersion = NOTIFYICON_VERSION_4;
            Shell_NotifyIconW(NIM_SETVERSION, &nid);
        }
    }

    pub fn update(&mut self, state: IconState, tip: &str) {
        if state == self.state && tip == self.tip {
            return;
        }
        self.state = state;
        self.tip = tip.to_string();
        let mut nid = self.data();
        nid.uFlags = NIF_ICON | NIF_TIP;
        nid.hIcon = self.icon(state);
        copy_to_field(&mut nid.szTip, tip);
        unsafe { Shell_NotifyIconW(NIM_MODIFY, &nid) };
    }

    pub fn notify(&self, title: &str, message: &str) {
        let mut nid = self.data();
        nid.uFlags = NIF_INFO;
        nid.dwInfoFlags = NIIF_INFO;
        copy_to_field(&mut nid.szInfoTitle, title);
        copy_to_field(&mut nid.szInfo, message);
        unsafe { Shell_NotifyIconW(NIM_MODIFY, &nid) };
    }

    pub fn remove(&mut self) {
        let nid = self.data();
        unsafe { Shell_NotifyIconW(NIM_DELETE, &nid) };
    }
}

impl Drop for Tray {
    fn drop(&mut self) {
        self.remove();
        for icon in self.icons {
            unsafe { DestroyIcon(icon) };
        }
    }
}

/// An HICON from straight-alpha RGBA pixels.
pub fn make_icon(state: IconState, size: u32) -> HICON {
    let rgba = render_rgba(state, size);
    unsafe {
        let mut bmi: BITMAPINFO = std::mem::zeroed();
        bmi.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: size as i32,
            biHeight: -(size as i32), // top-down
            biPlanes: 1,
            biBitCount: 32,
            biCompression: BI_RGB,
            ..std::mem::zeroed()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let color = CreateDIBSection(std::ptr::null_mut(), &bmi, DIB_RGB_COLORS, &mut bits, std::ptr::null_mut(), 0);
        if color.is_null() || bits.is_null() {
            return std::ptr::null_mut();
        }
        let dst = std::slice::from_raw_parts_mut(bits as *mut u8, rgba.len());
        for (d, s) in dst.chunks_exact_mut(4).zip(rgba.chunks_exact(4)) {
            d.copy_from_slice(&[s[2], s[1], s[0], s[3]]); // BGRA
        }
        let mask = CreateBitmap(size as i32, size as i32, 1, 1, std::ptr::null());
        let info = ICONINFO { fIcon: 1, xHotspot: 0, yHotspot: 0, hbmMask: mask, hbmColor: color };
        let icon = CreateIconIndirect(&info);
        DeleteObject(color);
        DeleteObject(mask);
        icon
    }
}
