/// Backend — ByteStream trait + PtyCapture + TmuxPipe implementations.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use base64::Engine;
use portable_pty::{CommandBuilder, NativePtySystem, PtySize, PtySystem};
use serde::Deserialize;
use serde_json::{json, Value};

// ── Fleet config ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct FleetAgent {
    pub name: String,
    #[serde(rename = "type", default)]
    pub agent_type: String,
    #[serde(default)]
    pub specialization: String,
    #[serde(default)]
    pub project: String,
    #[serde(default)]
    pub auto_start: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FleetServer {
    pub id: String,
    pub host: String,
    #[serde(default)]
    pub user: String,
    #[serde(default)]
    pub ssh_options: Vec<String>,
    #[serde(default = "default_boat_command")]
    pub boat_command: String,
    #[serde(default)]
    pub agents: Vec<FleetAgent>,
}

fn default_boat_command() -> String {
    "charons-boat stream".to_string()
}

#[derive(Debug, Deserialize)]
struct FleetConfig {
    #[serde(default)]
    servers: Vec<FleetServer>,
}

/// Load fleet config from ~/.charon/fleet.json.
pub fn load_fleet_config() -> Vec<FleetServer> {
    let path = dirs_home().join(".charon/fleet.json");
    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return vec![],
    };
    let config: FleetConfig = match serde_json::from_str(&content) {
        Ok(c) => c,
        Err(_) => return vec![],
    };
    config.servers
}

// ── ByteStream trait ────────────────────────────────────────────────────────

pub trait ByteStream: Send {
    /// Read any available bytes (non-blocking). Returns empty vec if nothing ready.
    fn read_available(&mut self) -> io::Result<Vec<u8>>;
    /// Write bytes to the backend (keystrokes → PTY child).
    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize>;
    /// Check if the backend stream has ended.
    fn is_eof(&self) -> bool;
    /// Resize the backend terminal dimensions.
    fn resize(&mut self, width: u16, height: u16) -> io::Result<()>;
}

// ── PtyCapture ──────────────────────────────────────────────────────────────

enum ReaderMsg {
    Data(Vec<u8>),
    Eof,
}

pub struct PtyCapture {
    writer: Box<dyn Write + Send>,
    rx: mpsc::Receiver<ReaderMsg>,
    pty_pair: Box<dyn portable_pty::MasterPty + Send>,
    eof: bool,
}

impl PtyCapture {
    /// Spawn a new child process inside a PTY.
    pub fn spawn(cmd: &[&str], width: u16, height: u16) -> io::Result<Self> {
        let pty_system = NativePtySystem::default();

        let pair = pty_system
            .openpty(PtySize {
                rows: height,
                cols: width,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;

        let mut command = CommandBuilder::new(cmd[0]);
        for arg in &cmd[1..] {
            command.arg(arg);
        }
        command.env("TERM", "xterm-256color");

        let _child = pair
            .slave
            .spawn_command(command)
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;

        let mut reader = pair
            .master
            .try_clone_reader()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;

        let writer = pair
            .master
            .take_writer()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))?;

        let (tx, rx) = mpsc::channel();

        thread::spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => { let _ = tx.send(ReaderMsg::Eof); break; }
                    Ok(n) => {
                        if tx.send(ReaderMsg::Data(buf[..n].to_vec())).is_err() { break; }
                    }
                    Err(_) => { let _ = tx.send(ReaderMsg::Eof); break; }
                }
            }
        });

        Ok(PtyCapture { writer, rx, pty_pair: pair.master, eof: false })
    }
}

impl ByteStream for PtyCapture {
    fn read_available(&mut self) -> io::Result<Vec<u8>> {
        if self.eof { return Ok(vec![]); }
        let mut all = Vec::new();
        loop {
            match self.rx.try_recv() {
                Ok(ReaderMsg::Data(data)) => all.extend(data),
                Ok(ReaderMsg::Eof) => { self.eof = true; break; }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => { self.eof = true; break; }
            }
        }
        Ok(all)
    }

    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize> {
        self.writer.write(data)
    }

    fn is_eof(&self) -> bool { self.eof }

    fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        self.pty_pair
            .resize(PtySize { rows: height, cols: width, pixel_width: 0, pixel_height: 0 })
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e.to_string()))
    }
}

// ── TmuxPane — attach to existing tmux session via capture + send-keys ──────
//
// tmux pipe-pane would be ideal for continuous byte streams, but it has
// limitations: output-only (no resize), needs cleanup, and the pipe doesn't
// carry the initial screen state. Instead we use a hybrid approach:
//
// 1. Initial snapshot via `tmux capture-pane -t <session> -p -e` (with ANSI escapes)
// 2. Continuous polling via capture-pane at high frequency for the focused pane
// 3. Input via writing to the pane's TTY device (raw bytes, not send-keys)
// 4. Resize via `tmux resize-window`
//
// This gives us full ANSI output (colors, cursor sequences) unlike the old
// approach that stripped ANSI. The VTE parser handles the rest.

pub struct TmuxPane {
    session_name: String,
    rx: mpsc::Receiver<ReaderMsg>,
    eof: bool,
    pane_tty: Option<String>,
    poll_handle: Option<thread::JoinHandle<()>>,
}

impl TmuxPane {
    /// Attach to an existing tmux session.
    pub fn attach(session_name: &str, width: u16, height: u16) -> io::Result<Self> {
        // Verify the session exists
        let check = Command::new("tmux")
            .args(["has-session", "-t", session_name])
            .output();
        match check {
            Ok(out) if out.status.success() => {}
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("tmux session '{}' not found", session_name),
                ));
            }
        }

        // Resize the tmux window to match our cell dimensions
        let _ = Command::new("tmux")
            .args([
                "resize-window", "-t", session_name,
                "-x", &width.to_string(), "-y", &height.to_string(),
            ])
            .output();

        // Get the pane TTY for raw input
        let pane_tty = Command::new("tmux")
            .args(["display-message", "-t", session_name, "-p", "#{pane_tty}"])
            .output()
            .ok()
            .and_then(|o| {
                if o.status.success() {
                    let tty = String::from_utf8_lossy(&o.stdout).trim().to_string();
                    if !tty.is_empty() && std::path::Path::new(&tty).exists() {
                        Some(tty)
                    } else {
                        None
                    }
                } else {
                    None
                }
            });

        let (tx, rx) = mpsc::channel();
        let sname = session_name.to_string();

        // Polling thread: capture-pane with ANSI escapes at high frequency
        let poll_handle = thread::spawn(move || {
            // First, do an initial full capture
            let mut last_content = String::new();

            loop {
                // capture-pane with -e flag preserves ANSI escape sequences
                let result = Command::new("tmux")
                    .args(["capture-pane", "-t", &sname, "-p", "-e"])
                    .output();

                match result {
                    Ok(out) if out.status.success() => {
                        let content = String::from_utf8_lossy(&out.stdout).to_string();
                        if content != last_content {
                            // Send the full screen as ANSI bytes — the VTE parser
                            // will handle cursor positioning via the escape sequences.
                            // We prepend a clear-screen + home to reset state cleanly.
                            let mut bytes = Vec::new();
                            // ESC[2J (clear screen) + ESC[H (cursor home)
                            bytes.extend_from_slice(b"\x1b[2J\x1b[H");
                            bytes.extend_from_slice(content.as_bytes());
                            if tx.send(ReaderMsg::Data(bytes)).is_err() {
                                break;
                            }
                            last_content = content;
                        }
                    }
                    Ok(_) => {
                        // Session probably died
                        let _ = tx.send(ReaderMsg::Eof);
                        break;
                    }
                    Err(_) => {
                        let _ = tx.send(ReaderMsg::Eof);
                        break;
                    }
                }

                thread::sleep(Duration::from_millis(100));
            }
        });

        Ok(TmuxPane {
            session_name: session_name.to_string(),
            rx,
            eof: false,
            pane_tty,
            poll_handle: Some(poll_handle),
        })
    }
}

impl ByteStream for TmuxPane {
    fn read_available(&mut self) -> io::Result<Vec<u8>> {
        if self.eof { return Ok(vec![]); }
        let mut all = Vec::new();
        loop {
            match self.rx.try_recv() {
                Ok(ReaderMsg::Data(data)) => all.extend(data),
                Ok(ReaderMsg::Eof) => { self.eof = true; break; }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => { self.eof = true; break; }
            }
        }
        Ok(all)
    }

    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize> {
        let special = match data {
            [b'\r'] | [b'\n'] => Some("Enter"),
            b"\x1b[A" => Some("Up"),
            b"\x1b[B" => Some("Down"),
            b"\x1b[C" => Some("Right"),
            b"\x1b[D" => Some("Left"),
            b"\x1b[3~" => Some("DC"),
            b"\t" => Some("Tab"),
            _ => None,
        };

        // Prefer writing directly to the pane TTY for raw byte fidelity.
        // This especially matters for Backspace, where some TUIs/shells want the
        // literal byte instead of a tmux key name translation.
        if let Some(ref tty) = self.pane_tty {
            if let Ok(mut f) = std::fs::OpenOptions::new().write(true).open(tty) {
                if let Ok(written) = f.write(data) {
                    if written > 0 {
                        return Ok(written);
                    }
                }
            }
        }

        if let Some(key_name) = special {
            let status = Command::new("tmux")
                .args(["send-keys", "-t", &self.session_name, key_name])
                .stdin(Stdio::null())
                .output()
                .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
            if status.status.success() {
                return Ok(data.len());
            }
        }
        // Fallback: use tmux send-keys with literal flag
        let text = String::from_utf8_lossy(data);
        let status = Command::new("tmux")
            .args(["send-keys", "-t", &self.session_name, "-l", &text])
            .stdin(Stdio::null())
            .output()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
        if status.status.success() {
            Ok(data.len())
        } else {
            Err(io::Error::new(io::ErrorKind::Other, "tmux send-keys failed"))
        }
    }

    fn is_eof(&self) -> bool { self.eof }

    fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        let status = Command::new("tmux")
            .args([
                "resize-window", "-t", &self.session_name,
                "-x", &width.to_string(), "-y", &height.to_string(),
            ])
            .output()
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
        if status.status.success() {
            Ok(())
        } else {
            Err(io::Error::new(io::ErrorKind::Other, "tmux resize-window failed"))
        }
    }
}

// ── BoatPane — direct charons-boat stream transport ───────────────────────

enum BoatConn {
    StreamProc { _child: Child, stdin: ChildStdin },
    Socket(UnixStream),
}

pub struct BoatPane {
    conn: BoatConn,
    rx: mpsc::Receiver<ReaderMsg>,
    eof: bool,
    session_id: String,
}

impl BoatPane {
    pub fn attach(session_id: &str, width: u16, height: u16) -> io::Result<Self> {
        if let Some(sock_path) = boat_socket_for(session_id) {
            return Self::attach_socket(session_id, &sock_path, width, height);
        }

        let script = project_root().join("tools/charons-boat/charons-boat");
        let mut child = Command::new(&script)
            .arg("stream")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;

        let stdin = child.stdin.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing boat stdin"))?;
        let stdout = child.stdout.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing boat stdout"))?;
        let (tx, rx) = mpsc::channel();
        let session = session_id.to_string();
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() { continue; }
                        let Ok(v) = serde_json::from_str::<Value>(trimmed) else { continue; };
                        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
                        match typ {
                            "output" => {
                                let sid = v.get("session").and_then(|x| x.as_str()).unwrap_or("");
                                if sid == session {
                                    let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                                    if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(data) {
                                        if tx.send(ReaderMsg::Data(decoded)).is_err() { return; }
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = tx.send(ReaderMsg::Eof);
        });

        let mut pane = Self { conn: BoatConn::StreamProc { _child: child, stdin }, rx, eof: false, session_id: session_id.to_string() };
        pane.send(json!({"type": "resize", "session": session_id, "cols": width, "rows": height}))?;
        pane.send(json!({"type": "focus", "session": session_id}))?;
        Ok(pane)
    }

    pub fn attach_socket(session_id: &str, socket_path: &str, width: u16, height: u16) -> io::Result<Self> {
        let mut stream = UnixStream::connect(socket_path)?;
        let reader_stream = stream.try_clone()?;
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            let reader = BufReader::new(reader_stream);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() { continue; }
                        let Ok(v) = serde_json::from_str::<Value>(trimmed) else { continue; };
                        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
                        if typ == "output" {
                            let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                            if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(data) {
                                if tx.send(ReaderMsg::Data(decoded)).is_err() { return; }
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = tx.send(ReaderMsg::Eof);
        });
        let sub = json!({"type": "subscribe"}).to_string() + "\n";
        stream.write_all(sub.as_bytes())?;
        let resize = json!({"type": "resize", "cols": width, "rows": height}).to_string() + "\n";
        stream.write_all(resize.as_bytes())?;
        stream.flush()?;
        Ok(Self { conn: BoatConn::Socket(stream), rx, eof: false, session_id: session_id.to_string() })
    }

    /// Connect to a remote server via SSH and speak the boat protocol over stdin/stdout.
    pub fn attach_remote(server: &FleetServer, session_id: &str, width: u16, height: u16) -> io::Result<Self> {
        let mut cmd = Command::new("ssh");

        // SSH options from fleet config
        for opt in &server.ssh_options {
            cmd.arg(opt);
        }

        // Ensure non-interactive and fast timeout
        cmd.args(["-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]);

        // Target: user@host
        let target = if server.user.is_empty() {
            server.host.clone()
        } else {
            format!("{}@{}", server.user, server.host)
        };
        cmd.arg(&target);

        // Remote command: charons-boat stream
        for part in server.boat_command.split_whitespace() {
            cmd.arg(part);
        }

        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| io::Error::new(io::ErrorKind::ConnectionRefused,
                format!("SSH to {} failed: {}", server.host, e)))?;

        let stdin = child.stdin.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing ssh stdin"))?;
        let stdout = child.stdout.take().ok_or_else(|| io::Error::new(io::ErrorKind::Other, "missing ssh stdout"))?;
        let (tx, rx) = mpsc::channel();
        let session = session_id.to_string();
        thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() { continue; }
                        let Ok(v) = serde_json::from_str::<Value>(trimmed) else { continue; };
                        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
                        match typ {
                            "output" => {
                                let sid = v.get("session").and_then(|x| x.as_str()).unwrap_or("");
                                if sid == session {
                                    let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                                    if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(data) {
                                        if tx.send(ReaderMsg::Data(decoded)).is_err() { return; }
                                    }
                                }
                            }
                            _ => {}
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = tx.send(ReaderMsg::Eof);
        });

        let mut pane = Self { conn: BoatConn::StreamProc { _child: child, stdin }, rx, eof: false, session_id: session_id.to_string() };
        pane.send(json!({"type": "resize", "session": session_id, "cols": width, "rows": height}))?;
        pane.send(json!({"type": "focus", "session": session_id}))?;
        Ok(pane)
    }

    fn send(&mut self, value: Value) -> io::Result<()> {
        let line = serde_json::to_string(&value)?;
        match &mut self.conn {
            BoatConn::StreamProc { stdin, .. } => {
                stdin.write_all(line.as_bytes())?;
                stdin.write_all(b"\n")?;
                stdin.flush()?;
            }
            BoatConn::Socket(stream) => {
                stream.write_all(line.as_bytes())?;
                stream.write_all(b"\n")?;
                stream.flush()?;
            }
        }
        Ok(())
    }
}

impl ByteStream for BoatPane {
    fn read_available(&mut self) -> io::Result<Vec<u8>> {
        if self.eof { return Ok(vec![]); }
        let mut all = Vec::new();
        loop {
            match self.rx.try_recv() {
                Ok(ReaderMsg::Data(data)) => all.extend(data),
                Ok(ReaderMsg::Eof) => { self.eof = true; break; }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => { self.eof = true; break; }
            }
        }
        Ok(all)
    }

    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize> {
        let encoded = base64::engine::general_purpose::STANDARD.encode(data);
        self.send(json!({"type": "input", "session": self.session_id, "data": encoded}))?;
        Ok(data.len())
    }

    fn is_eof(&self) -> bool { self.eof }

    fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        self.send(json!({"type": "resize", "session": self.session_id, "cols": width, "rows": height}))
    }
}

fn project_root() -> PathBuf {
    if let Ok(root) = std::env::var("CHARON_ROOT") {
        let path = PathBuf::from(root);
        if path.exists() {
            return path;
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        for anc in exe.ancestors() {
            let marker = anc.join("apps").join("core-daemon");
            if marker.exists() {
                return anc.to_path_buf();
            }
        }
    }

    PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf()
}

fn boat_socket_for(session_id: &str) -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let path = PathBuf::from(home).join(".charon/boats").join(format!("{}.json", session_id));
    let content = std::fs::read_to_string(path).ok()?;
    let v: Value = serde_json::from_str(&content).ok()?;
    if v.get("transport").and_then(|x| x.as_str()) != Some("pty") {
        return None;
    }
    let sock = v.get("socket").and_then(|x| x.as_str())?.to_string();
    if std::path::Path::new(&sock).exists() { Some(sock) } else { None }
}

// ── CharonPane — direct native charon session socket ──────────────────────

pub struct CharonPane {
    stream: UnixStream,
    rx: mpsc::Receiver<ReaderMsg>,
    eof: bool,
}

impl CharonPane {
    pub fn attach(socket_path: &str, width: u16, height: u16) -> io::Result<Self> {
        let mut stream = UnixStream::connect(socket_path)?;
        let reader_stream = stream.try_clone()?;
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            let reader = BufReader::new(reader_stream);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        let trimmed = line.trim();
                        if trimmed.is_empty() { continue; }
                        let Ok(v) = serde_json::from_str::<Value>(trimmed) else { continue; };
                        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
                        if typ == "output" {
                            let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                            if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(data) {
                                if tx.send(ReaderMsg::Data(decoded)).is_err() { return; }
                            }
                        }
                    }
                    Err(_) => break,
                }
            }
            let _ = tx.send(ReaderMsg::Eof);
        });
        let sub = json!({"type": "subscribe"}).to_string() + "\n";
        stream.write_all(sub.as_bytes())?;
        let resize = json!({"type": "resize", "cols": width, "rows": height}).to_string() + "\n";
        stream.write_all(resize.as_bytes())?;
        stream.flush()?;
        Ok(Self { stream, rx, eof: false })
    }

    fn send(&mut self, value: Value) -> io::Result<()> {
        let line = serde_json::to_string(&value)?;
        match self.stream.write_all(line.as_bytes())
            .and_then(|_| self.stream.write_all(b"\n"))
            .and_then(|_| self.stream.flush()) {
            Ok(()) => Ok(()),
            Err(e) => {
                if matches!(e.kind(), io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset | io::ErrorKind::NotConnected) {
                    self.eof = true;
                }
                Err(e)
            }
        }
    }
}

impl ByteStream for CharonPane {
    fn read_available(&mut self) -> io::Result<Vec<u8>> {
        if self.eof { return Ok(vec![]); }
        let mut all = Vec::new();
        loop {
            match self.rx.try_recv() {
                Ok(ReaderMsg::Data(data)) => all.extend(data),
                Ok(ReaderMsg::Eof) => { self.eof = true; break; }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => { self.eof = true; break; }
            }
        }
        Ok(all)
    }

    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize> {
        let encoded = base64::engine::general_purpose::STANDARD.encode(data);
        self.send(json!({"type": "input", "data": encoded}))?;
        Ok(data.len())
    }

    fn is_eof(&self) -> bool { self.eof }

    fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        self.send(json!({"type": "resize", "cols": width, "rows": height}))
    }
}

// ── Discovery — find tmux sessions to attach to ────────────────────────────

#[derive(Debug, Clone)]
pub struct DiscoveredSession {
    pub session_name: String,
    pub display_name: String,
    pub agent_type: String,
    pub transport: String,
    /// Set for remote-boat sessions: the fleet server ID.
    pub server_id: Option<String>,
}

/// Discover tmux sessions that can be attached to.
/// Prioritizes charons-boat registered sessions, then any tmux session.
pub fn discover_sessions() -> Vec<DiscoveredSession> {
    let mut sessions = Vec::new();

    // 1. Check charons-boat registrations
    let boat_dir = dirs_home().join(".charon/boats");
    if boat_dir.is_dir() {
        if let Ok(entries) = std::fs::read_dir(&boat_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map(|e| e == "json").unwrap_or(false) {
                    if let Ok(content) = std::fs::read_to_string(&path) {
                        if let Some(sess) = parse_boat_registration(&content) {
                            sessions.push(sess);
                        }
                    }
                }
            }
        }
    }

    let registered_names: std::collections::HashSet<String> =
        sessions.iter().map(|s| s.session_name.clone()).collect();

    // 2. List all tmux sessions, add any not already registered
    if let Ok(output) = Command::new("tmux")
        .args(["list-sessions", "-F", "#{session_name}"])
        .output()
    {
        if output.status.success() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            for line in stdout.lines() {
                let name = line.trim();
                if !name.is_empty() && !registered_names.contains(name) {
                    sessions.push(DiscoveredSession {
                        session_name: name.to_string(),
                        display_name: name.to_string(),
                        agent_type: "tmux".to_string(),
                        transport: "tmux".to_string(),
                        server_id: None,
                    });
                }
            }
        }
    }

    // Verify each local session still exists
    sessions.retain(|s| {
        if s.server_id.is_some() { return true; } // remote sessions skip tmux check
        Command::new("tmux")
            .args(["has-session", "-t", &s.session_name])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    });

    // 3. Fleet remote sessions
    for server in load_fleet_config() {
        for agent in &server.agents {
            let session_name = format!("{}:{}", server.id, agent.name);
            if !sessions.iter().any(|s| s.session_name == session_name) {
                sessions.push(DiscoveredSession {
                    session_name,
                    display_name: format!("{} @ {}", agent.name, server.id),
                    agent_type: agent.agent_type.clone(),
                    transport: "remote-boat".to_string(),
                    server_id: Some(server.id.clone()),
                });
            }
        }
    }

    sessions
}

fn parse_boat_registration(content: &str) -> Option<DiscoveredSession> {
    // Simple JSON parsing without serde — extract "session" and "name" fields
    let session = extract_json_string(content, "session")?;
    let name = extract_json_string(content, "name").unwrap_or_else(|| session.clone());
    let command = extract_json_string(content, "command").unwrap_or_default();

    // Derive agent type from command
    let agent = if command.contains("hermes") {
        "hermes"
    } else if command.contains("pi") {
        "pi"
    } else if command.contains("opencode") {
        "opencode"
    } else if command.contains("claude") {
        "claude"
    } else {
        "boat"
    };

    Some(DiscoveredSession {
        session_name: session,
        display_name: name,
        agent_type: agent.to_string(),
        transport: "boat".to_string(),
        server_id: None,
    })
}

fn extract_json_string(json: &str, key: &str) -> Option<String> {
    let pattern = format!("\"{}\"", key);
    let start = json.find(&pattern)?;
    let after_key = &json[start + pattern.len()..];
    // Skip whitespace and colon
    let after_colon = after_key.find(':')?;
    let value_part = &after_key[after_colon + 1..];
    // Find the opening quote
    let open = value_part.find('"')?;
    let value_start = &value_part[open + 1..];
    // Find the closing quote (handle escaped quotes)
    let mut end = 0;
    let chars: Vec<char> = value_start.chars().collect();
    while end < chars.len() {
        if chars[end] == '\\' {
            end += 2;
        } else if chars[end] == '"' {
            break;
        } else {
            end += 1;
        }
    }
    Some(value_start[..value_start.char_indices().nth(end)?.0].to_string())
}

fn dirs_home() -> std::path::PathBuf {
    std::env::var("HOME")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::path::PathBuf::from("/tmp"))
}
