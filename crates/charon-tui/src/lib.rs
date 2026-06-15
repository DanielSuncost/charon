//! Shared library for the Charon terminal stack.
//!
//! Exposes the backend-agnostic byte-stream + terminal-emulation modules so they
//! can be reused by both the `charon` TUI client and the `charond` daemon.

pub mod backend;
pub mod config;
pub mod daemon;
pub mod daemon_client;
pub mod detect;
pub mod parser;
pub mod protocol;
pub mod session;
pub mod terminal;
