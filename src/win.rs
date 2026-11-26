//! Small Win32 helpers shared by the UI modules.

use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::path::Path;

use windows_sys::Win32::Foundation::HWND;
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    MessageBoxW, IDYES, MB_ICONINFORMATION, MB_ICONQUESTION, MB_ICONWARNING, MB_OK, MB_SETFOREGROUND,
    MB_YESNO, SW_SHOWNORMAL,
};

use crate::config::APP_TITLE;

/// A NUL-terminated UTF-16 copy of `s`.
pub fn wide(s: &str) -> Vec<u16> {
    OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
}

pub fn wide_path(p: &Path) -> Vec<u16> {
    p.as_os_str().encode_wide().chain(std::iter::once(0)).collect()
}

/// Copy `s` into a fixed-size UTF-16 field, truncating and terminating it.
pub fn copy_to_field(field: &mut [u16], s: &str) {
    let mut n = 0;
    for unit in s.encode_utf16() {
        if n + 1 >= field.len() {
            break;
        }
        field[n] = unit;
        n += 1;
    }
    field[n] = 0;
}

/// Read a NUL-terminated UTF-16 string.
///
/// # Safety
/// `p` must point at a valid NUL-terminated UTF-16 string.
pub unsafe fn from_wide_ptr(p: *const u16) -> String {
    let mut len = 0;
    while *p.add(len) != 0 {
        len += 1;
    }
    String::from_utf16_lossy(std::slice::from_raw_parts(p, len))
}

pub fn message(owner: HWND, text: &str, kind: MessageKind) {
    let flags = match kind {
        MessageKind::Info => MB_ICONINFORMATION,
        MessageKind::Warning => MB_ICONWARNING,
    };
    let (t, c) = (wide(text), wide(APP_TITLE));
    unsafe { MessageBoxW(owner, t.as_ptr(), c.as_ptr(), MB_OK | flags | MB_SETFOREGROUND) };
}

pub fn confirm(owner: HWND, text: &str) -> bool {
    let (t, c) = (wide(text), wide(APP_TITLE));
    unsafe { MessageBoxW(owner, t.as_ptr(), c.as_ptr(), MB_YESNO | MB_ICONQUESTION) == IDYES }
}

#[derive(Clone, Copy)]
pub enum MessageKind {
    Info,
    Warning,
}

/// Open a URL or folder with whatever the shell associates with it.
pub fn shell_open(target: &str) -> bool {
    let (verb, file) = (wide("open"), wide(target));
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            file.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    result as isize > 32
}
