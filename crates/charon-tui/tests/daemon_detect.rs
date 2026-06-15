//! Integration test for Phase 4: the daemon classifies a live session and
//! broadcasts `status` transitions. We drive a shell to a blocking prompt and
//! assert the daemon reports `blocked`, then `idle` after the prompt is answered.

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
    fn input(&mut self, sid: &str, text: &str) {
        use base64::Engine;
        let data = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
        self.send(&format!(r#"{{"type":"input","session":"{sid}","data":"{data}"}}"#));
    }
    fn recv(&mut self) -> serde_json::Value {
        let mut line = String::new();
        let deadline = Instant::now() + Duration::from_secs(6);
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
    /// Wait until a `status` message reports the given state.
    fn wait_status(&mut self, want: &str) {
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut seen = Vec::new();
        while Instant::now() < deadline {
            let v = self.recv();
            if v.get("type").and_then(|t| t.as_str()) == Some("status") {
                let st = v.get("state").and_then(|s| s.as_str()).unwrap_or("");
                seen.push(st.to_string());
                if st == want {
                    return;
                }
            }
        }
        panic!("never saw status={want:?}; saw states: {seen:?}");
    }
    fn spawned(&mut self) -> String {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            let v = self.recv();
            if v.get("type").and_then(|t| t.as_str()) == Some("spawned") {
                return v["session"].as_str().unwrap().to_string();
            }
        }
        panic!("never saw spawned");
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
fn detects_blocked_then_idle() {
    let dir = std::env::temp_dir().join(format!("charond-detect-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    let _daemon = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);

    let mut c = Client::connect(&sock);
    c.send(r#"{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"cols":80,"rows":24}"#);
    let sid = c.spawned();

    // Shell prompt settles → idle.
    c.wait_status("idle");

    // A command that prints a prompt and waits for input → blocked.
    c.input(&sid, "read -p 'Proceed? ' answer\n");
    c.wait_status("blocked");

    // Answering returns to the shell prompt → idle.
    c.input(&sid, "y\n");
    c.wait_status("idle");

    let _ = std::fs::remove_dir_all(&dir);
}
