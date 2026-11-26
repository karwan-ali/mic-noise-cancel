//! Start-with-Windows via the per-user Run key. Needs no administrator rights.

use windows_sys::Win32::Foundation::ERROR_SUCCESS;
use windows_sys::Win32::System::Registry::{
    RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY, HKEY_CURRENT_USER,
    KEY_QUERY_VALUE, KEY_SET_VALUE, REG_SZ,
};

use crate::config::APP_NAME;
use crate::win::wide;
use crate::{log_info, log_warn};

const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";

struct Key(HKEY);

impl Drop for Key {
    fn drop(&mut self) {
        unsafe { RegCloseKey(self.0) };
    }
}

fn open(access: u32) -> Option<Key> {
    let path = wide(RUN_KEY);
    let mut key: HKEY = std::ptr::null_mut();
    let rc = unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, path.as_ptr(), 0, access, &mut key) };
    (rc == ERROR_SUCCESS).then_some(Key(key))
}

/// The command line Windows should run at logon.
fn command() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    Some(format!("\"{}\" --startup", exe.display()))
}

pub fn is_enabled() -> bool {
    let Some(key) = open(KEY_QUERY_VALUE) else { return false };
    let name = wide(APP_NAME);
    let rc = unsafe {
        RegQueryValueExW(key.0, name.as_ptr(), std::ptr::null(), std::ptr::null_mut(), std::ptr::null_mut(), std::ptr::null_mut())
    };
    rc == ERROR_SUCCESS
}

/// Add or remove the Run entry. Returns true on success.
pub fn set_enabled(enabled: bool) -> bool {
    let Some(key) = open(KEY_SET_VALUE) else {
        log_warn!("Could not open the Run key");
        return false;
    };
    let name = wide(APP_NAME);
    if enabled {
        let Some(cmd) = command() else { return false };
        let value = wide(&cmd);
        let rc = unsafe {
            RegSetValueExW(key.0, name.as_ptr(), 0, REG_SZ, value.as_ptr().cast(), (value.len() * 2) as u32)
        };
        if rc != ERROR_SUCCESS {
            log_warn!("Could not write the autostart entry ({rc})");
            return false;
        }
        log_info!("Autostart enabled: {cmd}");
    } else {
        unsafe { RegDeleteValueW(key.0, name.as_ptr()) };
        log_info!("Autostart disabled");
    }
    true
}
