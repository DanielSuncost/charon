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
mod daemon;
mod daemon_client;
mod detect;
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

use backend::discover_sessions;
use chat::{ChatViewMode, LaunchOptions};
use f1_mono::F1MonoCache;
use grid::compute_grid;
use native_session::NativeSessionServer;
use session::{BackendType, SessionCell};

use clipboard::{copy_to_clipboard, read_from_clipboard};
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

    let mut last_render = Instant::now();
    let frame_duration = Duration::from_millis(16);
    let mut needs_full_redraw = true;
    let mut local_view_dirty = false;
    let mut cached_chat = F1MonoCache::default();
    let mut last_rowing_tick = Instant::now();
    let mut last_session_poll = Instant::now() - Duration::from_secs(1);
    let mut last_daemon_poll = Instant::now() - Duration::from_secs(2);
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
        // Daemon-owned sessions are independent of the Python payload; poll the
        // daemon's inventory on a slow timer and surface new sessions as panes.
        if app.active_view == View::Sessions && last_daemon_poll.elapsed() >= Duration::from_millis(1000) {
            last_daemon_poll = Instant::now();
            if sync_daemon_panes(&mut app, outer_w, outer_h)? {
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

        // Mouse capture is only enabled for Session Grid and Inter-Agent views
        // (clicking panes, drag-select). Chat view never captures mouse — terminal
        // handles selection, copy, right-click, paste natively. Scroll in chat uses
        // PgUp/PgDn/arrow keys.
        let want_mouse_capture =
            (app.active_view == View::Sessions && !app.sessions.terminal_mode && app.sessions.app_mouse_mode)
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
                                        // When input is empty, scroll conversation (handles
                                        // alternate scroll mode where wheel → arrow keys).
                                        // When input has text, cycle command history.
                                        if app.chat.input.is_empty() && app.chat.history_index.is_none() {
                                            app.chat.scroll = app.chat.scroll.saturating_add(3);
                                        } else {
                                            app.chat.history_up();
                                            app.chat.maybe_open_command_menu();
                                        }
                                        local_view_dirty = true;
                                    }
                                    KeyCode::Down => {
                                        if app.chat.input.is_empty() && app.chat.history_index.is_none() {
                                            app.chat.scroll = app.chat.scroll.saturating_sub(3);
                                        } else {
                                            app.chat.history_down();
                                            app.chat.maybe_open_command_menu();
                                        }
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
                                    // Manual splits (Grid section): | side-by-side, - stacked,
                                    // = reset to auto-tile, < / > resize the focused split.
                                    KeyCode::Char('|') if app.sessions.section == SessionsSection::Grid => {
                                        if split_focused_pane(&mut app, layout::Dir::Horizontal, outer_w, outer_h)? {
                                            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            needs_full_redraw = true;
                                        }
                                    }
                                    KeyCode::Char('-') if app.sessions.section == SessionsSection::Grid => {
                                        if split_focused_pane(&mut app, layout::Dir::Vertical, outer_w, outer_h)? {
                                            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            needs_full_redraw = true;
                                        }
                                    }
                                    KeyCode::Char('=') if app.sessions.section == SessionsSection::Grid => {
                                        app.sessions.layout = None;
                                        session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                        needs_full_redraw = true;
                                    }
                                    // Tabs: [ / ] switch the active tab, t creates a new tab.
                                    KeyCode::Char('[') | KeyCode::Char(']') if app.sessions.section == SessionsSection::Grid => {
                                        let tabs = grid_tabs(&app);
                                        if tabs.len() > 1 {
                                            let cur = tabs.iter().position(|t| *t == app.sessions.active_tab).unwrap_or(0);
                                            let next = if key.code == KeyCode::Char(']') {
                                                (cur + 1) % tabs.len()
                                            } else {
                                                (cur + tabs.len() - 1) % tabs.len()
                                            };
                                            app.sessions.active_tab = tabs[next].clone();
                                            let visible = visible_pane_indices(&mut app);
                                            if let Some(first) = visible.first() {
                                                app.sessions.focused = *first;
                                            }
                                            session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                            needs_full_redraw = true;
                                        }
                                    }
                                    KeyCode::Char('t') if app.sessions.section == SessionsSection::Grid => {
                                        let new_tab = format!("tab-{}", grid_tabs(&app).len() + 1);
                                        daemon::ensure_running()?;
                                        let sock = daemon::control_socket();
                                        let sock_str = sock.to_string_lossy().to_string();
                                        let ephemeral = !config::active().persist_sessions;
                                        if let Ok(sid) = daemon_client::spawn_session(&sock, &[], 80, 24, ephemeral, Some(new_tab.clone())) {
                                            app.sessions.visible_agents.insert(sid.clone());
                                            if let Ok(cell) = SessionCell::attach_daemon(app.sessions.panes.len() as u64, &sid, &sid, &sock_str, 80, 24) {
                                                app.sessions.panes.push(cell);
                                                app.sessions.active_tab = new_tab;
                                                app.sessions.layout = None; // fresh tab → auto-tile
                                                last_daemon_poll = Instant::now() - Duration::from_secs(2);
                                                session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                                needs_full_redraw = true;
                                            }
                                        }
                                    }
                                    // Zoom: show the focused pane fullscreen.
                                    KeyCode::Char('z') if app.sessions.section == SessionsSection::Grid => {
                                        app.sessions.zoom = !app.sessions.zoom;
                                        session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                        needs_full_redraw = true;
                                    }
                                    // Kill the focused daemon session (terminate + delete history).
                                    KeyCode::Char('X') if app.sessions.section == SessionsSection::Grid => {
                                        if let Some(BackendType::DaemonPane { session_id }) =
                                            app.sessions.panes.get(app.sessions.focused).map(|c| c.backend_type.clone())
                                        {
                                            let _ = daemon_client::kill_session(&daemon::control_socket(), &session_id);
                                        }
                                        if app.sessions.panes.len() > 1 {
                                            app.sessions.panes.remove(app.sessions.focused);
                                            if app.sessions.focused >= app.sessions.panes.len() {
                                                app.sessions.focused = app.sessions.panes.len().saturating_sub(1);
                                            }
                                        }
                                        app.sessions.zoom = false;
                                        session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                        needs_full_redraw = true;
                                    }
                                    // Pin/unpin the focused daemon pane (persist vs ephemeral).
                                    KeyCode::Char('p') if app.sessions.section == SessionsSection::Grid => {
                                        if let Some(BackendType::DaemonPane { session_id }) =
                                            app.sessions.panes.get(app.sessions.focused).map(|c| c.backend_type.clone())
                                        {
                                            let currently_ephemeral = app.sessions.daemon_sessions.iter()
                                                .find(|s| s.id == session_id).map(|s| s.ephemeral).unwrap_or(true);
                                            let _ = daemon_client::set_persist(&daemon::control_socket(), &session_id, currently_ephemeral);
                                            last_daemon_poll = Instant::now() - Duration::from_secs(2); // refresh soon
                                            needs_full_redraw = true;
                                        }
                                    }
                                    KeyCode::Char('<') | KeyCode::Char('>') if app.sessions.section == SessionsSection::Grid => {
                                        if let Some(uid) = app.sessions.panes.get(app.sessions.focused).map(|c| c.uid) {
                                            if let Some(tree) = app.sessions.layout.as_mut() {
                                                let delta = if key.code == KeyCode::Char('>') { 0.05 } else { -0.05 };
                                                tree.resize(uid, delta);
                                                session_rects = relayout_sessions(&mut app, outer_w, outer_h)?;
                                                needs_full_redraw = true;
                                            }
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
                            // Mouse capture is on for scroll only.
                            // Selection and copy are handled by the terminal natively.
                            // Hold Shift to select text (bypasses mouse capture).
                            _ => {}
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
    stdout.write_all(b"\x1b[?1007l")?;  // disable alternate scroll mode
    stdout.queue(LeaveAlternateScreen)?;
    stdout.flush()?;
    ct::disable_raw_mode()?;
    println!("charon-tui exited.");
    Ok(())
}
