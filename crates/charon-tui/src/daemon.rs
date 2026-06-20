//! `charond` — the always-on Charon session daemon.
//!
//! Owns all `SessionCell`s (PTYs and, later, tmux/boat/remote backends), persists
//! their scrollback, and fans terminal output out to any number of attached
//! clients over a Unix socket. Clients (the TUI and other front-ends) hold no PTYs, so
//! closing a client never kills a session — this is what makes detach/reattach work.
//!
//! Phase 1 scope: local shell/command sessions, the control protocol handshake,
//! attach/detach with raw scrollback replay, input/resize, spawn/kill, list.
//!
//! Most of this module is the server, exercised by the `charond` binary and the
//! lib; the `charon` TUI links it too but only calls a few helpers, so it's
//! allowed to leave the rest "unused" from that crate's narrow view.
#![allow(dead_code)]

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, Instant};

use base64::Engine;
use serde::{Deserialize, Serialize};

use crate::backend::dirs_home;
use crate::detect;
use crate::protocol::{ClientMsg, DaemonMsg, SessionInfo, PROTO_VERSION};
use crate::session::SessionCell;

/// Per-session raw scrollback cap, in bytes. Bounds memory + replay payload size.
const SCROLLBACK_CAP: usize = 2 * 1024 * 1024; // 2 MiB
/// Main loop tick: drain client commands + poll sessions for output.
const TICK: Duration = Duration::from_millis(8);
/// No output for this long → a session is considered quiescent (idle/blocked).
const IDLE_THRESHOLD: Duration = Duration::from_millis(500);
/// Default workspace/tab for sessions created without one.
const DEFAULT_WORKSPACE: &str = "default";
const DEFAULT_TAB: &str = "main";
/// Grace after an ephemeral session's last client detaches before it's reaped —
/// covers spawn→attach handoff and brief reconnects.
const REAP_GRACE: Duration = Duration::from_secs(3);

/// Root Charon state dir. `$CHARON_DIR` overrides (used by tests for isolation).
fn charon_dir() -> PathBuf {
    if let Ok(d) = std::env::var("CHARON_DIR") {
        return PathBuf::from(d);
    }
    dirs_home().join(".charon")
}
fn socket_path() -> PathBuf {
    std::env::var("CHARON_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|_| charon_dir().join("charond.sock"))
}
fn pid_path() -> PathBuf {
    charon_dir().join("charond.pid")
}
/// Directory holding one subdirectory per persisted session.
fn sessions_dir() -> PathBuf {
    charon_dir().join("sessions")
}
fn session_dir(id: &str) -> PathBuf {
    sessions_dir().join(id)
}

fn default_shell() -> String {
    std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".to_string())
}

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

/// On-disk session metadata, enough to restore + respawn after a daemon restart.
#[derive(Serialize, Deserialize)]
struct PersistMeta {
    id: String,
    title: String,
    kind: String,
    #[serde(default)]
    workspace: String,
    #[serde(default)]
    tab: String,
    #[serde(default)]
    cmd: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    target: Option<String>,
    #[serde(default)]
    server: Option<String>,
    cols: u16,
    rows: u16,
}

/// Kinds whose backend lives outside the daemon and survives a daemon restart
/// (so they should be re-attached on restore rather than marked exited).
fn is_external_kind(kind: &str) -> bool {
    matches!(kind, "tmux" | "boat" | "charon" | "remote")
}

/// Construct a `SessionCell` for a backend kind. Shared by spawn, restore, respawn.
#[allow(clippy::too_many_arguments)]
fn build_cell(
    id: u64,
    kind: &str,
    title: &str,
    cmd: &[String],
    cwd: Option<&str>,
    target: Option<&str>,
    server: Option<&str>,
    cols: u16,
    rows: u16,
) -> io::Result<SessionCell> {
    fn need<'a>(kind: &str, v: Option<&'a str>) -> io::Result<&'a str> {
        v.filter(|s| !s.is_empty())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, format!("kind '{kind}' requires a target")))
    }
    match kind {
        "local" => {
            let argv: Vec<&str> = cmd.iter().map(String::as_str).collect();
            SessionCell::spawn_cwd(id, title, &argv, cols, rows, cwd)
        }
        "tmux" => SessionCell::attach_tmux(id, title, need(kind, target)?, cols, rows),
        "boat" => SessionCell::attach_boat(id, title, need(kind, target)?, cols, rows),
        "charon" => SessionCell::attach_charon(id, title, need(kind, target)?, cols, rows),
        "remote" => {
            let sid = need(kind, target)?;
            let server_id = need(kind, server)?;
            let fleet = crate::backend::load_fleet_config();
            match fleet.iter().find(|s| s.id == server_id) {
                Some(srv) => SessionCell::attach_remote_boat(id, title, srv, sid, cols, rows),
                None => Err(io::Error::new(io::ErrorKind::NotFound, format!("no fleet server: {server_id}"))),
            }
        }
        other => Err(io::Error::new(io::ErrorKind::InvalidInput, format!("unsupported kind: {other}"))),
    }
}
fn debug_log(msg: &str) {
    if std::env::var("CHARON_DEBUG").is_ok() {
        eprintln!("[charond] {msg}");
    }
}

// ── Internal state ──────────────────────────────────────────────────────────

/// A session owned by the daemon: the live terminal plus everything needed to
/// replay it to a freshly-attached client.
struct DaemonSession {
    id: String,
    cell: SessionCell,
    title: String,
    kind: String,
    /// Workspace + tab this session belongs to (grouping for front-ends).
    workspace: String,
    tab: String,
    /// Argv used to spawn the session; replayed on respawn after a restart.
    cmd: Vec<String>,
    cwd: Option<String>,
    /// Backend target (tmux name / boat id / charon socket) for non-local kinds.
    target: Option<String>,
    /// Fleet server id (for `kind = "remote"`).
    server: Option<String>,
    cols: u16,
    rows: u16,
    seq: u64,
    /// Raw post-backend bytes, capped at [`SCROLLBACK_CAP`]. Replayed on attach.
    scrollback: Vec<u8>,
    /// Client ids currently receiving this session's output.
    subscribers: Vec<u64>,
    state: String,
    /// Append handle for the on-disk scrollback log (None until persisted).
    log: Option<File>,
    log_len: usize,
    /// When output last arrived; drives idle/working heuristics.
    last_output_at: Instant,
    /// Ephemeral sessions die when their last client detaches and never persist.
    ephemeral: bool,
    /// If set, an ephemeral session with no clients is reaped at this time.
    reap_at: Option<Instant>,
}

impl DaemonSession {
    fn info(&self) -> SessionInfo {
        SessionInfo {
            id: self.id.clone(),
            title: self.title.clone(),
            kind: self.kind.clone(),
            workspace: self.workspace.clone(),
            tab: self.tab.clone(),
            ephemeral: self.ephemeral,
            cols: self.cols,
            rows: self.rows,
            state: self.state.clone(),
            seq: self.seq,
        }
    }

    /// Write `meta.json` so the session can be restored after a daemon restart.
    fn persist_meta(&self) {
        let dir = session_dir(&self.id);
        if std::fs::create_dir_all(&dir).is_err() {
            return;
        }
        let meta = PersistMeta {
            id: self.id.clone(),
            title: self.title.clone(),
            kind: self.kind.clone(),
            workspace: self.workspace.clone(),
            tab: self.tab.clone(),
            cmd: self.cmd.clone(),
            cwd: self.cwd.clone(),
            target: self.target.clone(),
            server: self.server.clone(),
            cols: self.cols,
            rows: self.rows,
        };
        if let Ok(json) = serde_json::to_string_pretty(&meta) {
            let _ = std::fs::write(dir.join("meta.json"), json);
        }
    }

    /// Open (and optionally truncate) the on-disk scrollback log.
    fn open_log(&mut self, truncate: bool) {
        let dir = session_dir(&self.id);
        if std::fs::create_dir_all(&dir).is_err() {
            return;
        }
        let path = dir.join("scrollback.log");
        let mut opts = std::fs::OpenOptions::new();
        opts.create(true).write(true);
        if truncate {
            opts.truncate(true);
        } else {
            opts.append(true);
        }
        if let Ok(f) = opts.open(&path) {
            self.log_len = std::fs::metadata(&path).map(|m| m.len() as usize).unwrap_or(0);
            self.log = Some(f);
        }
    }

    /// Append terminal bytes to the on-disk log, compacting if it grows too large.
    fn append_log(&mut self, bytes: &[u8]) {
        let ok = match self.log.as_mut() {
            Some(f) => f.write_all(bytes).is_ok(),
            None => false,
        };
        if ok {
            self.log_len += bytes.len();
            if self.log_len > SCROLLBACK_CAP * 2 {
                self.compact_log();
            }
        }
    }

    /// Rewrite the log from the (already-capped) in-memory scrollback.
    fn compact_log(&mut self) {
        let path = session_dir(&self.id).join("scrollback.log");
        if std::fs::write(&path, &self.scrollback).is_ok() {
            self.log_len = self.scrollback.len();
            self.open_log(false);
        }
    }
}

/// The write half of a connected client's socket, keyed by client id.
type Clients = HashMap<u64, UnixStream>;

/// Messages flowing from the accept/reader threads into the main event loop.
enum Inbound {
    Connect(u64, UnixStream),
    Msg(u64, ClientMsg),
    Disconnect(u64),
}

// ── Entry point ───────────────────────────────────────────────────────────────

/// Run the daemon. Blocks until the process is terminated.
pub fn run() -> io::Result<()> {
    let dir = charon_dir();
    std::fs::create_dir_all(&dir)?;
    let sock = socket_path();

    // Single-instance guard: if we can connect, a live daemon already owns the
    // socket — bail out. Otherwise the socket file is stale; remove it and bind.
    if UnixStream::connect(&sock).is_ok() {
        debug_log("another charond is already running; exiting");
        return Ok(());
    }
    let _ = std::fs::remove_file(&sock);
    let listener = UnixListener::bind(&sock)?;

    let _pid_guard = PidGuard::write()?;
    debug_log(&format!("listening on {}", sock.display()));

    let (tx, rx): (Sender<Inbound>, Receiver<Inbound>) = mpsc::channel();
    spawn_accept_thread(listener, tx);

    let mut daemon = Daemon::new();
    daemon.load_persisted();
    daemon.event_loop(rx);
    Ok(())
}

/// Accept connections; for each, register its write half with the main loop and
/// spawn a reader thread that forwards parsed commands.
fn spawn_accept_thread(listener: UnixListener, tx: Sender<Inbound>) {
    thread::spawn(move || {
        static NEXT_ID: AtomicU64 = AtomicU64::new(1);
        for conn in listener.incoming() {
            let Ok(stream) = conn else { break };
            let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
            let Ok(write_half) = stream.try_clone() else { continue };
            if tx.send(Inbound::Connect(id, write_half)).is_err() {
                break;
            }
            let tx_reader = tx.clone();
            thread::spawn(move || {
                let mut reader = BufReader::new(stream);
                let mut line = String::new();
                loop {
                    line.clear();
                    match reader.read_line(&mut line) {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {
                            if let Some(msg) = ClientMsg::parse(&line) {
                                if tx_reader.send(Inbound::Msg(id, msg)).is_err() {
                                    break;
                                }
                            }
                        }
                    }
                }
                let _ = tx_reader.send(Inbound::Disconnect(id));
            });
        }
    });
}

// ── The daemon event loop ─────────────────────────────────────────────────────

struct Daemon {
    sessions: HashMap<String, DaemonSession>,
    clients: Clients,
    next_session: u64,
    shutdown: bool,
}

impl Daemon {
    fn new() -> Self {
        Daemon {
            sessions: HashMap::new(),
            clients: HashMap::new(),
            next_session: 1,
            shutdown: false,
        }
    }

    /// Load sessions persisted on disk (from a prior daemon run) as `exited`
    /// sessions with their scrollback intact, so reattaching replays history and
    /// the client can respawn them. External-backed kinds are out of scope here
    /// (Phase 3 covers local sessions).
    fn load_persisted(&mut self) {
        let Ok(entries) = std::fs::read_dir(sessions_dir()) else {
            return;
        };
        let mut max_local = 0u64;
        for entry in entries.flatten() {
            let dir = entry.path();
            if !dir.is_dir() {
                continue;
            }
            let Ok(meta_str) = std::fs::read_to_string(dir.join("meta.json")) else {
                continue;
            };
            let Ok(meta) = serde_json::from_str::<PersistMeta>(&meta_str) else {
                continue;
            };
            let mut scrollback = std::fs::read(dir.join("scrollback.log")).unwrap_or_default();
            if scrollback.len() > SCROLLBACK_CAP {
                let drop = scrollback.len() - SCROLLBACK_CAP;
                scrollback.drain(0..drop);
            }
            if let Some(n) = meta.id.rsplit('-').next().and_then(|s| s.parse::<u64>().ok()) {
                max_local = max_local.max(n);
            }
            let (cols, rows) = (meta.cols.max(1), meta.rows.max(1));
            // External-backed sessions survive a daemon restart → re-attach them.
            // Local PTYs die with the daemon → restore as exited + respawnable.
            let (cell, state) = if is_external_kind(&meta.kind) {
                match build_cell(0, &meta.kind, &meta.title, &meta.cmd, meta.cwd.as_deref(), meta.target.as_deref(), meta.server.as_deref(), cols, rows) {
                    Ok(c) => (c, "working".to_string()),
                    Err(_) => (SessionCell::dead(0, &meta.title, cols, rows), "exited".to_string()),
                }
            } else {
                (SessionCell::dead(0, &meta.title, cols, rows), "exited".to_string())
            };
            let mut sess = DaemonSession {
                id: meta.id.clone(),
                cell,
                title: meta.title,
                kind: meta.kind,
                workspace: if meta.workspace.is_empty() { DEFAULT_WORKSPACE.to_string() } else { meta.workspace },
                tab: if meta.tab.is_empty() { DEFAULT_TAB.to_string() } else { meta.tab },
                cmd: meta.cmd,
                cwd: meta.cwd,
                target: meta.target,
                server: meta.server,
                cols,
                rows,
                seq: 0,
                scrollback,
                subscribers: Vec::new(),
                state,
                log: None,
                log_len: 0,
                last_output_at: Instant::now(),
                ephemeral: false,
                reap_at: None,
            };
            sess.open_log(false); // append handle, kept for respawn
            debug_log(&format!("restored session {}", sess.id));
            self.sessions.insert(meta.id, sess);
        }
        self.next_session = max_local + 1;
    }

    fn event_loop(&mut self, rx: Receiver<Inbound>) {
        loop {
            // 1. Drain all pending client commands.
            loop {
                match rx.try_recv() {
                    Ok(inbound) => self.handle_inbound(inbound),
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(mpsc::TryRecvError::Disconnected) => return,
                }
            }
            // 2. Graceful shutdown requested (upgrade/handoff): state is already
            //    persisted incrementally, so just stop the loop and let the
            //    PidGuard clean up the socket + pidfile.
            if self.shutdown {
                debug_log("graceful shutdown");
                return;
            }
            // 3. Poll every session for output and fan it out.
            self.pump_sessions();
            // 4. Reap ephemeral sessions whose grace has elapsed with no clients.
            self.reap_ephemeral();
            thread::sleep(TICK);
        }
    }

    fn handle_inbound(&mut self, inbound: Inbound) {
        match inbound {
            Inbound::Connect(id, stream) => {
                self.clients.insert(id, stream);
                debug_log(&format!("client {id} connected"));
            }
            Inbound::Disconnect(id) => {
                self.clients.remove(&id);
                let now = Instant::now();
                for sess in self.sessions.values_mut() {
                    sess.subscribers.retain(|c| *c != id);
                    // Ephemeral sessions whose last client just left are reaped
                    // after a short grace (allows reattach / spawn→attach handoff).
                    if sess.ephemeral && sess.subscribers.is_empty() && sess.reap_at.is_none() {
                        sess.reap_at = Some(now + REAP_GRACE);
                    }
                }
                debug_log(&format!("client {id} disconnected"));
            }
            Inbound::Msg(id, msg) => self.handle_msg(id, msg),
        }
    }

    fn handle_msg(&mut self, client: u64, msg: ClientMsg) {
        match msg {
            ClientMsg::Hello { .. } => {
                self.send(
                    client,
                    &DaemonMsg::Welcome {
                        proto: PROTO_VERSION,
                        daemon_version: env!("CARGO_PKG_VERSION").to_string(),
                        pid: std::process::id(),
                    },
                );
            }
            ClientMsg::List => {
                let sessions = self
                    .sessions
                    .values()
                    .map(|s| s.info())
                    .collect::<Vec<_>>();
                self.send(client, &DaemonMsg::Inventory { sessions });
            }
            ClientMsg::Attach {
                session,
                cols,
                rows,
                replay,
            } => self.handle_attach(client, &session, cols, rows, replay),
            ClientMsg::Detach { session } => {
                if let Some(s) = self.sessions.get_mut(&session) {
                    s.subscribers.retain(|c| *c != client);
                }
            }
            ClientMsg::Input { session, data } => {
                if let Some(s) = self.sessions.get_mut(&session) {
                    if let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(&data) {
                        let _ = s.cell.write(&bytes);
                    }
                }
            }
            ClientMsg::Resize {
                session,
                cols,
                rows,
            } => {
                if let Some(s) = self.sessions.get_mut(&session) {
                    s.cols = cols.max(1);
                    s.rows = rows.max(1);
                    let _ = s.cell.resize(s.cols, s.rows);
                }
            }
            ClientMsg::Spawn {
                kind,
                cmd,
                title,
                cwd,
                session,
                workspace,
                tab,
                target,
                server,
                ephemeral,
                cols,
                rows,
            } => self.handle_spawn(client, kind, cmd, title, cwd, session, workspace, tab, target, server, ephemeral, cols, rows),
            ClientMsg::Move {
                session,
                workspace,
                tab,
            } => self.handle_move(client, &session, workspace, tab),
            ClientMsg::SetPersist { session, persist } => self.handle_set_persist(client, &session, persist),
            ClientMsg::Kill { session } => {
                if self.sessions.remove(&session).is_some() {
                    // Explicit kill discards the persisted history too.
                    let _ = std::fs::remove_dir_all(session_dir(&session));
                    self.broadcast_all(&DaemonMsg::Exited { session });
                }
            }
            ClientMsg::Respawn { session } => self.handle_respawn(client, &session),
            ClientMsg::Ping { ts } => self.send(client, &DaemonMsg::Pong { ts }),
            ClientMsg::Shutdown => {
                self.send(client, &DaemonMsg::ShuttingDown);
                self.shutdown = true;
            }
        }
    }

    fn handle_attach(&mut self, client: u64, session: &str, cols: u16, rows: u16, replay: bool) {
        let Some(s) = self.sessions.get_mut(session) else {
            self.send(
                client,
                &DaemonMsg::Error {
                    code: "no_session".into(),
                    message: format!("no such session: {session}"),
                    session: Some(session.to_string()),
                },
            );
            return;
        };
        if !s.subscribers.contains(&client) {
            s.subscribers.push(client);
        }
        s.reap_at = None; // a client attached → cancel any pending ephemeral reap
        if cols > 0 && rows > 0 {
            s.cols = cols;
            s.rows = rows;
            let _ = s.cell.resize(cols, rows);
        }
        let snapshot = if replay && !s.scrollback.is_empty() {
            Some(DaemonMsg::Snapshot {
                session: session.to_string(),
                data: b64(&s.scrollback),
                cols: s.cols,
                rows: s.rows,
                seq: s.seq,
            })
        } else {
            None
        };
        let status = DaemonMsg::Status {
            session: session.to_string(),
            state: s.state.clone(),
            detail: None,
        };
        if let Some(snap) = snapshot {
            self.send(client, &snap);
        }
        self.send(client, &status);
    }

    #[allow(clippy::too_many_arguments)]
    fn handle_spawn(
        &mut self,
        client: u64,
        kind: String,
        cmd: Vec<String>,
        title: Option<String>,
        cwd: Option<String>,
        session: Option<String>,
        workspace: Option<String>,
        tab: Option<String>,
        target: Option<String>,
        server: Option<String>,
        ephemeral: bool,
        cols: u16,
        rows: u16,
    ) {
        let kind = if kind.is_empty() { "local".to_string() } else { kind };
        let id = session.unwrap_or_else(|| {
            let n = self.next_session;
            self.next_session += 1;
            format!("{kind}-{n:02}")
        });
        if self.sessions.contains_key(&id) {
            self.send(
                client,
                &DaemonMsg::Error {
                    code: "exists".into(),
                    message: format!("session already exists: {id}"),
                    session: Some(id),
                },
            );
            return;
        }
        let (cols, rows) = (cols.max(1).max(80), rows.max(1).max(24));
        let argv: Vec<String> = if kind == "local" && cmd.is_empty() {
            vec![default_shell()]
        } else {
            cmd
        };
        let title = title.unwrap_or_else(|| id.clone());
        match build_cell(self.next_session, &kind, &title, &argv, cwd.as_deref(), target.as_deref(), server.as_deref(), cols, rows) {
            Ok(cell) => {
                let mut sess = DaemonSession {
                    id: id.clone(),
                    cell,
                    title,
                    kind,
                    workspace: workspace.filter(|w| !w.is_empty()).unwrap_or_else(|| DEFAULT_WORKSPACE.to_string()),
                    tab: tab.filter(|t| !t.is_empty()).unwrap_or_else(|| DEFAULT_TAB.to_string()),
                    cmd: argv,
                    cwd,
                    target,
                    server,
                    cols,
                    rows,
                    seq: 0,
                    scrollback: Vec::new(),
                    subscribers: vec![client], // auto-attach the spawner
                    state: "working".to_string(),
                    log: None,
                    log_len: 0,
                    last_output_at: Instant::now(),
                    ephemeral,
                    reap_at: None,
                };
                // Ephemeral sessions are in-memory only (no disk persistence);
                // persistent ones write meta + scrollback so they survive a restart.
                if !ephemeral {
                    sess.persist_meta();
                    sess.open_log(true);
                }
                self.sessions.insert(id.clone(), sess);
                self.send(client, &DaemonMsg::Spawned { session: id.clone() });
                self.broadcast_all(&DaemonMsg::Status {
                    session: id,
                    state: "working".into(),
                    detail: None,
                });
            }
            Err(e) => self.send(
                client,
                &DaemonMsg::Error {
                    code: "spawn_failed".into(),
                    message: e.to_string(),
                    session: None,
                },
            ),
        }
    }

    /// Pin/unpin a session's lifetime. Pinning (persist) starts on-disk
    /// persistence (writing current scrollback) and cancels any reap; unpinning
    /// makes it ephemeral and drops its on-disk state.
    fn handle_set_persist(&mut self, client: u64, id: &str, persist: bool) {
        let Some(s) = self.sessions.get_mut(id) else {
            self.send(
                client,
                &DaemonMsg::Error {
                    code: "no_session".into(),
                    message: format!("no such session: {id}"),
                    session: Some(id.to_string()),
                },
            );
            return;
        };
        if persist {
            s.ephemeral = false;
            s.reap_at = None;
            s.persist_meta();
            s.open_log(true);
            let snapshot = s.scrollback.clone();
            s.append_log(&snapshot); // seed the on-disk log with current history
        } else {
            s.ephemeral = true;
            s.log = None;
            s.log_len = 0;
            let _ = std::fs::remove_dir_all(session_dir(id));
            if s.subscribers.is_empty() {
                s.reap_at = Some(Instant::now() + REAP_GRACE);
            }
        }
        // Reflect the change in any client's inventory on next poll.
        self.broadcast_all(&DaemonMsg::Status {
            session: id.to_string(),
            state: self.sessions.get(id).map(|s| s.state.clone()).unwrap_or_default(),
            detail: Some(if persist { "pinned".into() } else { "unpinned".into() }),
        });
    }

    /// Reap ephemeral sessions whose grace elapsed with no attached clients.
    fn reap_ephemeral(&mut self) {
        let now = Instant::now();
        let dead: Vec<String> = self
            .sessions
            .iter()
            .filter(|(_, s)| {
                s.ephemeral && s.subscribers.is_empty() && s.reap_at.map(|t| t <= now).unwrap_or(false)
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in dead {
            self.sessions.remove(&id);
            debug_log(&format!("reaped ephemeral session {id}"));
            self.broadcast_all(&DaemonMsg::Exited { session: id });
        }
    }

    /// Move a session into a workspace and/or tab (only provided fields change).
    fn handle_move(&mut self, client: u64, id: &str, workspace: Option<String>, tab: Option<String>) {
        match self.sessions.get_mut(id) {
            Some(s) => {
                if let Some(w) = workspace.filter(|w| !w.is_empty()) {
                    s.workspace = w;
                }
                if let Some(t) = tab.filter(|t| !t.is_empty()) {
                    s.tab = t;
                }
                s.persist_meta();
            }
            None => self.send(
                client,
                &DaemonMsg::Error {
                    code: "no_session".into(),
                    message: format!("no such session: {id}"),
                    session: Some(id.to_string()),
                },
            ),
        }
    }

    /// Re-run an exited session's command, preserving its scrollback.
    fn handle_respawn(&mut self, client: u64, id: &str) {
        let result = {
            let Some(s) = self.sessions.get_mut(id) else {
                self.send(
                    client,
                    &DaemonMsg::Error {
                        code: "no_session".into(),
                        message: format!("no such session: {id}"),
                        session: Some(id.to_string()),
                    },
                );
                return;
            };
            let argv: Vec<String> = if s.kind == "local" && s.cmd.is_empty() {
                vec![default_shell()]
            } else {
                s.cmd.clone()
            };
            // Local → re-run the command; external kinds → re-attach the backend.
            match build_cell(s.cell.id, &s.kind, &s.title, &argv, s.cwd.as_deref(), s.target.as_deref(), s.server.as_deref(), s.cols, s.rows) {
                Ok(cell) => {
                    s.cell = cell;
                    s.state = "working".to_string();
                    s.last_output_at = Instant::now();
                    Ok(())
                }
                Err(e) => Err(e.to_string()),
            }
        };
        match result {
            Ok(()) => self.broadcast_all(&DaemonMsg::Status {
                session: id.to_string(),
                state: "working".into(),
                detail: None,
            }),
            Err(message) => self.send(
                client,
                &DaemonMsg::Error {
                    code: "respawn_failed".into(),
                    message,
                    session: Some(id.to_string()),
                },
            ),
        }
    }

    /// Poll each session for new output, append to scrollback, and forward to
    /// subscribers. Detect EOF and mark exited.
    fn pump_sessions(&mut self) {
        let ids: Vec<String> = self.sessions.keys().cloned().collect();
        for id in ids {
            let (frame, exited, status_change) = {
                let Some(s) = self.sessions.get_mut(&id) else { continue };
                let bytes = s.cell.poll_collect().unwrap_or_default();
                let now = Instant::now();
                let frame = if bytes.is_empty() {
                    None
                } else {
                    s.seq += 1;
                    s.last_output_at = now;
                    append_capped(&mut s.scrollback, &bytes, SCROLLBACK_CAP);
                    s.append_log(&bytes);
                    Some(DaemonMsg::Output {
                        session: id.clone(),
                        data: b64(&bytes),
                        seq: s.seq,
                    })
                };
                let exited = s.cell.is_eof() && s.state != "exited";
                if exited {
                    s.state = "exited".to_string();
                }
                // Heuristic idle/working/blocked classification for live sessions.
                let status_change = if exited || s.state == "exited" {
                    None
                } else {
                    let quiescent = now.duration_since(s.last_output_at) >= IDLE_THRESHOLD;
                    let new_state = detect::classify(&s.cell.terminal.cursor_line(), quiescent).as_str();
                    if new_state != s.state {
                        s.state = new_state.to_string();
                        Some(new_state.to_string())
                    } else {
                        None
                    }
                };
                (frame, exited, status_change)
            };
            if let Some(frame) = frame {
                self.broadcast(&id, &frame);
            }
            if exited {
                self.broadcast_all(&DaemonMsg::Status {
                    session: id.clone(),
                    state: "exited".into(),
                    detail: None,
                });
            } else if let Some(state) = status_change {
                self.broadcast_all(&DaemonMsg::Status {
                    session: id.clone(),
                    state,
                    detail: None,
                });
            }
        }
    }

    // ── Wire helpers ──────────────────────────────────────────────────────────

    /// Send one message to a single client, dropping it on write failure.
    fn send(&mut self, client: u64, msg: &DaemonMsg) {
        let line = msg.to_line();
        let dead = match self.clients.get_mut(&client) {
            Some(stream) => stream.write_all(line.as_bytes()).and_then(|_| stream.flush()).is_err(),
            None => false,
        };
        if dead {
            self.drop_client(client);
        }
    }

    /// Send one message to every subscriber of a session.
    fn broadcast(&mut self, session: &str, msg: &DaemonMsg) {
        let subscribers = match self.sessions.get(session) {
            Some(s) => s.subscribers.clone(),
            None => return,
        };
        for client in subscribers {
            self.send(client, msg);
        }
    }

    /// Send one message to every connected client (lifecycle events).
    fn broadcast_all(&mut self, msg: &DaemonMsg) {
        let ids: Vec<u64> = self.clients.keys().copied().collect();
        for client in ids {
            self.send(client, msg);
        }
    }

    fn drop_client(&mut self, client: u64) {
        self.clients.remove(&client);
        for sess in self.sessions.values_mut() {
            sess.subscribers.retain(|c| *c != client);
        }
    }
}

/// Append `bytes` to `buf`, head-truncating so it never exceeds `cap`.
fn append_capped(buf: &mut Vec<u8>, bytes: &[u8], cap: usize) {
    buf.extend_from_slice(bytes);
    if buf.len() > cap {
        let overflow = buf.len() - cap;
        buf.drain(0..overflow);
    }
}

// ── Pidfile ───────────────────────────────────────────────────────────────────

struct PidGuard(PathBuf);

impl PidGuard {
    fn write() -> io::Result<Self> {
        let path = pid_path();
        std::fs::write(&path, format!("{}\n", std::process::id()))?;
        Ok(PidGuard(path))
    }
}

impl Drop for PidGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
        let _ = std::fs::remove_file(socket_path());
    }
}

/// True if a daemon is already listening on the control socket.
pub fn is_running() -> bool {
    UnixStream::connect(socket_path()).is_ok()
}

/// Path to the control socket (respects `$CHARON_SOCK`).
pub fn control_socket() -> PathBuf {
    socket_path()
}

/// Ensure a daemon is running, spawning `charond` (detached) if not. Blocks until
/// the control socket is accepting connections, or times out after ~3s.
pub fn ensure_running() -> io::Result<()> {
    if is_running() {
        return Ok(());
    }
    // Prefer the `charond` binary next to the current executable; fall back to PATH.
    let exe = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.join("charond")))
        .filter(|p| p.exists())
        .unwrap_or_else(|| PathBuf::from("charond"));
    Command::new(exe)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if is_running() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(50));
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "charond did not start within 3s",
    ))
}
