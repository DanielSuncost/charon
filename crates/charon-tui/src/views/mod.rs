//! View rendering modules for the charon TUI (F1-F4) plus shared chrome
//! (header/footer) and small cross-view helpers.

pub(crate) mod chat_view;
pub(crate) mod dashboard_view;
pub(crate) mod inter_agent_view;
pub(crate) mod sessions_view;

use std::io::{self, Write};

use crossterm::{cursor, style, QueueableCommand};
use serde_json::Value;

use crate::app::{App, View};
use crate::chat::ChatViewMode;
use crate::config;
use crate::render::Rect;
use crate::screen;

use self::sessions_view::grid_tabs;

pub(crate) fn payload_agents(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("agents"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

pub(crate) fn payload_projects(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("projects"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

pub(crate) fn payload_automations(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("automations"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}


pub(crate) fn payload_inter_agent_rooms(payload: Option<&Value>) -> Vec<&Value> {
    payload
        .and_then(|p| p.get("inter_agent_rooms"))
        .and_then(|a| a.as_array())
        .map(|v| v.iter().collect())
        .unwrap_or_default()
}

pub(crate) fn terminal_window_title(app: &App) -> String {
    let project = app.chat.onboarding_project();
    let project_name = project
        .split('/')
        .filter(|s| !s.is_empty())
        .last()
        .unwrap_or("default");
    format!("charon-{}", project_name)
}

pub(crate) fn draw_header<W: Write>(stdout: &mut W, app: &App, w: u16) -> io::Result<()> {
    write!(stdout, "\x1b]0;{}\x07", terminal_window_title(app))?;
    stdout.queue(cursor::MoveTo(0, 0))?;
    let hdr = config::active().theme.header;
    stdout.queue(style::SetForegroundColor(style::Color::Rgb { r: hdr.0, g: hdr.1, b: hdr.2 }))?;

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
                " │ grid mode (Enter to interact) │ |/- split = reset │ F6:mouse terminal"
            } else {
                " │ grid mode (native select/copy) │ |/- split = reset │ F6:mouse app"
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

    // Tab strip (F3 grid, when more than one tab exists): ‹active› others
    let tab_suffix = if app.active_view == View::Sessions && !app.sessions.terminal_mode {
        let tabs = grid_tabs(app);
        if tabs.len() > 1 {
            let strip = tabs
                .iter()
                .map(|t| if *t == app.sessions.active_tab { format!("‹{t}›") } else { t.clone() })
                .collect::<Vec<_>>()
                .join(" ");
            format!(" │ tabs: {strip} ([ ] switch, t new)")
        } else {
            String::new()
        }
    } else {
        String::new()
    };
    let header = format!(
        " CHARON │ {} │ F1:chat │ F2:dash │ F3:sessions │ F4:groups │ Ctrl+Q:quit{}{} ",
        view, extra, tab_suffix
    );
    let visible: String = header.chars().take(w as usize).collect();
    let pad = (w as usize).saturating_sub(visible.chars().count());
    write!(stdout, "{}{}", visible, " ".repeat(pad))?;
    stdout.queue(style::SetForegroundColor(style::Color::Reset))?;
    Ok(())
}

pub(crate) fn draw_footer<W: Write>(stdout: &mut W, app: &App, w: u16, h: u16) -> io::Result<()> {
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

pub(crate) fn draw_header_buf(buf: &mut screen::ScreenBuf, app: &App, w: u16) {
    let hdr = config::active().theme.header;
    let fg = style::Color::Rgb { r: hdr.0, g: hdr.1, b: hdr.2 };
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

pub(crate) fn draw_footer_buf(buf: &mut screen::ScreenBuf, _app: &App, w: u16, h: u16) {
    let y = h.saturating_sub(1);
    buf.fill(y, 0, w, ' ', style::Color::Reset, style::Color::Reset);
}


pub(crate) fn point_in_rect(area: Rect, x: u16, y: u16) -> bool {
    let left = area.x.saturating_sub(1);
    let top = area.y.saturating_sub(1);
    let right = area.x + area.width;
    let bottom = area.y + area.height;
    x >= left && x <= right && y >= top && y <= bottom
}


pub(crate) fn session_ids_match(a: &str, b: &str) -> bool {
    if a.is_empty() || b.is_empty() {
        return false;
    }
    a == b
        || (!a.starts_with("boat-") && format!("boat-{}", a) == b)
        || (!b.starts_with("boat-") && a == format!("boat-{}", b))
}

pub(crate) fn keep_index_visible(index: usize, scroll: &mut usize, height: usize) {
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
