//! `charond` — the always-on Charon session daemon binary.

fn main() {
    if let Err(e) = charon_tui::daemon::run() {
        eprintln!("charond: {e}");
        std::process::exit(1);
    }
}
