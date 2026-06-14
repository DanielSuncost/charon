/// TerminalState — flat grid VTE terminal emulator with cursor, scrollback,
/// alternate screen buffer, and dirty tracking.

use std::collections::VecDeque;

// ── Cell attributes ─────────────────────────────────────────────────────────

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct CellAttrs {
    pub bold: bool,
    pub dim: bool,
    pub italic: bool,
    pub underline: bool,
    pub blink: bool,
    pub inverse: bool,
    pub hidden: bool,
    pub strikethrough: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Color {
    Default,
    Indexed(u8),          // 0-255
    Rgb(u8, u8, u8),      // 24-bit truecolor
}

impl Default for Color {
    fn default() -> Self {
        Color::Default
    }
}

// ── Cell ────────────────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
pub struct Cell {
    pub ch: char,
    pub fg: Color,
    pub bg: Color,
    pub attrs: CellAttrs,
}

impl Default for Cell {
    fn default() -> Self {
        Cell {
            ch: ' ',
            fg: Color::Default,
            bg: Color::Default,
            attrs: CellAttrs::default(),
        }
    }
}

// ── Cursor ──────────────────────────────────────────────────────────────────

#[derive(Clone, Copy, Debug, PartialEq)]
#[allow(dead_code)] // full DECSCUSR cursor set; not all shapes emitted yet
pub enum CursorShape {
    Block,
    Bar,
    Underline,
}

#[derive(Clone, Debug)]
pub struct Cursor {
    pub x: u16,
    pub y: u16,
    pub shape: CursorShape,
    pub visible: bool,
}

impl Default for Cursor {
    fn default() -> Self {
        Cursor {
            x: 0,
            y: 0,
            shape: CursorShape::Block,
            visible: true,
        }
    }
}

// ── Scrollback line ─────────────────────────────────────────────────────────

#[derive(Clone, Debug)]
#[allow(dead_code)] // soft_wrapped reserved for reflow
pub struct Line {
    pub cells: Vec<Cell>,
    pub soft_wrapped: bool, // true if this line was wrapped (not a hard newline)
}

// ── Pen (current text style applied to new characters) ──────────────────────

#[derive(Clone, Debug, Default)]
struct Pen {
    fg: Color,
    bg: Color,
    attrs: CellAttrs,
}

// ── Saved cursor state (for DECSC/DECRC) ────────────────────────────────────

#[derive(Clone, Debug)]
struct SavedCursor {
    x: u16,
    y: u16,
    pen: Pen,
}

// ── TerminalState ───────────────────────────────────────────────────────────

pub struct TerminalState {
    pub width: u16,
    pub height: u16,
    pub grid: Vec<Cell>,               // flat: width * height, indexed [y * width + x]
    pub cursor: Cursor,
    pub scrollback: VecDeque<Line>,
    pub dirty: bool,

    // Alternate screen buffer (for vim, htop, etc.)
    alt_grid: Option<Vec<Cell>>,
    alt_cursor: Option<Cursor>,
    pub in_alt_screen: bool,

    // Internal state
    pen: Pen,
    saved_cursor: Option<SavedCursor>,
    scroll_top: u16,                   // scroll region top (inclusive)
    scroll_bottom: u16,                // scroll region bottom (inclusive)
    max_scrollback: usize,

    // Tab stops
    tab_stops: Vec<bool>,

    // Origin mode (DECOM): cursor addressing relative to scroll region
    origin_mode: bool,

    // Auto-wrap mode (DECAWM)
    auto_wrap: bool,
    // Pending wrap: cursor is at right margin, next printable char wraps
    pending_wrap: bool,

    // DEC Special Graphics charset (line-drawing mode)
    // When true, certain ASCII chars map to box-drawing Unicode chars
    pub charset_g0_special: bool,
}

impl TerminalState {
    pub fn new(width: u16, height: u16) -> Self {
        let size = (width as usize) * (height as usize);
        let mut tab_stops = vec![false; width as usize];
        for i in (0..width as usize).step_by(8) {
            tab_stops[i] = true;
        }
        TerminalState {
            width,
            height,
            grid: vec![Cell::default(); size],
            cursor: Cursor::default(),
            scrollback: VecDeque::new(),
            dirty: true,
            alt_grid: None,
            alt_cursor: None,
            in_alt_screen: false,
            pen: Pen::default(),
            saved_cursor: None,
            scroll_top: 0,
            scroll_bottom: height.saturating_sub(1),
            max_scrollback: 10_000,
            tab_stops,
            origin_mode: false,
            auto_wrap: true,
            pending_wrap: false,
            charset_g0_special: false,
        }
    }

    // ── Grid access ──────────────────────────────────────────────────────

    #[inline]
    pub(crate) fn idx(&self, x: u16, y: u16) -> usize {
        (y as usize) * (self.width as usize) + (x as usize)
    }

    #[inline]
    pub fn cell(&self, x: u16, y: u16) -> &Cell {
        &self.grid[self.idx(x, y)]
    }

    #[inline]
    pub(crate) fn cell_mut(&mut self, x: u16, y: u16) -> &mut Cell {
        let i = self.idx(x, y);
        &mut self.grid[i]
    }

    // ── Alternate screen buffer ──────────────────────────────────────────

    pub fn enter_alt_screen(&mut self) {
        if self.in_alt_screen {
            return;
        }
        let size = (self.width as usize) * (self.height as usize);
        self.alt_grid = Some(std::mem::replace(
            &mut self.grid,
            vec![Cell::default(); size],
        ));
        self.alt_cursor = Some(self.cursor.clone());
        self.cursor = Cursor::default();
        self.in_alt_screen = true;
        self.dirty = true;
    }

    pub fn exit_alt_screen(&mut self) {
        if !self.in_alt_screen {
            return;
        }
        if let Some(grid) = self.alt_grid.take() {
            self.grid = grid;
        }
        if let Some(cursor) = self.alt_cursor.take() {
            self.cursor = cursor;
        }
        self.in_alt_screen = false;
        self.dirty = true;
    }

    // ── Cursor movement ──────────────────────────────────────────────────

    fn clamp_cursor(&mut self) {
        if self.cursor.x >= self.width {
            self.cursor.x = self.width.saturating_sub(1);
        }
        if self.cursor.y >= self.height {
            self.cursor.y = self.height.saturating_sub(1);
        }
    }

    pub fn move_cursor_to(&mut self, x: u16, y: u16) {
        self.pending_wrap = false;
        self.cursor.x = x.min(self.width.saturating_sub(1));
        self.cursor.y = y.min(self.height.saturating_sub(1));
    }

    pub fn move_cursor_up(&mut self, n: u16) {
        self.pending_wrap = false;
        self.cursor.y = self.cursor.y.saturating_sub(n);
    }

    pub fn move_cursor_down(&mut self, n: u16) {
        self.pending_wrap = false;
        self.cursor.y = (self.cursor.y + n).min(self.height.saturating_sub(1));
    }

    pub fn move_cursor_forward(&mut self, n: u16) {
        self.pending_wrap = false;
        self.cursor.x = (self.cursor.x + n).min(self.width.saturating_sub(1));
    }

    pub fn move_cursor_backward(&mut self, n: u16) {
        self.pending_wrap = false;
        self.cursor.x = self.cursor.x.saturating_sub(n);
    }

    pub fn carriage_return(&mut self) {
        self.pending_wrap = false;
        self.cursor.x = 0;
    }

    pub fn save_cursor(&mut self) {
        self.saved_cursor = Some(SavedCursor {
            x: self.cursor.x,
            y: self.cursor.y,
            pen: self.pen.clone(),
        });
    }

    pub fn restore_cursor(&mut self) {
        if let Some(saved) = self.saved_cursor.take() {
            self.cursor.x = saved.x;
            self.cursor.y = saved.y;
            self.pen = saved.pen;
            self.clamp_cursor();
        }
    }

    // ── Scrolling ────────────────────────────────────────────────────────

    fn scroll_up(&mut self) {
        let top = self.scroll_top as usize;
        let bottom = self.scroll_bottom as usize;
        let w = self.width as usize;

        // Push top line to scrollback (only if scrolling full screen, not alt screen)
        if !self.in_alt_screen && top == 0 {
            let start = 0;
            let end = w;
            let line = Line {
                cells: self.grid[start..end].to_vec(),
                soft_wrapped: false,
            };
            self.scrollback.push_back(line);
            if self.scrollback.len() > self.max_scrollback {
                self.scrollback.pop_front();
            }
        }

        // Shift rows up within scroll region
        for row in top..bottom {
            let src_start = (row + 1) * w;
            let dst_start = row * w;
            for col in 0..w {
                self.grid[dst_start + col] = self.grid[src_start + col].clone();
            }
        }

        // Clear bottom row of scroll region
        let clear_start = bottom * w;
        for col in 0..w {
            self.grid[clear_start + col] = Cell::default();
        }

        self.dirty = true;
    }

    fn scroll_down(&mut self) {
        let top = self.scroll_top as usize;
        let bottom = self.scroll_bottom as usize;
        let w = self.width as usize;

        // Shift rows down within scroll region
        for row in (top + 1..=bottom).rev() {
            let src_start = (row - 1) * w;
            let dst_start = row * w;
            for col in 0..w {
                self.grid[dst_start + col] = self.grid[src_start + col].clone();
            }
        }

        // Clear top row of scroll region
        let clear_start = top * w;
        for col in 0..w {
            self.grid[clear_start + col] = Cell::default();
        }

        self.dirty = true;
    }

    pub fn linefeed(&mut self) {
        if self.cursor.y == self.scroll_bottom {
            self.scroll_up();
        } else if self.cursor.y < self.height - 1 {
            self.cursor.y += 1;
        }
        self.pending_wrap = false;
    }

    pub fn reverse_index(&mut self) {
        if self.cursor.y == self.scroll_top {
            self.scroll_down();
        } else if self.cursor.y > 0 {
            self.cursor.y -= 1;
        }
        self.pending_wrap = false;
    }

    // ── Character output ─────────────────────────────────────────────────

    /// Translate a character through the DEC Special Graphics charset.
    fn translate_charset(&self, ch: char) -> char {
        if !self.charset_g0_special {
            return ch;
        }
        match ch {
            'j' => '┘',
            'k' => '┐',
            'l' => '┌',
            'm' => '└',
            'n' => '┼',
            'q' => '─',
            't' => '├',
            'u' => '┤',
            'v' => '┴',
            'w' => '┬',
            'x' => '│',
            'a' => '▒',
            'f' => '°',
            'g' => '±',
            'h' => '░', // NL (board of squares approximation)
            'i' => '┘', // VT (lantern approximation)
            'o' => '⎺', // scan line 1
            'p' => '⎻', // scan line 3
            'r' => '⎼', // scan line 7
            's' => '⎽', // scan line 9
            '`' => '◆',
            '~' => '·',
            ',' => '←',
            '+' => '→',
            '.' => '↓',
            '-' => '↑',
            '0' => '█',
            'y' => '≤',
            'z' => '≥',
            '{' => 'π',
            '|' => '≠',
            '}' => '£',
            _ => ch,
        }
    }

    pub fn put_char(&mut self, ch: char) {
        let ch = self.translate_charset(ch);

        if self.pending_wrap && self.auto_wrap {
            self.cursor.x = 0;
            self.linefeed();
            self.pending_wrap = false;
        }

        if self.cursor.x < self.width && self.cursor.y < self.height {
            let fg = self.pen.fg;
            let bg = self.pen.bg;
            let attrs = self.pen.attrs;
            let i = self.idx(self.cursor.x, self.cursor.y);
            self.grid[i].ch = ch;
            self.grid[i].fg = fg;
            self.grid[i].bg = bg;
            self.grid[i].attrs = attrs;
            self.dirty = true;
        }

        if self.cursor.x >= self.width.saturating_sub(1) {
            if self.auto_wrap {
                self.pending_wrap = true;
            }
        } else {
            self.cursor.x += 1;
        }
    }

    pub fn tab(&mut self) {
        self.pending_wrap = false;
        let start = (self.cursor.x + 1) as usize;
        for i in start..(self.width as usize) {
            if self.tab_stops.get(i).copied().unwrap_or(false) {
                self.cursor.x = i as u16;
                return;
            }
        }
        self.cursor.x = self.width.saturating_sub(1);
    }

    pub fn backspace(&mut self) {
        self.pending_wrap = false;
        if self.cursor.x > 0 {
            self.cursor.x -= 1;
        }
    }

    // ── Erase operations ─────────────────────────────────────────────────

    pub fn erase_in_display(&mut self, mode: u16) {
        match mode {
            0 => {
                // Erase from cursor to end of display
                let start = self.idx(self.cursor.x, self.cursor.y);
                for i in start..self.grid.len() {
                    self.grid[i] = Cell::default();
                }
            }
            1 => {
                // Erase from start of display to cursor
                let end = self.idx(self.cursor.x, self.cursor.y) + 1;
                for i in 0..end.min(self.grid.len()) {
                    self.grid[i] = Cell::default();
                }
            }
            2 | 3 => {
                // Erase entire display (3 also clears scrollback)
                for cell in self.grid.iter_mut() {
                    *cell = Cell::default();
                }
                if mode == 3 {
                    self.scrollback.clear();
                }
            }
            _ => {}
        }
        self.dirty = true;
    }

    pub fn erase_in_line(&mut self, mode: u16) {
        let y = self.cursor.y;
        let w = self.width;
        match mode {
            0 => {
                // Erase from cursor to end of line
                for x in self.cursor.x..w {
                    *self.cell_mut(x, y) = Cell::default();
                }
            }
            1 => {
                // Erase from start of line to cursor
                for x in 0..=self.cursor.x.min(w - 1) {
                    *self.cell_mut(x, y) = Cell::default();
                }
            }
            2 => {
                // Erase entire line
                for x in 0..w {
                    *self.cell_mut(x, y) = Cell::default();
                }
            }
            _ => {}
        }
        self.dirty = true;
    }

    // ── Insert/delete lines and characters ───────────────────────────────

    pub fn insert_lines(&mut self, n: u16) {
        for _ in 0..n {
            self.scroll_down();
        }
    }

    pub fn delete_lines(&mut self, n: u16) {
        let saved_y = self.cursor.y;
        // Temporarily set scroll_top to cursor position for the scroll
        let old_top = self.scroll_top;
        self.scroll_top = saved_y;
        for _ in 0..n {
            self.scroll_up();
        }
        self.scroll_top = old_top;
    }

    pub fn delete_chars(&mut self, n: u16) {
        let y = self.cursor.y;
        let x = self.cursor.x as usize;
        let w = self.width as usize;
        let n = n as usize;

        for col in x..w {
            let src = col + n;
            if src < w {
                let i_dst = self.idx(col as u16, y);
                let i_src = self.idx(src as u16, y);
                self.grid[i_dst] = self.grid[i_src].clone();
            } else {
                let i = self.idx(col as u16, y);
                self.grid[i] = Cell::default();
            }
        }
        self.dirty = true;
    }

    pub fn insert_blank_chars(&mut self, n: u16) {
        let y = self.cursor.y;
        let x = self.cursor.x as usize;
        let w = self.width as usize;
        let n = n as usize;

        // Shift existing chars right
        for col in (x..w).rev() {
            let dst = col + n;
            if dst < w {
                let i_src = self.idx(col as u16, y);
                let i_dst = self.idx(dst as u16, y);
                self.grid[i_dst] = self.grid[i_src].clone();
            }
        }
        // Fill blanks
        for col in x..(x + n).min(w) {
            let i = self.idx(col as u16, y);
            self.grid[i] = Cell::default();
        }
        self.dirty = true;
    }

    // ── Scroll region ────────────────────────────────────────────────────

    pub fn set_scroll_region(&mut self, top: u16, bottom: u16) {
        let top = top.min(self.height.saturating_sub(1));
        let bottom = bottom.min(self.height.saturating_sub(1));
        if top < bottom {
            self.scroll_top = top;
            self.scroll_bottom = bottom;
            // Reset cursor to home (per spec, cursor goes to origin after DECSTBM)
            self.cursor.x = 0;
            self.cursor.y = if self.origin_mode { self.scroll_top } else { 0 };
        }
    }

    // ── SGR (Select Graphic Rendition) ───────────────────────────────────

    pub fn set_sgr(&mut self, params: &[u16]) {
        let mut i = 0;
        while i < params.len() {
            match params[i] {
                0 => self.pen = Pen::default(),
                1 => self.pen.attrs.bold = true,
                2 => self.pen.attrs.dim = true,
                3 => self.pen.attrs.italic = true,
                4 => self.pen.attrs.underline = true,
                5 => self.pen.attrs.blink = true,
                7 => self.pen.attrs.inverse = true,
                8 => self.pen.attrs.hidden = true,
                9 => self.pen.attrs.strikethrough = true,
                22 => { self.pen.attrs.bold = false; self.pen.attrs.dim = false; }
                23 => self.pen.attrs.italic = false,
                24 => self.pen.attrs.underline = false,
                25 => self.pen.attrs.blink = false,
                27 => self.pen.attrs.inverse = false,
                28 => self.pen.attrs.hidden = false,
                29 => self.pen.attrs.strikethrough = false,
                // Foreground standard colors
                30..=37 => self.pen.fg = Color::Indexed((params[i] - 30) as u8),
                38 => {
                    // Extended foreground
                    if i + 1 < params.len() {
                        match params[i + 1] {
                            5 if i + 2 < params.len() => {
                                self.pen.fg = Color::Indexed(params[i + 2] as u8);
                                i += 2;
                            }
                            2 if i + 4 < params.len() => {
                                self.pen.fg = Color::Rgb(
                                    params[i + 2] as u8,
                                    params[i + 3] as u8,
                                    params[i + 4] as u8,
                                );
                                i += 4;
                            }
                            _ => { i += 1; }
                        }
                    }
                }
                39 => self.pen.fg = Color::Default,
                // Background standard colors
                40..=47 => self.pen.bg = Color::Indexed((params[i] - 40) as u8),
                48 => {
                    // Extended background
                    if i + 1 < params.len() {
                        match params[i + 1] {
                            5 if i + 2 < params.len() => {
                                self.pen.bg = Color::Indexed(params[i + 2] as u8);
                                i += 2;
                            }
                            2 if i + 4 < params.len() => {
                                self.pen.bg = Color::Rgb(
                                    params[i + 2] as u8,
                                    params[i + 3] as u8,
                                    params[i + 4] as u8,
                                );
                                i += 4;
                            }
                            _ => { i += 1; }
                        }
                    }
                }
                49 => self.pen.bg = Color::Default,
                // Bright foreground
                90..=97 => self.pen.fg = Color::Indexed((params[i] - 90 + 8) as u8),
                // Bright background
                100..=107 => self.pen.bg = Color::Indexed((params[i] - 100 + 8) as u8),
                _ => {}
            }
            i += 1;
        }
    }

    // ── Resize ───────────────────────────────────────────────────────────

    pub fn resize(&mut self, new_width: u16, new_height: u16) {
        let new_size = (new_width as usize) * (new_height as usize);
        let mut new_grid = vec![Cell::default(); new_size];

        // Copy existing content
        let copy_w = (self.width.min(new_width)) as usize;
        let copy_h = (self.height.min(new_height)) as usize;
        for y in 0..copy_h {
            for x in 0..copy_w {
                let old_i = y * (self.width as usize) + x;
                let new_i = y * (new_width as usize) + x;
                new_grid[new_i] = self.grid[old_i].clone();
            }
        }

        self.grid = new_grid;
        self.width = new_width;
        self.height = new_height;
        self.scroll_top = 0;
        self.scroll_bottom = new_height.saturating_sub(1);

        // Rebuild tab stops
        self.tab_stops = vec![false; new_width as usize];
        for i in (0..new_width as usize).step_by(8) {
            self.tab_stops[i] = true;
        }

        self.clamp_cursor();
        self.dirty = true;

        // Also resize alt grid if it exists
        if let Some(ref mut alt) = self.alt_grid {
            let mut new_alt = vec![Cell::default(); new_size];
            let old_w = alt.len() / self.height.max(1) as usize; // approximate
            let cw = old_w.min(new_width as usize);
            let ch = (alt.len() / old_w.max(1)).min(new_height as usize);
            for y in 0..ch {
                for x in 0..cw {
                    let old_i = y * old_w + x;
                    let new_i = y * (new_width as usize) + x;
                    if old_i < alt.len() {
                        new_alt[new_i] = alt[old_i].clone();
                    }
                }
            }
            *alt = new_alt;
        }
    }

    // ── Mode setting helpers ─────────────────────────────────────────────

    pub fn set_origin_mode(&mut self, enabled: bool) {
        self.origin_mode = enabled;
        self.cursor.x = 0;
        self.cursor.y = if enabled { self.scroll_top } else { 0 };
    }

    pub fn set_auto_wrap(&mut self, enabled: bool) {
        self.auto_wrap = enabled;
    }
}
