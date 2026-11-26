//! Mic Noise Cancel - real-time microphone noise suppression for Windows.
//!
//! Lives in the system tray, cleans up the microphone and sends the result to
//! a virtual audio cable that Teams, Zoom or Discord can pick as their mic.

// No console window when started from Explorer.
#![cfg_attr(windows, windows_subsystem = "windows")]

mod config;
mod dsp;
mod i18n;
mod icon_draw;
mod log;
mod neural;
mod resample;
mod ring;
#[cfg(test)]
mod testutil;

#[cfg(windows)]
mod app;
#[cfg(windows)]
mod autostart;
#[cfg(windows)]
mod devices;
#[cfg(windows)]
mod engine;
#[cfg(windows)]
mod settings;
#[cfg(windows)]
mod shortcut;
#[cfg(windows)]
mod tray;
#[cfg(windows)]
mod win;

#[cfg(windows)]
fn main() {
    std::process::exit(app::main());
}

#[cfg(not(windows))]
fn main() {
    eprintln!("{} runs on Windows only. `cargo test` checks the signal processing here.", config::APP_TITLE);
    std::process::exit(1);
}
