use std::fs;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;

use base64::Engine;
use serde_json::json;

pub enum NativeCommand {
    Input(Vec<u8>),
    /// A client asked for a resize; the requested size itself is delivered
    /// via [`NativeSessionServer::requested_size`].
    Resize,
}

#[derive(Clone)]
pub struct NativeSessionServer {
    session_id: String,
    name: String,
    sock_path: PathBuf,
    reg_path: PathBuf,
    clients: Arc<Mutex<Vec<UnixStream>>>,
    snapshot: Arc<Mutex<Vec<u8>>>,
    requested_size: Arc<Mutex<Option<(u16, u16)>>>,
    rx: Arc<Mutex<Receiver<NativeCommand>>>,
}

impl NativeSessionServer {
    pub fn start(name: Option<String>) -> io::Result<Self> {
        if std::env::var("CHARON_BOAT_WRAPPED").ok().as_deref() == Some("1") {
            return Err(io::Error::new(io::ErrorKind::Other, "native session disabled inside boat wrapper"));
        }
        let base = boats_dir();
        let sock_dir = base.join("sockets");
        fs::create_dir_all(&sock_dir)?;
        fs::create_dir_all(&base)?;
        let name = name.unwrap_or_else(default_name);
        let session_id = format!("charon-rust-{}", name);
        let sock_path = sock_dir.join(format!("{}.sock", session_id));
        let reg_path = base.join(format!("{}.json", session_id));
        let _ = fs::remove_file(&sock_path);

        let listener = UnixListener::bind(&sock_path)?;
        let clients = Arc::new(Mutex::new(Vec::new()));
        let snapshot = Arc::new(Mutex::new(Vec::new()));
        let requested_size = Arc::new(Mutex::new(None));
        let (tx, rx): (Sender<NativeCommand>, Receiver<NativeCommand>) = mpsc::channel();
        let rx = Arc::new(Mutex::new(rx));
        let clients_thread = clients.clone();
        let snapshot_thread = snapshot.clone();
        let requested_size_thread = requested_size.clone();
        let tx_thread = tx.clone();
        let sid = session_id.clone();

        thread::spawn(move || {
            for conn in listener.incoming() {
                let Ok(mut stream) = conn else { break; };
                let Ok(clone) = stream.try_clone() else { continue; };
                let sid_inner = sid.clone();
                let snap_inner = snapshot_thread.clone();
                let clients_inner = clients_thread.clone();
                let requested_size_inner = requested_size_thread.clone();
                let tx_inner = tx_thread.clone();
                thread::spawn(move || {
                    let reader_stream = match clone.try_clone() { Ok(s) => s, Err(_) => return };
                    let mut reader = BufReader::new(reader_stream);
                    let mut line = String::new();
                    loop {
                        line.clear();
                        let Ok(n) = reader.read_line(&mut line) else { break; };
                        if n == 0 { break; }
                        let trimmed = line.trim();
                        if trimmed.is_empty() { continue; }
                        let Ok(v) = serde_json::from_str::<serde_json::Value>(trimmed) else { continue; };
                        let typ = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
                        if typ == "subscribe" {
                            if let Ok(bytes) = snap_inner.lock().map(|b| b.clone()) {
                                if !bytes.is_empty() {
                                    let msg = json!({
                                        "type": "output",
                                        "session": sid_inner,
                                        "data": base64::engine::general_purpose::STANDARD.encode(bytes),
                                    });
                                    let _ = stream.write_all(serde_json::to_string(&msg).unwrap_or_default().as_bytes());
                                    let _ = stream.write_all(b"\n");
                                }
                            }
                            let status = json!({"type": "status", "session": sid_inner, "status": "running"});
                            let _ = stream.write_all(serde_json::to_string(&status).unwrap_or_default().as_bytes());
                            let _ = stream.write_all(b"\n");
                            let _ = stream.flush();
                            if let Ok(cl) = stream.try_clone() {
                                if let Ok(mut clients) = clients_inner.lock() {
                                    clients.push(cl);
                                }
                            }
                        } else if typ == "input" {
                            let data = v.get("data").and_then(|x| x.as_str()).unwrap_or("");
                            if let Ok(decoded) = base64::engine::general_purpose::STANDARD.decode(data) {
                                let _ = tx_inner.send(NativeCommand::Input(decoded));
                            }
                        } else if typ == "resize" {
                            let cols = v.get("cols").and_then(|x| x.as_u64()).unwrap_or(0) as u16;
                            let rows = v.get("rows").and_then(|x| x.as_u64()).unwrap_or(0) as u16;
                            if let Ok(mut size) = requested_size_inner.lock() {
                                *size = Some((cols.max(1), rows.max(1)));
                            }
                            let _ = tx_inner.send(NativeCommand::Resize);
                        }
                    }
                });
            }
        });

        let server = Self { session_id, name, sock_path, reg_path, clients, snapshot, requested_size, rx };
        server.write_registration()?;
        Ok(server)
    }

    pub fn name(&self) -> &str { &self.name }
    pub fn socket_path(&self) -> &std::path::Path { &self.sock_path }

    pub fn drain_commands(&self) -> Vec<NativeCommand> {
        let mut out = Vec::new();
        if let Ok(rx) = self.rx.lock() {
            while let Ok(cmd) = rx.try_recv() {
                out.push(cmd);
            }
        }
        out
    }

    pub fn requested_size(&self) -> Option<(u16, u16)> {
        self.requested_size.lock().ok().and_then(|s| *s)
    }

    pub fn update_snapshot(&self, bytes: Vec<u8>) {
        if let Ok(mut snap) = self.snapshot.lock() {
            *snap = bytes.clone();
        }
        let msg = json!({
            "type": "output",
            "session": self.session_id,
            "data": base64::engine::general_purpose::STANDARD.encode(bytes),
        });
        let line = serde_json::to_string(&msg).unwrap_or_default() + "\n";
        if let Ok(mut clients) = self.clients.lock() {
            clients.retain_mut(|c| c.write_all(line.as_bytes()).and_then(|_| c.flush()).is_ok());
        }
    }

    fn write_registration(&self) -> io::Result<()> {
        let payload = json!({
            "session": self.session_id,
            "id": self.session_id,
            "name": self.name,
            "command": "charon-tui",
            "status": "running",
            "transport": "charon",
            "socket": self.sock_path,
            "source": "charon-rust",
        });
        fs::write(&self.reg_path, serde_json::to_string_pretty(&payload).unwrap_or_default() + "\n")
    }
}

impl Drop for NativeSessionServer {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.reg_path);
        let _ = fs::remove_file(&self.sock_path);
    }
}

fn boats_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp"))
        .join(".charon/boats")
}

fn default_name() -> String {
    let dir = boats_dir();
    for i in 1..1000 {
        let name = format!("{:02}", i);
        let reg = dir.join(format!("charon-rust-{}.json", name));
        if !reg.exists() {
            return name;
        }
    }
    "session".to_string()
}
