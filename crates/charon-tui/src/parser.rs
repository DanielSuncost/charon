/// AnsiParser — wraps the `vte` crate to dispatch escape sequences
/// to TerminalState updates.

use crate::terminal::TerminalState;
use vte::Params;

/// Wrapper that owns both the VTE parser and a mutable reference path to TerminalState.
/// We implement vte::Perform on a helper struct that holds &mut TerminalState.
pub struct AnsiParser {
    vte_parser: vte::Parser,
}

impl AnsiParser {
    pub fn new() -> Self {
        AnsiParser {
            vte_parser: vte::Parser::new(),
        }
    }

    /// Feed raw bytes from the PTY into the VTE parser, which dispatches
    /// events to update the TerminalState.
    pub fn process(&mut self, bytes: &[u8], terminal: &mut TerminalState) {
        let mut performer = Performer { terminal };
        self.vte_parser.advance(&mut performer, bytes);
    }
}

/// Performer implements vte::Perform and translates VTE events into
/// TerminalState mutations.
struct Performer<'a> {
    terminal: &'a mut TerminalState,
}

impl<'a> Performer<'a> {
    fn params_to_u16(params: &Params) -> Vec<u16> {
        params.iter().filter_map(|p| p.first().map(|&v| v)).collect()
    }
}

impl<'a> vte::Perform for Performer<'a> {
    /// A printable character
    fn print(&mut self, ch: char) {
        self.terminal.put_char(ch);
    }

    /// A C0 or C1 control character
    fn execute(&mut self, byte: u8) {
        match byte {
            // BEL
            0x07 => {} // bell — ignore for now
            // BS (backspace)
            0x08 => self.terminal.backspace(),
            // HT (horizontal tab)
            0x09 => self.terminal.tab(),
            // LF, VT, FF (line feed variants)
            0x0A | 0x0B | 0x0C => self.terminal.linefeed(),
            // CR (carriage return)
            0x0D => self.terminal.carriage_return(),
            // SO (shift out) — activate G1 charset (ignore, treat as no-op)
            0x0E => {}
            // SI (shift in) — activate G0 charset
            0x0F => {}
            _ => {}
        }
    }

    /// An escape sequence hook (DCS, etc.) — ignore for now
    fn hook(&mut self, _params: &Params, _intermediates: &[u8], _ignore: bool, _action: char) {}

    fn unhook(&mut self) {}

    fn put(&mut self, _byte: u8) {}

    /// An OSC (Operating System Command) string
    fn osc_dispatch(&mut self, _params: &[&[u8]], _bell_terminated: bool) {
        // OSC sequences: title changes, hyperlinks, etc. — ignore for Phase 1
    }

    /// A CSI (Control Sequence Introducer) sequence
    fn csi_dispatch(&mut self, params: &Params, intermediates: &[u8], _ignore: bool, action: char) {
        let p = Self::params_to_u16(params);
        let n = p.first().copied().unwrap_or(0);
        let m = if p.len() > 1 { p[1] } else { 0 };

        match (action, intermediates) {
            // ── Cursor movement ──────────────────────────────────────────

            // CUU — Cursor Up
            ('A', []) => self.terminal.move_cursor_up(n.max(1)),
            // CUD — Cursor Down
            ('B', []) => self.terminal.move_cursor_down(n.max(1)),
            // CUF — Cursor Forward
            ('C', []) => self.terminal.move_cursor_forward(n.max(1)),
            // CUB — Cursor Backward
            ('D', []) => self.terminal.move_cursor_backward(n.max(1)),
            // CNL — Cursor Next Line
            ('E', []) => {
                self.terminal.move_cursor_down(n.max(1));
                self.terminal.carriage_return();
            }
            // CPL — Cursor Previous Line
            ('F', []) => {
                self.terminal.move_cursor_up(n.max(1));
                self.terminal.carriage_return();
            }
            // CHA — Cursor Horizontal Absolute
            ('G', []) => {
                self.terminal.move_cursor_to(n.max(1).saturating_sub(1), self.terminal.cursor.y);
            }
            // CUP — Cursor Position (row;col, 1-based)
            ('H', []) | ('f', []) => {
                let row = n.max(1).saturating_sub(1);
                let col = m.max(1).saturating_sub(1);
                self.terminal.move_cursor_to(col, row);
            }

            // ── Erase ────────────────────────────────────────────────────

            // ED — Erase in Display
            ('J', []) => self.terminal.erase_in_display(n),
            // EL — Erase in Line
            ('K', []) => self.terminal.erase_in_line(n),

            // ── Insert/Delete ────────────────────────────────────────────

            // IL — Insert Lines
            ('L', []) => self.terminal.insert_lines(n.max(1)),
            // DL — Delete Lines
            ('M', []) => self.terminal.delete_lines(n.max(1)),
            // DCH — Delete Characters
            ('P', []) => self.terminal.delete_chars(n.max(1)),
            // ICH — Insert Blank Characters
            ('@', []) => self.terminal.insert_blank_chars(n.max(1)),
            // ECH — Erase Characters (fill with blanks)
            ('X', []) => {
                let count = n.max(1);
                let y = self.terminal.cursor.y;
                let x = self.terminal.cursor.x;
                for i in 0..count {
                    let cx = x + i;
                    if cx < self.terminal.width {
                        let idx = self.terminal.idx(cx, y);
                        self.terminal.grid[idx] = crate::terminal::Cell::default();
                    }
                }
                self.terminal.dirty = true;
            }

            // ── Scroll ───────────────────────────────────────────────────

            // SU — Scroll Up
            ('S', []) => {
                for _ in 0..n.max(1) {
                    self.terminal.linefeed();
                }
            }
            // SD — Scroll Down
            ('T', []) => {
                for _ in 0..n.max(1) {
                    self.terminal.reverse_index();
                }
            }

            // ── SGR — Select Graphic Rendition ───────────────────────────

            ('m', []) => {
                if p.is_empty() {
                    self.terminal.set_sgr(&[0]);
                } else {
                    self.terminal.set_sgr(&p);
                }
            }

            // ── Scroll Region ────────────────────────────────────────────

            // DECSTBM — Set Top and Bottom Margins
            ('r', []) => {
                let top = n.max(1).saturating_sub(1);
                let bottom = if m == 0 {
                    self.terminal.height.saturating_sub(1)
                } else {
                    m.saturating_sub(1)
                };
                self.terminal.set_scroll_region(top, bottom);
            }

            // ── Cursor save/restore ──────────────────────────────────────

            ('s', []) => self.terminal.save_cursor(),
            ('u', []) => self.terminal.restore_cursor(),

            // ── VPA — Vertical Position Absolute ─────────────────────────

            ('d', []) => {
                let row = n.max(1).saturating_sub(1);
                self.terminal.move_cursor_to(self.terminal.cursor.x, row);
            }

            // ── DEC Private Modes ────────────────────────────────────────

            ('h', [b'?']) => self.handle_dec_set(n, true),
            ('l', [b'?']) => self.handle_dec_set(n, false),

            // ── Standard mode set/reset ──────────────────────────────────

            ('h', []) => {} // SM — ignore for now
            ('l', []) => {} // RM — ignore for now

            // ── Device Status Report ─────────────────────────────────────

            ('n', []) => {} // DSR — we can't respond in Phase 1, ignore

            // ── Tab stops ────────────────────────────────────────────────

            ('g', []) => {} // TBC — ignore for now

            _ => {
                // Unknown CSI sequence — ignore
            }
        }
    }

    /// An ESC sequence (not CSI, not OSC)
    fn esc_dispatch(&mut self, intermediates: &[u8], _ignore: bool, byte: u8) {
        match (byte, intermediates) {
            // RI — Reverse Index
            (b'M', []) => self.terminal.reverse_index(),
            // DECSC — Save Cursor
            (b'7', []) => self.terminal.save_cursor(),
            // DECRC — Restore Cursor
            (b'8', []) => self.terminal.restore_cursor(),
            // RIS — Full Reset
            (b'c', []) => {
                let w = self.terminal.width;
                let h = self.terminal.height;
                *self.terminal = TerminalState::new(w, h);
            }
            // IND — Index (move cursor down, scroll if at bottom)
            (b'D', []) => self.terminal.linefeed(),
            // NEL — Next Line
            (b'E', []) => {
                self.terminal.carriage_return();
                self.terminal.linefeed();
            }
            // DECID — identify terminal (ignore)
            (b'Z', []) => {}
            // Charset designations — G0 set
            (b'0', [b'(']) => self.terminal.charset_g0_special = true,   // DEC Special Graphics
            (b'B', [b'(']) => self.terminal.charset_g0_special = false,  // US ASCII
            (b'A', [b'(']) => self.terminal.charset_g0_special = false,  // UK (treat as ASCII)
            // G1 set — ignore for now
            (b'B', [b')']) | (b'0', [b')']) | (b'A', [b')']) => {}
            _ => {}
        }
    }
}

impl<'a> Performer<'a> {
    fn handle_dec_set(&mut self, mode: u16, enable: bool) {
        match mode {
            // DECCKM — Cursor Keys Mode (application vs normal)
            1 => {} // TODO: track for keystroke encoding
            // DECOM — Origin Mode
            6 => self.terminal.set_origin_mode(enable),
            // DECAWM — Auto Wrap Mode
            7 => self.terminal.set_auto_wrap(enable),
            // Show/Hide Cursor
            25 => self.terminal.cursor.visible = enable,
            // Alt Screen Buffer variants
            47 | 1047 => {
                if enable {
                    self.terminal.enter_alt_screen();
                } else {
                    self.terminal.exit_alt_screen();
                }
            }
            // Alt Screen + Save/Restore Cursor (most common, used by vim/htop)
            1049 => {
                if enable {
                    self.terminal.save_cursor();
                    self.terminal.enter_alt_screen();
                    self.terminal.erase_in_display(2);
                } else {
                    self.terminal.exit_alt_screen();
                    self.terminal.restore_cursor();
                }
            }
            // Bracketed Paste Mode — ignore for now
            2004 => {}
            // Mouse tracking modes — ignore
            1000 | 1002 | 1003 | 1006 => {}
            // Focus events — ignore
            1004 => {}
            _ => {}
        }
    }
}


