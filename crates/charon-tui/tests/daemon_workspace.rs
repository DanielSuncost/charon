//! Integration test for Phase 6 (daemon model): per-session workspace + tab,
//! the `move` command, defaults, and persistence across a daemon restart.

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
        let deadline = Instant::now() + Duration::from_secs(5);
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
    fn spawn(&mut self, json: &str) -> String {
        self.send(json);
        self.recv_type("spawned")["session"].as_str().unwrap().to_string()
    }
    fn find(&mut self, sid: &str) -> serde_json::Value {
        self.send(r#"{"type":"list"}"#);
        let inv = self.recv_type("inventory");
        inv["sessions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == serde_json::json!(sid))
            .cloned()
            .unwrap_or_else(|| panic!("session {sid} not in inventory"))
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
fn workspace_and_tab_model() {
    let dir = std::env::temp_dir().join(format!("charond-ws-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    let mut daemon = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);

    let sid = {
        let mut c = Client::connect(&sock);

        // Spawn with an explicit workspace + tab.
        let sid = c.spawn(
            r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"workspace":"proj-a","tab":"build","cols":80,"rows":24}"#,
        );
        let s = c.find(&sid);
        assert_eq!(s["workspace"], "proj-a");
        assert_eq!(s["tab"], "build");

        // Spawn without one → defaults.
        let sid2 = c.spawn(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"]}"#);
        let s2 = c.find(&sid2);
        assert_eq!(s2["workspace"], "default");
        assert_eq!(s2["tab"], "main");

        // Move the first session to another workspace (tab unchanged).
        c.send(&format!(r#"{{"type":"move","session":"{sid}","workspace":"proj-b"}}"#));
        std::thread::sleep(Duration::from_millis(150));
        let moved = c.find(&sid);
        assert_eq!(moved["workspace"], "proj-b", "workspace should change");
        assert_eq!(moved["tab"], "build", "tab should be unchanged by partial move");

        sid
    };

    // Persist across a daemon restart.
    daemon.0.kill().unwrap();
    daemon.0.wait().unwrap();
    let _daemon2 = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);

    let mut c = Client::connect(&sock);
    let restored = c.find(&sid);
    assert_eq!(restored["workspace"], "proj-b", "workspace should survive restart");
    assert_eq!(restored["tab"], "build", "tab should survive restart");

    let _ = std::fs::remove_dir_all(&dir);
}
