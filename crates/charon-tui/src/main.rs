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
mod cli;
pub mod clipboard;
mod config;
mod daemon_client;
mod f1_mono;
mod layout;
mod grid;
mod input;
mod native_session;
mod parser;
mod protocol;
mod render;
mod screen;
mod session;
mod terminal;
mod util;
mod views;

use std::io::{self, Write};
use std::time::{Duration, Instant};

use app::{App, SessionsSection, View};
use crossterm::{
    cursor,
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyModifiers, MouseButton, MouseEventKind,
    },
    terminal::{self as ct, EnterAlternateScreen, LeaveAlternateScreen},
    QueueableCommand,
};

// The daemon engine (and its state-detection helper) is linked from the
// library crate — the same code `charond` runs — rather than re-compiled
// into this binary, so its internals aren't falsely "dead" here.
use charon_tui::daemon;

use backend::discover_sessions;
use chat::{ChatViewMode, LaunchOptions};
use f1_mono::F1MonoCache;
use grid::compute_grid;
use native_session::NativeSessionServer;
use session::{BackendType, SessionCell};

use clipboard::copy_to_clipboard;
use cli::{parse_args, LaunchMode};
use input::{apply_native_commands, apply_native_input_bytes, encode_key};
use views::chat_view;
use views::dashboard_view::draw_dashboard;
use views::inter_agent_view::{
    conversation_transcript_rows, copy_inter_agent_selection, draw_inter_agent,
    inter_agent_stream_area, sync_inter_agent_room_panes, transcript_max_scroll,
    transcript_point_at_mouse,
};
use views::sessions_view::{
    build_initial_sessions, build_native_session_snapshot, draw_sessions,
    ensure_native_self_pane, grid_tabs, next_grid_focus, pane_agent_id, pane_at_point,
    project_names, relayout_sessions, scroll_session_pane, session_list_rows,
    split_focused_pane, sync_daemon_panes, sync_session_panes_from_payload,
    visible_pane_indices, visible_session_agent_ids, SessionListRow,
};
use views::{
    draw_footer, draw_footer_buf, draw_header, draw_header_buf, payload_agents,
    payload_automations, payload_inter_agent_rooms, payload_projects, point_in_rect,
};

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

    if matches!(mode, &LaunchMode::DaemonUpgrade) {
        if daemon::is_running() {
            daemon_client::shutdown(&daemon::control_socket())
                .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("shutdown failed: {e}")))?;
            // Wait for the old daemon to release the socket.
            let deadline = Instant::now() + Duration::from_secs(5);
            while daemon::is_running() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        daemon::ensure_running()?;
        println!("charond upgraded and restarted.");
        return Ok(());
    }

    if matches!(mode, &LaunchMode::DaemonList) {
        daemon::ensure_running()?;
        match daemon_client::list_sessions(&daemon::control_socket()) {
            Ok(sessions) if sessions.is_empty() => println!("No daemon sessions."),
            Ok(sessions) => {
                println!("Daemon sessions:");
                for s in sessions {
                    println!("  {} [{}] {}x{} {} (seq {})", s.id, s.kind, s.cols, s.rows, s.state, s.seq);
                }
            }
            Err(e) => {
                eprintln!("Error: {e}");
                std::process::exit(1);
            }
        }
        return Ok(());
    }

    let (outer_w, outer_h) = ct::size()?;
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
    let session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
    // (native transcript render state removed with the legacy renderer)

    clipboard::configure_tmux_clipboard();

    ct::enable_raw_mode()?;
    let mut stdout = io::stdout();
    stdout.queue(EnterAlternateScreen)?;
    stdout.queue(EnableBracketedPaste)?;
    // Enable alternate scroll mode: terminal converts scroll wheel into
    // Up/Down arrow key sequences when in the alternate screen buffer.
    // This lets us receive scroll events as key events WITHOUT capturing
    // the mouse — so terminal-native selection, right-click, and Cmd+C
    // all work normally.
    stdout.write_all(b"\x1b[?1007h")?;
    let mut mouse_capture_enabled = false;
    if app.active_view == View::Sessions {
        stdout.queue(EnableMouseCapture)?;
        mouse_capture_enabled = true;
    }
    stdout.queue(cursor::Hide)?;
    stdout.queue(ct::Clear(ct::ClearType::All))?;
    stdout.queue(cursor::MoveTo(0, 0))?;
    stdout.flush()?;

    let mut event_loop = EventLoop {
        app,
        native_session,
        stdout,
        outer_w,
        outer_h,
        session_rects,
        mouse_capture_enabled,
        last_render: Instant::now(),
        needs_full_redraw: true,
        local_view_dirty: false,
        cached_chat: F1MonoCache::default(),
        last_rowing_tick: Instant::now(),
        last_session_poll: Instant::now() - Duration::from_secs(1),
        last_daemon_poll: Instant::now() - Duration::from_secs(2),
        front_buf: screen::ScreenBuf::new(outer_w, outer_h),
        back_buf: screen::ScreenBuf::new(outer_w, outer_h),
        last_chat_msg_count: 0,
        last_cache_rebuild: Instant::now(),
        last_snapshot: Instant::now(),
        was_streaming: false,
    };
    event_loop.run()?;
    let mut stdout = event_loop.stdout;

    stdout.queue(cursor::Show)?;
    stdout.queue(DisableMouseCapture)?;
    stdout.queue(DisableBracketedPaste)?;
    stdout.write_all(b"\x1b[?1007l")?;  // disable alternate scroll mode
    stdout.queue(LeaveAlternateScreen)?;
    stdout.flush()?;
    ct::disable_raw_mode()?;
    println!("charon-tui exited.");
    Ok(())
}


/// Frame budget for coalesced renders (~60 fps).
const FRAME_DURATION: Duration = Duration::from_millis(16);

/// All mutable state owned by the interactive event loop.
///
/// `main()` performs one-time setup (CLI dispatch, terminal modes), builds
/// this struct, and calls [`EventLoop::run`]; teardown happens back in
/// `main()` after `run` returns.
struct EventLoop {
    app: App,
    native_session: Option<NativeSessionServer>,
    stdout: io::Stdout,
    outer_w: u16,
    outer_h: u16,
    session_rects: Vec<render::Rect>,
    mouse_capture_enabled: bool,
    last_render: Instant,
    needs_full_redraw: bool,
    local_view_dirty: bool,
    cached_chat: F1MonoCache,
    last_rowing_tick: Instant,
    last_session_poll: Instant,
    last_daemon_poll: Instant,
    front_buf: screen::ScreenBuf,
    back_buf: screen::ScreenBuf,
    last_chat_msg_count: usize,
    last_cache_rebuild: Instant,
    last_snapshot: Instant,
    was_streaming: bool,
}


/// Per-iteration dirt tracking produced by [`EventLoop::poll_and_sync`].
struct DirtyFlags {
    /// A visible session/room pane produced new output.
    any_dirty: bool,
    /// The chat backend produced new events.
    chat_dirty: bool,
    /// The native-session server delivered input/resize commands.
    native_input_dirty: bool,
}

impl EventLoop {
    /// Drive the TUI until the user quits (Ctrl+Q). Errors propagate to
    /// `main()` immediately, before terminal teardown — same as the previous
    /// inline loop.
    fn run(&mut self) -> io::Result<()> {
        loop {
            let flags = self.poll_and_sync()?;
            self.render_if_due(&flags)?;
            self.sync_mouse_capture()?;

            let event_poll_interval = match self.app.active_view {
                View::Chat => Duration::from_millis(4),
                View::Sessions if self.app.sessions.terminal_mode => Duration::from_millis(4),
                View::Sessions => Duration::from_millis(16),
                _ => Duration::from_millis(33),
            };
            // Block up to event_poll_interval for first event, then drain all queued
            // events. This coalesces bursts (e.g. rapid scrolling) into a single
            // render. On timeout, skip straight to the next poll/render pass.
            if !event::poll(event_poll_interval)? { continue; }
            loop {
                if !event::poll(Duration::ZERO)? { break; }
                match event::read()? {
                    Event::Key(key) => {
                        if self.handle_key(key)? {
                            return Ok(());
                        }
                    }
                    Event::Paste(text) => self.handle_paste(text)?,
                    Event::Mouse(mouse) => self.handle_mouse(mouse)?,
                    Event::Resize(w, h) => self.handle_resize(w, h)?,
                    _ => {}
                }
            }

            self.reap_closed_panes()?;
        }
    }

    /// Poll backends (panes, chat, native-session commands) and sync pane
    /// structure with the latest payload / daemon inventory.
    fn poll_and_sync(&mut self) -> io::Result<DirtyFlags> {
        self.app.chat.clear_expired_notices();
        self.app.inter_agent.clear_expired_notices();
        let mut any_dirty = false;
        let pane_poll_interval = match self.app.active_view {
            View::Sessions if self.app.sessions.terminal_mode => Duration::from_millis(16),
            View::Sessions => Duration::from_millis(33),
            _ => Duration::from_millis(250),
        };
        if self.last_session_poll.elapsed() >= pane_poll_interval && self.app.active_view != View::Chat {
            let visible_set: std::collections::HashSet<usize> = if self.app.active_view == View::Sessions {
                visible_pane_indices(&mut self.app).into_iter().collect()
            } else {
                (0..self.app.sessions.panes.len()).collect()
            };
            for (idx, cell) in self.app.sessions.panes.iter_mut().enumerate() {
                if !visible_set.contains(&idx) {
                    continue;
                }
                cell.poll()?;
                if cell.terminal.dirty {
                    any_dirty = true;
                }
            }
            if self.app.active_view == View::InterAgent {
                for cell in self.app.inter_agent.room_panes.iter_mut() {
                    cell.poll()?;
                    if cell.terminal.dirty {
                        cell.reset_viewport_scroll();
                        any_dirty = true;
                    }
                }
            }
            self.last_session_poll = Instant::now();
        }
        let chat_dirty = self.app.chat.poll();
        if chat_dirty {
        }
        let native_input_dirty = if let Some(server) = &self.native_session {
            let commands = server.drain_commands();
            let dirty = !commands.is_empty();
            if dirty {
                apply_native_commands(&mut self.app, commands);
            }
            dirty
        } else {
            false
        };
        if native_input_dirty && self.app.active_view == View::Chat {
        }
        let mut session_structure_changed = false;
        if self.app.active_view == View::Sessions && (chat_dirty || native_input_dirty) && self.app.chat.refresh_payload.is_some() {
            self.app.sessions.backend_filter_pending = false;
            let mut changed = sync_session_panes_from_payload(&mut self.app, self.outer_w, self.outer_h)?;
            if ensure_native_self_pane(&mut self.app, self.native_session.as_ref(), self.outer_w, self.outer_h)? {
                changed = true;
            }
            if changed {
                session_structure_changed = true;
                self.needs_full_redraw = true;
            }
        }
        // Daemon-owned sessions are independent of the Python payload; poll the
        // daemon's inventory on a slow timer and surface new sessions as panes.
        if self.app.active_view == View::Sessions && self.last_daemon_poll.elapsed() >= Duration::from_millis(1000) {
            self.last_daemon_poll = Instant::now();
            if sync_daemon_panes(&mut self.app, self.outer_w, self.outer_h)? {
                session_structure_changed = true;
                self.needs_full_redraw = true;
            }
        }
        if self.app.active_view == View::InterAgent && (chat_dirty || native_input_dirty) && self.app.chat.refresh_payload.is_some() {
            let refresh_payload = self.app.chat.refresh_payload.clone();
            let rooms = payload_inter_agent_rooms(refresh_payload.as_ref());
            if let Some(room) = rooms.get(self.app.inter_agent.selected).cloned() {
                if room.get("kind").and_then(|v| v.as_str()).unwrap_or("") != "libris" {
                    if sync_inter_agent_room_panes(&mut self.app, &room, self.outer_w.saturating_sub(8), self.outer_h.saturating_sub(10))? {
                        self.needs_full_redraw = true;
                    }
                }
            }
        }
        if session_structure_changed {
            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
            let visible = visible_pane_indices(&mut self.app);
            if let Some(first) = visible.first() {
                if !visible.contains(&self.app.sessions.focused) {
                    self.app.sessions.focused = *first;
                }
            }
        }

        Ok(DirtyFlags { any_dirty, chat_dirty, native_input_dirty })
    }

    /// Redraw the active view if anything is dirty and the frame budget allows,
    /// and publish a native-session snapshot on a slow timer.
    fn render_if_due(&mut self, flags: &DirtyFlags) -> io::Result<()> {
        let &DirtyFlags { any_dirty, chat_dirty, native_input_dirty } = flags;
        let now = Instant::now();
        if self.app.active_view == View::Chat && chat_view::chat_rowing_active(&self.app) && now.duration_since(self.last_rowing_tick) >= Duration::from_millis(300) {
            self.local_view_dirty = true;
            self.last_rowing_tick = now;
        }
        let sessions_dirty = self.app.active_view == View::Sessions && (any_dirty || native_input_dirty || self.local_view_dirty);
        let dashboard_dirty = self.app.active_view == View::Dashboard && (chat_dirty || native_input_dirty || self.local_view_dirty);
        let chat_view_dirty = self.app.active_view == View::Chat && (chat_dirty || native_input_dirty || self.local_view_dirty);
        let inter_agent_dirty = self.app.active_view == View::InterAgent && (any_dirty || chat_dirty || native_input_dirty || self.local_view_dirty);
        let native_snapshot_dirty = native_input_dirty;

        if (self.needs_full_redraw || sessions_dirty || dashboard_dirty || chat_view_dirty || inter_agent_dirty || native_snapshot_dirty) && now.duration_since(self.last_render) >= FRAME_DURATION {
            let force_all = self.needs_full_redraw;

            if self.app.active_view == View::Chat {
                // Only force cache rebuild when messages actually changed.
                // Throttle streaming rebuilds to ~100ms to avoid 11-19ms rebuilds every frame.
                let msg_count = self.app.chat.messages.len();
                let msg_changed = msg_count != self.last_chat_msg_count;
                let stream_ended = self.was_streaming && !self.app.chat.streaming;
                let cache_age = now.duration_since(self.last_cache_rebuild);
                let streaming_update = self.app.chat.streaming && chat_dirty
                    && cache_age >= Duration::from_millis(200);
                let content_changed = msg_changed || streaming_update || stream_ended
                    || native_input_dirty || force_all;
                if msg_changed { self.last_chat_msg_count = msg_count; }
                if content_changed { self.last_cache_rebuild = now; }
                self.was_streaming = self.app.chat.streaming;
                f1_mono::ensure_cache(&self.app, self.outer_w, self.outer_h, &mut self.cached_chat, content_changed);

                self.back_buf.clear();
                draw_header_buf(&mut self.back_buf, &self.app, self.outer_w);
                f1_mono::draw(&mut self.back_buf, &self.app, self.outer_w, self.outer_h, &self.cached_chat);
                draw_footer_buf(&mut self.back_buf, &self.app, self.outer_w, self.outer_h);
                screen::flush(&self.back_buf, &self.front_buf, &mut self.stdout)?;
                std::mem::swap(&mut self.front_buf, &mut self.back_buf);
            } else {
                // Legacy direct rendering for other views
                self.stdout.queue(cursor::Hide)?;
                if self.needs_full_redraw {
                    self.stdout.queue(ct::Clear(ct::ClearType::All))?;
                }
                draw_header(&mut self.stdout, &self.app, self.outer_w)?;
                match self.app.active_view {
                    View::Chat => unreachable!(),
                    View::Dashboard => draw_dashboard(&mut self.stdout, &self.app, self.outer_w, self.outer_h)?,
                    View::Sessions => draw_sessions(
                        &mut self.stdout,
                        &mut self.app,
                        &self.session_rects,
                        force_all,
                        self.outer_w,
                        self.outer_h,
                        self.native_session.as_ref().map(|s| s.socket_path().to_string_lossy().to_string()).as_deref(),
                    )?,
                    View::InterAgent => draw_inter_agent(&mut self.stdout, &mut self.app, self.outer_w, self.outer_h)?,
                }
                draw_footer(&mut self.stdout, &self.app, self.outer_w, self.outer_h)?;
                self.stdout.flush()?;
            }
            self.needs_full_redraw = false;
            // Throttle native snapshot publishing — it rebuilds the entire visual
            // cache from scratch which is expensive (11-19ms+). Only publish
            // every 2 seconds, or immediately on force_all (view switch/resize).
            if force_all || ((chat_dirty || native_input_dirty) && now.duration_since(self.last_snapshot) >= Duration::from_secs(2)) {
                if let Some(server) = &self.native_session {
                    let self_sock = server.socket_path().to_string_lossy().to_string();
                    let (snap_w, snap_h) = server.requested_size().unwrap_or((self.outer_w, self.outer_h));
                    server.update_snapshot(build_native_session_snapshot(&mut self.app, snap_w.max(1), snap_h.max(1), Some(&self_sock)));
                }
                self.last_snapshot = now;
            }
            self.last_render = now;
            self.local_view_dirty = false;
        }

        Ok(())
    }

    /// Enable/disable terminal mouse capture to match the active view's needs.
    fn sync_mouse_capture(&mut self) -> io::Result<()> {
        // Mouse capture is only enabled for Session Grid and Inter-Agent views
        // (clicking panes, drag-select). Chat view never captures mouse — terminal
        // handles selection, copy, right-click, paste natively. Scroll in chat uses
        // PgUp/PgDn/arrow keys.
        let want_mouse_capture =
            (self.app.active_view == View::Sessions && !self.app.sessions.terminal_mode && self.app.sessions.app_mouse_mode)
            || (self.app.active_view == View::InterAgent && self.app.inter_agent.app_mouse_mode);
        if want_mouse_capture != self.mouse_capture_enabled {
            if want_mouse_capture {
                self.stdout.queue(EnableMouseCapture)?;
            } else {
                self.stdout.queue(DisableMouseCapture)?;
            }
            self.stdout.flush()?;
            self.mouse_capture_enabled = want_mouse_capture;
        }

        Ok(())
    }

    /// Handle one key event. Returns `Ok(true)` when the user quits.
    fn handle_key(&mut self, key: event::KeyEvent) -> io::Result<bool> {
        match key.code {
            KeyCode::F(1) => {
                self.app.active_view = View::Chat;
                self.front_buf.clear();
                self.stdout.queue(ct::Clear(ct::ClearType::All))?;
                self.stdout.flush()?;
                self.needs_full_redraw = true;
                return Ok(false);
            }
            KeyCode::F(2) => { self.app.active_view = View::Dashboard; self.needs_full_redraw = true; return Ok(false); }
            KeyCode::F(3) => {
                self.app.active_view = View::Sessions;
                self.app.chat.request_refresh();
                self.needs_full_redraw = true;
                return Ok(false);
            }
            KeyCode::F(4) => {
                self.app.active_view = View::InterAgent;
                self.app.chat.request_refresh();
                self.needs_full_redraw = true;
                return Ok(false);
            }
            _ => {}
        }

        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('q') {
            return Ok(true);
        }

        match self.app.active_view {
            View::Chat => self.handle_chat_key(key)?,
            View::Dashboard => self.handle_dashboard_key(key),
            View::InterAgent => self.handle_inter_agent_key(key)?,
            View::Sessions => self.handle_sessions_key(key)?,
        }
        Ok(false)
    }

    /// Keys for the F1 chat view (input, overlays, scrolling, selection).
    fn handle_chat_key(&mut self, key: event::KeyEvent) -> io::Result<()> {
        if key.code == KeyCode::F(5) {
            self.app.chat.view_mode = match self.app.chat.view_mode {
                ChatViewMode::Transcript => ChatViewMode::Workspace,
                ChatViewMode::Workspace => ChatViewMode::Transcript,
            };
            match self.app.chat.view_mode {
                ChatViewMode::Transcript => {
                    self.app.chat.info_pane_open = false;
                    self.app.chat.app_mouse_mode = false;
                    self.app.chat.selection_anchor = None;
                    self.app.chat.selection_focus = None;
                    self.app.chat.selection_dragging = false;
                }
                ChatViewMode::Workspace => {
                    self.app.chat.info_pane_open = true;
                    self.app.chat.app_mouse_mode = false;
                    self.app.chat.selection_anchor = None;
                    self.app.chat.selection_focus = None;
                    self.app.chat.selection_dragging = false;
                }
            }
            self.needs_full_redraw = true;
        } else if key.code == KeyCode::F(6) {
            self.app.chat.app_mouse_mode = !self.app.chat.app_mouse_mode;
            self.app.chat.selection_dragging = false;
            self.app.chat.selection_anchor = None;
            self.app.chat.selection_focus = None;
            self.needs_full_redraw = true;
        } else if key.code == KeyCode::Esc {
            if self.app.chat.menu_open() {
                self.app.chat.close_menu();
            }
            self.app.chat.selection_anchor = None;
            self.app.chat.selection_focus = None;
            self.app.chat.selection_dragging = false;
            self.local_view_dirty = true;
        } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            let _ = f1_mono::copy_selection(&mut self.app, &self.cached_chat);
            self.local_view_dirty = true;
        } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
            if self.app.chat.view_mode == ChatViewMode::Transcript {
                self.app.chat.info_pane_open = !self.app.chat.info_pane_open;
                self.needs_full_redraw = true;
            }
        } else if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('i') {
            self.app.chat.info_pane_tab = (self.app.chat.info_pane_tab + 1) % 4;
            self.local_view_dirty = true;
        } else if key.code == KeyCode::BackTab {
            self.app.chat.info_pane_tab = (self.app.chat.info_pane_tab + 2) % 4;
            self.local_view_dirty = true;
        } else if self.app.chat.info_pane_open
            && !self.app.chat.copy_mode
            && !self.app.chat.approval_open()
            && !self.app.chat.auth_open()
            && !self.app.chat.menu_open()
            && key.code == KeyCode::Right {
            self.app.chat.info_pane_tab = (self.app.chat.info_pane_tab + 1) % 4;
            self.local_view_dirty = true;
        } else if self.app.chat.info_pane_open
            && !self.app.chat.copy_mode
            && !self.app.chat.approval_open()
            && !self.app.chat.auth_open()
            && !self.app.chat.menu_open()
            && key.code == KeyCode::Left {
            self.app.chat.info_pane_tab = (self.app.chat.info_pane_tab + 2) % 4;
            self.local_view_dirty = true;
        } else if self.app.chat.copy_mode {
            if key.code == KeyCode::Esc {
                self.app.chat.copy_mode = false;
                self.local_view_dirty = true;
            }
        } else if self.app.chat.approval_open() {
            match key.code {
                KeyCode::Esc => {
                    self.app.chat.approval_deny();
                    self.local_view_dirty = true;
                }
                KeyCode::Up | KeyCode::Left => {
                    self.app.chat.approval_move_prev();
                    self.local_view_dirty = true;
                }
                KeyCode::Down | KeyCode::Right => {
                    self.app.chat.approval_move_next();
                    self.local_view_dirty = true;
                }
                KeyCode::Enter => {
                    self.app.chat.approval_accept_selected();
                    self.local_view_dirty = true;
                }
                _ => {}
            }
        } else if self.app.chat.menu_open() {
            match key.code {
                KeyCode::Esc => {
                    self.app.chat.close_menu();
                    self.local_view_dirty = true;
                }
                KeyCode::Up => {
                    self.app.chat.menu_move_up();
                    self.local_view_dirty = true;
                }
                KeyCode::Down => {
                    self.app.chat.menu_move_down();
                    self.local_view_dirty = true;
                }
                KeyCode::Enter => {
                    self.app.chat.menu_select();
                    self.local_view_dirty = true;
                }
                KeyCode::Tab => {
                    self.app.chat.menu_move_down();
                    self.local_view_dirty = true;
                }
                KeyCode::BackTab => {
                    self.app.chat.menu_move_up();
                    self.local_view_dirty = true;
                }
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.app.chat.input.push(c);
                    self.app.chat.maybe_open_command_menu();
                    self.local_view_dirty = true;
                }
                KeyCode::Backspace => {
                    self.app.chat.input.pop();
                    self.app.chat.maybe_open_command_menu();
                    self.local_view_dirty = true;
                }
                _ => {}
            }
        } else if self.app.chat.auth_open() {
            match key.code {
                KeyCode::Esc => {
                    self.app.chat.auth_dismiss();
                    self.local_view_dirty = true;
                }
                KeyCode::Left => {
                    self.app.chat.auth_move_prev();
                    self.local_view_dirty = true;
                }
                KeyCode::Right | KeyCode::Tab => {
                    self.app.chat.auth_move_next();
                    self.local_view_dirty = true;
                }
                KeyCode::Enter => {
                    self.app.chat.auth_activate_selected();
                    self.local_view_dirty = true;
                }
                KeyCode::Char('o') | KeyCode::Char('O') => {
                    self.app.chat.auth_action_index = 0;
                    self.app.chat.auth_activate_selected();
                    self.local_view_dirty = true;
                }
                KeyCode::Char('c') | KeyCode::Char('C') => {
                    self.app.chat.auth_action_index = 1;
                    self.app.chat.auth_activate_selected();
                    self.local_view_dirty = true;
                }
                KeyCode::Char(ch) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    // Any other typing dismisses the auth overlay and
                    // routes the key to the input. A stale overlay (e.g.
                    // left open after auth already succeeded) must never
                    // trap the user into a "can't type" state.
                    self.app.chat.auth_dismiss();
                    self.app.chat.input.push(ch);
                    self.app.chat.maybe_open_command_menu();
                    self.local_view_dirty = true;
                }
                _ => {}
            }
        } else {
            match key.code {
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.app.chat.input.push(c);
                    self.app.chat.maybe_open_command_menu();
                    self.local_view_dirty = true;
                }
                KeyCode::Backspace => {
                    self.app.chat.input.pop();
                    self.app.chat.maybe_open_command_menu();
                    self.local_view_dirty = true;
                }
                KeyCode::Enter => {
                    self.app.chat.submit_input();
                    self.local_view_dirty = true;
                }
                KeyCode::Tab => {
                    if self.app.chat.input.trim().starts_with('/') {
                        self.app.chat.maybe_open_command_menu();
                        self.local_view_dirty = true;
                    }
                }
                KeyCode::Up if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.app.chat.scroll = self.app.chat.scroll.saturating_add(1);
                    self.local_view_dirty = true;
                }
                KeyCode::Down if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    self.app.chat.scroll = self.app.chat.scroll.saturating_sub(1);
                    self.local_view_dirty = true;
                }
                KeyCode::Up => {
                    // When input is empty, scroll conversation (handles
                    // alternate scroll mode where wheel → arrow keys).
                    // When input has text, cycle command history.
                    if self.app.chat.input.is_empty() && self.app.chat.history_index.is_none() {
                        self.app.chat.scroll = self.app.chat.scroll.saturating_add(3);
                    } else {
                        self.app.chat.history_up();
                        self.app.chat.maybe_open_command_menu();
                    }
                    self.local_view_dirty = true;
                }
                KeyCode::Down => {
                    if self.app.chat.input.is_empty() && self.app.chat.history_index.is_none() {
                        self.app.chat.scroll = self.app.chat.scroll.saturating_sub(3);
                    } else {
                        self.app.chat.history_down();
                        self.app.chat.maybe_open_command_menu();
                    }
                    self.local_view_dirty = true;
                }
                KeyCode::PageUp => {
                    self.app.chat.scroll = self.app.chat.scroll.saturating_add(10);
                    self.local_view_dirty = true;
                }
                KeyCode::PageDown => {
                    self.app.chat.scroll = self.app.chat.scroll.saturating_sub(10);
                    self.local_view_dirty = true;
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Keys for the F2 dashboard view (focus movement across columns/rows).
    fn handle_dashboard_key(&mut self, key: event::KeyEvent) {
        let agent_count = payload_agents(self.app.chat.refresh_payload.as_ref()).len();
        let project_count = payload_projects(self.app.chat.refresh_payload.as_ref()).len();
        let automation_count = payload_automations(self.app.chat.refresh_payload.as_ref()).len();
        match key.code {
            KeyCode::Left => {
                self.app.dashboard.focus_col = self.app.dashboard.focus_col.saturating_sub(1);
                self.needs_full_redraw = true;
            }
            KeyCode::Right => {
                if self.app.dashboard.focus_col < 2 {
                    self.app.dashboard.focus_col += 1;
                }
                self.needs_full_redraw = true;
            }
            KeyCode::Tab => {
                self.app.dashboard.focus_row = (self.app.dashboard.focus_row + 1) % 4;
                self.app.dashboard.focus_col = 0;
                self.needs_full_redraw = true;
            }
            KeyCode::BackTab => {
                self.app.dashboard.focus_row = (self.app.dashboard.focus_row + 2) % 4;
                self.app.dashboard.focus_col = 0;
                self.needs_full_redraw = true;
            }
            KeyCode::Up => {
                if self.app.dashboard.focus_col == 0 {
                    match self.app.dashboard.focus_row {
                        0 => self.app.dashboard.agent_index = self.app.dashboard.agent_index.saturating_sub(1),
                        1 => self.app.dashboard.project_index = self.app.dashboard.project_index.saturating_sub(1),
                        2 => self.app.dashboard.automation_index = self.app.dashboard.automation_index.saturating_sub(1),
                        _ => {}
                    }
                } else {
                    self.app.dashboard.focus_row = self.app.dashboard.focus_row.saturating_sub(1);
                }
                self.needs_full_redraw = true;
            }
            KeyCode::Down => {
                if self.app.dashboard.focus_col == 0 {
                    match self.app.dashboard.focus_row {
                        0 if self.app.dashboard.agent_index + 1 < agent_count => self.app.dashboard.agent_index += 1,
                        1 if self.app.dashboard.project_index + 1 < project_count => self.app.dashboard.project_index += 1,
                        2 if self.app.dashboard.automation_index + 1 < automation_count => self.app.dashboard.automation_index += 1,
                        _ => {}
                    }
                } else if self.app.dashboard.focus_row < 2 {
                    self.app.dashboard.focus_row += 1;
                }
                self.needs_full_redraw = true;
            }
            _ => {}
        }
    }

    /// Keys for the F4 inter-agent view (room list, transcript, graph).
    fn handle_inter_agent_key(&mut self, key: event::KeyEvent) -> io::Result<()> {
        if key.code == KeyCode::F(6) {
            self.app.inter_agent.app_mouse_mode = !self.app.inter_agent.app_mouse_mode;
            self.app.inter_agent.transcript_dragging = false;
            if !self.app.inter_agent.app_mouse_mode {
                self.app.inter_agent.transcript_anchor = None;
                self.app.inter_agent.transcript_focus = None;
            }
            self.needs_full_redraw = true;
            return Ok(());
        }
        let rooms = payload_inter_agent_rooms(self.app.chat.refresh_payload.as_ref());
        let room_count = rooms.len();
        let selected_room = rooms.get(self.app.inter_agent.selected).map(|room| (*room).clone());
        let selected_kind = selected_room.as_ref()
            .and_then(|r| r.get("kind")).and_then(|v| v.as_str()).unwrap_or("");
        if self.app.inter_agent.delete_confirm_open {
            match key.code {
                KeyCode::Esc | KeyCode::Char('n') | KeyCode::Char('N') => {
                    self.app.inter_agent.delete_confirm_open = false;
                    self.needs_full_redraw = true;
                }
                KeyCode::Enter | KeyCode::Char('y') | KeyCode::Char('Y') => {
                    if !self.app.inter_agent.delete_target_room_id.is_empty() {
                        let _ = self.app.chat.backend.send_command(&format!("/delete-room {}", self.app.inter_agent.delete_target_room_id));
                        self.app.chat.request_refresh();
                    }
                    self.app.inter_agent.delete_confirm_open = false;
                    self.app.inter_agent.delete_target_room_id.clear();
                    self.app.inter_agent.delete_target_title.clear();
                    self.app.inter_agent.room_panes.clear();
                    self.app.inter_agent.room_panes_room_id.clear();
                    self.needs_full_redraw = true;
                }
                _ => {}
            }
            return Ok(());
        }
        match key.code {
            KeyCode::Esc => {
                self.app.inter_agent.transcript_anchor = None;
                self.app.inter_agent.transcript_focus = None;
                self.app.inter_agent.transcript_dragging = false;
                self.needs_full_redraw = true;
            }
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                if let Some(room) = selected_room.as_ref() {
                    if let Some(area) = inter_agent_stream_area(&self.app, self.outer_w, self.outer_h) {
                        let _ = copy_inter_agent_selection(&mut self.app, room, area);
                        self.needs_full_redraw = true;
                    }
                }
            }
            KeyCode::Char('d') | KeyCode::Delete => {
                if let Some(room) = selected_room.as_ref() {
                    let room_id = room.get("id").and_then(|v| v.as_str()).unwrap_or("");
                    if !room_id.is_empty() {
                        self.app.inter_agent.delete_confirm_open = true;
                        self.app.inter_agent.delete_target_room_id = room_id.to_string();
                        self.app.inter_agent.delete_target_title = room.get("title").and_then(|v| v.as_str()).unwrap_or(room_id).to_string();
                        self.needs_full_redraw = true;
                    }
                }
            }
            KeyCode::Tab => {
                if selected_kind == "libris" {
                    self.app.inter_agent.graph_focus = !self.app.inter_agent.graph_focus;
                }
                self.needs_full_redraw = true;
            }
            KeyCode::Up => {
                if self.app.inter_agent.graph_focus && selected_kind == "libris" {
                    self.app.inter_agent.selected_node = self.app.inter_agent.selected_node.saturating_sub(1);
                } else {
                    self.app.inter_agent.selected = self.app.inter_agent.selected.saturating_sub(1);
                    self.app.inter_agent.selected_node = 0;
                    self.app.inter_agent.event_scroll = 0;
                    self.app.inter_agent.topic_detail = false;
                    self.app.inter_agent.transcript_anchor = None;
                    self.app.inter_agent.transcript_focus = None;
                    self.app.inter_agent.transcript_dragging = false;
                }
                self.needs_full_redraw = true;
            }
            KeyCode::Down => {
                if self.app.inter_agent.graph_focus && selected_kind == "libris" {
                    self.app.inter_agent.selected_node = self.app.inter_agent.selected_node.saturating_add(1);
                } else {
                    if self.app.inter_agent.selected + 1 < room_count {
                        self.app.inter_agent.selected += 1;
                    }
                    self.app.inter_agent.selected_node = 0;
                    self.app.inter_agent.event_scroll = 0;
                    self.app.inter_agent.topic_detail = false;
                    self.app.inter_agent.transcript_anchor = None;
                    self.app.inter_agent.transcript_focus = None;
                    self.app.inter_agent.transcript_dragging = false;
                }
                self.needs_full_redraw = true;
            }
            KeyCode::Left => {
                if selected_kind == "libris" {
                    self.app.inter_agent.graph_focus = false;
                    self.needs_full_redraw = true;
                }
            }
            KeyCode::Right => {
                if selected_kind == "libris" {
                    self.app.inter_agent.graph_focus = true;
                    self.needs_full_redraw = true;
                }
            }
            KeyCode::Enter => {
                if selected_kind == "libris" {
                    self.app.inter_agent.topic_detail = !self.app.inter_agent.topic_detail;
                }
                self.needs_full_redraw = true;
            }
            KeyCode::PageUp => {
                self.app.inter_agent.event_scroll = self.app.inter_agent.event_scroll.saturating_add(10);
                self.needs_full_redraw = true;
            }
            KeyCode::PageDown => {
                self.app.inter_agent.event_scroll = self.app.inter_agent.event_scroll.saturating_sub(10);
                self.needs_full_redraw = true;
            }
            _ => {}
        }
        Ok(())
    }

    /// Keys for the F3 sessions view (terminal mode, grid, tabs, splits).
    fn handle_sessions_key(&mut self, key: event::KeyEvent) -> io::Result<()> {
        if self.app.sessions.terminal_mode {
            let exit_terminal_mode = matches!(key.code, KeyCode::F(4))
                || (key.modifiers.contains(KeyModifiers::CONTROL)
                    && matches!(key.code, KeyCode::Char(']') | KeyCode::Char('g') | KeyCode::Char('G')));
            if exit_terminal_mode {
                self.app.sessions.terminal_mode = false;
                self.needs_full_redraw = true;
                return Ok(());
            }
            let encoded = encode_key(&key);
            if !encoded.is_empty() {
                let is_local_self = match (self.app.sessions.panes.get(self.app.sessions.focused), self.native_session.as_ref()) {
                    (Some(cell), Some(server)) => match &cell.backend_type {
                        BackendType::CharonPane { socket_path } => socket_path == &server.socket_path().to_string_lossy().to_string(),
                        _ => false,
                    },
                    _ => false,
                };
                if is_local_self {
                    if let Some(cell) = self.app.sessions.panes.get_mut(self.app.sessions.focused) {
                        cell.reset_viewport_scroll();
                    }
                    let saved_view = self.app.active_view;
                    if self.app.active_view == View::Sessions {
                        self.app.active_view = View::Chat;
                    }
                    apply_native_input_bytes(&mut self.app, &encoded);
                    self.app.active_view = saved_view;
                    self.needs_full_redraw = true;
                } else if let Some(cell) = self.app.sessions.panes.get_mut(self.app.sessions.focused) {
                    cell.reset_viewport_scroll();
                    cell.write(&encoded)?;
                }
            }
        } else {
            match key.code {
                KeyCode::F(6) => {
                    self.app.sessions.app_mouse_mode = !self.app.sessions.app_mouse_mode;
                    self.needs_full_redraw = true;
                }
                KeyCode::Tab => {
                    self.app.sessions.section = match self.app.sessions.section {
                        SessionsSection::Agents => SessionsSection::Projects,
                        SessionsSection::Projects => SessionsSection::Grid,
                        SessionsSection::Grid => SessionsSection::Agents,
                    };
                    self.needs_full_redraw = true;
                }
                KeyCode::BackTab => {
                    self.app.sessions.section = match self.app.sessions.section {
                        SessionsSection::Agents => SessionsSection::Grid,
                        SessionsSection::Projects => SessionsSection::Agents,
                        SessionsSection::Grid => SessionsSection::Projects,
                    };
                    self.needs_full_redraw = true;
                }
                KeyCode::Enter => {
                    match self.app.sessions.section {
                        SessionsSection::Grid => {
                            self.app.sessions.terminal_mode = true;
                        }
                        SessionsSection::Agents => {
                            let rows = session_list_rows(&mut self.app);
                            match rows.get(self.app.sessions.agent_index) {
                                Some(SessionListRow::Session { id, .. }) => {
                                    if !self.app.sessions.visible_agents.insert(id.clone()) {
                                        self.app.sessions.visible_agents.remove(id);
                                    }
                                }
                                Some(SessionListRow::AgentHeader { session_ids, .. }) => {
                                    let all_selected = session_ids.iter().all(|id| self.app.sessions.visible_agents.contains(id));
                                    if all_selected {
                                        for id in session_ids { self.app.sessions.visible_agents.remove(id); }
                                    } else {
                                        for id in session_ids { self.app.sessions.visible_agents.insert(id.clone()); }
                                    }
                                }
                                _ => {}
                            }
                            let visible = visible_pane_indices(&mut self.app);
                            if let Some(first) = visible.first() {
                                self.app.sessions.focused = *first;
                            }
                            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        }
                        SessionsSection::Projects => {
                            let projects = project_names(self.app.chat.refresh_payload.as_ref());
                            self.app.sessions.selected_project = if self.app.sessions.project_index == 0 {
                                None
                            } else {
                                projects.get(self.app.sessions.project_index - 1).cloned()
                            };
                            self.app.sessions.visible_agents.clear();
                            let visible = visible_session_agent_ids(&mut self.app);
                            if let Some(first_id) = visible.first() {
                                if let Some((idx, _)) = self.app.sessions.panes.iter().enumerate().find(|(i, c)| pane_agent_id(c, self.app.chat.refresh_payload.as_ref(), *i) == *first_id) {
                                    self.app.sessions.focused = idx;
                                }
                            }
                            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        }
                    }
                    self.needs_full_redraw = true;
                }
                KeyCode::Up => {
                    match self.app.sessions.section {
                        SessionsSection::Agents => {
                            self.app.sessions.agent_index = self.app.sessions.agent_index.saturating_sub(1);
                        }
                        SessionsSection::Projects => {
                            self.app.sessions.project_index = self.app.sessions.project_index.saturating_sub(1);
                        }
                        SessionsSection::Grid => {
                            let visible = visible_pane_indices(&mut self.app);
                            if let Some(next) = next_grid_focus(self.app.sessions.focused, &visible, &self.session_rects, KeyCode::Up) {
                                self.app.sessions.focused = next;
                            }
                        }
                    }
                    self.needs_full_redraw = true;
                }
                KeyCode::Down => {
                    match self.app.sessions.section {
                        SessionsSection::Agents => {
                            let rows = session_list_rows(&mut self.app);
                            if self.app.sessions.agent_index + 1 < rows.len() {
                                self.app.sessions.agent_index += 1;
                            }
                        }
                        SessionsSection::Projects => {
                            let len = project_names(self.app.chat.refresh_payload.as_ref()).len() + 1;
                            if self.app.sessions.project_index + 1 < len {
                                self.app.sessions.project_index += 1;
                            }
                        }
                        SessionsSection::Grid => {
                            let visible = visible_pane_indices(&mut self.app);
                            if let Some(next) = next_grid_focus(self.app.sessions.focused, &visible, &self.session_rects, KeyCode::Down) {
                                self.app.sessions.focused = next;
                            } else if let Some(first) = visible.first() {
                                self.app.sessions.focused = *first;
                            }
                        }
                    }
                    self.needs_full_redraw = true;
                }
                KeyCode::Left | KeyCode::Right => {
                    match self.app.sessions.section {
                        SessionsSection::Agents => {
                            let rows = session_list_rows(&mut self.app);
                            if let Some(SessionListRow::AgentHeader { name, collapsed, .. }) = rows.get(self.app.sessions.agent_index) {
                                if key.code == KeyCode::Left && !collapsed {
                                    self.app.sessions.collapsed_agents.insert(name.clone());
                                    self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                                } else if key.code == KeyCode::Right && *collapsed {
                                    self.app.sessions.collapsed_agents.remove(name);
                                    self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                                }
                            }
                        }
                        SessionsSection::Grid => {
                            let visible = visible_pane_indices(&mut self.app);
                            if let Some(next) = next_grid_focus(self.app.sessions.focused, &visible, &self.session_rects, key.code) {
                                self.app.sessions.focused = next;
                            }
                        }
                        SessionsSection::Projects => {}
                    }
                    self.needs_full_redraw = true;
                }
                KeyCode::Char('n') => {
                    let title = format!("bash-{}", self.app.sessions.panes.len());
                    let temp = compute_grid(self.app.sessions.panes.len() + 1, self.outer_w, self.outer_h.saturating_sub(2)).2;
                    if let Some(r) = temp.last() {
                        self.app.sessions.panes.push(SessionCell::spawn(self.app.sessions.panes.len() as u64, &title, &["bash"], r.width, r.height)?);
                        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        self.app.sessions.focused = self.app.sessions.panes.len() - 1;
                        self.needs_full_redraw = true;
                    }
                }
                KeyCode::Char('w') => {
                    if self.app.sessions.panes.len() > 1 {
                        self.app.sessions.panes.remove(self.app.sessions.focused);
                        if self.app.sessions.focused >= self.app.sessions.panes.len() {
                            self.app.sessions.focused = self.app.sessions.panes.len().saturating_sub(1);
                        }
                        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        self.needs_full_redraw = true;
                    }
                }
                // Manual splits (Grid section): | side-by-side, - stacked,
                // = reset to auto-tile, < / > resize the focused split.
                KeyCode::Char('|') if self.app.sessions.section == SessionsSection::Grid => {
                    if split_focused_pane(&mut self.app, layout::Dir::Horizontal, self.outer_w, self.outer_h)? {
                        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        self.needs_full_redraw = true;
                    }
                }
                KeyCode::Char('-') if self.app.sessions.section == SessionsSection::Grid => {
                    if split_focused_pane(&mut self.app, layout::Dir::Vertical, self.outer_w, self.outer_h)? {
                        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        self.needs_full_redraw = true;
                    }
                }
                KeyCode::Char('=') if self.app.sessions.section == SessionsSection::Grid => {
                    self.app.sessions.layout = None;
                    self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                    self.needs_full_redraw = true;
                }
                // Tabs: [ / ] switch the active tab, t creates a new tab.
                KeyCode::Char('[') | KeyCode::Char(']') if self.app.sessions.section == SessionsSection::Grid => {
                    let tabs = grid_tabs(&self.app);
                    if tabs.len() > 1 {
                        let cur = tabs.iter().position(|t| *t == self.app.sessions.active_tab).unwrap_or(0);
                        let next = if key.code == KeyCode::Char(']') {
                            (cur + 1) % tabs.len()
                        } else {
                            (cur + tabs.len() - 1) % tabs.len()
                        };
                        self.app.sessions.active_tab = tabs[next].clone();
                        let visible = visible_pane_indices(&mut self.app);
                        if let Some(first) = visible.first() {
                            self.app.sessions.focused = *first;
                        }
                        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                        self.needs_full_redraw = true;
                    }
                }
                KeyCode::Char('t') if self.app.sessions.section == SessionsSection::Grid => {
                    let new_tab = format!("tab-{}", grid_tabs(&self.app).len() + 1);
                    daemon::ensure_running()?;
                    let sock = daemon::control_socket();
                    let sock_str = sock.to_string_lossy().to_string();
                    let ephemeral = !config::active().persist_sessions;
                    if let Ok(sid) = daemon_client::spawn_session(&sock, &[], 80, 24, ephemeral, Some(new_tab.clone())) {
                        self.app.sessions.visible_agents.insert(sid.clone());
                        if let Ok(cell) = SessionCell::attach_daemon(self.app.sessions.panes.len() as u64, &sid, &sid, &sock_str, 80, 24) {
                            self.app.sessions.panes.push(cell);
                            self.app.sessions.active_tab = new_tab;
                            self.app.sessions.layout = None; // fresh tab → auto-tile
                            self.last_daemon_poll = Instant::now() - Duration::from_secs(2);
                            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                            self.needs_full_redraw = true;
                        }
                    }
                }
                // Zoom: show the focused pane fullscreen.
                KeyCode::Char('z') if self.app.sessions.section == SessionsSection::Grid => {
                    self.app.sessions.zoom = !self.app.sessions.zoom;
                    self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                    self.needs_full_redraw = true;
                }
                // Kill the focused daemon session (terminate + delete history).
                KeyCode::Char('X') if self.app.sessions.section == SessionsSection::Grid => {
                    if let Some(BackendType::DaemonPane { session_id }) =
                        self.app.sessions.panes.get(self.app.sessions.focused).map(|c| c.backend_type.clone())
                    {
                        let _ = daemon_client::kill_session(&daemon::control_socket(), &session_id);
                    }
                    if self.app.sessions.panes.len() > 1 {
                        self.app.sessions.panes.remove(self.app.sessions.focused);
                        if self.app.sessions.focused >= self.app.sessions.panes.len() {
                            self.app.sessions.focused = self.app.sessions.panes.len().saturating_sub(1);
                        }
                    }
                    self.app.sessions.zoom = false;
                    self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                    self.needs_full_redraw = true;
                }
                // Pin/unpin the focused daemon pane (persist vs ephemeral).
                KeyCode::Char('p') if self.app.sessions.section == SessionsSection::Grid => {
                    if let Some(BackendType::DaemonPane { session_id }) =
                        self.app.sessions.panes.get(self.app.sessions.focused).map(|c| c.backend_type.clone())
                    {
                        let currently_ephemeral = self.app.sessions.daemon_sessions.iter()
                            .find(|s| s.id == session_id).map(|s| s.ephemeral).unwrap_or(true);
                        let _ = daemon_client::set_persist(&daemon::control_socket(), &session_id, currently_ephemeral);
                        self.last_daemon_poll = Instant::now() - Duration::from_secs(2); // refresh soon
                        self.needs_full_redraw = true;
                    }
                }
                KeyCode::Char('<') | KeyCode::Char('>') if self.app.sessions.section == SessionsSection::Grid => {
                    if let Some(uid) = self.app.sessions.panes.get(self.app.sessions.focused).map(|c| c.uid) {
                        if let Some(tree) = self.app.sessions.layout.as_mut() {
                            let delta = if key.code == KeyCode::Char('>') { 0.05 } else { -0.05 };
                            tree.resize(uid, delta);
                            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
                            self.needs_full_redraw = true;
                        }
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Route a bracketed paste to the chat input or the focused session pane.
    fn handle_paste(&mut self, text: String) -> io::Result<()> {
        if self.app.active_view == View::Chat && !self.app.chat.copy_mode {
            self.app.chat.input.push_str(&text);
            self.app.chat.maybe_open_command_menu();
            self.local_view_dirty = true;
        } else if self.app.active_view == View::Sessions && self.app.sessions.terminal_mode {
            let bytes = text.into_bytes();
            let is_local_self = match (self.app.sessions.panes.get(self.app.sessions.focused), self.native_session.as_ref()) {
                (Some(cell), Some(server)) => match &cell.backend_type {
                    BackendType::CharonPane { socket_path } => socket_path == &server.socket_path().to_string_lossy().to_string(),
                    _ => false,
                },
                _ => false,
            };
            if is_local_self {
                if let Some(cell) = self.app.sessions.panes.get_mut(self.app.sessions.focused) {
                    cell.reset_viewport_scroll();
                }
                let saved_view = self.app.active_view;
                self.app.active_view = View::Chat;
                apply_native_input_bytes(&mut self.app, &bytes);
                self.app.active_view = saved_view;
                self.needs_full_redraw = true;
            } else if let Some(cell) = self.app.sessions.panes.get_mut(self.app.sessions.focused) {
                cell.reset_viewport_scroll();
                cell.write(&bytes)?;
            }
        }
        Ok(())
    }

    /// Handle scroll/click/drag events for the views that capture the mouse.
    fn handle_mouse(&mut self, mouse: event::MouseEvent) -> io::Result<()> {
        if self.app.active_view == View::Chat && self.app.chat.app_mouse_mode {
            let area = f1_mono::content_area(self.outer_w, self.outer_h);
            let lines = &self.cached_chat.visual.lines;
            match mouse.kind {
                MouseEventKind::ScrollUp => {
                    self.app.chat.context_menu = None;
                    if point_in_rect(area, mouse.column, mouse.row) {
                        let max_scroll = lines.len().saturating_sub(area.height as usize);
                        self.app.chat.scroll = (self.app.chat.scroll + 3).min(max_scroll);
                        self.local_view_dirty = true;
                    }
                }
                MouseEventKind::ScrollDown => {
                    self.app.chat.context_menu = None;
                    if point_in_rect(area, mouse.column, mouse.row) {
                        self.app.chat.scroll = self.app.chat.scroll.saturating_sub(3);
                        self.local_view_dirty = true;
                    }
                }
                // Mouse capture is on for scroll only.
                // Selection and copy are handled by the terminal natively.
                // Hold Shift to select text (bypasses mouse capture).
                _ => {}
            }
        } else if self.app.active_view == View::Sessions && self.app.sessions.app_mouse_mode {
            match mouse.kind {
                MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                    if let Some(pane_idx) = pane_at_point(&mut self.app, &self.session_rects, mouse.column, mouse.row) {
                        self.app.sessions.focused = pane_idx;
                        self.app.sessions.section = SessionsSection::Grid;
                        let _ = scroll_session_pane(&mut self.app, pane_idx, matches!(mouse.kind, MouseEventKind::ScrollUp), self.native_session.as_ref())?;
                        self.needs_full_redraw = true;
                    }
                }
                MouseEventKind::Down(_) | MouseEventKind::Drag(_) | MouseEventKind::Moved => {
                    if let Some(pane_idx) = pane_at_point(&mut self.app, &self.session_rects, mouse.column, mouse.row) {
                        if self.app.sessions.focused != pane_idx {
                            self.app.sessions.focused = pane_idx;
                            self.app.sessions.section = SessionsSection::Grid;
                            self.needs_full_redraw = true;
                        }
                    }
                }
                _ => {}
            }
        } else if self.app.active_view == View::InterAgent && self.app.inter_agent.app_mouse_mode {
            match mouse.kind {
                MouseEventKind::ScrollUp => {
                    if let Some(area) = inter_agent_stream_area(&self.app, self.outer_w, self.outer_h) {
                        if point_in_rect(area, mouse.column, mouse.row) {
                            self.app.inter_agent.event_scroll = self.app.inter_agent.event_scroll.saturating_add(3);
                            self.needs_full_redraw = true;
                        }
                    }
                }
                MouseEventKind::ScrollDown => {
                    if let Some(area) = inter_agent_stream_area(&self.app, self.outer_w, self.outer_h) {
                        if point_in_rect(area, mouse.column, mouse.row) {
                            self.app.inter_agent.event_scroll = self.app.inter_agent.event_scroll.saturating_sub(3);
                            self.needs_full_redraw = true;
                        }
                    }
                }
                MouseEventKind::Down(MouseButton::Left) => {
                    let rooms = payload_inter_agent_rooms(self.app.chat.refresh_payload.as_ref());
                    if let (Some(room), Some(area)) = (rooms.get(self.app.inter_agent.selected), inter_agent_stream_area(&self.app, self.outer_w, self.outer_h)) {
                        let rows = conversation_transcript_rows(room, area.width as usize);
                        if let Some(point) = transcript_point_at_mouse(&rows, area, self.app.inter_agent.event_scroll, mouse.column, mouse.row) {
                            self.app.inter_agent.transcript_anchor = Some(point);
                            self.app.inter_agent.transcript_focus = Some(point);
                            self.app.inter_agent.transcript_dragging = true;
                        } else {
                            self.app.inter_agent.transcript_anchor = None;
                            self.app.inter_agent.transcript_focus = None;
                            self.app.inter_agent.transcript_dragging = false;
                        }
                        self.needs_full_redraw = true;
                    }
                }
                MouseEventKind::Down(MouseButton::Right) | MouseEventKind::Up(MouseButton::Right) => {
                    let rooms = payload_inter_agent_rooms(self.app.chat.refresh_payload.as_ref());
                    let room_owned = rooms.get(self.app.inter_agent.selected).map(|room| (*room).clone());
                    if let (Some(room), Some(area)) = (room_owned, inter_agent_stream_area(&self.app, self.outer_w, self.outer_h)) {
                        let _ = copy_inter_agent_selection(&mut self.app, &room, area);
                        self.needs_full_redraw = true;
                    }
                }
                MouseEventKind::Drag(MouseButton::Left) => {
                    if self.app.inter_agent.transcript_dragging {
                        let rooms = payload_inter_agent_rooms(self.app.chat.refresh_payload.as_ref());
                        if let (Some(room), Some(area)) = (rooms.get(self.app.inter_agent.selected), inter_agent_stream_area(&self.app, self.outer_w, self.outer_h)) {
                            let rows = conversation_transcript_rows(room, area.width as usize);
                            let max_scroll = transcript_max_scroll(&rows, area);
                            if mouse.row < area.y {
                                self.app.inter_agent.event_scroll = (self.app.inter_agent.event_scroll + 1).min(max_scroll);
                            } else if mouse.row >= area.y.saturating_add(area.height) {
                                self.app.inter_agent.event_scroll = self.app.inter_agent.event_scroll.saturating_sub(1);
                            }
                            if let Some(point) = transcript_point_at_mouse(&rows, area, self.app.inter_agent.event_scroll, mouse.column, mouse.row) {
                                self.app.inter_agent.transcript_focus = Some(point);
                                self.needs_full_redraw = true;
                            }
                        }
                    }
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    if self.app.inter_agent.transcript_dragging {
                        self.app.inter_agent.transcript_dragging = false;
                        let rooms = payload_inter_agent_rooms(self.app.chat.refresh_payload.as_ref());
                        if let (Some(room), Some(area)) = (rooms.get(self.app.inter_agent.selected), inter_agent_stream_area(&self.app, self.outer_w, self.outer_h)) {
                            let rows = conversation_transcript_rows(room, area.width as usize);
                            if let Some(point) = transcript_point_at_mouse(&rows, area, self.app.inter_agent.event_scroll, mouse.column, mouse.row) {
                                self.app.inter_agent.transcript_focus = Some(point);
                            }
                        }
                        self.needs_full_redraw = true;
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Track a terminal resize: relayout panes and resize the double buffers.
    fn handle_resize(&mut self, w: u16, h: u16) -> io::Result<()> {
        self.outer_w = w;
        self.outer_h = h;
        self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
        self.front_buf.resize(self.outer_w, self.outer_h);
        self.back_buf.resize(self.outer_w, self.outer_h);
        self.needs_full_redraw = true;
        Ok(())
    }

    /// Drop panes whose backing process reached EOF and refocus/relayout.
    fn reap_closed_panes(&mut self) -> io::Result<()> {
        let before = self.app.sessions.panes.len();
        self.app.sessions.panes.retain(|c| !c.is_eof());
        if self.app.sessions.panes.len() != before {
            if self.app.sessions.focused >= self.app.sessions.panes.len() {
                self.app.sessions.focused = self.app.sessions.panes.len().saturating_sub(1);
            }
            self.session_rects = relayout_sessions(&mut self.app, self.outer_w, self.outer_h)?;
            self.needs_full_redraw = true;
        }
        Ok(())
    }
}
