//! Integration test for Phase 3: a session's scrollback survives the *daemon*
//! restarting. Spawn a session, kill the daemon, restart it (same CHARON_DIR),
//! and verify the session is restored as `exited` with its history replayable,
//! then respawnable back to a live shell.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

fn wait_for_socket(path: &PathBuf) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if UnixStream::connect(path).is_ok() {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("charond socket never appeared at {}", path.display());
}

fn start_daemon(dir: &PathBuf) -> Child {
    Command::new(env!("CARGO_BIN_EXE_charond"))
        .env("CHARON_DIR", dir)
        .spawn()
        .expect("spawn charond")
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
    fn recv(&mut self) -> serde_json::Value {
        let mut line = String::new();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if Instant::now() > deadline {
                panic!("timed out reading from daemon");
            }
            line.clear();
            if self.reader.read_line(&mut line).unwrap_or(0) == 0 {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                return v;
            }
        }
    }
    fn wait_type(&mut self, typ: &str) -> serde_json::Value {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            let v = self.recv();
            if v.get("type").and_then(|t| t.as_str()) == Some(typ) {
                return v;
            }
        }
        panic!("never saw message type {typ}");
    }
    fn wait_output_contains(&mut self, needle: &str) {
        use base64::Engine;
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut acc = String::new();
        while Instant::now() < deadline {
            let v = self.recv();
            let t = v.get("type").and_then(|t| t.as_str()).unwrap_or("");
            if t == "output" || t == "snapshot" {
                let data = v.get("data").and_then(|d| d.as_str()).unwrap_or("");
                if let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(data) {
                    acc.push_str(&String::from_utf8_lossy(&bytes));
                    if acc.contains(needle) {
                        return;
                    }
                }
            }
        }
        panic!("never saw {needle:?} in output; got: {acc:?}");
    }
}

#[test]
fn scrollback_survives_daemon_restart() {
    let dir = std::env::temp_dir().join(format!("charond-persist-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    // ── First daemon: spawn a session and produce some output. ──
    let mut daemon = start_daemon(&dir);
    wait_for_socket(&sock);

    let sid = {
        let mut c = Client::connect(&sock);
        c.send(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"cols":80,"rows":24}"#);
        let sid = c.wait_type("spawned")["session"].as_str().unwrap().to_string();
        std::thread::sleep(Duration::from_millis(300));
        c.send(&format!(
            r#"{{"type":"input","session":"{sid}","data":"{}"}}"#,
            base64_encode("echo PERSIST_MARK_123\n")
        ));
        c.wait_output_contains("PERSIST_MARK_123");
        sid
    };

    // ── Kill the daemon hard, then restart it against the same state dir. ──
    daemon.kill().unwrap();
    daemon.wait().unwrap();

    let mut daemon2 = start_daemon(&dir);
    wait_for_socket(&sock);
    let cleanup = scopeguard(daemon2.id());

    let mut c = Client::connect(&sock);

    // Restored in inventory as exited.
    c.send(r#"{"type":"list"}"#);
    let inv = c.wait_type("inventory");
    let restored = inv["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["id"] == serde_json::json!(sid))
        .unwrap_or_else(|| panic!("session {sid} not restored after daemon restart"));
    assert_eq!(restored["state"], "exited", "restored session should be exited");

    // Replay shows the prior scrollback.
    c.send(&format!(
        r#"{{"type":"attach","session":"{sid}","cols":80,"rows":24,"replay":true}}"#
    ));
    c.wait_output_contains("PERSIST_MARK_123");

    // Respawn brings it back to a live shell.
    c.send(&format!(r#"{{"type":"respawn","session":"{sid}"}}"#));
    std::thread::sleep(Duration::from_millis(300));
    c.send(&format!(
        r#"{{"type":"input","session":"{sid}","data":"{}"}}"#,
        base64_encode("echo AFTER_RESPAWN\n")
    ));
    c.wait_output_contains("AFTER_RESPAWN");

    drop(cleanup);
    let _ = daemon2.kill();
    let _ = daemon2.wait();
    let _ = std::fs::remove_dir_all(&dir);
}

fn base64_encode(s: &str) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(s.as_bytes())
}

/// Best-effort: if the test panics, make sure the second daemon is killed.
fn scopeguard(pid: u32) -> impl Drop {
    struct G(u32);
    impl Drop for G {
        fn drop(&mut self) {
            let _ = Command::new("kill").arg(self.0.to_string()).status();
        }
    }
    G(pid)
}
