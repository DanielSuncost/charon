//! User configuration: `~/.charon/config.toml` (override dir with `$CHARON_DIR`).
//!
//! Holds the active [`Theme`], mouse/behavior flags, and a keybindings map. Colors
//! are plain RGB triples so this module stays free of any TUI/crossterm dependency
//! and is unit-testable; the front-end converts [`Rgb`] to its own color type.
//!
//! Defaults reproduce the current hardcoded appearance, so an absent or partial
//! config changes nothing.
//!
//! Each binary uses a different subset (the TUI reads the theme; the daemon will
//! read the detection table later), so unused-in-one-crate items are expected.
#![allow(dead_code)]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::OnceLock;

use serde::Deserialize;

use crate::backend::dirs_home;

/// The process-wide active config, loaded once on first access.
static ACTIVE: OnceLock<Config> = OnceLock::new();

/// The active configuration, loaded from disk on first call (then cached).
pub fn active() -> &'static Config {
    ACTIVE.get_or_init(Config::load)
}

/// An 8-bit-per-channel RGB color.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rgb(pub u8, pub u8, pub u8);

impl Rgb {
    /// Parse `#rrggbb` (the leading `#` is optional). Returns `None` if malformed.
    pub fn from_hex(s: &str) -> Option<Rgb> {
        let h = s.trim().trim_start_matches('#');
        if h.len() != 6 {
            return None;
        }
        let r = u8::from_str_radix(&h[0..2], 16).ok()?;
        let g = u8::from_str_radix(&h[2..4], 16).ok()?;
        let b = u8::from_str_radix(&h[4..6], 16).ok()?;
        Some(Rgb(r, g, b))
    }
}

/// A resolved color theme.
#[derive(Clone, Debug, PartialEq)]
pub struct Theme {
    pub name: String,
    pub header: Rgb,
    pub accent: Rgb,
    pub status_idle: Rgb,
    pub status_working: Rgb,
    pub status_blocked: Rgb,
    pub border: Rgb,
}

impl Theme {
    /// The default theme — matches the current hardcoded TUI colors.
    pub fn charon_dark() -> Theme {
        Theme {
            name: "charon-dark".to_string(),
            header: Rgb(167, 139, 250),       // current header purple
            accent: Rgb(100, 90, 130),        // current dashboard purple
            status_idle: Rgb(100, 116, 139),  // slate
            status_working: Rgb(212, 175, 55),// gold
            status_blocked: Rgb(251, 146, 60),// orange (attention)
            border: Rgb(148, 163, 184),       // slate
        }
    }

    /// Look up a built-in theme by name.
    pub fn builtin(name: &str) -> Option<Theme> {
        let t = match name {
            "charon-dark" => Theme::charon_dark(),
            "midnight" => Theme {
                name: "midnight".to_string(),
                header: Rgb(129, 140, 248),
                accent: Rgb(55, 65, 81),
                status_idle: Rgb(71, 85, 105),
                status_working: Rgb(96, 165, 250),
                status_blocked: Rgb(244, 114, 182),
                border: Rgb(51, 65, 85),
            },
            "mono" => Theme {
                name: "mono".to_string(),
                header: Rgb(229, 229, 229),
                accent: Rgb(115, 115, 115),
                status_idle: Rgb(115, 115, 115),
                status_working: Rgb(212, 212, 212),
                status_blocked: Rgb(245, 245, 245),
                border: Rgb(82, 82, 82),
            },
            _ => return None,
        };
        Some(t)
    }

    /// Names of the built-in themes.
    pub fn builtin_names() -> &'static [&'static str] {
        &["charon-dark", "midnight", "mono"]
    }

    /// Overlay any set fields from a `[themes.*]` table onto this theme.
    fn overlay(mut self, raw: &RawTheme) -> Theme {
        let apply = |slot: &mut Rgb, val: &Option<String>| {
            if let Some(hex) = val {
                if let Some(rgb) = Rgb::from_hex(hex) {
                    *slot = rgb;
                }
            }
        };
        apply(&mut self.header, &raw.header);
        apply(&mut self.accent, &raw.accent);
        apply(&mut self.status_idle, &raw.status_idle);
        apply(&mut self.status_working, &raw.status_working);
        apply(&mut self.status_blocked, &raw.status_blocked);
        apply(&mut self.border, &raw.border);
        self
    }
}

/// Resolved user configuration.
#[derive(Clone, Debug)]
pub struct Config {
    pub theme: Theme,
    pub mouse: bool,
    pub default_view: String,
    /// If false (default), TUI-spawned sessions are ephemeral (end on close,
    /// Claude-Code style). Set true to make them persist across restarts.
    pub persist_sessions: bool,
    pub keys: HashMap<String, String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            theme: Theme::charon_dark(),
            mouse: true,
            default_view: "chat".to_string(),
            persist_sessions: false,
            keys: HashMap::new(),
        }
    }
}

impl Config {
    /// Load config from disk; falls back to [`Config::default`] if absent or invalid.
    pub fn load() -> Config {
        match std::fs::read_to_string(config_path()) {
            Ok(s) => Config::from_toml(&s).unwrap_or_default(),
            Err(_) => Config::default(),
        }
    }

    /// Parse config from a TOML string and resolve the active theme.
    pub fn from_toml(s: &str) -> Result<Config, toml::de::Error> {
        let raw: RawConfig = toml::from_str(s)?;
        let name = raw.ui.theme.unwrap_or_else(|| "charon-dark".to_string());
        // Base on a built-in if the name matches one; else start from charon-dark.
        let base = Theme::builtin(&name).unwrap_or_else(|| {
            let mut t = Theme::charon_dark();
            t.name = name.clone();
            t
        });
        // A matching [themes.<name>] table overrides/defines fields.
        let theme = match raw.themes.get(&name) {
            Some(raw_theme) => base.overlay(raw_theme),
            None => base,
        };
        Ok(Config {
            theme,
            mouse: raw.ui.mouse.unwrap_or(true),
            default_view: raw.ui.default_view.unwrap_or_else(|| "chat".to_string()),
            persist_sessions: raw.ui.persist_sessions.unwrap_or(false),
            keys: raw.keys,
        })
    }
}

fn config_path() -> PathBuf {
    if let Ok(d) = std::env::var("CHARON_DIR") {
        return PathBuf::from(d).join("config.toml");
    }
    dirs_home().join(".charon/config.toml")
}

// ── Raw TOML shapes ───────────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
struct RawConfig {
    #[serde(default)]
    ui: RawUi,
    #[serde(default)]
    themes: HashMap<String, RawTheme>,
    #[serde(default)]
    keys: HashMap<String, String>,
}

#[derive(Deserialize, Default)]
struct RawUi {
    theme: Option<String>,
    mouse: Option<bool>,
    default_view: Option<String>,
    persist_sessions: Option<bool>,
}

#[derive(Deserialize, Default)]
struct RawTheme {
    header: Option<String>,
    accent: Option<String>,
    status_idle: Option<String>,
    status_working: Option<String>,
    status_blocked: Option<String>,
    border: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_matches_current_colors() {
        let c = Config::default();
        assert_eq!(c.theme.name, "charon-dark");
        assert_eq!(c.theme.header, Rgb(167, 139, 250));
        assert!(c.mouse);
    }

    #[test]
    fn empty_toml_is_default() {
        let c = Config::from_toml("").unwrap();
        assert_eq!(c.theme, Theme::charon_dark());
    }

    #[test]
    fn selects_builtin_theme() {
        let c = Config::from_toml("[ui]\ntheme = \"mono\"\n").unwrap();
        assert_eq!(c.theme.name, "mono");
        assert_eq!(c.theme.header, Rgb(229, 229, 229));
    }

    #[test]
    fn custom_theme_table_defines_and_overrides() {
        let toml = "
[ui]
theme = \"mine\"
[themes.mine]
header = \"#010203\"
status_blocked = \"#ff0000\"
";
        let c = Config::from_toml(toml).unwrap();
        assert_eq!(c.theme.name, "mine");
        assert_eq!(c.theme.header, Rgb(1, 2, 3));
        assert_eq!(c.theme.status_blocked, Rgb(255, 0, 0));
        // Unset fields fall back to the charon-dark base.
        assert_eq!(c.theme.accent, Theme::charon_dark().accent);
    }

    #[test]
    fn overlay_on_builtin() {
        let toml = "
[ui]
theme = \"charon-dark\"
[themes.charon-dark]
header = \"#000000\"
";
        let c = Config::from_toml(toml).unwrap();
        assert_eq!(c.theme.header, Rgb(0, 0, 0));
        assert_eq!(c.theme.border, Theme::charon_dark().border);
    }

    #[test]
    fn hex_parsing() {
        assert_eq!(Rgb::from_hex("#a78bfa"), Some(Rgb(167, 139, 250)));
        assert_eq!(Rgb::from_hex("a78bfa"), Some(Rgb(167, 139, 250)));
        assert_eq!(Rgb::from_hex("#xyz"), None);
        assert_eq!(Rgb::from_hex("#12345"), None);
    }
}
