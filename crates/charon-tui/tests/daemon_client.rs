//! Integration test: spawn the real `charond` binary, create a session over the
//! raw protocol, then attach via `DaemonClient` and verify a full input/output
//! round-trip through the `ByteStream` interface.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

use charon_tui::backend::ByteStream;
use charon_tui::daemon_client::DaemonClient;

struct Daemon(Child);
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

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

#[test]
fn daemon_client_round_trip() {
    let sock = std::env::temp_dir().join(format!("charond-it-{}.sock", std::process::id()));
    let _ = std::fs::remove_file(&sock);

    let _daemon = Daemon(
        Command::new(env!("CARGO_BIN_EXE_charond"))
            .env("CHARON_SOCK", &sock)
            .spawn()
            .expect("spawn charond"),
    );
    wait_for_socket(&sock);

    // Spawn a session over the raw protocol and grab its id.
    let mut raw = UnixStream::connect(&sock).expect("connect");
    raw.write_all(b"{\"type\":\"hello\",\"proto\":1,\"client\":\"it\"}\n")
        .unwrap();
    raw.write_all(b"{\"type\":\"spawn\",\"kind\":\"local\",\"cmd\":[\"bash\",\"--norc\",\"-i\"],\"cols\":80,\"rows\":24}\n")
        .unwrap();
    raw.flush().unwrap();

    let mut reader = BufReader::new(raw.try_clone().unwrap());
    let sid = read_spawned(&mut reader);

    // Attach through the client ByteStream and round-trip a command.
    let mut client = DaemonClient::attach(&sock, &sid, 80, 24, true).expect("attach");
    std::thread::sleep(Duration::from_millis(300));
    client
        .write_bytes(b"echo ROUNDTRIP_OK\n")
        .expect("write input");

    let deadline = Instant::now() + Duration::from_secs(5);
    let mut seen = String::new();
    while Instant::now() < deadline {
        let bytes = client.read_available().expect("read");
        if !bytes.is_empty() {
            seen.push_str(&String::from_utf8_lossy(&bytes));
            if seen.contains("ROUNDTRIP_OK") {
                return; // success
            }
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("did not observe command output through DaemonClient; saw: {seen:?}");
}

fn read_spawned(reader: &mut BufReader<UnixStream>) -> String {
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut line = String::new();
    while Instant::now() < deadline {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            continue;
        }
        let v: serde_json::Value = match serde_json::from_str(line.trim()) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if v.get("type").and_then(|t| t.as_str()) == Some("spawned") {
            return v["session"].as_str().unwrap().to_string();
        }
    }
    panic!("never received 'spawned'");
}
