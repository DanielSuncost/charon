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
        let summary = libris_delivery_summary(room);
        let (_, _, event_area) =
            libris_panel_areas(main_x, main_w, w, h, summary.state);
        Some(event_area)
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct GraphPoint {
    x: u16,
    y: u16,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct GraphAnchors {
    top: GraphPoint,
    bottom: GraphPoint,
    left: GraphPoint,
    right: GraphPoint,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum LibrisDeliveryState {
    Working,
    Ready,
    Incomplete,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct LibrisDeliveryArtifact {
    label: String,
    path: String,
    media_type: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct LibrisDeliverySummary {
    state: LibrisDeliveryState,
    topic_count: usize,
    primary_artifact: Option<LibrisDeliveryArtifact>,
    artifacts: Vec<LibrisDeliveryArtifact>,
    reason: String,
}

fn libris_delivery_artifact(value: Option<&Value>) -> Option<LibrisDeliveryArtifact> {
    let artifact = value?.as_object()?;
    let path = artifact
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if path.is_empty() {
        return None;
    }
    let label = artifact
        .get("label")
        .and_then(Value::as_str)
        .unwrap_or("Artifact")
        .trim();
    let media_type = artifact
        .get("media_type")
        .and_then(Value::as_str)
        .unwrap_or("application/octet-stream")
        .trim();
    Some(LibrisDeliveryArtifact {
        label: if label.is_empty() {
            "Artifact".to_string()
        } else {
            label.to_string()
        },
        path: path.to_string(),
        media_type: if media_type.is_empty() {
            "application/octet-stream".to_string()
        } else {
            media_type.to_string()
        },
    })
}

fn libris_delivery_summary(room: &Value) -> LibrisDeliverySummary {
    let manifest = room.get("delivery_manifest").and_then(Value::as_object);
    let manifest_status = manifest
        .and_then(|value| value.get("status"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let manifest_ready = manifest
        .and_then(|value| value.get("ready"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let topic_count = manifest
        .and_then(|value| value.get("topic_count"))
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(0);
    let primary_artifact = manifest
        .and_then(|value| value.get("primary_artifact"))
        .and_then(|value| libris_delivery_artifact(Some(value)));
    let raw_artifacts = manifest
        .and_then(|value| value.get("artifacts"))
        .and_then(Value::as_array);
    let artifacts = raw_artifacts
        .map(|values| {
            values
                .iter()
                .filter_map(|value| libris_delivery_artifact(Some(value)))
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let reason = manifest
        .and_then(|value| value.get("reason"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();

    let primary_is_absolute = primary_artifact
        .as_ref()
        .is_some_and(|artifact| std::path::Path::new(&artifact.path).is_absolute());
    let artifacts_are_complete_and_absolute = raw_artifacts.is_some_and(|values| {
        !values.is_empty()
            && artifacts.len() == values.len()
            && artifacts
                .iter()
                .all(|artifact| std::path::Path::new(&artifact.path).is_absolute())
    });
    let valid_ready = manifest_status == "ready"
        && manifest_ready
        && topic_count > 0
        && primary_is_absolute
        && artifacts_are_complete_and_absolute;

    let room_status = room.get("status").and_then(Value::as_str).unwrap_or("");
    let terminal_without_delivery = matches!(
        room_status,
        "delivered" | "failed" | "delivery_failed" | "budget_exhausted" | "stopped"
    );
    let invalid_delivery_claim =
        manifest_status == "ready" || manifest_ready || room_status == "delivered";
    let state = if valid_ready {
        LibrisDeliveryState::Ready
    } else if manifest_status == "incomplete" || terminal_without_delivery || invalid_delivery_claim
    {
        LibrisDeliveryState::Incomplete
    } else {
        LibrisDeliveryState::Working
    };

    LibrisDeliverySummary {
        state,
        topic_count,
        primary_artifact,
        artifacts,
        reason,
    }
}

fn libris_panel_areas(
    main_x: u16,
    main_w: u16,
    outer_w: u16,
    outer_h: u16,
    delivery_state: LibrisDeliveryState,
) -> (Rect, Rect, Rect) {
    let graph_area = Rect {
        x: main_x,
        y: 2,
        width: ((main_w as f32) * 0.62) as u16,
        height: outer_h.saturating_sub(4),
    };
    let detail_x = graph_area
        .x
        .saturating_add(graph_area.width)
        .saturating_add(2);
    let detail_w = outer_w.saturating_sub(detail_x.saturating_add(2));
    let info_h = if delivery_state == LibrisDeliveryState::Working {
        (outer_h.saturating_sub(4) / 2).max(8)
    } else {
        (outer_h.saturating_sub(4).saturating_mul(2) / 3).max(8)
    };
    let detail_area = Rect {
        x: detail_x,
        y: 2,
        width: detail_w,
        height: info_h.saturating_sub(1),
    };
    let event_area = Rect {
        x: detail_x,
        y: detail_area
            .y
            .saturating_add(detail_area.height)
            .saturating_add(1),
        width: detail_w,
        height: outer_h.saturating_sub(detail_area.height.saturating_add(5)),
    };
    (graph_area, detail_area, event_area)
}

fn libris_delivery_status_label(state: LibrisDeliveryState) -> &'static str {
    match state {
        LibrisDeliveryState::Working => "WORKING",
        LibrisDeliveryState::Ready => "READY",
        LibrisDeliveryState::Incomplete => "INCOMPLETE",
    }
}

fn libris_delivery_banner_text(summary: &LibrisDeliverySummary) -> Option<String> {
    let report_label = if summary.topic_count == 1 {
        "REPORT"
    } else {
        "REPORTS"
    };
    match summary.state {
        LibrisDeliveryState::Ready => Some(format!(
            "✓ DELIVERY READY • {} {}",
            summary.topic_count, report_label
        )),
        LibrisDeliveryState::Incomplete => Some(format!(
            "⚠ DELIVERY INCOMPLETE • {} {}",
            summary.topic_count, report_label
        )),
        LibrisDeliveryState::Working => None,
    }
}

fn libris_delivery_sidebar_marker(summary: &LibrisDeliverySummary) -> &'static str {
    match summary.state {
        LibrisDeliveryState::Ready => "✓",
        LibrisDeliveryState::Incomplete => "⚠",
        LibrisDeliveryState::Working => "",
    }
}

fn libris_delivery_graph_area(area: Rect, summary: &LibrisDeliverySummary) -> Rect {
    if libris_delivery_banner_text(summary).is_some() && area.height > 2 {
        Rect {
            height: area.height.saturating_sub(1),
            ..area
        }
    } else {
        area
    }
}

fn libris_delivery_detail_lines(summary: &LibrisDeliverySummary, width: usize) -> Vec<String> {
    let mut lines = vec![
        format!("delivery: {}", libris_delivery_status_label(summary.state)),
        format!("reports: {}", summary.topic_count),
    ];

    if summary.state == LibrisDeliveryState::Incomplete && !summary.reason.is_empty() {
        lines.push(format!("reason: {}", summary.reason));
    }

    match summary.primary_artifact.as_ref() {
        Some(primary) => {
            lines.push(format!(
                "primary: {} ({})",
                primary.label, primary.media_type
            ));
            for path_line in wrap_plain_text(&primary.path, width.saturating_sub(2).max(1)) {
                lines.push(format!("  {}", path_line));
            }
        }
        None => lines.push(format!(
            "primary: {}",
            if summary.state == LibrisDeliveryState::Working {
                "pending"
            } else {
                "missing"
            }
        )),
    }

    lines.push(format!("artifacts ({}):", summary.artifacts.len()));
    if summary.artifacts.is_empty() {
        lines.push(format!(
            "  {}",
            if summary.state == LibrisDeliveryState::Working {
                "pending"
            } else {
                "none"
            }
        ));
    } else {
        for artifact in &summary.artifacts {
            lines.push(format!("- {} ({})", artifact.label, artifact.media_type));
            for path_line in wrap_plain_text(&artifact.path, width.saturating_sub(2).max(1)) {
                lines.push(format!("  {}", path_line));
            }
        }
    }
    lines
}

fn detail_scroll_window(
    total_lines: usize,
    requested_scroll: usize,
    height: usize,
) -> (usize, usize) {
    if total_lines == 0 || height == 0 {
        return (0, 0);
    }
    let start = requested_scroll.min(total_lines.saturating_sub(height));
    let end = start.saturating_add(height).min(total_lines);
    (start, end)
}

fn draw_libris_delivery_banner<W: Write>(
    stdout: &mut W,
    area: Rect,
    summary: &LibrisDeliverySummary,
) -> io::Result<()> {
    let Some(text) = libris_delivery_banner_text(summary) else {
        return Ok(());
    };
    if area.width == 0 || area.height == 0 {
        return Ok(());
    }

    let visible = truncate_columns(&text, area.width);
    let visible_width = text_columns(&visible);
    let left_padding = area.width.saturating_sub(visible_width) / 2;
    let right_padding = area
        .width
        .saturating_sub(visible_width)
        .saturating_sub(left_padding);
    let (foreground, background) = match summary.state {
        LibrisDeliveryState::Ready => (
            style::Color::Rgb {
                r: 220,
                g: 252,
                b: 231,
            },
            style::Color::Rgb {
                r: 20,
                g: 83,
                b: 45,
            },
        ),
        LibrisDeliveryState::Incomplete => (
            style::Color::Rgb {
                r: 254,
                g: 243,
                b: 199,
            },
            style::Color::Rgb {
                r: 120,
                g: 53,
                b: 15,
            },
        ),
        LibrisDeliveryState::Working => return Ok(()),
    };
    let y = area.y.saturating_add(area.height.saturating_sub(1));
    stdout.queue(cursor::MoveTo(area.x, y))?;
    stdout.queue(style::SetForegroundColor(foreground))?;
    stdout.queue(style::SetBackgroundColor(background))?;
    stdout.queue(style::SetAttribute(style::Attribute::Bold))?;
    write!(
        stdout,
        "{}{}{}",
        " ".repeat(left_padding as usize),
        visible,
        " ".repeat(right_padding as usize)
    )?;
    stdout.queue(style::SetAttribute(style::Attribute::Reset))?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    stdout.queue(style::SetBackgroundColor(style::Color::Reset))?;
    Ok(())
}

pub(crate) fn libris_role_color(role: &str, active: bool) -> style::Color {
    let base = match role {
        "coordinator" => style::Color::Rgb {
            r: 196,
            g: 181,
            b: 253,
        },
        "researcher" => style::Color::Rgb {
            r: 103,
            g: 232,
            b: 249,
        },
        "judge" => style::Color::Rgb {
            r: 251,
            g: 191,
            b: 36,
        },
        "shade" => style::Color::Rgb {
            r: 148,
            g: 163,
            b: 184,
        },
        _ => style::Color::Rgb {
            r: 148,
            g: 163,
            b: 184,
        },
    };
    if active {
        base
    } else {
        style::Color::DarkGrey
    }
}

pub(crate) fn draw_box_text<W: Write>(
    stdout: &mut W,
    area: Rect,
    lines: &[String],
    color: style::Color,
) -> io::Result<()> {
    for (i, line) in lines.iter().take(area.height as usize).enumerate() {
        stdout.queue(cursor::MoveTo(area.x, area.y + i as u16))?;
        stdout.queue(style::SetForegroundColor(color))?;
        let visible: String = line.chars().take(area.width as usize).collect();
        write!(
            stdout,
            "{}{}",
            visible,
            " ".repeat((area.width as usize).saturating_sub(visible.chars().count()))
        )?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    }
    Ok(())
}

pub(crate) fn graph_anchors(rect: Rect) -> GraphAnchors {
    let cx = rect.x.saturating_add(rect.width / 2);
    let cy = rect.y.saturating_add(rect.height / 2);
    GraphAnchors {
        top: GraphPoint {
            x: cx,
            y: rect.y.saturating_sub(1),
        },
        bottom: GraphPoint {
            x: cx,
            y: rect.y.saturating_add(rect.height),
        },
        left: GraphPoint {
            x: rect.x.saturating_sub(1),
            y: cy,
        },
        right: GraphPoint {
            x: rect.x.saturating_add(rect.width),
            y: cy,
        },
    }
}

pub(crate) fn libris_edge_color(active_now: bool, activity_strength: f64) -> style::Color {
    if active_now || activity_strength >= 0.95 {
        style::Color::Rgb {
            r: 96,
            g: 165,
            b: 250,
        }
    } else if activity_strength >= 0.70 {
        style::Color::Rgb {
            r: 59,
            g: 130,
            b: 246,
        }
    } else if activity_strength >= 0.35 {
        style::Color::Rgb {
            r: 100,
            g: 116,
            b: 139,
        }
    } else {
        style::Color::DarkGrey
    }
}

pub(crate) fn mid_u16(a: u16, b: u16) -> u16 {
    a.min(b) + (a.max(b) - a.min(b)) / 2
}

const NODE_MIN_CONTENT_WIDTH: u16 = 8;
const NODE_MAX_CONTENT_WIDTH: u16 = 22;
const COORDINATOR_MAX_CONTENT_WIDTH: u16 = 30;
const NODE_CONTENT_HEIGHT: u16 = 1;
const TOPIC_MIN_FULL_WIDTH: u16 = 24;
const TOPIC_MIN_FULL_HEIGHT: u16 = 11;
const TOPIC_MAX_COLUMNS: u16 = 3;
const TOPIC_COLUMN_GAP: u16 = 4;
const TOPIC_ROW_GAP: u16 = 3;
const TOPIC_COMPACT_MIN_FULL_WIDTH: u16 = 16;
const TOPIC_COMPACT_MIN_FULL_HEIGHT: u16 = 8;
const TOPIC_COMPACT_COLUMN_GAP: u16 = 2;
const TOPIC_COMPACT_ROW_GAP: u16 = 1;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum TopicClusterDensity {
    Full,
    Compact,
}

#[derive(Clone, Copy, Debug)]
struct TopicClusterLayout {
    rect: Rect,
    row: u16,
    fanout_anchor: GraphPoint,
    density: TopicClusterDensity,
}

#[derive(Clone, Copy, Debug)]
struct NodePairLayout {
    first: Option<Rect>,
    second: Option<Rect>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct OrthogonalRoute {
    points: Vec<GraphPoint>,
}

fn text_columns(text: &str) -> u16 {
    text.chars().count().min(u16::MAX as usize) as u16
}

fn truncate_columns(text: &str, width: u16) -> String {
    text.chars().take(width as usize).collect()
}

fn graph_node_state_line(node: &LibrisGraphNode) -> String {
    let state = if node.phase.trim().is_empty() {
        node.status.as_str()
    } else {
        node.phase.as_str()
    };
    if state.trim().is_empty() {
        node.role.clone()
    } else {
        format!("{} • {}", node.role, state)
    }
}

fn compact_node_width(node: &LibrisGraphNode, cap: u16) -> u16 {
    let title_width = text_columns(&node.name).saturating_add(4);
    let state_width = text_columns(&graph_node_state_line(node));
    title_width
        .max(state_width)
        .max(NODE_MIN_CONTENT_WIDTH)
        .min(cap.max(1))
}

fn graph_box_fits(container: Rect, node: Rect) -> bool {
    let container_right = container.x.saturating_add(container.width);
    let container_bottom = container.y.saturating_add(container.height);
    node.x.saturating_sub(1) >= container.x
        && node.y.saturating_sub(1) >= container.y
        && node.x.saturating_add(node.width) < container_right
        && node.y.saturating_add(node.height) < container_bottom
}

fn centered_graph_box(
    container: Rect,
    border_top: u16,
    desired_content_width: u16,
    content_height: u16,
) -> Option<Rect> {
    if container.width < 3 || container.height < content_height.saturating_add(2) {
        return None;
    }
    let content_width = desired_content_width
        .max(1)
        .min(container.width.saturating_sub(2));
    let full_width = content_width.saturating_add(2);
    let border_left = container
        .x
        .saturating_add(container.width.saturating_sub(full_width) / 2);
    let rect = Rect {
        x: border_left.saturating_add(1),
        y: border_top.saturating_add(1),
        width: content_width,
        height: content_height,
    };
    graph_box_fits(container, rect).then_some(rect)
}

fn topic_grid_columns(
    area_width: u16,
    topic_count: usize,
    min_full_width: u16,
    column_gap: u16,
) -> u16 {
    if topic_count == 0 {
        return 0;
    }
    let usable_width = area_width.saturating_sub(4);
    let width_limited =
        usable_width.saturating_add(column_gap) / min_full_width.saturating_add(column_gap);
    width_limited
        .max(1)
        .min(TOPIC_MAX_COLUMNS)
        .min(topic_count.min(u16::MAX as usize) as u16)
}

fn layout_topic_clusters_with_density(
    area: Rect,
    topic_count: usize,
    first_border_y: u16,
    min_full_width: u16,
    min_full_height: u16,
    column_gap: u16,
    row_gap: u16,
    density: TopicClusterDensity,
) -> Vec<TopicClusterLayout> {
    if topic_count > u16::MAX as usize {
        return Vec::new();
    }
    let cols = topic_grid_columns(area.width, topic_count, min_full_width, column_gap);
    if cols == 0 || area.width < 8 || area.height < 4 {
        return Vec::new();
    }
    let rows = (topic_count as u16).saturating_add(cols - 1) / cols;
    let area_bottom_exclusive = area.y.saturating_add(area.height);
    let first_border_y = first_border_y.max(area.y);
    let available_height = area_bottom_exclusive.saturating_sub(first_border_y);
    let row_gaps = row_gap.saturating_mul(rows.saturating_sub(1));
    if available_height
        < rows
            .saturating_mul(min_full_height)
            .saturating_add(row_gaps)
    {
        return Vec::new();
    }

    // Reserve a left-side bus lane and one right-side cell so lower topic rows
    // can fan out without sending a connector through an earlier cluster.
    let grid_left = area.x.saturating_add(3);
    let grid_width = area.width.saturating_sub(4);
    let column_gaps = column_gap.saturating_mul(cols.saturating_sub(1));
    if grid_width < cols.saturating_mul(3).saturating_add(column_gaps) {
        return Vec::new();
    }
    let full_width = grid_width.saturating_sub(column_gaps) / cols;
    let full_height = available_height.saturating_sub(row_gaps) / rows;
    if full_width < 3 || full_height < min_full_height {
        return Vec::new();
    }

    let mut layouts = Vec::with_capacity(topic_count);
    for index in 0..topic_count {
        let col = (index as u16) % cols;
        let row = (index as u16) / cols;
        let border_left =
            grid_left.saturating_add(col.saturating_mul(full_width.saturating_add(column_gap)));
        let border_top =
            first_border_y.saturating_add(row.saturating_mul(full_height.saturating_add(row_gap)));
        let rect = Rect {
            x: border_left.saturating_add(1),
            y: border_top.saturating_add(1),
            width: full_width.saturating_sub(2),
            height: full_height.saturating_sub(2),
        };
        // The right-side port is intentionally kept clear of the cluster title.
        let fanout_anchor = GraphPoint {
            x: rect.x.saturating_add(rect.width).saturating_sub(2),
            y: rect.y.saturating_sub(1),
        };
        layouts.push(TopicClusterLayout {
            rect,
            row,
            fanout_anchor,
            density,
        });
    }
    layouts
}

fn layout_topic_clusters(
    area: Rect,
    topic_count: usize,
    first_border_y: u16,
) -> Vec<TopicClusterLayout> {
    let full = layout_topic_clusters_with_density(
        area,
        topic_count,
        first_border_y,
        TOPIC_MIN_FULL_WIDTH,
        TOPIC_MIN_FULL_HEIGHT,
        TOPIC_COLUMN_GAP,
        TOPIC_ROW_GAP,
        TopicClusterDensity::Full,
    );
    if full.len() == topic_count {
        return full;
    }

    layout_topic_clusters_with_density(
        area,
        topic_count,
        first_border_y,
        TOPIC_COMPACT_MIN_FULL_WIDTH,
        TOPIC_COMPACT_MIN_FULL_HEIGHT,
        TOPIC_COMPACT_COLUMN_GAP,
        TOPIC_COMPACT_ROW_GAP,
        TopicClusterDensity::Compact,
    )
}

fn layout_node_pair(
    container: Rect,
    first_desired_width: u16,
    second_desired_width: u16,
) -> NodePairLayout {
    let available_full_width = container.width.saturating_sub(2);
    let pair_gap = 3u16;
    let max_each_content = (available_full_width.saturating_sub(pair_gap) / 2).saturating_sub(2);
    if max_each_content >= NODE_MIN_CONTENT_WIDTH
        && container.height >= NODE_CONTENT_HEIGHT.saturating_add(3)
    {
        let first_width = first_desired_width
            .max(NODE_MIN_CONTENT_WIDTH)
            .min(max_each_content);
        let second_width = second_desired_width
            .max(NODE_MIN_CONTENT_WIDTH)
            .min(max_each_content);
        let first_full = first_width.saturating_add(2);
        let second_full = second_width.saturating_add(2);
        let first_border_left = container.x.saturating_add(1);
        let second_border_left = container
            .x
            .saturating_add(container.width)
            .saturating_sub(1)
            .saturating_sub(second_full);
        let first = Rect {
            x: first_border_left.saturating_add(1),
            y: container.y.saturating_add(1),
            width: first_width,
            height: NODE_CONTENT_HEIGHT,
        };
        let second = Rect {
            x: second_border_left.saturating_add(1),
            y: container.y.saturating_add(1),
            width: second_width,
            height: NODE_CONTENT_HEIGHT,
        };
        if first_border_left
            .saturating_add(first_full)
            .saturating_add(pair_gap)
            <= second_border_left
            && graph_box_fits(container, first)
            && graph_box_fits(container, second)
        {
            return NodePairLayout {
                first: Some(first),
                second: Some(second),
            };
        }
    }

    let first = centered_graph_box(
        container,
        container.y,
        first_desired_width,
        NODE_CONTENT_HEIGHT,
    );
    let second_border_top = first
        .map(|rect| rect.y.saturating_add(rect.height).saturating_add(1))
        .unwrap_or(container.y);
    let second = centered_graph_box(
        container,
        second_border_top,
        second_desired_width,
        NODE_CONTENT_HEIGHT,
    );
    NodePairLayout { first, second }
}

fn layout_shade_tray(
    container: Rect,
    node_bottom_boundary: u16,
    status_y: u16,
    desired_content_width: u16,
) -> Option<Rect> {
    let tray_border_top = node_bottom_boundary.saturating_add(1);
    centered_graph_box(
        container,
        tray_border_top,
        desired_content_width,
        NODE_CONTENT_HEIGHT,
    )
    .filter(|rect| rect.y.saturating_add(rect.height) < status_y)
}

fn route_between_boxes(source: GraphAnchors, target: GraphAnchors) -> Option<OrthogonalRoute> {
    let points = if source.bottom.y < target.top.y {
        let mid_y = mid_u16(source.bottom.y, target.top.y);
        vec![
            source.bottom,
            GraphPoint {
                x: source.bottom.x,
                y: mid_y,
            },
            GraphPoint {
                x: target.top.x,
                y: mid_y,
            },
            target.top,
        ]
    } else if target.bottom.y < source.top.y {
        let mid_y = mid_u16(target.bottom.y, source.top.y);
        vec![
            source.top,
            GraphPoint {
                x: source.top.x,
                y: mid_y,
            },
            GraphPoint {
                x: target.bottom.x,
                y: mid_y,
            },
            target.bottom,
        ]
    } else if source.right.x < target.left.x {
        let mid_x = mid_u16(source.right.x, target.left.x);
        vec![
            source.right,
            GraphPoint {
                x: mid_x,
                y: source.right.y,
            },
            GraphPoint {
                x: mid_x,
                y: target.left.y,
            },
            target.left,
        ]
    } else if target.right.x < source.left.x {
        let mid_x = mid_u16(target.right.x, source.left.x);
        vec![
            source.left,
            GraphPoint {
                x: mid_x,
                y: source.left.y,
            },
            GraphPoint {
                x: mid_x,
                y: target.right.y,
            },
            target.right,
        ]
    } else {
        return None;
    };

    let mut compact = Vec::with_capacity(points.len());
    for point in points {
        if compact.last() != Some(&point) {
            compact.push(point);
        }
    }
    (compact.len() >= 2).then_some(OrthogonalRoute { points: compact })
}

fn vertically_between(source: GraphAnchors, target: GraphAnchors, obstacle: Rect) -> bool {
    let obstacle = graph_anchors(obstacle);
    if source.bottom.y < target.top.y {
        obstacle.top.y > source.bottom.y && obstacle.bottom.y < target.top.y
    } else if target.bottom.y < source.top.y {
        obstacle.top.y > target.bottom.y && obstacle.bottom.y < source.top.y
    } else {
        false
    }
}

fn route_shade_pool_to_target(
    pool: GraphAnchors,
    target: GraphAnchors,
    cluster: Rect,
    obstacles: &[Rect],
) -> Option<OrthogonalRoute> {
    if !obstacles
        .iter()
        .any(|obstacle| vertically_between(pool, target, *obstacle))
    {
        return route_between_boxes(pool, target);
    }

    let right_lane = cluster.x.saturating_add(cluster.width.saturating_sub(1));
    let mut points = if right_lane > pool.right.x.max(target.right.x) {
        vec![
            pool.right,
            GraphPoint {
                x: right_lane,
                y: pool.right.y,
            },
            GraphPoint {
                x: right_lane,
                y: target.right.y,
            },
            target.right,
        ]
    } else {
        let left_lane = cluster.x;
        if left_lane >= pool.left.x.min(target.left.x) {
            return None;
        }
        vec![
            pool.left,
            GraphPoint {
                x: left_lane,
                y: pool.left.y,
            },
            GraphPoint {
                x: left_lane,
                y: target.left.y,
            },
            target.left,
        ]
    };
    points.dedup();
    (points.len() >= 2).then_some(OrthogonalRoute { points })
}

fn write_graph_glyph<W: Write>(
    stdout: &mut W,
    point: GraphPoint,
    glyph: char,
    color: style::Color,
) -> io::Result<()> {
    stdout.queue(cursor::MoveTo(point.x, point.y))?;
    stdout.queue(style::SetForegroundColor(color))?;
    write!(stdout, "{}", glyph)?;
    Ok(())
}

fn draw_heavy_vline<W: Write>(
    stdout: &mut W,
    x: u16,
    y1: u16,
    y2: u16,
    color: style::Color,
) -> io::Result<()> {
    for y in y1.min(y2)..=y1.max(y2) {
        write_graph_glyph(stdout, GraphPoint { x, y }, '┃', color)?;
    }
    Ok(())
}

fn draw_heavy_hline<W: Write>(
    stdout: &mut W,
    x1: u16,
    x2: u16,
    y: u16,
    color: style::Color,
) -> io::Result<()> {
    for x in x1.min(x2)..=x1.max(x2) {
        write_graph_glyph(stdout, GraphPoint { x, y }, '━', color)?;
    }
    Ok(())
}

fn heavy_junction_glyph(up: bool, down: bool, left: bool, right: bool) -> char {
    match (up, down, left, right) {
        (true, true, true, true) => '╋',
        (true, true, false, true) => '┣',
        (true, true, true, false) => '┫',
        (false, true, true, true) => '┳',
        (true, false, true, true) => '┻',
        (false, true, false, true) => '┏',
        (false, true, true, false) => '┓',
        (true, false, false, true) => '┗',
        (true, false, true, false) => '┛',
        (true, true, false, false) => '┃',
        (false, false, true, true) => '━',
        (true, false, false, false) | (false, true, false, false) => '┃',
        (false, false, true, false) | (false, false, false, true) => '━',
        _ => '◆',
    }
}

fn route_vertex_glyph(previous: GraphPoint, current: GraphPoint, next: GraphPoint) -> char {
    heavy_junction_glyph(
        previous.y < current.y || next.y < current.y,
        previous.y > current.y || next.y > current.y,
        previous.x < current.x || next.x < current.x,
        previous.x > current.x || next.x > current.x,
    )
}

fn route_endpoint_glyph(endpoint: GraphPoint, neighbor: GraphPoint) -> char {
    if neighbor.y > endpoint.y {
        '┳'
    } else if neighbor.y < endpoint.y {
        '┻'
    } else if neighbor.x > endpoint.x {
        '┣'
    } else {
        '┫'
    }
}

fn draw_orthogonal_route<W: Write>(
    stdout: &mut W,
    route: &OrthogonalRoute,
    color: style::Color,
) -> io::Result<()> {
    for segment in route.points.windows(2) {
        let first = segment[0];
        let second = segment[1];
        if first.x == second.x {
            draw_heavy_vline(stdout, first.x, first.y, second.y, color)?;
        } else if first.y == second.y {
            draw_heavy_hline(stdout, first.x, second.x, first.y, color)?;
        }
    }
    if let (Some(first), Some(second)) = (route.points.first(), route.points.get(1)) {
        write_graph_glyph(stdout, *first, route_endpoint_glyph(*first, *second), color)?;
    }
    if route.points.len() > 2 {
        for index in 1..route.points.len() - 1 {
            write_graph_glyph(
                stdout,
                route.points[index],
                route_vertex_glyph(
                    route.points[index - 1],
                    route.points[index],
                    route.points[index + 1],
                ),
                color,
            )?;
        }
    }
    if let (Some(last), Some(previous)) = (
        route.points.last(),
        route.points.get(route.points.len().saturating_sub(2)),
    ) {
        write_graph_glyph(stdout, *last, route_endpoint_glyph(*last, *previous), color)?;
    }
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn topic_fanout_lane_y(cluster: &TopicClusterLayout) -> u16 {
    let offset = match cluster.density {
        TopicClusterDensity::Full => 2,
        TopicClusterDensity::Compact => 1,
    };
    cluster.fanout_anchor.y.saturating_sub(offset)
}

fn draw_coordinator_fanout<W: Write>(
    stdout: &mut W,
    coordinator: GraphAnchors,
    clusters: &[TopicClusterLayout],
    color: style::Color,
) -> io::Result<()> {
    if clusters.is_empty() {
        return Ok(());
    }
    if clusters.len() == 1 {
        let target = clusters[0].fanout_anchor;
        let route = OrthogonalRoute {
            points: if coordinator.bottom.x == target.x {
                vec![coordinator.bottom, target]
            } else {
                let mid_y = mid_u16(coordinator.bottom.y, target.y);
                vec![
                    coordinator.bottom,
                    GraphPoint {
                        x: coordinator.bottom.x,
                        y: mid_y,
                    },
                    GraphPoint {
                        x: target.x,
                        y: mid_y,
                    },
                    target,
                ]
            },
        };
        draw_orthogonal_route(stdout, &route, color)?;
        return Ok(());
    }

    let max_row = clusters
        .iter()
        .map(|cluster| cluster.row)
        .max()
        .unwrap_or(0);
    let first_lane_y = clusters
        .iter()
        .filter(|cluster| cluster.row == 0)
        .map(topic_fanout_lane_y)
        .min()
        .unwrap_or(coordinator.bottom.y);
    let bus_x = clusters
        .iter()
        .map(|cluster| cluster.rect.x.saturating_sub(3))
        .min()
        .unwrap_or(coordinator.bottom.x);
    draw_heavy_vline(
        stdout,
        coordinator.bottom.x,
        coordinator.bottom.y,
        first_lane_y,
        color,
    )?;
    write_graph_glyph(stdout, coordinator.bottom, '┳', color)?;

    let mut last_lane_y = first_lane_y;
    let mut lane_bounds = Vec::new();
    for row in 0..=max_row {
        let row_clusters: Vec<&TopicClusterLayout> = clusters
            .iter()
            .filter(|cluster| cluster.row == row)
            .collect();
        if row_clusters.is_empty() {
            continue;
        }
        let lane_y = row_clusters
            .iter()
            .map(|cluster| topic_fanout_lane_y(cluster))
            .min()
            .unwrap_or(first_lane_y);
        last_lane_y = lane_y;
        let max_x = row_clusters
            .iter()
            .map(|cluster| cluster.fanout_anchor.x)
            .max()
            .unwrap_or(bus_x);
        draw_heavy_hline(stdout, bus_x, max_x, lane_y, color)?;
        for cluster in row_clusters {
            let target = cluster.fanout_anchor;
            draw_heavy_vline(stdout, target.x, lane_y, target.y, color)?;
            write_graph_glyph(
                stdout,
                GraphPoint {
                    x: target.x,
                    y: lane_y,
                },
                heavy_junction_glyph(false, target.y > lane_y, target.x > bus_x, target.x < max_x),
                color,
            )?;
            write_graph_glyph(stdout, target, '┻', color)?;
        }
        lane_bounds.push((row, lane_y, max_x));
    }
    if max_row > 0 {
        draw_heavy_vline(stdout, bus_x, first_lane_y, last_lane_y, color)?;
    }
    for (row, lane_y, max_x) in &lane_bounds {
        write_graph_glyph(
            stdout,
            GraphPoint {
                x: bus_x,
                y: *lane_y,
            },
            heavy_junction_glyph(*row > 0, *row < max_row, false, *max_x > bus_x),
            color,
        )?;
    }
    let first_max_x = lane_bounds
        .iter()
        .find(|(row, _, _)| *row == 0)
        .map(|(_, _, max_x)| *max_x)
        .unwrap_or(bus_x);
    let coordinator_x = coordinator.bottom.x;
    let coordinator_has_drop = clusters.iter().any(|cluster| {
        cluster.row == 0
            && cluster.fanout_anchor.x == coordinator_x
            && cluster.fanout_anchor.y > first_lane_y
    });
    write_graph_glyph(
        stdout,
        GraphPoint {
            x: coordinator_x,
            y: first_lane_y,
        },
        heavy_junction_glyph(
            first_lane_y > coordinator.bottom.y,
            coordinator_has_drop || (coordinator_x == bus_x && max_row > 0),
            coordinator_x > bus_x && coordinator_x <= first_max_x,
            coordinator_x < first_max_x && coordinator_x >= bus_x,
        ),
        color,
    )?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

fn render_compact_node<W: Write>(
    stdout: &mut W,
    node: &LibrisGraphNode,
    rect: Rect,
    selected: bool,
) -> io::Result<()> {
    let title = truncate_columns(&node.name, rect.width.saturating_sub(4));
    render::render_border_colored(
        stdout,
        rect,
        &title,
        libris_role_color(&node.role, selected),
    )?;
    draw_box_text(
        stdout,
        rect,
        &[truncate_columns(&graph_node_state_line(node), rect.width)],
        style::Color::Rgb {
            r: 226,
            g: 232,
            b: 240,
        },
    )
}

pub(crate) fn draw_libris_graph<W: Write>(
    stdout: &mut W,
    room: &Value,
    area: Rect,
    selected_node: usize,
) -> io::Result<Vec<LibrisGraphNode>> {
    let nodes = room
        .get("nodes")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let topics = room
        .get("topics")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let edges = room
        .get("edges")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let graph_nodes: Vec<LibrisGraphNode> = nodes
        .iter()
        .map(|n| LibrisGraphNode {
            agent_id: n
                .get("agent_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            name: n
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("agent")
                .to_string(),
            role: n
                .get("role")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            status: n
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("idle")
                .to_string(),
            phase: n
                .get("phase")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            topic_slug: n
                .get("topic_slug")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            phase_summary: n
                .get("phase_summary")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            live_line: n
                .get("live_line")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        })
        .collect();

    let delivery_summary = libris_delivery_summary(room);
    draw_libris_delivery_banner(stdout, area, &delivery_summary)?;
    let area = libris_delivery_graph_area(area, &delivery_summary);
    if area.width < 8 || area.height < 4 {
        return Ok(graph_nodes);
    }

    let coordinator = graph_nodes
        .iter()
        .find(|node| node.role == "coordinator")
        .cloned();
    let mut node_anchors: std::collections::HashMap<String, GraphAnchors> =
        std::collections::HashMap::new();
    let mut coordinator_anchors = None;
    let mut cluster_start_y = area.y.saturating_add(1);

    if let Some(coord) = coordinator.as_ref() {
        let width = compact_node_width(coord, COORDINATOR_MAX_CONTENT_WIDTH)
            .min(area.width.saturating_sub(2));
        if let Some(rect) = centered_graph_box(area, area.y, width, NODE_CONTENT_HEIGHT) {
            let coord_idx = graph_nodes
                .iter()
                .position(|node| node.agent_id == coord.agent_id)
                .unwrap_or(usize::MAX);
            render_compact_node(stdout, coord, rect, coord_idx == selected_node)?;
            let anchors = graph_anchors(rect);
            node_anchors.insert(coord.agent_id.clone(), anchors);
            coordinator_anchors = Some(anchors);
            cluster_start_y = anchors.bottom.y.saturating_add(4);
        }
    }

    if topics.is_empty() {
        let message_y = cluster_start_y.min(area.y.saturating_add(area.height - 1));
        stdout.queue(cursor::MoveTo(area.x.saturating_add(2), message_y))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(
            stdout,
            "{}",
            truncate_columns(
                "Waiting for Libris topic clusters\u{2026}",
                area.width.saturating_sub(3),
            )
        )?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        return Ok(graph_nodes);
    }

    let cluster_layouts = layout_topic_clusters(area, topics.len(), cluster_start_y);
    if cluster_layouts.len() != topics.len() {
        let message_y = cluster_start_y.min(area.y.saturating_add(area.height - 1));
        stdout.queue(cursor::MoveTo(area.x.saturating_add(1), message_y))?;
        stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
        write!(
            stdout,
            "{}",
            truncate_columns(
                "Graph compacted: enlarge the terminal to inspect topic nodes.",
                area.width.saturating_sub(2),
            )
        )?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        return Ok(graph_nodes);
    }

    for (topic, layout) in topics.iter().zip(cluster_layouts.iter()) {
        let cluster_rect = layout.rect;
        let topic_title = topic
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("topic");
        let topic_status = topic.get("status").and_then(|v| v.as_str()).unwrap_or("");
        let title_display = if topic_status.is_empty() {
            topic_title.to_string()
        } else {
            format!("{} ({})", topic_title, topic_status)
        };
        // Leave a visible connector port at the right end of the top boundary.
        let title_reserve = match layout.density {
            TopicClusterDensity::Full => 7,
            TopicClusterDensity::Compact => 5,
        };
        let title_trunc = truncate_columns(
            &title_display,
            cluster_rect.width.saturating_sub(title_reserve),
        );
        render::render_border_colored(
            stdout,
            cluster_rect,
            &title_trunc,
            style::Color::Rgb {
                r: 80,
                g: 70,
                b: 120,
            },
        )?;

        let slug = topic
            .get("topic_slug")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let researcher = graph_nodes
            .iter()
            .find(|node| node.role == "researcher" && node.topic_slug == slug)
            .cloned();
        let judge = graph_nodes
            .iter()
            .find(|node| node.role == "judge" && node.topic_slug == slug)
            .cloned();
        let shades: Vec<&LibrisGraphNode> = graph_nodes
            .iter()
            .filter(|node| node.role == "shade" && node.topic_slug == slug)
            .collect();
        let status_y = cluster_rect
            .y
            .saturating_add(cluster_rect.height.saturating_sub(1));

        let mut node_bottom_boundary = cluster_rect.y;
        let mut researcher_rect = None;
        let mut judge_rect = None;
        match (researcher.as_ref(), judge.as_ref()) {
            (Some(researcher), Some(judge)) => {
                let pair = layout_node_pair(
                    cluster_rect,
                    compact_node_width(researcher, NODE_MAX_CONTENT_WIDTH),
                    compact_node_width(judge, NODE_MAX_CONTENT_WIDTH),
                );
                researcher_rect = pair.first;
                judge_rect = pair.second;
            }
            (Some(researcher), None) => {
                researcher_rect = centered_graph_box(
                    cluster_rect,
                    cluster_rect.y,
                    compact_node_width(researcher, NODE_MAX_CONTENT_WIDTH),
                    NODE_CONTENT_HEIGHT,
                );
            }
            (None, Some(judge)) => {
                judge_rect = centered_graph_box(
                    cluster_rect,
                    cluster_rect.y,
                    compact_node_width(judge, NODE_MAX_CONTENT_WIDTH),
                    NODE_CONTENT_HEIGHT,
                );
            }
            (None, None) => {}
        }

        if let (Some(node), Some(rect)) = (researcher.as_ref(), researcher_rect) {
            let index = graph_nodes
                .iter()
                .position(|candidate| candidate.agent_id == node.agent_id)
                .unwrap_or(usize::MAX);
            render_compact_node(stdout, node, rect, index == selected_node)?;
            let anchors = graph_anchors(rect);
            node_bottom_boundary = node_bottom_boundary.max(anchors.bottom.y);
            node_anchors.insert(node.agent_id.clone(), anchors);
        }
        if let (Some(node), Some(rect)) = (judge.as_ref(), judge_rect) {
            let index = graph_nodes
                .iter()
                .position(|candidate| candidate.agent_id == node.agent_id)
                .unwrap_or(usize::MAX);
            render_compact_node(stdout, node, rect, index == selected_node)?;
            let anchors = graph_anchors(rect);
            node_bottom_boundary = node_bottom_boundary.max(anchors.bottom.y);
            node_anchors.insert(node.agent_id.clone(), anchors);
        }

        let shade_active_count = shades
            .iter()
            .filter(|shade| {
                edges.iter().any(|edge| {
                    (edge
                        .get("from_agent_id")
                        .and_then(|value| value.as_str())
                        .unwrap_or("")
                        == shade.agent_id
                        || edge
                            .get("to_agent_id")
                            .and_then(|value| value.as_str())
                            .unwrap_or("")
                            == shade.agent_id)
                        && edge
                            .get("active_now")
                            .and_then(|value| value.as_bool())
                            .unwrap_or(false)
                })
            })
            .count();
        if !shades.is_empty() {
            let header = format!(
                "shade pool: {} • {} active",
                shades.len(),
                shade_active_count
            );
            let names = shades
                .iter()
                .map(|shade| shade.name.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            let desired_width = text_columns(&header)
                .saturating_add(4)
                .max(text_columns(&names))
                .max(NODE_MIN_CONTENT_WIDTH)
                .min(NODE_MAX_CONTENT_WIDTH.saturating_add(4));
            let shade_tray_rect =
                layout_shade_tray(cluster_rect, node_bottom_boundary, status_y, desired_width);
            if let Some(rect) = shade_tray_rect {
                let tray_color = if shade_active_count > 0 {
                    style::Color::Rgb {
                        r: 148,
                        g: 163,
                        b: 184,
                    }
                } else {
                    style::Color::DarkGrey
                };
                render::render_border_colored(
                    stdout,
                    rect,
                    &truncate_columns(&header, rect.width.saturating_sub(4)),
                    tray_color,
                )?;
                draw_box_text(
                    stdout,
                    rect,
                    &[truncate_columns(&names, rect.width)],
                    style::Color::Rgb {
                        r: 120,
                        g: 130,
                        b: 150,
                    },
                )?;
                let anchors = graph_anchors(rect);

                let detail_y = anchors.bottom.y.saturating_add(2);
                let detail_rows = status_y.saturating_sub(detail_y);
                for (shade_index, shade) in shades.iter().enumerate().take(detail_rows as usize) {
                    let index = graph_nodes
                        .iter()
                        .position(|node| node.agent_id == shade.agent_id)
                        .unwrap_or(usize::MAX);
                    let shade_line = format!(
                        "{} {} • {}",
                        if index == selected_node { "▸" } else { "·" },
                        shade.name,
                        if shade.phase.is_empty() {
                            shade.status.as_str()
                        } else {
                            shade.phase.as_str()
                        },
                    );
                    stdout.queue(cursor::MoveTo(
                        cluster_rect.x.saturating_add(1),
                        detail_y.saturating_add(shade_index as u16),
                    ))?;
                    stdout.queue(style::SetForegroundColor(if index == selected_node {
                        style::Color::Rgb {
                            r: 148,
                            g: 163,
                            b: 184,
                        }
                    } else {
                        style::Color::DarkGrey
                    }))?;
                    write!(
                        stdout,
                        "{}",
                        truncate_columns(&shade_line, cluster_rect.width.saturating_sub(2))
                    )?;
                }
                stdout.queue(style::SetForegroundColor(style::Color::Reset))?;

                // The tray represents the whole shade pool. Collapse all
                // individual shade exchanges into at most one semantic route
                // per visible target. The side-lane fallback keeps a stacked
                // sibling out of the route without changing the endpoint.
                for (target, target_rect, sibling_rect) in [
                    (researcher.as_ref(), researcher_rect, judge_rect),
                    (judge.as_ref(), judge_rect, researcher_rect),
                ] {
                    let (Some(target), Some(target_rect)) = (target, target_rect) else {
                        continue;
                    };
                    let relevant_edges: Vec<&Value> = edges
                        .iter()
                        .filter(|edge| {
                            let from_id = edge
                                .get("from_agent_id")
                                .and_then(Value::as_str)
                                .unwrap_or("");
                            let to_id = edge
                                .get("to_agent_id")
                                .and_then(Value::as_str)
                                .unwrap_or("");
                            let from_shade = shades.iter().any(|shade| shade.agent_id == from_id);
                            let to_shade = shades.iter().any(|shade| shade.agent_id == to_id);
                            (from_shade && to_id == target.agent_id)
                                || (to_shade && from_id == target.agent_id)
                        })
                        .collect();
                    if relevant_edges.is_empty() {
                        continue;
                    }
                    let active = relevant_edges.iter().any(|edge| {
                        edge.get("active_now")
                            .and_then(Value::as_bool)
                            .unwrap_or(false)
                    });
                    let strength = relevant_edges
                        .iter()
                        .filter_map(|edge| edge.get("activity_strength").and_then(Value::as_f64))
                        .fold(0.0f64, f64::max);
                    let obstacles: Vec<Rect> = sibling_rect.into_iter().collect();
                    if let Some(route) = route_shade_pool_to_target(
                        anchors,
                        graph_anchors(target_rect),
                        cluster_rect,
                        &obstacles,
                    ) {
                        draw_orthogonal_route(stdout, &route, libris_edge_color(active, strength))?;
                    }
                }
            }
        }

        let topic_edges: Vec<&Value> = edges
            .iter()
            .filter(|edge| {
                edge.get("topic_slug")
                    .and_then(|value| value.as_str())
                    .unwrap_or("")
                    == slug
            })
            .collect();
        let active_count = topic_edges
            .iter()
            .filter(|edge| {
                edge.get("active_now")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(false)
            })
            .count();
        if status_y > node_bottom_boundary {
            let status_line = if layout.density == TopicClusterDensity::Compact {
                if shades.is_empty() {
                    format!("{} links • {} active", topic_edges.len(), active_count)
                } else {
                    format!(
                        "{} shades • {} links • {} active",
                        shades.len(),
                        topic_edges.len(),
                        active_count
                    )
                }
            } else {
                let shade_suffix = if shades.is_empty() {
                    String::new()
                } else {
                    format!(" • {} shades", shades.len())
                };
                format!(
                    "{} links • {} active{}",
                    topic_edges.len(),
                    active_count,
                    shade_suffix
                )
            };
            stdout.queue(cursor::MoveTo(cluster_rect.x.saturating_add(1), status_y))?;
            stdout.queue(style::SetForegroundColor(if active_count > 0 {
                style::Color::Rgb {
                    r: 34,
                    g: 197,
                    b: 94,
                }
            } else {
                style::Color::DarkGrey
            }))?;
            write!(
                stdout,
                "{}",
                truncate_columns(&status_line, cluster_rect.width.saturating_sub(2))
            )?;
            stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        }

        // A semantic researcher/judge link is routed from exact side or
        // top/bottom boundary anchors; no fixed-width ASCII bridge is assumed.
        if let (Some(_), Some(_), Some(researcher_rect), Some(judge_rect)) = (
            researcher.as_ref(),
            judge.as_ref(),
            researcher_rect,
            judge_rect,
        ) {
            let relevant_edges: Vec<&Value> = edges
                .iter()
                .filter(|edge| {
                    edge.get("topic_slug")
                        .and_then(|value| value.as_str())
                        .unwrap_or("")
                        == slug
                        && ((edge
                            .get("from_role")
                            .and_then(|value| value.as_str())
                            .unwrap_or("")
                            == "researcher"
                            && edge
                                .get("to_role")
                                .and_then(|value| value.as_str())
                                .unwrap_or("")
                                == "judge")
                            || (edge
                                .get("from_role")
                                .and_then(|value| value.as_str())
                                .unwrap_or("")
                                == "judge"
                                && edge
                                    .get("to_role")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or("")
                                    == "researcher"))
                })
                .collect();
            let active = relevant_edges.iter().any(|edge| {
                edge.get("active_now")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(false)
            });
            let strength = relevant_edges
                .iter()
                .filter_map(|edge| {
                    edge.get("activity_strength")
                        .and_then(|value| value.as_f64())
                })
                .fold(0.0f64, f64::max);
            if let Some(route) =
                route_between_boxes(graph_anchors(researcher_rect), graph_anchors(judge_rect))
            {
                draw_orthogonal_route(stdout, &route, libris_edge_color(active, strength))?;
            }
        }
    }

    if let Some(coordinator) = coordinator_anchors {
        let active = edges.iter().any(|edge| {
            edge.get("from_role")
                .and_then(|value| value.as_str())
                .unwrap_or("")
                == "coordinator"
                && edge
                    .get("active_now")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(false)
        });
        draw_coordinator_fanout(
            stdout,
            coordinator,
            &cluster_layouts,
            if active {
                style::Color::Rgb {
                    r: 96,
                    g: 165,
                    b: 250,
                }
            } else {
                style::Color::Rgb {
                    r: 91,
                    g: 76,
                    b: 140,
                }
            },
        )?;
    }

    // Draw remaining communication links. Coordinator links are represented by
    // the collision-free topic fanout, and researcher/judge links were drawn
    // within their topic cluster above.
    for edge in &edges {
        let source_id = edge
            .get("from_agent_id")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        let target_id = edge
            .get("to_agent_id")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        let source_role = edge
            .get("from_role")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        let target_role = edge
            .get("to_role")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        if source_role == "coordinator"
            || target_role == "coordinator"
            || source_role == "shade"
            || target_role == "shade"
            || (source_role == "researcher" && target_role == "judge")
            || (source_role == "judge" && target_role == "researcher")
        {
            continue;
        }
        let Some(source) = node_anchors.get(source_id).copied() else {
            continue;
        };
        let Some(target) = node_anchors.get(target_id).copied() else {
            continue;
        };
        let Some(route) = route_between_boxes(source, target) else {
            continue;
        };
        let active = edge
            .get("active_now")
            .and_then(|value| value.as_bool())
            .unwrap_or(false);
        let strength = edge
            .get("activity_strength")
            .and_then(|value| value.as_f64())
            .unwrap_or(0.15);
        draw_orthogonal_route(stdout, &route, libris_edge_color(active, strength))?;
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
    render::render_border(
        stdout,
        list_area,
        "coordination",
        !app.inter_agent.graph_focus && !app.inter_agent.detail_focus,
    )?;

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
        let delivery_summary = (kind == "libris").then(|| libris_delivery_summary(room));
        let delivery_marker = delivery_summary
            .as_ref()
            .map(libris_delivery_sidebar_marker)
            .unwrap_or("");
        let kind_label = if delivery_marker.is_empty() {
            kind.to_string()
        } else {
            format!("{} {}", delivery_marker, kind)
        };
        let line = if project_name.is_empty() {
            format!("{} {}: {}", if i == app.inter_agent.selected { "▸" } else { " " }, kind_label, title)
        } else {
            format!("{} {}: {} [{}]", if i == app.inter_agent.selected { "▸" } else { " " }, kind_label, title, project_name)
        };
        stdout.queue(cursor::MoveTo(list_area.x, list_area.y + row as u16))?;
        let room_color = if i == app.inter_agent.selected {
            style::Color::Rgb { r: 212, g: 196, b: 168 }
        } else {
            match delivery_summary.as_ref().map(|summary| summary.state) {
                Some(LibrisDeliveryState::Ready) => style::Color::Rgb { r: 74, g: 222, b: 128 },
                Some(LibrisDeliveryState::Incomplete) => style::Color::Rgb { r: 251, g: 191, b: 36 },
                _ => style::Color::DarkGrey,
            }
        };
        stdout.queue(style::SetForegroundColor(room_color))?;
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
            let delivery_summary = libris_delivery_summary(room);
            let (graph_area, node_area, event_area) =
                libris_panel_areas(main_x, main_w, w, h, delivery_summary.state);
            render::render_border(stdout, graph_area, if app.inter_agent.graph_focus { "graph *" } else { "graph" }, app.inter_agent.graph_focus)?;
            let detail_title = match (app.inter_agent.topic_detail, app.inter_agent.detail_focus) {
                (true, true) => "topic details *",
                (true, false) => "topic details",
                (false, true) => "details *",
                (false, false) => "details",
            };
            render::render_border(stdout, node_area, detail_title, app.inter_agent.detail_focus)?;
            render::render_border(stdout, event_area, "events", false)?;

            let graph_nodes = draw_libris_graph(stdout, room, graph_area, app.inter_agent.selected_node)?;
            if app.inter_agent.selected_node >= graph_nodes.len() && !graph_nodes.is_empty() {
                app.inter_agent.selected_node = graph_nodes.len() - 1;
            }
            let mut detail_lines =
                libris_delivery_detail_lines(&delivery_summary, node_area.width as usize);
            detail_lines.push(String::new());
            if let Some(node) = graph_nodes.get(app.inter_agent.selected_node) {
                let budget = room.get("budget_status").and_then(|v| v.as_object());
                let topic = if node.topic_slug.is_empty() {
                    None
                } else {
                    room.get("topics").and_then(|v| v.as_array()).and_then(|arr| arr.iter().find(|t| t.get("topic_slug").and_then(|v| v.as_str()) == Some(node.topic_slug.as_str())))
                };
                let promising_sources = room.get("promising_sources").and_then(|v| v.as_array()).cloned().unwrap_or_default();

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
                } else {
                    detail_lines.push(node.name.clone());
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
                    if !node.phase_summary.is_empty() {
                        detail_lines.push(String::new());
                        detail_lines.push("summary:".to_string());
                        for wrapped in wrap_plain_text(&node.phase_summary, node_area.width as usize) {
                            detail_lines.push(wrapped);
                        }
                    }
                }
            } else {
                detail_lines.push("No graph node selected.".to_string());
            }

            let detail_body_height = node_area.height.saturating_sub(1) as usize;
            let (detail_start, detail_end) = detail_scroll_window(
                detail_lines.len(),
                app.inter_agent.detail_scroll,
                detail_body_height,
            );
            app.inter_agent.detail_scroll = detail_start;
            for (row, (line_index, line)) in detail_lines
                .iter()
                .enumerate()
                .skip(detail_start)
                .take(detail_end.saturating_sub(detail_start))
                .enumerate()
            {
                stdout.queue(cursor::MoveTo(node_area.x, node_area.y + row as u16))?;
                let delivery_color = match delivery_summary.state {
                    LibrisDeliveryState::Ready => style::Color::Rgb { r: 74, g: 222, b: 128 },
                    LibrisDeliveryState::Incomplete => style::Color::Rgb { r: 251, g: 191, b: 36 },
                    LibrisDeliveryState::Working => style::Color::Rgb { r: 148, g: 163, b: 184 },
                };
                stdout.queue(style::SetForegroundColor(if line_index == 0 {
                    delivery_color
                } else {
                    style::Color::Reset
                }))?;
                let visible: String = line.chars().take(node_area.width as usize).collect();
                write!(stdout, "{}{}", visible, " ".repeat((node_area.width as usize).saturating_sub(visible.chars().count())))?;
                stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
            }
            if node_area.height > 0 {
                let range = if detail_lines.is_empty() {
                    "0/0".to_string()
                } else {
                    format!("{}-{}/{}", detail_start + 1, detail_end, detail_lines.len())
                };
                let controls = if app.inter_agent.detail_focus {
                    "↑/↓ scroll • PgUp/PgDn page • Tab next"
                } else if app.inter_agent.graph_focus {
                    "↑/↓ node • Tab details • Enter topic/node"
                } else {
                    "Tab graph • → graph • Enter topic/node"
                };
                let hint = format!("{} • {}", range, controls);
                stdout.queue(cursor::MoveTo(
                    node_area.x,
                    node_area.y + node_area.height.saturating_sub(1),
                ))?;
                stdout.queue(style::SetForegroundColor(style::Color::DarkGrey))?;
                let visible = truncate_columns(&hint, node_area.width);
                write!(
                    stdout,
                    "{}{}",
                    visible,
                    " ".repeat(
                        (node_area.width as usize).saturating_sub(visible.chars().count())
                    )
                )?;
                stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
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

#[cfg(test)]
mod graph_layout_tests {
    use super::*;
    use serde_json::json;

    fn graph_node(name: &str, role: &str, phase: &str) -> LibrisGraphNode {
        LibrisGraphNode {
            agent_id: format!("{role}-{name}"),
            name: name.to_string(),
            role: role.to_string(),
            status: "running".to_string(),
            phase: phase.to_string(),
            topic_slug: "topic".to_string(),
            phase_summary: String::new(),
            live_line: String::new(),
        }
    }

    fn seven_topic_room() -> Value {
        let topics = (0..7)
            .map(|index| {
                json!({
                    "topic_slug": format!("topic-{index}"),
                    "title": format!("topic-{index}"),
                    "status": "working"
                })
            })
            .collect::<Vec<_>>();
        let mut nodes = vec![json!({
            "agent_id": "coordinator",
            "name": "Libris coordinator",
            "role": "coordinator",
            "status": "running",
            "phase": "fanout"
        })];
        let mut edges = Vec::new();
        for index in 0..7 {
            let slug = format!("topic-{index}");
            let researcher = format!("researcher-{index}");
            let judge = format!("judge-{index}");
            nodes.push(json!({
                "agent_id": researcher,
                "name": format!("researcher-{index}"),
                "role": "researcher",
                "status": "running",
                "phase": "search",
                "topic_slug": slug
            }));
            nodes.push(json!({
                "agent_id": judge,
                "name": format!("judge-{index}"),
                "role": "judge",
                "status": "running",
                "phase": "review",
                "topic_slug": slug
            }));
            for shade_index in 0..2 {
                let shade = format!("shade-{index}-{shade_index}");
                nodes.push(json!({
                    "agent_id": shade,
                    "name": format!("shade-{shade_index}"),
                    "role": "shade",
                    "status": "running",
                    "phase": "assist",
                    "topic_slug": slug
                }));
                edges.push(json!({
                    "from_agent_id": shade,
                    "to_agent_id": researcher,
                    "from_role": "shade",
                    "to_role": "researcher",
                    "topic_slug": slug,
                    "active_now": shade_index == 0,
                    "activity_strength": 0.8
                }));
            }
            edges.push(json!({
                "from_agent_id": "coordinator",
                "to_agent_id": researcher,
                "from_role": "coordinator",
                "to_role": "researcher",
                "topic_slug": slug,
                "active_now": true,
                "activity_strength": 1.0
            }));
            edges.push(json!({
                "from_agent_id": researcher,
                "to_agent_id": judge,
                "from_role": "researcher",
                "to_role": "judge",
                "topic_slug": slug,
                "active_now": true,
                "activity_strength": 0.9
            }));
        }
        json!({
            "kind": "libris",
            "status": "active",
            "topics": topics,
            "nodes": nodes,
            "edges": edges
        })
    }

    #[test]
    fn graph_anchors_land_on_exact_box_boundaries() {
        let anchors = graph_anchors(Rect {
            x: 10,
            y: 5,
            width: 12,
            height: 1,
        });

        assert_eq!(anchors.top, GraphPoint { x: 16, y: 4 });
        assert_eq!(anchors.bottom, GraphPoint { x: 16, y: 6 });
        assert_eq!(anchors.left, GraphPoint { x: 9, y: 5 });
        assert_eq!(anchors.right, GraphPoint { x: 22, y: 5 });
    }

    #[test]
    fn compact_node_width_tracks_visible_content_and_caps_long_labels() {
        let short = graph_node("A", "judge", "run");
        let long = graph_node(
            "A coordinator name that should not stretch the entire graph",
            "coordinator",
            "planning",
        );

        assert_eq!(compact_node_width(&short, NODE_MAX_CONTENT_WIDTH), 11);
        assert_eq!(
            compact_node_width(&long, COORDINATOR_MAX_CONTENT_WIDTH),
            COORDINATOR_MAX_CONTENT_WIDTH
        );
    }

    #[test]
    fn common_f4_size_keeps_two_by_two_fanout_and_shade_tray() {
        // This is the graph pane produced by the normal 120×40 F4 split.
        let area = Rect {
            x: 27,
            y: 2,
            width: 56,
            height: 36,
        };
        let clusters = layout_topic_clusters(area, 4, 8);

        assert_eq!(clusters.len(), 4);
        assert_eq!(
            clusters
                .iter()
                .map(|cluster| cluster.row)
                .collect::<Vec<_>>(),
            vec![0, 0, 1, 1]
        );
        assert!(clusters
            .iter()
            .all(|cluster| cluster.density == TopicClusterDensity::Full
                && cluster.rect.width == 22
                && cluster.rect.height == 11));

        let cluster = clusters[0].rect;
        let pair = layout_node_pair(cluster, 12, 12);
        let first = pair.first.expect("researcher box");
        let second = pair.second.expect("judge box");
        assert!(graph_anchors(first).bottom.y < graph_anchors(second).top.y);

        let node_bottom = graph_anchors(second).bottom.y;
        let status_y = cluster.y + cluster.height - 1;
        let tray = layout_shade_tray(cluster, node_bottom, status_y, 16)
            .expect("shade tray should fit below a stacked pair");
        assert!(tray.y + tray.height < status_y);
        assert!(graph_box_fits(cluster, tray));

        let route = route_shade_pool_to_target(
            graph_anchors(tray),
            graph_anchors(first),
            cluster,
            &[second],
        )
        .expect("aggregate shade route");
        let judge = graph_anchors(second);
        assert_eq!(route.points.last(), Some(&graph_anchors(first).right));
        assert!(route.points[1].x > judge.right.x);
    }

    #[test]
    fn seven_topic_compact_fanout_fits_120x40_and_160x48() {
        let cases = [
            (
                Rect {
                    x: 27,
                    y: 2,
                    width: 56,
                    height: 36,
                },
                14,
                7,
            ),
            (
                Rect {
                    x: 36,
                    y: 2,
                    width: 75,
                    height: 44,
                },
                20,
                10,
            ),
        ];

        for (area, expected_width, expected_height) in cases {
            let clusters = layout_topic_clusters(area, 7, 8);
            assert_eq!(clusters.len(), 7, "area: {area:?}");
            assert_eq!(
                clusters
                    .iter()
                    .map(|cluster| cluster.row)
                    .collect::<Vec<_>>(),
                vec![0, 0, 0, 1, 1, 1, 2]
            );
            assert!(clusters.iter().all(|cluster| {
                cluster.density == TopicClusterDensity::Compact
                    && cluster.rect.width == expected_width
                    && cluster.rect.height == expected_height
            }));
            for cluster in clusters {
                let pair = layout_node_pair(cluster.rect, 12, 12);
                let researcher = pair.first.expect("compact researcher box");
                let judge = pair.second.expect("compact judge box");
                assert!(graph_box_fits(cluster.rect, researcher));
                assert!(graph_box_fits(cluster.rect, judge));
                assert!(
                    cluster.rect.y + cluster.rect.height - 1 > graph_anchors(judge).bottom.y,
                    "compact status row remains visible"
                );
            }
        }
    }

    #[test]
    fn seven_topic_compact_fanout_survives_delivery_banner() {
        let summary = LibrisDeliverySummary {
            state: LibrisDeliveryState::Ready,
            topic_count: 7,
            primary_artifact: None,
            artifacts: Vec::new(),
            reason: String::new(),
        };
        for area in [
            Rect {
                x: 27,
                y: 2,
                width: 56,
                height: 36,
            },
            Rect {
                x: 36,
                y: 2,
                width: 75,
                height: 44,
            },
        ] {
            let graph_area = libris_delivery_graph_area(area, &summary);
            assert_eq!(graph_area.height, area.height - 1);
            assert_eq!(layout_topic_clusters(graph_area, 7, 8).len(), 7);
        }
    }

    #[test]
    fn seven_topic_graph_renders_nodes_fanout_and_compact_shade_summary() {
        let room = seven_topic_room();
        for area in [
            Rect {
                x: 27,
                y: 2,
                width: 56,
                height: 36,
            },
            Rect {
                x: 36,
                y: 2,
                width: 75,
                height: 44,
            },
        ] {
            let mut output = Vec::new();
            draw_libris_graph(&mut output, &room, area, 0).expect("seven-topic graph renders");
            let rendered = String::from_utf8(output).expect("terminal output is UTF-8");
            assert!(!rendered.contains("Graph compacted"));
            assert!(rendered.contains("Libris coordinator"));
            assert!(rendered.contains("topic-0"));
            assert!(rendered.contains("topic-6"));
            assert!(rendered.contains("shades"));
            assert!(rendered.contains('━'));
            assert!(rendered.contains('┃'));
        }
    }

    #[test]
    fn narrow_graph_layout_fails_closed_without_overflowing() {
        let area = Rect {
            x: 2,
            y: 2,
            width: 18,
            height: 8,
        };
        assert!(layout_topic_clusters(area, 4, 5).is_empty());
    }

    #[test]
    fn fanout_junction_glyphs_have_only_real_arms() {
        assert_eq!(
            heavy_junction_glyph(false, true, true, false),
            '┓',
            "rightmost topic drop connects left and down"
        );
        assert_eq!(
            heavy_junction_glyph(false, true, false, true),
            '┏',
            "first bus row connects right and down"
        );
        assert_eq!(
            heavy_junction_glyph(true, false, false, true),
            '┗',
            "last bus row connects up and right"
        );
        assert_eq!(
            heavy_junction_glyph(true, false, true, true),
            '┻',
            "coordinator intersects the lane from above"
        );
    }

    #[test]
    fn malformed_extreme_geometry_saturates_or_fails_closed() {
        let anchors = graph_anchors(Rect {
            x: u16::MAX - 1,
            y: u16::MAX - 1,
            width: 10,
            height: 10,
        });
        assert_eq!(anchors.right.x, u16::MAX);
        assert_eq!(anchors.bottom.y, u16::MAX);

        let area = Rect {
            x: 0,
            y: 0,
            width: 80,
            height: 40,
        };
        assert!(layout_topic_clusters(area, u16::MAX as usize + 1, 4).is_empty());
    }

    #[test]
    fn orthogonal_route_starts_and_ends_on_box_boundaries() {
        let source = graph_anchors(Rect {
            x: 5,
            y: 5,
            width: 8,
            height: 1,
        });
        let target = graph_anchors(Rect {
            x: 18,
            y: 5,
            width: 8,
            height: 1,
        });
        let route = route_between_boxes(source, target).expect("horizontal route");

        assert_eq!(route.points.first(), Some(&source.right));
        assert_eq!(route.points.last(), Some(&target.left));
    }

    #[test]
    fn routed_edges_render_with_heavy_line_glyphs() {
        let route = OrthogonalRoute {
            points: vec![
                GraphPoint { x: 1, y: 1 },
                GraphPoint { x: 1, y: 3 },
                GraphPoint { x: 4, y: 3 },
            ],
        };
        let mut output = Vec::new();
        draw_orthogonal_route(&mut output, &route, style::Color::Blue).expect("route renders");
        let rendered = String::from_utf8(output).expect("terminal output is UTF-8");

        assert!(rendered.contains('┃'));
        assert!(rendered.contains('━'));
        assert!(rendered.contains('┗'));
        assert!(!rendered.contains("---"));
        assert!(!rendered.contains("<=>"));
    }

    #[test]
    fn valid_manifest_drives_ready_banner_sidebar_and_delivery_details() {
        let room = json!({
            "status": "delivered",
            "delivery_manifest": {
                "status": "ready",
                "ready": true,
                "topic_count": 2,
                "primary_artifact": {
                    "label": "Shareable report",
                    "path": "/tmp/libris-delivery/report.html",
                    "media_type": "text/html"
                },
                "artifacts": [
                    {
                        "label": "Shareable report",
                        "path": "/tmp/libris-delivery/report.html",
                        "media_type": "text/html"
                    },
                    {
                        "label": "Executive summary",
                        "path": "/tmp/libris-delivery/executive-summary.md",
                        "media_type": "text/markdown"
                    }
                ]
            }
        });

        let summary = libris_delivery_summary(&room);
        assert_eq!(summary.state, LibrisDeliveryState::Ready);
        assert_eq!(summary.topic_count, 2);
        assert_eq!(libris_delivery_sidebar_marker(&summary), "✓");
        assert_eq!(
            libris_delivery_banner_text(&summary).as_deref(),
            Some("✓ DELIVERY READY • 2 REPORTS")
        );

        let details = libris_delivery_detail_lines(&summary, 120).join("\n");
        assert!(details.contains("delivery: READY"));
        assert!(details.contains("reports: 2"));
        assert!(details.contains("primary: Shareable report (text/html)"));
        assert!(details.contains("/tmp/libris-delivery/report.html"));
        assert!(details.contains("artifacts (2):"));
        assert!(details.contains("Executive summary (text/markdown)"));
        assert!(details.contains("/tmp/libris-delivery/executive-summary.md"));
    }

    #[test]
    fn artifact_detail_scroll_reaches_the_last_manifest_path() {
        let artifacts = (0..8)
            .map(|index| LibrisDeliveryArtifact {
                label: format!("Artifact {index}"),
                path: if index == 7 {
                    "/tmp/libris-delivery/final-artifact.md".to_string()
                } else {
                    format!("/tmp/libris-delivery/artifact-{index}.md")
                },
                media_type: "text/markdown".to_string(),
            })
            .collect::<Vec<_>>();
        let summary = LibrisDeliverySummary {
            state: LibrisDeliveryState::Ready,
            topic_count: 7,
            primary_artifact: artifacts.first().cloned(),
            artifacts,
            reason: String::new(),
        };
        let lines = libris_delivery_detail_lines(&summary, 80);
        let body_height = 5;
        let (first_start, first_end) = detail_scroll_window(lines.len(), 0, body_height);
        let first_page = lines[first_start..first_end].join("\n");
        assert!(!first_page.contains("final-artifact.md"));

        let (last_start, last_end) = detail_scroll_window(lines.len(), usize::MAX, body_height);
        let last_page = lines[last_start..last_end].join("\n");
        assert!(last_start > 0);
        assert_eq!(last_end, lines.len());
        assert!(last_page.contains("/tmp/libris-delivery/final-artifact.md"));
    }

    #[test]
    fn final_selection_alone_never_claims_a_successful_delivery() {
        let working_room = json!({
            "status": "active",
            "final_selection_markdown": "# A stale selection"
        });
        let working = libris_delivery_summary(&working_room);
        assert_eq!(working.state, LibrisDeliveryState::Working);
        assert!(libris_delivery_banner_text(&working).is_none());
        assert_eq!(libris_delivery_sidebar_marker(&working), "");

        let delivered_room = json!({
            "status": "delivered",
            "final_selection_markdown": "# A stale selection"
        });
        let incomplete = libris_delivery_summary(&delivered_room);
        assert_eq!(incomplete.state, LibrisDeliveryState::Incomplete);
        assert_eq!(
            libris_delivery_banner_text(&incomplete).as_deref(),
            Some("⚠ DELIVERY INCOMPLETE • 0 REPORTS")
        );
        assert_eq!(libris_delivery_sidebar_marker(&incomplete), "⚠");
    }

    #[test]
    fn ready_claim_with_relative_or_missing_artifacts_is_incomplete() {
        let room = json!({
            "status": "delivered",
            "delivery_manifest": {
                "status": "ready",
                "ready": true,
                "topic_count": 1,
                "primary_artifact": {
                    "label": "Report",
                    "path": "delivery/report.html",
                    "media_type": "text/html"
                },
                "artifacts": []
            }
        });

        let summary = libris_delivery_summary(&room);
        assert_eq!(summary.state, LibrisDeliveryState::Incomplete);
        assert_eq!(libris_delivery_status_label(summary.state), "INCOMPLETE");
    }

    #[test]
    fn delivery_banner_renders_on_reserved_graph_footer_rows() {
        let room = json!({
            "status": "delivered",
            "nodes": [],
            "topics": [],
            "delivery_manifest": {
                "status": "ready",
                "ready": true,
                "topic_count": 1,
                "primary_artifact": {
                    "label": "Report",
                    "path": "/tmp/libris-delivery/report.html",
                    "media_type": "text/html"
                },
                "artifacts": [{
                    "label": "Report",
                    "path": "/tmp/libris-delivery/report.html",
                    "media_type": "text/html"
                }]
            }
        });
        let area = Rect {
            x: 2,
            y: 2,
            width: 44,
            height: 12,
        };
        let summary = libris_delivery_summary(&room);
        let graph_area = libris_delivery_graph_area(area, &summary);
        assert_eq!(graph_area.x, area.x);
        assert_eq!(graph_area.y, area.y);
        assert_eq!(graph_area.width, area.width);
        assert_eq!(graph_area.height, area.height - 1);

        let mut output = Vec::new();
        draw_libris_graph(&mut output, &room, area, 0).expect("graph renders");
        let rendered = String::from_utf8(output).expect("terminal output is UTF-8");
        assert!(rendered.contains("DELIVERY READY"));
        assert!(rendered.contains("Waiting for Libris topic clusters"));
    }
}
