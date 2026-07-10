//! Command-line argument parsing for the `charon` binary.

#[derive(Clone, Debug)]
pub(crate) enum LaunchMode {
    AutoDiscover,
    SpawnCommand(Vec<String>),
    AttachSession(String),
    ListSessions,
    /// Spawn a new daemon-backed session (persists across TUI restarts) and attach.
    DaemonSpawn(Vec<String>),
    /// Attach to an existing daemon session by id.
    DaemonAttach(String),
    /// Respawn an exited daemon session (re-run its command) and attach.
    DaemonRespawn(String),
    /// Gracefully stop the running daemon and start a fresh one (binary upgrade).
    DaemonUpgrade,
    /// Print the daemon's session inventory and exit.
    DaemonList,
}

#[derive(Clone, Debug)]
pub(crate) struct CliOptions {
    pub(crate) launch_mode: LaunchMode,
    pub(crate) provider: Option<String>,
    pub(crate) resume: Option<String>,
    pub(crate) agent: Option<String>,
}

pub(crate) fn parse_args() -> CliOptions {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut provider = None;
    let mut resume = None;
    let mut agent = None;
    let mut i = 0usize;
    let mut remaining: Vec<String> = Vec::new();

    while i < args.len() {
        let arg = &args[i];
        if arg == "--" {
            remaining.extend_from_slice(&args[i..]);
            break;
        } else if arg == "--provider" {
            if let Some(val) = args.get(i + 1) { provider = Some(val.clone()); i += 2; continue; }
            eprintln!("Error: --provider requires a value");
            std::process::exit(1);
        } else if let Some(val) = arg.strip_prefix("--provider=") {
            provider = Some(val.to_string());
            i += 1;
            continue;
        } else if arg == "--resume" {
            if let Some(next) = args.get(i + 1) {
                if next.starts_with('-') {
                    resume = Some("latest".to_string());
                    i += 1;
                    continue;
                }
                resume = Some(next.clone());
                i += 2;
                continue;
            }
            resume = Some("latest".to_string());
            i += 1;
            continue;
        } else if let Some(val) = arg.strip_prefix("--resume=") {
            resume = Some(if val.is_empty() { "latest".to_string() } else { val.to_string() });
            i += 1;
            continue;
        } else if arg == "--agent" {
            if let Some(val) = args.get(i + 1) { agent = Some(val.clone()); i += 2; continue; }
            eprintln!("Error: --agent requires a value");
            std::process::exit(1);
        } else if let Some(val) = arg.strip_prefix("--agent=") {
            agent = Some(val.to_string());
            i += 1;
            continue;
        }

        remaining.push(arg.clone());
        i += 1;
    }

    let launch_mode = if remaining.is_empty() {
        LaunchMode::AutoDiscover
    } else if remaining[0] == "--list" || remaining[0] == "-l" {
        LaunchMode::ListSessions
    } else if remaining[0] == "--daemon-list" {
        LaunchMode::DaemonList
    } else if remaining[0] == "--daemon-attach" {
        if let Some(id) = remaining.get(1) {
            LaunchMode::DaemonAttach(id.clone())
        } else {
            eprintln!("Error: --daemon-attach requires a session id");
            std::process::exit(1);
        }
    } else if remaining[0] == "--daemon-spawn" {
        LaunchMode::DaemonSpawn(remaining[1..].to_vec())
    } else if remaining[0] == "--daemon-respawn" {
        if let Some(id) = remaining.get(1) {
            LaunchMode::DaemonRespawn(id.clone())
        } else {
            eprintln!("Error: --daemon-respawn requires a session id");
            std::process::exit(1);
        }
    } else if remaining[0] == "--daemon-upgrade" {
        LaunchMode::DaemonUpgrade
    } else if remaining[0] == "--attach" || remaining[0] == "-a" {
        if let Some(name) = remaining.get(1) {
            LaunchMode::AttachSession(name.clone())
        } else {
            eprintln!("Error: --attach requires a session name");
            std::process::exit(1);
        }
    } else if remaining[0] == "--" {
        let cmd = remaining[1..].to_vec();
        if cmd.is_empty() {
            eprintln!("Error: -- requires a command");
            std::process::exit(1);
        }
        LaunchMode::SpawnCommand(cmd)
    } else {
        LaunchMode::SpawnCommand(remaining)
    };

    CliOptions { launch_mode, provider, resume, agent }
}
