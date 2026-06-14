//! Double-buffered terminal rendering.
//!
//! Render each frame into a `ScreenBuf`, then call `flush` to diff against
//! the previous frame and emit only the changed cells.  Tracks cursor position
//! and last-emitted style so redundant ANSI sequences are suppressed.

use std::io::Write;

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
    #[allow(dead_code)] // accessor kept for the type's interface
    pub fn get(&self, x: u16, y: u16) -> Cell {
        if x < self.width && y < self.height {
            self.cells[self.idx(x, y)]
        } else {
            Cell::default()
        }
    }

    #[inline]
    pub fn set(&mut self, x: u16, y: u16, cell: Cell) {
        if x < self.width && y < self.height {
            let i = self.idx(x, y);
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
            self.set(col, y, Cell { ch, fg, bg, bold });
            col += 1;
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

/// Compare `current` against `prev` and emit only the cells that changed.
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
            if same_size && cur == prev.cells[row_off + x as usize] {
                continue;
            }

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
            cx = x + 1;
            cy = y;
        }
    }

    // Reset terminal state
    out.queue(style::SetAttribute(style::Attribute::Reset))?;
    out.queue(style::SetForegroundColor(Color::Reset))?;
    out.queue(style::SetBackgroundColor(Color::Reset))?;
    out.flush()
}
