/// charon-tui — Full Rust TUI shell (Milestone 1).
///
/// Current status:
/// - F1 Chat: placeholder native chat view
/// - F2 Dashboard: placeholder native dashboard
/// - F3 Sessions: live VTE session grid
///
/// This is the clean foundation for replacing the current Bun/OpenTUI frontend.

mod app;
mod backend;
mod chat;
mod grid;
mod native_session;
mod parser;
mod render;
mod session;
mod terminal;

use std::fs::OpenOptions;
use std::io::{self, Write};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use app::{App, SessionsSection, TextPoint, View};
use base64::Engine;
use crossterm::{
    cursor,
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEventKind,
    },
    style::{self},
    terminal as ct,
    QueueableCommand,
};

use backend::discover_sessions;
use chat::{ChatContextMenu, ChatMessage, ChatTextPoint};
use grid::compute_grid;
use native_session::{NativeCommand, NativeSessionServer};
use parser::AnsiParser;
use render::Rect;
use serde::Deserialize;
use serde_json::Value;
use session::{BackendType, SessionCell};
use terminal::TerminalState;

enum LaunchMode {
    AutoDiscover,
    SpawnCommand(Vec<String>),
    AttachSession(String),
    ListSessions,
}

fn copy_to_clipboard(text: &str) -> bool {
    let attempts: &[(&str, &[&str])] = &[
        ("wl-copy", &[]),
        ("xclip", &["-selection", "clipboard"]),
        ("xsel", &["--clipboard", "--input"]),
        ("pbcopy", &[]),
    ];
    for (cmd, args) in attempts {
        let mut child = match Command::new(cmd)
            .args(*args)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn() {
                Ok(child) => child,
                Err(_) => continue,
            };
        if let Some(mut stdin) = child.stdin.take() {
            if stdin.write_all(text.as_bytes()).is_ok() && child.wait().map(|s| s.success()).unwrap_or(false) {
                return true;
            }
        }
    }

    let encoded = base64::engine::general_purpose::STANDARD.encode(text.as_bytes());
    let osc52 = format!("\x1b]52;c;{}\x07", encoded);
    if let Ok(mut tty) = OpenOptions::new().write(true).open("/dev/tty") {
        if tty.write_all(osc52.as_bytes()).is_ok() && tty.flush().is_ok() {
            return true;
        }
    }
    false
}

fn parse_args() -> LaunchMode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        return LaunchMode::AutoDiscover;
    }
    if args[0] == "--list" || args[0] == "-l" {
        return LaunchMode::ListSessions;
    }
    if args[0] == "--attach" || args[0] == "-a" {
        if let Some(name) = args.get(1) {
            return LaunchMode::AttachSession(name.clone());
        }
        eprintln!("Error: --attach requires a session name");
        std::process::exit(1);
    }
    if args[0] == "--" {
        let cmd = args[1..].to_vec();
        if cmd.is_empty() {
            eprintln!("Error: -- requires a command");
            std::process::exit(1);
        }
        return LaunchMode::SpawnCommand(cmd);
    }
    LaunchMode::SpawnCommand(args)
}

fn encode_key(key: &KeyEvent) -> Vec<u8> {
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

fn apply_native_commands(app: &mut App, commands: Vec<NativeCommand>) {
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

fn apply_native_input_bytes(app: &mut App, bytes: &[u8]) {
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

fn build_initial_sessions(mode: &LaunchMode, outer_w: u16, outer_h: u16) -> io::Result<Vec<SessionCell>> {
    let mut sessions = Vec::new();
    let mut next_id = 0u64;

    match mode {
        LaunchMode::ListSessions => {}
        LaunchMode::SpawnCommand(cmd) => {
            let cmd_refs: Vec<&str> = cmd.iter().map(|s| s.as_str()).collect();
            let (_, _, rects) = compute_grid(1, outer_w, outer_h.saturating_sub(2));
            if let Some(r) = rects.first() {
                let title = cmd.first().map(|s| s.as_str()).unwrap_or("shell");
                sessions.push(SessionCell::spawn(next_id, title, &cmd_refs, r.width, r.height)?);
            }
        }
        LaunchMode::AttachSession(name) => {
            let (_, _, rects) = compute_grid(1, outer_w, outer_h.saturating_sub(2));
            if let Some(r) = rects.first() {
                sessions.push(SessionCell::attach_tmux(next_id, name, name, r.width, r.height)?);
            }
        }
        LaunchMode::AutoDiscover => {
            // Start empty in auto-discover mode and let backend metadata decide
            // which sessions should exist/be shown. This avoids flashing stale
            // local tmux sessions before the real F3 model arrives.
        }
    }

    Ok(sessions)
}

fn payload_agents(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("agents"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

fn payload_projects(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("projects"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

fn payload_activity(payload: Option<&Value>) -> Vec<String> {
    payload
        .and_then(|p| p.get("activity"))
        .and_then(|a| a.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_str().map(|s| s.to_string())).collect())
        .unwrap_or_default()
}

fn payload_inter_agent_rooms(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("inter_agent_rooms"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

fn terminal_window_title(app: &App) -> String {
    let project = app.chat.onboarding_project();
    let project_name = project
        .split('/')
        .filter(|s| !s.is_empty())
        .last()
        .unwrap_or("default");
    format!("charon-{}", project_name)
}

fn draw_header<W: Write>(stdout: &mut W, app: &App, w: u16) -> io::Result<()> {
    write!(stdout, "\x1b]0;{}\x07", terminal_window_title(app))?;
    stdout.queue(cursor::MoveTo(0, 0))?;
    stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 167, g: 139, b: 250 }))?;

    let view = match app.active_view {
        View::Chat => "F1 Chat",
        View::Dashboard => "F2 Dashboard",
        View::Sessions => "F3 Sessions",
        View::InterAgent => "F4 Inter-Agent",
    };

    let sessions_mode = if app.active_view == View::Sessions {
        if app.sessions.terminal_mode { " │ terminal mode (Ctrl+] / Ctrl+G / F4)" } else { " │ grid mode (Enter to interact)" }
    } else {
        ""
    };

    let header = format!(
        " CHARON │ {} │ F1 Chat │ F2 Dashboard │ F3 Sessions │ F4 Groups │ Ctrl+Q Quit{} ",
        view, sessions_mode
    );
    let visible: String = header.chars().take(w as usize).collect();
    let pad = (w as usize).saturating_sub(visible.chars().count());
    write!(stdout, "{}{}", visible, " ".repeat(pad))?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn draw_footer<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    stdout.queue(cursor::MoveTo(0, h.saturating_sub(1)))?;
    stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 100, g: 90, b: 130 }))?;
    let line = match app.active_view {
        View::Chat => {
            " ".repeat(w as usize)
        },
        View::Dashboard => {
            let payload = app.chat.refresh_payload.as_ref();
            format!(
                " Dashboard │ agents:{} │ projects:{} │ activity:{} ",
                payload_agents(payload).len(),
                payload_projects(payload).len(),
                payload_activity(payload).len(),
            )
        }
        View::Sessions => {
            let fc = app.sessions.panes.get(app.sessions.focused);
            let title = fc.map(|c| c.title.as_str()).unwrap_or("none");
            let alt = fc.map(|c| if c.terminal.in_alt_screen { " [ALT]" } else { "" }).unwrap_or("");
            format!(" Sessions │ pane {}: {}{} ", app.sessions.focused + 1, title, alt)
        }
        View::InterAgent => {
            let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
            format!(" Groups │ rooms:{} ", rooms.len())
        }
    };
    let visible: String = line.chars().take(w as usize).collect();
    let pad = (w as usize).saturating_sub(visible.chars().count());
    write!(stdout, "{}{}", visible, " ".repeat(pad))?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn draw_placeholder_panel<W: Write>(stdout: &mut W, area: Rect, title: &str, lines: &[String]) -> io::Result<()> {
    render::render_border(stdout, area, title, true)?;
    let max_lines = area.height as usize;
    for (i, line) in lines.iter().take(max_lines).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}", visible)?;
    }
    Ok(())
}

fn render_local_charon_preview<W: Write>(stdout: &mut W, app: &mut App, area: Rect, self_socket_to_hide: Option<&str>) -> io::Result<()> {
    let saved_view = app.active_view;
    if app.active_view == View::Sessions {
        app.active_view = View::Chat;
    }
    let bytes = build_native_session_snapshot(app, area.width.max(1), area.height.max(1), self_socket_to_hide);
    app.active_view = saved_view;
    let mut terminal = TerminalState::new(area.width.max(1), area.height.max(1));
    let mut parser = AnsiParser::new();
    parser.process(&bytes, &mut terminal);
    render::render_terminal(stdout, &terminal, area, 0)
}

fn build_native_session_snapshot(app: &mut App, w: u16, h: u16, self_socket_to_hide: Option<&str>) -> Vec<u8> {
    let mut out = Vec::new();
    let snapshot_view = if app.active_view == View::Sessions || matches!(chat_layout_variant(w, h), ChatLayoutVariant::Tiny | ChatLayoutVariant::Mid) {
        View::Chat
    } else {
        app.active_view
    };
    let session_rects = if snapshot_view == View::Sessions {
        session_grid_rects(app, w, h)
    } else {
        Vec::new()
    };
    let tiny_snapshot = matches!(chat_layout_variant(w, h), ChatLayoutVariant::Tiny);
    let saved_view = app.active_view;
    app.active_view = snapshot_view;
    let _ = out.queue(cursor::Hide);
    let _ = out.queue(ct::Clear(ct::ClearType::All));
    if !tiny_snapshot {
        let _ = draw_header(&mut out, app, w);
    }
    let _ = match snapshot_view {
        View::Chat => draw_chat(&mut out, app, w, h),
        View::Dashboard => draw_dashboard(&mut out, app, w, h),
        View::Sessions => draw_sessions(&mut out, app, &session_rects, true, w, h, self_socket_to_hide),
        View::InterAgent => draw_inter_agent(&mut out, app, w, h),
    };
    if !tiny_snapshot {
        let _ = draw_footer(&mut out, app, w, h);
    }
    app.active_view = saved_view;
    out
}

fn chat_rowing_active(app: &App) -> bool {
    if app.chat.streaming {
        return true;
    }
    matches!(app.chat.messages.last(),
        Some(ChatMessage::Assistant { streaming: true, .. })
        | Some(ChatMessage::Thinking { streaming: true, .. })
        | Some(ChatMessage::ToolCall { .. })
    )
}

fn chat_reserved_bottom(app: &App, variant: ChatLayoutVariant) -> u16 {
    let mut reserved = match variant {
        ChatLayoutVariant::Full => 6,
        ChatLayoutVariant::Mid => 5,
        ChatLayoutVariant::Tiny => 4,
    };
    if chat_rowing_active(app) && variant != ChatLayoutVariant::Tiny {
        reserved += 3;
    }
    reserved
}

fn rowing_indicator_lines(frame: usize) -> Vec<ChatRenderLine> {
    let water = style::Color::Rgb { r: 99, g: 102, b: 241 };
    let wave_d = style::Color::Rgb { r: 67, g: 56, b: 202 };
    let hull = style::Color::Rgb { r: 127, g: 29, b: 29 };
    let hull_l = style::Color::Rgb { r: 153, g: 27, b: 27 };
    let figure = style::Color::Rgb { r: 220, g: 38, b: 38 };
    let figure_d = style::Color::Rgb { r: 153, g: 27, b: 27 };
    let oar = style::Color::Rgb { r: 212, g: 196, b: 168 };
    let lantern_bright = style::Color::Rgb { r: 251, g: 191, b: 36 };
    let lantern_dim = style::Color::Rgb { r: 245, g: 158, b: 11 };
    let lantern_glow = style::Color::Rgb { r: 253, g: 230, b: 138 };
    let spark = style::Color::Rgb { r: 252, g: 211, b: 77 };
    let mk = |spans: Vec<(style::Color, &str)>| ChatRenderLine {
        spans: spans.into_iter().map(|(fg, text)| ChatSpan { fg, text: text.to_string() }).collect(),
        bg: None,
    };
    match frame % 4 {
        0 => vec![
            mk(vec![(style::Color::Reset, "        "), (figure, "▵"), (oar, "_"), (spark, "·"), (lantern_bright, "◈"), (spark, "*."), (style::Color::Reset, "      ")]),
            mk(vec![(style::Color::Reset, "       "), (figure_d, "█"), (oar, "─╱"), (style::Color::Reset, "            ")]),
            mk(vec![(water, "  ≈"), (wave_d, "~"), (hull, "╘"), (hull_l, "▬▬"), (hull, "▬"), (hull_l, "▬▬"), (hull, "╛"), (wave_d, "~"), (water, "≈"), (wave_d, "~~  ")]),
        ],
        1 => vec![
            mk(vec![(style::Color::Reset, "       "), (figure, "▵"), (oar, "_"), (spark, "·"), (lantern_dim, "◈"), (spark, " *"), (style::Color::Reset, "       ")]),
            mk(vec![(style::Color::Reset, "      "), (figure_d, "█"), (oar, "─│"), (style::Color::Reset, "             ")]),
            mk(vec![(wave_d, " ~"), (water, "≈"), (hull, "╘"), (hull_l, "▬▬"), (hull, "▬"), (hull_l, "▬▬"), (hull, "╛"), (water, "≈"), (wave_d, "~"), (water, "≈   ")]),
        ],
        2 => vec![
            mk(vec![(style::Color::Reset, "        "), (figure, "▵"), (oar, "_"), (spark, "*"), (lantern_bright, "◈"), (lantern_glow, "˙"), (style::Color::Reset, "      ")]),
            mk(vec![(style::Color::Reset, "       "), (figure_d, "█"), (oar, "─╲"), (style::Color::Reset, "            ")]),
            mk(vec![(water, " ≈"), (wave_d, "~"), (water, "≈"), (hull, "╘"), (hull_l, "▬▬"), (hull, "▬"), (hull_l, "▬▬"), (hull, "╛"), (wave_d, "~"), (water, "≈   ")]),
        ],
        _ => vec![
            mk(vec![(style::Color::Reset, "        "), (figure, "▵"), (oar, "_"), (spark, " ·"), (lantern_dim, "◈"), (spark, "*"), (style::Color::Reset, "      ")]),
            mk(vec![(style::Color::Reset, "       "), (figure_d, "█"), (oar, "─│"), (style::Color::Reset, "            ")]),
            mk(vec![(wave_d, "  ~"), (water, "≈"), (hull, "╘"), (hull_l, "▬▬"), (hull, "▬"), (hull_l, "▬▬"), (hull, "╛"), (water, "≈"), (wave_d, "~~  ")]),
        ],
    }
}

fn wrap_plain_text(s: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return vec![];
    }
    let mut out = Vec::new();
    for raw in s.lines() {
        let chars: Vec<char> = raw.chars().collect();
        if chars.is_empty() {
            out.push(String::new());
            continue;
        }
        let mut start = 0usize;
        while start < chars.len() {
            let end = (start + width).min(chars.len());
            out.push(chars[start..end].iter().collect());
            start = end;
        }
    }
    if out.is_empty() { out.push(String::new()); }
    out
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ChatLayoutVariant {
    Full,
    Mid,
    Tiny,
}

fn chat_layout_variant(w: u16, h: u16) -> ChatLayoutVariant {
    if w >= 95 && h >= 30 {
        ChatLayoutVariant::Full
    } else if w >= 60 && h >= 18 {
        ChatLayoutVariant::Mid
    } else {
        ChatLayoutVariant::Tiny
    }
}

#[derive(Deserialize)]
struct MascotCell {
    x: usize,
    y: usize,
    ch: String,
    fg: Option<[u8; 3]>,
}

#[derive(Deserialize)]
struct MascotSprite {
    width: usize,
    height: usize,
    cells: Vec<MascotCell>,
}

#[derive(Deserialize)]
struct MascotTitleSource {
    x: usize,
    y: usize,
    w: usize,
    h: usize,
}

#[derive(Deserialize)]
struct MascotTinyTitle {
    path: String,
    fg: [u8; 3],
}

#[derive(Deserialize)]
struct MascotConfig {
    tiny_title: Option<MascotTinyTitle>,
    tiny_title_source: Option<MascotTitleSource>,
}

fn mascot_sprite() -> &'static MascotSprite {
    static SPRITE: OnceLock<MascotSprite> = OnceLock::new();
    SPRITE.get_or_init(|| serde_json::from_str(include_str!("../../../assets/lantern_wraith_terminal_sprite_v2.json")).expect("valid mascot sprite"))
}

fn mascot_config() -> &'static MascotConfig {
    static CONFIG: OnceLock<MascotConfig> = OnceLock::new();
    CONFIG.get_or_init(|| serde_json::from_str(include_str!("../../../assets/mascot_config.json")).expect("valid mascot config"))
}

fn animation_clock_start() -> Instant {
    static START: OnceLock<Instant> = OnceLock::new();
    *START.get_or_init(Instant::now)
}

#[derive(Clone)]
struct ChatSpan {
    fg: style::Color,
    text: String,
}

#[derive(Clone)]
struct ChatRenderLine {
    spans: Vec<ChatSpan>,
    bg: Option<style::Color>,
}

fn single_span_line(fg: style::Color, bg: Option<style::Color>, text: impl Into<String>) -> ChatRenderLine {
    ChatRenderLine {
        spans: vec![ChatSpan { fg, text: text.into() }],
        bg,
    }
}

fn brand_lines(width: usize, variant: ChatLayoutVariant) -> Vec<ChatRenderLine> {
    let mid_title = include_str!("../../../assets/title_ascii_mid.txt");
    let cfg = mascot_config();
    let title_fg = cfg.tiny_title.as_ref().map(|t| t.fg).unwrap_or([176, 146, 62]);
    let title_color = style::Color::Rgb { r: title_fg[0], g: title_fg[1], b: title_fg[2] };
    let subtitle_color = style::Color::Rgb { r: 120, g: 100, b: 70 };
    let default_dark = style::Color::Rgb { r: 26, g: 26, b: 26 };
    let mut out = Vec::new();

    if variant == ChatLayoutVariant::Tiny || width < 38 {
        out.push(ChatRenderLine {
            spans: vec![
                ChatSpan { fg: style::Color::Rgb { r: 90, g: 68, b: 40 }, text: "━━━ ".to_string() },
                ChatSpan { fg: title_color, text: "❈ CHARON ❈".to_string() },
                ChatSpan { fg: style::Color::Rgb { r: 90, g: 68, b: 40 }, text: " ━━━".to_string() },
            ],
            bg: None,
        });
        out.push(single_span_line(subtitle_color, None, "  Agent Operating System"));
        out.push(single_span_line(style::Color::Reset, None, String::new()));
        return out;
    }

    let sprite = mascot_sprite();
    let scale = if variant == ChatLayoutVariant::Full { 1.0 } else { 0.55 };
    let cols = width.min(((sprite.width as f32) * scale).floor().max(1.0) as usize);
    let rows = ((sprite.height as f32) * scale).floor().max(1.0) as usize;
    let mut chars = vec![vec![' '; cols]; rows];
    let mut colors = vec![vec![default_dark; cols]; rows];
    let title_src = cfg.tiny_title_source.as_ref().map(|s| (s.x, s.y, s.w, s.h)).unwrap_or((10usize, 16usize, 54usize, 4usize));

    for cell in &sprite.cells {
        let x = ((cell.x as f32) * scale).floor() as usize;
        let y = ((cell.y as f32) * scale).floor() as usize;
        if x >= cols || y >= rows { continue; }
        let ch = cell.ch.chars().next().unwrap_or(' ');
        if variant == ChatLayoutVariant::Mid
            && cell.x >= title_src.0 && cell.x < title_src.0 + title_src.2
            && cell.y >= title_src.1 && cell.y < title_src.1 + title_src.3 {
            continue;
        }
        if ch == ' ' && cell.fg.is_none() { continue; }
        chars[y][x] = ch;
        if let Some([r, g, b]) = cell.fg {
            colors[y][x] = style::Color::Rgb { r, g, b };
        }
    }

    if variant == ChatLayoutVariant::Mid {
        let stamp_x = ((title_src.0 as f32) * scale).floor() as usize;
        let stamp_y = ((title_src.1 as f32) * scale).floor() as usize;
        for (dy, line) in mid_title.lines().filter(|l| !l.is_empty()).enumerate() {
            let y = stamp_y + dy;
            if y >= rows { continue; }
            for (dx, ch) in line.chars().enumerate() {
                let x = stamp_x + dx;
                if x >= cols { break; }
                if ch == ' ' { continue; }
                chars[y][x] = ch;
                colors[y][x] = title_color;
            }
        }
    }

    let mut last_row = rows.saturating_sub(1);
    while last_row > 0 && chars[last_row].iter().enumerate().all(|(x, c)| *c == ' ' && colors[last_row][x] == default_dark) {
        last_row -= 1;
    }
    for y in 0..=last_row {
        let mut line_end = cols.saturating_sub(1);
        while line_end > 0 && chars[y][line_end] == ' ' && colors[y][line_end] == default_dark {
            line_end -= 1;
        }
        line_end += 1;
        let mut x = 0;
        let mut spans = Vec::new();
        while x < line_end {
            let color = colors[y][x];
            let mut buf = String::new();
            buf.push(chars[y][x]);
            x += 1;
            while x < line_end && colors[y][x] == color {
                buf.push(chars[y][x]);
                x += 1;
            }
            spans.push(ChatSpan { fg: color, text: buf });
        }
        out.push(ChatRenderLine { spans, bg: None });
    }

    out.push(single_span_line(subtitle_color, None, "  Agent Operating System"));
    out.push(single_span_line(style::Color::Reset, None, String::new()));
    out
}

fn normalize_inline_markdown(text: &str) -> String {
    let mut s = text.to_string();
    if let Ok(re) = regex::Regex::new(r"\[([^\]]+)\]\(([^\)]+)\)") {
        s = re.replace_all(&s, "$1").to_string();
    }
    if let Ok(re) = regex::Regex::new(r"`([^`]+)`") {
        s = re.replace_all(&s, "‹$1›").to_string();
    }
    if let Ok(re) = regex::Regex::new(r"\*\*\*([^*]+)\*\*\*") {
        s = re.replace_all(&s, "$1").to_string();
    }
    if let Ok(re) = regex::Regex::new(r"\*\*([^*]+)\*\*") {
        s = re.replace_all(&s, "$1").to_string();
    }
    if let Ok(re) = regex::Regex::new(r"\*([^*]+)\*") {
        s = re.replace_all(&s, "$1").to_string();
    }
    s
}

fn push_chat_block(lines: &mut Vec<ChatRenderLine>, text: &str, width: usize, fg: style::Color, bg: Option<style::Color>, left_pad: usize) {
    let inner = width.saturating_sub(left_pad);
    for wrapped in wrap_plain_text(text, inner.max(1)) {
        let mut line = String::new();
        line.push_str(&" ".repeat(left_pad));
        line.push_str(&wrapped);
        lines.push(single_span_line(fg, bg, line));
    }
}

fn build_chat_visual_lines(app: &App, width: usize, variant: ChatLayoutVariant) -> Vec<ChatRenderLine> {
    let robe_bg = style::Color::Rgb { r: 42, g: 18, b: 21 };
    let robe_fg = style::Color::Rgb { r: 224, g: 208, b: 192 };
    let robe_heading = style::Color::Rgb { r: 232, g: 213, b: 163 };
    let user_bg = style::Color::Rgb { r: 30, g: 36, b: 51 };
    let user_fg = style::Color::Rgb { r: 226, g: 232, b: 240 };
    let thought_bg = style::Color::Rgb { r: 20, g: 15, b: 31 };
    let tool_bg = style::Color::Rgb { r: 13, g: 13, b: 26 };
    let code_fg = style::Color::Rgb { r: 230, g: 237, b: 243 };
    let code_bg = style::Color::Rgb { r: 22, g: 27, b: 34 };
    let mut visual_lines = brand_lines(width, variant);

    if app.chat.messages.len() <= 1 {
        visual_lines.push(single_span_line(style::Color::DarkGrey, None, "  Welcome to Charon. Type a message to begin."));
        visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
    }

    for message in &app.chat.messages {
        match message {
            ChatMessage::User { text } => {
                push_chat_block(&mut visual_lines, text, width, user_fg, Some(user_bg), 1);
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::Assistant { text, streaming } => {
                let mut code_block = false;
                for raw in text.lines() {
                    let trimmed = raw.trim_start();
                    if trimmed.starts_with("```") {
                        code_block = !code_block;
                        let label = trimmed.trim_start_matches("```").trim();
                        let border = if code_block {
                            if label.is_empty() { " ┌ code".to_string() } else { format!(" ┌ {}", label) }
                        } else {
                            " └".to_string()
                        };
                        push_chat_block(&mut visual_lines, &border, width, style::Color::DarkGrey, Some(code_bg), 1);
                        continue;
                    }
                    if code_block {
                        push_chat_block(&mut visual_lines, raw, width, code_fg, Some(code_bg), 2);
                        continue;
                    }
                    if let Some(rest) = trimmed.strip_prefix("### ") {
                        push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, robe_heading, Some(robe_bg), 1);
                        continue;
                    }
                    if let Some(rest) = trimmed.strip_prefix("## ") {
                        push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, robe_heading, Some(robe_bg), 1);
                        continue;
                    }
                    if let Some(rest) = trimmed.strip_prefix("# ") {
                        push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, style::Color::Rgb { r: 240, g: 230, b: 208 }, Some(robe_bg), 1);
                        continue;
                    }
                    if trimmed.starts_with("- ") || trimmed.starts_with("* ") {
                        let body = normalize_inline_markdown(&trimmed[2..]);
                        push_chat_block(&mut visual_lines, &format!(" • {}", body), width, robe_fg, Some(robe_bg), 1);
                        continue;
                    }
                    let numbered = trimmed.chars().take_while(|c| c.is_ascii_digit()).count();
                    if numbered > 0 && trimmed.chars().nth(numbered) == Some('.') {
                        push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(trimmed)), width, robe_fg, Some(robe_bg), 1);
                        continue;
                    }
                    push_chat_block(&mut visual_lines, &normalize_inline_markdown(raw), width, robe_fg, Some(robe_bg), 1);
                }
                if *streaming {
                    visual_lines.push(single_span_line(robe_fg, Some(robe_bg), "  ▊"));
                }
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::Thinking { text, streaming } => {
                let header = if text.is_empty() { "  ⟪ visible thoughts ⟫".to_string() } else { format!("  ⟪ visible thoughts ⟫ {}", text.lines().next().unwrap_or("")) };
                push_chat_block(&mut visual_lines, &header, width, style::Color::Rgb { r: 221, g: 214, b: 254 }, Some(thought_bg), 0);
                for extra in text.lines().skip(1) {
                    push_chat_block(&mut visual_lines, &format!(" {}", extra), width, style::Color::Rgb { r: 196, g: 181, b: 253 }, Some(thought_bg), 0);
                }
                if *streaming {
                    visual_lines.push(single_span_line(style::Color::Rgb { r: 221, g: 214, b: 254 }, Some(thought_bg), "  ▊"));
                }
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::ToolCall { tool, summary } => {
                let (icon, fg, bg) = match tool.as_str() {
                    "Read" => ("📄", style::Color::Rgb { r: 110, g: 231, b: 183 }, style::Color::Rgb { r: 13, g: 26, b: 20 }),
                    "Write" => ("✏", style::Color::Rgb { r: 251, g: 191, b: 36 }, style::Color::Rgb { r: 26, g: 26, b: 13 }),
                    "Edit" => ("🔧", style::Color::Rgb { r: 245, g: 158, b: 11 }, style::Color::Rgb { r: 26, g: 20, b: 13 }),
                    "Bash" => ("⚡", style::Color::Rgb { r: 147, g: 197, b: 253 }, style::Color::Rgb { r: 13, g: 13, b: 26 }),
                    _ => ("⚙", style::Color::Rgb { r: 165, g: 180, b: 252 }, tool_bg),
                };
                let text = if summary.is_empty() { format!(" {} {}", icon, tool) } else { format!(" {} {}  {}", icon, tool, summary) };
                push_chat_block(&mut visual_lines, &text, width, fg, Some(bg), 1);
            }
            ChatMessage::ToolResult { tool, content, is_error } => {
                let fg = if *is_error { style::Color::Rgb { r: 252, g: 165, b: 165 } } else { style::Color::Rgb { r: 165, g: 180, b: 252 } };
                push_chat_block(&mut visual_lines, &format!(" ▶ {}", tool), width, fg, Some(tool_bg), 1);
                for line in content.lines().take(8) {
                    push_chat_block(&mut visual_lines, &format!("   {}", line), width, fg, Some(tool_bg), 0);
                }
                if content.lines().count() > 8 {
                    push_chat_block(&mut visual_lines, "   ...", width, style::Color::DarkGrey, Some(tool_bg), 0);
                }
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::Status { text } => {
                push_chat_block(&mut visual_lines, &format!("  {}", text), width, style::Color::Yellow, None, 0);
            }
            ChatMessage::Error { text } => {
                push_chat_block(&mut visual_lines, &format!("  ✗ {}", text), width, style::Color::Rgb { r: 248, g: 113, b: 113 }, None, 0);
            }
            ChatMessage::Stderr { text } => {
                push_chat_block(&mut visual_lines, &format!("  stderr: {}", text), width, style::Color::Rgb { r: 248, g: 113, b: 113 }, None, 0);
            }
        }
    }
    visual_lines
}

fn chat_line_text(line: &ChatRenderLine) -> String {
    let mut out = String::new();
    for span in &line.spans {
        out.push_str(&span.text);
    }
    out
}

fn draw_chat_line<W: Write>(stdout: &mut W, x: u16, y: u16, width: usize, line: &ChatRenderLine, selection_cols: Option<(usize, usize)>) -> io::Result<()> {
    stdout.queue(cursor::MoveTo(x, y))?;
    let mut col = 0usize;
    for span in &line.spans {
        for ch in span.text.chars() {
            if col >= width { break; }
            let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
            stdout.queue(style::SetForegroundColor(if selected { style::Color::Black } else { span.fg }))?;
            stdout.queue(style::SetBackgroundColor(if selected {
                style::Color::Rgb { r: 226, g: 232, b: 240 }
            } else {
                line.bg.unwrap_or(style::Color::Reset)
            }))?;
            write!(stdout, "{}", ch)?;
            col += 1;
        }
        if col >= width { break; }
    }
    while col < width {
        let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
        stdout.queue(style::SetForegroundColor(if selected { style::Color::Black } else { style::Color::Reset }))?;
        stdout.queue(style::SetBackgroundColor(if selected {
            style::Color::Rgb { r: 226, g: 232, b: 240 }
        } else {
            line.bg.unwrap_or(style::Color::Reset)
        }))?;
        write!(stdout, " ")?;
        col += 1;
    }
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    stdout.queue(style::SetBackgroundColor(style::Color::Reset))?;
    Ok(())
}

fn charon_box_border_color() -> style::Color {
    style::Color::Rgb { r: 59, g: 24, b: 28 }
}

fn draw_chat_border<W: Write>(stdout: &mut W, area: Rect, title: &str) -> io::Result<()> {
    render::render_border_colored(stdout, area, title, match charon_box_border_color() {
        style::Color::Rgb { r, g, b } => crossterm::style::Color::Rgb { r, g, b },
        _ => crossterm::style::Color::Rgb { r: 59, g: 24, b: 28 },
    })
}

fn fmt_k(n: u64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", (n as f64) / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", (n as f64) / 1_000.0)
    } else {
        n.to_string()
    }
}

fn session_info_tokens<'a>(app: &'a App) -> Option<&'a Value> {
    app.chat
        .refresh_payload
        .as_ref()
        .and_then(|p| p.get("session_info"))
        .and_then(|i| i.get("tokens"))
}

fn draw_chat_status_line<W: Write>(stdout: &mut W, y: u16, width: u16, left: &str, right: &str, right_color: style::Color) -> io::Result<()> {
    let total = width as usize;
    let left_len = left.chars().count();
    let right_len = right.chars().count();
    let pad = total.saturating_sub(left_len + right_len).max(1);
    stdout.queue(cursor::MoveTo(0, y))?;
    stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 74, g: 74, b: 94 }))?;
    write!(stdout, "{}", left)?;
    write!(stdout, "{}", " ".repeat(pad))?;
    stdout.queue(style::SetForegroundColor(right_color))?;
    let right_vis: String = right.chars().take(total.saturating_sub(left_len + pad)).collect();
    write!(stdout, "{}", right_vis)?;
    let used = left_len + pad + right_vis.chars().count();
    if used < total {
        write!(stdout, "{}", " ".repeat(total - used))?;
    }
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn draw_info_panel<W: Write>(stdout: &mut W, app: &App, area: Rect) -> io::Result<()> {
    draw_chat_border(stdout, area, "")?;

    let info = app.chat.refresh_payload.as_ref().and_then(|p| p.get("session_info"));
    let backend_tasks = info.and_then(|i| i.get("tasks")).and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let goals = info.and_then(|i| i.get("goals")).and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let user_model = info.and_then(|i| i.get("user_model")).and_then(|v| v.as_str()).unwrap_or("");
    let tokens = info.and_then(|i| i.get("tokens"));
    let fmt_k = |n: u64| if n >= 1000 { format!("{:.1}k", (n as f64) / 1000.0) } else { n.to_string() };

    let mut lines: Vec<(style::Color, String)> = Vec::new();
    let tabs = ["Outcomes", "Goals", "Model"];
    let tab_text = tabs.iter().enumerate().map(|(i, tab)| {
        if i == app.chat.info_pane_tab {
            format!("[{}]", tab)
        } else {
            tab.to_string()
        }
    }).collect::<Vec<_>>().join("  ");
    let tab_width = tab_text.chars().count();
    let tab_pad = ((area.width as usize).saturating_sub(tab_width)) / 2;
    let tab_line = format!("{}{}", " ".repeat(tab_pad), tab_text);
    lines.push((style::Color::Rgb { r: 196, g: 181, b: 253 }, tab_line));
    lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
    let footer_reserve = 8usize;
    let content_budget = (area.height as usize).saturating_sub(2 + footer_reserve);

    match app.chat.info_pane_tab {
        0 => {
            let mut seen = std::collections::HashSet::new();
            let mut outcome_rows: Vec<(String, String, Option<u64>, Option<u64>, Option<usize>, Option<u64>, Option<u64>)> = Vec::new();

            for task in backend_tasks.iter().rev() {
                let title = task.get("title").or_else(|| task.get("summary")).or_else(|| task.get("instruction")).and_then(|v| v.as_str()).unwrap_or("").trim().to_string();
                if title.is_empty() {
                    continue;
                }
                let key = title.to_lowercase();
                if !seen.insert(key) {
                    continue;
                }
                outcome_rows.push((
                    title,
                    task.get("status").and_then(|v| v.as_str()).unwrap_or("completed").to_string(),
                    task.get("tool_calls").and_then(|v| v.as_u64()),
                    task.get("turns").and_then(|v| v.as_u64()),
                    task.get("files_touched").and_then(|v| v.as_array()).map(|a| a.len()),
                    task.get("tokens_in").and_then(|v| v.as_u64()),
                    task.get("tokens_out").and_then(|v| v.as_u64()),
                ));
            }

            if outcome_rows.len() < 8 {
                for provisional in app.chat.provisional_outcomes.iter().rev() {
                    let key = provisional.summary.to_lowercase();
                    if seen.insert(key) {
                        outcome_rows.push((
                            provisional.summary.clone(),
                            if provisional.done { "completed".to_string() } else { "active".to_string() },
                            None,
                            None,
                            None,
                            None,
                            None,
                        ));
                    }
                    if outcome_rows.len() >= 8 {
                        break;
                    }
                }
            }

            if outcome_rows.is_empty() {
                lines.push((style::Color::DarkGrey, "No outcomes yet.".to_string()));
                lines.push((style::Color::DarkGrey, "Submit a concrete request".to_string()));
                lines.push((style::Color::DarkGrey, "to track it here.".to_string()));
            } else {
                let mut used = 0usize;
                for (title, status, tool_calls, turns, file_count, tokens_in_row, tokens_out_row) in outcome_rows.into_iter().take(8) {
                    if used >= content_budget {
                        break;
                    }
                    let icon = match status.as_str() {
                        "failed" => "[-]",
                        "active" | "running" | "pending" => "[~]",
                        _ => "[+]",
                    };
                    let color = match status.as_str() {
                        "failed" => style::Color::Rgb { r: 239, g: 68, b: 68 },
                        "active" | "running" | "pending" => style::Color::Rgb { r: 245, g: 158, b: 11 },
                        _ => style::Color::Rgb { r: 34, g: 197, b: 94 },
                    };
                    lines.push((color, format!("{} {}", icon, title)));
                    used += 1;
                    let mut meta = Vec::new();
                    if let Some(tool_calls) = tool_calls { meta.push(format!("{}t", tool_calls)); }
                    if let Some(turns) = turns { meta.push(format!("{}↻", turns)); }
                    if let Some(file_count) = file_count { meta.push(format!("{}f", file_count)); }
                    if let Some(tokens_in_row) = tokens_in_row { if tokens_in_row > 0 { meta.push(format!("{}↑", fmt_k(tokens_in_row))); } }
                    if let Some(tokens_out_row) = tokens_out_row { if tokens_out_row > 0 { meta.push(format!("{}↓", fmt_k(tokens_out_row))); } }
                    if !meta.is_empty() && used < content_budget {
                        lines.push((style::Color::DarkGrey, format!("  {}", meta.join("  "))));
                        used += 1;
                    }
                }
            }
        }
        1 => {
            if let Some(summary) = info.and_then(|i| i.get("goal_summary")) {
                let active_goal_id = summary.get("active_goal_id").and_then(|v| v.as_str()).unwrap_or("");
                let proposed = summary.get("proposed").and_then(|v| v.as_u64()).unwrap_or(0);
                let confirmed = summary.get("confirmed").and_then(|v| v.as_u64()).unwrap_or(0);
                let executing = summary.get("executing").and_then(|v| v.as_u64()).unwrap_or(0);
                let verifying = summary.get("verifying").and_then(|v| v.as_u64()).unwrap_or(0);
                let backlog = summary.get("backlog").and_then(|v| v.as_u64()).unwrap_or(0);
                lines.push((style::Color::DarkGrey, format!("proposed:{}  confirmed:{}  exec:{}  verify:{}", proposed, confirmed, executing, verifying)));
                let active_suffix = if active_goal_id.is_empty() {
                    String::new()
                } else {
                    format!("  active:{}", active_goal_id.chars().take(8).collect::<String>())
                };
                lines.push((style::Color::DarkGrey, format!("backlog:{}{}", backlog, active_suffix)));
                lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
            }
            if goals.is_empty() {
                lines.push((style::Color::DarkGrey, "No goals detected.".to_string()));
            } else {
                for goal in goals.iter().take(8) {
                    let status = goal.get("status").and_then(|v| v.as_str()).unwrap_or("backlog");
                    let icon = match status {
                        "active" => "●",
                        "proposed" => "◆",
                        "confirmed" => "◉",
                        "executing" => "▶",
                        "verifying" => "?",
                        "completed" => "✓",
                        "failed" => "✗",
                        _ => "○",
                    };
                    let color = match status {
                        "active" => style::Color::Rgb { r: 34, g: 197, b: 94 },
                        "proposed" => style::Color::Rgb { r: 167, g: 139, b: 250 },
                        "confirmed" => style::Color::Rgb { r: 96, g: 165, b: 250 },
                        "executing" => style::Color::Rgb { r: 245, g: 158, b: 11 },
                        "verifying" => style::Color::Rgb { r: 250, g: 204, b: 21 },
                        "completed" => style::Color::Rgb { r: 110, g: 231, b: 183 },
                        "failed" => style::Color::Rgb { r: 239, g: 68, b: 68 },
                        _ => style::Color::DarkGrey,
                    };
                    let title = goal.get("title").and_then(|v| v.as_str()).unwrap_or("");
                    lines.push((color, format!("{} {}", icon, title)));
                    let mut meta = vec![format!("[{}]", status)];
                    if let Some(scope) = goal.get("scope").and_then(|v| v.as_str()) {
                        if !scope.is_empty() { meta.push(scope.to_string()); }
                    }
                    if let Some(criteria) = goal.get("criteria").and_then(|v| v.as_array()) {
                        if !criteria.is_empty() { meta.push(format!("{} criteria", criteria.len())); }
                    }
                    if status == "proposed" {
                        meta.push("/confirm /reject".to_string());
                    }
                    lines.push((style::Color::DarkGrey, format!("  {}", meta.join(" "))));
                    if let Some(criteria) = goal.get("criteria").and_then(|v| v.as_array()) {
                        for criterion in criteria.iter().take(2) {
                            if let Some(text) = criterion.as_str() {
                                lines.push((style::Color::Rgb { r: 148, g: 163, b: 184 }, format!("   • {}", text)));
                            }
                        }
                        if criteria.len() > 2 {
                            lines.push((style::Color::DarkGrey, format!("   … {} more", criteria.len() - 2)));
                        }
                    }
                }
            }
        }
        _ => {
            if user_model.trim().is_empty() {
                lines.push((style::Color::DarkGrey, "No user model yet.".to_string()));
                lines.push((style::Color::DarkGrey, "Charon learns your".to_string()));
                lines.push((style::Color::DarkGrey, "preferences over time.".to_string()));
            } else {
                for line in user_model.lines().filter(|l| !l.trim().chars().all(|c| c == '═')) {
                    lines.push((style::Color::Rgb { r: 212, g: 196, b: 168 }, line.to_string()));
                }
            }
        }
    }

    let max_content_end = 2 + content_budget;
    if lines.len() > max_content_end {
        lines.truncate(max_content_end);
    }

    lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
    let chat_in = tokens.and_then(|t| t.get("chat_in")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.input_tokens);
    let chat_out = tokens.and_then(|t| t.get("chat_out")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.output_tokens);
    lines.push((style::Color::DarkGrey, format!("chat: {}↑ {}↓", fmt_k(chat_in), fmt_k(chat_out))));
    if let Some(max_ctx) = tokens.and_then(|t| t.get("max_context")).and_then(|v| v.as_u64()).or(app.chat.usage.context_window) {
        if max_ctx > 0 {
            lines.push((style::Color::DarkGrey, format!("max ctx: {}", fmt_k(max_ctx))));
        }
    }
    if let Some(consol) = tokens.and_then(|t| t.get("consolidation_tokens")).and_then(|v| v.as_u64()) {
        if consol > 0 {
            lines.push((style::Color::DarkGrey, format!("bg: ~{} consol", fmt_k(consol))));
        }
    }
    lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
    lines.push((style::Color::DarkGrey, "←/→ or Ctrl+I: switch".to_string()));
    lines.push((style::Color::DarkGrey, "Shift+Tab: back  Ctrl+P: hide".to_string()));

    for (i, (color, line)) in lines.into_iter().take(area.height as usize).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        stdout.queue(style::SetForegroundColor(color))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        let pad = (area.width as usize).saturating_sub(visible.chars().count());
        write!(stdout, "{}{}", visible, " ".repeat(pad))?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    Ok(())
}

fn onboarding_lines(app: &App) -> Vec<String> {
    if app.chat.refresh_payload.is_none() || app.chat.onboarding_complete() || app.chat.engine_ready() {
        return vec![];
    }
    let step = app.chat.onboarding_step();
    let provider = app.chat.onboarding_provider();
    let project = app.chat.onboarding_project();
    let mut lines = vec!["Setup required before full chat use.".to_string()];
    match step.as_str() {
        "provider-mode" | "" => {
            lines.push("Choose a provider: /setup provider claude-code | codex | lmstudio | api".to_string());
        }
        "provider-auth" => {
            lines.push(format!("Provider selected: {}", if provider.is_empty() { "unknown" } else { &provider }));
            lines.push("Finish authentication in the popup or use /setup auth-code <CODE>".to_string());
        }
        "model" => {
            lines.push(format!("Provider selected: {}", if provider.is_empty() { "unknown" } else { &provider }));
            lines.push("Choose a model with /setup model or /model".to_string());
        }
        "complete" | "done" => {
            lines.push("Setup is almost done. Use /setup complete if needed.".to_string());
        }
        other => {
            lines.push(format!("Current setup step: {}", other));
        }
    }
    if !project.is_empty() {
        lines.push(format!("Project: {}", project));
    }
    lines.push("Tab fills menu items. Enter executes them.".to_string());
    lines
}

fn draw_popup_box<W: Write>(stdout: &mut W, area: Rect, title: &str, lines: &[(style::Color, String)]) -> io::Result<()> {
    render::render_border(stdout, area, title, true)?;
    for (i, (color, line)) in lines.iter().take(area.height as usize).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        stdout.queue(style::SetForegroundColor(*color))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}", visible)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    Ok(())
}

fn chat_content_area(app: &App, w: u16, h: u16) -> Rect {
    let variant = chat_layout_variant(w, h);
    let reserved_bottom = chat_reserved_bottom(app, variant);
    let info_pane_w = if app.chat.info_pane_open && variant == ChatLayoutVariant::Full && w >= 100 {
        ((w as f32) * 0.15).floor().max(18.0) as u16
    } else {
        0
    };
    let pane_gutter = if info_pane_w > 0 { 5 } else { 0 };
    let left_w = if info_pane_w > 0 { w.saturating_sub(info_pane_w + pane_gutter) } else { w };
    Rect {
        x: if variant == ChatLayoutVariant::Tiny { 1 } else { 2 },
        y: if variant == ChatLayoutVariant::Tiny { 1 } else { 2 },
        width: left_w.saturating_sub(if variant == ChatLayoutVariant::Tiny { 2 } else { 4 }),
        height: h.saturating_sub(reserved_bottom + if variant == ChatLayoutVariant::Tiny { 1 } else { 2 }),
    }
}

fn chat_selection_bounds(app: &App) -> Option<(ChatTextPoint, ChatTextPoint)> {
    let a = app.chat.selection_anchor?;
    let b = app.chat.selection_focus?;
    if (a.row, a.col) <= (b.row, b.col) { Some((a, b)) } else { Some((b, a)) }
}

fn chat_visual_window_len(total_rows: usize, area: Rect, scroll: usize) -> (usize, usize) {
    let max_lines = area.height as usize;
    let clamped = scroll.min(total_rows.saturating_sub(max_lines));
    let visible_count = total_rows.saturating_sub(clamped);
    let start = visible_count.saturating_sub(max_lines);
    let end = start + max_lines.min(visible_count);
    (start.min(total_rows), end.min(total_rows))
}

fn chat_point_at_mouse(lines: &[ChatRenderLine], area: Rect, scroll: usize, x: u16, y: u16) -> Option<ChatTextPoint> {
    if lines.is_empty() || area.width == 0 || area.height == 0 {
        return None;
    }
    let clamped_x = x.clamp(area.x, area.x.saturating_add(area.width).saturating_sub(1));
    let clamped_y = y.clamp(area.y, area.y.saturating_add(area.height).saturating_sub(1));
    let (start, end) = chat_visual_window_len(lines.len(), area, scroll);
    let visible = &lines[start..end];
    if visible.is_empty() {
        return None;
    }
    let rel_y = clamped_y.saturating_sub(area.y) as usize;
    let row_idx = start + rel_y.min(visible.len().saturating_sub(1));
    let text_len = lines.get(row_idx).map(|r| chat_line_text(r).chars().count()).unwrap_or(0);
    let col = (clamped_x.saturating_sub(area.x) as usize).min(text_len);
    Some(ChatTextPoint { row: row_idx, col })
}

fn chat_selection_text(lines: &[ChatRenderLine], bounds: (ChatTextPoint, ChatTextPoint)) -> String {
    let (start, end) = bounds;
    let mut out = String::new();
    for row_idx in start.row..=end.row {
        let line = lines.get(row_idx).map(chat_line_text).unwrap_or_default();
        let chars: Vec<char> = line.chars().collect();
        let line_len = chars.len();
        let from = if row_idx == start.row { start.col.min(line_len) } else { 0 };
        let to = if row_idx == end.row { end.col.min(line_len) } else { line_len };
        if from < to {
            out.extend(chars[from..to].iter().copied());
        }
        if row_idx != end.row {
            out.push('\n');
        }
    }
    out
}

fn chat_selection_cols_for_row(row: usize, bounds: Option<(ChatTextPoint, ChatTextPoint)>) -> Option<(usize, usize)> {
    let (start, end) = bounds?;
    if row < start.row || row > end.row {
        return None;
    }
    if start.row == end.row {
        return Some((start.col, end.col));
    }
    if row == start.row {
        return Some((start.col, usize::MAX));
    }
    if row == end.row {
        return Some((0, end.col));
    }
    Some((0, usize::MAX))
}

fn chat_context_menu_items(app: &App) -> Vec<&'static str> {
    let mut items = Vec::new();
    if chat_selection_bounds(app).is_some() {
        items.push("Copy selection");
        items.push("Clear selection");
    }
    items.push("Scroll to bottom");
    items
}

fn chat_context_menu_area(app: &App, w: u16, h: u16) -> Option<Rect> {
    let menu = app.chat.context_menu.as_ref()?;
    let items = chat_context_menu_items(app);
    let width = items.iter().map(|s| s.chars().count()).max().unwrap_or(12) as u16 + 2;
    let height = items.len() as u16;
    let x = menu.x.min(w.saturating_sub(width + 2)).max(1);
    let y = menu.y.min(h.saturating_sub(height + 2)).max(1);
    Some(Rect { x, y, width, height })
}

fn activate_chat_context_menu(app: &mut App, w: u16, h: u16) -> bool {
    let selected = app.chat.context_menu.as_ref().map(|m| m.selected).unwrap_or(0);
    let items = chat_context_menu_items(app);
    let choice = items.get(selected).copied().unwrap_or("");
    match choice {
        "Copy selection" => {
            if let Some(bounds) = chat_selection_bounds(app) {
                let area = chat_content_area(app, w, h);
                let lines = build_chat_visual_lines(app, area.width as usize, chat_layout_variant(w, h));
                let text = chat_selection_text(&lines, bounds);
                app.chat.context_menu = None;
                return !text.is_empty() && copy_to_clipboard(&text);
            }
        }
        "Clear selection" => {
            app.chat.selection_anchor = None;
            app.chat.selection_focus = None;
            app.chat.selection_dragging = false;
            app.chat.context_menu = None;
            return true;
        }
        "Scroll to bottom" => {
            app.chat.scroll = 0;
            app.chat.context_menu = None;
            return true;
        }
        _ => {}
    }
    app.chat.context_menu = None;
    false
}

fn draw_chat<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    let variant = chat_layout_variant(w, h);
    let reserved_bottom = chat_reserved_bottom(app, variant);
    let info_pane_w = if app.chat.info_pane_open && variant == ChatLayoutVariant::Full && w >= 100 {
        ((w as f32) * 0.15).floor().max(18.0) as u16
    } else {
        0
    };
    let pane_gutter = if info_pane_w > 0 { 5 } else { 0 };
    let left_w = if info_pane_w > 0 { w.saturating_sub(info_pane_w + pane_gutter) } else { w };
    let content = chat_content_area(app, w, h);
    let content_w = content.width as usize;
    let visual_lines = build_chat_visual_lines(app, content_w, variant);
    let max_lines = content.height as usize;
    let max_scroll = visual_lines.len().saturating_sub(max_lines);
    let scroll = app.chat.scroll.min(max_scroll);
    let (start, end) = chat_visual_window_len(visual_lines.len(), content, scroll);
    let slice = &visual_lines[start..end];
    let selection_bounds = chat_selection_bounds(app);

    for (i, line) in slice.iter().enumerate() {
        let row_idx = start + i;
        let selection_cols = chat_selection_cols_for_row(row_idx, selection_bounds);
        draw_chat_line(stdout, content.x, content.y + i as u16, content_w, line, selection_cols)?;
    }

    if info_pane_w > 0 {
        let area = Rect {
            x: left_w + 2,
            y: 2,
            width: info_pane_w.saturating_sub(2),
            height: h.saturating_sub(4),
        };
        draw_info_panel(stdout, app, area)?;
    }

    let provider = app.chat.provider_model();
    let onboarding = if app.chat.refresh_payload.is_none() { "loading" } else if app.chat.onboarding_complete() { "complete" } else { "setup" };
    let effort = "medium";
    let onboarding_project = app.chat.onboarding_project();
    let project_name = onboarding_project.split('/').filter(|s| !s.is_empty()).last().unwrap_or("");
    let session_display = if app.chat.session_id.is_empty() { "none" } else { &app.chat.session_id };
    let left1 = if app.chat.onboarding_complete() {
        let mut parts = vec![session_display.to_string()];
        if !project_name.is_empty() {
            parts.push(project_name.to_string());
        }
        format!("  {}", parts.join("  "))
    } else {
        format!("  charon  onboarding:{}", onboarding)
    };
    let right1 = format!("{} {}  effort:{}", if provider.contains("api") { "(api)" } else { "(provider)" }, provider, effort);

    let tokens = session_info_tokens(app);
    let chat_in = tokens.and_then(|t| t.get("chat_in")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.input_tokens);
    let chat_out = tokens.and_then(|t| t.get("chat_out")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.output_tokens);
    let max_ctx = tokens.and_then(|t| t.get("max_context")).and_then(|v| v.as_u64()).or(app.chat.usage.context_window).unwrap_or(0);
    let goal_inf = tokens.and_then(|t| t.get("goal_inference_tokens")).and_then(|v| v.as_u64()).unwrap_or(0);
    let consol = tokens.and_then(|t| t.get("consolidation_tokens")).and_then(|v| v.as_u64()).unwrap_or(0);
    let ctx = app.chat.usage.context_pct.map(|v| format!("{:.0}%", v)).unwrap_or_else(|| "-".to_string());
    let left2 = if app.chat.onboarding_complete() {
        let mut parts = vec!["  ♡ interactive".to_string(), format!("ctx:{}", ctx), format!("chat:{}↑ {}↓", fmt_k(chat_in), fmt_k(chat_out))];
        if max_ctx > 0 {
            parts.push(format!("max:{}", fmt_k(max_ctx)));
        }
        if goal_inf > 0 {
            parts.push(format!("goal:{}", fmt_k(goal_inf)));
        }
        if consol > 0 {
            parts.push(format!("bg:{}", fmt_k(consol)));
        }
        if let Some(hint) = app.chat.orchestration_parse_hint() {
            parts.push(hint);
        }
        parts.push(format!("thoughts:{}{}", if app.chat.show_thoughts { "on" } else { "off" }, if app.chat.show_timestamps { "  ⏱" } else { "" }));
        parts.join("  ")
    } else {
        "  type / for commands".to_string()
    };
    let right2 = if app.chat.streaming {
        "Esc:/interrupt  Enter:steer  /queue:follow-up".to_string()
    } else {
        "F1:chat  F2:dash  F3:sessions  Ctrl+P:info".to_string()
    };

    if chat_rowing_active(app) && variant != ChatLayoutVariant::Tiny {
        let frame = ((animation_clock_start().elapsed().as_millis() / 300) % 4) as usize;
        let activity_lines = rowing_indicator_lines(frame);
        // Lift the 3-line rowing animation one row higher so the boat/water
        // line clears the bottom input/status area instead of being clipped.
        let activity_y = h.saturating_sub(9);
        for (i, line) in activity_lines.iter().enumerate() {
            if activity_y + i as u16 >= h.saturating_sub(5) {
                break;
            }
            draw_chat_line(stdout, 2, activity_y + i as u16, left_w.saturating_sub(4) as usize, line, None)?;
        }
    }

    let input_area = if variant == ChatLayoutVariant::Tiny {
        let input_area = Rect {
            x: 1,
            y: h.saturating_sub(3),
            width: left_w.saturating_sub(2),
            height: 1,
        };
        if input_area.width > 0 {
            draw_chat_border(stdout, input_area, "")?;
            stdout.queue(cursor::MoveTo(input_area.x, input_area.y))?;
            stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 248, g: 250, b: 252 }))?;
            let prompt = format!("> {}", app.chat.input);
            let visible: String = prompt.chars().take(input_area.width as usize).collect();
            let pad = (input_area.width as usize).saturating_sub(visible.chars().count());
            write!(stdout, "{}{}", visible, " ".repeat(pad))?;
            stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
            Some(input_area)
        } else {
            None
        }
    } else {
        let input_area = Rect { x: 1, y: h.saturating_sub(5), width: left_w.saturating_sub(2), height: 1 };
        draw_chat_border(stdout, input_area, "")?;
        stdout.queue(cursor::MoveTo(input_area.x, input_area.y))?;
        stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 248, g: 250, b: 252 }))?;
        let prompt = format!("> {}", app.chat.input);
        let visible: String = prompt.chars().take(input_area.width as usize).collect();
        let pad = (input_area.width as usize).saturating_sub(visible.chars().count());
        write!(stdout, "{}{}", visible, " ".repeat(pad))?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        Some(input_area)
    };

    match variant {
        ChatLayoutVariant::Full => {
            draw_chat_status_line(stdout, h.saturating_sub(3), left_w, &left1, &right1, style::Color::Rgb { r: 85, g: 85, b: 112 })?;
            draw_chat_status_line(stdout, h.saturating_sub(2), left_w, &left2, &right2, if app.chat.streaming { style::Color::Rgb { r: 180, g: 83, b: 9 } } else { style::Color::Rgb { r: 59, g: 59, b: 79 } })?;
        }
        ChatLayoutVariant::Mid => {
            let combined = format!("{}  │  {}", left2, right2);
            draw_chat_status_line(stdout, h.saturating_sub(2), left_w, &combined, "", style::Color::Rgb { r: 59, g: 59, b: 79 })?;
        }
        ChatLayoutVariant::Tiny => {
            let hint = if app.chat.streaming { "Enter: steer" } else { "interactive" };
            draw_chat_status_line(stdout, h.saturating_sub(1), left_w, &format!("  {}", hint), "", style::Color::Rgb { r: 59, g: 59, b: 79 })?;
        }
    }

    let helper_lines = onboarding_lines(app);
    if variant != ChatLayoutVariant::Tiny && !helper_lines.is_empty() && !app.chat.menu_open() && app.chat.auth_url.is_none() && !app.chat.approval_open() {
        let helper_w = (w.saturating_sub(8)).min(72);
        let helper_h = ((helper_lines.len() as u16) + 2).min(8);
        let helper = Rect { x: 3, y: 3, width: helper_w.saturating_sub(2), height: helper_h.saturating_sub(2) };
        let lines: Vec<(style::Color, String)> = helper_lines.into_iter().map(|line| (style::Color::Rgb { r: 250, g: 204, b: 21 }, line)).collect();
        draw_popup_box(stdout, helper, "setup", &lines)?;
    }

    if app.chat.menu_open() && variant != ChatLayoutVariant::Tiny {
        let anchor = input_area.unwrap_or(Rect { x: 1, y: h.saturating_sub(2), width: left_w.saturating_sub(2), height: 1 });
        let menu_w = anchor.width.min(96).max(24);
        let desired_h = (app.chat.menu_items.len() as u16).min(10) + 2;
        let menu_h = desired_h.min(anchor.y.saturating_sub(2).max(4));
        let menu_x = anchor.x.saturating_sub(1);
        let menu_y = anchor.y.saturating_sub(menu_h + 1);
        let area = Rect { x: menu_x + 1, y: menu_y + 1, width: menu_w.saturating_sub(2), height: menu_h.saturating_sub(2) };
        render::render_border(stdout, area, app.chat.menu_title.as_deref().unwrap_or("menu"), true)?;
        for (i, item) in app.chat.menu_items.iter().take(area.height as usize).enumerate() {
            stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
            let selected = i == app.chat.menu_index;
            stdout.queue(style::SetForegroundColor(if selected { style::Color::Rgb { r: 196, g: 181, b: 253 } } else if item.executable { style::Color::Rgb { r: 226, g: 232, b: 240 } } else { style::Color::Rgb { r: 148, g: 163, b: 184 } }))?;
            let mut line = if item.age.is_empty() {
                format!("{} {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc)
            } else {
                format!("{} {}  {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc, item.age)
            };
            line = line.chars().take(area.width as usize).collect();
            write!(stdout, "{}", line)?;
            stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        }
    }

    if variant != ChatLayoutVariant::Tiny {
        if let Some(area) = chat_context_menu_area(app, w, h) {
            render::render_border(stdout, area, "", true)?;
            let items = chat_context_menu_items(app);
            let selected = app.chat.context_menu.as_ref().map(|m| m.selected).unwrap_or(0);
            for (i, item) in items.iter().take(area.height as usize).enumerate() {
                stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
                stdout.queue(style::SetForegroundColor(if i == selected { style::Color::Rgb { r: 196, g: 181, b: 253 } } else { style::Color::Rgb { r: 226, g: 232, b: 240 } }))?;
                let text = format!("{} {}", if i == selected { "▸" } else { " " }, item);
                let visible: String = text.chars().take(area.width as usize).collect();
                let pad = (area.width as usize).saturating_sub(visible.chars().count());
                write!(stdout, "{}{}", visible, " ".repeat(pad))?;
                stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
            }
        }
    }

    if variant != ChatLayoutVariant::Tiny {
        if let Some(approval) = app.chat.approval.as_ref() {
            let popup_w = (w.saturating_sub(12)).min(90);
            let popup_h = 9u16.min(h.saturating_sub(6));
            let popup_x = (w.saturating_sub(popup_w)) / 2;
            let popup_y = (h.saturating_sub(popup_h)) / 2;
            let area = Rect { x: popup_x + 1, y: popup_y + 1, width: popup_w.saturating_sub(2), height: popup_h.saturating_sub(2) };
            let risk_color = match approval.risk.as_str() {
                "dangerous" => style::Color::Rgb { r: 239, g: 68, b: 68 },
                "network" => style::Color::Rgb { r: 245, g: 158, b: 11 },
                _ => style::Color::Rgb { r: 99, g: 102, b: 241 },
            };
            let mut lines = vec![
                (style::Color::Rgb { r: 226, g: 232, b: 240 }, format!("Tool: {}", approval.tool)),
                (risk_color, format!("Risk: {} — {}", approval.risk, approval.reason)),
            ];
            if !approval.params.is_empty() {
                for wrapped in wrap_plain_text(&approval.params, area.width as usize) {
                    lines.push((style::Color::DarkGrey, wrapped));
                }
            }
            let options = ["Approve", "Deny", "Approve all for session"];
            for (idx, label) in options.iter().enumerate() {
                lines.push((if idx == approval.selected { style::Color::Rgb { r: 34, g: 197, b: 94 } } else { style::Color::Rgb { r: 148, g: 163, b: 184 } }, format!("{} {}", if idx == approval.selected { "▸" } else { " " }, label)));
            }
            lines.push((style::Color::DarkGrey, "Use ↑/↓ then Enter. Esc denies.".to_string()));
            draw_popup_box(stdout, area, "approval required", &lines)?;
        }
    }

    if variant != ChatLayoutVariant::Tiny {
        if let Some(url) = app.chat.auth_url.as_ref() {
            let provider = app.chat.auth_provider.as_deref().unwrap_or("provider");
            let popup_w = (w.saturating_sub(8)).min(96);
            let popup_h = 12u16.min(h.saturating_sub(6));
            let popup_x = (w.saturating_sub(popup_w)) / 2;
            let popup_y = (h.saturating_sub(popup_h)) / 2;
            let area = Rect { x: popup_x + 1, y: popup_y + 1, width: popup_w.saturating_sub(2), height: popup_h.saturating_sub(2) };
            let mut lines = vec![
                (style::Color::Rgb { r: 196, g: 181, b: 253 }, format!("Authentication required: {}", provider)),
                (style::Color::Rgb { r: 226, g: 232, b: 240 }, "Browser launch is automatic when possible.".to_string()),
                (style::Color::Rgb { r: 148, g: 163, b: 184 }, "If nothing opened, use one of the actions below:".to_string()),
                (style::Color::DarkGrey, String::new()),
            ];
            for wrapped in wrap_plain_text(url, area.width as usize) {
                lines.push((style::Color::Rgb { r: 96, g: 165, b: 250 }, wrapped));
            }
            lines.push((style::Color::DarkGrey, String::new()));
            let auth_actions = ["Open browser", "Copy link", "Dismiss"];
            for (idx, label) in auth_actions.iter().enumerate() {
                lines.push((
                    if idx == app.chat.auth_action_index { style::Color::Rgb { r: 34, g: 197, b: 94 } } else { style::Color::Rgb { r: 148, g: 163, b: 184 } },
                    format!("{} {}", if idx == app.chat.auth_action_index { "▸" } else { " " }, label),
                ));
            }
            lines.push((style::Color::Rgb { r: 148, g: 163, b: 184 }, "Fallback: /setup auth-code <CODE>".to_string()));
            lines.push((style::Color::Rgb { r: 148, g: 163, b: 184 }, "Use ←/→ or Tab, Enter to act, Esc to dismiss.".to_string()));
            draw_popup_box(stdout, area, "auth", &lines)?;
        }
    }
    Ok(())
}

fn draw_dashboard<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    let left = Rect { x: 1, y: 2, width: (w / 2).saturating_sub(2), height: h.saturating_sub(4) };
    let right = Rect { x: (w / 2) + 1, y: 2, width: (w / 2).saturating_sub(2), height: h.saturating_sub(4) };

    let payload = app.chat.refresh_payload.as_ref();
    let agents = payload_agents(payload);
    let projects = payload_projects(payload);
    let activity = payload_activity(payload);

    let mut left_lines = vec![
        format!("Provider/model: {}", app.chat.provider_model()),
        format!("Session: {}", if app.chat.session_id.is_empty() { "none" } else { &app.chat.session_id }),
        "".to_string(),
        "Agents".to_string(),
    ];
    for (i, agent) in agents.iter().enumerate() {
        let prefix = if i == app.dashboard.selected { ">" } else { " " };
        let name = agent.get("name").and_then(|v| v.as_str()).unwrap_or("agent");
        let status = agent.get("status").and_then(|v| v.as_str()).unwrap_or("idle");
        let role = agent.get("role").and_then(|v| v.as_str()).unwrap_or("");
        left_lines.push(format!("{} {} [{}] {}", prefix, name, status, role));
    }

    let mut right_lines = Vec::new();
    if let Some(agent) = agents.get(app.dashboard.selected) {
        right_lines.push(format!("Name: {}", agent.get("name").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("ID: {}", agent.get("id").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("Status: {}", agent.get("status").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("Role: {}", agent.get("role").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("Project: {}", agent.get("project").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("Goal: {}", agent.get("goal").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push(format!("Summary: {}", agent.get("last_summary").and_then(|v| v.as_str()).unwrap_or("")));
        right_lines.push("".to_string());
        if let Some(stats) = agent.get("shade_stats") {
            right_lines.push(format!("Shade stats: {}", stats));
        }
        if let Some(ledger) = agent.get("ledger").and_then(|v| v.as_array()) {
            right_lines.push("Recent ledger:".to_string());
            for item in ledger.iter().take(5) {
                right_lines.push(format!("- {} {}", item.get("status").and_then(|v| v.as_str()).unwrap_or(""), item.get("task_id").and_then(|v| v.as_str()).unwrap_or("")));
            }
        }
    } else {
        right_lines.push("No agent data yet.".to_string());
    }

    right_lines.push("".to_string());
    right_lines.push(format!("Projects: {}", projects.len()));
    for p in projects.iter().take(6) {
        let name = p.get("name").and_then(|v| v.as_str()).unwrap_or("project");
        let active = p.get("active").and_then(|v| v.as_bool()).unwrap_or(false);
        right_lines.push(format!("- {}{}", name, if active { " [active]" } else { "" }));
    }
    right_lines.push("".to_string());
    right_lines.push("Activity:".to_string());
    for line in activity.iter().rev().take(8) {
        right_lines.push(format!("- {}", line));
    }

    draw_placeholder_panel(stdout, left, "agents", &left_lines)?;
    draw_placeholder_panel(stdout, right, "details", &right_lines)?;
    Ok(())
}

fn inter_agent_event_lines(room: &Value, event_scroll: usize, max_lines: usize) -> Vec<String> {
    let mut lines = Vec::new();
    let title = room.get("title").and_then(|v| v.as_str()).unwrap_or("untitled");
    let kind = room.get("kind").and_then(|v| v.as_str()).unwrap_or("group");
    let status = room.get("status").and_then(|v| v.as_str()).unwrap_or("active");
    let active_speaker = room.get("active_speaker").and_then(|v| v.as_str()).unwrap_or("");
    lines.push(format!("{}  [{}]", title, kind));
    lines.push(format!("status: {}{}", status, if active_speaker.is_empty() { String::new() } else { format!("  • active: {}", active_speaker) }));
    if let Some(parts) = room.get("participants").and_then(|v| v.as_array()) {
        let participants = parts.iter().filter_map(|p| {
            let role = p.get("role").and_then(|v| v.as_str()).unwrap_or("");
            let name = p.get("name").and_then(|v| v.as_str()).unwrap_or("");
            if name.is_empty() { None } else if role.is_empty() { Some(name.to_string()) } else { Some(format!("{} ({})", name, role)) }
        }).collect::<Vec<_>>().join(", ");
        if !participants.is_empty() { lines.push(format!("participants: {}", participants)); }
    }
    lines.push("Wheel/PgUp/PgDn: scroll  •  drag: select  •  y/Ctrl+C: copy  •  d/Delete: remove room".to_string());
    lines.push(String::new());
    if let Some(events) = room.get("events").and_then(|v| v.as_array()) {
        let start = events.len().saturating_sub(max_lines + event_scroll);
        let end = events.len().saturating_sub(event_scroll);
        for event in &events[start.min(events.len())..end.min(events.len())] {
            let ts = event.get("ts").or_else(|| event.get("timestamp")).and_then(|v| v.as_str()).unwrap_or("");
            let typ = event.get("type").and_then(|v| v.as_str()).unwrap_or("event");
            let role = event.get("speaker_role").and_then(|v| v.as_str()).unwrap_or("");
            let label = match typ {
                "conversation_turn_started" => format!("▶ {} turn", if role.is_empty() { "agent" } else { role }),
                "participant_output" => format!("💬 {}", if role.is_empty() { "agent" } else { role }),
                "turn_timeout" => format!("⏱ {} timeout", if role.is_empty() { "agent" } else { role }),
                "conversation_started" => "✓ started".to_string(),
                "conversation_stopped" => "■ stopped".to_string(),
                other => other.to_string(),
            };
            let mut msg = String::new();
            if let Some(summary) = event.get("summary").and_then(|v| v.as_str()) {
                msg.push_str(summary);
            } else if let Some(topic) = event.get("topic").and_then(|v| v.as_str()) {
                msg.push_str(topic);
            } else if let Some(title) = event.get("title").and_then(|v| v.as_str()) {
                msg.push_str(title);
            } else if let Some(session) = event.get("session").and_then(|v| v.as_str()) {
                msg.push_str(session);
            }
            let line = if msg.is_empty() { format!("{}  {}", ts, label) } else { format!("{}  {}  {}", ts, label, msg) };
            lines.push(line);
        }
    }
    lines
}

#[derive(Clone)]
struct TranscriptRow {
    text: String,
    fg: style::Color,
    bg: style::Color,
}

fn conversation_transcript_rows(room: &Value, width: usize) -> Vec<TranscriptRow> {
    fn role_label(role: &str) -> String {
        match role {
            "teacher" => "Teacher".to_string(),
            "student" => "Student".to_string(),
            "advocate" => "Advocate".to_string(),
            "opposition" => "Opposition".to_string(),
            "researcher" => "Researcher".to_string(),
            "reviewer" => "Reviewer".to_string(),
            "strategist" => "Strategist".to_string(),
            "critic" => "Critic".to_string(),
            "planner" => "Planner".to_string(),
            "architect" => "Architect".to_string(),
            "optimist" => "Optimist".to_string(),
            "skeptic" => "Skeptic".to_string(),
            "driver" => "Driver".to_string(),
            "navigator" => "Navigator".to_string(),
            other if other.starts_with("peer-") => {
                let suffix = other.trim_start_matches("peer-");
                format!("Peer {}", suffix)
            }
            other => other.replace('-', " "),
        }
    }

    fn palette(idx: usize) -> (style::Color, style::Color) {
        const PALETTES: &[(style::Color, style::Color)] = &[
            (
                style::Color::Rgb { r: 254, g: 242, b: 242 },
                style::Color::Rgb { r: 92, g: 31, b: 31 },
            ),
            (
                style::Color::Rgb { r: 251, g: 241, b: 230 },
                style::Color::Rgb { r: 58, g: 35, b: 24 },
            ),
            (
                style::Color::Rgb { r: 255, g: 237, b: 213 },
                style::Color::Rgb { r: 96, g: 52, b: 29 },
            ),
            (
                style::Color::Rgb { r: 250, g: 232, b: 255 },
                style::Color::Rgb { r: 70, g: 28, b: 71 },
            ),
            (
                style::Color::Rgb { r: 237, g: 233, b: 254 },
                style::Color::Rgb { r: 49, g: 46, b: 92 },
            ),
            (
                style::Color::Rgb { r: 254, g: 249, b: 195 },
                style::Color::Rgb { r: 81, g: 50, b: 16 },
            ),
        ];
        PALETTES[idx % PALETTES.len()]
    }

    let mut rows = Vec::new();
    let mut speaker_palette: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let mut next_palette = 0usize;
    if let Some(events) = room.get("events").and_then(|v| v.as_array()) {
        for event in events {
            let typ = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
            if typ == "participant_output" {
                let role = event.get("speaker_role").and_then(|v| v.as_str()).unwrap_or("agent");
                let session = event.get("session").and_then(|v| v.as_str()).unwrap_or("");
                let speaker_key = if !session.is_empty() { session.to_string() } else { role.to_string() };
                let palette_idx = *speaker_palette.entry(speaker_key).or_insert_with(|| {
                    let idx = next_palette;
                    next_palette += 1;
                    idx
                });
                let label = role_label(role);
                let text = event.get("text").and_then(|v| v.as_str())
                    .or_else(|| event.get("summary").and_then(|v| v.as_str()))
                    .unwrap_or("")
                    .trim();
                if text.is_empty() {
                    continue;
                }
                let (fg, bg) = palette(palette_idx);
                rows.push(TranscriptRow { text: format!("{}:", label), fg, bg });
                for wrapped in wrap_plain_text(text, width.saturating_sub(2).max(8)) {
                    rows.push(TranscriptRow { text: wrapped, fg, bg });
                }
                rows.push(TranscriptRow { text: String::new(), fg: style::Color::Reset, bg: style::Color::Reset });
            } else if matches!(typ, "turn_timeout" | "conversation_stalled" | "conversation_stopped") {
                let summary = event.get("summary").and_then(|v| v.as_str())
                    .or_else(|| event.get("topic").and_then(|v| v.as_str()))
                    .unwrap_or(typ);
                let label = match typ {
                    "turn_timeout" => "System: timeout",
                    "conversation_stalled" => "System: stalled",
                    "conversation_stopped" => "System: stopped",
                    _ => "System",
                };
                let fg = style::Color::Rgb { r: 254, g: 226, b: 226 };
                let bg = style::Color::Rgb { r: 127, g: 29, b: 29 };
                rows.push(TranscriptRow { text: label.to_string(), fg, bg });
                for wrapped in wrap_plain_text(summary, width.saturating_sub(2).max(8)) {
                    rows.push(TranscriptRow { text: wrapped, fg, bg });
                }
                rows.push(TranscriptRow { text: String::new(), fg: style::Color::Reset, bg: style::Color::Reset });
            }
        }
    }
    rows
}

fn inter_agent_stream_area(app: &App, w: u16, h: u16) -> Option<Rect> {
    let refresh_payload = app.chat.refresh_payload.clone();
    let rooms = payload_inter_agent_rooms(refresh_payload.as_ref());
    let room = rooms.get(app.inter_agent.selected)?;
    let sidebar_w = ((w as f32) * 0.22) as u16;
    let main_x = sidebar_w + 1;
    let main_w = w.saturating_sub(sidebar_w + 3);
    let kind = room.get("kind").and_then(|v| v.as_str()).unwrap_or("group");
    if kind == "libris" {
        let graph_area = Rect { x: main_x, y: 2, width: ((main_w as f32) * 0.62) as u16, height: h.saturating_sub(4) };
        let detail_x = graph_area.x + graph_area.width + 2;
        let detail_w = w.saturating_sub(detail_x + 2);
        let info_h = (h.saturating_sub(4) / 2).max(8);
        let node_area = Rect { x: detail_x, y: 2, width: detail_w, height: info_h.saturating_sub(1) };
        Some(Rect { x: detail_x, y: node_area.y + node_area.height + 1, width: detail_w, height: h.saturating_sub(node_area.height + 5) })
    } else {
        let sessions_h = (h.saturating_sub(6) / 2).max(8);
        let session_area = Rect { x: main_x, y: 2, width: main_w, height: sessions_h };
        Some(Rect { x: main_x, y: session_area.y + session_area.height + 2, width: main_w, height: h.saturating_sub(session_area.height + 6) })
    }
}

fn point_in_rect(area: Rect, x: u16, y: u16) -> bool {
    let left = area.x.saturating_sub(1);
    let top = area.y.saturating_sub(1);
    let right = area.x + area.width;
    let bottom = area.y + area.height;
    x >= left && x <= right && y >= top && y <= bottom
}

fn point_in_content(area: Rect, x: u16, y: u16) -> bool {
    x >= area.x && x < area.x.saturating_add(area.width) && y >= area.y && y < area.y.saturating_add(area.height)
}

fn ordered_points(a: TextPoint, b: TextPoint) -> (TextPoint, TextPoint) {
    if (a.row, a.col) <= (b.row, b.col) { (a, b) } else { (b, a) }
}

fn transcript_selection_bounds(app: &App) -> Option<(TextPoint, TextPoint)> {
    let a = app.inter_agent.transcript_anchor?;
    let b = app.inter_agent.transcript_focus?;
    Some(ordered_points(a, b))
}

fn transcript_row_window_len(total_rows: usize, area: Rect, event_scroll: usize) -> (usize, usize) {
    let start = total_rows.saturating_sub(area.height as usize + event_scroll);
    let end = total_rows.saturating_sub(event_scroll);
    (start.min(total_rows), end.min(total_rows))
}

fn transcript_point_at_mouse(rows: &[TranscriptRow], area: Rect, event_scroll: usize, x: u16, y: u16) -> Option<TextPoint> {
    if rows.is_empty() || area.width == 0 || area.height == 0 {
        return None;
    }
    let clamped_x = x.clamp(area.x, area.x.saturating_add(area.width).saturating_sub(1));
    let clamped_y = y.clamp(area.y, area.y.saturating_add(area.height).saturating_sub(1));
    let (start, end) = transcript_row_window_len(rows.len(), area, event_scroll);
    let visible = &rows[start..end];
    if visible.is_empty() {
        return None;
    }
    let rel_y = clamped_y.saturating_sub(area.y) as usize;
    let row_idx = start + rel_y.min(visible.len().saturating_sub(1));
    let text_len = rows.get(row_idx).map(|r| r.text.chars().count()).unwrap_or(0);
    let col = (clamped_x.saturating_sub(area.x) as usize).min(text_len);
    Some(TextPoint { row: row_idx, col })
}

fn transcript_max_scroll(rows: &[TranscriptRow], area: Rect) -> usize {
    rows.len().saturating_sub(area.height as usize)
}

fn transcript_selection_text(rows: &[TranscriptRow], bounds: (TextPoint, TextPoint)) -> String {
    let (start, end) = bounds;
    let mut out = String::new();
    for row_idx in start.row..=end.row {
        let line = rows.get(row_idx).map(|r| r.text.as_str()).unwrap_or("");
        let chars: Vec<char> = line.chars().collect();
        let line_len = chars.len();
        let from = if row_idx == start.row { start.col.min(line_len) } else { 0 };
        let to = if row_idx == end.row { end.col.min(line_len) } else { line_len };
        if from < to {
            out.extend(chars[from..to].iter().copied());
        }
        if row_idx != end.row {
            out.push('\n');
        }
    }
    out
}

fn row_index_selected(row: usize, col: usize, start: TextPoint, end: TextPoint) -> bool {
    if row < start.row || row > end.row {
        return false;
    }
    if start.row == end.row {
        return row == start.row && col >= start.col && col < end.col;
    }
    if row == start.row {
        return col >= start.col;
    }
    if row == end.row {
        return col < end.col;
    }
    true
}

fn draw_conversation_stream<W: Write>(stdout: &mut W, room: &Value, area: Rect, event_scroll: usize, selection: Option<(TextPoint, TextPoint)>) -> io::Result<()> {
    let rows = conversation_transcript_rows(room, area.width as usize);
    if rows.is_empty() {
        let lines = inter_agent_event_lines(room, event_scroll, area.height.saturating_sub(1) as usize);
        for (i, line) in lines.into_iter().take(area.height as usize).enumerate() {
            stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
            let visible: String = line.chars().take(area.width as usize).collect();
            write!(stdout, "{}{}", visible, " ".repeat((area.width as usize).saturating_sub(visible.chars().count())))?;
        }
        return Ok(());
    }
    let (start, end) = transcript_row_window_len(rows.len(), area, event_scroll);
    for (i, row) in rows[start..end].iter().enumerate() {
        let screen_y = area.y + i as u16;
        stdout.queue(cursor::MoveTo(area.x, screen_y))?;
        let chars: Vec<char> = row.text.chars().take(area.width as usize).collect();
        let line_len = row.text.chars().count();
        for col in 0..area.width as usize {
            let ch = chars.get(col).copied().unwrap_or(' ');
            let mut fg = row.fg;
            let mut bg = row.bg;
            if let Some((sel_start, sel_end)) = selection {
                if line_len > 0 && row_index_selected(start + i, col, sel_start, sel_end) {
                    fg = style::Color::Black;
                    bg = style::Color::Rgb { r: 226, g: 232, b: 240 };
                }
            }
            stdout.queue(style::SetForegroundColor(fg))?;
            stdout.queue(style::SetBackgroundColor(bg))?;
            write!(stdout, "{}", ch)?;
        }
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        stdout.queue(style::SetBackgroundColor(style::Color::Reset))?;
    }
    Ok(())
}

#[derive(Clone)]
struct LibrisGraphNode {
    agent_id: String,
    name: String,
    role: String,
    status: String,
    phase: String,
    topic_slug: String,
    phase_summary: String,
    live_line: String,
}

#[derive(Clone, Copy)]
struct GraphPoint {
    x: u16,
    y: u16,
}

#[derive(Clone, Copy)]
struct GraphAnchors {
    center: GraphPoint,
    left: GraphPoint,
    right: GraphPoint,
    top: GraphPoint,
    bottom: GraphPoint,
}

fn libris_role_color(role: &str, active: bool) -> style::Color {
    let base = match role {
        "coordinator" => style::Color::Rgb { r: 196, g: 181, b: 253 },
        "researcher" => style::Color::Rgb { r: 103, g: 232, b: 249 },
        "judge" => style::Color::Rgb { r: 251, g: 191, b: 36 },
        "shade" => style::Color::Rgb { r: 148, g: 163, b: 184 },
        _ => style::Color::Rgb { r: 148, g: 163, b: 184 },
    };
    if active { base } else { style::Color::DarkGrey }
}

fn draw_box_text<W: Write>(stdout: &mut W, area: Rect, lines: &[String], color: style::Color) -> io::Result<()> {
    for (i, line) in lines.iter().take(area.height as usize).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        stdout.queue(style::SetForegroundColor(color))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}{}", visible, " ".repeat((area.width as usize).saturating_sub(visible.chars().count())))?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    Ok(())
}

fn draw_vline<W: Write>(stdout: &mut W, x: u16, y1: u16, y2: u16, color: style::Color) -> io::Result<()> {
    let start = y1.min(y2);
    let end = y1.max(y2);
    for y in start..=end {
        stdout.queue(cursor::MoveTo(x, y))?;
        stdout.queue(style::SetForegroundColor(color))?;
        write!(stdout, "│")?;
    }
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn draw_hline<W: Write>(stdout: &mut W, x1: u16, x2: u16, y: u16, color: style::Color) -> io::Result<()> {
    let start = x1.min(x2);
    let end = x1.max(x2);
    for x in start..=end {
        stdout.queue(cursor::MoveTo(x, y))?;
        stdout.queue(style::SetForegroundColor(color))?;
        write!(stdout, "─")?;
    }
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn graph_anchors(rect: Rect) -> GraphAnchors {
    let cx = rect.x + rect.width / 2;
    let cy = rect.y + rect.height / 2;
    GraphAnchors {
        center: GraphPoint { x: cx, y: cy },
        left: GraphPoint { x: rect.x.saturating_sub(1), y: cy },
        right: GraphPoint { x: rect.x + rect.width, y: cy },
        top: GraphPoint { x: cx, y: rect.y.saturating_sub(1) },
        bottom: GraphPoint { x: cx, y: rect.y + rect.height },
    }
}

fn libris_edge_color(active_now: bool, activity_strength: f64) -> style::Color {
    if active_now || activity_strength >= 0.95 {
        style::Color::Rgb { r: 96, g: 165, b: 250 }
    } else if activity_strength >= 0.70 {
        style::Color::Rgb { r: 59, g: 130, b: 246 }
    } else if activity_strength >= 0.35 {
        style::Color::Rgb { r: 100, g: 116, b: 139 }
    } else {
        style::Color::DarkGrey
    }
}

fn mid_u16(a: u16, b: u16) -> u16 {
    a.min(b) + (a.max(b) - a.min(b)) / 2
}

fn draw_libris_graph<W: Write>(stdout: &mut W, room: &Value, area: Rect, selected_node: usize) -> io::Result<Vec<LibrisGraphNode>> {
    let nodes = room.get("nodes").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let topics = room.get("topics").and_then(|v| v.as_array()).cloned().unwrap_or_default();
    let edges = room.get("edges").and_then(|v| v.as_array()).cloned().unwrap_or_default();

    let mut graph_nodes: Vec<LibrisGraphNode> = nodes.iter().map(|n| LibrisGraphNode {
        agent_id: n.get("agent_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        name: n.get("name").and_then(|v| v.as_str()).unwrap_or("agent").to_string(),
        role: n.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        status: n.get("status").and_then(|v| v.as_str()).unwrap_or("idle").to_string(),
        phase: n.get("phase").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        topic_slug: n.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        phase_summary: n.get("phase_summary").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        live_line: n.get("live_line").and_then(|v| v.as_str()).unwrap_or("").to_string(),
    }).collect();

    let coordinator = graph_nodes.iter().find(|n| n.role == "coordinator").cloned();
    let graph_inner = Rect {
        x: area.x,
        y: area.y,
        width: area.width,
        height: area.height,
    };

    let mut node_anchors: std::collections::HashMap<String, GraphAnchors> = std::collections::HashMap::new();
    let mut coord_center: Option<GraphPoint> = None;
    if let Some(coord) = coordinator {
        let box_w = graph_inner.width.min(28).max(18);
        let box_h = 4u16;
        let box_x = graph_inner.x + graph_inner.width.saturating_sub(box_w) / 2;
        let box_y = graph_inner.y;
        let coord_idx = graph_nodes.iter().position(|n| n.agent_id == coord.agent_id).unwrap_or(usize::MAX);
        let node_rect = Rect { x: box_x, y: box_y, width: box_w.saturating_sub(2), height: box_h };
        render::render_border_colored(stdout, node_rect, &coord.name, libris_role_color("coordinator", selected_node == coord_idx))?;
        draw_box_text(stdout, node_rect, &[
            format!("{} • {}", coord.role, coord.phase),
            coord.topic_slug.clone(),
            coord.live_line.clone(),
        ], style::Color::Rgb { r: 226, g: 232, b: 240 })?;
        node_anchors.insert(coord.agent_id.clone(), graph_anchors(node_rect));
        coord_center = Some(GraphPoint { x: node_rect.x + node_rect.width / 2, y: node_rect.y + node_rect.height + 1 });
    }

    let cluster_y = graph_inner.y + 6;
    let cluster_h = graph_inner.height.saturating_sub(8).max(6);
    let topic_count = topics.len().max(1) as u16;
    let cluster_gap = if topic_count >= 4 { 1u16 } else { 2u16 };
    let cluster_w = if topic_count > 0 { graph_inner.width.saturating_sub(cluster_gap * topic_count.saturating_sub(1)) / topic_count } else { graph_inner.width };
    let compact_nodes = topic_count >= 3 || graph_inner.width < 110;
    let mut topic_centers: Vec<GraphPoint> = Vec::new();

    for (ti, topic) in topics.iter().enumerate() {
        let tx = graph_inner.x + ti as u16 * (cluster_w + cluster_gap);
        let cluster_rect = Rect { x: tx, y: cluster_y, width: cluster_w.saturating_sub(2), height: cluster_h.saturating_sub(1) };
        topic_centers.push(GraphPoint { x: cluster_rect.x + cluster_rect.width / 2, y: cluster_rect.y.saturating_sub(1) });
        let topic_title = topic.get("title").and_then(|v| v.as_str()).unwrap_or("topic");
        render::render_border_colored(stdout, cluster_rect, topic_title, style::Color::Rgb { r: 59, g: 50, b: 82 })?;

        let slug = topic.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("");
        let researcher = graph_nodes.iter().find(|n| n.role == "researcher" && n.topic_slug == slug).cloned();
        let judge = graph_nodes.iter().find(|n| n.role == "judge" && n.topic_slug == slug).cloned();
        let shades: Vec<LibrisGraphNode> = graph_nodes.iter().filter(|n| n.role == "shade" && n.topic_slug == slug).cloned().collect();

        let top_row_y = cluster_rect.y + 1;
        let node_w = (cluster_rect.width.saturating_sub(3) / 2).max(if compact_nodes { 10 } else { 12 });
        let node_h = if compact_nodes { 3u16 } else { 4u16 };
        if let Some(r) = researcher.clone() {
            let selected = graph_nodes.iter().position(|n| n.agent_id == r.agent_id).unwrap_or(usize::MAX) == selected_node;
            let rect = Rect { x: cluster_rect.x, y: top_row_y, width: node_w, height: node_h };
            render::render_border_colored(stdout, rect, &r.name, libris_role_color("researcher", selected))?;
            let lines = if compact_nodes {
                vec![format!("r • {}", r.phase), r.live_line.clone()]
            } else {
                vec![format!("researcher • {}", r.phase), slug.to_string(), r.live_line.clone()]
            };
            draw_box_text(stdout, rect, &lines, style::Color::Rgb { r: 226, g: 232, b: 240 })?;
            node_anchors.insert(r.agent_id.clone(), graph_anchors(rect));
        }
        if let Some(j) = judge.clone() {
            let selected = graph_nodes.iter().position(|n| n.agent_id == j.agent_id).unwrap_or(usize::MAX) == selected_node;
            let rect = Rect { x: cluster_rect.x + node_w + 3, y: top_row_y, width: node_w, height: node_h };
            render::render_border_colored(stdout, rect, &j.name, libris_role_color("judge", selected))?;
            let lines = if compact_nodes {
                vec![format!("j • {}", j.phase), j.live_line.clone()]
            } else {
                vec![format!("judge • {}", j.phase), slug.to_string(), j.live_line.clone()]
            };
            draw_box_text(stdout, rect, &lines, style::Color::Rgb { r: 226, g: 232, b: 240 })?;
            node_anchors.insert(j.agent_id.clone(), graph_anchors(rect));
        }

        let rj_active = edges.iter().any(|e| {
            e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug
                && e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)
                && ((e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "judge")
                    || (e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "judge" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher"))
        });
        stdout.queue(cursor::MoveTo(cluster_rect.x + 2, top_row_y + node_h + 1))?;
        stdout.queue(style::SetForegroundColor(if rj_active { style::Color::Rgb { r: 96, g: 165, b: 250 } } else { style::Color::DarkGrey }))?;
        let comm = "researcher <=> judge";
        let vis: String = comm.chars().take(cluster_rect.width.saturating_sub(4) as usize).collect();
        write!(stdout, "{}", vis)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;

        let mut sy = top_row_y + node_h + 3;
        for shade in shades.iter().take(3) {
            let selected = graph_nodes.iter().position(|n| n.agent_id == shade.agent_id).unwrap_or(usize::MAX) == selected_node;
            let rect = Rect { x: cluster_rect.x + 2, y: sy, width: cluster_rect.width.saturating_sub(4), height: if compact_nodes { 2 } else { 3 } };
            render::render_border_colored(stdout, rect, &shade.name, libris_role_color("shade", selected))?;
            let lines = if compact_nodes {
                vec![shade.live_line.clone()]
            } else {
                vec![format!("shade • {}", shade.phase), shade.live_line.clone()]
            };
            draw_box_text(stdout, rect, &lines, style::Color::Rgb { r: 203, g: 213, b: 225 })?;
            node_anchors.insert(shade.agent_id.clone(), graph_anchors(rect));
            sy = sy.saturating_add(if compact_nodes { 3 } else { 4 });
        }

        let shade_active = edges.iter().any(|e| {
            e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug
                && e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)
                && (e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "shade"
                    || e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "shade")
        });

        let edge_summary = edges.iter().filter(|e| e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug);
        let active = edge_summary.clone().filter(|e| e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)).count();
        let total = edge_summary.count();
        let status_line = format!("{} edges  {} active{}", total, active, if shade_active { "  shade:return" } else { "" });
        stdout.queue(cursor::MoveTo(cluster_rect.x, cluster_rect.y + cluster_rect.height.saturating_sub(1)))?;
        stdout.queue(style::SetForegroundColor(if active > 0 { style::Color::Rgb { r: 34, g: 197, b: 94 } } else { style::Color::DarkGrey }))?;
        let visible: String = status_line.chars().take(cluster_rect.width as usize).collect();
        write!(stdout, "{}", visible)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    if let Some(coord) = coord_center {
        if let (Some(first), Some(last)) = (topic_centers.first().copied(), topic_centers.last().copied()) {
            let trunk_y = cluster_y.saturating_sub(3);
            let edge_color = style::Color::DarkGrey;
            if trunk_y > coord.y {
                draw_vline(stdout, coord.x, coord.y, trunk_y, edge_color)?;
            }
            draw_hline(stdout, first.x, last.x, trunk_y, edge_color)?;
            for tp in &topic_centers {
                draw_vline(stdout, tp.x, trunk_y, tp.y, edge_color)?;
            }
        }
    }

    for edge in &edges {
        let src = edge.get("from_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        let dst = edge.get("to_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        let from_role = edge.get("from_role").and_then(|v| v.as_str()).unwrap_or("");
        let to_role = edge.get("to_role").and_then(|v| v.as_str()).unwrap_or("");
        let active_now = edge.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false);
        let activity_strength = edge.get("activity_strength").and_then(|v| v.as_f64()).unwrap_or(0.15);
        let color = libris_edge_color(active_now, activity_strength);
        let Some(src_anchor) = node_anchors.get(src).copied() else { continue; };
        let Some(dst_anchor) = node_anchors.get(dst).copied() else { continue; };

        let (start, end, mid_y) = if from_role == "researcher" && to_role == "judge" {
            (src_anchor.right, dst_anchor.left, src_anchor.right.y)
        } else if from_role == "judge" && to_role == "researcher" {
            (src_anchor.left, dst_anchor.right, src_anchor.left.y)
        } else if from_role == "shade" && to_role == "researcher" {
            let m = dst_anchor.bottom.y + ((src_anchor.top.y.saturating_sub(dst_anchor.bottom.y)) / 2);
            (src_anchor.top, dst_anchor.bottom, m)
        } else if from_role == "researcher" && to_role == "shade" {
            let m = src_anchor.bottom.y + ((dst_anchor.top.y.saturating_sub(src_anchor.bottom.y)) / 2);
            (src_anchor.bottom, dst_anchor.top, m)
        } else if from_role == "coordinator" {
            (src_anchor.bottom, dst_anchor.top, cluster_y.saturating_sub(3))
        } else if to_role == "coordinator" {
            (src_anchor.top, dst_anchor.bottom, cluster_y.saturating_sub(3))
        } else {
            let m = mid_u16(src_anchor.center.y, dst_anchor.center.y);
            (src_anchor.center, dst_anchor.center, m)
        };

        if start.y == end.y {
            draw_hline(stdout, start.x, end.x, start.y, color)?;
        } else {
            draw_vline(stdout, start.x, start.y, mid_y, color)?;
            draw_hline(stdout, start.x, end.x, mid_y, color)?;
            draw_vline(stdout, end.x, mid_y, end.y, color)?;
        }
    }

    if topics.is_empty() {
        stdout.queue(cursor::MoveTo(graph_inner.x, graph_inner.y + 2))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "Waiting for Libris topic clusters…")?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    Ok(graph_nodes)
}

fn room_session_meta(room: &Value, payload: Option<&Value>) -> Vec<SessionAgentMeta> {
    let mut wanted: std::collections::HashSet<String> = std::collections::HashSet::new();
    if let Some(arr) = room.get("participant_sessions").and_then(|v| v.as_array()) {
        for v in arr {
            if let Some(s) = v.as_str() {
                wanted.insert(s.to_string());
            }
        }
    }
    if let Some(arr) = room.get("participants").and_then(|v| v.as_array()) {
        for p in arr {
            if let Some(s) = p.get("session").and_then(|v| v.as_str()) {
                wanted.insert(s.to_string());
            }
        }
    }
    session_agent_meta(payload)
        .into_iter()
        .filter(|m| wanted.contains(&m.tmux) || wanted.contains(&m.id) || wanted.contains(&format!("boat-{}", m.tmux)))
        .collect()
}

#[derive(Clone)]
struct RoomPaneVisual {
    title: String,
    status: String,
    border_color: style::Color,
}

fn role_title(role: &str, fallback_name: &str, idx: usize) -> String {
    match role {
        "teacher" => "Hermes Teacher".to_string(),
        "student" => "Hermes Student".to_string(),
        "developer" => format!("Hermes Developer {}", idx + 1),
        "participant" => format!("Hermes Participant {}", idx + 1),
        _ if !fallback_name.trim().is_empty() => fallback_name.trim().to_string(),
        _ => format!("Hermes {}", idx + 1),
    }
}

fn room_pane_visuals(room: &Value) -> std::collections::HashMap<String, RoomPaneVisual> {
    let mut visuals = std::collections::HashMap::new();
    let active_speaker = room.get("active_speaker").and_then(|v| v.as_str()).unwrap_or("");
    let mut timed_out_role = String::new();
    if let Some(events) = room.get("events").and_then(|v| v.as_array()) {
        for event in events.iter().rev() {
            let typ = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
            if typ == "turn_timeout" {
                timed_out_role = event.get("speaker_role").and_then(|v| v.as_str()).unwrap_or("").to_string();
                break;
            }
            if typ == "participant_output" || typ == "conversation_turn_started" {
                break;
            }
        }
    }

    if let Some(parts) = room.get("participants").and_then(|v| v.as_array()) {
        for (idx, part) in parts.iter().enumerate() {
            let session = part.get("session").and_then(|v| v.as_str()).unwrap_or("");
            if session.is_empty() {
                continue;
            }
            let role = part.get("role").and_then(|v| v.as_str()).unwrap_or("participant");
            let fallback_name = part.get("name").and_then(|v| v.as_str()).unwrap_or("Hermes");
            let (status, border_color) = if !timed_out_role.is_empty() && timed_out_role == role {
                ("TIMED OUT".to_string(), style::Color::Rgb { r: 248, g: 113, b: 113 })
            } else if !active_speaker.is_empty() && active_speaker == role {
                ("ACTIVE".to_string(), style::Color::Rgb { r: 34, g: 197, b: 94 })
            } else {
                ("WAITING".to_string(), style::Color::DarkGrey)
            };
            visuals.insert(session.to_string(), RoomPaneVisual {
                title: format!("{} [{}]", role_title(role, fallback_name, idx), status),
                status,
                border_color,
            });
        }
    }
    visuals
}

fn sync_inter_agent_room_panes(app: &mut App, room: &Value, outer_w: u16, outer_h: u16) -> io::Result<bool> {
    let room_id = room.get("id").and_then(|v| v.as_str()).unwrap_or("");
    if room_id.is_empty() {
        return Ok(false);
    }
    if app.inter_agent.room_panes_room_id != room_id {
        app.inter_agent.room_panes.clear();
        app.inter_agent.room_panes_room_id = room_id.to_string();
    }
    let visuals = room_pane_visuals(room);
    let metas = room_session_meta(room, app.chat.refresh_payload.as_ref());
    if metas.is_empty() {
        return Ok(false);
    }
    let target_total = metas.len().max(1);
    let (_, _, rects) = compute_grid(target_total, outer_w.max(20), outer_h.max(8));
    let mut changed = false;
    for meta in metas {
        let visual = visuals.get(&meta.tmux).or_else(|| visuals.get(&format!("boat-{}", meta.tmux)));
        let title = visual.map(|v| v.title.clone()).unwrap_or_else(|| compose_session_title(&meta));
        let existing_idx = app.inter_agent.room_panes.iter().position(|c| match &c.backend_type {
            BackendType::BoatPane { session_id } => !meta.tmux.is_empty() && session_id == &meta.tmux,
            BackendType::TmuxPane { session_name } => !meta.tmux.is_empty() && session_name == &meta.tmux,
            BackendType::CharonPane { socket_path } => meta.transport == "charon" && !meta.socket.is_empty() && socket_path == &meta.socket,
            BackendType::LocalPty => false,
        });
        if let Some(idx) = existing_idx {
            if let Some(cell) = app.inter_agent.room_panes.get_mut(idx) {
                cell.title = title;
            }
            continue;
        }
        let idx = app.inter_agent.room_panes.len();
        let r = rects.get(idx).copied().unwrap_or(Rect { x: 0, y: 0, width: 80, height: 24 });
        let cell = if meta.transport == "charon" && !meta.socket.is_empty() {
            SessionCell::attach_charon(idx as u64, &title, &meta.socket, r.width.max(1), r.height.max(1))
        } else if meta.transport == "pty" && !meta.socket.is_empty() {
            SessionCell::attach_boat_socket(idx as u64, &title, &meta.tmux, &meta.socket, r.width.max(1), r.height.max(1))
        } else if meta.source == "boat" {
            SessionCell::attach_boat(idx as u64, &title, &meta.tmux, r.width.max(1), r.height.max(1))
        } else if !meta.tmux.is_empty() {
            SessionCell::attach_tmux(idx as u64, &title, &meta.tmux, r.width.max(1), r.height.max(1))
        } else {
            continue;
        };
        if let Ok(cell) = cell {
            app.inter_agent.room_panes.push(cell);
            changed = true;
        }
    }
    Ok(changed)
}

fn draw_room_panes<W: Write>(stdout: &mut W, app: &mut App, room: &Value, area: Rect, force_all: bool) -> io::Result<()> {
    let count = app.inter_agent.room_panes.len().max(1);
    let (_, _, rects) = compute_grid(count, area.width, area.height);
    let visuals = room_pane_visuals(room);
    for (i, cell) in app.inter_agent.room_panes.iter_mut().enumerate() {
        let Some(mut r) = rects.get(i).copied() else { continue; };
        r.x = r.x.saturating_add(area.x);
        r.y = r.y.saturating_add(area.y);
        let _ = cell.resize(r.width.max(1), r.height.max(1));
        cell.reset_viewport_scroll();
        let visual = match &cell.backend_type {
            BackendType::BoatPane { session_id } => visuals.get(session_id),
            BackendType::TmuxPane { session_name } => visuals.get(session_name),
            BackendType::CharonPane { .. } | BackendType::LocalPty => None,
        };
        let title = visual.map(|v| v.title.as_str()).unwrap_or(cell.title.as_str());
        let border_color = visual.map(|v| v.border_color).unwrap_or(style::Color::DarkGrey);
        render::render_border_colored(stdout, r, title, border_color)?;
        if cell.terminal.dirty || force_all {
            render::render_terminal(stdout, &cell.terminal, r, cell.viewport_scroll)?;
            cell.terminal.dirty = false;
        }
        if let Some(visual) = visual {
            let status = visual.status.as_str();
            let y = r.y + r.height.saturating_sub(1);
            stdout.queue(cursor::MoveTo(r.x, y))?;
            stdout.queue(style::SetForegroundColor(border_color))?;
            let visible: String = status.chars().take(r.width as usize).collect();
            write!(stdout, "{}{}", visible, " ".repeat((r.width as usize).saturating_sub(visible.chars().count())))?;
            stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        }
    }
    Ok(())
}

fn draw_delete_room_modal<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    if !app.inter_agent.delete_confirm_open {
        return Ok(());
    }
    let width = w.saturating_sub(20).min(72).max(36);
    let height = 6u16;
    let x = (w.saturating_sub(width)) / 2 + 1;
    let y = (h.saturating_sub(height)) / 2;
    let area = Rect { x, y, width: width.saturating_sub(2), height };
    render::render_border_colored(stdout, area, "delete room?", style::Color::Rgb { r: 248, g: 113, b: 113 })?;
    let lines = vec![
        format!("Delete room record: {}", app.inter_agent.delete_target_title),
        "This removes the conversation record from the backend room list.".to_string(),
        "Enter / y: confirm   Esc / n: cancel".to_string(),
    ];
    for (i, line) in lines.into_iter().enumerate().take(area.height as usize) {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}{}", visible, " ".repeat((area.width as usize).saturating_sub(visible.chars().count())))?;
    }
    Ok(())
}

fn draw_inter_agent<W: Write>(stdout: &mut W, app: &mut App, w: u16, h: u16) -> io::Result<()> {
    let sidebar_w = ((w as f32) * 0.22) as u16;
    let list_area = Rect { x: 1, y: 2, width: sidebar_w.saturating_sub(2), height: h.saturating_sub(4) };
    render::render_border(stdout, list_area, "groups", true)?;

    let refresh_payload = app.chat.refresh_payload.clone();
    let rooms = payload_inter_agent_rooms(refresh_payload.as_ref());
    if app.inter_agent.selected >= rooms.len() && !rooms.is_empty() {
        app.inter_agent.selected = rooms.len() - 1;
    }
    keep_index_visible(app.inter_agent.selected, &mut app.inter_agent.scroll, list_area.height as usize);
    let list_end = (app.inter_agent.scroll + list_area.height as usize).min(rooms.len());
    for (row, room) in rooms[app.inter_agent.scroll..list_end].iter().enumerate() {
        let i = app.inter_agent.scroll + row;
        let kind = room.get("kind").and_then(|v| v.as_str()).unwrap_or("group");
        let title = room.get("title").and_then(|v| v.as_str()).unwrap_or("untitled");
        let project = room.get("project").and_then(|v| v.as_str()).unwrap_or("");
        let project_name = project.split('/').filter(|s| !s.is_empty()).last().unwrap_or(project);
        let line = if project_name.is_empty() {
            format!("{} {}: {}", if i == app.inter_agent.selected { "▸" } else { " " }, kind, title)
        } else {
            format!("{} {}: {} [{}]", if i == app.inter_agent.selected { "▸" } else { " " }, kind, title, project_name)
        };
        stdout.queue(cursor::MoveTo(list_area.x, list_area.y + row as u16))?;
        stdout.queue(style::SetForegroundColor(if i == app.inter_agent.selected { style::Color::Rgb { r: 212, g: 196, b: 168 } } else { style::Color::DarkGrey }))?;
        let visible: String = line.chars().take(list_area.width as usize).collect();
        write!(stdout, "{}", visible)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    let main_x = sidebar_w + 1;
    let main_w = w.saturating_sub(sidebar_w + 3);
    if let Some(room) = rooms.get(app.inter_agent.selected).cloned() {
        let kind = room.get("kind").and_then(|v| v.as_str()).unwrap_or("group").to_string();
        if kind != "libris" {
            let _ = sync_inter_agent_room_panes(app, &room, main_w.saturating_sub(2), h.saturating_sub(8));
        }
        if kind == "libris" {
            let graph_area = Rect { x: main_x, y: 2, width: ((main_w as f32) * 0.62) as u16, height: h.saturating_sub(4) };
            let detail_x = graph_area.x + graph_area.width + 2;
            let detail_w = w.saturating_sub(detail_x + 2);
            let info_h = (h.saturating_sub(4) / 2).max(8);
            let node_area = Rect { x: detail_x, y: 2, width: detail_w, height: info_h.saturating_sub(1) };
            let event_area = Rect { x: detail_x, y: node_area.y + node_area.height + 1, width: detail_w, height: h.saturating_sub(node_area.height + 5) };
            render::render_border(stdout, graph_area, if app.inter_agent.graph_focus { "graph *" } else { "graph" }, app.inter_agent.graph_focus)?;
            render::render_border(stdout, node_area, if app.inter_agent.topic_detail { "topic" } else { "selection" }, !app.inter_agent.graph_focus)?;
            render::render_border(stdout, event_area, "events", false)?;

            let graph_nodes = draw_libris_graph(stdout, room, graph_area, app.inter_agent.selected_node)?;
            if app.inter_agent.selected_node >= graph_nodes.len() && !graph_nodes.is_empty() {
                app.inter_agent.selected_node = graph_nodes.len() - 1;
            }
            if let Some(node) = graph_nodes.get(app.inter_agent.selected_node) {
                let budget = room.get("budget_status").and_then(|v| v.as_object());
                let topic = if node.topic_slug.is_empty() {
                    None
                } else {
                    room.get("topics").and_then(|v| v.as_array()).and_then(|arr| arr.iter().find(|t| t.get("topic_slug").and_then(|v| v.as_str()) == Some(node.topic_slug.as_str())))
                };
                let promising_sources = room.get("promising_sources").and_then(|v| v.as_array()).cloned().unwrap_or_default();
                let final_selection = room.get("final_selection_markdown").and_then(|v| v.as_str()).unwrap_or("");
                let mut detail_lines = Vec::new();

                if app.inter_agent.topic_detail && topic.is_some() {
                    let topic = topic.unwrap();
                    detail_lines.push(format!("topic: {}", topic.get("title").and_then(|v| v.as_str()).unwrap_or(node.topic_slug.as_str())));
                    detail_lines.push(format!("slug: {}", node.topic_slug));
                    if let Some(budget) = budget {
                        let continue_running = budget.get("continue_running").and_then(|v| v.as_bool()).unwrap_or(true);
                        detail_lines.push(format!("budget: {}", if continue_running { "ok" } else { "constrained" }));
                    }
                    let topic_status = topic.get("status").and_then(|v| v.as_str()).unwrap_or("");
                    if !topic_status.is_empty() {
                        detail_lines.push(format!("status: {}", topic_status));
                    }
                    let checkpoint_count = topic.get("checkpoint_count").and_then(|v| v.as_u64()).unwrap_or(0);
                    let best = topic.get("best_checkpoint_id").and_then(|v| v.as_str()).unwrap_or("");
                    detail_lines.push(format!("checkpoints: {}", checkpoint_count));
                    if !best.is_empty() {
                        detail_lines.push(format!("best checkpoint: {}", best));
                    }
                    let topic_source_count = promising_sources.iter().filter(|s| s.get("topic_slug").and_then(|v| v.as_str()) == Some(node.topic_slug.as_str())).count();
                    detail_lines.push(format!("promising sources: {}", topic_source_count));
                    let participants = graph_nodes.iter().filter(|n| n.topic_slug == node.topic_slug).map(|n| format!("{} ({})", n.name, n.role)).collect::<Vec<_>>();
                    if !participants.is_empty() {
                        detail_lines.push(String::new());
                        detail_lines.push("participants:".to_string());
                        for p in participants {
                            detail_lines.push(format!("- {}", p));
                        }
                    }
                    if !final_selection.trim().is_empty() {
                        detail_lines.push(String::new());
                        detail_lines.push("selection snippet:".to_string());
                        for wrapped in wrap_plain_text(final_selection.lines().next().unwrap_or(""), node_area.width as usize) {
                            detail_lines.push(wrapped);
                        }
                    }
                } else {
                    detail_lines.push(format!("{}", node.name));
                    detail_lines.push(format!("role: {}", node.role));
                    detail_lines.push(format!("status: {}", node.status));
                    detail_lines.push(format!("phase: {}", if node.phase.is_empty() { "-" } else { &node.phase }));
                    if !node.live_line.is_empty() {
                        detail_lines.push(format!("live: {}", node.live_line));
                    }
                    if let Some(budget) = budget {
                        let continue_running = budget.get("continue_running").and_then(|v| v.as_bool()).unwrap_or(true);
                        detail_lines.push(format!("budget: {}", if continue_running { "ok" } else { "constrained" }));
                    }
                    if !node.topic_slug.is_empty() {
                        detail_lines.push(format!("topic: {}", node.topic_slug));
                        if let Some(topic) = topic {
                            let checkpoint_count = topic.get("checkpoint_count").and_then(|v| v.as_u64()).unwrap_or(0);
                            let best = topic.get("best_checkpoint_id").and_then(|v| v.as_str()).unwrap_or("");
                            let topic_status = topic.get("status").and_then(|v| v.as_str()).unwrap_or("");
                            if !topic_status.is_empty() {
                                detail_lines.push(format!("topic status: {}", topic_status));
                            }
                            detail_lines.push(format!("checkpoints: {}", checkpoint_count));
                            if !best.is_empty() {
                                detail_lines.push(format!("best checkpoint: {}", best));
                            }
                        }
                    }
                    if !promising_sources.is_empty() {
                        detail_lines.push(format!("promising sources: {}", promising_sources.len()));
                    }
                    if !final_selection.trim().is_empty() {
                        detail_lines.push("final selection: yes".to_string());
                    }
                    if !node.phase_summary.is_empty() {
                        detail_lines.push(String::new());
                        detail_lines.push("summary:".to_string());
                        for wrapped in wrap_plain_text(&node.phase_summary, node_area.width as usize) {
                            detail_lines.push(wrapped);
                        }
                    }
                }

                detail_lines.push(String::new());
                detail_lines.push(if app.inter_agent.graph_focus { "Tab: room list focus".to_string() } else { "Tab: graph focus".to_string() });
                detail_lines.push(format!("Enter: {} detail", if app.inter_agent.topic_detail { "node" } else { "topic" }));
                detail_lines.push("↑/↓: select node   PgUp/PgDn: event scroll".to_string());
                for (i, line) in detail_lines.into_iter().take(node_area.height as usize).enumerate() {
                    stdout.queue(cursor::MoveTo(node_area.x, node_area.y + i as u16))?;
                    let visible: String = line.chars().take(node_area.width as usize).collect();
                    write!(stdout, "{}{}", visible, " ".repeat((node_area.width as usize).saturating_sub(visible.chars().count())))?;
                }
            }

            let lines = inter_agent_event_lines(room, app.inter_agent.event_scroll, event_area.height.saturating_sub(1) as usize);
            for (i, line) in lines.into_iter().take(event_area.height as usize).enumerate() {
                stdout.queue(cursor::MoveTo(event_area.x, event_area.y + i as u16))?;
                let visible: String = line.chars().take(event_area.width as usize).collect();
                write!(stdout, "{}{}", visible, " ".repeat((event_area.width as usize).saturating_sub(visible.chars().count())))?;
            }
        } else {
            let sessions_h = (h.saturating_sub(6) / 2).max(8);
            let session_area = Rect { x: main_x, y: 2, width: main_w, height: sessions_h };
            let detail_area = Rect { x: main_x, y: session_area.y + session_area.height + 2, width: main_w, height: h.saturating_sub(session_area.height + 6) };
            render::render_border(stdout, session_area, "sessions", true)?;
            render::render_border(stdout, detail_area, "stream", false)?;
            if app.inter_agent.room_panes.is_empty() {
                stdout.queue(cursor::MoveTo(session_area.x, session_area.y))?;
                stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
                write!(stdout, "Waiting for participant sessions…")?;
                stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
                if let Some(parts) = room.get("participants").and_then(|v| v.as_array()) {
                    for (i, part) in parts.iter().take(session_area.height.saturating_sub(2) as usize).enumerate() {
                        let role = part.get("role").and_then(|v| v.as_str()).unwrap_or("participant");
                        let name = part.get("name").and_then(|v| v.as_str()).unwrap_or("Hermes");
                        stdout.queue(cursor::MoveTo(session_area.x, session_area.y + 1 + i as u16))?;
                        write!(stdout, "- {} ({})", name, role)?;
                    }
                }
            } else {
                draw_room_panes(stdout, app, room, session_area, true)?;
            }
            draw_conversation_stream(stdout, room, detail_area, app.inter_agent.event_scroll, transcript_selection_bounds(app))?;
        }
    }
    draw_delete_room_modal(stdout, app, w, h)?;
    Ok(())
}

#[derive(Clone)]
struct SessionAgentMeta {
    id: String,
    agent_id: String,
    name: String,
    project: String,
    specialization: String,
    last_summary: String,
    tmux: String,
    status: String,
    source: String,
    process_target: String,
    live_session_id: String,
    session_label: String,
    transport: String,
    socket: String,
}

#[derive(Clone)]
enum SessionListRow {
    AgentHeader {
        name: String,
        project: String,
        detail: String,
        count: usize,
        session_ids: Vec<String>,
        collapsed: bool,
    },
    Session { id: String, label: String, status: String },
}

fn compose_session_title(meta: &SessionAgentMeta) -> String {
    let mut parts = vec![meta.name.clone()];
    let project_name = meta.project.split('/').filter(|s| !s.is_empty()).last().unwrap_or(&meta.project).trim();
    if !project_name.is_empty() {
        parts.push(project_name.to_string());
    }
    let detail = if !meta.specialization.trim().is_empty() {
        meta.specialization.trim().to_string()
    } else if !meta.last_summary.trim().is_empty() {
        meta.last_summary.trim().chars().take(64).collect::<String>()
    } else if !meta.session_label.trim().is_empty()
        && meta.session_label.trim() != meta.name.trim()
        && !meta.session_label.trim().eq_ignore_ascii_case(project_name)
    {
        meta.session_label.trim().chars().take(64).collect::<String>()
    } else {
        String::new()
    };
    if !detail.is_empty() {
        parts.push(detail);
    }
    parts.join(" - ")
}

fn session_agent_meta(payload: Option<&Value>) -> Vec<SessionAgentMeta> {
    let agent_map: std::collections::HashMap<String, (String, String, String, String)> = payload_agents(payload)
        .into_iter()
        .filter_map(|agent| {
            let role = agent.get("role").and_then(|v| v.as_str()).unwrap_or("");
            if role == "shade" {
                return None;
            }
            let id = agent.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if id.is_empty() {
                return None;
            }
            let name = agent.get("name").and_then(|v| v.as_str()).unwrap_or("agent").to_string();
            let project = agent.get("project").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let specialization = agent.get("specialization").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let last_summary = agent.get("last_summary").and_then(|v| v.as_str()).unwrap_or("").to_string();
            Some((id, (name, project, specialization, last_summary)))
        })
        .collect();

    if let Some(arr) = payload.and_then(|p| p.get("sessions")).and_then(|v| v.as_array()) {
        let mut out = Vec::new();
        for sess in arr {
            let role = sess.get("role").and_then(|v| v.as_str()).unwrap_or("");
            if role == "shade" {
                continue;
            }
            let agent_id = sess.get("agentId").or_else(|| sess.get("agent_id")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let id = sess.get("id").and_then(|v| v.as_str())
                .or_else(|| if agent_id.is_empty() { None } else { Some(agent_id.as_str()) })
                .unwrap_or("")
                .to_string();
            let fallback_name = sess.get("agentName").or_else(|| sess.get("agent_name")).and_then(|v| v.as_str()).unwrap_or("agent").to_string();
            let joined = agent_map.get(&agent_id).cloned().unwrap_or_else(|| (fallback_name.clone(), String::new(), String::new(), String::new()));
            let name = if !joined.0.is_empty() { joined.0 } else { fallback_name };
            let project = sess.get("project").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).unwrap_or(&joined.1).to_string();
            let specialization = joined.2;
            let last_summary = joined.3;
            let tmux = sess.get("tmuxSession").or_else(|| sess.get("tmux_session")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let status = sess.get("status").and_then(|v| v.as_str()).unwrap_or("idle").to_string();
            let source = sess.get("source").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let has_tmux = sess.get("hasTmux").and_then(|v| v.as_bool()).unwrap_or(false);
            let process_target = sess.get("processTarget").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let live_session_id = sess.get("liveSessionId").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let session_label = sess.get("sessionLabel").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let transport = sess.get("transport").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let socket = sess.get("socket").and_then(|v| v.as_str()).unwrap_or("").to_string();
            if source == "virtual" || source == "tmux" || source == "detected" || source == "live" {
                continue;
            }
            if id.is_empty() && tmux.is_empty() && name.is_empty() {
                continue;
            }
            if !has_tmux && tmux.is_empty() && source != "live" && transport != "charon" {
                continue;
            }
            out.push(SessionAgentMeta { id, agent_id, name, project, specialization, last_summary, tmux, status, source, process_target, live_session_id, session_label, transport, socket });
        }
        if !out.is_empty() {
            return out;
        }
    }

    payload_agents(payload)
        .into_iter()
        .filter_map(|agent| {
            let role = agent.get("role").and_then(|v| v.as_str()).unwrap_or("");
            if role == "shade" {
                return None;
            }
            let id = agent.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let name = agent.get("name").and_then(|v| v.as_str()).unwrap_or("agent").to_string();
            let project = agent.get("project").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let specialization = agent.get("specialization").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let last_summary = agent.get("last_summary").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let tmux = agent.get("tmux_session").or_else(|| agent.get("tmuxSession")).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let status = agent.get("status").and_then(|v| v.as_str()).unwrap_or("idle").to_string();
            if id.is_empty() && tmux.is_empty() && name.is_empty() {
                None
            } else {
                Some(SessionAgentMeta { id: id.clone(), agent_id: id, name: name.clone(), project, specialization, last_summary, tmux, status, source: "agent".to_string(), process_target: String::new(), live_session_id: String::new(), session_label: name, transport: String::new(), socket: String::new() })
            }
        })
        .collect()
}

fn filtered_session_meta(app: &App) -> Vec<SessionAgentMeta> {
    session_agent_meta(app.chat.refresh_payload.as_ref())
        .into_iter()
        .filter(|m| {
            app.sessions.selected_project.as_ref().map(|p| {
                let mp = m.project.split('/').last().unwrap_or(&m.project);
                mp == p
            }).unwrap_or(true)
        })
        .collect()
}

fn session_list_rows(app: &mut App) -> Vec<SessionListRow> {
    let filtered = filtered_session_meta(app);
    let mut grouped: std::collections::BTreeMap<String, Vec<SessionAgentMeta>> = std::collections::BTreeMap::new();
    for m in filtered {
        grouped.entry(m.name.clone()).or_default().push(m);
    }
    let mut rows = Vec::new();
    let mut session_ids = Vec::new();
    for (agent_name, mut sessions) in grouped {
        sessions.sort_by(|a, b| a.tmux.cmp(&b.tmux).then(a.id.cmp(&b.id)));
        let child_ids: Vec<String> = sessions.iter().map(|s| s.id.clone()).collect();
        let collapsed = app.sessions.collapsed_agents.contains(&agent_name);
        let project = sessions.iter()
            .find_map(|s| {
                let p = s.project.split('/').filter(|x| !x.is_empty()).last().unwrap_or(&s.project).trim();
                if p.is_empty() { None } else { Some(p.to_string()) }
            })
            .unwrap_or_default();
        let detail = sessions.iter()
            .find_map(|s| {
                if !s.specialization.trim().is_empty() {
                    Some(s.specialization.trim().to_string())
                } else if !s.last_summary.trim().is_empty() {
                    Some(s.last_summary.trim().chars().take(40).collect::<String>())
                } else {
                    None
                }
            })
            .unwrap_or_default();
        rows.push(SessionListRow::AgentHeader { name: agent_name.clone(), project, detail, count: sessions.len(), session_ids: child_ids.clone(), collapsed });
        if !collapsed {
            for sess in sessions {
                let project_name = sess.project.split('/').last().unwrap_or(&sess.project);
                let label = if sess.source == "live" && !sess.live_session_id.is_empty() {
                    format!("session {}", sess.live_session_id.split('-').last().unwrap_or(&sess.live_session_id))
                } else if sess.source == "detected" && !sess.process_target.is_empty() {
                    if project_name.is_empty() {
                        format!("{} session", sess.process_target)
                    } else {
                        format!("{} · {}", sess.process_target, project_name)
                    }
                } else {
                    compose_session_title(&sess)
                };
                session_ids.push(sess.id.clone());
                rows.push(SessionListRow::Session { id: sess.id, label, status: sess.status });
            }
        } else {
            session_ids.extend(child_ids);
        }
    }
    let current_ids: std::collections::HashSet<String> = session_ids.iter().cloned().collect();
    if app.sessions.visible_agents.is_empty() {
        for id in &session_ids {
            app.sessions.visible_agents.insert(id.clone());
        }
    } else {
        app.sessions.visible_agents.retain(|id| current_ids.contains(id));
        if app.sessions.visible_agents.is_empty() {
            for id in &session_ids {
                app.sessions.visible_agents.insert(id.clone());
            }
        }
    }
    for id in &session_ids {
        if !app.sessions.known_session_ids.contains(id) {
            app.sessions.visible_agents.insert(id.clone());
        }
    }
    app.sessions.known_session_ids = current_ids;
    rows
}

fn visible_session_agent_ids(app: &mut App) -> Vec<String> {
    let meta = session_agent_meta(app.chat.refresh_payload.as_ref());
    if meta.is_empty() {
        if app.sessions.backend_filter_pending {
            return vec![];
        }
        return app.sessions.panes.iter().enumerate().map(|(i, _)| format!("pane:{}", i)).collect();
    }
    session_list_rows(app)
        .into_iter()
        .filter_map(|row| match row {
            SessionListRow::Session { id, .. } if app.sessions.visible_agents.contains(&id) => Some(id),
            _ => None,
        })
        .collect()
}

fn pane_agent_id(cell: &SessionCell, payload: Option<&Value>, idx: usize) -> String {
    for m in session_agent_meta(payload) {
        let backend_match = match &cell.backend_type {
            BackendType::TmuxPane { session_name } => !m.tmux.is_empty() && m.tmux == *session_name,
            BackendType::BoatPane { session_id } => !m.tmux.is_empty() && m.tmux == *session_id,
            BackendType::CharonPane { socket_path } => m.transport == "charon" && !m.socket.is_empty() && m.socket == *socket_path,
            BackendType::LocalPty => false,
        };
        if backend_match {
            return m.id;
        }
    }
    format!("pane:{}", idx)
}

fn visible_pane_indices(app: &mut App) -> Vec<usize> {
    let allowed = visible_session_agent_ids(app);
    let matched: Vec<usize> = app.sessions.panes.iter().enumerate().filter_map(|(i, cell)| {
        let id = pane_agent_id(cell, app.chat.refresh_payload.as_ref(), i);
        if allowed.contains(&id) {
            Some(i)
        } else if matches!(cell.backend_type, BackendType::CharonPane { .. }) && id.starts_with("pane:") {
            Some(i)
        } else {
            None
        }
    }).collect();
    if matched.is_empty() && !allowed.is_empty() && !app.sessions.backend_filter_pending {
        return (0..app.sessions.panes.len()).collect();
    }
    matched
}

fn ensure_native_self_pane(app: &mut App, server: Option<&NativeSessionServer>, outer_w: u16, outer_h: u16) -> io::Result<bool> {
    let Some(server) = server else { return Ok(false); };
    let socket = server.socket_path().to_string_lossy().to_string();
    let exists = app.sessions.panes.iter().any(|c| match &c.backend_type {
        BackendType::CharonPane { socket_path } => socket_path == &socket,
        _ => false,
    });
    if exists {
        return Ok(false);
    }
    let idx = app.sessions.panes.len();
    let (_, _, rects) = compute_grid((idx + 1).max(1), outer_w.saturating_sub(((outer_w as f32) * 0.125) as u16 + 2), outer_h.saturating_sub(2));
    let r = rects.get(idx).copied().unwrap_or(Rect { x: 0, y: 0, width: 80, height: 24 });
    let label = format!("charon-{}", server.name());
    let cell = SessionCell::attach_charon(idx as u64, &label, &socket, r.width.max(1), r.height.max(1))?;
    app.sessions.panes.push(cell);
    Ok(true)
}

fn sync_session_panes_from_payload(app: &mut App, outer_w: u16, outer_h: u16) -> io::Result<bool> {
    let metas = session_agent_meta(app.chat.refresh_payload.as_ref());
    if metas.is_empty() {
        return Ok(false);
    }
    let target_total = metas.len().max(1);
    let (_, _, rects) = compute_grid(target_total, outer_w.saturating_sub(((outer_w as f32) * 0.125) as u16 + 2), outer_h.saturating_sub(2));
    let mut changed = false;
    for meta in metas {
        let composed_title = compose_session_title(&meta);
        let existing_idx = app.sessions.panes.iter().enumerate().find_map(|(i, c)| {
            let pid = pane_agent_id(c, app.chat.refresh_payload.as_ref(), i);
            let matched = pid == meta.id
                || match &c.backend_type {
                    BackendType::TmuxPane { session_name } => !meta.tmux.is_empty() && session_name == &meta.tmux,
                    BackendType::BoatPane { session_id } => !meta.tmux.is_empty() && session_id == &meta.tmux,
                    BackendType::CharonPane { socket_path } => meta.transport == "charon" && !meta.socket.is_empty() && socket_path == &meta.socket,
                    BackendType::LocalPty => false,
                };
            if matched { Some(i) } else { None }
        });
        if let Some(i) = existing_idx {
            if let Some(cell) = app.sessions.panes.get_mut(i) {
                if cell.title != composed_title {
                    cell.title = composed_title.clone();
                    cell.terminal.dirty = true;
                    changed = true;
                }
            }
            continue;
        }
        let idx = app.sessions.panes.len();
        let r = rects.get(idx).copied().unwrap_or(Rect { x: 0, y: 0, width: 80, height: 24 });
        let cell = if meta.transport == "charon" && !meta.socket.is_empty() {
            SessionCell::attach_charon(idx as u64, &composed_title, &meta.socket, r.width.max(1), r.height.max(1))
        } else if meta.transport == "pty" && !meta.socket.is_empty() {
            SessionCell::attach_boat_socket(idx as u64, &composed_title, &meta.tmux, &meta.socket, r.width.max(1), r.height.max(1))
        } else if meta.source == "boat" {
            SessionCell::attach_boat(idx as u64, &composed_title, &meta.tmux, r.width.max(1), r.height.max(1))
        } else if !meta.tmux.is_empty() {
            SessionCell::attach_tmux(idx as u64, &composed_title, &meta.tmux, r.width.max(1), r.height.max(1))
        } else {
            continue;
        };
        if let Ok(cell) = cell {
            app.sessions.panes.push(cell);
            changed = true;
        }
    }
    Ok(changed)
}

fn project_names(payload: Option<&Value>) -> Vec<String> {
    let mut names: Vec<String> = payload_projects(payload)
        .into_iter()
        .filter_map(|p| p.get("name").and_then(|v| v.as_str()).map(|s| s.to_string()))
        .collect();
    names.sort();
    names.dedup();
    names
}

fn keep_index_visible(index: usize, scroll: &mut usize, height: usize) {
    if height == 0 {
        *scroll = 0;
        return;
    }
    if index < *scroll {
        *scroll = index;
    } else if index >= *scroll + height {
        *scroll = index + 1 - height;
    }
}

fn pane_at_point(app: &mut App, rects: &[Rect], x: u16, y: u16) -> Option<usize> {
    let visible = visible_pane_indices(app);
    for (draw_i, pane_i) in visible.iter().enumerate() {
        let Some(r) = rects.get(draw_i) else { continue; };
        let left = r.x.saturating_sub(1);
        let top = r.y.saturating_sub(1);
        let right = r.x + r.width;
        let bottom = r.y + r.height;
        if x >= left && x <= right && y >= top && y <= bottom {
            return Some(*pane_i);
        }
    }
    None
}

fn scroll_session_pane(app: &mut App, pane_idx: usize, up: bool, native_session: Option<&NativeSessionServer>) -> io::Result<bool> {
    let bytes = if up { b"\x1b[5~".as_slice() } else { b"\x1b[6~".as_slice() };
    let Some(cell) = app.sessions.panes.get_mut(pane_idx) else {
        return Ok(false);
    };
    match &cell.backend_type {
        BackendType::CharonPane { socket_path } => {
            let is_local_self = native_session.map(|server| {
                socket_path == &server.socket_path().to_string_lossy().to_string()
            }).unwrap_or(false);
            if is_local_self {
                let saved_view = app.active_view;
                app.active_view = View::Chat;
                apply_native_input_bytes(app, bytes);
                app.active_view = saved_view;
            } else {
                cell.write(bytes)?;
            }
            Ok(true)
        }
        _ => {
            if up {
                cell.scroll_viewport_up((cell.terminal.height.max(1) / 2).max(1) as usize);
            } else {
                cell.scroll_viewport_down((cell.terminal.height.max(1) / 2).max(1) as usize);
            }
            Ok(true)
        }
    }
}

fn next_grid_focus(current_pane: usize, visible: &[usize], rects: &[Rect], direction: KeyCode) -> Option<usize> {
    let current_pos = visible.iter().position(|v| *v == current_pane)?;
    let cur = rects.get(current_pos)?;
    let cur_cx = cur.x as i32 + cur.width as i32 / 2;
    let cur_cy = cur.y as i32 + cur.height as i32 / 2;
    let mut best: Option<(i32, usize)> = None;
    for (idx, pane) in visible.iter().enumerate() {
        if *pane == current_pane { continue; }
        let Some(r) = rects.get(idx) else { continue; };
        let cx = r.x as i32 + r.width as i32 / 2;
        let cy = r.y as i32 + r.height as i32 / 2;
        let dx = cx - cur_cx;
        let dy = cy - cur_cy;
        let valid = match direction {
            KeyCode::Left => dx < 0,
            KeyCode::Right => dx > 0,
            KeyCode::Up => dy < 0,
            KeyCode::Down => dy > 0,
            _ => false,
        };
        if !valid { continue; }
        let primary = match direction {
            KeyCode::Left | KeyCode::Right => dx.abs(),
            KeyCode::Up | KeyCode::Down => dy.abs(),
            _ => 0,
        };
        let secondary = match direction {
            KeyCode::Left | KeyCode::Right => dy.abs(),
            KeyCode::Up | KeyCode::Down => dx.abs(),
            _ => 0,
        };
        let score = primary * 100 + secondary;
        if best.map(|(s, _)| score < s).unwrap_or(true) {
            best = Some((score, *pane));
        }
    }
    best.map(|(_, pane)| pane)
}

fn session_grid_rects(app: &mut App, outer_w: u16, outer_h: u16) -> Vec<Rect> {
    let sidebar_w = ((outer_w as f32) * 0.125) as u16;
    let grid_x = 1 + sidebar_w.min(outer_w.saturating_sub(8));
    let grid_w = outer_w.saturating_sub(grid_x + 1);
    let visible = visible_pane_indices(app);
    let (_, _, rects) = compute_grid(visible.len().max(1), grid_w, outer_h.saturating_sub(2));
    rects.into_iter().map(|mut r| { r.x += grid_x; r.y += 1; r }).collect()
}

fn relayout_sessions(app: &mut App, outer_w: u16, outer_h: u16) -> io::Result<Vec<Rect>> {
    let visible = visible_pane_indices(app);
    let rects = session_grid_rects(app, outer_w, outer_h);
    for idx in &visible {
        if let Some(pos) = visible.iter().position(|v| v == idx) {
            if let Some(r) = rects.get(pos) {
                if let Some(cell) = app.sessions.panes.get_mut(*idx) {
                    cell.resize(r.width, r.height)?;
                }
            }
        }
    }
    Ok(rects)
}

fn draw_sessions<W: Write>(stdout: &mut W, app: &mut App, rects: &[Rect], force_all: bool, w: u16, h: u16, self_socket_to_hide: Option<&str>) -> io::Result<()> {
    let sidebar_w = ((w as f32) * 0.125) as u16;
    let agents_area = Rect { x: 1, y: 2, width: sidebar_w.saturating_sub(2), height: (h.saturating_sub(4)) / 2 };
    let projects_area = Rect { x: 1, y: agents_area.y + agents_area.height + 1, width: sidebar_w.saturating_sub(2), height: h.saturating_sub(agents_area.height + 5) };
    render::render_border(stdout, agents_area, "agents", app.sessions.section == SessionsSection::Agents)?;
    render::render_border(stdout, projects_area, "projects", app.sessions.section == SessionsSection::Projects)?;

    let rows = session_list_rows(app);
    if app.sessions.agent_index >= rows.len() && !rows.is_empty() { app.sessions.agent_index = rows.len() - 1; }
    keep_index_visible(app.sessions.agent_index, &mut app.sessions.agent_scroll, agents_area.height as usize);
    if app.sessions.agent_scroll > 0 {
        stdout.queue(cursor::MoveTo(agents_area.x + agents_area.width.saturating_sub(1), agents_area.y))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "↑")?;
    }
    let agent_slice_end = (app.sessions.agent_scroll + agents_area.height as usize).min(rows.len());
    for (row, item) in rows[app.sessions.agent_scroll..agent_slice_end].iter().enumerate() {
        let i = app.sessions.agent_scroll + row;
        stdout.queue(cursor::MoveTo(agents_area.x, agents_area.y + row as u16))?;
        let prefix = if app.sessions.section == SessionsSection::Agents && i == app.sessions.agent_index { "▸" } else { " " };
        match item {
            SessionListRow::AgentHeader { name, project, detail, count, session_ids, collapsed } => {
                let selected_count = session_ids.iter().filter(|id| app.sessions.visible_agents.contains(*id)).count();
                let mark = if selected_count == 0 { "[ ]" } else if selected_count == session_ids.len() { "[x]" } else { "[-]" };
                let glyph = if *collapsed { "▸" } else { "▾" };
                stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 212, g: 196, b: 168 }))?;
                let base = format!("{} {} {} ", prefix, glyph, mark);
                let suffix = format!(" ({})", count);
                let total_w = agents_area.width as usize;
                let budget = total_w.saturating_sub(base.chars().count() + suffix.chars().count());
                let mut label = name.clone();
                let with_project = if !project.is_empty() { format!("{} - {}", name, project) } else { name.clone() };
                let with_detail = if !project.is_empty() && !detail.is_empty() {
                    format!("{} - {} - {}", name, project, detail)
                } else if !detail.is_empty() {
                    format!("{} - {}", name, detail)
                } else {
                    with_project.clone()
                };
                if with_detail.chars().count() <= budget {
                    label = with_detail;
                } else if with_project.chars().count() <= budget {
                    label = with_project;
                }
                let label: String = label.chars().take(budget).collect();
                let line = format!("{}{}{}", base, label, suffix);
                let visible: String = line.chars().take(total_w).collect();
                write!(stdout, "{}", visible)?;
            }
            SessionListRow::Session { id, label, status } => {
                let checked = app.sessions.visible_agents.contains(id);
                stdout.queue(style::SetForegroundColor(if checked { style::Color::Green } else { style::Color::DarkGrey }))?;
                let line = format!("{}  [{}] {} ({})", prefix, if checked { "x" } else { " " }, label, status);
                let visible: String = line.chars().take(agents_area.width as usize).collect();
                write!(stdout, "{}", visible)?;
            }
        }
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    if agent_slice_end < rows.len() && agents_area.height > 0 {
        stdout.queue(cursor::MoveTo(agents_area.x + agents_area.width.saturating_sub(1), agents_area.y + agents_area.height.saturating_sub(1)))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "↓")?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    let mut projects = vec!["All Projects".to_string()];
    projects.extend(project_names(app.chat.refresh_payload.as_ref()));
    if app.sessions.project_index >= projects.len() && !projects.is_empty() { app.sessions.project_index = projects.len() - 1; }
    keep_index_visible(app.sessions.project_index, &mut app.sessions.project_scroll, projects_area.height as usize);
    if app.sessions.project_scroll > 0 {
        stdout.queue(cursor::MoveTo(projects_area.x + projects_area.width.saturating_sub(1), projects_area.y))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "↑")?;
    }
    let project_slice_end = (app.sessions.project_scroll + projects_area.height as usize).min(projects.len());
    for (row, project) in projects[app.sessions.project_scroll..project_slice_end].iter().enumerate() {
        let i = app.sessions.project_scroll + row;
        stdout.queue(cursor::MoveTo(projects_area.x, projects_area.y + row as u16))?;
        let active = if i == 0 { app.sessions.selected_project.is_none() } else { app.sessions.selected_project.as_deref() == Some(project.as_str()) };
        let prefix = if app.sessions.section == SessionsSection::Projects && i == app.sessions.project_index { "▸" } else { " " };
        stdout.queue(style::SetForegroundColor(if active { style::Color::Yellow } else { style::Color::DarkGrey }))?;
        let line = format!("{} [{}] {}", prefix, if active { "x" } else { " " }, project);
        let visible: String = line.chars().take(projects_area.width as usize).collect();
        write!(stdout, "{}", visible)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    if project_slice_end < projects.len() && projects_area.height > 0 {
        stdout.queue(cursor::MoveTo(projects_area.x + projects_area.width.saturating_sub(1), projects_area.y + projects_area.height.saturating_sub(1)))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "↓")?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    let visible = visible_pane_indices(app);
    if visible.is_empty() {
        let grid_x = 1 + sidebar_w.min(w.saturating_sub(8));
        let area = Rect { x: grid_x, y: 2, width: w.saturating_sub(grid_x + 1), height: h.saturating_sub(4) };
        render::render_border(stdout, area, "grid", app.sessions.section == SessionsSection::Grid)?;
        stdout.queue(cursor::MoveTo(area.x + 1, area.y + 1))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        if app.sessions.backend_filter_pending {
            write!(stdout, "Loading session metadata…")?;
        } else {
            write!(stdout, "No visible sessions.")?;
        }
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    for (draw_i, pane_i) in visible.iter().enumerate() {
        if let Some(area) = rects.get(draw_i) {
            let (title, backend_type, dirty) = match app.sessions.panes.get(*pane_i) {
                Some(cell) => (cell.title.clone(), cell.backend_type.clone(), cell.terminal.dirty),
                None => continue,
            };
            let focused = *pane_i == app.sessions.focused && app.sessions.section == SessionsSection::Grid;
            render::render_border(stdout, *area, &title, focused)?;
            let is_self_charon = match (&backend_type, self_socket_to_hide) {
                (BackendType::CharonPane { socket_path }, Some(sock)) => socket_path == sock,
                _ => false,
            };
            if is_self_charon {
                render_local_charon_preview(stdout, app, *area, self_socket_to_hide)?;
                if let Some(cell) = app.sessions.panes.get_mut(*pane_i) {
                    cell.terminal.dirty = false;
                }
            } else if dirty || force_all {
                if let Some(cell) = app.sessions.panes.get_mut(*pane_i) {
                    render::render_terminal(stdout, &cell.terminal, *area, cell.viewport_scroll)?;
                    cell.terminal.dirty = false;
                }
            }
        }
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let mode = parse_args();

    if let LaunchMode::ListSessions = mode {
        let discovered = discover_sessions();
        if discovered.is_empty() {
            println!("No tmux sessions found.");
        } else {
            println!("Discoverable sessions:");
            for s in discovered {
                println!("  {} → tmux:{} ({})", s.display_name, s.session_name, s.agent_type);
            }
        }
        return Ok(());
    }

    let (mut outer_w, mut outer_h) = ct::size()?;
    let panes = build_initial_sessions(&mode, outer_w, outer_h)?;
    let mut app = App::new(panes)?;
    let native_session = NativeSessionServer::start(None).ok();
    app.sessions.backend_filter_pending = matches!(mode, LaunchMode::AutoDiscover);
    let _ = ensure_native_self_pane(&mut app, native_session.as_ref(), outer_w, outer_h);
    let mut session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;

    ct::enable_raw_mode()?;
    let mut stdout = io::stdout();
    stdout.queue(EnableBracketedPaste)?;
    let mut mouse_capture_enabled = false;
    if app.active_view == View::Sessions {
        stdout.queue(EnableMouseCapture)?;
        mouse_capture_enabled = true;
    }
    stdout.queue(cursor::Hide)?;
    stdout.queue(ct::Clear(ct::ClearType::All))?;
    stdout.queue(cursor::MoveTo(0, 0))?;
    write!(stdout, "\x1b[3J")?;
    stdout.flush()?;

    let mut last_render = Instant::now();
    let frame_duration = Duration::from_millis(16);
    let mut needs_full_redraw = true;
    let mut local_view_dirty = false;
    let mut last_rowing_tick = Instant::now();
    let mut last_session_poll = Instant::now() - Duration::from_secs(1);

    loop {
        let mut any_dirty = false;
        let pane_poll_interval = match app.active_view {
            View::Sessions if app.sessions.terminal_mode => Duration::from_millis(16),
            View::Sessions => Duration::from_millis(33),
            _ => Duration::from_millis(250),
        };
        if last_session_poll.elapsed() >= pane_poll_interval {
            let visible_set: std::collections::HashSet<usize> = if app.active_view == View::Sessions {
                visible_pane_indices(&mut app).into_iter().collect()
            } else {
                (0..app.sessions.panes.len()).collect()
            };
            for (idx, cell) in app.sessions.panes.iter_mut().enumerate() {
                if !visible_set.contains(&idx) {
                    continue;
                }
                cell.poll()?;
                if cell.terminal.dirty {
                    any_dirty = true;
                }
            }
            if app.active_view == View::InterAgent {
                for cell in app.inter_agent.room_panes.iter_mut() {
                    cell.poll()?;
                    if cell.terminal.dirty {
                        cell.reset_viewport_scroll();
                        any_dirty = true;
                    }
                }
            }
            last_session_poll = Instant::now();
        }
        let chat_dirty = app.chat.poll();
        let native_input_dirty = if let Some(server) = &native_session {
            let commands = server.drain_commands();
            let dirty = !commands.is_empty();
            if dirty {
                apply_native_commands(&mut app, commands);
                needs_full_redraw = true;
            }
            dirty
        } else {
            false
        };
        let mut session_structure_changed = false;
        if app.active_view == View::Sessions && (chat_dirty || native_input_dirty) && app.chat.refresh_payload.is_some() {
            app.sessions.backend_filter_pending = false;
            let mut changed = sync_session_panes_from_payload(&mut app, outer_w, outer_h)?;
            if ensure_native_self_pane(&mut app, native_session.as_ref(), outer_w, outer_h)? {
                changed = true;
            }
            if changed {
                session_structure_changed = true;
                needs_full_redraw = true;
            }
        }
        if app.active_view == View::InterAgent && (chat_dirty || native_input_dirty) && app.chat.refresh_payload.is_some() {
            let refresh_payload = app.chat.refresh_payload.clone();
            let rooms = payload_inter_agent_rooms(refresh_payload.as_ref());
            if let Some(room) = rooms.get(app.inter_agent.selected).cloned() {
                if room.get("kind").and_then(|v| v.as_str()).unwrap_or("") != "libris" {
                    if sync_inter_agent_room_panes(&mut app, &room, outer_w.saturating_sub(8), outer_h.saturating_sub(10))? {
                        needs_full_redraw = true;
                    }
                }
            }
        }
        if session_structure_changed {
            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
            let visible = visible_pane_indices(&mut app);
            if let Some(first) = visible.first() {
                if !visible.contains(&app.sessions.focused) {
                    app.sessions.focused = *first;
                }
            }
        }

        let now = Instant::now();
        if app.active_view == View::Chat && chat_rowing_active(&app) && now.duration_since(last_rowing_tick) >= Duration::from_millis(300) {
            local_view_dirty = true;
            last_rowing_tick = now;
        }
        let sessions_dirty = app.active_view == View::Sessions && (any_dirty || native_input_dirty || local_view_dirty);
        let dashboard_dirty = app.active_view == View::Dashboard && (chat_dirty || native_input_dirty || local_view_dirty);
        let chat_view_dirty = app.active_view == View::Chat && (chat_dirty || native_input_dirty || local_view_dirty);
        let inter_agent_dirty = app.active_view == View::InterAgent && (any_dirty || chat_dirty || native_input_dirty || local_view_dirty);
        let native_snapshot_dirty = native_input_dirty;
        if (needs_full_redraw || sessions_dirty || dashboard_dirty || chat_view_dirty || inter_agent_dirty || native_snapshot_dirty) && now.duration_since(last_render) >= frame_duration {
            stdout.queue(cursor::Hide)?;
            let force_all = needs_full_redraw;
            if needs_full_redraw {
                stdout.queue(ct::Clear(ct::ClearType::All))?;
                needs_full_redraw = false;
            }

            draw_header(&mut stdout, &app, outer_w)?;
            match app.active_view {
                View::Chat => draw_chat(&mut stdout, &app, outer_w, outer_h)?,
                View::Dashboard => draw_dashboard(&mut stdout, &app, outer_w, outer_h)?,
                View::Sessions => draw_sessions(
                    &mut stdout,
                    &mut app,
                    &session_rects,
                    force_all,
                    outer_w,
                    outer_h,
                    native_session.as_ref().map(|s| s.socket_path().to_string_lossy().to_string()).as_deref(),
                )?,
                View::InterAgent => draw_inter_agent(&mut stdout, &mut app, outer_w, outer_h)?,
            }
            draw_footer(&mut stdout, &app, outer_w, outer_h)?;
            if let Some(server) = &native_session {
                let self_sock = server.socket_path().to_string_lossy().to_string();
                let (snap_w, snap_h) = server.requested_size().unwrap_or((outer_w, outer_h));
                server.update_snapshot(build_native_session_snapshot(&mut app, snap_w.max(1), snap_h.max(1), Some(&self_sock)));
            }
            stdout.flush()?;
            last_render = now;
            local_view_dirty = false;
        }

        let want_mouse_capture = app.active_view == View::Chat
            || (app.active_view == View::Sessions && !app.sessions.terminal_mode)
            || app.active_view == View::InterAgent;
        if want_mouse_capture != mouse_capture_enabled {
            if want_mouse_capture {
                stdout.queue(EnableMouseCapture)?;
            } else {
                stdout.queue(DisableMouseCapture)?;
            }
            stdout.flush()?;
            mouse_capture_enabled = want_mouse_capture;
        }

        let event_poll_interval = match app.active_view {
            View::Sessions if app.sessions.terminal_mode => Duration::from_millis(8),
            View::Sessions => Duration::from_millis(16),
            _ => Duration::from_millis(33),
        };
        if event::poll(event_poll_interval)? {
            match event::read()? {
                Event::Key(key) => {
                    match key.code {
                        KeyCode::F(1) => { app.active_view = View::Chat; needs_full_redraw = true; continue; }
                        KeyCode::F(2) => { app.active_view = View::Dashboard; needs_full_redraw = true; continue; }
                        KeyCode::F(3) => {
                            app.active_view = View::Sessions;
                            app.chat.request_refresh();
                            needs_full_redraw = true;
                            continue;
                        }
                        KeyCode::F(4) => {
                            app.active_view = View::InterAgent;
                            app.chat.request_refresh();
                            needs_full_redraw = true;
                            continue;
                        }
                        _ => {}
                    }

                    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('q') {
                        break;
                    }

                    match app.active_view {
                        View::Chat => {
                            if app.chat.context_menu.is_some() {
                                match key.code {
                                    KeyCode::Esc => {
                                        app.chat.context_menu = None;
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Up => {
                                        if let Some(menu) = app.chat.context_menu.as_mut() {
                                            menu.selected = menu.selected.saturating_sub(1);
                                        }
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down => {
                                        let len = chat_context_menu_items(&app).len();
                                        if let Some(menu) = app.chat.context_menu.as_mut() {
                                            if menu.selected + 1 < len {
                                                menu.selected += 1;
                                            }
                                        }
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        let _ = activate_chat_context_menu(&mut app, outer_w, outer_h);
                                        local_view_dirty = true;
                                    }
                                    _ => {}
                                }
                            } else if key.code == KeyCode::Esc {
                                app.chat.selection_anchor = None;
                                app.chat.selection_focus = None;
                                app.chat.selection_dragging = false;
                                local_view_dirty = true;
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                                if let Some(bounds) = chat_selection_bounds(&app) {
                                    let area = chat_content_area(&app, outer_w, outer_h);
                                    let lines = build_chat_visual_lines(&app, area.width as usize, chat_layout_variant(outer_w, outer_h));
                                    let text = chat_selection_text(&lines, bounds);
                                    if !text.is_empty() && copy_to_clipboard(&text) {
                                        local_view_dirty = true;
                                    }
                                }
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
                                app.chat.info_pane_open = !app.chat.info_pane_open;
                                needs_full_redraw = true;
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('i') {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 1) % 3;
                                local_view_dirty = true;
                            } else if key.code == KeyCode::BackTab {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 2) % 3;
                                local_view_dirty = true;
                            } else if app.chat.info_pane_open
                                && !app.chat.copy_mode
                                && !app.chat.approval_open()
                                && !app.chat.auth_open()
                                && !app.chat.menu_open()
                                && key.code == KeyCode::Right {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 1) % 3;
                                local_view_dirty = true;
                            } else if app.chat.info_pane_open
                                && !app.chat.copy_mode
                                && !app.chat.approval_open()
                                && !app.chat.auth_open()
                                && !app.chat.menu_open()
                                && key.code == KeyCode::Left {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 2) % 3;
                                local_view_dirty = true;
                            } else if key.code == KeyCode::F(6) {
                                app.chat.copy_mode = !app.chat.copy_mode;
                                local_view_dirty = true;
                            } else if app.chat.copy_mode {
                                if key.code == KeyCode::Esc {
                                    app.chat.copy_mode = false;
                                    local_view_dirty = true;
                                }
                            } else if app.chat.approval_open() {
                                match key.code {
                                    KeyCode::Esc => {
                                        app.chat.approval_deny();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Up | KeyCode::Left => {
                                        app.chat.approval_move_prev();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down | KeyCode::Right => {
                                        app.chat.approval_move_next();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        app.chat.approval_accept_selected();
                                        local_view_dirty = true;
                                    }
                                    _ => {}
                                }
                            } else if app.chat.auth_open() {
                                match key.code {
                                    KeyCode::Esc => {
                                        app.chat.auth_dismiss();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Left => {
                                        app.chat.auth_move_prev();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Right | KeyCode::Tab => {
                                        app.chat.auth_move_next();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        app.chat.auth_activate_selected();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Char('o') | KeyCode::Char('O') => {
                                        app.chat.auth_action_index = 0;
                                        app.chat.auth_activate_selected();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Char('c') | KeyCode::Char('C') => {
                                        app.chat.auth_action_index = 1;
                                        app.chat.auth_activate_selected();
                                        local_view_dirty = true;
                                    }
                                    _ => {}
                                }
                            } else if app.chat.menu_open() {
                                match key.code {
                                    KeyCode::Esc => {
                                        app.chat.close_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Up => {
                                        app.chat.menu_move_up();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down => {
                                        app.chat.menu_move_down();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Tab => {
                                        app.chat.menu_fill_input();
                                        app.chat.close_menu();
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        app.chat.menu_select();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                                        app.chat.input.push(c);
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Backspace => {
                                        app.chat.input.pop();
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    _ => {}
                                }
                            } else {
                                match key.code {
                                    KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                                        app.chat.input.push(c);
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Backspace => {
                                        app.chat.input.pop();
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        app.chat.submit_input();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Tab => {
                                        if app.chat.input.trim().starts_with('/') {
                                            app.chat.maybe_open_command_menu();
                                            local_view_dirty = true;
                                        }
                                    }
                                    KeyCode::Up if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                        app.chat.scroll = app.chat.scroll.saturating_add(1);
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                        app.chat.scroll = app.chat.scroll.saturating_sub(1);
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Up => {
                                        app.chat.history_up();
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down => {
                                        app.chat.history_down();
                                        app.chat.maybe_open_command_menu();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::PageUp => {
                                        app.chat.scroll = app.chat.scroll.saturating_add(10);
                                        local_view_dirty = true;
                                    }
                                    KeyCode::PageDown => {
                                        app.chat.scroll = app.chat.scroll.saturating_sub(10);
                                        local_view_dirty = true;
                                    }
                                    _ => {}
                                }
                            }
                        }
                        View::Dashboard => {
                            let agent_count = payload_agents(app.chat.refresh_payload.as_ref()).len();
                            match key.code {
                                KeyCode::Up => {
                                    app.dashboard.selected = app.dashboard.selected.saturating_sub(1);
                                    needs_full_redraw = true;
                                }
                                KeyCode::Down => {
                                    if app.dashboard.selected + 1 < agent_count {
                                        app.dashboard.selected += 1;
                                    }
                                    needs_full_redraw = true;
                                }
                                _ => {}
                            }
                        }
                        View::InterAgent => {
                            let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                            let room_count = rooms.len();
                            let selected_room = rooms.get(app.inter_agent.selected);
                            let selected_kind = selected_room
                                .and_then(|r| r.get("kind")).and_then(|v| v.as_str()).unwrap_or("");
                            if app.inter_agent.delete_confirm_open {
                                match key.code {
                                    KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => {
                                        app.inter_agent.delete_confirm_open = false;
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Enter | KeyCode::Char('y') | KeyCode::Char('Y') => {
                                        if !app.inter_agent.delete_target_room_id.is_empty() {
                                            let _ = app.chat.backend.send_command(&format!("/delete-room {}", app.inter_agent.delete_target_room_id));
                                            app.chat.request_refresh();
                                        }
                                        app.inter_agent.delete_confirm_open = false;
                                        app.inter_agent.delete_target_room_id.clear();
                                        app.inter_agent.delete_target_title.clear();
                                        app.inter_agent.room_panes.clear();
                                        app.inter_agent.room_panes_room_id.clear();
                                        needs_full_redraw = true;
                                    }
                                    _ => {}
                                }
                                continue;
                            }
                            match key.code {
                                KeyCode::Esc => {
                                    app.inter_agent.transcript_anchor = None;
                                    app.inter_agent.transcript_focus = None;
                                    app.inter_agent.transcript_dragging = false;
                                    needs_full_redraw = true;
                                }
                                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                                    if let Some(room) = selected_room {
                                        if let Some(bounds) = transcript_selection_bounds(&app) {
                                            let area = inter_agent_stream_area(&app, outer_w, outer_h);
                                            if let Some(area) = area {
                                                let rows = conversation_transcript_rows(room, area.width as usize);
                                                let text = transcript_selection_text(&rows, bounds);
                                                if !text.is_empty() && copy_to_clipboard(&text) {
                                                    needs_full_redraw = true;
                                                }
                                            }
                                        }
                                    }
                                }
                                KeyCode::Char('y') | KeyCode::Char('Y') => {
                                    if let Some(room) = selected_room {
                                        if let Some(bounds) = transcript_selection_bounds(&app) {
                                            let area = inter_agent_stream_area(&app, outer_w, outer_h);
                                            if let Some(area) = area {
                                                let rows = conversation_transcript_rows(room, area.width as usize);
                                                let text = transcript_selection_text(&rows, bounds);
                                                if !text.is_empty() && copy_to_clipboard(&text) {
                                                    needs_full_redraw = true;
                                                }
                                            }
                                        }
                                    }
                                }
                                KeyCode::Char('d') | KeyCode::Delete => {
                                    if let Some(room) = selected_room {
                                        let room_id = room.get("id").and_then(|v| v.as_str()).unwrap_or("");
                                        if !room_id.is_empty() {
                                            app.inter_agent.delete_confirm_open = true;
                                            app.inter_agent.delete_target_room_id = room_id.to_string();
                                            app.inter_agent.delete_target_title = room.get("title").and_then(|v| v.as_str()).unwrap_or(room_id).to_string();
                                            needs_full_redraw = true;
                                        }
                                    }
                                }
                                KeyCode::Tab => {
                                    if selected_kind == "libris" {
                                        app.inter_agent.graph_focus = !app.inter_agent.graph_focus;
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::Up => {
                                    if app.inter_agent.graph_focus && selected_kind == "libris" {
                                        app.inter_agent.selected_node = app.inter_agent.selected_node.saturating_sub(1);
                                    } else {
                                        app.inter_agent.selected = app.inter_agent.selected.saturating_sub(1);
                                        app.inter_agent.selected_node = 0;
                                        app.inter_agent.event_scroll = 0;
                                        app.inter_agent.topic_detail = false;
                                        app.inter_agent.transcript_anchor = None;
                                        app.inter_agent.transcript_focus = None;
                                        app.inter_agent.transcript_dragging = false;
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::Down => {
                                    if app.inter_agent.graph_focus && selected_kind == "libris" {
                                        app.inter_agent.selected_node = app.inter_agent.selected_node.saturating_add(1);
                                    } else {
                                        if app.inter_agent.selected + 1 < room_count {
                                            app.inter_agent.selected += 1;
                                        }
                                        app.inter_agent.selected_node = 0;
                                        app.inter_agent.event_scroll = 0;
                                        app.inter_agent.topic_detail = false;
                                        app.inter_agent.transcript_anchor = None;
                                        app.inter_agent.transcript_focus = None;
                                        app.inter_agent.transcript_dragging = false;
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::Left => {
                                    if selected_kind == "libris" {
                                        app.inter_agent.graph_focus = false;
                                        needs_full_redraw = true;
                                    }
                                }
                                KeyCode::Right => {
                                    if selected_kind == "libris" {
                                        app.inter_agent.graph_focus = true;
                                        needs_full_redraw = true;
                                    }
                                }
                                KeyCode::Enter => {
                                    if selected_kind == "libris" {
                                        app.inter_agent.topic_detail = !app.inter_agent.topic_detail;
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::PageUp => {
                                    app.inter_agent.event_scroll = app.inter_agent.event_scroll.saturating_add(10);
                                    needs_full_redraw = true;
                                }
                                KeyCode::PageDown => {
                                    app.inter_agent.event_scroll = app.inter_agent.event_scroll.saturating_sub(10);
                                    needs_full_redraw = true;
                                }
                                _ => {}
                            }
                        }
                        View::Sessions => {
                            if app.sessions.terminal_mode {
                                let exit_terminal_mode = matches!(key.code, KeyCode::F(4))
                                    || (key.modifiers.contains(KeyModifiers::CONTROL)
                                        && matches!(key.code, KeyCode::Char(']') | KeyCode::Char('g') | KeyCode::Char('G')));
                                if exit_terminal_mode {
                                    app.sessions.terminal_mode = false;
                                    needs_full_redraw = true;
                                    continue;
                                }
                                let encoded = encode_key(&key);
                                if !encoded.is_empty() {
                                    let is_local_self = match (app.sessions.panes.get(app.sessions.focused), native_session.as_ref()) {
                                        (Some(cell), Some(server)) => match &cell.backend_type {
                                            BackendType::CharonPane { socket_path } => socket_path == &server.socket_path().to_string_lossy().to_string(),
                                            _ => false,
                                        },
                                        _ => false,
                                    };
                                    if is_local_self {
                                        if let Some(cell) = app.sessions.panes.get_mut(app.sessions.focused) {
                                            cell.reset_viewport_scroll();
                                        }
                                        let saved_view = app.active_view;
                                        if app.active_view == View::Sessions {
                                            app.active_view = View::Chat;
                                        }
                                        apply_native_input_bytes(&mut app, &encoded);
                                        app.active_view = saved_view;
                                        needs_full_redraw = true;
                                    } else if let Some(cell) = app.sessions.panes.get_mut(app.sessions.focused) {
                                        cell.reset_viewport_scroll();
                                        cell.write(&encoded)?;
                                    }
                                }
                            } else {
                                match key.code {
                                    KeyCode::Tab => {
                                        app.sessions.section = match app.sessions.section {
                                            SessionsSection::Agents => SessionsSection::Projects,
                                            SessionsSection::Projects => SessionsSection::Grid,
                                            SessionsSection::Grid => SessionsSection::Agents,
                                        };
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::BackTab => {
                                        app.sessions.section = match app.sessions.section {
                                            SessionsSection::Agents => SessionsSection::Grid,
                                            SessionsSection::Projects => SessionsSection::Agents,
                                            SessionsSection::Grid => SessionsSection::Projects,
                                        };
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Enter => {
                                        match app.sessions.section {
                                            SessionsSection::Grid => {
                                                app.sessions.terminal_mode = true;
                                            }
                                            SessionsSection::Agents => {
                                                let rows = session_list_rows(&mut app);
                                                match rows.get(app.sessions.agent_index) {
                                                    Some(SessionListRow::Session { id, .. }) => {
                                                        if !app.sessions.visible_agents.insert(id.clone()) {
                                                            app.sessions.visible_agents.remove(id);
                                                        }
                                                    }
                                                    Some(SessionListRow::AgentHeader { session_ids, .. }) => {
                                                        let all_selected = session_ids.iter().all(|id| app.sessions.visible_agents.contains(id));
                                                        if all_selected {
                                                            for id in session_ids { app.sessions.visible_agents.remove(id); }
                                                        } else {
                                                            for id in session_ids { app.sessions.visible_agents.insert(id.clone()); }
                                                        }
                                                    }
                                                    _ => {}
                                                }
                                                let visible = visible_pane_indices(&mut app);
                                                if let Some(first) = visible.first() {
                                                    app.sessions.focused = *first;
                                                }
                                                session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            }
                                            SessionsSection::Projects => {
                                                let projects = project_names(app.chat.refresh_payload.as_ref());
                                                app.sessions.selected_project = if app.sessions.project_index == 0 {
                                                    None
                                                } else {
                                                    projects.get(app.sessions.project_index - 1).cloned()
                                                };
                                                app.sessions.visible_agents.clear();
                                                let visible = visible_session_agent_ids(&mut app);
                                                if let Some(first_id) = visible.first() {
                                                    if let Some((idx, _)) = app.sessions.panes.iter().enumerate().find(|(i, c)| pane_agent_id(c, app.chat.refresh_payload.as_ref(), *i) == *first_id) {
                                                        app.sessions.focused = idx;
                                                    }
                                                }
                                                session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            }
                                        }
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Up => {
                                        match app.sessions.section {
                                            SessionsSection::Agents => {
                                                app.sessions.agent_index = app.sessions.agent_index.saturating_sub(1);
                                            }
                                            SessionsSection::Projects => {
                                                app.sessions.project_index = app.sessions.project_index.saturating_sub(1);
                                            }
                                            SessionsSection::Grid => {
                                                let visible = visible_pane_indices(&mut app);
                                                if let Some(next) = next_grid_focus(app.sessions.focused, &visible, &session_rects, KeyCode::Up) {
                                                    app.sessions.focused = next;
                                                }
                                            }
                                        }
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Down => {
                                        match app.sessions.section {
                                            SessionsSection::Agents => {
                                                let rows = session_list_rows(&mut app);
                                                if app.sessions.agent_index + 1 < rows.len() {
                                                    app.sessions.agent_index += 1;
                                                }
                                            }
                                            SessionsSection::Projects => {
                                                let len = project_names(app.chat.refresh_payload.as_ref()).len() + 1;
                                                if app.sessions.project_index + 1 < len {
                                                    app.sessions.project_index += 1;
                                                }
                                            }
                                            SessionsSection::Grid => {
                                                let visible = visible_pane_indices(&mut app);
                                                if let Some(next) = next_grid_focus(app.sessions.focused, &visible, &session_rects, KeyCode::Down) {
                                                    app.sessions.focused = next;
                                                } else if let Some(first) = visible.first() {
                                                    app.sessions.focused = *first;
                                                }
                                            }
                                        }
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Left | KeyCode::Right => {
                                        match app.sessions.section {
                                            SessionsSection::Agents => {
                                                let rows = session_list_rows(&mut app);
                                                if let Some(SessionListRow::AgentHeader { name, collapsed, .. }) = rows.get(app.sessions.agent_index) {
                                                    if key.code == KeyCode::Left && !collapsed {
                                                        app.sessions.collapsed_agents.insert(name.clone());
                                                        session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                                    } else if key.code == KeyCode::Right && *collapsed {
                                                        app.sessions.collapsed_agents.remove(name);
                                                        session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                                    }
                                                }
                                            }
                                            SessionsSection::Grid => {
                                                let visible = visible_pane_indices(&mut app);
                                                if let Some(next) = next_grid_focus(app.sessions.focused, &visible, &session_rects, key.code) {
                                                    app.sessions.focused = next;
                                                }
                                            }
                                            SessionsSection::Projects => {}
                                        }
                                        needs_full_redraw = true;
                                    }
                                    KeyCode::Char('n') => {
                                        let title = format!("bash-{}", app.sessions.panes.len());
                                        let temp = compute_grid(app.sessions.panes.len() + 1, outer_w, outer_h.saturating_sub(2)).2;
                                        if let Some(r) = temp.last() {
                                            app.sessions.panes.push(SessionCell::spawn(app.sessions.panes.len() as u64, &title, &["bash"], r.width, r.height)?);
                                            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            app.sessions.focused = app.sessions.panes.len() - 1;
                                            needs_full_redraw = true;
                                        }
                                    }
                                    KeyCode::Char('w') => {
                                        if app.sessions.panes.len() > 1 {
                                            app.sessions.panes.remove(app.sessions.focused);
                                            if app.sessions.focused >= app.sessions.panes.len() {
                                                app.sessions.focused = app.sessions.panes.len().saturating_sub(1);
                                            }
                                            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            needs_full_redraw = true;
                                        }
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }
                }
                Event::Paste(text) => {
                    if app.active_view == View::Chat && !app.chat.copy_mode {
                        app.chat.input.push_str(&text);
                        app.chat.maybe_open_command_menu();
                        needs_full_redraw = true;
                    } else if app.active_view == View::Sessions && app.sessions.terminal_mode {
                        let bytes = text.into_bytes();
                        let is_local_self = match (app.sessions.panes.get(app.sessions.focused), native_session.as_ref()) {
                            (Some(cell), Some(server)) => match &cell.backend_type {
                                BackendType::CharonPane { socket_path } => socket_path == &server.socket_path().to_string_lossy().to_string(),
                                _ => false,
                            },
                            _ => false,
                        };
                        if is_local_self {
                            if let Some(cell) = app.sessions.panes.get_mut(app.sessions.focused) {
                                cell.reset_viewport_scroll();
                            }
                            let saved_view = app.active_view;
                            app.active_view = View::Chat;
                            apply_native_input_bytes(&mut app, &bytes);
                            app.active_view = saved_view;
                            needs_full_redraw = true;
                        } else if let Some(cell) = app.sessions.panes.get_mut(app.sessions.focused) {
                            cell.reset_viewport_scroll();
                            cell.write(&bytes)?;
                        }
                    }
                }
                Event::Mouse(mouse) => {
                    if app.active_view == View::Chat {
                        let area = chat_content_area(&app, outer_w, outer_h);
                        let lines = build_chat_visual_lines(&app, area.width as usize, chat_layout_variant(outer_w, outer_h));
                        match mouse.kind {
                            MouseEventKind::ScrollUp => {
                                if point_in_rect(area, mouse.column, mouse.row) {
                                    let max_scroll = lines.len().saturating_sub(area.height as usize);
                                    app.chat.scroll = (app.chat.scroll + 3).min(max_scroll);
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::ScrollDown => {
                                if point_in_rect(area, mouse.column, mouse.row) {
                                    app.chat.scroll = app.chat.scroll.saturating_sub(3);
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Down(MouseButton::Left) => {
                                if let Some(menu_area) = chat_context_menu_area(&app, outer_w, outer_h) {
                                    if point_in_rect(menu_area, mouse.column, mouse.row) {
                                        if let Some(menu) = app.chat.context_menu.as_mut() {
                                            menu.selected = mouse.row.saturating_sub(menu_area.y) as usize;
                                        }
                                        let _ = activate_chat_context_menu(&mut app, outer_w, outer_h);
                                        local_view_dirty = true;
                                        continue;
                                    }
                                    app.chat.context_menu = None;
                                }
                                if let Some(point) = chat_point_at_mouse(&lines, area, app.chat.scroll, mouse.column, mouse.row) {
                                    app.chat.selection_anchor = Some(point);
                                    app.chat.selection_focus = Some(point);
                                    app.chat.selection_dragging = true;
                                } else {
                                    app.chat.selection_anchor = None;
                                    app.chat.selection_focus = None;
                                    app.chat.selection_dragging = false;
                                }
                                local_view_dirty = true;
                            }
                            MouseEventKind::Drag(MouseButton::Left) => {
                                if app.chat.selection_dragging {
                                    let max_scroll = lines.len().saturating_sub(area.height as usize);
                                    if mouse.row < area.y {
                                        app.chat.scroll = (app.chat.scroll + 1).min(max_scroll);
                                    } else if mouse.row >= area.y.saturating_add(area.height) {
                                        app.chat.scroll = app.chat.scroll.saturating_sub(1);
                                    }
                                    if let Some(point) = chat_point_at_mouse(&lines, area, app.chat.scroll, mouse.column, mouse.row) {
                                        app.chat.selection_focus = Some(point);
                                    }
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Up(MouseButton::Left) => {
                                if app.chat.selection_dragging {
                                    app.chat.selection_dragging = false;
                                    if let Some(point) = chat_point_at_mouse(&lines, area, app.chat.scroll, mouse.column, mouse.row) {
                                        app.chat.selection_focus = Some(point);
                                    }
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Down(MouseButton::Right) | MouseEventKind::Up(MouseButton::Right) => {
                                app.chat.context_menu = Some(ChatContextMenu {
                                    x: mouse.column,
                                    y: mouse.row,
                                    selected: 0,
                                });
                                local_view_dirty = true;
                            }
                            _ => {}
                        }
                    } else if app.active_view == View::Sessions {
                        match mouse.kind {
                            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                                if let Some(pane_idx) = pane_at_point(&mut app, &session_rects, mouse.column, mouse.row) {
                                    app.sessions.focused = pane_idx;
                                    app.sessions.section = SessionsSection::Grid;
                                    let _ = scroll_session_pane(&mut app, pane_idx, matches!(mouse.kind, MouseEventKind::ScrollUp), native_session.as_ref())?;
                                    needs_full_redraw = true;
                                }
                            }
                            MouseEventKind::Down(_) | MouseEventKind::Drag(_) | MouseEventKind::Moved => {
                                if let Some(pane_idx) = pane_at_point(&mut app, &session_rects, mouse.column, mouse.row) {
                                    if app.sessions.focused != pane_idx {
                                        app.sessions.focused = pane_idx;
                                        app.sessions.section = SessionsSection::Grid;
                                        needs_full_redraw = true;
                                    }
                                }
                            }
                            _ => {}
                        }
                    } else if app.active_view == View::InterAgent {
                        match mouse.kind {
                            MouseEventKind::ScrollUp => {
                                if let Some(area) = inter_agent_stream_area(&app, outer_w, outer_h) {
                                    if point_in_rect(area, mouse.column, mouse.row) {
                                        app.inter_agent.event_scroll = app.inter_agent.event_scroll.saturating_add(3);
                                        needs_full_redraw = true;
                                    }
                                }
                            }
                            MouseEventKind::ScrollDown => {
                                if let Some(area) = inter_agent_stream_area(&app, outer_w, outer_h) {
                                    if point_in_rect(area, mouse.column, mouse.row) {
                                        app.inter_agent.event_scroll = app.inter_agent.event_scroll.saturating_sub(3);
                                        needs_full_redraw = true;
                                    }
                                }
                            }
                            MouseEventKind::Down(MouseButton::Left) => {
                                let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                                if let (Some(room), Some(area)) = (rooms.get(app.inter_agent.selected), inter_agent_stream_area(&app, outer_w, outer_h)) {
                                    let rows = conversation_transcript_rows(room, area.width as usize);
                                    if let Some(point) = transcript_point_at_mouse(&rows, area, app.inter_agent.event_scroll, mouse.column, mouse.row) {
                                        app.inter_agent.transcript_anchor = Some(point);
                                        app.inter_agent.transcript_focus = Some(point);
                                        app.inter_agent.transcript_dragging = true;
                                    } else {
                                        app.inter_agent.transcript_anchor = None;
                                        app.inter_agent.transcript_focus = None;
                                        app.inter_agent.transcript_dragging = false;
                                    }
                                    needs_full_redraw = true;
                                }
                            }
                            MouseEventKind::Down(MouseButton::Right) | MouseEventKind::Up(MouseButton::Right) => {
                                let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                                if let (Some(room), Some(area)) = (rooms.get(app.inter_agent.selected), inter_agent_stream_area(&app, outer_w, outer_h)) {
                                    let rows = conversation_transcript_rows(room, area.width as usize);
                                    if let Some(bounds) = transcript_selection_bounds(&app) {
                                        let text = transcript_selection_text(&rows, bounds);
                                        if !text.is_empty() && copy_to_clipboard(&text) {
                                            needs_full_redraw = true;
                                        }
                                    }
                                }
                            }
                            MouseEventKind::Drag(MouseButton::Left) => {
                                if app.inter_agent.transcript_dragging {
                                    let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                                    if let (Some(room), Some(area)) = (rooms.get(app.inter_agent.selected), inter_agent_stream_area(&app, outer_w, outer_h)) {
                                        let rows = conversation_transcript_rows(room, area.width as usize);
                                        let max_scroll = transcript_max_scroll(&rows, area);
                                        if mouse.row < area.y {
                                            app.inter_agent.event_scroll = (app.inter_agent.event_scroll + 1).min(max_scroll);
                                        } else if mouse.row >= area.y.saturating_add(area.height) {
                                            app.inter_agent.event_scroll = app.inter_agent.event_scroll.saturating_sub(1);
                                        }
                                        if let Some(point) = transcript_point_at_mouse(&rows, area, app.inter_agent.event_scroll, mouse.column, mouse.row) {
                                            app.inter_agent.transcript_focus = Some(point);
                                            needs_full_redraw = true;
                                        }
                                    }
                                }
                            }
                            MouseEventKind::Up(MouseButton::Left) => {
                                if app.inter_agent.transcript_dragging {
                                    app.inter_agent.transcript_dragging = false;
                                    let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                                    if let (Some(room), Some(area)) = (rooms.get(app.inter_agent.selected), inter_agent_stream_area(&app, outer_w, outer_h)) {
                                        let rows = conversation_transcript_rows(room, area.width as usize);
                                        if let Some(point) = transcript_point_at_mouse(&rows, area, app.inter_agent.event_scroll, mouse.column, mouse.row) {
                                            app.inter_agent.transcript_focus = Some(point);
                                        }
                                    }
                                    needs_full_redraw = true;
                                }
                            }
                            _ => {}
                        }
                    }
                }
                Event::Resize(w, h) => {
                    outer_w = w;
                    outer_h = h;
                    session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                    needs_full_redraw = true;
                }
                _ => {}
            }
        }

        let before = app.sessions.panes.len();
        app.sessions.panes.retain(|c| !c.is_eof());
        if app.sessions.panes.len() != before {
            if app.sessions.focused >= app.sessions.panes.len() {
                app.sessions.focused = app.sessions.panes.len().saturating_sub(1);
            }
            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
            needs_full_redraw = true;
        }
    }

    stdout.queue(cursor::Show)?;
    stdout.queue(DisableMouseCapture)?;
    stdout.queue(DisableBracketedPaste)?;
    stdout.flush()?;
    ct::disable_raw_mode()?;
    println!("charon-tui exited.");
    Ok(())
}
