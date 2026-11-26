//! Audio device discovery through WASAPI (via cpal).
//!
//! WASAPI lists each active endpoint once, under the same friendly name the
//! Windows Sound settings show, so there is nothing to de-duplicate.

use cpal::traits::{DeviceTrait, HostTrait};

use crate::config::Config;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Input,
    Output,
}

// Substrings that identify the input side of a virtual audio cable, i.e. the
// device we should *write* the cleaned microphone signal to.
const VIRTUAL_SINK_HINTS: &[&str] = &[
    "cable input",
    "voicemeeter input",
    "voicemeeter aux input",
    "voicemeeter vaio3 input",
    "line 1 (virtual audio cable)",
    "virtual cable",
];

// The matching capture side, which other apps pick as their "microphone".
const VIRTUAL_SOURCE_HINTS: &[&str] = &["cable output", "voicemeeter output", "line 1 (virtual audio cable)"];

fn matches(name: &str, hints: &[&str]) -> bool {
    let lowered = name.to_lowercase();
    hints.iter().any(|h| lowered.contains(h))
}

pub fn is_virtual_sink(name: &str) -> bool {
    matches(name, VIRTUAL_SINK_HINTS)
}

fn device_name(dev: &cpal::Device) -> Option<String> {
    dev.description().ok().map(|d| d.name().trim().to_string())
}

/// Every active device of one kind, as (name, device), sorted by name.
pub fn list(kind: Kind) -> Vec<(String, cpal::Device)> {
    let host = cpal::default_host();
    let devices = match kind {
        Kind::Input => host.input_devices().map(|d| d.collect::<Vec<_>>()),
        Kind::Output => host.output_devices().map(|d| d.collect::<Vec<_>>()),
    };
    let mut out: Vec<(String, cpal::Device)> = match devices {
        Ok(devs) => devs.into_iter().filter_map(|d| Some((device_name(&d)?, d))).collect(),
        Err(e) => {
            crate::log_warn!("Device enumeration failed: {e}");
            Vec::new()
        }
    };
    out.sort_by_key(|(name, _)| name.to_lowercase());
    out
}

pub fn names(kind: Kind) -> Vec<String> {
    list(kind).into_iter().map(|(n, _)| n).collect()
}

/// Resolve a stored device name back to a live device.
pub fn find_by_name(kind: Kind, name: &str) -> Option<(String, cpal::Device)> {
    let wanted = name.trim().to_lowercase();
    if wanted.is_empty() {
        return None;
    }
    let devices = list(kind);
    if let Some(i) = devices.iter().position(|(n, _)| n.to_lowercase() == wanted) {
        return devices.into_iter().nth(i);
    }
    // Drivers sometimes append or trim a suffix between reboots.
    devices.into_iter().find(|(n, _)| {
        let n = n.to_lowercase();
        n.contains(&wanted) || wanted.contains(&n)
    })
}

/// The system default device.
pub fn default_device(kind: Kind) -> Option<(String, cpal::Device)> {
    let host = cpal::default_host();
    let dev = match kind {
        Kind::Input => host.default_input_device(),
        Kind::Output => host.default_output_device(),
    }?;
    Some((device_name(&dev)?, dev))
}

pub fn find_virtual_sink() -> Option<String> {
    names(Kind::Output).into_iter().find(|n| matches(n, VIRTUAL_SINK_HINTS))
}

/// The capture side of the cable - what other apps should select.
pub fn find_virtual_source() -> Option<String> {
    names(Kind::Input).into_iter().find(|n| matches(n, VIRTUAL_SOURCE_HINTS))
}

/// Fill in any unset device so a fresh install just works: the system
/// microphone in, the virtual cable out, the real speakers for monitoring.
pub fn suggest_defaults(cfg: &mut Config) -> bool {
    let mut changed = false;

    if cfg.input_device.is_none() {
        let dev = default_device(Kind::Input)
            .map(|(n, _)| n)
            .filter(|n| !matches(n, VIRTUAL_SOURCE_HINTS))
            .or_else(|| names(Kind::Input).into_iter().find(|n| !matches(n, VIRTUAL_SOURCE_HINTS)));
        if let Some(name) = dev {
            cfg.input_device = Some(name);
            changed = true;
        }
    }

    if cfg.output_device.is_none() {
        if let Some(name) = find_virtual_sink() {
            cfg.output_device = Some(name);
            changed = true;
        }
    }

    if cfg.monitor_device.is_none() {
        if let Some((name, _)) = default_device(Kind::Output) {
            if !is_virtual_sink(&name) {
                cfg.monitor_device = Some(name);
                changed = true;
            }
        }
    }

    changed
}
