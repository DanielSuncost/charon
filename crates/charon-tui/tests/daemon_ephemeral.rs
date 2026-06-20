//! Integration test for the session lifetime model (finish-plan A):
//! an `ephemeral` session is reaped shortly after its last client disconnects,
//! while a persistent session survives. Covers Claude-Code-style "ends on close".

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
    fn spawn(&mut self, json: &str) -> String {
        self.send(json);
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut line = String::new();
        while Instant::now() < deadline {
            line.clear();
            if self.reader.read_line(&mut line).unwrap_or(0) == 0 {
                std::thread::sleep(Duration::from_millis(10));
                continue;
            }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
                if v.get("type").and_then(|t| t.as_str()) == Some("spawned") {
                    return v["session"].as_str().unwrap().to_string();
                }
            }
        }
        panic!("no spawned");
    }
}

fn list_ids(sock: &PathBuf) -> Vec<String> {
    let mut c = Client::connect(sock);
    c.send(r#"{"type":"list"}"#);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut line = String::new();
    while Instant::now() < deadline {
        line.clear();
        if c.reader.read_line(&mut line).unwrap_or(0) == 0 {
            std::thread::sleep(Duration::from_millis(10));
            continue;
        }
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(line.trim()) {
            if v.get("type").and_then(|t| t.as_str()) == Some("inventory") {
                return v["sessions"].as_array().unwrap().iter()
                    .map(|s| s["id"].as_str().unwrap().to_string()).collect();
            }
        }
    }
    panic!("no inventory");
}

struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
}

#[test]
fn ephemeral_reaped_persistent_survives() {
    let dir = std::env::temp_dir().join(format!("charond-eph-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    let _daemon = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);

    // Client A spawns one ephemeral and one persistent session (auto-subscribed to A).
    let (eph, persist) = {
        let mut a = Client::connect(&sock);
        let eph = a.spawn(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"ephemeral":true}"#);
        let persist = a.spawn(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"ephemeral":false}"#);
        let ids = list_ids(&sock);
        assert!(ids.contains(&eph) && ids.contains(&persist), "both present while attached");
        (eph, persist)
        // `a` drops here → A disconnects → ephemeral starts its reap grace.
    };

    // After the grace (3s) the ephemeral session is gone; the persistent one remains.
    std::thread::sleep(Duration::from_millis(4000));
    let ids = list_ids(&sock);
    assert!(!ids.contains(&eph), "ephemeral session should be reaped after client left; got {ids:?}");
    assert!(ids.contains(&persist), "persistent session should survive; got {ids:?}");

    let _ = std::fs::remove_dir_all(&dir);
}
