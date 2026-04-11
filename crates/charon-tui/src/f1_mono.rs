use crossterm::style::Color;

use crate::app::App;
use crate::chat::{ChatTextPoint, ContextMenu};
use crate::chat_view::{self, ChatRenderLine, ChatVisualCache};
use crate::render::Rect;
use crate::screen::{Cell, ScreenBuf};

pub struct F1MonoCache {
    pub visual: ChatVisualCache,
}

impl Default for F1MonoCache {
    fn default() -> Self {
        Self { visual: ChatVisualCache::default() }
    }
}

pub fn ensure_cache(app: &App, w: u16, h: u16, cache: &mut F1MonoCache, force: bool) {
    chat_view::ensure_chat_visual_cache(app, w, h, &mut cache.visual, force);
}

pub(crate) fn transcript_area_with_rowing(w: u16, h: u16, rowing: bool) -> Rect {
    let x = 2;
    let y = 2;
    let width = w.saturating_sub(4).max(8);
    let reserved_bottom = if h >= 24 { 7 } else { 5 };
    let rowing_reserve = if rowing && h >= 24 { 4 } else { 0 };
    let height = h.saturating_sub(y + reserved_bottom + rowing_reserve).max(3);
    Rect { x, y, width, height }
}

pub(crate) fn transcript_area(w: u16, h: u16) -> Rect {
    transcript_area_with_rowing(w, h, false)
}

pub(crate) fn input_area(w: u16, h: u16) -> Rect {
    let width = w.saturating_sub(4).max(8);
    let y = h.saturating_sub(5);
    Rect { x: 2, y, width, height: 1 }
}

pub(crate) fn footer_y(h: u16) -> u16 {
    h.saturating_sub(2)
}

fn rowing_area(w: u16, h: u16) -> (u16, u16) {
    let transcript = transcript_area_with_rowing(w, h, true);
    let y = transcript.y + transcript.height + 1; // row after transcript border bottom
    let input = input_area(w, h);
    let available = input.y.saturating_sub(1).saturating_sub(y); // rows between transcript bottom and input border top
    (y, available)
}

fn selection_bounds(app: &App) -> Option<(ChatTextPoint, ChatTextPoint)> {
    let a = app.chat.selection_anchor?;
    let b = app.chat.selection_focus?;
    if (a.row, a.col) <= (b.row, b.col) {
        Some((a, b))
    } else {
        Some((b, a))
    }
}

pub(crate) fn visible_window(total_rows: usize, area: Rect, scroll: usize) -> (usize, usize) {
    let max_lines = area.height as usize;
    let clamped = scroll.min(total_rows.saturating_sub(max_lines));
    let visible_count = total_rows.saturating_sub(clamped);
    let start = visible_count.saturating_sub(max_lines);
    let end = start + max_lines.min(visible_count);
    (start.min(total_rows), end.min(total_rows))
}

fn selection_cols_for_row(line: &ChatRenderLine, row: usize, bounds: Option<(ChatTextPoint, ChatTextPoint)>) -> Option<(usize, usize)> {
    let (start, end) = bounds?;
    if !line.selectable || row < start.row || row > end.row {
        return None;
    }
    let copy_len = line.copy_text.chars().count();
    let (sel_start, sel_end) = if start.row == end.row {
        (start.col.min(copy_len), end.col.min(copy_len))
    } else if row == start.row {
        (start.col.min(copy_len), copy_len)
    } else if row == end.row {
        (0, end.col.min(copy_len))
    } else {
        (0, copy_len)
    };
    if sel_start >= sel_end && copy_len > 0 {
        return None;
    }
    Some((line.copy_offset + sel_start, line.copy_offset + sel_end))
}

const SEL_FG: Color = Color::Black;
const SEL_BG: Color = Color::Rgb { r: 226, g: 232, b: 240 };

fn draw_line(buf: &mut ScreenBuf, x: u16, y: u16, width: usize, line: &ChatRenderLine, selection_cols: Option<(usize, usize)>) {
    let bg_default = line.bg.unwrap_or(Color::Reset);
    let mut col = 0usize;
    for span in &line.spans {
        for ch in span.text.chars() {
            if col >= width { break; }
            let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
            buf.set(x + col as u16, y, Cell {
                ch,
                fg: if selected { SEL_FG } else { span.fg },
                bg: if selected { SEL_BG } else { bg_default },
                bold: false,
            });
            col += 1;
        }
        if col >= width { break; }
    }
    while col < width {
        let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
        buf.set(x + col as u16, y, Cell {
            ch: ' ',
            fg: if selected { SEL_FG } else { Color::Reset },
            bg: if selected { SEL_BG } else { bg_default },
            bold: false,
        });
        col += 1;
    }
}

fn render_border(buf: &mut ScreenBuf, area: Rect, title: &str, focused: bool) {
    let border = if focused {
        Color::Rgb { r: 167, g: 139, b: 250 }
    } else {
        Color::Rgb { r: 59, g: 50, b: 82 }
    };
    let title_color = Color::Rgb { r: 212, g: 196, b: 168 };
    let lx = area.x.saturating_sub(1);
    let ty = area.y.saturating_sub(1);

    // Top: ╭─ title ───╮  or  ╭────╮ if title is empty
    buf.set(lx, ty, Cell { ch: '╭', fg: border, bg: Color::Reset, bold: false });
    let mut cx = lx + 1;
    if !title.is_empty() {
        buf.set(cx, ty, Cell { ch: '─', fg: border, bg: Color::Reset, bold: false });
        cx += 1;
        buf.set(cx, ty, Cell { ch: ' ', fg: border, bg: Color::Reset, bold: false });
        cx += 1;
        for ch in title.chars() {
            buf.set(cx, ty, Cell { ch, fg: title_color, bg: Color::Reset, bold: false });
            cx += 1;
        }
        buf.set(cx, ty, Cell { ch: ' ', fg: border, bg: Color::Reset, bold: false });
        cx += 1;
    }
    let right_edge = lx + area.width + 1;
    while cx < right_edge {
        buf.set(cx, ty, Cell { ch: '─', fg: border, bg: Color::Reset, bold: false });
        cx += 1;
    }
    buf.set(right_edge, ty, Cell { ch: '╮', fg: border, bg: Color::Reset, bold: false });

    // Sides
    for row in 0..area.height {
        buf.set(lx, area.y + row, Cell { ch: '│', fg: border, bg: Color::Reset, bold: false });
        buf.set(area.x + area.width, area.y + row, Cell { ch: '│', fg: border, bg: Color::Reset, bold: false });
    }

    // Bottom: ╰───╯
    let by = area.y + area.height;
    buf.set(lx, by, Cell { ch: '╰', fg: border, bg: Color::Reset, bold: false });
    for x in 1..=area.width {
        buf.set(lx + x, by, Cell { ch: '─', fg: border, bg: Color::Reset, bold: false });
    }
    buf.set(right_edge, by, Cell { ch: '╯', fg: border, bg: Color::Reset, bold: false });
}

fn draw_transcript(buf: &mut ScreenBuf, app: &App, area: Rect, lines: &[ChatRenderLine]) {
    render_border(buf, area, "chat", true);
    let bounds = selection_bounds(app);
    let (start, end) = visible_window(lines.len(), area, app.chat.scroll);
    let visible = &lines[start..end];
    for (i, line) in visible.iter().enumerate() {
        let row_idx = start + i;
        let sel = selection_cols_for_row(line, row_idx, bounds);
        draw_line(buf, area.x, area.y + i as u16, area.width as usize, line, sel);
    }
    for row in visible.len() as u16..area.height {
        buf.fill(area.y + row, area.x, area.x + area.width, ' ', Color::Reset, Color::Reset);
    }
}

pub(crate) fn tail_visible_text(text: &str, width: usize) -> String {
    if width == 0 { return String::new(); }
    let chars: Vec<char> = text.chars().collect();
    let start = chars.len().saturating_sub(width);
    chars[start..].iter().collect()
}

pub(crate) fn fmt_k(n: u64) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", (n as f64) / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", (n as f64) / 1_000.0)
    } else {
        n.to_string()
    }
}

fn draw_input(buf: &mut ScreenBuf, app: &App, area: Rect) {
    render_border(buf, area, "", true);
    let fg = Color::Rgb { r: 248, g: 250, b: 252 };
    let visible = tail_visible_text(&app.chat.input, area.width as usize);
    buf.put_str(area.x, area.y, &visible, fg, Color::Reset, false);
    let used = visible.chars().count() as u16;
    buf.fill(area.y, area.x + used, area.x + area.width, ' ', fg, Color::Reset);
}

fn draw_footer(buf: &mut ScreenBuf, app: &App, w: u16, h: u16) {
    let y0 = footer_y(h).saturating_sub(1);
    let provider = app.chat.provider_model();
    let chat_in = app.chat.usage.input_tokens;
    let chat_out = app.chat.usage.output_tokens;
    let ctx = app.chat.usage.context_pct.map(|v| format!("{:.0}%", v)).unwrap_or_else(|| "-".into());
    let queue_str = if app.chat.pending_queue.is_empty() {
        String::new()
    } else {
        format!("  queued:{}", app.chat.pending_queue.len())
    };
    let left = format!("  ❈ CHARON  {}  ctx:{}  chat:{}↑ {}↓{}", provider, ctx, fmt_k(chat_in), fmt_k(chat_out), queue_str);
    let right = if app.chat.streaming {
        "PgUp/PgDn scroll  Ctrl+C copy  Esc clear"
    } else {
        "Enter send  / commands  PgUp/PgDn scroll"
    };
    let fg1 = Color::Rgb { r: 120, g: 100, b: 70 };
    let fg2 = Color::Rgb { r: 90, g: 90, b: 110 };
    buf.fill(y0, 0, w, ' ', Color::Reset, Color::Reset);
    buf.fill(y0 + 1, 0, w, ' ', Color::Reset, Color::Reset);
    buf.put_str(0, y0, &tail_visible_text(&left, w as usize), fg1, Color::Reset, false);
    buf.put_str(0, y0 + 1, &tail_visible_text(right, w as usize), fg2, Color::Reset, false);
}

fn draw_menu(buf: &mut ScreenBuf, app: &App, anchor: Rect) {
    if !app.chat.menu_open() {
        return;
    }
    let menu_w = anchor.width.min(96).max(24);
    let desired_h = (app.chat.menu_items.len() as u16).min(10) + 2;
    let menu_h = desired_h.min(anchor.y.saturating_sub(2).max(4));
    let area = Rect {
        x: anchor.x,
        y: anchor.y.saturating_sub(menu_h + 1),
        width: menu_w,
        height: menu_h.saturating_sub(2),
    };
    render_border(buf, area, app.chat.menu_title.as_deref().unwrap_or("menu"), true);
    let visible_rows = area.height as usize;
    let total_items = app.chat.menu_items.len();
    let start_idx = if total_items > visible_rows {
        app.chat.menu_index
            .saturating_sub(visible_rows.saturating_sub(1))
            .min(total_items.saturating_sub(visible_rows))
    } else {
        0
    };
    for (row, item) in app.chat.menu_items.iter().skip(start_idx).take(visible_rows).enumerate() {
        let item_idx = start_idx + row;
        let selected = item_idx == app.chat.menu_index;
        let fg = if selected {
            Color::Rgb { r: 196, g: 181, b: 253 }
        } else if item.executable {
            Color::Rgb { r: 226, g: 232, b: 240 }
        } else {
            Color::Rgb { r: 148, g: 163, b: 184 }
        };
        let line = if item.age.is_empty() {
            format!("{} {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc)
        } else {
            format!("{} {}  {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc, item.age)
        };
        let truncated: String = line.chars().take(area.width as usize).collect();
        let y = area.y + row as u16;
        buf.put_str(area.x, y, &truncated, fg, Color::Reset, false);
        let used = truncated.chars().count() as u16;
        buf.fill(y, area.x + used, area.x + area.width, ' ', fg, Color::Reset);
    }
}

fn draw_context_menu(buf: &mut ScreenBuf, menu: &ContextMenu, w: u16, h: u16) {
    let items: Vec<&str> = if menu.has_selection {
        vec!["  Copy ", " Paste "]
    } else {
        vec![" Paste "]
    };
    let menu_w: u16 = 10;
    let menu_h = items.len() as u16;
    let mx = menu.x.min(w.saturating_sub(menu_w + 2));
    let my = if menu.y + menu_h + 2 >= h {
        menu.y.saturating_sub(menu_h + 2)
    } else {
        menu.y + 1
    };
    let border = Color::Rgb { r: 100, g: 100, b: 120 };
    let menu_bg = Color::Rgb { r: 30, g: 30, b: 40 };

    // Top border
    let top_y = my;
    buf.set(mx, top_y, Cell { ch: '┌', fg: border, bg: menu_bg, bold: false });
    for i in 1..=menu_w {
        buf.set(mx + i, top_y, Cell { ch: '─', fg: border, bg: menu_bg, bold: false });
    }
    buf.set(mx + menu_w + 1, top_y, Cell { ch: '┐', fg: border, bg: menu_bg, bold: false });

    // Items
    for (i, item) in items.iter().enumerate() {
        let row = my + 1 + i as u16;
        let selected = i == menu.selected;
        buf.set(mx, row, Cell { ch: '│', fg: border, bg: menu_bg, bold: false });
        let (ifg, ibg) = if selected {
            (Color::Rgb { r: 30, g: 30, b: 40 }, Color::Rgb { r: 196, g: 181, b: 253 })
        } else {
            (Color::Rgb { r: 226, g: 232, b: 240 }, menu_bg)
        };
        let padded: String = format!("{:<width$}", item, width = menu_w as usize);
        buf.put_str(mx + 1, row, &padded, ifg, ibg, false);
        buf.set(mx + menu_w + 1, row, Cell { ch: '│', fg: border, bg: menu_bg, bold: false });
    }

    // Bottom border
    let bot_y = my + 1 + menu_h;
    buf.set(mx, bot_y, Cell { ch: '└', fg: border, bg: menu_bg, bold: false });
    for i in 1..=menu_w {
        buf.set(mx + i, bot_y, Cell { ch: '─', fg: border, bg: menu_bg, bold: false });
    }
    buf.set(mx + menu_w + 1, bot_y, Cell { ch: '┘', fg: border, bg: menu_bg, bold: false });
}

pub fn context_menu_item_count(menu: &ContextMenu) -> usize {
    if menu.has_selection { 2 } else { 1 }
}

fn draw_popup(buf: &mut ScreenBuf, x: u16, y: u16, w: u16, h: u16, title: &str, lines: &[(Color, String)]) {
    let area = Rect { x: x + 1, y: y + 1, width: w.saturating_sub(2), height: h.saturating_sub(2) };
    render_border(buf, area, title, true);
    // Fill interior with dark bg
    let bg = Color::Rgb { r: 15, g: 15, b: 25 };
    for row in 0..area.height {
        buf.fill(area.y + row, area.x, area.x + area.width, ' ', Color::Reset, bg);
    }
    for (i, (fg, text)) in lines.iter().enumerate() {
        if i as u16 >= area.height { break; }
        let truncated: String = text.chars().take(area.width as usize).collect();
        buf.put_str(area.x, area.y + i as u16, &truncated, *fg, bg, false);
    }
}

fn draw_approval(buf: &mut ScreenBuf, app: &App, w: u16, h: u16) {
    let Some(approval) = app.chat.approval.as_ref() else { return; };
    let popup_w = (w.saturating_sub(12)).min(90);
    let popup_h = 9u16.min(h.saturating_sub(6));
    let popup_x = (w.saturating_sub(popup_w)) / 2;
    let popup_y = (h.saturating_sub(popup_h)) / 2;
    let risk_color = match approval.risk.as_str() {
        "dangerous" => Color::Rgb { r: 239, g: 68, b: 68 },
        "network" => Color::Rgb { r: 245, g: 158, b: 11 },
        _ => Color::Rgb { r: 99, g: 102, b: 241 },
    };
    let mut lines: Vec<(Color, String)> = vec![
        (Color::Rgb { r: 226, g: 232, b: 240 }, format!("Tool: {}", approval.tool)),
        (risk_color, format!("Risk: {} — {}", approval.risk, approval.reason)),
    ];
    if !approval.params.is_empty() {
        let inner_w = popup_w.saturating_sub(4) as usize;
        for chunk in approval.params.as_bytes().chunks(inner_w.max(1)) {
            if let Ok(s) = std::str::from_utf8(chunk) {
                lines.push((Color::DarkGrey, s.to_string()));
            }
        }
    }
    let options = ["Approve", "Deny", "Approve all for session"];
    for (idx, label) in options.iter().enumerate() {
        let sel = idx == approval.selected;
        let fg = if sel { Color::Rgb { r: 34, g: 197, b: 94 } } else { Color::Rgb { r: 148, g: 163, b: 184 } };
        lines.push((fg, format!("{} {}", if sel { "▸" } else { " " }, label)));
    }
    lines.push((Color::DarkGrey, "Use ↑/↓ then Enter. Esc denies.".to_string()));
    draw_popup(buf, popup_x, popup_y, popup_w, popup_h, "approval required", &lines);
}

fn draw_auth(buf: &mut ScreenBuf, app: &App, w: u16, h: u16) {
    let Some(url) = app.chat.auth_url.as_ref() else { return; };
    let provider = app.chat.auth_provider.as_deref().unwrap_or("provider");
    let popup_w = (w.saturating_sub(8)).min(96);
    let popup_h = 7u16.min(h.saturating_sub(4));
    let popup_x = (w.saturating_sub(popup_w)) / 2;
    let popup_y = (h.saturating_sub(popup_h)) / 2;
    let mut lines: Vec<(Color, String)> = vec![
        (Color::Rgb { r: 226, g: 232, b: 240 }, format!("Authenticate with {}", provider)),
        (Color::DarkGrey, String::new()),
        (Color::Rgb { r: 99, g: 102, b: 241 }, url.clone()),
        (Color::DarkGrey, String::new()),
    ];
    let options = ["Open in browser", "Cancel"];
    for (idx, label) in options.iter().enumerate() {
        let sel = idx == app.chat.auth_action_index;
        let fg = if sel { Color::Rgb { r: 34, g: 197, b: 94 } } else { Color::Rgb { r: 148, g: 163, b: 184 } };
        lines.push((fg, format!("{} {}", if sel { "▸" } else { " " }, label)));
    }
    draw_popup(buf, popup_x, popup_y, popup_w, popup_h, "authentication", &lines);
}

fn draw_info_pane(buf: &mut ScreenBuf, app: &App, w: u16, h: u16) {
    let pane_w = (w.saturating_sub(8)).min(44).max(28);
    let pane_h = h.saturating_sub(8).min(30).max(10);
    let px = w.saturating_sub(pane_w + 3);
    let py: u16 = 3;

    let tab = app.chat.info_pane_tab;
    let tabs = ["Outcomes", "Goals", "Model", "Ideas"];
    let tab_line: String = tabs.iter().enumerate().map(|(i, t)| {
        if i == tab { format!("[{}]", t) } else { t.to_string() }
    }).collect::<Vec<_>>().join(" ");

    let info = app.chat.refresh_payload.as_ref()
        .and_then(|p| p.get("session_info"));
    let dim = Color::Rgb { r: 100, g: 100, b: 120 };
    let fg = Color::Rgb { r: 212, g: 196, b: 168 };
    let hi = Color::Rgb { r: 196, g: 181, b: 253 };
    let bg = Color::Rgb { r: 15, g: 15, b: 25 };

    // Draw pane background + border
    let area = Rect { x: px + 1, y: py + 1, width: pane_w.saturating_sub(2), height: pane_h.saturating_sub(2) };
    render_border(buf, area, "info (Ctrl+I cycle, Ctrl+P close)", true);
    for row in 0..area.height {
        buf.fill(area.y + row, area.x, area.x + area.width, ' ', dim, bg);
    }

    // Tab bar
    buf.put_str(area.x, area.y, &tab_line, hi, bg, false);
    let used = tab_line.chars().count() as u16;
    buf.fill(area.y, area.x + used, area.x + area.width, ' ', dim, bg);
    // Separator
    for x in area.x..area.x + area.width {
        buf.set(x, area.y + 1, Cell { ch: '─', fg: dim, bg, bold: false });
    }

    let content_y = area.y + 2;
    let content_h = area.height.saturating_sub(2);
    let cw = area.width as usize;

    match tab {
        0 => {
            // Outcomes
            let tasks = info.and_then(|i| i.get("tasks")).and_then(|v| v.as_array());
            let mut row = 0u16;
            if let Some(tasks) = tasks {
                for task in tasks.iter().rev().take(content_h as usize) {
                    let title = task.get("title").and_then(|v| v.as_str()).unwrap_or("task");
                    let status = task.get("status").and_then(|v| v.as_str()).unwrap_or("active");
                    let icon = if status == "completed" { "✓" } else { "○" };
                    let line: String = format!("{} {}", icon, title).chars().take(cw).collect();
                    buf.put_str(area.x, content_y + row, &line, fg, bg, false);
                    row += 1;
                }
            }
            // Provisional outcomes
            for po in app.chat.provisional_outcomes.iter().rev() {
                if row >= content_h { break; }
                let line: String = format!("◌ {}", po.summary).chars().take(cw).collect();
                buf.put_str(area.x, content_y + row, &line, dim, bg, false);
                row += 1;
            }
            if row == 0 {
                buf.put_str(area.x, content_y, "No outcomes yet.", dim, bg, false);
            }
        }
        1 => {
            // Goals
            let goals = info.and_then(|i| i.get("goals")).and_then(|v| v.as_array());
            let mut row = 0u16;
            if let Some(goals) = goals {
                for goal in goals.iter().take(content_h as usize) {
                    let title = goal.get("title").and_then(|v| v.as_str()).unwrap_or("goal");
                    let status = goal.get("status").and_then(|v| v.as_str()).unwrap_or("backlog");
                    let line: String = format!("• {} [{}]", title, status).chars().take(cw).collect();
                    buf.put_str(area.x, content_y + row, &line, fg, bg, false);
                    row += 1;
                }
            }
            if row == 0 {
                buf.put_str(area.x, content_y, "No goals yet.", dim, bg, false);
            }
        }
        2 => {
            // User Model
            let model_text = info.and_then(|i| i.get("user_model")).and_then(|v| v.as_str()).unwrap_or("");
            if model_text.is_empty() {
                buf.put_str(area.x, content_y, "No user model yet.", dim, bg, false);
            } else {
                for (i, line) in model_text.lines().take(content_h as usize).enumerate() {
                    let truncated: String = line.chars().take(cw).collect();
                    buf.put_str(area.x, content_y + i as u16, &truncated, fg, bg, false);
                }
            }
        }
        3 => {
            // Ideas
            let ideas = info.and_then(|i| i.get("ideas")).and_then(|v| v.as_array());
            let mut row = 0u16;
            if let Some(ideas) = ideas {
                for (i, idea) in ideas.iter().rev().take(content_h as usize).enumerate() {
                    let summary = idea.get("summary").and_then(|v| v.as_str()).unwrap_or("?");
                    let cat = idea.get("category").and_then(|v| v.as_str()).unwrap_or("");
                    let src = if idea.get("source").and_then(|v| v.as_str()) == Some("auto") { "⚡" } else { "✏" };
                    let tag = if !cat.is_empty() && cat != "general" { format!("[{}] ", cat) } else { String::new() };
                    let line: String = format!("{} #{} {}{}", src, i + 1, tag, summary).chars().take(cw).collect();
                    buf.put_str(area.x, content_y + row, &line, fg, bg, false);
                    row += 1;
                }
            }
            // Also show local pending queue if any
            if !app.chat.pending_queue.is_empty() {
                if row > 0 && row < content_h {
                    for x in area.x..area.x + area.width {
                        buf.set(x, content_y + row, Cell { ch: '─', fg: dim, bg, bold: false });
                    }
                    row += 1;
                }
                for msg in &app.chat.pending_queue {
                    if row >= content_h { break; }
                    let line: String = format!("⏳ {}", msg).chars().take(cw).collect();
                    buf.put_str(area.x, content_y + row, &line, Color::Rgb { r: 148, g: 163, b: 184 }, bg, false);
                    row += 1;
                }
            }
            if row == 0 {
                buf.put_str(area.x, content_y, "No ideas yet. Use /idea <text>.", dim, bg, false);
            }
        }
        _ => {}
    }
}

/// Render the full F1 chat view into the screen buffer.
pub fn draw(buf: &mut ScreenBuf, app: &App, w: u16, h: u16, cache: &F1MonoCache) {
    let rowing = chat_view::chat_rowing_active(app);
    let transcript = transcript_area_with_rowing(w, h, rowing);
    let input = input_area(w, h);

    draw_transcript(buf, app, transcript, &cache.visual.lines);

    // Rowing/thinking animation between transcript and input
    if rowing {
        let frame = ((chat_view::animation_clock_start().elapsed().as_millis() / 300) % 4) as usize;
        let activity_lines = chat_view::rowing_indicator_lines(frame);
        let (ry, ravail) = rowing_area(w, h);
        let width = transcript.width as usize;
        for (i, line) in activity_lines.iter().enumerate() {
            if i as u16 >= ravail { break; }
            draw_line(buf, transcript.x, ry + i as u16, width, line, None);
        }
    }

    draw_input(buf, app, input);
    draw_footer(buf, app, w, h);
    draw_menu(buf, app, input);
    if let Some(ref ctx) = app.chat.context_menu {
        draw_context_menu(buf, ctx, w, h);
    }
    // Overlays
    if app.chat.info_pane_open {
        draw_info_pane(buf, app, w, h);
    }
    if app.chat.approval_open() {
        draw_approval(buf, app, w, h);
    }
    if app.chat.auth_open() {
        draw_auth(buf, app, w, h);
    }
}

pub fn point_at_mouse(cache: &F1MonoCache, app: &App, w: u16, h: u16, x: u16, y: u16) -> Option<ChatTextPoint> {
    let area = transcript_area(w, h);
    if cache.visual.lines.is_empty() || area.width == 0 || area.height == 0 {
        return None;
    }
    let clamped_x = x.clamp(area.x, area.x.saturating_add(area.width).saturating_sub(1));
    let clamped_y = y.clamp(area.y, area.y.saturating_add(area.height).saturating_sub(1));
    let (start, end) = visible_window(cache.visual.lines.len(), area, app.chat.scroll);
    let visible = &cache.visual.lines[start..end];
    if visible.is_empty() {
        return None;
    }
    let rel_y = clamped_y.saturating_sub(area.y) as usize;
    let row_idx = start + rel_y.min(visible.len().saturating_sub(1));
    let line = cache.visual.lines.get(row_idx)?;
    if !line.selectable {
        return None;
    }
    let text_len = line.copy_text.chars().count();
    let rel_x = clamped_x.saturating_sub(area.x) as usize;
    let col = rel_x.saturating_sub(line.copy_offset).min(text_len);
    Some(ChatTextPoint { row: row_idx, col })
}

pub fn copy_selection(app: &mut App, cache: &F1MonoCache) -> bool {
    chat_view::copy_chat_selection(app, &cache.visual.lines)
}

pub fn content_area(w: u16, h: u16) -> Rect {
    transcript_area(w, h)
}
