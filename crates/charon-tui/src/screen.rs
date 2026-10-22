//! Double-buffered terminal rendering.
//!
//! Render each frame into a `ScreenBuf`, then call `flush` to diff against
//! the previous frame and emit only the changed cells.  Tracks cursor position
//! and last-emitted style so redundant ANSI sequences are suppressed.

use std::io::Write;
use unicode_width::UnicodeWidthChar;

/// Terminal columns, never byte/scalar count. Controls are rendered as spaces.
pub fn char_columns(ch: char) -> usize {
    if ch.is_control() { 1 } else { ch.width().unwrap_or(0) }
}

use crossterm::style::Color;
use crossterm::{cursor, style, QueueableCommand};

// ── Cell ────────────────────────────────────────────────────────────────────

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    pub fg: Color,
    pub bg: Color,
    pub bold: bool,
}

impl Default for Cell {
    fn default() -> Self {
        Self { ch: ' ', fg: Color::Reset, bg: Color::Reset, bold: false }
    }
}

// ── ScreenBuf ───────────────────────────────────────────────────────────────

pub struct ScreenBuf {
    pub width: u16,
    pub height: u16,
    cells: Vec<Cell>,
}

impl ScreenBuf {
    pub fn new(w: u16, h: u16) -> Self {
        Self { width: w, height: h, cells: vec![Cell::default(); w as usize * h as usize] }
    }

    pub fn resize(&mut self, w: u16, h: u16) {
        self.width = w;
        self.height = h;
        self.cells = vec![Cell::default(); w as usize * h as usize];
    }

    pub fn clear(&mut self) {
        self.cells.fill(Cell::default());
    }

    #[inline]
    fn idx(&self, x: u16, y: u16) -> usize {
        y as usize * self.width as usize + x as usize
    }

    #[inline]
    pub fn set(&mut self, x: u16, y: u16, cell: Cell) {
        if x < self.width && y < self.height {
            let i = self.idx(x, y);
            // NUL is reserved internally for the trailing half of a wide cell.
            let mut cell = cell;
            if cell.ch.is_control() { cell.ch = ' '; }
            let width = char_columns(cell.ch);
            if width == 0 { return; } // standalone combining/format controls cannot occupy a cell
            if x > 0 && self.cells[i].ch == '\0' {
                self.cells[i - 1] = Cell::default();
            }
            if char_columns(self.cells[i].ch) == 2 && x + 1 < self.width {
                self.cells[i + 1] = Cell::default();
            }
            if width == 2 {
                if x + 1 >= self.width { cell.ch = ' '; }
                else { self.cells[i + 1] = Cell { ch: '\0', ..cell }; }
            }
            self.cells[i] = cell;
        }
    }

    /// Write a string left-to-right starting at (x, y), clipping at the right edge.
    pub fn put_str(&mut self, x: u16, y: u16, text: &str, fg: Color, bg: Color, bold: bool) {
        let mut col = x;
        for ch in text.chars() {
            if col >= self.width {
                break;
            }
            let width = char_columns(ch) as u16;
            if width == 0 { continue; }
            if col.saturating_add(width) > self.width { break; }
            self.set(col, y, Cell { ch, fg, bg, bold });
            col += width;
        }
    }

    /// Fill columns [x0, x1) on row y with the given character and style.
    pub fn fill(&mut self, y: u16, x0: u16, x1: u16, ch: char, fg: Color, bg: Color) {
        let end = x1.min(self.width);
        for x in x0..end {
            self.set(x, y, Cell { ch, fg, bg, bold: false });
        }
    }
}

// ── Flush (diff emitter) ────────────────────────────────────────────────────

/// Compare frames and repaint changed rows, including their blank margins.
/// Tracks cursor position and style state to minimise ANSI output.
pub fn flush(
    current: &ScreenBuf,
    prev: &ScreenBuf,
    stdout: &mut impl Write,
) -> std::io::Result<()> {
    use std::io::BufWriter;

    let mut out = BufWriter::with_capacity(32 * 1024, stdout);
    let mut last_fg: Option<Color> = None;
    let mut last_bg: Option<Color> = None;
    let mut last_bold: Option<bool> = None;
    let mut cx: u16 = u16::MAX;
    let mut cy: u16 = u16::MAX;
    let same_size = prev.width == current.width && prev.height == current.height;

    out.queue(cursor::Hide)?;

    for y in 0..current.height {
        let row_off = y as usize * current.width as usize;

        // Fast path: skip entire row if unchanged
        if same_size {
            let cur_row = &current.cells[row_off..row_off + current.width as usize];
            let prev_row = &prev.cells[row_off..row_off + current.width as usize];
            if cur_row == prev_row {
                continue;
            }
        }

        for x in 0..current.width {
            let cur = current.cells[row_off + x as usize];
            // Repaint the whole changed row. A prior wide glyph may have
            // occupied a neighbour whose logical cell is otherwise unchanged.
            if cur.ch == '\0' { continue; }

            // Position cursor
            if cy != y || cx != x {
                out.queue(cursor::MoveTo(x, y))?;
            }

            // Bold
            if last_bold != Some(cur.bold) {
                if cur.bold {
                    out.queue(style::SetAttribute(style::Attribute::Bold))?;
                } else {
                    out.queue(style::SetAttribute(style::Attribute::NormalIntensity))?;
                }
                last_bold = Some(cur.bold);
            }

            // Foreground
            if last_fg != Some(cur.fg) {
                out.queue(style::SetForegroundColor(cur.fg))?;
                last_fg = Some(cur.fg);
            }

            // Background
            if last_bg != Some(cur.bg) {
                out.queue(style::SetBackgroundColor(cur.bg))?;
                last_bg = Some(cur.bg);
            }

            // Character — write UTF-8 directly to avoid format machinery
            let mut buf = [0u8; 4];
            let s = cur.ch.encode_utf8(&mut buf);
            out.write_all(s.as_bytes())?;
            cx = x.saturating_add(char_columns(cur.ch) as u16);
            cy = y;
        }
    }

    // Reset terminal state
    out.queue(style::SetAttribute(style::Attribute::Reset))?;
    out.queue(style::SetForegroundColor(Color::Reset))?;
    out.queue(style::SetBackgroundColor(Color::Reset))?;
    out.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_text_reserves_columns_and_never_crosses_right_edge() {
        let mut buf = ScreenBuf::new(8, 2);
        buf.put_str(1, 0, "A界🧠B", Color::Reset, Color::Reset, false);
        assert_eq!(buf.cells.iter().take(8).map(|c| c.ch).collect::<Vec<_>>(),
                   vec![' ', 'A', '界', '\0', '🧠', '\0', 'B', ' ']);
        buf.put_str(7, 1, "界", Color::Reset, Color::Reset, false);
        assert_eq!(buf.cells[15].ch, ' ');
        let mut ansi = Vec::new();
        flush(&buf, &ScreenBuf::new(0, 0), &mut ansi).unwrap();
        assert!(!ansi.contains(&0));
    }

    #[test]
    fn shrinking_content_repaints_blank_side_margins() {
        let mut old = ScreenBuf::new(12, 1);
        old.put_str(2, 0, "old content", Color::Reset, Color::Reset, false);
        let mut new = ScreenBuf::new(12, 1);
        new.put_str(2, 0, "new", Color::Reset, Color::Reset, false);
        let mut ansi = Vec::new();
        flush(&new, &old, &mut ansi).unwrap();
        let text = String::from_utf8(ansi).unwrap();
        assert!(text.contains("  new       "), "row and gutters must be repainted: {text:?}");
        let mut idle = Vec::new();
        flush(&new, &new, &mut idle).unwrap();
        assert!(!String::from_utf8(idle).unwrap().contains("new"));
    }

    #[test]
    fn overwriting_either_half_clears_wide_glyph() {
        let mut buf = ScreenBuf::new(5, 1);
        buf.put_str(1, 0, "界", Color::Reset, Color::Reset, false);
        buf.set(2, 0, Cell { ch: 'x', ..Cell::default() });
        assert_eq!(buf.cells[1].ch, ' ');
        assert_eq!(buf.cells[2].ch, 'x');
        buf.put_str(1, 0, "界", Color::Reset, Color::Reset, false);
        buf.set(1, 0, Cell { ch: 'a', ..Cell::default() });
        assert_eq!(buf.cells[2].ch, ' ');
    }

    #[test]
    fn control_characters_cannot_move_the_physical_cursor() {
        let mut buf = ScreenBuf::new(20, 1);
        buf.put_str(0, 0, "a\nb\rc\td\u{1b}[2J", Color::Reset, Color::Reset, false);
        assert!(buf.cells.iter().all(|c| !c.ch.is_control()));
        let mut ansi = Vec::new();
        flush(&buf, &ScreenBuf::new(0, 0), &mut ansi).unwrap();
        let text = String::from_utf8(ansi).unwrap();
        assert!(!text.contains("\u{1b}[2J"));
        assert!(!text.contains('\n'));
    }
}
