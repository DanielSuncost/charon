/// Render — blit a TerminalState grid into the outer terminal via crossterm.

use std::io::{self, Write};

use crossterm::{
    cursor,
    style::{Attribute, Color as CtColor, SetAttribute, SetBackgroundColor, SetForegroundColor},
    QueueableCommand,
};

use crate::terminal::{Color, CursorShape, TerminalState};

/// Viewport rectangle in the outer terminal.
#[derive(Clone, Copy, Debug)]
pub struct Rect {
    pub x: u16,
    pub y: u16,
    pub width: u16,
    pub height: u16,
}

/// Convert our Color enum to a crossterm Color.
fn to_ct_color(color: Color) -> CtColor {
    match color {
        Color::Default => CtColor::Reset,
        Color::Indexed(idx) => CtColor::AnsiValue(idx),
        Color::Rgb(r, g, b) => CtColor::Rgb { r, g, b },
    }
}

/// Render a border around a viewport area.
pub fn render_border<W: Write>(
    out: &mut W,
    area: Rect,
    title: &str,
    focused: bool,
) -> io::Result<()> {
    let border_color = if focused {
        CtColor::Rgb { r: 167, g: 139, b: 250 } // purple accent
    } else {
        CtColor::Rgb { r: 59, g: 50, b: 82 }    // dim border
    };
    render_border_colored(out, area, title, border_color)
}

pub fn render_border_colored<W: Write>(
    out: &mut W,
    area: Rect,
    title: &str,
    border_color: CtColor,
) -> io::Result<()> {
    let title_color = CtColor::Rgb { r: 212, g: 196, b: 168 };

    // Top border with brighter title text
    out.queue(cursor::MoveTo(area.x.saturating_sub(1), area.y.saturating_sub(1)))?;
    out.queue(SetForegroundColor(border_color))?;
    write!(out, "╭─ ")?;
    out.queue(SetForegroundColor(title_color))?;
    write!(out, "{}", title)?;
    out.queue(SetForegroundColor(border_color))?;
    write!(out, " ")?;
    let inner_width = area.width as usize;
    let prefix_display_len = 4 + title.len(); // ╭─ <title> <space> in display columns
    let fill_count = inner_width.saturating_sub(prefix_display_len);
    let fill = "─".repeat(fill_count);
    write!(out, "{}╮", fill)?;

    // Side borders
    for row in 0..area.height {
        out.queue(cursor::MoveTo(area.x.saturating_sub(1), area.y + row))?;
        write!(out, "│")?;
        out.queue(cursor::MoveTo(area.x + area.width, area.y + row))?;
        write!(out, "│")?;
    }

    // Bottom border
    out.queue(cursor::MoveTo(area.x.saturating_sub(1), area.y + area.height))?;
    let bottom = "─".repeat(area.width as usize);
    write!(out, "╰{}╯", bottom)?;

    out.queue(SetForegroundColor(CtColor::Reset))?;
    Ok(())
}

fn scrolled_cell<'a>(
    terminal: &'a TerminalState,
    viewport_scroll: usize,
    visible_rows: u16,
    col: u16,
    row: u16,
) -> Option<&'a crate::terminal::Cell> {
    let total_rows = terminal.scrollback.len() + terminal.height as usize;
    let start_row = total_rows.saturating_sub(viewport_scroll + visible_rows as usize);
    let render_row = start_row + row as usize;
    if render_row < terminal.scrollback.len() {
        let line = terminal.scrollback.get(render_row)?;
        line.cells.get(col as usize)
    } else {
        let grid_row = (render_row - terminal.scrollback.len()) as u16;
        if col < terminal.width && grid_row < terminal.height {
            Some(terminal.cell(col, grid_row))
        } else {
            None
        }
    }
}

/// Blit the TerminalState grid into the outer terminal at the given area.
#[allow(unused_assignments)] // last_* track the previous cell's style; the final write is intentionally unread
pub fn render_terminal<W: Write>(
    out: &mut W,
    terminal: &TerminalState,
    area: Rect,
    viewport_scroll: usize,
) -> io::Result<()> {
    let render_h = area.height.min(terminal.height);
    let render_w = area.width.min(terminal.width);

    let mut last_fg = Color::Default;
    let mut last_bg = Color::Default;
    let mut last_bold = false;
    let mut last_dim = false;
    let mut last_italic = false;
    let mut last_underline = false;

    for row in 0..render_h {
        out.queue(cursor::MoveTo(area.x, area.y + row))?;

        // Reset at start of each row for clean state
        out.queue(SetForegroundColor(CtColor::Reset))?;
        out.queue(SetBackgroundColor(CtColor::Reset))?;
        out.queue(SetAttribute(Attribute::Reset))?;
        last_fg = Color::Default;
        last_bg = Color::Default;
        last_bold = false;
        last_dim = false;
        last_italic = false;
        last_underline = false;

        for col in 0..render_w {
            let default_cell = crate::terminal::Cell::default();
            let cell = scrolled_cell(terminal, viewport_scroll, render_h, col, row).unwrap_or(&default_cell);

            // Resolve colors (handle inverse)
            let (fg, bg) = if cell.attrs.inverse {
                (cell.bg, cell.fg)
            } else {
                (cell.fg, cell.bg)
            };

            // Only emit color changes when they differ
            if fg != last_fg {
                out.queue(SetForegroundColor(to_ct_color(fg)))?;
                last_fg = fg;
            }
            if bg != last_bg {
                out.queue(SetBackgroundColor(to_ct_color(bg)))?;
                last_bg = bg;
            }

            // Attributes
            if cell.attrs.bold != last_bold {
                if cell.attrs.bold {
                    out.queue(SetAttribute(Attribute::Bold))?;
                } else {
                    out.queue(SetAttribute(Attribute::NormalIntensity))?;
                }
                last_bold = cell.attrs.bold;
            }
            // Intentionally ignore terminal "dim" in session panes so live agent
            // output remains readable in the grid while monitoring multiple sessions.
            if cell.attrs.dim != last_dim {
                if !cell.attrs.dim {
                    out.queue(SetAttribute(Attribute::NormalIntensity))?;
                }
                last_dim = cell.attrs.dim;
            }
            if cell.attrs.italic != last_italic {
                if cell.attrs.italic {
                    out.queue(SetAttribute(Attribute::Italic))?;
                } else {
                    out.queue(SetAttribute(Attribute::NoItalic))?;
                }
                last_italic = cell.attrs.italic;
            }
            if cell.attrs.underline != last_underline {
                if cell.attrs.underline {
                    out.queue(SetAttribute(Attribute::Underlined))?;
                } else {
                    out.queue(SetAttribute(Attribute::NoUnderline))?;
                }
                last_underline = cell.attrs.underline;
            }

            write!(out, "{}", cell.ch)?;
        }
    }

    // Reset attributes after rendering
    out.queue(SetAttribute(Attribute::Reset))?;
    out.queue(SetForegroundColor(CtColor::Reset))?;
    out.queue(SetBackgroundColor(CtColor::Reset))?;

    // Render cursor if visible
    if viewport_scroll == 0 && terminal.cursor.visible {
        let cx = terminal.cursor.x;
        let cy = terminal.cursor.y;
        if cx < render_w && cy < render_h {
            out.queue(cursor::MoveTo(area.x + cx, area.y + cy))?;

            match terminal.cursor.shape {
                CursorShape::Block => {
                    out.queue(cursor::SetCursorStyle::SteadyBlock)?;
                }
            }
            out.queue(cursor::Show)?;
        }
    }

    Ok(())
}
