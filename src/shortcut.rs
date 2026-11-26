//! Desktop and Start Menu shortcuts, written with the shell's own IShellLink.
//!
//! The folders are resolved with SHGetKnownFolderPath rather than guessed from
//! the home directory, because a redirected (OneDrive) Desktop is common.

use std::path::PathBuf;

use windows::core::{Interface, PCWSTR};
use windows::Win32::System::Com::{CoCreateInstance, IPersistFile, CLSCTX_INPROC_SERVER};
use windows::Win32::UI::Shell::{IShellLinkW, ShellLink};
use windows_sys::Win32::System::Com::CoTaskMemFree;
use windows_sys::Win32::UI::Shell::{FOLDERID_Desktop, FOLDERID_Programs, SHGetKnownFolderPath};

use crate::config::APP_TITLE;
use crate::i18n::{t, S};
use crate::win::{from_wide_ptr, wide, wide_path};
use crate::{log_info, log_warn};

#[derive(Clone, Copy)]
pub enum Where {
    Desktop,
    StartMenu,
}

pub fn location(place: Where) -> Option<PathBuf> {
    let id = match place {
        Where::Desktop => &FOLDERID_Desktop,
        Where::StartMenu => &FOLDERID_Programs,
    };
    let mut raw: windows_sys::core::PWSTR = std::ptr::null_mut();
    let hr = unsafe { SHGetKnownFolderPath(id, 0, std::ptr::null_mut(), &mut raw) };
    let folder = (hr >= 0 && !raw.is_null()).then(|| unsafe { from_wide_ptr(raw) });
    unsafe { CoTaskMemFree(raw as *const _) };
    Some(PathBuf::from(folder?).join(format!("{APP_TITLE}.lnk")))
}

pub fn exists(place: Where) -> bool {
    location(place).is_some_and(|p| p.is_file())
}

/// Create (or overwrite) a shortcut to this executable.
pub fn create(place: Where) -> bool {
    let Some(path) = location(place) else {
        log_warn!("Could not resolve a shortcut folder");
        return false;
    };
    match write_link(&path) {
        Ok(()) => {
            log_info!("Created shortcut at {}", path.display());
            path.is_file()
        }
        Err(e) => {
            log_warn!("Creating the shortcut at {} failed: {e}", path.display());
            false
        }
    }
}

fn write_link(path: &std::path::Path) -> windows::core::Result<()> {
    let exe = std::env::current_exe().map_err(|_| windows::core::Error::empty())?;
    let dir = exe.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let (exe_w, dir_w, desc_w, path_w) =
        (wide_path(&exe), wide_path(&dir), wide(t(S::ShortcutDescription)), wide_path(path));
    unsafe {
        let link: IShellLinkW = CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER)?;
        link.SetPath(PCWSTR(exe_w.as_ptr()))?;
        link.SetWorkingDirectory(PCWSTR(dir_w.as_ptr()))?;
        link.SetDescription(PCWSTR(desc_w.as_ptr()))?;
        link.SetIconLocation(PCWSTR(exe_w.as_ptr()), 0)?;
        link.cast::<IPersistFile>()?.Save(PCWSTR(path_w.as_ptr()), true)?;
    }
    Ok(())
}
