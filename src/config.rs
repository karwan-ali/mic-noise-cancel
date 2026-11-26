//! Persistent settings, stored as JSON under %APPDATA%\MicNoiseCancel.
//!
//! The file format is the same one the Python version wrote, so existing
//! settings carry over.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::dsp::EnhancerParams;
use crate::neural::NeuralModel;

pub const APP_NAME: &str = "MicNoiseCancel";
pub const APP_TITLE: &str = "Mic Noise Cancel";
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    // Devices are stored by name, not index: indices shuffle when hardware is
    // plugged in or out, names survive a reboot.
    pub input_device: Option<String>,
    pub output_device: Option<String>,
    pub monitor_device: Option<String>,
    pub monitor_enabled: bool,
    pub monitor_gain_db: f32,
    /// None follows the Windows display language until the user picks one.
    pub language: Option<String>,
    pub enabled: bool,
    pub autostart: bool,
    pub preset: String,
    pub samplerate: u32,
    // DSP
    pub reduction_db: f32,
    pub gate_enabled: bool,
    pub gate_floor_db: f32,
    pub vad_threshold_db: f32,
    pub vad_hangover_ms: f32,
    pub gate_attack_ms: f32,
    pub gate_release_ms: f32,
    pub highpass_hz: f32,
    pub input_gain_db: f32,
    pub makeup_gain_db: f32,
    pub use_rnnoise: bool,
    /// DeepFilterNet replaces RNNoise as the first stage when on. A separate
    /// flag rather than a new enum, so older files keep their meaning.
    pub use_deepfilternet: bool,
    /// How far DeepFilterNet may push noise down; 100 dB means no limit.
    pub deepfilter_limit_db: f32,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            input_device: None,
            output_device: None,
            monitor_device: None,
            monitor_enabled: false,
            monitor_gain_db: -6.0,
            language: None,
            enabled: true,
            autostart: false,
            preset: "aggressive".into(),
            samplerate: 48000,
            reduction_db: 30.0,
            gate_enabled: true,
            gate_floor_db: -60.0,
            vad_threshold_db: 1.5,
            vad_hangover_ms: 180.0,
            gate_attack_ms: 8.0,
            gate_release_ms: 220.0,
            highpass_hz: 90.0,
            input_gain_db: 0.0,
            makeup_gain_db: 0.0,
            use_rnnoise: true,
            use_deepfilternet: false,
            deepfilter_limit_db: 100.0,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Preset {
    Light,
    Balanced,
    Aggressive,
}

impl Preset {
    pub const ALL: [Preset; 3] = [Preset::Light, Preset::Balanced, Preset::Aggressive];

    pub fn id(self) -> &'static str {
        match self {
            Preset::Light => "light",
            Preset::Balanced => "balanced",
            Preset::Aggressive => "aggressive",
        }
    }

    /// (reduction_db, gate_enabled, gate_floor_db, vad_threshold_db, highpass_hz)
    fn values(self) -> (f32, bool, f32, f32, f32) {
        match self {
            Preset::Light => (14.0, false, -20.0, 1.5, 70.0),
            Preset::Balanced => (22.0, true, -28.0, 1.5, 80.0),
            Preset::Aggressive => (30.0, true, -60.0, 1.5, 90.0),
        }
    }
}

impl Config {
    pub fn apply_preset(&mut self, preset: Preset) {
        let (reduction, gate, floor, vad, hp) = preset.values();
        self.reduction_db = reduction;
        self.gate_enabled = gate;
        self.gate_floor_db = floor;
        self.vad_threshold_db = vad;
        self.highpass_hz = hp;
        self.preset = preset.id().into();
    }

    /// Which preset (if any) the current DSP values correspond to.
    pub fn detect_preset(&self) -> Option<Preset> {
        let close = |a: f32, b: f32| (a - b).abs() < 1e-4;
        Preset::ALL.into_iter().find(|p| {
            let (reduction, gate, floor, vad, hp) = p.values();
            close(self.reduction_db, reduction)
                && self.gate_enabled == gate
                && close(self.gate_floor_db, floor)
                && close(self.vad_threshold_db, vad)
                && close(self.highpass_hz, hp)
        })
    }

    pub fn enhancer_params(&self) -> EnhancerParams {
        EnhancerParams {
            reduction_db: self.reduction_db,
            gate_floor_db: self.gate_floor_db,
            vad_threshold_db: self.vad_threshold_db,
            vad_hangover_ms: self.vad_hangover_ms,
            gate_attack_ms: self.gate_attack_ms,
            gate_release_ms: self.gate_release_ms,
            highpass_hz: self.highpass_hz,
            input_gain_db: self.input_gain_db,
            makeup_gain_db: self.makeup_gain_db,
            gate_enabled: self.gate_enabled,
        }
    }

    /// The network that runs before the app's own noise reduction.
    pub fn neural_model(&self) -> NeuralModel {
        if self.use_deepfilternet {
            NeuralModel::DeepFilterNet
        } else if self.use_rnnoise {
            NeuralModel::RNNoise
        } else {
            NeuralModel::Off
        }
    }

    pub fn set_neural_model(&mut self, model: NeuralModel) {
        self.use_deepfilternet = model == NeuralModel::DeepFilterNet;
        // RNNoise's flag is left alone under DeepFilterNet: switching back
        // from it to "RNNoise" then sets it again anyway.
        match model {
            NeuralModel::Off => self.use_rnnoise = false,
            NeuralModel::RNNoise => self.use_rnnoise = true,
            NeuralModel::DeepFilterNet => {}
        }
    }

    /// Parse a stored file. Each key is taken on its own, so one value of the
    /// wrong type costs that setting rather than the whole file.
    pub fn from_json(text: &str) -> Config {
        let defaults = Config::default();
        let Ok(serde_json::Value::Object(stored)) = serde_json::from_str::<serde_json::Value>(text)
        else {
            crate::log_warn!("config.json is not a JSON object, using defaults");
            return defaults;
        };
        let mut merged = serde_json::to_value(&defaults).expect("defaults serialise");
        for (key, value) in stored {
            let Some(slot) = merged.get(&key) else { continue }; // unknown keys are dropped
            let previous = slot.clone();
            merged[&key] = value;
            if serde_json::from_value::<Config>(merged.clone()).is_err() {
                crate::log_warn!("Ignoring config value for {key}: wrong type");
                merged[&key] = previous;
            }
        }
        serde_json::from_value(merged).unwrap_or(defaults)
    }
}

pub fn config_dir() -> PathBuf {
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| Path::new(&h).join(".config")))
        .unwrap_or_else(|| PathBuf::from("."));
    let dir = base.join(APP_NAME);
    let _ = std::fs::create_dir_all(&dir);
    dir
}

pub fn config_path() -> PathBuf {
    config_dir().join("config.json")
}

pub fn load() -> Config {
    let path = config_path();
    match std::fs::read_to_string(&path) {
        Ok(text) => Config::from_json(&text),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            crate::log_info!("No config yet, using defaults ({})", path.display());
            Config::default()
        }
        Err(e) => {
            crate::log_warn!("Could not read {}: {e}", path.display());
            Config::default()
        }
    }
}

/// Write atomically so a crash mid-write cannot leave a truncated file.
pub fn save(cfg: &Config) -> bool {
    let path = config_path();
    let tmp = path.with_extension("json.tmp");
    let result = serde_json::to_string_pretty(cfg)
        .map_err(std::io::Error::other)
        .and_then(|text| std::fs::write(&tmp, text))
        .and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = result {
        let _ = std::fs::remove_file(&tmp);
        crate::log_warn!("Could not save config to {}: {e}", path.display());
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn presets_round_trip() {
        for p in Preset::ALL {
            let mut cfg = Config::default();
            cfg.apply_preset(p);
            assert_eq!(cfg.detect_preset(), Some(p));
            assert_eq!(cfg.preset, p.id());
        }
        let mut cfg = Config::default();
        cfg.reduction_db = 17.0;
        assert_eq!(cfg.detect_preset(), None);
        assert_eq!(Config::default().detect_preset(), Some(Preset::Aggressive));
    }

    #[test]
    fn reads_the_python_format() {
        let text = r#"{
          "input_device": "Mikrofon (Realtek(R) Audio)",
          "output_device": null,
          "samplerate": 48000,
          "reduction_db": 22,
          "gate_enabled": true,
          "language": "sv",
          "bogus_key": 1,
          "highpass_hz": "oops"
        }"#;
        let cfg = Config::from_json(text);
        assert_eq!(cfg.input_device.as_deref(), Some("Mikrofon (Realtek(R) Audio)"));
        assert_eq!(cfg.output_device, None);
        assert_eq!(cfg.reduction_db, 22.0);
        assert_eq!(cfg.language.as_deref(), Some("sv"));
        assert_eq!(cfg.highpass_hz, 90.0, "a bad value keeps the default");
        assert_eq!(Config::from_json("not json"), Config::default());
    }

    #[test]
    fn neural_model_round_trips_and_old_files_keep_their_meaning() {
        let mut cfg = Config::default();
        assert_eq!(cfg.neural_model(), NeuralModel::RNNoise);
        for m in NeuralModel::ALL {
            cfg.set_neural_model(m);
            assert_eq!(cfg.neural_model(), m);
        }
        // Written before DeepFilterNet existed.
        let old = Config::from_json(r#"{ "use_rnnoise": false }"#);
        assert_eq!(old.neural_model(), NeuralModel::Off);
        assert_eq!(old.deepfilter_limit_db, 100.0);
        let dfn = Config::from_json(r#"{ "use_rnnoise": true, "use_deepfilternet": true }"#);
        assert_eq!(dfn.neural_model(), NeuralModel::DeepFilterNet);
    }

    #[test]
    fn serialises_back() {
        let mut cfg = Config::default();
        cfg.input_device = Some("Mic \"quoted\" ö".into());
        cfg.set_neural_model(NeuralModel::DeepFilterNet);
        cfg.deepfilter_limit_db = 24.0;
        let text = serde_json::to_string_pretty(&cfg).unwrap();
        assert_eq!(Config::from_json(&text), cfg);
    }
}
