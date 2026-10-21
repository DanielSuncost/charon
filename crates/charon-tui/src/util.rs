//! Small shared helpers used by both the `charon` TUI binary and the library.

use std::path::{Path, PathBuf};

/// The Python backend `BackendProcess::start` runs, relative to the root.
pub const BACKEND_SCRIPT: &str = "apps/tui/opentui/chat_backend.py";

/// Locate the repository root that contains the Charon tooling.
///
/// Resolution order: `CHARON_ROOT` env var, then walking up from the current
/// executable to the first directory holding the backend script, then the
/// workspace root relative to this crate's manifest. The marker must be a file
/// the backend actually needs: it used to name `apps/core-daemon`, which was
/// removed, so every binary fell through to the compile-time path and a binary
/// built in one checkout and copied into another ran the first checkout's
/// backend and `.charon_state`.
pub fn project_root() -> PathBuf {
    if let Ok(root) = std::env::var("CHARON_ROOT") {
        let path = PathBuf::from(root);
        if is_charon_root(&path) {
            return path;
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        let exe = exe.canonicalize().unwrap_or(exe);
        if let Some(root) = exe.ancestors().find(|anc| is_charon_root(anc)) {
            return root.to_path_buf();
        }
    }

    PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf()
}

fn is_charon_root(path: &Path) -> bool {
    path.join(BACKEND_SCRIPT).is_file()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_is_recognised_by_the_backend_script_it_launches() {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        assert!(is_charon_root(&root), "{} lacks {}", root.display(), BACKEND_SCRIPT);
        assert!(!is_charon_root(&root.join("crates")));
    }
}
