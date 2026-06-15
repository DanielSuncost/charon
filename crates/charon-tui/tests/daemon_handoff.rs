//! Integration test for Phase 7 (graceful-drain handoff): a `shutdown` cleanly
//! stops the daemon (socket released, state persisted) and a fresh daemon restores
//! the session. This is the upgrade fallback path (no live fd-passing).

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

fn start_daemon(dir: &PathBuf) -> Child {
    Command::new(env!("CARGO_BIN_EXE_charond"))
        .env("CHARON_DIR", dir)
        .spawn()
        .expect("spawn charond")
}

fn wait_for_socket(path: &PathBuf) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if UnixStream::connect(path).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("charond socket never appeared");
}

struct Client {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
}

impl Client {
    fn connect(sock: &PathBuf) -> Self {
        let s = UnixStream::connect(sock).expect("connect");
        let reader = BufReader::new(s.try_clone().unwrap());
        let mut c = Client { reader, writer: s };
        c.send(r#"{"type":"hello","proto":1,"client":"it"}"#);
        c
    }
    fn send(&mut self, line: &str) {
        self.writer.write_all(line.as_bytes()).unwrap();
        self.writer.write_all(b"\n").unwrap();
        self.writer.flush().unwrap();
    }
    fn recv_type(&mut self, typ: &str) -> serde_json::Value {
        let deadline = Instant::now() + Duration::from_secs(6);
        let mut line = String::new();
        while Instant::now() < deadline {
            line.clear();
            if self.reader.read_line(&mut line).unwrap_or(0) == 0 {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                if v.get("type").and_then(|t| t.as_str()) == Some(typ) {
                    return v;
                }
            }
        }
        panic!("never saw message type {typ}");
    }
    fn wait_output(&mut self, needle: &str) {
        use base64::Engine;
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut acc = String::new();
        while Instant::now() < deadline {
            let mut line = String::new();
            if self.reader.read_line(&mut line).unwrap_or(0) == 0 {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                let t = v.get("type").and_then(|t| t.as_str()).unwrap_or("");
                if t == "output" || t == "snapshot" {
                    if let Some(d) = v.get("data").and_then(|d| d.as_str()) {
                        if let Ok(b) = base64::engine::general_purpose::STANDARD.decode(d) {
                            acc.push_str(&String::from_utf8_lossy(&b));
                            if acc.contains(needle) {
                                return;
                            }
                        }
                    }
                }
            }
        }
        panic!("never saw {needle:?} in output");
    }
}

struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn graceful_shutdown_then_restore() {
    let dir = std::env::temp_dir().join(format!("charond-handoff-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    let mut daemon = start_daemon(&dir);
    wait_for_socket(&sock);

    let sid = {
        let mut c = Client::connect(&sock);
        c.send(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"cols":80,"rows":24}"#);
        let sid = c.recv_type("spawned")["session"].as_str().unwrap().to_string();
        std::thread::sleep(Duration::from_millis(300));
        use base64::Engine;
        let data = base64::engine::general_purpose::STANDARD.encode(b"echo HANDOFF_MARK\n");
        c.send(&format!(r#"{{"type":"input","session":"{sid}","data":"{data}"}}"#));
        c.wait_output("HANDOFF_MARK");

        // Graceful shutdown: expect an ack, then the daemon exits.
        c.send(r#"{"type":"shutdown"}"#);
        c.recv_type("shutting_down");
        sid
    };

    // The daemon process exits and releases the socket (clean, not kill -9).
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut exited = false;
    while Instant::now() < deadline {
        if let Ok(Some(_)) = daemon.try_wait() {
            exited = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(exited, "daemon did not exit after shutdown");
    assert!(UnixStream::connect(&sock).is_err(), "socket should be removed on graceful exit");

    // Start the upgraded daemon; the session is restored with its history.
    let _daemon2 = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);
    let mut c = Client::connect(&sock);
    c.send(r#"{"type":"list"}"#);
    let inv = c.recv_type("inventory");
    assert!(
        inv["sessions"].as_array().unwrap().iter().any(|s| s["id"] == serde_json::json!(sid)),
        "session not restored after handoff"
    );
    c.send(&format!(r#"{{"type":"attach","session":"{sid}","cols":80,"rows":24,"replay":true}}"#));
    c.wait_output("HANDOFF_MARK");

    let _ = std::fs::remove_dir_all(&dir);
}
