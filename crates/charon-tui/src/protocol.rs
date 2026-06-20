//! Control protocol for the `charond` daemon.
//!
//! Newline-delimited JSON ("JSON-lines"), one object per line, over a Unix
//! domain socket. Terminal payloads are base64 in a `data` field. See
//! `docs/plans/charond-daemon.md` for the full spec.
//!
//! Each binary uses a different subset of the message helpers, so unused-in-one-
//! crate items are expected.
#![allow(dead_code)]

use serde::{Deserialize, Serialize};

/// Protocol version negotiated in the `hello`/`welcome` handshake.
pub const PROTO_VERSION: u32 = 1;

/// Messages a client sends to the daemon.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMsg {
    /// Handshake. Daemon replies with [`DaemonMsg::Welcome`].
    Hello {
        #[serde(default)]
        proto: u32,
        #[serde(default)]
        client: String,
        #[serde(default)]
        pid: u32,
    },
    /// Request the full session inventory → [`DaemonMsg::Inventory`].
    List,
    /// Subscribe to a session's output. `replay` requests a scrollback snapshot first.
    Attach {
        session: String,
        #[serde(default)]
        cols: u16,
        #[serde(default)]
        rows: u16,
        #[serde(default)]
        replay: bool,
    },
    /// Stop receiving a session's output (the session keeps running).
    Detach { session: String },
    /// Keystrokes for a session PTY (base64).
    Input { session: String, data: String },
    /// Resize a session.
    Resize {
        session: String,
        cols: u16,
        rows: u16,
    },
    /// Create a new session. `kind`: `local` (cmd/cwd), `tmux`/`boat`/`charon`
    /// (`target` = tmux name / boat session id / charon socket path), or `remote`
    /// (`server` = fleet id, `target` = remote session id).
    Spawn {
        #[serde(default)]
        kind: String,
        #[serde(default)]
        cmd: Vec<String>,
        #[serde(default)]
        title: Option<String>,
        #[serde(default)]
        cwd: Option<String>,
        /// Backend-specific target (tmux name / boat id / socket path).
        #[serde(default)]
        target: Option<String>,
        /// Fleet server id (for `kind = "remote"`).
        #[serde(default)]
        server: Option<String>,
        /// Ephemeral sessions die when their last client detaches (Claude-Code
        /// style) and never persist to disk. Default false = persistent.
        #[serde(default)]
        ephemeral: bool,
        /// Optional explicit session id; daemon assigns one if absent.
        #[serde(default)]
        session: Option<String>,
        #[serde(default)]
        workspace: Option<String>,
        #[serde(default)]
        tab: Option<String>,
        #[serde(default)]
        cols: u16,
        #[serde(default)]
        rows: u16,
    },
    /// Move a session into a workspace and/or tab (only provided fields change).
    Move {
        session: String,
        #[serde(default)]
        workspace: Option<String>,
        #[serde(default)]
        tab: Option<String>,
    },
    /// Terminate a session.
    Kill { session: String },
    /// Re-run an exited session's original command (in its original cwd),
    /// preserving its persisted scrollback.
    Respawn { session: String },
    /// Liveness / latency probe.
    Ping {
        #[serde(default)]
        ts: u64,
    },
    /// Ask the daemon to flush state and exit cleanly (for upgrade/handoff).
    /// Sessions are persisted; local ones become respawnable after restart.
    Shutdown,
}

/// Messages the daemon sends to clients.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum DaemonMsg {
    Welcome {
        proto: u32,
        daemon_version: String,
        pid: u32,
    },
    Inventory {
        sessions: Vec<SessionInfo>,
    },
    /// Terminal bytes for a session (base64). `seq` enables gap detection on reconnect.
    Output {
        session: String,
        data: String,
        seq: u64,
    },
    /// Full scrollback replay on `attach`+`replay` (base64).
    Snapshot {
        session: String,
        data: String,
        cols: u16,
        rows: u16,
        seq: u64,
    },
    Status {
        session: String,
        state: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        detail: Option<String>,
    },
    Spawned {
        session: String,
    },
    Exited {
        session: String,
    },
    Error {
        code: String,
        message: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        session: Option<String>,
    },
    Pong {
        ts: u64,
    },
    /// Acknowledges a [`ClientMsg::Shutdown`]; the daemon exits right after sending.
    ShuttingDown,
}

/// One session's metadata, as reported in [`DaemonMsg::Inventory`].
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionInfo {
    pub id: String,
    pub title: String,
    pub kind: String,
    #[serde(default)]
    pub workspace: String,
    #[serde(default)]
    pub tab: String,
    pub cols: u16,
    pub rows: u16,
    pub state: String,
    pub seq: u64,
}

impl DaemonMsg {
    /// Serialize to a single protocol line (newline-terminated).
    pub fn to_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| "{}".to_string()) + "\n"
    }
}

impl ClientMsg {
    /// Parse one protocol line. Returns `None` for blank lines or malformed JSON.
    pub fn parse(line: &str) -> Option<Self> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        serde_json::from_str(trimmed).ok()
    }

    /// Serialize to a single protocol line (newline-terminated).
    pub fn to_line(&self) -> String {
        serde_json::to_string(self).unwrap_or_else(|_| "{}".to_string()) + "\n"
    }
}
