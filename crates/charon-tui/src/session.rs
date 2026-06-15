/// SessionCell — one live terminal: TerminalState + AnsiParser + Backend.

use crate::backend::{BoatPane, ByteStream, CharonPane, FleetServer, PtyCapture, TmuxPane};
use crate::daemon_client::DaemonClient;
use crate::parser::AnsiParser;
use crate::terminal::TerminalState;

use std::io;

#[allow(dead_code)] // session metadata; not all fields read in the TUI
pub struct SessionCell {
    pub terminal: TerminalState,
    pub parser: AnsiParser,
    pub backend: Box<dyn ByteStream>,
    pub title: String,
    pub id: u64,
    pub backend_type: BackendType,
    pub viewport_scroll: usize,
}

#[derive(Clone, Debug)]
#[allow(dead_code)] // backend kinds; not all are instantiated in every build
pub enum BackendType {
    LocalPty,
    TmuxPane { session_name: String },
    BoatPane { session_id: String },
    RemoteBoat { server_id: String, session_id: String },
    CharonPane { socket_path: String },
    DaemonPane { session_id: String },
}

impl SessionCell {
    /// Spawn a new session running `cmd` (e.g. ["bash"]).
    pub fn spawn(id: u64, title: &str, cmd: &[&str], width: u16, height: u16) -> io::Result<Self> {
        let pty = PtyCapture::spawn(cmd, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(pty),
            title: title.to_string(),
            id,
            backend_type: BackendType::LocalPty,
            viewport_scroll: 0,
        })
    }

    /// Attach to an existing tmux session.
    pub fn attach_tmux(id: u64, title: &str, session_name: &str, width: u16, height: u16) -> io::Result<Self> {
        let tmux = TmuxPane::attach(session_name, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(tmux),
            title: title.to_string(),
            id,
            backend_type: BackendType::TmuxPane { session_name: session_name.to_string() },
            viewport_scroll: 0,
        })
    }

    /// Attach to a charons-boat session directly.
    pub fn attach_boat(id: u64, title: &str, session_id: &str, width: u16, height: u16) -> io::Result<Self> {
        let boat = BoatPane::attach(session_id, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(boat),
            title: title.to_string(),
            id,
            backend_type: BackendType::BoatPane { session_id: session_id.to_string() },
            viewport_scroll: 0,
        })
    }

    pub fn attach_boat_socket(id: u64, title: &str, session_id: &str, socket_path: &str, width: u16, height: u16) -> io::Result<Self> {
        let boat = BoatPane::attach_socket(session_id, socket_path, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(boat),
            title: title.to_string(),
            id,
            backend_type: BackendType::BoatPane { session_id: session_id.to_string() },
            viewport_scroll: 0,
        })
    }

    /// Attach to a remote agent via SSH + boat protocol.
    pub fn attach_remote_boat(id: u64, title: &str, server: &FleetServer, session_id: &str, width: u16, height: u16) -> io::Result<Self> {
        let boat = BoatPane::attach_remote(server, session_id, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(boat),
            title: title.to_string(),
            id,
            backend_type: BackendType::RemoteBoat { server_id: server.id.clone(), session_id: session_id.to_string() },
            viewport_scroll: 0,
        })
    }

    /// Attach to a session owned by the `charond` daemon. The session keeps
    /// running in the daemon even after this client (and the whole TUI) exits —
    /// reattaching replays its scrollback. `replay` is always requested.
    pub fn attach_daemon(id: u64, title: &str, session_id: &str, socket_path: &str, width: u16, height: u16) -> io::Result<Self> {
        let client = DaemonClient::attach(std::path::Path::new(socket_path), session_id, width, height, true)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(client),
            title: title.to_string(),
            id,
            backend_type: BackendType::DaemonPane { session_id: session_id.to_string() },
            viewport_scroll: 0,
        })
    }

    pub fn attach_charon(id: u64, title: &str, socket_path: &str, width: u16, height: u16) -> io::Result<Self> {
        let charon = CharonPane::attach(socket_path, width, height)?;
        Ok(SessionCell {
            terminal: TerminalState::new(width, height),
            parser: AnsiParser::new(),
            backend: Box::new(charon),
            title: title.to_string(),
            id,
            backend_type: BackendType::CharonPane { socket_path: socket_path.to_string() },
            viewport_scroll: 0,
        })
    }

    /// Read any available bytes from backend and feed to VTE parser.
    pub fn poll(&mut self) -> io::Result<()> {
        self.poll_collect()?;
        Ok(())
    }

    /// Like [`poll`](Self::poll), but also returns the raw bytes read from the
    /// backend. Used by the daemon to fan out terminal output to remote clients.
    pub fn poll_collect(&mut self) -> io::Result<Vec<u8>> {
        let bytes = self.backend.read_available()?;
        if !bytes.is_empty() {
            self.parser.process(&bytes, &mut self.terminal);
            if self.viewport_scroll == 0 {
                self.terminal.dirty = true;
            }
        }
        Ok(bytes)
    }

    /// Forward keystrokes to the backend PTY.
    pub fn write(&mut self, data: &[u8]) -> io::Result<()> {
        match self.backend.write_bytes(data) {
            Ok(_) => Ok(()),
            Err(e) if matches!(e.kind(), io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset | io::ErrorKind::NotConnected) => Ok(()),
            Err(e) => Err(e),
        }
    }

    /// Resize the terminal and backend.
    pub fn resize(&mut self, width: u16, height: u16) -> io::Result<()> {
        self.terminal.resize(width, height);
        match self.backend.resize(width, height) {
            Ok(()) => Ok(()),
            Err(e) if matches!(e.kind(), io::ErrorKind::BrokenPipe | io::ErrorKind::ConnectionReset | io::ErrorKind::NotConnected) => Ok(()),
            Err(e) => Err(e),
        }
    }

    pub fn is_eof(&self) -> bool {
        self.backend.is_eof()
    }

    pub fn max_viewport_scroll(&self) -> usize {
        self.terminal.scrollback.len()
    }

    pub fn scroll_viewport_up(&mut self, lines: usize) {
        self.viewport_scroll = (self.viewport_scroll.saturating_add(lines)).min(self.max_viewport_scroll());
        self.terminal.dirty = true;
    }

    pub fn scroll_viewport_down(&mut self, lines: usize) {
        self.viewport_scroll = self.viewport_scroll.saturating_sub(lines);
        self.terminal.dirty = true;
    }

    pub fn reset_viewport_scroll(&mut self) {
        if self.viewport_scroll != 0 {
            self.viewport_scroll = 0;
            self.terminal.dirty = true;
        }
    }

    #[allow(dead_code)] // accessor kept for the type's interface
    pub fn tmux_session_name(&self) -> Option<&str> {
        match &self.backend_type {
            BackendType::TmuxPane { session_name } => Some(session_name.as_str()),
            _ => None,
        }
    }
}
