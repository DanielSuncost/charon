//! Small shared helpers used by both the `charon` TUI binary and the library.

use std::path::PathBuf;

/// Locate the repository root that contains the Charon tooling.
///
/// Resolution order: `CHARON_ROOT` env var, then walking up from the current
/// executable looking for the `apps/core-daemon` marker, then falling back to
/// the workspace root relative to this crate's manifest.
pub fn project_root() -> PathBuf {
    if let Ok(root) = std::env::var("CHARON_ROOT") {
        let path = PathBuf::from(root);
        if path.exists() {
            return path;
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        for anc in exe.ancestors() {
            let marker = anc.join("apps").join("core-daemon");
            if marker.exists() {
                return anc.to_path_buf();
            }
        }
    }

    PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap().to_path_buf()
}
