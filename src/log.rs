//! A tiny rotating file logger: app.log next to config.json, 512 KB per file,
//! two backups. Nothing is buffered, so a crash still leaves the last lines.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

const MAX_BYTES: u64 = 512 * 1024;
const BACKUPS: u32 = 2;

struct Sink {
    path: PathBuf,
    file: File,
    written: u64,
}

static SINK: Mutex<Option<Sink>> = Mutex::new(None);

pub fn init(path: PathBuf) {
    let file = OpenOptions::new().create(true).append(true).open(&path);
    if let Ok(file) = file {
        let written = file.metadata().map(|m| m.len()).unwrap_or(0);
        *SINK.lock().unwrap_or_else(|e| e.into_inner()) = Some(Sink { path, file, written });
    }
}

pub fn write(level: &str, args: std::fmt::Arguments) {
    let line = format!("{} {:<7} {}\n", timestamp(), level, args);
    let mut guard = SINK.lock().unwrap_or_else(|e| e.into_inner());
    match guard.as_mut() {
        Some(sink) => {
            if sink.written + line.len() as u64 > MAX_BYTES {
                rotate(sink);
            }
            if sink.file.write_all(line.as_bytes()).is_ok() {
                sink.written += line.len() as u64;
            }
        }
        None => eprint!("{line}"),
    }
}

fn rotate(sink: &mut Sink) {
    let name = |i: u32| {
        let mut p = sink.path.clone().into_os_string();
        p.push(format!(".{i}"));
        PathBuf::from(p)
    };
    for i in (1..BACKUPS).rev() {
        let _ = std::fs::rename(name(i), name(i + 1));
    }
    let _ = std::fs::rename(&sink.path, name(1));
    if let Ok(file) = OpenOptions::new().create(true).write(true).truncate(true).open(&sink.path) {
        sink.file = file;
        sink.written = 0;
    }
}

#[cfg(windows)]
fn timestamp() -> String {
    let mut t = unsafe { std::mem::zeroed() };
    unsafe { windows_sys::Win32::System::SystemInformation::GetLocalTime(&mut t) };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02},{:03}",
        t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond, t.wMilliseconds
    )
}

#[cfg(not(windows))]
fn timestamp() -> String {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let s = d.as_secs();
    format!("{:02}:{:02}:{:02},{:03}Z", s / 3600 % 24, s / 60 % 60, s % 60, d.subsec_millis())
}

#[macro_export]
macro_rules! log_info {
    ($($arg:tt)*) => { $crate::log::write("INFO", format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! log_warn {
    ($($arg:tt)*) => { $crate::log::write("WARNING", format_args!($($arg)*)) };
}

#[macro_export]
macro_rules! log_error {
    ($($arg:tt)*) => { $crate::log::write("ERROR", format_args!($($arg)*)) };
}
