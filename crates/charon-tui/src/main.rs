/// charon-tui — Rust TUI for the Charon agent operating system.
///
/// Views:
/// - F1 Chat: streaming conversation with tool use, selection, and context panel
/// - F2 Dashboard: agent status overview
/// - F3 Sessions: live VTE session grid with embedded terminal emulators
/// - F4 Inter-agent: conversation rooms and team coordination

mod app;
mod backend;
mod chat;
mod chat_view;
pub mod clipboard;
mod f1_mono;
mod grid;
mod native_session;
mod parser;
mod render;
mod screen;
mod session;
mod terminal;

use std::io::{self, Write};
use std::time::{Duration, Instant};

use app::{App, SessionsSection, TextPoint, View};
use crossterm::{
    cursor,
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEventKind,
    },
    style::{self},
    terminal::{self as ct, EnterAlternateScreen, LeaveAlternateScreen},
    QueueableCommand,
};

use backend::discover_sessions;
use chat::{ChatViewMode, LaunchOptions};
use f1_mono::F1MonoCache;
use grid::compute_grid;
use native_session::{NativeCommand, NativeSessionServer};
use parser::AnsiParser;
use render::Rect;
use serde_json::Value;
use session::{BackendType, SessionCell};
use terminal::TerminalState;

#[derive(Clone, Debug)]
enum LaunchMode {
    AutoDiscover,
    SpawnCommand(Vec<String>),
    AttachSession(String),
    ListSessions,
}

#[derive(Clone, Debug)]
struct CliOptions {
    launch_mode: LaunchMode,
    provider: Option<String>,
    resume: Option<String>,
    agent: Option<String>,
}

use clipboard::{copy_to_clipboard, read_from_clipboard};

fn parse_args() -> CliOptions {
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
    let next_id = 0u64;

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

fn payload_automations(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("automations"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
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
        View::Chat => match app.chat.view_mode {
            ChatViewMode::Transcript => "Chat/Transcript",
            ChatViewMode::Workspace => "Chat/Workspace",
        },
        View::Dashboard => "Dashboard",
        View::Sessions => "Sessions",
        View::InterAgent => "Groups",
    };

    let extra = match app.active_view {
        View::Chat => match app.chat.view_mode {
            ChatViewMode::Transcript => {
                if app.chat.app_mouse_mode {
                    " │ F5:workspace │ F6:mouse terminal"
                } else {
                    " │ F5:workspace │ F6:mouse app"
                }
            }
            ChatViewMode::Workspace => {
                if app.chat.app_mouse_mode {
                    " │ F5:transcript │ F6:mouse terminal"
                } else {
                    " │ F5:transcript │ F6:mouse app"
                }
            }
        },
        View::Sessions => {
            if app.sessions.terminal_mode {
                " │ terminal mode (Ctrl+] / Ctrl+G / F4)"
            } else if app.sessions.app_mouse_mode {
                " │ grid mode (Enter to interact) │ F6:mouse terminal"
            } else {
                " │ grid mode (native select/copy) │ F6:mouse app"
            }
        }
        View::InterAgent => {
            if app.inter_agent.app_mouse_mode {
                " │ F6:mouse terminal"
            } else {
                " │ F6:mouse app"
            }
        }
        View::Dashboard => "",
    };

    let header = format!(
        " CHARON │ {} │ F1:chat │ F2:dash │ F3:sessions │ F4:groups │ Ctrl+Q:quit{} ",
        view, extra
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
            let row_name = match app.dashboard.focus_row {
                0 => "agents",
                1 => "projects",
                2 => "automations",
                _ => "dashboard",
            };
            format!(
                " Dashboard │ agents:{} │ projects:{} │ automations:{} │ focus:{}:{} ",
                payload_agents(payload).len(),
                payload_projects(payload).len(),
                payload_automations(payload).len(),
                row_name,
                app.dashboard.focus_col + 1,
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
            if let Some((notice, _ok)) = app.inter_agent.clipboard_notice_text() {
                format!(" Groups │ rooms:{} │ {} ", rooms.len(), notice)
            } else {
                format!(" Groups │ rooms:{} ", rooms.len())
            }
        }
    };
    let visible: String = line.chars().take(w as usize).collect();
    let pad = (w as usize).saturating_sub(visible.chars().count());
    write!(stdout, "{}{}", visible, " ".repeat(pad))?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn draw_header_buf(buf: &mut screen::ScreenBuf, app: &App, w: u16) {
    let fg = style::Color::Rgb { r: 167, g: 139, b: 250 };
    let view = match app.active_view {
        View::Chat => match app.chat.view_mode {
            ChatViewMode::Transcript => "Chat/Transcript",
            ChatViewMode::Workspace => "Chat/Workspace",
        },
        View::Dashboard => "Dashboard",
        View::Sessions => "Sessions",
        View::InterAgent => "Groups",
    };
    let header = format!(
        " CHARON │ {} │ F1:chat │ F2:dash │ F3:sessions │ F4:groups │ Ctrl+Q:quit ",
        view
    );
    buf.put_str(0, 0, &header, fg, style::Color::Reset, false);
    let used = header.chars().count() as u16;
    buf.fill(0, used, w, ' ', fg, style::Color::Reset);
}

fn draw_footer_buf(buf: &mut screen::ScreenBuf, _app: &App, w: u16, h: u16) {
    let y = h.saturating_sub(1);
    buf.fill(y, 0, w, ' ', style::Color::Reset, style::Color::Reset);
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
    let snapshot_view = if app.active_view == View::Sessions || matches!(chat_view::chat_layout_variant(w, h), chat_view::ChatLayoutVariant::Tiny | chat_view::ChatLayoutVariant::Mid) {
        View::Chat
    } else {
        app.active_view
    };
    let session_rects = if snapshot_view == View::Sessions {
        session_grid_rects(app, w, h)
    } else {
        Vec::new()
    };
    let tiny_snapshot = matches!(chat_view::chat_layout_variant(w, h), chat_view::ChatLayoutVariant::Tiny);
    let saved_view = app.active_view;
    app.active_view = snapshot_view;
    let _ = out.queue(cursor::Hide);
    let _ = out.queue(ct::Clear(ct::ClearType::All));
    if !tiny_snapshot {
        let _ = draw_header(&mut out, app, w);
    }
    let _ = match snapshot_view {
        View::Chat => {
            let mut cache = chat_view::ChatVisualCache::default();
            chat_view::ensure_chat_visual_cache(app, w, h, &mut cache, true);
            chat_view::draw_chat(&mut out, app, w, h, true, &cache.lines)
        },
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

fn copy_inter_agent_selection(app: &mut App, room: &Value, area: Rect) -> bool {
    let Some(bounds) = transcript_selection_bounds(app) else {
        app.inter_agent.set_clipboard_notice("Nothing selected", false);
        return false;
    };
    let rows = conversation_transcript_rows(room, area.width as usize);
    let text = transcript_selection_text(&rows, bounds);
    if text.is_empty() {
        app.inter_agent.set_clipboard_notice("Nothing selected", false);
        return false;
    }
    match copy_to_clipboard(&text) {
        Ok(path) => {
            app.inter_agent.set_clipboard_notice(format!("Copied via {}", path), true);
            true
        }
        Err(err) => {
            app.inter_agent.set_clipboard_notice(err, false);
            false
        }
    }
}

fn dashboard_sparkline(points: &[u64]) -> String {
    let glyphs = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
    let max = points.iter().copied().max().unwrap_or(0);
    if max == 0 {
        return "▁".repeat(points.len().max(1));
    }
    points.iter().map(|v| {
        let idx = ((*v as f64 / max as f64) * (glyphs.len() as f64 - 1.0)).round() as usize;
        glyphs[idx.min(glyphs.len() - 1)]
    }).collect()
}

fn draw_dashboard_panel<W: Write>(stdout: &mut W, area: Rect, title: &str, lines: &[String], focused: bool) -> io::Result<()> {
    render::render_border(stdout, area, title, focused)?;
    let max_lines = area.height as usize;
    for (i, line) in lines.iter().take(max_lines).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}", visible)?;
    }
    Ok(())
}

fn rect_rows(area: Rect, rows: usize) -> Vec<Rect> {
    let mut out = Vec::new();
    let base_h = area.height / rows as u16;
    let extra = area.height % rows as u16;
    let mut y = area.y;
    for idx in 0..rows {
        let h = base_h + if idx < extra as usize { 1 } else { 0 };
        out.push(Rect { x: area.x, y, width: area.width, height: h });
        y += h;
    }
    out
}

fn rect_cols(area: Rect, widths: [u16; 3]) -> [Rect; 3] {
    let total = widths[0] + widths[1] + widths[2];
    let w1 = area.width.saturating_mul(widths[0]) / total.max(1);
    let w2 = area.width.saturating_mul(widths[1]) / total.max(1);
    let used = w1 + w2;
    let w3 = area.width.saturating_sub(used);
    [
        Rect { x: area.x, y: area.y, width: w1.saturating_sub(1), height: area.height.saturating_sub(1) },
        Rect { x: area.x + w1, y: area.y, width: w2.saturating_sub(1), height: area.height.saturating_sub(1) },
        Rect { x: area.x + used, y: area.y, width: w3.saturating_sub(1), height: area.height.saturating_sub(1) },
    ]
}

fn flatten_goal_tree(node: &Value, depth: usize, out: &mut Vec<String>) {
    let title = node.get("title").and_then(|v| v.as_str()).unwrap_or("goal");
    let status = node.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let marker = if matches!(status, "completed") { "[x]" } else if matches!(status, "active" | "executing" | "planning" | "verifying") { "[>]" } else { "[ ]" };
    out.push(format!("{}{} {}", "  ".repeat(depth), marker, title));
    if let Some(children) = node.get("children").and_then(|v| v.as_array()) {
        for child in children {
            flatten_goal_tree(child, depth + 1, out);
        }
    }
}

fn draw_dashboard<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    let outer = Rect { x: 1, y: 2, width: w.saturating_sub(2), height: h.saturating_sub(4) };
    let rows = rect_rows(outer, 3);
    let payload = app.chat.refresh_payload.as_ref();
    let agents = payload_agents(payload);
    let projects = payload_projects(payload);
    let automations = payload_automations(payload);

    let agent_idx = app.dashboard.agent_index.min(agents.len().saturating_sub(1));
    let project_idx = app.dashboard.project_index.min(projects.len().saturating_sub(1));
    let automation_idx = app.dashboard.automation_index.min(automations.len().saturating_sub(1));

    // Row 1: Agents
    {
        let cols = rect_cols(rows[0], [28, 38, 34]);
        let mut list = vec![format!("Provider/model: {}", app.chat.provider_model())];
        for (i, agent) in agents.iter().enumerate() {
            let prefix = if i == agent_idx { ">" } else { " " };
            let name = agent.get("name").and_then(|v| v.as_str()).unwrap_or("agent");
            let status = agent.get("status").and_then(|v| v.as_str()).unwrap_or("idle");
            let role = agent.get("role").and_then(|v| v.as_str()).unwrap_or("");
            list.push(format!("{} {} [{}] {}", prefix, name, status, role));
        }
        if agents.is_empty() { list.push("No agents yet.".to_string()); }

        let mut detail = Vec::new();
        let mut recent = Vec::new();
        if let Some(agent) = agents.get(agent_idx) {
            detail.push(format!("Name: {}", agent.get("name").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("ID: {}", agent.get("id").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Role: {}", agent.get("role").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Status: {}", agent.get("status").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Mode: {}", agent.get("mode").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Project: {}", agent.get("project").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Parent: {}", agent.get("parent_agent_id").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last active: {}", agent.get("last_active").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Goal: {}", agent.get("goal").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Summary: {}", agent.get("last_summary").and_then(|v| v.as_str()).unwrap_or("")));
            recent.push("Recent outcomes".to_string());
            if let Some(ledger) = agent.get("ledger").and_then(|v| v.as_array()) {
                for item in ledger.iter().take(6) {
                    recent.push(format!("- {} {}", item.get("status").and_then(|v| v.as_str()).unwrap_or(""), item.get("task_id").and_then(|v| v.as_str()).unwrap_or("")));
                }
            }
            if let Some(actions) = agent.get("recent_actions").and_then(|v| v.as_array()) {
                for item in actions.iter().take(4) {
                    if let Some(s) = item.as_str() { recent.push(format!("- {}", s)); }
                }
            }
        } else {
            detail.push("No agent selected.".to_string());
            recent.push("No recent outcomes.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "agents list", &list, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "agent details", &detail, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "agent outcomes", &recent, app.dashboard.focus_row == 0 && app.dashboard.focus_col == 2)?;
    }

    // Row 2: Projects
    {
        let cols = rect_cols(rows[1], [24, 42, 34]);
        let mut list = Vec::new();
        for (i, project) in projects.iter().enumerate() {
            let prefix = if i == project_idx { ">" } else { " " };
            let name = project.get("name").and_then(|v| v.as_str()).unwrap_or("project");
            let active = if project.get("active").and_then(|v| v.as_bool()).unwrap_or(false) { "active" } else { "idle" };
            let agents_count = project.get("agent_details").and_then(|v| v.as_array()).map(|v| v.len()).unwrap_or(0);
            list.push(format!("{} {} [{}] {}a", prefix, name, active, agents_count));
        }
        if projects.is_empty() { list.push("No projects yet.".to_string()); }

        let mut detail = Vec::new();
        let mut goals = Vec::new();
        if let Some(project) = projects.get(project_idx) {
            let usage = project.get("usage").unwrap_or(&Value::Null);
            let points: Vec<u64> = project.get("activity_points").and_then(|v| v.as_array()).map(|arr| arr.iter().filter_map(|v| v.as_u64()).collect()).unwrap_or_default();
            detail.push(format!("Name: {}", project.get("name").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Path: {}", project.get("path").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Active: {}", project.get("active").and_then(|v| v.as_bool()).unwrap_or(false)));
            detail.push(format!("Agents: {}", project.get("agent_details").and_then(|v| v.as_array()).map(|v| v.len()).unwrap_or(0)));
            detail.push(format!("Tokens: {}", usage.get("total_tokens").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Cost USD: {:.4}", usage.get("estimated_cost_usd").and_then(|v| v.as_f64()).unwrap_or(0.0)));
            detail.push(format!("Hours est: {:.2}", usage.get("hours_spent_estimate").and_then(|v| v.as_f64()).unwrap_or(0.0)));
            detail.push(format!("Libris ops: {}", usage.get("libris_operations").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Dev ops: {}", usage.get("devop_operations").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Activity: {}", dashboard_sparkline(&points)));
            goals.push("Goal tree".to_string());
            if let Some(tree) = project.get("goal_tree").and_then(|v| v.as_array()) {
                for node in tree.iter().take(12) {
                    flatten_goal_tree(node, 0, &mut goals);
                }
            }
            if goals.len() == 1 {
                goals.push("No goals recorded yet.".to_string());
            }
        } else {
            detail.push("No project selected.".to_string());
            goals.push("No goals recorded yet.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "projects list", &list, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "project details", &detail, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "project goals", &goals, app.dashboard.focus_row == 1 && app.dashboard.focus_col == 2)?;
    }

    // Row 3: Automations
    {
        let cols = rect_cols(rows[2], [24, 40, 36]);
        let mut list = Vec::new();
        for (i, automation) in automations.iter().enumerate() {
            let prefix = if i == automation_idx { ">" } else { " " };
            let title = automation.get("title").and_then(|v| v.as_str()).unwrap_or("automation");
            let status = automation.get("status").and_then(|v| v.as_str()).unwrap_or("active");
            let health = automation.get("health").and_then(|v| v.as_str()).unwrap_or("unknown");
            let mode = automation.get("mode").and_then(|v| v.as_str()).unwrap_or("");
            list.push(format!("{} {} [{}:{}]", prefix, title, status, if mode.is_empty() { health } else { mode }));
        }
        if automations.is_empty() { list.push("No automations yet.".to_string()); }

        let mut detail = Vec::new();
        let mut runs = Vec::new();
        if let Some(automation) = automations.get(automation_idx) {
            let schedule = automation.get("schedule").unwrap_or(&Value::Null);
            let sched_desc = if schedule.get("type").and_then(|v| v.as_str()) == Some("cron") {
                format!("cron {}", schedule.get("cron").and_then(|v| v.as_str()).unwrap_or(""))
            } else if automation.get("mode").and_then(|v| v.as_str()) == Some("continuous") {
                format!("continuous/{}s", schedule.get("poll_seconds").and_then(|v| v.as_u64()).unwrap_or(60))
            } else {
                format!("every {}s", schedule.get("interval_seconds").and_then(|v| v.as_u64()).unwrap_or(0))
            };
            detail.push(format!("Title: {}", automation.get("title").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("ID: {}", automation.get("automation_id").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Kind: {}", automation.get("kind").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Mode: {}", automation.get("mode").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Schedule: {}", sched_desc));
            detail.push(format!("Status: {}", automation.get("status").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Health: {}", automation.get("health").and_then(|v| v.as_str()).unwrap_or("")));
            detail.push(format!("Next run: {}", automation.get("next_run_at").and_then(|v| v.as_str()).unwrap_or("continuous")));
            detail.push(format!("Heartbeat: {}", automation.get("last_heartbeat_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last success: {}", automation.get("last_success_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Last failure: {}", automation.get("last_failure_at").and_then(|v| v.as_str()).unwrap_or("-")));
            detail.push(format!("Consecutive failures: {}", automation.get("consecutive_failures").and_then(|v| v.as_u64()).unwrap_or(0)));
            detail.push(format!("Result: {}", automation.get("last_result_summary").and_then(|v| v.as_str()).unwrap_or("")));
            runs.push("Recent runs".to_string());
            if let Some(items) = automation.get("runs_tail").and_then(|v| v.as_array()) {
                for item in items.iter().rev().take(8) {
                    let ts = item.get("ts").and_then(|v| v.as_str()).unwrap_or("");
                    let ok = item.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                    let summary = item.get("summary").and_then(|v| v.as_str()).unwrap_or("");
                    runs.push(format!("- {} [{}] {}", ts, if ok { "ok" } else { "fail" }, summary));
                    if let Some(details) = item.get("details") {
                        if let Some(path) = details.get("screenshot").and_then(|v| v.as_str()) {
                            runs.push(format!("  screenshot: {}", path));
                        }
                    }
                }
            }
            if runs.len() == 1 { runs.push("No runs yet.".to_string()); }
        } else {
            detail.push("No automation selected.".to_string());
            runs.push("No runs yet.".to_string());
        }
        draw_dashboard_panel(stdout, cols[0], "automations list", &list, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 0)?;
        draw_dashboard_panel(stdout, cols[1], "automation details", &detail, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 1)?;
        draw_dashboard_panel(stdout, cols[2], "automation runs", &runs, app.dashboard.focus_row == 2 && app.dashboard.focus_col == 2)?;
    }

    Ok(())
}

fn inter_agent_event_lines(room: &Value, event_scroll: usize, max_lines: usize, app_mouse_mode: bool) -> Vec<String> {
    let mut lines = Vec::new();
    let title = room.get("title").and_then(|v| v.as_str()).unwrap_or("untitled");
    let kind = room.get("kind").and_then(|v| v.as_str()).unwrap_or("group");
    let status = room.get("status").and_then(|v| v.as_str()).unwrap_or("active");
    let active_speaker = room.get("active_speaker").and_then(|v| v.as_str()).unwrap_or("");
    let active_state = room.get("active_state").and_then(|v| v.as_str()).unwrap_or("");
    lines.push(format!("{}  [{}]", title, kind));
    let mut status_bits = vec![format!("status: {}", status)];
    if !active_speaker.is_empty() {
        status_bits.push(format!("active: {}", active_speaker));
    }
    if !active_state.is_empty() {
        status_bits.push(format!("state: {}", active_state));
    }
    lines.push(status_bits.join("  • "));
    if let Some(parts) = room.get("participants").and_then(|v| v.as_array()) {
        let participants = parts.iter().filter_map(|p| {
            let role = p.get("role").and_then(|v| v.as_str()).unwrap_or("");
            let name = p.get("name").and_then(|v| v.as_str()).unwrap_or("");
            if name.is_empty() { None } else if role.is_empty() { Some(name.to_string()) } else { Some(format!("{} ({})", name, role)) }
        }).collect::<Vec<_>>().join(", ");
        if !participants.is_empty() { lines.push(format!("participants: {}", participants)); }
    }
    lines.push(format!(
        "{}  •  d/Delete: remove room",
        if app_mouse_mode {
            "Wheel/PgUp/PgDn: scroll  •  drag: select  •  right-click/Ctrl+C: copy  •  F6:mouse app"
        } else {
            "Terminal selection/right-click active  •  Ctrl+C: copy selection  •  F6:mouse terminal"
        }
    ));
    lines.push(String::new());
    if let Some(events) = room.get("events").and_then(|v| v.as_array()) {
        let mut filtered: Vec<&Value> = Vec::new();
        let mut last_tool_key = String::new();
        for event in events {
            let typ = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
            if typ == "participant_tool_progress" {
                let role = event.get("speaker_role").and_then(|v| v.as_str()).unwrap_or("");
                let turn = event.get("turn").and_then(|v| v.as_u64()).unwrap_or(0);
                let tool = event.get("tool_name").and_then(|v| v.as_str()).unwrap_or("");
                let phase = event.get("tool_phase").and_then(|v| v.as_str()).unwrap_or("");
                let key = format!("{}:{}:{}:{}", role, turn, tool, phase);
                if key == last_tool_key {
                    continue;
                }
                last_tool_key = key;
            } else {
                last_tool_key.clear();
            }
            filtered.push(event);
        }
        let start = filtered.len().saturating_sub(max_lines + event_scroll);
        let end = filtered.len().saturating_sub(event_scroll);
        for event in &filtered[start.min(filtered.len())..end.min(filtered.len())] {
            let ts = event.get("ts").or_else(|| event.get("timestamp")).and_then(|v| v.as_str()).unwrap_or("");
            let typ = event.get("type").and_then(|v| v.as_str()).unwrap_or("event");
            let role = event.get("speaker_role").and_then(|v| v.as_str()).unwrap_or("");
            let label = match typ {
                "conversation_turn_started" => format!("▶ {} turn", if role.is_empty() { "agent" } else { role }),
                "participant_output" => format!("💬 {}", if role.is_empty() { "agent" } else { role }),
                "participant_tool_progress" => format!("🛠 {}", if role.is_empty() { "agent" } else { role }),
                "turn_timeout" => format!("⏱ {} timeout", if role.is_empty() { "agent" } else { role }),
                "turn_nudged" => format!("↪ {} nudged", if role.is_empty() { "agent" } else { role }),
                "conversation_started" => "✓ started".to_string(),
                "conversation_stopped" => "■ stopped".to_string(),
                other => other.to_string(),
            };
            let mut msg = String::new();
            if typ == "participant_tool_progress" {
                let tool = event.get("tool_name").and_then(|v| v.as_str()).unwrap_or("");
                let phase = event.get("tool_phase").and_then(|v| v.as_str()).unwrap_or("");
                if !tool.is_empty() && !phase.is_empty() {
                    msg.push_str(&format!("{} {}", tool, phase));
                } else if !tool.is_empty() {
                    msg.push_str(tool);
                } else if !phase.is_empty() {
                    msg.push_str(phase);
                }
            }
            if msg.is_empty() {
                if let Some(summary) = event.get("summary").and_then(|v| v.as_str()) {
                    msg.push_str(summary);
                } else if let Some(topic) = event.get("topic").and_then(|v| v.as_str()) {
                msg.push_str(topic);
                } else if let Some(title) = event.get("title").and_then(|v| v.as_str()) {
                    msg.push_str(title);
                } else if let Some(session) = event.get("session").and_then(|v| v.as_str()) {
                    msg.push_str(session);
                }
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

fn draw_conversation_stream<W: Write>(stdout: &mut W, room: &Value, area: Rect, event_scroll: usize, selection: Option<(TextPoint, TextPoint)>, app_mouse_mode: bool) -> io::Result<()> {
    let rows = conversation_transcript_rows(room, area.width as usize);
    if rows.is_empty() {
        let lines = inter_agent_event_lines(room, event_scroll, area.height.saturating_sub(1) as usize, app_mouse_mode);
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
#[allow(dead_code)] // layout anchors; left/right reserved for the split view
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

    let graph_nodes: Vec<LibrisGraphNode> = nodes.iter().map(|n| LibrisGraphNode {
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

    let mut node_anchors: std::collections::HashMap<String, GraphAnchors> = std::collections::HashMap::new();
    let mut coord_bottom_y: u16 = area.y;

    // ── Coordinator box ────────────────────────────────────────────────
    if let Some(coord) = coordinator {
        let label = format!("coordinator • {}", coord.phase);
        let content_w = label.len().max(coord.name.len()).max(20) + 2;
        let box_w = (content_w as u16).min(area.width.saturating_sub(4));
        let box_h = 2u16; // label + live_line
        let box_x = area.x + area.width.saturating_sub(box_w) / 2;
        let box_y = area.y;
        let coord_idx = graph_nodes.iter().position(|n| n.agent_id == coord.agent_id).unwrap_or(usize::MAX);
        let node_rect = Rect { x: box_x, y: box_y, width: box_w, height: box_h };
        render::render_border_colored(stdout, node_rect, &coord.name, libris_role_color("coordinator", selected_node == coord_idx))?;
        let live_trunc: String = coord.live_line.chars().take(box_w as usize).collect();
        draw_box_text(stdout, node_rect, &[label, live_trunc], style::Color::Rgb { r: 226, g: 232, b: 240 })?;
        node_anchors.insert(coord.agent_id.clone(), graph_anchors(node_rect));
        coord_bottom_y = box_y + box_h + 2; // +2 for border bottom + gap
    }

    // ── Topic grid layout ──────────────────────────────────────────────
    let topic_count = topics.len();
    if topic_count == 0 {
        stdout.queue(cursor::MoveTo(area.x + 2, area.y + 3))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(stdout, "Waiting for Libris topic clusters\u{2026}")?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        return Ok(graph_nodes);
    }

    // Decide grid columns: 1 col if narrow or single topic, 2 cols otherwise
    let grid_cols: u16 = if topic_count <= 1 || area.width < 70 { 1 } else { 2 };
    let grid_rows = ((topic_count as u16) + grid_cols - 1) / grid_cols;
    let col_gap = 3u16;
    let row_gap = 2u16;
    let col_w = if grid_cols > 1 {
        (area.width.saturating_sub(col_gap * (grid_cols - 1))) / grid_cols
    } else {
        area.width
    };

    // Height per topic cluster: distribute remaining space evenly
    let available_h = area.height.saturating_sub(coord_bottom_y - area.y + row_gap);
    let cluster_h = (available_h.saturating_sub(row_gap * grid_rows.saturating_sub(1))) / grid_rows;
    let cluster_h = cluster_h.max(8); // minimum usable height

    // Trunk line from coordinator down to topic row
    let trunk_y = coord_bottom_y;
    let topics_start_y = trunk_y + 2;

    // Draw coordinator → topics trunk
    if graph_nodes.iter().any(|n| n.role == "coordinator") {
        let coord_cx = area.x + area.width / 2;
        let trunk_color = style::Color::Rgb { r: 70, g: 60, b: 100 };

        // Vertical from coordinator down
        draw_vline(stdout, coord_cx, coord_bottom_y.saturating_sub(1), trunk_y, trunk_color)?;

        if topic_count > 1 {
            // Horizontal spine across topics
            let first_cx = area.x + col_w / 2;
            let last_col = (topic_count as u16 - 1) % grid_cols;
            let last_cx = area.x + last_col * (col_w + col_gap) + col_w / 2;
            draw_hline(stdout, first_cx, last_cx, trunk_y, trunk_color)?;

            // Vertical drops to each topic in the first row
            let first_row_count = topic_count.min(grid_cols as usize);
            for ci in 0..first_row_count {
                let cx = area.x + (ci as u16) * (col_w + col_gap) + col_w / 2;
                draw_vline(stdout, cx, trunk_y, topics_start_y.saturating_sub(1), trunk_color)?;
            }
        } else {
            draw_vline(stdout, coord_cx, trunk_y, topics_start_y.saturating_sub(1), trunk_color)?;
        }
    }

    // ── Render each topic cluster ──────────────────────────────────────
    for (ti, topic) in topics.iter().enumerate() {
        let col = (ti as u16) % grid_cols;
        let row = (ti as u16) / grid_cols;
        let cx = area.x + col * (col_w + col_gap);
        let cy = topics_start_y + row * (cluster_h + row_gap);

        // Cluster border
        let inner_w = col_w.saturating_sub(2);
        let inner_h = cluster_h.saturating_sub(2);
        let cluster_rect = Rect { x: cx + 1, y: cy + 1, width: inner_w, height: inner_h };
        let topic_title = topic.get("title").and_then(|v| v.as_str()).unwrap_or("topic");
        let topic_status = topic.get("status").and_then(|v| v.as_str()).unwrap_or("");
        let title_display = if topic_status.is_empty() {
            topic_title.to_string()
        } else {
            format!("{} ({})", topic_title, topic_status)
        };
        let title_trunc: String = title_display.chars().take(inner_w.saturating_sub(4) as usize).collect();
        render::render_border_colored(stdout, cluster_rect, &title_trunc, style::Color::Rgb { r: 80, g: 70, b: 120 })?;

        let slug = topic.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("");
        let researcher = graph_nodes.iter().find(|n| n.role == "researcher" && n.topic_slug == slug).cloned();
        let judge = graph_nodes.iter().find(|n| n.role == "judge" && n.topic_slug == slug).cloned();
        let shades: Vec<&LibrisGraphNode> = graph_nodes.iter().filter(|n| n.role == "shade" && n.topic_slug == slug).collect();

        // ── Researcher + Judge: side by side within cluster ────────
        let agent_y = cluster_rect.y;
        let half_w = inner_w.saturating_sub(5) / 2; // leave 3-char gap + 2 padding
        let agent_h = 2u16; // role•phase + live_line

        if let Some(r) = researcher.clone() {
            let is_sel = graph_nodes.iter().position(|n| n.agent_id == r.agent_id).unwrap_or(usize::MAX) == selected_node;
            let rect = Rect { x: cluster_rect.x, y: agent_y, width: half_w, height: agent_h };
            render::render_border_colored(stdout, rect, &r.name, libris_role_color("researcher", is_sel))?;
            let phase_line = format!("researcher • {}", r.phase);
            let phase_trunc: String = phase_line.chars().take(half_w as usize).collect();
            let live_trunc: String = r.live_line.chars().take(half_w as usize).collect();
            draw_box_text(stdout, rect, &[phase_trunc, live_trunc], style::Color::Rgb { r: 226, g: 232, b: 240 })?;
            node_anchors.insert(r.agent_id.clone(), graph_anchors(rect));
        }

        if let Some(j) = judge.clone() {
            let is_sel = graph_nodes.iter().position(|n| n.agent_id == j.agent_id).unwrap_or(usize::MAX) == selected_node;
            let jx = cluster_rect.x + half_w + 5;
            let rect = Rect { x: jx, y: agent_y, width: half_w, height: agent_h };
            render::render_border_colored(stdout, rect, &j.name, libris_role_color("judge", is_sel))?;
            let phase_line = format!("judge • {}", j.phase);
            let phase_trunc: String = phase_line.chars().take(half_w as usize).collect();
            let live_trunc: String = j.live_line.chars().take(half_w as usize).collect();
            draw_box_text(stdout, rect, &[phase_trunc, live_trunc], style::Color::Rgb { r: 226, g: 232, b: 240 })?;
            node_anchors.insert(j.agent_id.clone(), graph_anchors(rect));
        }

        // ── Researcher <=> Judge edge indicator ────────────────────
        let rj_active = edges.iter().any(|e| {
            e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug
                && e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)
                && ((e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "judge")
                    || (e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "judge" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher"))
        });
        let rj_strength = edges.iter().filter_map(|e| {
            if e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug
                && ((e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "judge")
                    || (e.get("from_role").and_then(|v| v.as_str()).unwrap_or("") == "judge" && e.get("to_role").and_then(|v| v.as_str()).unwrap_or("") == "researcher")) {
                e.get("activity_strength").and_then(|v| v.as_f64())
            } else { None }
        }).fold(0.0f64, f64::max);
        // Draw connecting arrow between researcher and judge boxes
        let arrow_y = agent_y + 1; // middle of the agent boxes
        let arrow_x1 = cluster_rect.x + half_w + 1;
        let _arrow_x2 = cluster_rect.x + half_w + 4;
        let arrow_color = if rj_active {
            style::Color::Rgb { r: 96, g: 165, b: 250 }
        } else {
            libris_edge_color(false, rj_strength)
        };
        stdout.queue(cursor::MoveTo(arrow_x1, arrow_y))?;
        stdout.queue(style::SetForegroundColor(arrow_color))?;
        if rj_active {
            write!(stdout, "<=>")?;
        } else {
            write!(stdout, "---")?;
        }
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;

        // ── Shade tray (collapsed summary) ─────────────────────────
        let shade_y = agent_y + agent_h + 2; // below agent boxes + border + gap
        let shade_active_count = shades.iter().filter(|s| {
            edges.iter().any(|e| {
                (e.get("from_agent_id").and_then(|v| v.as_str()).unwrap_or("") == s.agent_id
                    || e.get("to_agent_id").and_then(|v| v.as_str()).unwrap_or("") == s.agent_id)
                    && e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)
            })
        }).count();
        let shade_total = shades.len();

        if shade_total > 0 && shade_y < cluster_rect.y + inner_h {
            // Shade summary tray
            let tray_w = inner_w.saturating_sub(2);
            let tray_x = cluster_rect.x + 1;

            // Build shade name list for the summary
            let shade_names: Vec<&str> = shades.iter().map(|s| s.name.as_str()).collect();
            let names_joined = shade_names.join(", ");

            let header = format!("{} shade{} ({} active)",
                shade_total,
                if shade_total == 1 { "" } else { "s" },
                shade_active_count
            );
            let header_trunc: String = header.chars().take(tray_w as usize).collect();

            // Register shade nodes in anchors for edge drawing
            for shade in &shades {
                let shade_rect = Rect { x: tray_x, y: shade_y, width: tray_w, height: 1 };
                node_anchors.insert(shade.agent_id.clone(), graph_anchors(shade_rect));
            }

            let tray_color = if shade_active_count > 0 {
                style::Color::Rgb { r: 148, g: 163, b: 184 }
            } else {
                style::Color::DarkGrey
            };

            // Draw tray box
            let tray_h = 2u16.min(inner_h.saturating_sub(shade_y - cluster_rect.y));
            if tray_h >= 2 {
                let tray_rect = Rect { x: tray_x, y: shade_y, width: tray_w, height: tray_h };
                render::render_border_colored(stdout, tray_rect, &header_trunc, tray_color)?;
                let names_trunc: String = names_joined.chars().take(tray_w as usize).collect();
                draw_box_text(stdout, tray_rect, &[names_trunc], style::Color::Rgb { r: 120, g: 130, b: 150 })?;

                // Show individual shade phases if there's room
                let detail_y = shade_y + tray_h + 2;
                let remaining = (cluster_rect.y + inner_h).saturating_sub(detail_y);
                for (si, shade) in shades.iter().enumerate().take(remaining as usize) {
                    let is_sel = graph_nodes.iter().position(|n| n.agent_id == shade.agent_id).unwrap_or(usize::MAX) == selected_node;
                    let shade_line = format!("  {} {} • {}",
                        if is_sel { "▸" } else { "·" },
                        shade.name,
                        if shade.phase.is_empty() { &shade.status } else { &shade.phase }
                    );
                    let shade_trunc: String = shade_line.chars().take(tray_w as usize).collect();
                    stdout.queue(cursor::MoveTo(tray_x, detail_y + si as u16))?;
                    stdout.queue(style::SetForegroundColor(if is_sel {
                        style::Color::Rgb { r: 148, g: 163, b: 184 }
                    } else {
                        style::Color::DarkGrey
                    }))?;
                    write!(stdout, "{}", shade_trunc)?;
                    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
                    // Update anchor for this shade to its actual rendered position
                    let shade_rect = Rect { x: tray_x, y: detail_y + si as u16, width: tray_w, height: 1 };
                    node_anchors.insert(shade.agent_id.clone(), graph_anchors(shade_rect));
                }
            }
        }

        // ── Edge/activity summary at cluster bottom ────────────────
        let edge_summary = edges.iter().filter(|e| e.get("topic_slug").and_then(|v| v.as_str()).unwrap_or("") == slug);
        let active_count = edge_summary.clone().filter(|e| e.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false)).count();
        let total_edges = edge_summary.count();
        let status_line = format!("{} edges • {} active", total_edges, active_count);
        let status_y = cluster_rect.y + inner_h.saturating_sub(1);
        stdout.queue(cursor::MoveTo(cluster_rect.x + 1, status_y))?;
        stdout.queue(style::SetForegroundColor(if active_count > 0 {
            style::Color::Rgb { r: 34, g: 197, b: 94 }
        } else {
            style::Color::DarkGrey
        }))?;
        let status_trunc: String = status_line.chars().take(inner_w.saturating_sub(2) as usize).collect();
        write!(stdout, "{}", status_trunc)?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }

    // ── Draw communication edges ───────────────────────────────────────
    for edge in &edges {
        let src = edge.get("from_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        let dst = edge.get("to_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        let from_role = edge.get("from_role").and_then(|v| v.as_str()).unwrap_or("");
        let to_role = edge.get("to_role").and_then(|v| v.as_str()).unwrap_or("");
        let active_now = edge.get("active_now").and_then(|v| v.as_bool()).unwrap_or(false);
        let activity_strength = edge.get("activity_strength").and_then(|v| v.as_f64()).unwrap_or(0.15);

        // Skip researcher<=>judge edges (drawn inline as arrows)
        if (from_role == "researcher" && to_role == "judge") || (from_role == "judge" && to_role == "researcher") {
            continue;
        }

        let color = libris_edge_color(active_now, activity_strength);
        let Some(src_anchor) = node_anchors.get(src).copied() else { continue; };
        let Some(dst_anchor) = node_anchors.get(dst).copied() else { continue; };

        let (start, end, mid_y) = if from_role == "shade" && to_role == "researcher" {
            let m = dst_anchor.bottom.y + ((src_anchor.top.y.saturating_sub(dst_anchor.bottom.y)) / 2);
            (src_anchor.top, dst_anchor.bottom, m)
        } else if from_role == "researcher" && to_role == "shade" {
            let m = src_anchor.bottom.y + ((dst_anchor.top.y.saturating_sub(src_anchor.bottom.y)) / 2);
            (src_anchor.bottom, dst_anchor.top, m)
        } else if from_role == "coordinator" {
            (src_anchor.bottom, dst_anchor.top, topics_start_y.saturating_sub(1))
        } else if to_role == "coordinator" {
            (src_anchor.top, dst_anchor.bottom, topics_start_y.saturating_sub(1))
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
            BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => !meta.tmux.is_empty() && session_id == &meta.tmux,
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
            BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => visuals.get(session_id),
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

            let lines = inter_agent_event_lines(room, app.inter_agent.event_scroll, event_area.height.saturating_sub(1) as usize, app.inter_agent.app_mouse_mode);
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
            draw_conversation_stream(stdout, room, detail_area, app.inter_agent.event_scroll, transcript_selection_bounds(app), app.inter_agent.app_mouse_mode)?;
        }
    }
    draw_delete_room_modal(stdout, app, w, h)?;
    Ok(())
}

#[derive(Clone)]
#[allow(dead_code)] // mirrors agent JSON; not all fields read
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
    server_id: String,
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
            if !has_tmux && tmux.is_empty() && source != "live" && transport != "charon" && transport != "remote-boat" {
                continue;
            }
            let server_id = sess.get("server_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
            out.push(SessionAgentMeta { id, agent_id, name, project, specialization, last_summary, tmux, status, source, process_target, live_session_id, session_label, transport, socket, server_id });
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
                Some(SessionAgentMeta { id: id.clone(), agent_id: id, name: name.clone(), project, specialization, last_summary, tmux, status, source: "agent".to_string(), process_target: String::new(), live_session_id: String::new(), session_label: name, transport: String::new(), socket: String::new(), server_id: String::new() })
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
            BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => !m.tmux.is_empty() && m.tmux == *session_id,
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
                    BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => !meta.tmux.is_empty() && session_id == &meta.tmux,
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
        } else if meta.transport == "remote-boat" && !meta.server_id.is_empty() {
            // Remote agent — find server in fleet config and connect via SSH
            let fleet = backend::load_fleet_config();
            if let Some(server) = fleet.iter().find(|s| s.id == meta.server_id) {
                let session_id = if meta.tmux.is_empty() { &meta.name } else { &meta.tmux };
                SessionCell::attach_remote_boat(idx as u64, &composed_title, server, session_id, r.width.max(1), r.height.max(1))
            } else {
                continue;
            }
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
    let cli = parse_args();
    let mode = &cli.launch_mode;

    if matches!(mode, &LaunchMode::ListSessions) {
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
    let panes = build_initial_sessions(mode, outer_w, outer_h)?;
    let launch = LaunchOptions {
        provider: cli.provider.clone(),
        resume: cli.resume.clone(),
        agent: cli.agent.clone(),
    };
    let mut app = App::new(panes, launch)?;
    let native_session = NativeSessionServer::start(None).ok();
    app.sessions.backend_filter_pending = matches!(mode, &LaunchMode::AutoDiscover);
    let _ = ensure_native_self_pane(&mut app, native_session.as_ref(), outer_w, outer_h);
    let mut session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
    // (native transcript render state removed with the legacy renderer)

    clipboard::configure_tmux_clipboard();

    ct::enable_raw_mode()?;
    let mut stdout = io::stdout();
    stdout.queue(EnterAlternateScreen)?;
    stdout.queue(EnableBracketedPaste)?;
    let mut mouse_capture_enabled = false;
    if app.active_view == View::Chat || app.active_view == View::Sessions {
        stdout.queue(EnableMouseCapture)?;
        mouse_capture_enabled = true;
    }
    stdout.queue(cursor::Hide)?;
    stdout.queue(ct::Clear(ct::ClearType::All))?;
    stdout.queue(cursor::MoveTo(0, 0))?;
    stdout.flush()?;

    let mut last_render = Instant::now();
    let frame_duration = Duration::from_millis(16);
    let mut needs_full_redraw = true;
    let mut local_view_dirty = false;
    let mut cached_chat = F1MonoCache::default();
    let mut last_rowing_tick = Instant::now();
    let mut last_session_poll = Instant::now() - Duration::from_secs(1);
    let mut front_buf = screen::ScreenBuf::new(outer_w, outer_h);
    let mut back_buf = screen::ScreenBuf::new(outer_w, outer_h);
    let mut last_chat_msg_count: usize = 0;
    let mut last_cache_rebuild = Instant::now();
    let mut last_snapshot = Instant::now();
    let mut was_streaming = false;

    'main: loop {
        app.chat.clear_expired_notices();
        app.inter_agent.clear_expired_notices();
        let mut any_dirty = false;
        let pane_poll_interval = match app.active_view {
            View::Sessions if app.sessions.terminal_mode => Duration::from_millis(16),
            View::Sessions => Duration::from_millis(33),
            _ => Duration::from_millis(250),
        };
        if last_session_poll.elapsed() >= pane_poll_interval && app.active_view != View::Chat {
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
        if chat_dirty {
        }
        let native_input_dirty = if let Some(server) = &native_session {
            let commands = server.drain_commands();
            let dirty = !commands.is_empty();
            if dirty {
                apply_native_commands(&mut app, commands);
            }
            dirty
        } else {
            false
        };
        if native_input_dirty && app.active_view == View::Chat {
        }
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
        if app.active_view == View::Chat && chat_view::chat_rowing_active(&app) && now.duration_since(last_rowing_tick) >= Duration::from_millis(300) {
            local_view_dirty = true;
            last_rowing_tick = now;
        }
        let sessions_dirty = app.active_view == View::Sessions && (any_dirty || native_input_dirty || local_view_dirty);
        let dashboard_dirty = app.active_view == View::Dashboard && (chat_dirty || native_input_dirty || local_view_dirty);
        let chat_view_dirty = app.active_view == View::Chat && (chat_dirty || native_input_dirty || local_view_dirty);
        let inter_agent_dirty = app.active_view == View::InterAgent && (any_dirty || chat_dirty || native_input_dirty || local_view_dirty);
        let native_snapshot_dirty = native_input_dirty;

        if (needs_full_redraw || sessions_dirty || dashboard_dirty || chat_view_dirty || inter_agent_dirty || native_snapshot_dirty) && now.duration_since(last_render) >= frame_duration {
            let force_all = needs_full_redraw;

            if app.active_view == View::Chat {
                // Only force cache rebuild when messages actually changed.
                // Throttle streaming rebuilds to ~100ms to avoid 11-19ms rebuilds every frame.
                let msg_count = app.chat.messages.len();
                let msg_changed = msg_count != last_chat_msg_count;
                let stream_ended = was_streaming && !app.chat.streaming;
                let cache_age = now.duration_since(last_cache_rebuild);
                let streaming_update = app.chat.streaming && chat_dirty
                    && cache_age >= Duration::from_millis(200);
                let content_changed = msg_changed || streaming_update || stream_ended
                    || native_input_dirty || force_all;
                if msg_changed { last_chat_msg_count = msg_count; }
                if content_changed { last_cache_rebuild = now; }
                was_streaming = app.chat.streaming;
                f1_mono::ensure_cache(&app, outer_w, outer_h, &mut cached_chat, content_changed);

                back_buf.clear();
                draw_header_buf(&mut back_buf, &app, outer_w);
                f1_mono::draw(&mut back_buf, &app, outer_w, outer_h, &cached_chat);
                draw_footer_buf(&mut back_buf, &app, outer_w, outer_h);
                screen::flush(&back_buf, &front_buf, &mut stdout)?;
                std::mem::swap(&mut front_buf, &mut back_buf);
            } else {
                // Legacy direct rendering for other views
                stdout.queue(cursor::Hide)?;
                if needs_full_redraw {
                    stdout.queue(ct::Clear(ct::ClearType::All))?;
                }
                draw_header(&mut stdout, &app, outer_w)?;
                match app.active_view {
                    View::Chat => unreachable!(),
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
                stdout.flush()?;
            }
            needs_full_redraw = false;
            // Throttle native snapshot publishing — it rebuilds the entire visual
            // cache from scratch which is expensive (11-19ms+). Only publish
            // every 2 seconds, or immediately on force_all (view switch/resize).
            if force_all || ((chat_dirty || native_input_dirty) && now.duration_since(last_snapshot) >= Duration::from_secs(2)) {
                if let Some(server) = &native_session {
                    let self_sock = server.socket_path().to_string_lossy().to_string();
                    let (snap_w, snap_h) = server.requested_size().unwrap_or((outer_w, outer_h));
                    server.update_snapshot(build_native_session_snapshot(&mut app, snap_w.max(1), snap_h.max(1), Some(&self_sock)));
                }
                last_snapshot = now;
            }
            last_render = now;
            local_view_dirty = false;
        }

        let want_mouse_capture = (app.active_view == View::Chat && app.chat.app_mouse_mode)
            || (app.active_view == View::Sessions && !app.sessions.terminal_mode && app.sessions.app_mouse_mode)
            || (app.active_view == View::InterAgent && app.inter_agent.app_mouse_mode);
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
            View::Chat => Duration::from_millis(4),
            View::Sessions if app.sessions.terminal_mode => Duration::from_millis(4),
            View::Sessions => Duration::from_millis(16),
            _ => Duration::from_millis(33),
        };
        // Block up to event_poll_interval for first event, then drain all queued events.
        // This coalesces bursts (e.g. rapid scrolling) into a single render.
        if !event::poll(event_poll_interval)? { continue; }
        loop {
            if !event::poll(Duration::ZERO)? { break; }
            match event::read()? {
                Event::Key(key) => {
                    match key.code {
                        KeyCode::F(1) => {
                            app.active_view = View::Chat;
                            front_buf.clear();
                            stdout.queue(ct::Clear(ct::ClearType::All))?;
                            stdout.flush()?;
                            needs_full_redraw = true;
                            continue;
                        }
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
                        break 'main;
                    }

                    match app.active_view {
                        View::Chat => {
                            if key.code == KeyCode::F(5) {
                                app.chat.view_mode = match app.chat.view_mode {
                                    ChatViewMode::Transcript => ChatViewMode::Workspace,
                                    ChatViewMode::Workspace => ChatViewMode::Transcript,
                                };
                                match app.chat.view_mode {
                                    ChatViewMode::Transcript => {
                                        app.chat.info_pane_open = false;
                                        app.chat.app_mouse_mode = false;
                                        app.chat.selection_anchor = None;
                                        app.chat.selection_focus = None;
                                        app.chat.selection_dragging = false;
                                    }
                                    ChatViewMode::Workspace => {
                                        app.chat.info_pane_open = true;
                                        app.chat.app_mouse_mode = false;
                                        app.chat.selection_anchor = None;
                                        app.chat.selection_focus = None;
                                        app.chat.selection_dragging = false;
                                    }
                                }
                                needs_full_redraw = true;
                            } else if key.code == KeyCode::F(6) {
                                app.chat.app_mouse_mode = !app.chat.app_mouse_mode;
                                app.chat.selection_dragging = false;
                                app.chat.selection_anchor = None;
                                app.chat.selection_focus = None;
                                needs_full_redraw = true;
                            } else if app.chat.context_menu.is_some() {
                                match key.code {
                                    KeyCode::Up => {
                                        if let Some(ref mut ctx) = app.chat.context_menu {
                                            ctx.selected = ctx.selected.saturating_sub(1);
                                        }
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down => {
                                        if let Some(ref mut ctx) = app.chat.context_menu {
                                            let count = f1_mono::context_menu_item_count(ctx);
                                            ctx.selected = (ctx.selected + 1).min(count.saturating_sub(1));
                                        }
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Enter => {
                                        if let Some(ctx) = app.chat.context_menu.take() {
                                            let copy_idx = if ctx.has_selection { Some(0) } else { None };
                                            let paste_idx = if ctx.has_selection { 1 } else { 0 };
                                            if copy_idx == Some(ctx.selected) {
                                                let _ = f1_mono::copy_selection(&mut app, &cached_chat);
                                            } else if ctx.selected == paste_idx {
                                                if let Some(text) = read_from_clipboard() {
                                                    app.chat.input.push_str(&text);
                                                }
                                            }
                                        }
                                        local_view_dirty = true;
                                    }
                                    _ => {
                                        // Dismiss context menu on any other key (Esc, Ctrl+C, etc.)
                                        app.chat.context_menu = None;
                                        local_view_dirty = true;
                                    }
                                }
                            } else if key.code == KeyCode::Esc {
                                if app.chat.menu_open() {
                                    app.chat.close_menu();
                                }
                                app.chat.selection_anchor = None;
                                app.chat.selection_focus = None;
                                app.chat.selection_dragging = false;
                                local_view_dirty = true;
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
                                let _ = f1_mono::copy_selection(&mut app, &cached_chat);
                                local_view_dirty = true;
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
                                if app.chat.view_mode == ChatViewMode::Transcript {
                                    app.chat.info_pane_open = !app.chat.info_pane_open;
                                    needs_full_redraw = true;
                                }
                            } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('i') {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 1) % 4;
                                local_view_dirty = true;
                            } else if key.code == KeyCode::BackTab {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 2) % 4;
                                local_view_dirty = true;
                            } else if app.chat.info_pane_open
                                && !app.chat.copy_mode
                                && !app.chat.approval_open()
                                && !app.chat.auth_open()
                                && !app.chat.menu_open()
                                && key.code == KeyCode::Right {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 1) % 4;
                                local_view_dirty = true;
                            } else if app.chat.info_pane_open
                                && !app.chat.copy_mode
                                && !app.chat.approval_open()
                                && !app.chat.auth_open()
                                && !app.chat.menu_open()
                                && key.code == KeyCode::Left {
                                app.chat.info_pane_tab = (app.chat.info_pane_tab + 2) % 4;
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
                                    KeyCode::Enter => {
                                        app.chat.menu_select();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Tab => {
                                        app.chat.menu_move_down();
                                        local_view_dirty = true;
                                    }
                                    KeyCode::BackTab => {
                                        app.chat.menu_move_up();
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
                            let project_count = payload_projects(app.chat.refresh_payload.as_ref()).len();
                            let automation_count = payload_automations(app.chat.refresh_payload.as_ref()).len();
                            match key.code {
                                KeyCode::Left => {
                                    app.dashboard.focus_col = app.dashboard.focus_col.saturating_sub(1);
                                    needs_full_redraw = true;
                                }
                                KeyCode::Right => {
                                    if app.dashboard.focus_col < 2 {
                                        app.dashboard.focus_col += 1;
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::Tab => {
                                    app.dashboard.focus_row = (app.dashboard.focus_row + 1) % 4;
                                    app.dashboard.focus_col = 0;
                                    needs_full_redraw = true;
                                }
                                KeyCode::BackTab => {
                                    app.dashboard.focus_row = (app.dashboard.focus_row + 2) % 4;
                                    app.dashboard.focus_col = 0;
                                    needs_full_redraw = true;
                                }
                                KeyCode::Up => {
                                    if app.dashboard.focus_col == 0 {
                                        match app.dashboard.focus_row {
                                            0 => app.dashboard.agent_index = app.dashboard.agent_index.saturating_sub(1),
                                            1 => app.dashboard.project_index = app.dashboard.project_index.saturating_sub(1),
                                            2 => app.dashboard.automation_index = app.dashboard.automation_index.saturating_sub(1),
                                            _ => {}
                                        }
                                    } else {
                                        app.dashboard.focus_row = app.dashboard.focus_row.saturating_sub(1);
                                    }
                                    needs_full_redraw = true;
                                }
                                KeyCode::Down => {
                                    if app.dashboard.focus_col == 0 {
                                        match app.dashboard.focus_row {
                                            0 if app.dashboard.agent_index + 1 < agent_count => app.dashboard.agent_index += 1,
                                            1 if app.dashboard.project_index + 1 < project_count => app.dashboard.project_index += 1,
                                            2 if app.dashboard.automation_index + 1 < automation_count => app.dashboard.automation_index += 1,
                                            _ => {}
                                        }
                                    } else if app.dashboard.focus_row < 2 {
                                        app.dashboard.focus_row += 1;
                                    }
                                    needs_full_redraw = true;
                                }
                                _ => {}
                            }
                        }
                        View::InterAgent => {
                            if key.code == KeyCode::F(6) {
                                app.inter_agent.app_mouse_mode = !app.inter_agent.app_mouse_mode;
                                app.inter_agent.transcript_dragging = false;
                                if !app.inter_agent.app_mouse_mode {
                                    app.inter_agent.transcript_anchor = None;
                                    app.inter_agent.transcript_focus = None;
                                }
                                needs_full_redraw = true;
                                continue;
                            }
                            let rooms = payload_inter_agent_rooms(app.chat.refresh_payload.as_ref());
                            let room_count = rooms.len();
                            let selected_room = rooms.get(app.inter_agent.selected).map(|room| (*room).clone());
                            let selected_kind = selected_room.as_ref()
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
                                    if let Some(room) = selected_room.as_ref() {
                                        if let Some(area) = inter_agent_stream_area(&app, outer_w, outer_h) {
                                            let _ = copy_inter_agent_selection(&mut app, room, area);
                                            needs_full_redraw = true;
                                        }
                                    }
                                }
                                KeyCode::Char('d') | KeyCode::Delete => {
                                    if let Some(room) = selected_room.as_ref() {
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
                                    KeyCode::F(6) => {
                                        app.sessions.app_mouse_mode = !app.sessions.app_mouse_mode;
                                        needs_full_redraw = true;
                                    }
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
                        local_view_dirty = true;
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
                    if app.active_view == View::Chat && app.chat.app_mouse_mode {
                        let area = f1_mono::content_area(outer_w, outer_h);
                        let lines = &cached_chat.visual.lines;
                        match mouse.kind {
                            MouseEventKind::ScrollUp => {
                                app.chat.context_menu = None;
                                if point_in_rect(area, mouse.column, mouse.row) {
                                    let max_scroll = lines.len().saturating_sub(area.height as usize);
                                    app.chat.scroll = (app.chat.scroll + 3).min(max_scroll);
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::ScrollDown => {
                                app.chat.context_menu = None;
                                if point_in_rect(area, mouse.column, mouse.row) {
                                    app.chat.scroll = app.chat.scroll.saturating_sub(3);
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Down(MouseButton::Left) => {
                                if let Some(ctx) = app.chat.context_menu.clone() {
                                    // Match draw_context_menu geometry exactly:
                                    // menu_w=10, borders add 2, top border row, items start at my+1
                                    let menu_w: u16 = 10;
                                    let item_count = f1_mono::context_menu_item_count(&ctx);
                                    let menu_h = item_count as u16;
                                    let my = if ctx.y + menu_h + 2 >= outer_h {
                                        ctx.y.saturating_sub(menu_h + 2)
                                    } else {
                                        ctx.y + 1
                                    };
                                    let mx = ctx.x.min(outer_w.saturating_sub(menu_w + 2));
                                    // Items are at rows my+1 .. my+1+item_count, columns mx+1 .. mx+1+menu_w
                                    let item_y_start = my + 1;
                                    if mouse.column > mx && mouse.column <= mx + menu_w
                                        && mouse.row >= item_y_start && mouse.row < item_y_start + menu_h
                                    {
                                        let clicked = (mouse.row - item_y_start) as usize;
                                        let copy_idx = if ctx.has_selection { Some(0) } else { None };
                                        let paste_idx = if ctx.has_selection { 1 } else { 0 };
                                        if copy_idx == Some(clicked) {
                                            let _ = f1_mono::copy_selection(&mut app, &cached_chat);
                                        } else if clicked == paste_idx {
                                            if let Some(text) = read_from_clipboard() {
                                                app.chat.input.push_str(&text);
                                            }
                                        }
                                        app.chat.context_menu = None;
                                    } else {
                                        app.chat.context_menu = None;
                                    }
                                    local_view_dirty = true;
                                } else {
                                    if let Some(point) = f1_mono::point_at_mouse(&cached_chat, &app, outer_w, outer_h, mouse.column, mouse.row) {
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
                            }
                            MouseEventKind::Drag(MouseButton::Left) => {
                                if app.chat.selection_dragging {
                                    let max_scroll = lines.len().saturating_sub(area.height as usize);
                                    if mouse.row < area.y {
                                        app.chat.scroll = (app.chat.scroll + 1).min(max_scroll);
                                    } else if mouse.row >= area.y.saturating_add(area.height) {
                                        app.chat.scroll = app.chat.scroll.saturating_sub(1);
                                    }
                                    if let Some(point) = f1_mono::point_at_mouse(&cached_chat, &app, outer_w, outer_h, mouse.column, mouse.row) {
                                        app.chat.selection_focus = Some(point);
                                    }
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Up(MouseButton::Left) => {
                                if app.chat.selection_dragging {
                                    app.chat.selection_dragging = false;
                                    if let Some(point) = f1_mono::point_at_mouse(&cached_chat, &app, outer_w, outer_h, mouse.column, mouse.row) {
                                        app.chat.selection_focus = Some(point);
                                    }
                                    // Auto-copy on mouse-up if there's a real selection
                                    if app.chat.selection_anchor != app.chat.selection_focus {
                                        let _ = f1_mono::copy_selection(&mut app, &cached_chat);
                                    }
                                    local_view_dirty = true;
                                }
                            }
                            MouseEventKind::Down(MouseButton::Right) => {
                                let has_sel = app.chat.selection_anchor.is_some()
                                    && app.chat.selection_focus.is_some()
                                    && app.chat.selection_anchor != app.chat.selection_focus;
                                app.chat.context_menu = Some(crate::chat::ContextMenu {
                                    x: mouse.column,
                                    y: mouse.row,
                                    selected: 0,
                                    has_selection: has_sel,
                                });
                                local_view_dirty = true;
                            }
                            MouseEventKind::Up(MouseButton::Right) => {}
                            _ => {
                                // Dismiss context menu on any other mouse event
                                if app.chat.context_menu.is_some() {
                                    app.chat.context_menu = None;
                                    local_view_dirty = true;
                                }
                            }
                        }
                    } else if app.active_view == View::Sessions && app.sessions.app_mouse_mode {
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
                    } else if app.active_view == View::InterAgent && app.inter_agent.app_mouse_mode {
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
                                let room_owned = rooms.get(app.inter_agent.selected).map(|room| (*room).clone());
                                if let (Some(room), Some(area)) = (room_owned, inter_agent_stream_area(&app, outer_w, outer_h)) {
                                    let _ = copy_inter_agent_selection(&mut app, &room, area);
                                    needs_full_redraw = true;
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
                    front_buf.resize(outer_w, outer_h);
                    back_buf.resize(outer_w, outer_h);
                    needs_full_redraw = true;
                }
                _ => {}
            }
        } // event drain loop

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
    stdout.queue(LeaveAlternateScreen)?;
    stdout.flush()?;
    ct::disable_raw_mode()?;
    println!("charon-tui exited.");
    Ok(())
}
