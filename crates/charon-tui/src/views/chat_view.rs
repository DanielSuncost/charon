use std::io::{self, Write};
use std::sync::OnceLock;
use std::time::Instant;

use crossterm::{cursor, style, QueueableCommand};
use serde::Deserialize;
use serde_json::Value;

use crate::app::App;
use crate::chat::{ChatMessage, ChatTextPoint, ChatViewMode};
use crate::copy_to_clipboard;
use crate::render::{self, Rect};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ChatLayoutVariant {
    Full,
    Mid,
    Tiny,
}

#[derive(Clone)]
pub struct ChatSpan {
    pub fg: style::Color,
    pub text: String,
}

#[derive(Clone)]
pub struct ChatRenderLine {
    pub spans: Vec<ChatSpan>,
    pub bg: Option<style::Color>,
    pub copy_text: String,
    pub copy_offset: usize,
    pub selectable: bool,
}

#[derive(Default)]
pub struct ChatVisualCache {
    pub width: usize,
    pub variant: Option<ChatLayoutVariant>,
    pub lines: Vec<ChatRenderLine>,
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
#[allow(dead_code)] // mascot asset metadata; not all fields read
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
    SPRITE.get_or_init(|| serde_json::from_str(include_str!("../../../../assets/lantern_wraith_terminal_sprite_v2.json")).expect("valid mascot sprite"))
}

fn mascot_config() -> &'static MascotConfig {
    static CONFIG: OnceLock<MascotConfig> = OnceLock::new();
    CONFIG.get_or_init(|| serde_json::from_str(include_str!("../../../../assets/mascot_config.json")).expect("valid mascot config"))
}

pub(crate) fn animation_clock_start() -> Instant {
    static START: OnceLock<Instant> = OnceLock::new();
    *START.get_or_init(Instant::now)
}

pub fn chat_layout_variant(w: u16, h: u16) -> ChatLayoutVariant {
    if w >= 95 && h >= 30 {
        ChatLayoutVariant::Full
    } else if w >= 60 && h >= 18 {
        ChatLayoutVariant::Mid
    } else {
        ChatLayoutVariant::Tiny
    }
}

pub fn chat_rowing_active(app: &App) -> bool {
    if app.chat.streaming {
        return true;
    }
    matches!(app.chat.messages.last(),
        Some(ChatMessage::Assistant { streaming: true, .. })
        | Some(ChatMessage::Thinking { streaming: true, .. })
        | Some(ChatMessage::ToolCall { .. })
    )
}

pub fn chat_reserved_bottom(app: &App, variant: ChatLayoutVariant) -> u16 {
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

pub fn chat_info_pane_width(app: &App, w: u16, h: u16) -> u16 {
    let variant = chat_layout_variant(w, h);
    if app.chat.view_mode != ChatViewMode::Workspace {
        return 0;
    }
    match variant {
        ChatLayoutVariant::Full if w >= 100 => ((w as f32) * 0.18).floor().max(22.0) as u16,
        ChatLayoutVariant::Mid if w >= 84 => ((w as f32) * 0.24).floor().max(20.0) as u16,
        _ => 0,
    }
}

pub fn chat_content_area(app: &App, w: u16, h: u16) -> Rect {
    let variant = chat_layout_variant(w, h);
    let reserved_bottom = chat_reserved_bottom(app, variant);
    let info_pane_w = chat_info_pane_width(app, w, h);
    let pane_gutter = if info_pane_w > 0 { 5 } else { 0 };
    let left_w = if info_pane_w > 0 { w.saturating_sub(info_pane_w + pane_gutter) } else { w };
    Rect {
        x: if variant == ChatLayoutVariant::Tiny { 1 } else { 2 },
        y: if variant == ChatLayoutVariant::Tiny { 1 } else { 2 },
        width: left_w.saturating_sub(if variant == ChatLayoutVariant::Tiny { 2 } else { 4 }),
        height: h.saturating_sub(reserved_bottom + if variant == ChatLayoutVariant::Tiny { 1 } else { 2 }),
    }
}

pub fn ensure_chat_visual_cache(app: &App, w: u16, h: u16, cache: &mut ChatVisualCache, force: bool) {
    let variant = chat_layout_variant(w, h);
    let content = chat_content_area(app, w, h);
    let width = content.width as usize;
    if force || cache.width != width || cache.variant != Some(variant) {
        cache.lines = build_chat_visual_lines(app, width, variant);
        cache.width = width;
        cache.variant = Some(variant);
    }
}

pub fn chat_selection_bounds(app: &App) -> Option<(ChatTextPoint, ChatTextPoint)> {
    let a = app.chat.selection_anchor?;
    let b = app.chat.selection_focus?;
    if (a.row, a.col) <= (b.row, b.col) { Some((a, b)) } else { Some((b, a)) }
}

pub fn chat_visual_window_len(total_rows: usize, area: Rect, scroll: usize) -> (usize, usize) {
    let max_lines = area.height as usize;
    let clamped = scroll.min(total_rows.saturating_sub(max_lines));
    let visible_count = total_rows.saturating_sub(clamped);
    let start = visible_count.saturating_sub(max_lines);
    let end = start + max_lines.min(visible_count);
    (start.min(total_rows), end.min(total_rows))
}

pub fn copy_chat_selection(app: &mut App, lines: &[ChatRenderLine]) -> bool {
    let Some(bounds) = chat_selection_bounds(app) else {
        app.chat.set_clipboard_notice("Nothing selected", false);
        return false;
    };
    let text = chat_selection_text(lines, bounds);
    if text.is_empty() {
        app.chat.set_clipboard_notice("Nothing selected", false);
        return false;
    }
    match copy_to_clipboard(&text) {
        Ok(path) => {
            app.chat.set_clipboard_notice(format!("Copied via {}", path), true);
            true
        }
        Err(err) => {
            app.chat.set_clipboard_notice(err, false);
            false
        }
    }
}

pub fn draw_chat<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16, repaint_transcript: bool, visual_lines: &[ChatRenderLine]) -> io::Result<()> {
    if repaint_transcript {
        draw_chat_transcript(stdout, app, w, h, visual_lines)?;
    }
    draw_chat_chrome(stdout, app, w, h)
}

pub(crate) fn rowing_indicator_lines(frame: usize) -> Vec<ChatRenderLine> {
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
        copy_text: String::new(),
        copy_offset: 0,
        selectable: false,
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

fn single_span_line(fg: style::Color, bg: Option<style::Color>, text: impl Into<String>) -> ChatRenderLine {
    let text = text.into();
    ChatRenderLine {
        spans: vec![ChatSpan { fg, text: text.clone() }],
        bg,
        copy_text: text,
        copy_offset: 0,
        selectable: true,
    }
}

fn nonselectable_line(fg: style::Color, bg: Option<style::Color>, text: impl Into<String>) -> ChatRenderLine {
    let text = text.into();
    ChatRenderLine {
        spans: vec![ChatSpan { fg, text }],
        bg,
        copy_text: String::new(),
        copy_offset: 0,
        selectable: false,
    }
}

fn brand_lines(width: usize, variant: ChatLayoutVariant) -> Vec<ChatRenderLine> {
    let mid_title = include_str!("../../../../assets/title_ascii_mid.txt");
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
            copy_text: String::new(),
            copy_offset: 0,
            selectable: false,
        });
        out.push(nonselectable_line(subtitle_color, None, "  Agent Operating System"));
        out.push(nonselectable_line(style::Color::Reset, None, String::new()));
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
        out.push(ChatRenderLine { copy_text: String::new(), copy_offset: 0, selectable: false, spans, bg: None });
    }

    out.push(nonselectable_line(subtitle_color, None, "  Agent Operating System"));
    out.push(nonselectable_line(style::Color::Reset, None, String::new()));
    out
}

fn normalize_inline_markdown(text: &str) -> String {
    let mut s = text.to_string();
    if let Ok(re) = regex::Regex::new(r"\[([^\]]+)\]\(([^\)]+)\)") { s = re.replace_all(&s, "$1").to_string(); }
    if let Ok(re) = regex::Regex::new(r"`([^`]+)`") { s = re.replace_all(&s, "‹$1›").to_string(); }
    if let Ok(re) = regex::Regex::new(r"\*\*\*([^*]+)\*\*\*") { s = re.replace_all(&s, "$1").to_string(); }
    if let Ok(re) = regex::Regex::new(r"\*\*([^*]+)\*\*") { s = re.replace_all(&s, "$1").to_string(); }
    if let Ok(re) = regex::Regex::new(r"\*([^*]+)\*") { s = re.replace_all(&s, "$1").to_string(); }
    s
}

fn push_chat_block(lines: &mut Vec<ChatRenderLine>, text: &str, width: usize, fg: style::Color, bg: Option<style::Color>, left_pad: usize) {
    let inner = width.saturating_sub(left_pad);
    for wrapped in wrap_plain_text(text, inner.max(1)) {
        let mut render_line = String::new();
        render_line.push_str(&" ".repeat(left_pad));
        render_line.push_str(&wrapped);
        lines.push(ChatRenderLine { spans: vec![ChatSpan { fg, text: render_line }], bg, copy_text: wrapped, copy_offset: left_pad, selectable: true });
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
    if app.chat.messages.is_empty() {
        visual_lines.push(nonselectable_line(style::Color::DarkGrey, None, "  Welcome to Charon. Type a message to begin."));
        visual_lines.push(nonselectable_line(style::Color::Reset, None, String::new()));
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
                        let border = if code_block { if label.is_empty() { " ┌ code".to_string() } else { format!(" ┌ {}", label) } } else { " └".to_string() };
                        push_chat_block(&mut visual_lines, &border, width, style::Color::DarkGrey, Some(code_bg), 1);
                        continue;
                    }
                    if code_block { push_chat_block(&mut visual_lines, raw, width, code_fg, Some(code_bg), 2); continue; }
                    if let Some(rest) = trimmed.strip_prefix("### ") { push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, robe_heading, Some(robe_bg), 1); continue; }
                    if let Some(rest) = trimmed.strip_prefix("## ") { push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, robe_heading, Some(robe_bg), 1); continue; }
                    if let Some(rest) = trimmed.strip_prefix("# ") { push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(rest)), width, style::Color::Rgb { r: 240, g: 230, b: 208 }, Some(robe_bg), 1); continue; }
                    if trimmed.starts_with("- ") || trimmed.starts_with("* ") { let body = normalize_inline_markdown(&trimmed[2..]); push_chat_block(&mut visual_lines, &format!(" • {}", body), width, robe_fg, Some(robe_bg), 1); continue; }
                    let numbered = trimmed.chars().take_while(|c| c.is_ascii_digit()).count();
                    if numbered > 0 && trimmed.chars().nth(numbered) == Some('.') { push_chat_block(&mut visual_lines, &format!(" {}", normalize_inline_markdown(trimmed)), width, robe_fg, Some(robe_bg), 1); continue; }
                    push_chat_block(&mut visual_lines, &normalize_inline_markdown(raw), width, robe_fg, Some(robe_bg), 1);
                }
                if *streaming { visual_lines.push(single_span_line(robe_fg, Some(robe_bg), "  ▊")); }
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::Thinking { text, streaming } => {
                let header = if text.is_empty() { "  ⟪ visible thoughts ⟫".to_string() } else { format!("  ⟪ visible thoughts ⟫ {}", text.lines().next().unwrap_or("")) };
                push_chat_block(&mut visual_lines, &header, width, style::Color::Rgb { r: 221, g: 214, b: 254 }, Some(thought_bg), 0);
                for extra in text.lines().skip(1) { push_chat_block(&mut visual_lines, &format!(" {}", extra), width, style::Color::Rgb { r: 196, g: 181, b: 253 }, Some(thought_bg), 0); }
                if *streaming { visual_lines.push(single_span_line(style::Color::Rgb { r: 221, g: 214, b: 254 }, Some(thought_bg), "  ▊")); }
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
                for line in content.lines().take(8) { push_chat_block(&mut visual_lines, &format!("   {}", line), width, fg, Some(tool_bg), 0); }
                if content.lines().count() > 8 { push_chat_block(&mut visual_lines, "   ...", width, style::Color::DarkGrey, Some(tool_bg), 0); }
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
            ChatMessage::Status { text } => push_chat_block(&mut visual_lines, &format!("  {}", text), width, style::Color::Yellow, None, 0),
            ChatMessage::Error { text } => push_chat_block(&mut visual_lines, &format!("  ✗ {}", text), width, style::Color::Rgb { r: 248, g: 113, b: 113 }, None, 0),
            ChatMessage::Stderr { text } => push_chat_block(&mut visual_lines, &format!("  stderr: {}", text), width, style::Color::Rgb { r: 248, g: 113, b: 113 }, None, 0),
            ChatMessage::QueuedUser { text, tag } => {
                // Render like a user message but with a dim italic tag prefix
                let tagged = format!("  {}: {}", tag, text);
                let queued_fg = style::Color::Rgb { r: 148, g: 163, b: 184 };
                let queued_bg = style::Color::Rgb { r: 25, g: 30, b: 42 };
                push_chat_block(&mut visual_lines, &tagged, width, queued_fg, Some(queued_bg), 1);
                visual_lines.push(single_span_line(style::Color::Reset, None, String::new()));
            }
        }
    }
    visual_lines
}

fn draw_chat_line<W: Write>(stdout: &mut W, x: u16, y: u16, width: usize, line: &ChatRenderLine, selection_cols: Option<(usize, usize)>) -> io::Result<()> {
    stdout.queue(cursor::MoveTo(x, y))?;
    let mut col = 0usize;
    for span in &line.spans {
        for ch in span.text.chars() {
            if col >= width { break; }
            let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
            stdout.queue(style::SetForegroundColor(if selected { style::Color::Black } else { span.fg }))?;
            stdout.queue(style::SetBackgroundColor(if selected { style::Color::Rgb { r: 226, g: 232, b: 240 } } else { line.bg.unwrap_or(style::Color::Reset) }))?;
            write!(stdout, "{}", ch)?;
            col += 1;
        }
        if col >= width { break; }
    }
    while col < width {
        let selected = selection_cols.map(|(a, b)| col >= a && col < b).unwrap_or(false);
        stdout.queue(style::SetForegroundColor(if selected { style::Color::Black } else { style::Color::Reset }))?;
        stdout.queue(style::SetBackgroundColor(if selected { style::Color::Rgb { r: 226, g: 232, b: 240 } } else { line.bg.unwrap_or(style::Color::Reset) }))?;
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
    render::render_border_colored(stdout, area, title, match charon_box_border_color() { style::Color::Rgb { r, g, b } => crossterm::style::Color::Rgb { r, g, b }, _ => crossterm::style::Color::Rgb { r: 59, g: 24, b: 28 } })
}

fn fmt_k(n: u64) -> String {
    if n >= 1_000_000 { format!("{:.1}M", (n as f64) / 1_000_000.0) } else if n >= 1_000 { format!("{:.1}k", (n as f64) / 1_000.0) } else { n.to_string() }
}

fn session_info_tokens<'a>(app: &'a App) -> Option<&'a Value> {
    app.chat.refresh_payload.as_ref().and_then(|p| p.get("session_info")).and_then(|i| i.get("tokens"))
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
    if used < total { write!(stdout, "{}", " ".repeat(total - used))?; }
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
    let fmt_kk = |n: u64| if n >= 1000 { format!("{:.1}k", (n as f64) / 1000.0) } else { n.to_string() };
    let mut lines: Vec<(style::Color, String)> = Vec::new();
    let tabs = ["Outcomes", "Goals", "Model"];
    let tab_text = tabs.iter().enumerate().map(|(i, tab)| if i == app.chat.info_pane_tab { format!("[{}]", tab) } else { tab.to_string() }).collect::<Vec<_>>().join("  ");
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
                if title.is_empty() { continue; }
                let key = title.to_lowercase();
                if !seen.insert(key) { continue; }
                outcome_rows.push((title, task.get("status").and_then(|v| v.as_str()).unwrap_or("completed").to_string(), task.get("tool_calls").and_then(|v| v.as_u64()), task.get("turns").and_then(|v| v.as_u64()), task.get("files_touched").and_then(|v| v.as_array()).map(|a| a.len()), task.get("tokens_in").and_then(|v| v.as_u64()), task.get("tokens_out").and_then(|v| v.as_u64())));
            }
            if outcome_rows.len() < 8 {
                for provisional in app.chat.provisional_outcomes.iter().rev() {
                    let key = provisional.summary.to_lowercase();
                    if seen.insert(key) {
                        outcome_rows.push((provisional.summary.clone(), if provisional.done { "completed".to_string() } else { "active".to_string() }, None, None, None, None, None));
                    }
                    if outcome_rows.len() >= 8 { break; }
                }
            }
            if outcome_rows.is_empty() {
                lines.push((style::Color::DarkGrey, "No outcomes yet.".to_string()));
                lines.push((style::Color::DarkGrey, "Submit a concrete request".to_string()));
                lines.push((style::Color::DarkGrey, "to track it here.".to_string()));
            } else {
                let mut used = 0usize;
                for (title, status, tool_calls, turns, file_count, tokens_in_row, tokens_out_row) in outcome_rows.into_iter().take(8) {
                    if used >= content_budget { break; }
                    let icon = match status.as_str() { "failed" => "[-]", "active" | "running" | "pending" => "[~]", _ => "[+]" };
                    let color = match status.as_str() { "failed" => style::Color::Rgb { r: 239, g: 68, b: 68 }, "active" | "running" | "pending" => style::Color::Rgb { r: 245, g: 158, b: 11 }, _ => style::Color::Rgb { r: 34, g: 197, b: 94 } };
                    lines.push((color, format!("{} {}", icon, title)));
                    used += 1;
                    let mut meta = Vec::new();
                    if let Some(tool_calls) = tool_calls { meta.push(format!("{}t", tool_calls)); }
                    if let Some(turns) = turns { meta.push(format!("{}↻", turns)); }
                    if let Some(file_count) = file_count { meta.push(format!("{}f", file_count)); }
                    if let Some(tokens_in_row) = tokens_in_row { if tokens_in_row > 0 { meta.push(format!("{}↑", fmt_kk(tokens_in_row))); } }
                    if let Some(tokens_out_row) = tokens_out_row { if tokens_out_row > 0 { meta.push(format!("{}↓", fmt_kk(tokens_out_row))); } }
                    if !meta.is_empty() && used < content_budget { lines.push((style::Color::DarkGrey, format!("  {}", meta.join("  ")))); used += 1; }
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
                let active_suffix = if active_goal_id.is_empty() { String::new() } else { format!("  active:{}", active_goal_id.chars().take(8).collect::<String>()) };
                lines.push((style::Color::DarkGrey, format!("backlog:{}{}", backlog, active_suffix)));
                lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
            }
            if goals.is_empty() {
                lines.push((style::Color::DarkGrey, "No goals detected.".to_string()));
            } else {
                for goal in goals.iter().take(8) {
                    let status = goal.get("status").and_then(|v| v.as_str()).unwrap_or("backlog");
                    let icon = match status { "active" => "●", "proposed" => "◆", "confirmed" => "◉", "executing" => "▶", "verifying" => "?", "completed" => "✓", "failed" => "✗", _ => "○" };
                    let color = match status { "active" => style::Color::Rgb { r: 34, g: 197, b: 94 }, "proposed" => style::Color::Rgb { r: 167, g: 139, b: 250 }, "confirmed" => style::Color::Rgb { r: 96, g: 165, b: 250 }, "executing" => style::Color::Rgb { r: 245, g: 158, b: 11 }, "verifying" => style::Color::Rgb { r: 250, g: 204, b: 21 }, "completed" => style::Color::Rgb { r: 110, g: 231, b: 183 }, "failed" => style::Color::Rgb { r: 239, g: 68, b: 68 }, _ => style::Color::DarkGrey };
                    let title = goal.get("title").and_then(|v| v.as_str()).unwrap_or("");
                    lines.push((color, format!("{} {}", icon, title)));
                }
            }
        }
        _ => {
            if user_model.trim().is_empty() {
                lines.push((style::Color::DarkGrey, "No user model yet.".to_string()));
                lines.push((style::Color::DarkGrey, "Charon learns your".to_string()));
                lines.push((style::Color::DarkGrey, "preferences over time.".to_string()));
            } else {
                for line in user_model.lines().filter(|l| !l.trim().chars().all(|c| c == '═')) { lines.push((style::Color::Rgb { r: 212, g: 196, b: 168 }, line.to_string())); }
            }
        }
    }
    let max_content_end = 2 + content_budget;
    if lines.len() > max_content_end { lines.truncate(max_content_end); }
    lines.push((style::Color::Rgb { r: 59, g: 50, b: 82 }, "─".repeat(area.width.saturating_sub(1) as usize)));
    let chat_in = tokens.and_then(|t| t.get("chat_in")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.input_tokens);
    let chat_out = tokens.and_then(|t| t.get("chat_out")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.output_tokens);
    lines.push((style::Color::DarkGrey, format!("chat: {}↑ {}↓", fmt_kk(chat_in), fmt_kk(chat_out))));
    if let Some(max_ctx) = tokens.and_then(|t| t.get("max_context")).and_then(|v| v.as_u64()).or(app.chat.usage.context_window) { if max_ctx > 0 { lines.push((style::Color::DarkGrey, format!("max ctx: {}", fmt_kk(max_ctx)))); } }
    if let Some(consol) = tokens.and_then(|t| t.get("consolidation_tokens")).and_then(|v| v.as_u64()) { if consol > 0 { lines.push((style::Color::DarkGrey, format!("bg: ~{} consol", fmt_kk(consol)))); } }
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
    if app.chat.refresh_payload.is_none() || app.chat.onboarding_complete() || app.chat.engine_ready() { return vec![]; }
    let step = app.chat.onboarding_step();
    let provider = app.chat.onboarding_provider();
    let project = app.chat.onboarding_project();
    let mut lines = vec!["Setup required before full chat use.".to_string()];
    match step.as_str() {
        "provider-mode" | "" => lines.push("Choose a provider: /setup provider claude-code | codex | lmstudio | api".to_string()),
        "provider-auth" => { lines.push(format!("Provider selected: {}", if provider.is_empty() { "unknown" } else { &provider })); lines.push("Finish authentication in the popup or use /setup auth-code <CODE>".to_string()); }
        "model" => { lines.push(format!("Provider selected: {}", if provider.is_empty() { "unknown" } else { &provider })); lines.push("Choose a model with /setup model or /model".to_string()); }
        "complete" | "done" => lines.push("Setup is almost done. Use /setup complete if needed.".to_string()),
        other => lines.push(format!("Current setup step: {}", other)),
    }
    if !project.is_empty() { lines.push(format!("Project: {}", project)); }
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

fn chat_selection_text(lines: &[ChatRenderLine], bounds: (ChatTextPoint, ChatTextPoint)) -> String {
    let (start, end) = bounds;
    let mut out = String::new();
    let mut wrote_any = false;
    for row_idx in start.row..=end.row {
        let Some(line) = lines.get(row_idx) else { continue; };
        if !line.selectable { continue; }
        let chars: Vec<char> = line.copy_text.chars().collect();
        let line_len = chars.len();
        let from = if row_idx == start.row { start.col.min(line_len) } else { 0 };
        let to = if row_idx == end.row { end.col.min(line_len) } else { line_len };
        if wrote_any { out.push('\n'); }
        if from < to { out.extend(chars[from..to].iter().copied()); }
        wrote_any = true;
    }
    while out.ends_with('\n') { out.pop(); }
    out
}

fn chat_selection_cols_for_row(line: &ChatRenderLine, row: usize, bounds: Option<(ChatTextPoint, ChatTextPoint)>) -> Option<(usize, usize)> {
    let (start, end) = bounds?;
    if !line.selectable || row < start.row || row > end.row { return None; }
    let copy_len = line.copy_text.chars().count();
    let (sel_start, sel_end) = if start.row == end.row { (start.col.min(copy_len), end.col.min(copy_len)) } else if row == start.row { (start.col.min(copy_len), copy_len) } else if row == end.row { (0, end.col.min(copy_len)) } else { (0, copy_len) };
    if sel_start >= sel_end && copy_len > 0 { return None; }
    Some((line.copy_offset + sel_start, line.copy_offset + sel_end))
}

fn tail_visible_text(text: &str, width: usize) -> String {
    if width == 0 { return String::new(); }
    let chars: Vec<char> = text.chars().collect();
    let len = chars.len();
    let start = len.saturating_sub(width);
    chars[start..].iter().collect()
}

fn draw_chat_transcript<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16, visual_lines: &[ChatRenderLine]) -> io::Result<()> {
    let content = chat_content_area(app, w, h);
    let content_w = content.width as usize;
    let max_lines = content.height as usize;
    let max_scroll = visual_lines.len().saturating_sub(max_lines);
    let scroll = app.chat.scroll.min(max_scroll);
    let (start, end) = chat_visual_window_len(visual_lines.len(), content, scroll);
    let slice = &visual_lines[start..end];
    let selection_bounds = chat_selection_bounds(app);

    for (i, line) in slice.iter().enumerate() {
        let row_idx = start + i;
        let selection_cols = chat_selection_cols_for_row(line, row_idx, selection_bounds);
        draw_chat_line(stdout, content.x, content.y + i as u16, content_w, line, selection_cols)?;
    }

    for row in slice.len() as u16..content.height {
        stdout.queue(cursor::MoveTo(content.x, content.y + row))?;
        write!(stdout, "{}", " ".repeat(content.width as usize))?;
    }
    Ok(())
}

fn draw_chat_chrome<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
    let variant = chat_layout_variant(w, h);
    let info_pane_w = chat_info_pane_width(app, w, h);
    let pane_gutter = if info_pane_w > 0 { 5 } else { 0 };
    let left_w = if info_pane_w > 0 { w.saturating_sub(info_pane_w + pane_gutter) } else { w };
    if info_pane_w > 0 {
        let area = Rect { x: left_w + 2, y: 2, width: info_pane_w.saturating_sub(2), height: h.saturating_sub(4) };
        draw_info_panel(stdout, app, area)?;
    } else if app.chat.view_mode == ChatViewMode::Transcript && app.chat.info_pane_open && variant != ChatLayoutVariant::Tiny {
        let overlay_w = (w.saturating_sub(8)).min(42).max(28);
        let overlay_h = h.saturating_sub(8).min(28).max(12);
        let area = Rect { x: w.saturating_sub(overlay_w + 3), y: 3, width: overlay_w, height: overlay_h };
        draw_info_panel(stdout, app, area)?;
    }
    let provider = app.chat.provider_model();
    let onboarding = if app.chat.refresh_payload.is_none() { "loading" } else if app.chat.onboarding_complete() { "complete" } else { "setup" };
    let onboarding_project = app.chat.onboarding_project();
    let project_name = onboarding_project.split('/').filter(|s| !s.is_empty()).last().unwrap_or("");
    let session_display = if app.chat.session_id.is_empty() { "none" } else { &app.chat.session_id };
    let left1 = if app.chat.onboarding_complete() { let mut parts = vec![session_display.to_string()]; if !project_name.is_empty() { parts.push(project_name.to_string()); } format!("  {}", parts.join("  ")) } else { format!("  charon  onboarding:{}", onboarding) };
    let right1 = format!("{} {}  effort:medium", if provider.contains("api") { "(api)" } else { "(provider)" }, provider);
    let tokens = session_info_tokens(app);
    let chat_in = tokens.and_then(|t| t.get("chat_in")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.input_tokens);
    let chat_out = tokens.and_then(|t| t.get("chat_out")).and_then(|v| v.as_u64()).unwrap_or(app.chat.usage.output_tokens);
    let max_ctx = tokens.and_then(|t| t.get("max_context")).and_then(|v| v.as_u64()).or(app.chat.usage.context_window).unwrap_or(0);
    let goal_inf = tokens.and_then(|t| t.get("goal_inference_tokens")).and_then(|v| v.as_u64()).unwrap_or(0);
    let consol = tokens.and_then(|t| t.get("consolidation_tokens")).and_then(|v| v.as_u64()).unwrap_or(0);
    let ctx = app.chat.usage.context_pct.map(|v| format!("{:.0}%", v)).unwrap_or_else(|| "-".to_string());
    let left2 = if app.chat.onboarding_complete() { let mut parts = vec!["  ♡ interactive".to_string(), format!("ctx:{}", ctx), format!("chat:{}↑ {}↓", fmt_k(chat_in), fmt_k(chat_out))]; if max_ctx > 0 { parts.push(format!("max:{}", fmt_k(max_ctx))); } if goal_inf > 0 { parts.push(format!("goal:{}", fmt_k(goal_inf))); } if consol > 0 { parts.push(format!("bg:{}", fmt_k(consol))); } if let Some(hint) = app.chat.orchestration_parse_hint() { parts.push(hint); } parts.push(format!("thoughts:{}{}", if app.chat.show_thoughts { "on" } else { "off" }, if app.chat.show_timestamps { "  ⏱" } else { "" })); parts.join("  ") } else { "  type / for commands".to_string() };
    let mode_label = match app.chat.view_mode { ChatViewMode::Transcript => "transcript", ChatViewMode::Workspace => "workspace" };
    let mouse_mode = if app.chat.app_mouse_mode { "mouse:app" } else { "mouse:terminal" };
    let mut right2 = match app.chat.view_mode {
        ChatViewMode::Transcript => if app.chat.streaming { format!("F5:{}  Native wheel/select/right-click  F6:{}", mode_label, mouse_mode) } else { format!("F5:{}  Native wheel/select/right-click  Ctrl+P:peek  F6:{}", mode_label, mouse_mode) },
        ChatViewMode::Workspace => if app.chat.streaming { format!("F5:{}  Wheel:scroll  Persistent pane  F6:{}", mode_label, mouse_mode) } else { format!("F5:{}  Wheel:scroll  Persistent pane  ←/→/Ctrl+I tabs  F6:{}", mode_label, mouse_mode) },
    };
    if let Some((notice, _ok)) = app.chat.clipboard_notice_text() { right2 = notice.to_string(); }
    if chat_rowing_active(app) && variant != ChatLayoutVariant::Tiny {
        let frame = ((animation_clock_start().elapsed().as_millis() / 300) % 4) as usize;
        let activity_lines = rowing_indicator_lines(frame);
        let activity_y = h.saturating_sub(9);
        for (i, line) in activity_lines.iter().enumerate() {
            if activity_y + i as u16 >= h.saturating_sub(5) { break; }
            draw_chat_line(stdout, 2, activity_y + i as u16, left_w.saturating_sub(4) as usize, line, None)?;
        }
    }
    let input_area = if variant == ChatLayoutVariant::Tiny {
        let input_area = Rect { x: 1, y: h.saturating_sub(3), width: left_w.saturating_sub(2), height: 1 };
        if input_area.width > 0 { draw_chat_border(stdout, input_area, "")?; stdout.queue(cursor::MoveTo(input_area.x, input_area.y))?; stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 248, g: 250, b: 252 }))?; let prompt = format!("> {}", app.chat.input); let visible = tail_visible_text(&prompt, input_area.width as usize); let pad = (input_area.width as usize).saturating_sub(visible.chars().count()); write!(stdout, "{}{}", visible, " ".repeat(pad))?; stdout.queue(style::SetForegroundColor(style::Color::Reset))?; Some(input_area) } else { None }
    } else {
        let input_area = Rect { x: 1, y: h.saturating_sub(5), width: left_w.saturating_sub(2), height: 1 };
        draw_chat_border(stdout, input_area, "")?;
        stdout.queue(cursor::MoveTo(input_area.x, input_area.y))?;
        stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: 248, g: 250, b: 252 }))?;
        let prompt = format!("> {}", app.chat.input);
        let visible = tail_visible_text(&prompt, input_area.width as usize);
        let pad = (input_area.width as usize).saturating_sub(visible.chars().count());
        write!(stdout, "{}{}", visible, " ".repeat(pad))?;
        stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        Some(input_area)
    };
    match variant {
        ChatLayoutVariant::Full => { draw_chat_status_line(stdout, h.saturating_sub(3), left_w, &left1, &right1, style::Color::Rgb { r: 85, g: 85, b: 112 })?; draw_chat_status_line(stdout, h.saturating_sub(2), left_w, &left2, &right2, if app.chat.streaming { style::Color::Rgb { r: 180, g: 83, b: 9 } } else { style::Color::Rgb { r: 59, g: 59, b: 79 } })?; }
        ChatLayoutVariant::Mid => { let combined = format!("{}  │  {}", left2, right2); draw_chat_status_line(stdout, h.saturating_sub(2), left_w, &combined, "", style::Color::Rgb { r: 59, g: 59, b: 79 })?; }
        ChatLayoutVariant::Tiny => { let hint = if app.chat.streaming { "Enter: steer" } else { "interactive" }; draw_chat_status_line(stdout, h.saturating_sub(1), left_w, &format!("  {}", hint), "", style::Color::Rgb { r: 59, g: 59, b: 79 })?; }
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
        let visible_rows = area.height as usize;
        let total_items = app.chat.menu_items.len();
        let start_idx = if total_items > visible_rows { app.chat.menu_index.saturating_sub(visible_rows.saturating_sub(1)).min(total_items.saturating_sub(visible_rows)) } else { 0 };
        for (row, item) in app.chat.menu_items.iter().skip(start_idx).take(visible_rows).enumerate() {
            let item_idx = start_idx + row;
            stdout.queue(cursor::MoveTo(area.x, area.y + row as u16))?;
            let selected = item_idx == app.chat.menu_index;
            stdout.queue(style::SetForegroundColor(if selected { style::Color::Rgb { r: 196, g: 181, b: 253 } } else if item.executable { style::Color::Rgb { r: 226, g: 232, b: 240 } } else { style::Color::Rgb { r: 148, g: 163, b: 184 } }))?;
            let mut line = if item.age.is_empty() { format!("{} {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc) } else { format!("{} {}  {}  {}", if selected { "▸" } else { " " }, item.cmd, item.desc, item.age) };
            line = line.chars().take(area.width as usize).collect();
            write!(stdout, "{}", line)?;
            stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
        }
    }
    Ok(())
}
