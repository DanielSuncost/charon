use std::fs::OpenOptions;
use std::io::Write;
use std::process::{Command, Stdio};

use base64::Engine;

/// Configure tmux for clipboard passthrough (OSC 52) if running inside tmux.
/// Should be called once at TUI startup.
pub fn configure_tmux_clipboard() {
    if std::env::var_os("TMUX").is_none() {
        return;
    }
    // allow-passthrough lets OSC 52 sequences reach the outer terminal.
    // set-clipboard tells tmux to handle clipboard sequences itself as well.
    // mouse on is required for tmux to forward mouse events to the application
    // (without it, tmux drops mouse events even if the app enables mouse tracking).
    // All set at pane level (-p) to avoid clobbering the user's global config.
    for args in [
        vec!["set", "-p", "allow-passthrough", "on"],
        vec!["set", "-p", "mouse", "on"],
        vec!["set", "-s", "set-clipboard", "on"],
    ] {
        let _ = Command::new("tmux")
            .args(&args)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

fn osc52_clipboard_sequence(text: &str) -> Result<String, String> {
    let term = std::env::var("TERM").unwrap_or_default();
    if term.is_empty() || term == "dumb" {
        return Err("clipboard unavailable in dumb terminal".to_string());
    }

    let encoded = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
    let inner = format!("\x1b]52;c;{}\x07", encoded);
    if std::env::var_os("TMUX").is_some() {
        Ok(format!(
            "\x1bPtmux;\x1b{}\x1b\\",
            inner.replace("\x1b", "\x1b\x1b")
        ))
    } else if std::env::var_os("STY").is_some() {
        Ok(format!(
            "\x1bP{}\x1b\\",
            inner.replace("\x1b", "\x1b\x1b")
        ))
    } else {
        Ok(inner)
    }
}

/// Platform-ordered list of clipboard write commands.
fn copy_commands() -> Vec<(&'static str, &'static [&'static str], &'static str)> {
    let mut cmds = Vec::new();
    // On macOS, pbcopy is always available and most reliable — try it first.
    if cfg!(target_os = "macos") {
        cmds.push(("pbcopy", [].as_slice(), "pbcopy"));
    }
    cmds.push(("wl-copy", [].as_slice(), "system clipboard"));
    cmds.push(("xclip", ["-selection", "clipboard"].as_slice(), "xclip"));
    cmds.push(("xsel", ["--clipboard", "--input"].as_slice(), "xsel"));
    if !cfg!(target_os = "macos") {
        cmds.push(("pbcopy", [].as_slice(), "pbcopy"));
    }
    cmds
}

/// Platform-ordered list of clipboard read commands.
fn paste_commands() -> Vec<(&'static str, &'static [&'static str])> {
    let mut cmds = Vec::new();
    if cfg!(target_os = "macos") {
        cmds.push(("pbpaste", [].as_slice()));
    }
    cmds.push(("wl-paste", ["--no-newline"].as_slice()));
    cmds.push(("xclip", ["-selection", "clipboard", "-o"].as_slice()));
    cmds.push(("xsel", ["--clipboard", "--output"].as_slice()));
    if !cfg!(target_os = "macos") {
        cmds.push(("pbpaste", [].as_slice()));
    }
    cmds
}

/// Copy text to the system clipboard. Returns a label describing the method used.
pub fn copy_to_clipboard(text: &str) -> Result<&'static str, String> {
    for (cmd, args, label) in copy_commands() {
        let mut child = match Command::new(cmd)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(_) => continue,
        };
        if let Some(mut stdin) = child.stdin.take() {
            if stdin.write_all(text.as_bytes()).is_ok()
                && child.wait().map(|s| s.success()).unwrap_or(false)
            {
                return Ok(label);
            }
        }
    }

    // Fallback: OSC 52 escape sequence
    let seq = osc52_clipboard_sequence(text)?;
    let mut tty = OpenOptions::new()
        .write(true)
        .open("/dev/tty")
        .map_err(|e| format!("clipboard failed: {}", e))?;
    tty.write_all(seq.as_bytes())
        .and_then(|_| tty.flush())
        .map_err(|e| format!("clipboard write failed: {}", e))?;
    Ok(if std::env::var_os("TMUX").is_some() {
        "OSC52 via tmux"
    } else {
        "OSC52"
    })
}

/// Convenience wrapper returning bool (for callers that don't need the method label).
pub fn copy_to_clipboard_bool(text: &str) -> bool {
    copy_to_clipboard(text).is_ok()
}

/// Read text from the system clipboard.
pub fn read_from_clipboard() -> Option<String> {
    for (cmd, args) in paste_commands() {
        if let Ok(output) = Command::new(cmd)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .output()
        {
            if output.status.success() {
                return String::from_utf8(output.stdout).ok();
            }
        }
    }
    None
}
