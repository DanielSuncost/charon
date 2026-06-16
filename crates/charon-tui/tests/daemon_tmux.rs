//! Integration test for daemon spawn kinds: adopt an existing tmux session via
//! `spawn {kind:"tmux"}`, and confirm it RE-ATTACHES (survives) after a daemon
//! restart — unlike local PTYs, which restore as exited. Skips if tmux is absent.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

fn have_tmux() -> bool {
    Command::new("tmux").arg("-V").output().map(|o| o.status.success()).unwrap_or(false)
}

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
    fn recv_type(&mut self, typ: &str, secs: u64) -> serde_json::Value {
        let deadline = Instant::now() + Duration::from_secs(secs);
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
    fn find(&mut self, sid: &str) -> Option<serde_json::Value> {
        self.send(r#"{"type":"list"}"#);
        let inv = self.recv_type("inventory", 5);
        inv["sessions"].as_array().unwrap().iter()
            .find(|s| s["id"] == serde_json::json!(sid)).cloned()
    }
}

struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); }
}

#[test]
fn tmux_kind_reattaches_after_restart() {
    if !have_tmux() {
        eprintln!("skipping: tmux not available");
        return;
    }
    let tmux_name = format!("charond-it-{}", std::process::id());
    // Create an independent tmux session.
    let ok = Command::new("tmux")
        .args(["new-session", "-d", "-s", &tmux_name, "-x", "80", "-y", "24"])
        .status().map(|s| s.success()).unwrap_or(false);
    assert!(ok, "could not create tmux session");
    struct TmuxGuard(String);
    impl Drop for TmuxGuard {
        fn drop(&mut self) {
            let _ = Command::new("tmux").args(["kill-session", "-t", &self.0]).status();
        }
    }
    let _tmux_guard = TmuxGuard(tmux_name.clone());

    let dir = std::env::temp_dir().join(format!("charond-tmux-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("charond.sock");

    let mut daemon = start_daemon(&dir);
    wait_for_socket(&sock);

    let sid = {
        let mut c = Client::connect(&sock);
        c.send(&format!(
            r#"{{"type":"spawn","kind":"tmux","target":"{tmux_name}","cols":80,"rows":24}}"#
        ));
        let sid = c.recv_type("spawned", 5)["session"].as_str().unwrap().to_string();
        // It should be a tmux-kind session in the inventory.
        let s = c.find(&sid).expect("session present");
        assert_eq!(s["kind"], "tmux");
        sid
    };

    // Kill the daemon hard; the tmux session lives on independently.
    daemon.kill().unwrap();
    daemon.wait().unwrap();

    // Restart: an external (tmux) session must be RE-ATTACHED (working), not exited.
    let _daemon2 = Daemon(start_daemon(&dir));
    wait_for_socket(&sock);
    let mut c = Client::connect(&sock);
    let restored = c.find(&sid).expect("tmux session not restored after restart");
    assert_eq!(restored["kind"], "tmux");
    assert_ne!(restored["state"], "exited", "external tmux session should re-attach, not exit");

    let _ = std::fs::remove_dir_all(&dir);
}
