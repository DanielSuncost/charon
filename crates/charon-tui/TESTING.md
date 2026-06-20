# Testing the Charon daemon stack (`charond` + TUI)

Two layers: **automated** (Rust tests — run these every change) and **manual TUI
verification** (the interactive grid/sidebar/splits that can't be checked headless).

All tests and manual runs use an **isolated state dir** via `$CHARON_DIR` (and/or
`$CHARON_SOCK`) so they never touch your real `~/.charon`.

---

## 1. Automated tests

Run from the crate (cargo needs the manifest):

```bash
cargo test --manifest-path crates/charon-tui/Cargo.toml
# or:  cd crates/charon-tui && cargo test
```

Expected: **all green** (currently 33 tests). Build must be warning-clean except the
3 known pre-existing warnings in the `charon` bin (`read_from_clipboard`,
`context_menu_item_count`, `point_at_mouse`).

### Unit tests (pure logic — fast, deterministic)
| Module | Covers |
|---|---|
| `src/layout.rs` | split-tree geometry: tiling, gaps, nesting, remove/collapse, hit-test, resize |
| `src/config.rs` | `config.toml` parsing, theme selection, `[themes.*]` overlay, hex parsing |
| `src/detect.rs` | state classifier (working/idle/blocked) |

### Integration tests (spawn the real `charond` binary, drive it over the socket)
| Test | Proves |
|---|---|
| `tests/daemon_client.rs` | spawn + input/output round-trip through `DaemonClient` |
| `tests/daemon_persist.rs` | scrollback survives a hard `kill -9` → restore → replay → respawn |
| `tests/daemon_detect.rs` | `idle → blocked → idle` status broadcast over the protocol |
| `tests/daemon_workspace.rs` | workspace/tab spawn, defaults, `move`, restart persistence |
| `tests/daemon_handoff.rs` | graceful `shutdown` → socket released + process exits → restore |
| `tests/daemon_tmux.rs` | adopt a real tmux session; re-attaches (not exited) after restart. **Skips if tmux is absent.** |
| `tests/daemon_ephemeral.rs` | ephemeral session is reaped after its client disconnects (grace); persistent one survives |
| `tests/daemon_persist_toggle.rs` | `set_persist` pins an ephemeral session → it survives disconnect and reports `ephemeral=false` |

Notes:
- Integration tests set `CHARON_DIR` to a unique temp dir and clean up after.
- They spawn `CARGO_BIN_EXE_charond`, so `cargo test` builds the daemon first.
- Run a single one: `cargo test --manifest-path crates/charon-tui/Cargo.toml --test daemon_handoff`.

---

## 2. Manual TUI verification

The F3 grid, sidebar grouping, status colors, and (pending) manual splits are
**interactive** — they aren't exercised by the automated tests. Verify them by hand
after any change that touches `main.rs`/`app.rs`/`render.rs`/`grid.rs`/`layout.rs`.

### Setup (isolated, won't touch your real sessions)

```bash
cd crates/charon-tui && cargo build
export CHARON_DIR=/tmp/charon-manual            # isolated state
rm -rf "$CHARON_DIR" && mkdir -p "$CHARON_DIR"
BIN=crates/charon-tui/target/debug              # from repo root, adjust as needed
```

### A. Daemon lifecycle (no TUI needed)
```bash
"$BIN/charond" &                                # start daemon
"$BIN/charon" --daemon-list                     # → "No daemon sessions."
# spawn two sessions in different workspaces via the protocol:
python3 - <<'PY'
import socket,os,time
s=socket.socket(socket.AF_UNIX); s.connect(os.path.join(os.environ["CHARON_DIR"],"charond.sock"))
s.sendall(b'{"type":"hello","proto":1,"client":"t"}\n')
s.sendall(b'{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"workspace":"alpha"}\n')
s.sendall(b'{"type":"spawn","kind":"local","cmd":["bash","--norc","-i"],"workspace":"beta"}\n')
time.sleep(0.4); s.close()
PY
"$BIN/charon" --daemon-list                     # → two sessions, workspaces alpha/beta
```

### B. F3 grid in the TUI  *(eyeball these)*
1. `"$BIN/charon"` → press **F3** (Sessions).
   - [ ] The two daemon sessions appear as panes.
   - [ ] Pane titles show `alpha/…` and `beta/…` (workspace prefix).
   - [ ] Borders are **state-colored** (idle = slate; run a `sleep 1` inside a focused
         pane via Enter→type to see it flip to working/gold).
2. Sidebar (Tab to the **agents** pane):
   - [ ] Daemon sessions appear under `◈ alpha` / `◈ beta` headers.
   - [ ] Enter on a header/session toggles its pane visibility in the grid.
3. Interact: focus a pane, **Enter** (terminal mode), type `echo hi`, **Ctrl+]** to exit.
   - [ ] Input/output works against the daemon-backed pane.
4. Manual splits (Grid section, focus a pane):
   - [ ] `|` splits it side-by-side with a **new shell**; `-` splits stacked.
   - [ ] `<` / `>` resize the focused split; `=` resets to auto-tile.
   - [ ] Closing a pane (`w`) collapses the split; remaining panes re-fill.
   - [ ] `p` pins the focused daemon pane → 📌 appears; quit & relaunch → it persisted.

### C. Detach / reattach
- [ ] Quit the TUI (the daemon keeps running). Relaunch → F3 → panes repaint with prior
      scrollback (reattach + replay).

### D. Session restore across daemon restart
```bash
pkill -9 -f "$BIN/charond"                      # hard-kill the daemon
"$BIN/charond" &                                 # restart
"$BIN/charon" --daemon-list                      # local sessions show "exited" (respawnable)
```
- [ ] Local sessions restored as `exited` with history; a tmux-kind session (if any) shows live.

### E. Upgrade (graceful handoff)
```bash
"$BIN/charon" --daemon-upgrade                   # → "charond upgraded and restarted."
"$BIN/charon" --daemon-list                      # sessions still listed
```

### F. tmux adopt
```bash
tmux new-session -d -s demo
# spawn {kind:"tmux", target:"demo"} via the protocol (see A), then F3 → the tmux pane appears.
tmux kill-session -t demo
```

### G. Config / theme
```bash
cat > "$CHARON_DIR/config.toml" <<'TOML'
[ui]
theme = "midnight"
persist_sessions = false     # default: TUI sessions are ephemeral (end on close)
TOML
"$BIN/charon"                                    # header + daemon borders use the midnight palette
```
- [ ] **Ephemeral default:** with `persist_sessions=false`, a session you `|`-split
      in the TUI disappears after you quit (Claude-Code style). Set `true` → it
      survives and reattaches on next launch.

### Teardown
```bash
pkill -f "$BIN/charond"; rm -rf "$CHARON_DIR"
```

---

## 3. Pre-commit checklist
- [ ] `cargo test --manifest-path crates/charon-tui/Cargo.toml` → all green.
- [ ] `cargo build --manifest-path crates/charon-tui/Cargo.toml` → only the 3 known warnings.
- [ ] If `main.rs`/grid/sidebar/layout changed → run the relevant **§2 manual checks**.
- [ ] `grep -rniE "herdr|zellij" crates/charon-tui/src crates/charon-tui/tests docs/` → empty
      (no external-competitor references; Styx is allowed).
- [ ] Stage only your files (another agent may have WIP); never bundle unrelated changes.

---

## 4. Interactive surfaces NOT covered by automated tests (verify by hand)
- F3 grid rendering, focus/navigation, status border colors, workspace title prefixes.
- Sidebar workspace grouping + visibility toggles.
- Manual split layout: the `layout.rs` engine (incl. `reconcile`/`linear`) is unit-tested;
  the keybinding + render integration (`|`/`-`/`=`/`<`/`>`) is wired but **interactive** —
  verify via §2-B step 4. Mouse drag-to-resize on split borders is not yet implemented.
- Mouse interactions (click-focus, selection, scroll).
