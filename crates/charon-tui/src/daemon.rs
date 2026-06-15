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
use crate::protocol::{ClientMsg, DaemonMsg, SessionInfo, PROTO_VERSION};
use crate::session::SessionCell;

/// Per-session raw scrollback cap, in bytes. Bounds memory + replay payload size.
const SCROLLBACK_CAP: usize = 2 * 1024 * 1024; // 2 MiB
/// Main loop tick: drain client commands + poll sessions for output.
const TICK: Duration = Duration::from_millis(8);

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
    cmd: Vec<String>,
    #[serde(default)]
    cwd: Option<String>,
    cols: u16,
    rows: u16,
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
    /// Argv used to spawn the session; replayed on respawn after a restart.
    cmd: Vec<String>,
    cwd: Option<String>,
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
}

impl DaemonSession {
    fn info(&self) -> SessionInfo {
        SessionInfo {
            id: self.id.clone(),
            title: self.title.clone(),
            kind: self.kind.clone(),
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
            cmd: self.cmd.clone(),
            cwd: self.cwd.clone(),
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
}

impl Daemon {
    fn new() -> Self {
        Daemon {
            sessions: HashMap::new(),
            clients: HashMap::new(),
            next_session: 1,
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
            if let Some(n) = meta
                .id
                .strip_prefix("local-")
                .and_then(|s| s.parse::<u64>().ok())
            {
                max_local = max_local.max(n);
            }
            let (cols, rows) = (meta.cols.max(1), meta.rows.max(1));
            let mut sess = DaemonSession {
                id: meta.id.clone(),
                cell: SessionCell::dead(0, &meta.title, cols, rows),
                title: meta.title,
                kind: meta.kind,
                cmd: meta.cmd,
                cwd: meta.cwd,
                cols,
                rows,
                seq: 0,
                scrollback,
                subscribers: Vec::new(),
                state: "exited".to_string(),
                log: None,
                log_len: 0,
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
            // 2. Poll every session for output and fan it out.
            self.pump_sessions();
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
                for sess in self.sessions.values_mut() {
                    sess.subscribers.retain(|c| *c != id);
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
                cols,
                rows,
            } => self.handle_spawn(client, kind, cmd, title, cwd, session, cols, rows),
            ClientMsg::Kill { session } => {
                if self.sessions.remove(&session).is_some() {
                    // Explicit kill discards the persisted history too.
                    let _ = std::fs::remove_dir_all(session_dir(&session));
                    self.broadcast_all(&DaemonMsg::Exited { session });
                }
            }
            ClientMsg::Respawn { session } => self.handle_respawn(client, &session),
            ClientMsg::Ping { ts } => self.send(client, &DaemonMsg::Pong { ts }),
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
        cols: u16,
        rows: u16,
    ) {
        let kind = if kind.is_empty() { "local".to_string() } else { kind };
        if kind != "local" {
            self.send(
                client,
                &DaemonMsg::Error {
                    code: "unsupported_kind".into(),
                    message: format!("spawn kind '{kind}' not supported yet"),
                    session: None,
                },
            );
            return;
        }
        let id = session.unwrap_or_else(|| {
            let n = self.next_session;
            self.next_session += 1;
            format!("local-{n:02}")
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
        let argv: Vec<String> = if cmd.is_empty() {
            vec![default_shell()]
        } else {
            cmd
        };
        let argv_ref: Vec<&str> = argv.iter().map(String::as_str).collect();
        let title = title.unwrap_or_else(|| id.clone());
        match SessionCell::spawn_cwd(self.next_session, &title, &argv_ref, cols, rows, cwd.as_deref()) {
            Ok(cell) => {
                let mut sess = DaemonSession {
                    id: id.clone(),
                    cell,
                    title,
                    kind,
                    cmd: argv,
                    cwd,
                    cols,
                    rows,
                    seq: 0,
                    scrollback: Vec::new(),
                    subscribers: vec![client], // auto-attach the spawner
                    state: "working".to_string(),
                    log: None,
                    log_len: 0,
                };
                sess.persist_meta();
                sess.open_log(true);
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
            let argv: Vec<String> = if s.cmd.is_empty() {
                vec![default_shell()]
            } else {
                s.cmd.clone()
            };
            let argv_ref: Vec<&str> = argv.iter().map(String::as_str).collect();
            match SessionCell::spawn_cwd(s.cell.id, &s.title, &argv_ref, s.cols, s.rows, s.cwd.as_deref()) {
                Ok(cell) => {
                    s.cell = cell;
                    s.state = "working".to_string();
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
            let (frame, exited) = {
                let Some(s) = self.sessions.get_mut(&id) else { continue };
                let bytes = s.cell.poll_collect().unwrap_or_default();
                let frame = if bytes.is_empty() {
                    None
                } else {
                    s.seq += 1;
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
                (frame, exited)
            };
            if let Some(frame) = frame {
                self.broadcast(&id, &frame);
            }
            if exited {
                self.broadcast(
                    &id,
                    &DaemonMsg::Status {
                        session: id.clone(),
                        state: "exited".into(),
                        detail: None,
                    },
                );
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
