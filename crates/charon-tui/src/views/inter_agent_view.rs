//! F4 Inter-agent view: conversation rooms, transcript stream, Libris graph,
//! and room pane management.

use std::io::{self, Write};

use crossterm::{cursor, style, QueueableCommand};
use serde_json::Value;

use crate::app::{App, TextPoint};
use crate::clipboard::copy_to_clipboard;
use crate::grid::compute_grid;
use crate::render::{self, Rect};
use crate::session::{BackendType, SessionCell};

use super::sessions_view::{compose_session_title, session_agent_meta, SessionAgentMeta};
use super::{keep_index_visible, payload_inter_agent_rooms, session_ids_match};

pub(crate) fn wrap_plain_text(s: &str, width: usize) -> Vec<String> {
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

pub(crate) fn copy_inter_agent_selection(app: &mut App, room: &Value, area: Rect) -> bool {
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

pub(crate) fn inter_agent_event_lines(room: &Value, event_scroll: usize, max_lines: usize, app_mouse_mode: bool) -> Vec<String> {
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
pub(crate) struct TranscriptRow {
    text: String,
    fg: style::Color,
    bg: style::Color,
}

pub(crate) fn conversation_transcript_rows(room: &Value, width: usize) -> Vec<TranscriptRow> {
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

pub(crate) fn inter_agent_stream_area(app: &App, w: u16, h: u16) -> Option<Rect> {
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

pub(crate) fn ordered_points(a: TextPoint, b: TextPoint) -> (TextPoint, TextPoint) {
    if (a.row, a.col) <= (b.row, b.col) { (a, b) } else { (b, a) }
}

pub(crate) fn transcript_selection_bounds(app: &App) -> Option<(TextPoint, TextPoint)> {
    let a = app.inter_agent.transcript_anchor?;
    let b = app.inter_agent.transcript_focus?;
    Some(ordered_points(a, b))
}

pub(crate) fn transcript_row_window_len(total_rows: usize, area: Rect, event_scroll: usize) -> (usize, usize) {
    let start = total_rows.saturating_sub(area.height as usize + event_scroll);
    let end = total_rows.saturating_sub(event_scroll);
    (start.min(total_rows), end.min(total_rows))
}

pub(crate) fn transcript_point_at_mouse(rows: &[TranscriptRow], area: Rect, event_scroll: usize, x: u16, y: u16) -> Option<TextPoint> {
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

pub(crate) fn transcript_max_scroll(rows: &[TranscriptRow], area: Rect) -> usize {
    rows.len().saturating_sub(area.height as usize)
}

pub(crate) fn transcript_selection_text(rows: &[TranscriptRow], bounds: (TextPoint, TextPoint)) -> String {
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

pub(crate) fn row_index_selected(row: usize, col: usize, start: TextPoint, end: TextPoint) -> bool {
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

pub(crate) fn draw_conversation_stream<W: Write>(stdout: &mut W, room: &Value, area: Rect, event_scroll: usize, selection: Option<(TextPoint, TextPoint)>, app_mouse_mode: bool) -> io::Result<()> {
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
pub(crate) struct LibrisGraphNode {
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
pub(crate) struct GraphPoint {
    x: u16,
    y: u16,
}

#[derive(Clone, Copy)]
pub(crate) struct GraphAnchors {
    center: GraphPoint,
    top: GraphPoint,
    bottom: GraphPoint,
}

pub(crate) fn libris_role_color(role: &str, active: bool) -> style::Color {
    let base = match role {
        "coordinator" => style::Color::Rgb { r: 196, g: 181, b: 253 },
        "researcher" => style::Color::Rgb { r: 103, g: 232, b: 249 },
        "judge" => style::Color::Rgb { r: 251, g: 191, b: 36 },
        "shade" => style::Color::Rgb { r: 148, g: 163, b: 184 },
        _ => style::Color::Rgb { r: 148, g: 163, b: 184 },
    };
    if active { base } else { style::Color::DarkGrey }
}

pub(crate) fn draw_box_text<W: Write>(stdout: &mut W, area: Rect, lines: &[String], color: style::Color) -> io::Result<()> {
    for (i, line) in lines.iter().take(area.height as usize).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        stdout.queue(style::SetForegroundColor(color))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(stdout, "{}{}", visible, " ".repeat((area.width as usize).saturating_sub(visible.chars().count())))?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    Ok(())
}

pub(crate) fn draw_vline<W: Write>(stdout: &mut W, x: u16, y1: u16, y2: u16, color: style::Color) -> io::Result<()> {
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

pub(crate) fn draw_hline<W: Write>(stdout: &mut W, x1: u16, x2: u16, y: u16, color: style::Color) -> io::Result<()> {
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

pub(crate) fn graph_anchors(rect: Rect) -> GraphAnchors {
    let cx = rect.x + rect.width / 2;
    let cy = rect.y + rect.height / 2;
    GraphAnchors {
        center: GraphPoint { x: cx, y: cy },
        top: GraphPoint { x: cx, y: rect.y.saturating_sub(1) },
        bottom: GraphPoint { x: cx, y: rect.y + rect.height },
    }
}

pub(crate) fn libris_edge_color(active_now: bool, activity_strength: f64) -> style::Color {
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

pub(crate) fn mid_u16(a: u16, b: u16) -> u16 {
    a.min(b) + (a.max(b) - a.min(b)) / 2
}

pub(crate) fn draw_libris_graph<W: Write>(stdout: &mut W, room: &Value, area: Rect, selected_node: usize) -> io::Result<Vec<LibrisGraphNode>> {
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

pub(crate) fn room_session_meta(room: &Value, payload: Option<&Value>) -> Vec<SessionAgentMeta> {
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
        .filter(|m| wanted.contains(&m.id) || wanted.iter().any(|w| session_ids_match(w, &m.tmux)))
        .collect()
}

#[derive(Clone)]
pub(crate) struct RoomPaneVisual {
    title: String,
    status: String,
    border_color: style::Color,
}

pub(crate) fn role_title(role: &str, fallback_name: &str, idx: usize) -> String {
    match role {
        "teacher" => "Hermes Teacher".to_string(),
        "student" => "Hermes Student".to_string(),
        "developer" => format!("Hermes Developer {}", idx + 1),
        "participant" => format!("Hermes Participant {}", idx + 1),
        _ if !fallback_name.trim().is_empty() => fallback_name.trim().to_string(),
        _ => format!("Hermes {}", idx + 1),
    }
}

pub(crate) fn room_pane_visuals(room: &Value) -> std::collections::HashMap<String, RoomPaneVisual> {
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

pub(crate) fn sync_inter_agent_room_panes(app: &mut App, room: &Value, outer_w: u16, outer_h: u16) -> io::Result<bool> {
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
        let visual = visuals.get(&meta.tmux).or_else(|| {
            if meta.tmux.starts_with("boat-") {
                visuals.get(meta.tmux.trim_start_matches("boat-"))
            } else {
                visuals.get(&format!("boat-{}", meta.tmux))
            }
        });
        let title = visual.map(|v| v.title.clone()).unwrap_or_else(|| compose_session_title(&meta));
        let existing_idx = app.inter_agent.room_panes.iter().position(|c| match &c.backend_type {
            BackendType::BoatPane { session_id } | BackendType::RemoteBoat { session_id, .. } => session_ids_match(session_id, &meta.tmux),
            BackendType::TmuxPane { session_name } => session_ids_match(session_name, &meta.tmux),
            BackendType::CharonPane { socket_path } => meta.transport == "charon" && !meta.socket.is_empty() && socket_path == &meta.socket,
            BackendType::LocalPty | BackendType::DaemonPane { .. } => false,
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

pub(crate) fn draw_room_panes<W: Write>(stdout: &mut W, app: &mut App, room: &Value, area: Rect, force_all: bool) -> io::Result<()> {
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
            BackendType::CharonPane { .. } | BackendType::LocalPty | BackendType::DaemonPane { .. } => None,
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

pub(crate) fn draw_delete_room_modal<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
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

pub(crate) fn draw_inter_agent<W: Write>(stdout: &mut W, app: &mut App, w: u16, h: u16) -> io::Result<()> {
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
