//! Client-side `ByteStream` over the `charond` control socket.
//!
//! `DaemonClient` attaches to a daemon-owned session and presents it through the
//! same [`ByteStream`](crate::backend::ByteStream) trait the TUI already uses for
//! local PTYs, tmux, and boat panes — so the render/grid code needs no changes.
//! Output (and the initial replay snapshot) arrive as raw terminal bytes; input
//! and resize are forwarded as protocol commands.

use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use base64::Engine;

use crate::backend::ByteStream;
use crate::protocol::{ClientMsg, DaemonMsg, SessionInfo, PROTO_VERSION};

pub struct DaemonClient {
    session: String,
    writer: UnixStream,
    rx: Receiver<Vec<u8>>,
    eof: Arc<AtomicBool>,
}

impl DaemonClient {
    /// Connect to the daemon and attach to `session`. With `replay`, the daemon
    /// first sends a scrollback snapshot so the pane repaints its prior state.
    pub fn attach(
        socket_path: &Path,
        session: &str,
        cols: u16,
        rows: u16,
        replay: bool,
    ) -> io::Result<Self> {
        let stream = UnixStream::connect(socket_path)?;
        let mut writer = stream.try_clone()?;
        send(
            &mut writer,
            &ClientMsg::Hello {
                proto: PROTO_VERSION,
                client: "tui".to_string(),
                pid: std::process::id(),
            },
        )?;
        send(
            &mut writer,
            &ClientMsg::Attach {
                session: session.to_string(),
                cols,
                rows,
                replay,
            },
        )?;

        let (tx, rx) = mpsc::channel();
        let eof = Arc::new(AtomicBool::new(false));
        let eof_thread = eof.clone();
        let want = session.to_string();
        thread::spawn(move || {
            let mut reader = BufReader::new(stream);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        let Ok(msg) = serde_json::from_str::<DaemonMsg>(line.trim()) else {
                            continue;
                        };
                        match msg {
                            DaemonMsg::Output { session, data, .. }
                            | DaemonMsg::Snapshot { session, data, .. }
                                if session == want =>
                            {
                                if let Ok(bytes) =
                                    base64::engine::general_purpose::STANDARD.decode(&data)
                                {
                                    if tx.send(bytes).is_err() {
                                        break;
                                    }
                                }
                            }
                            DaemonMsg::Exited { session } if session == want => break,
                            _ => {}
                        }
                    }
                }
            }
            eof_thread.store(true, Ordering::Relaxed);
        });

        Ok(DaemonClient {
            session: session.to_string(),
            writer,
            rx,
            eof,
        })
    }
}

impl ByteStream for DaemonClient {
    fn read_available(&mut self) -> io::Result<Vec<u8>> {
        let mut out = Vec::new();
        while let Ok(bytes) = self.rx.try_recv() {
            out.extend_from_slice(&bytes);
        }
        Ok(out)
    }

    fn write_bytes(&mut self, data: &[u8]) -> io::Result<usize> {
        send(
            &mut self.writer,
            &ClientMsg::Input {
                session: self.session.clone(),
                data: base64::engine::general_purpose::STANDARD.encode(data),
            },
        )?;
        Ok(data.len())
    }

    fn is_eof(&self) -> bool {
        self.eof.load(Ordering::Relaxed)
    }

    fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        send(
            &mut self.writer,
            &ClientMsg::Resize {
                session: self.session.clone(),
                cols: width,
                rows: height,
            },
        )
    }
}

fn send(stream: &mut UnixStream, msg: &ClientMsg) -> io::Result<()> {
    stream.write_all(msg.to_line().as_bytes())?;
    stream.flush()
}

/// One-shot: ask the daemon to spawn a local session and return its id.
/// `cmd` empty → the daemon spawns the default shell.
pub fn spawn_session(socket: &Path, cmd: &[String], cols: u16, rows: u16) -> io::Result<String> {
    let mut stream = UnixStream::connect(socket)?;
    send(&mut stream, &hello())?;
    send(
        &mut stream,
        &ClientMsg::Spawn {
            kind: "local".to_string(),
            cmd: cmd.to_vec(),
            title: None,
            cwd: None,
            session: None,
            cols,
            rows,
        },
    )?;
    let read = stream.try_clone()?;
    let mut reader = BufReader::new(read);
    let mut line = String::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        match serde_json::from_str::<DaemonMsg>(line.trim()) {
            Ok(DaemonMsg::Spawned { session }) => return Ok(session),
            Ok(DaemonMsg::Error { message, .. }) => {
                return Err(io::Error::new(io::ErrorKind::Other, message))
            }
            _ => {}
        }
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "no 'spawned' response from daemon",
    ))
}

/// One-shot: respawn an exited session (re-run its command, keep scrollback).
pub fn respawn_session(socket: &Path, session: &str) -> io::Result<()> {
    let mut stream = UnixStream::connect(socket)?;
    send(&mut stream, &hello())?;
    send(
        &mut stream,
        &ClientMsg::Respawn {
            session: session.to_string(),
        },
    )?;
    let read = stream.try_clone()?;
    let mut reader = BufReader::new(read);
    let mut line = String::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        match serde_json::from_str::<DaemonMsg>(line.trim()) {
            Ok(DaemonMsg::Status { session: s, state, .. }) if s == session && state == "working" => {
                return Ok(())
            }
            Ok(DaemonMsg::Error { message, .. }) => {
                return Err(io::Error::new(io::ErrorKind::Other, message))
            }
            _ => {}
        }
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "no respawn confirmation from daemon",
    ))
}

/// One-shot: fetch the daemon's session inventory.
pub fn list_sessions(socket: &Path) -> io::Result<Vec<SessionInfo>> {
    let mut stream = UnixStream::connect(socket)?;
    send(&mut stream, &hello())?;
    send(&mut stream, &ClientMsg::List)?;
    let read = stream.try_clone()?;
    let mut reader = BufReader::new(read);
    let mut line = String::new();
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        if let Ok(DaemonMsg::Inventory { sessions }) = serde_json::from_str::<DaemonMsg>(line.trim())
        {
            return Ok(sessions);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "no 'inventory' response from daemon",
    ))
}

fn hello() -> ClientMsg {
    ClientMsg::Hello {
        proto: PROTO_VERSION,
        client: "cli".to_string(),
        pid: std::process::id(),
    }
}
