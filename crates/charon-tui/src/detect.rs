//! Heuristic session-state detection.
//!
//! Classifies a session into a coarse state from its rendered output and timing.
//! For Charon-run agents the runtime reports state directly; this layer covers
//! local/observed sessions (shells and agents we can only watch) so every front-end
//! can show a consistent `idle / working / blocked / done / exited` indicator.
//!
//! `Done`/`Exited` are part of the complete state vocabulary but aren't produced by
//! the output heuristic (the daemon sets `exited` on EOF; `done` is for
//! agent-reported status), so the module allows otherwise-dead variants.
#![allow(dead_code)]

/// Coarse session state shared with clients via the `status` protocol message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    Working,
    Blocked,
    Done,
    Exited,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            State::Idle => "idle",
            State::Working => "working",
            State::Blocked => "blocked",
            State::Done => "done",
            State::Exited => "exited",
        }
    }
}

/// Substrings (lower-cased) on the active line that indicate the session is
/// waiting for user input. Kept conservative to avoid false positives.
const BLOCKED_PATTERNS: &[&str] = &[
    "(y/n)",
    "[y/n]",
    "(yes/no)",
    "yes/no?",
    "y/n?",
    "password:",
    "password for",
    "passphrase",
    "press enter",
    "press return",
    "press any key",
    "are you sure",
    "do you want to",
    "continue?",
    "proceed?",
    "overwrite?",
    "(end)", // pager
];

/// Classify a session.
///
/// - `tail` is the rendered text of the active line (e.g. the cursor line).
/// - `quiescent` is true when no new output has arrived for the idle threshold.
///
/// While output is actively flowing the session is [`State::Working`]. Once it
/// settles, a waiting-for-input prompt is [`State::Blocked`]; otherwise [`State::Idle`].
pub fn classify(tail: &str, quiescent: bool) -> State {
    if !quiescent {
        return State::Working;
    }
    let line = tail.trim().to_lowercase();
    if !line.is_empty() && BLOCKED_PATTERNS.iter().any(|p| line.contains(p)) {
        return State::Blocked;
    }
    State::Idle
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flowing_output_is_working() {
        assert_eq!(classify("compiling crate 3/12", false), State::Working);
        // Even a prompt-looking line is "working" while output is still flowing.
        assert_eq!(classify("Continue? ", false), State::Working);
    }

    #[test]
    fn quiescent_prompt_is_idle() {
        assert_eq!(classify("user@host:~/proj$ ", true), State::Idle);
        assert_eq!(classify("", true), State::Idle);
    }

    #[test]
    fn quiescent_question_is_blocked() {
        assert_eq!(classify("Proceed? ", true), State::Blocked);
        assert_eq!(classify("Overwrite existing file? (y/n) ", true), State::Blocked);
        assert_eq!(classify("[sudo] password for user:", true), State::Blocked);
        assert_eq!(classify("Press ENTER to continue", true), State::Blocked);
    }

    #[test]
    fn ordinary_quiescent_line_is_idle() {
        assert_eq!(classify("done building.", true), State::Idle);
    }
}
