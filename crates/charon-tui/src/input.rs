//! Input handling: key encoding and application of native-session commands.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::app::{App, View};
use crate::native_session::NativeCommand;

pub(crate) fn encode_key(key: &KeyEvent) -> Vec<u8> {
    if key.modifiers.contains(KeyModifiers::CONTROL) {
        if let KeyCode::Char(c) = key.code {
            let ctrl_byte = (c as u8).wrapping_sub(b'a').wrapping_add(1);
            return vec![ctrl_byte];
        }
    }

    match key.code {
        KeyCode::Char(c) => {
            let mut buf = [0u8; 4];
            c.encode_utf8(&mut buf).as_bytes().to_vec()
        }
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Backspace => vec![0x7f],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::Esc => vec![0x1b],
        KeyCode::Up => b"\x1b[A".to_vec(),
        KeyCode::Down => b"\x1b[B".to_vec(),
        KeyCode::Right => b"\x1b[C".to_vec(),
        KeyCode::Left => b"\x1b[D".to_vec(),
        KeyCode::Home => b"\x1b[H".to_vec(),
        KeyCode::End => b"\x1b[F".to_vec(),
        KeyCode::PageUp => b"\x1b[5~".to_vec(),
        KeyCode::PageDown => b"\x1b[6~".to_vec(),
        KeyCode::Insert => b"\x1b[2~".to_vec(),
        KeyCode::Delete => b"\x1b[3~".to_vec(),
        KeyCode::F(1) => b"\x1bOP".to_vec(),
        KeyCode::F(2) => b"\x1bOQ".to_vec(),
        KeyCode::F(3) => b"\x1bOR".to_vec(),
        KeyCode::F(4) => b"\x1bOS".to_vec(),
        _ => vec![],
    }
}

pub(crate) fn apply_native_commands(app: &mut App, commands: Vec<NativeCommand>) {
    for cmd in commands {
        match cmd {
            NativeCommand::Input(bytes) => {
                let force_chat_context = app.active_view != View::Chat
                    && bytes != b"\x1bOP"
                    && bytes != b"\x1bOQ"
                    && bytes != b"\x1bOR"
                    && bytes != b"\x1bOS";
                if force_chat_context {
                    let saved_view = app.active_view;
                    app.active_view = View::Chat;
                    apply_native_input_bytes(app, &bytes);
                    app.active_view = saved_view;
                } else {
                    apply_native_input_bytes(app, &bytes);
                }
            }
            NativeCommand::Resize { .. } => {}
        }
    }
}

pub(crate) fn apply_native_input_bytes(app: &mut App, bytes: &[u8]) {
    if bytes.is_empty() {
        return;
    }

    match bytes {
        b"\x1bOP" => { app.active_view = View::Chat; return; }
        b"\x1bOQ" => { app.active_view = View::Dashboard; return; }
        b"\x1bOR" => { app.active_view = View::Sessions; return; }
        b"\x1bOS" => { app.active_view = View::InterAgent; return; }
        b"\x1b[5~" => {
            app.chat.scroll = app.chat.scroll.saturating_add(10);
            return;
        }
        b"\x1b[6~" => {
            app.chat.scroll = app.chat.scroll.saturating_sub(10);
            return;
        }
        b"\t" => {
            if app.active_view == View::Chat {
                if app.chat.menu_open() {
                    app.chat.menu_fill_input();
                    app.chat.close_menu();
                    app.chat.maybe_open_command_menu();
                } else if app.chat.input.trim().starts_with('/') {
                    app.chat.maybe_open_command_menu();
                }
            }
            return;
        }
        _ => {}
    }

    if app.active_view != View::Chat {
        return;
    }

    if app.chat.approval_open() || app.chat.auth_open() {
        return;
    }

    if app.chat.menu_open() {
        match bytes {
            b"\x1b[A" => app.chat.menu_move_up(),
            b"\x1b[B" => app.chat.menu_move_down(),
            b"\r" | b"\n" => app.chat.menu_select(),
            b"\x1b" => app.chat.close_menu(),
            b"\x7f" => {
                app.chat.input.pop();
                app.chat.maybe_open_command_menu();
            }
            _ => {
                if let Ok(s) = std::str::from_utf8(bytes) {
                    app.chat.input.push_str(s);
                    app.chat.maybe_open_command_menu();
                }
            }
        }
        return;
    }

    match bytes {
        b"\r" | b"\n" => app.chat.submit_input(),
        b"\x7f" => {
            app.chat.input.pop();
            app.chat.maybe_open_command_menu();
        }
        b"\x1b[A" => {
            app.chat.history_up();
            app.chat.maybe_open_command_menu();
        }
        b"\x1b[B" => {
            app.chat.history_down();
            app.chat.maybe_open_command_menu();
        }
        _ => {
            if let Ok(s) = std::str::from_utf8(bytes) {
                app.chat.input.push_str(s);
                app.chat.maybe_open_command_menu();
            }
        }
    }
}
