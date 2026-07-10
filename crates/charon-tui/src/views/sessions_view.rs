//! F3 Sessions view: live VTE session grid, session list, pane layout and
//! synchronization with daemon / payload state.

use std::io::{self, Write};

use crossterm::{cursor, event::KeyCode, style, terminal as ct, QueueableCommand};
use serde_json::Value;

use crate::app::{App, SessionsSection, View};
use crate::backend;
use crate::config;
use crate::daemon;
use crate::daemon_client;
use crate::input::apply_native_input_bytes;
use crate::parser::AnsiParser;
use crate::terminal::TerminalState;
use crate::cli::LaunchMode;
use crate::grid::compute_grid;
use crate::layout;
use crate::native_session::NativeSessionServer;
use crate::render::{self, Rect};
use crate::session::{BackendType, SessionCell};

use super::dashboard_view::draw_dashboard;
use super::inter_agent_view::draw_inter_agent;
use super::{
    chat_view, draw_footer, draw_header, keep_index_visible, payload_agents, payload_projects,
    session_ids_match,
};

pub(crate) fn build_initial_sessions(mode: &LaunchMode, outer_w: u16, outer_h: u16) -> io::Result<Vec<SessionCell>> {
    let mut sessions = Vec::new();
    let next_id = 0u64;

    match mode {
        LaunchMode::ListSessions | LaunchMode::DaemonList | LaunchMode::DaemonUpgrade => {}
        LaunchMode::DaemonSpawn(cmd) => {
            daemon::ensure_running()?;
            let sock = daemon::control_socket();
            let (_, _, rects) = compute_grid(1, outer_w, outer_h.saturating_sub(2));
            if let Some(r) = rects.first() {
                let ephemeral = !config::active().persist_sessions;
                let sid = daemon_client::spawn_session(&sock, cmd, r.width, r.height, ephemeral, None)
                    .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("daemon spawn failed: {e}")))?;
                let title = cmd.first().map(|s| s.as_str()).unwrap_or("shell");
                let sock_str = sock.to_string_lossy();
                sessions.push(SessionCell::attach_daemon(next_id, title, &sid, &sock_str, r.width, r.height)?);
            }
        }
        LaunchMode::DaemonAttach(id) => {
            daemon::ensure_running()?;
            let sock = daemon::control_socket();
            let (_, _, rects) = compute_grid(1, outer_w, outer_h.saturating_sub(2));
            if let Some(r) = rects.first() {
                let sock_str = sock.to_string_lossy();
                sessions.push(SessionCell::attach_daemon(next_id, id, id, &sock_str, r.width, r.height)?);
            }
        }
        LaunchMode::DaemonRespawn(id) => {
            daemon::ensure_running()?;
            let sock = daemon::control_socket();
            daemon_client::respawn_session(&sock, id)
                .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("daemon respawn failed: {e}")))?;
            let (_, _, rects) = compute_grid(1, outer_w, outer_h.saturating_sub(2));
            if let Some(r) = rects.first() {
                let sock_str = sock.to_string_lossy();
                sessions.push(SessionCell::attach_daemon(next_id, id, id, &sock_str, r.width, r.height)?);
            }
        }
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

pub(crate) fn render_local_charon_preview<W: Write>(stdout: &mut W, app: &mut App, area: Rect, self_socket_to_hide: Option<&str>) -> io::Result<()> {
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

pub(crate) fn build_native_session_snapshot(app: &mut App, w: u16, h: u16, self_socket_to_hide: Option<&str>) -> Vec<u8> {
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




#[derive(Clone)]
#[allow(dead_code)] // mirrors agent JSON; not all fields read
pub(crate) struct SessionAgentMeta {
    pub(crate) id: String,
    pub(crate) agent_id: String,
    pub(crate) name: String,
    pub(crate) project: String,
    pub(crate) specialization: String,
    pub(crate) last_summary: String,
    pub(crate) tmux: String,
    pub(crate) status: String,
    pub(crate) source: String,
    pub(crate) process_target: String,
    pub(crate) live_session_id: String,
    pub(crate) session_label: String,
    pub(crate) transport: String,
    pub(crate) socket: String,
    pub(crate) server_id: String,
}

#[derive(Clone)]
pub(crate) enum SessionListRow {
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

pub(crate) fn compose_session_title(meta: &SessionAgentMeta) -> String {
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

pub(crate) fn session_agent_meta(payload: Option<&Value>) -> Vec<SessionAgentMeta> {
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

pub(crate) fn filtered_session_meta(app: &App) -> Vec<SessionAgentMeta> {
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

pub(crate) fn session_list_rows(app: &mut App) -> Vec<SessionListRow> {
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
    // Daemon-owned sessions, grouped by workspace (independent of the payload).
    let mut by_ws: std::collections::BTreeMap<String, Vec<crate::protocol::SessionInfo>> =
        std::collections::BTreeMap::new();
    for info in &app.sessions.daemon_sessions {
        if info.state == "exited" {
            continue;
        }
        let ws = if info.workspace.is_empty() { "default".to_string() } else { info.workspace.clone() };
        by_ws.entry(ws).or_default().push(info.clone());
    }
    for (ws, infos) in by_ws {
        let child_ids: Vec<String> = infos.iter().map(|i| i.id.clone()).collect();
        let header = format!("◈ {ws}");
        let collapsed = app.sessions.collapsed_agents.contains(&header);
        rows.push(SessionListRow::AgentHeader {
            name: header,
            project: String::new(),
            detail: "daemon".to_string(),
            count: infos.len(),
            session_ids: child_ids.clone(),
            collapsed,
        });
        if collapsed {
            session_ids.extend(child_ids);
        } else {
            for info in infos {
                let label = if info.title.is_empty() { info.id.clone() } else { info.title.clone() };
                session_ids.push(info.id.clone());
                rows.push(SessionListRow::Session { id: info.id, label, status: info.state });
            }
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

pub(crate) fn visible_session_agent_ids(app: &mut App) -> Vec<String> {
    let rows = session_list_rows(app);
    let has_sessions = rows.iter().any(|r| matches!(r, SessionListRow::Session { .. }));
    if !has_sessions {
        if app.sessions.backend_filter_pending {
            return vec![];
        }
        return app.sessions.panes.iter().enumerate().map(|(i, _)| format!("pane:{}", i)).collect();
    }
    rows.into_iter()
        .filter_map(|row| match row {
            SessionListRow::Session { id, .. } if app.sessions.visible_agents.contains(&id) => Some(id),
            _ => None,
        })
        .collect()
}

pub(crate) fn pane_agent_id(cell: &SessionCell, payload: Option<&Value>, idx: usize) -> String {
    // Daemon panes identify by their session id (matches the sidebar rows).
    if let BackendType::DaemonPane { session_id } = &cell.backend_type {
        return session_id.clone();
    }
    for m in session_agent_meta(payload) {
        let backend_match = match &cell.backend_type {
            BackendType::TmuxPane { session_name } => session_ids_match(&m.tmux, session_name),
            BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => session_ids_match(&m.tmux, session_id),
            BackendType::CharonPane { socket_path } => m.transport == "charon" && !m.socket.is_empty() && m.socket == *socket_path,
            BackendType::LocalPty | BackendType::DaemonPane { .. } => false,
        };
        if backend_match {
            return m.id;
        }
    }
    format!("pane:{}", idx)
}

/// The tab a pane belongs to: a daemon pane's daemon-reported tab, else "main".
pub(crate) fn pane_tab(app: &App, idx: usize) -> String {
    match app.sessions.panes.get(idx).map(|c| &c.backend_type) {
        Some(BackendType::DaemonPane { session_id }) => app
            .sessions
            .daemon_sessions
            .iter()
            .find(|s| &s.id == session_id)
            .map(|s| if s.tab.is_empty() { "main".to_string() } else { s.tab.clone() })
            .unwrap_or_else(|| "main".to_string()),
        _ => "main".to_string(),
    }
}

/// Distinct tabs across all panes, "main" first then the rest sorted.
pub(crate) fn grid_tabs(app: &App) -> Vec<String> {
    let mut tabs: Vec<String> = Vec::new();
    for i in 0..app.sessions.panes.len() {
        let t = pane_tab(app, i);
        if !tabs.contains(&t) {
            tabs.push(t);
        }
    }
    if !tabs.iter().any(|t| t == "main") {
        tabs.insert(0, "main".to_string());
    }
    tabs.sort_by(|a, b| match (a.as_str(), b.as_str()) {
        ("main", "main") => std::cmp::Ordering::Equal,
        ("main", _) => std::cmp::Ordering::Less,
        (_, "main") => std::cmp::Ordering::Greater,
        _ => a.cmp(b),
    });
    tabs
}

pub(crate) fn visible_pane_indices(app: &mut App) -> Vec<usize> {
    // Zoom: show only the focused pane fullscreen.
    if app.sessions.zoom && app.sessions.focused < app.sessions.panes.len() {
        return vec![app.sessions.focused];
    }
    // Keep the active tab valid (it may have emptied).
    let tabs = grid_tabs(app);
    if !tabs.contains(&app.sessions.active_tab) {
        app.sessions.active_tab = "main".to_string();
    }
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
    let mut base = if matched.is_empty() && !allowed.is_empty() && !app.sessions.backend_filter_pending {
        (0..app.sessions.panes.len()).collect()
    } else {
        matched
    };
    // Show only the active tab's panes. Only filter when more than one tab exists,
    // so single-tab setups behave exactly as before.
    if grid_tabs(app).len() > 1 {
        let active = app.sessions.active_tab.clone();
        base.retain(|i| pane_tab(app, *i) == active);
    }
    base
}

pub(crate) fn ensure_native_self_pane(app: &mut App, server: Option<&NativeSessionServer>, outer_w: u16, outer_h: u16) -> io::Result<bool> {
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

/// Split the focused pane: spawn a new daemon shell and place it beside the
/// focused pane in the manual layout (seeding the layout from the current grid if
/// none is active yet). Returns whether the grid changed.
pub(crate) fn split_focused_pane(app: &mut App, dir: layout::Dir, _outer_w: u16, _outer_h: u16) -> io::Result<bool> {
    let Some(focused_uid) = app.sessions.panes.get(app.sessions.focused).map(|c| c.uid) else {
        return Ok(false);
    };
    let pre_uids: Vec<u64> = visible_pane_indices(app)
        .iter()
        .filter_map(|i| app.sessions.panes.get(*i).map(|c| c.uid))
        .collect();
    daemon::ensure_running()?;
    let sock = daemon::control_socket();
    let sock_str = sock.to_string_lossy().to_string();
    let ephemeral = !config::active().persist_sessions;
    let tab = Some(app.sessions.active_tab.clone());
    let new_sid = daemon_client::spawn_session(&sock, &[], 80, 24, ephemeral, tab)
        .map_err(|e| io::Error::new(io::ErrorKind::Other, format!("split spawn failed: {e}")))?;
    let cell = SessionCell::attach_daemon(app.sessions.panes.len() as u64, &new_sid, &new_sid, &sock_str, 80, 24)?;
    let new_uid = cell.uid;
    // Make sure the new session is shown in the grid.
    app.sessions.visible_agents.insert(new_sid.clone());
    app.sessions.panes.push(cell);
    let base = app.sessions.layout.take().or_else(|| layout::Node::linear(&pre_uids));
    let tree = match base {
        Some(t) => t.split(focused_uid, new_uid, dir, 0.5),
        None => layout::Node::Leaf(new_uid),
    };
    app.sessions.layout = Some(tree);
    app.sessions.focused = app.sessions.panes.len() - 1;
    Ok(true)
}

/// Surface live `charond` sessions as panes in the F3 grid. Additive and
/// independent of the Python payload path: only runs when a daemon is already
/// running, attaches any session not yet shown, and skips exited ones.
pub(crate) fn sync_daemon_panes(app: &mut App, outer_w: u16, outer_h: u16) -> io::Result<bool> {
    if !daemon::is_running() {
        return Ok(false);
    }
    let sock = daemon::control_socket();
    let sessions = match daemon_client::list_sessions(&sock) {
        Ok(s) => s,
        Err(_) => return Ok(false),
    };
    let sock_str = sock.to_string_lossy().to_string();
    app.sessions.daemon_sessions = sessions.clone();
    let mut changed = false;
    for info in sessions {
        // Record state for border coloring (including exited sessions).
        app.sessions.daemon_states.insert(info.id.clone(), info.state.clone());
        if info.state == "exited" {
            continue;
        }
        let exists = app.sessions.panes.iter().any(|c| {
            matches!(&c.backend_type, BackendType::DaemonPane { session_id } if session_id == &info.id)
        });
        if exists {
            continue;
        }
        let idx = app.sessions.panes.len();
        let (_, _, rects) = compute_grid(
            (idx + 1).max(1),
            outer_w.saturating_sub(((outer_w as f32) * 0.125) as u16 + 2),
            outer_h.saturating_sub(2),
        );
        let r = rects.get(idx).copied().unwrap_or(Rect { x: 0, y: 0, width: 80, height: 24 });
        let base = if info.title.is_empty() { info.id.clone() } else { info.title.clone() };
        // Surface the workspace as a grouping prefix when it's not the default.
        let title = if info.workspace.is_empty() || info.workspace == "default" {
            base
        } else {
            format!("{}/{}", info.workspace, base)
        };
        if let Ok(cell) = SessionCell::attach_daemon(idx as u64, &title, &info.id, &sock_str, r.width.max(1), r.height.max(1)) {
            app.sessions.panes.push(cell);
            changed = true;
        }
    }
    Ok(changed)
}

pub(crate) fn sync_session_panes_from_payload(app: &mut App, outer_w: u16, outer_h: u16) -> io::Result<bool> {
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
                    BackendType::TmuxPane { session_name } => session_ids_match(session_name, &meta.tmux),
                    BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => session_ids_match(session_id, &meta.tmux),
                    BackendType::CharonPane { socket_path } => meta.transport == "charon" && !meta.socket.is_empty() && socket_path == &meta.socket,
                    BackendType::LocalPty | BackendType::DaemonPane { .. } => false,
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

pub(crate) fn project_names(payload: Option<&Value>) -> Vec<String> {
    let mut names: Vec<String> = payload_projects(payload)
        .into_iter()
        .filter_map(|p| p.get("name").and_then(|v| v.as_str()).map(|s| s.to_string()))
        .collect();
    names.sort();
    names.dedup();
    names
}

pub(crate) fn pane_at_point(app: &mut App, rects: &[Rect], x: u16, y: u16) -> Option<usize> {
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

pub(crate) fn scroll_session_pane(app: &mut App, pane_idx: usize, up: bool, native_session: Option<&NativeSessionServer>) -> io::Result<bool> {
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

pub(crate) fn next_grid_focus(current_pane: usize, visible: &[usize], rects: &[Rect], direction: KeyCode) -> Option<usize> {
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

pub(crate) fn session_grid_rects(app: &mut App, outer_w: u16, outer_h: u16) -> Vec<Rect> {
    let sidebar_w = ((outer_w as f32) * 0.125) as u16;
    let grid_x = 1 + sidebar_w.min(outer_w.saturating_sub(8));
    let grid_w = outer_w.saturating_sub(grid_x + 1);
    let grid_h = outer_h.saturating_sub(2);
    let visible = visible_pane_indices(app);

    // Manual split layout, if active: reconcile against the visible panes' uids and
    // place each pane per the layout tree. Falls back to auto-tile if it can't.
    if app.sessions.layout.is_some() {
        let visible_uids: Vec<u64> = visible
            .iter()
            .filter_map(|i| app.sessions.panes.get(*i).map(|c| c.uid))
            .collect();
        let reconciled = app.sessions.layout.take().and_then(|t| t.reconcile(&visible_uids));
        app.sessions.layout = reconciled.clone();
        if let Some(tree) = reconciled {
            let area = layout::Rect { x: grid_x, y: 1, width: grid_w, height: grid_h };
            let placed: std::collections::HashMap<u64, layout::Rect> = tree.compute(area, 1).into_iter().collect();
            return visible
                .iter()
                .map(|i| {
                    let uid = app.sessions.panes.get(*i).map(|c| c.uid).unwrap_or(0);
                    match placed.get(&uid) {
                        Some(r) => Rect { x: r.x, y: r.y, width: r.width, height: r.height },
                        None => Rect { x: grid_x, y: 1, width: grid_w.max(1), height: grid_h.max(1) },
                    }
                })
                .collect();
        }
    }

    let (_, _, rects) = compute_grid(visible.len().max(1), grid_w, grid_h);
    rects.into_iter().map(|mut r| { r.x += grid_x; r.y += 1; r }).collect()
}

pub(crate) fn relayout_sessions(app: &mut App, outer_w: u16, outer_h: u16) -> io::Result<Vec<Rect>> {
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

pub(crate) fn draw_sessions<W: Write>(stdout: &mut W, app: &mut App, rects: &[Rect], force_all: bool, w: u16, h: u16, self_socket_to_hide: Option<&str>) -> io::Result<()> {
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
            // Pin marker for persistent daemon panes.
            let title = if let BackendType::DaemonPane { session_id } = &backend_type {
                let persistent = app.sessions.daemon_sessions.iter().find(|s| &s.id == session_id).map(|s| !s.ephemeral).unwrap_or(false);
                if persistent { format!("📌 {title}") } else { title }
            } else {
                title
            };
            // Daemon panes color their border by the daemon-reported state.
            match &backend_type {
                BackendType::DaemonPane { session_id } if !focused => {
                    let state = app.sessions.daemon_states.get(session_id).map(String::as_str);
                    let theme = &config::active().theme;
                    let c = match state {
                        Some("working") => theme.status_working,
                        Some("blocked") => theme.status_blocked,
                        Some("exited") => config::Rgb(122, 130, 160),
                        _ => theme.status_idle,
                    };
                    render::render_border_colored(stdout, *area, &title, style::Color::Rgb { r: c.0, g: c.1, b: c.2 })?;
                }
                _ => render::render_border(stdout, *area, &title, focused)?,
            }
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
