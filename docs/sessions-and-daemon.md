# Charon sessions & the daemon (`charond`)

Charon keeps your terminal/agent **sessions in a persistent local daemon**, so they
live independently of any window. Close the TUI, upgrade it, or open a second view —
your sessions are still there. The F3 **Sessions** grid is a full multiplexer over them.

This is local-only: everything runs on your machine under `~/.charon/`. Nothing is sent
anywhere.

---

## Quick start

```bash
charon                       # the TUI; press F3 for the Sessions grid
charon --daemon-spawn        # start a persistent shell session and open it
charon --daemon-list         # list daemon sessions
```

The daemon (`charond`) starts automatically the first time you need it. It keeps
running after you close the TUI.

---

## What you get

- **Detach / reattach.** Close the TUI without killing your work; reopen and your
  sessions repaint with their scrollback.
- **Survives restarts & upgrades.** `charon --daemon-upgrade` swaps the binary without
  losing sessions; persistent sessions even survive a daemon crash.
- **A real multiplexer (F3).** Tile, **split**, resize, and group panes — each pane is a
  live session.
- **Every session type.** Local shells, adopted **tmux** sessions, boat-wrapped agents,
  remote/fleet agents — all in one grid.
- **State at a glance.** Pane borders are colored by live state (working / blocked /
  idle), detected by the daemon.
- **Workspaces.** Sessions group by workspace in the grid and sidebar.
- **One runtime, many UIs.** The same daemon backs the TUI today and the Styx desktop
  app next — a session started in one shows up in the other.

---

## Session lifetime (important)

By default, TUI sessions are **ephemeral** — like Claude Code, they end when you close
them. Opt into persistence when you want sessions to outlive the window.

```toml
# ~/.charon/config.toml
[ui]
persist_sessions = false   # default: ephemeral (sessions end on close)
                           # true: sessions persist + reattach on next launch
```

- **Ephemeral** sessions are in-memory only and are cleaned up shortly after their last
  window detaches (a few seconds' grace covers quick reconnects).
- **Persistent** sessions write their scrollback to `~/.charon/sessions/<id>/` and come
  back when you relaunch; local ones return as `exited` and can be respawned, while
  tmux/boat/remote ones re-attach live.
- **Pin individual panes:** regardless of the global default, press `p` on a pane in the
  F3 grid to pin it persistent (📌) or unpin it back to ephemeral.

---

## The F3 Sessions grid

| Key | Action |
|---|---|
| `F3` | open the Sessions view |
| `Tab` / `Shift+Tab` | move between the agents sidebar, projects, and the grid |
| `Enter` (on a grid pane) | interact with the focused pane (terminal mode) |
| `Ctrl+]` | leave terminal mode |
| arrows | move focus between panes |
| `\|` | split the focused pane **side by side** (opens a new shell) |
| `-` | split the focused pane **stacked** |
| `<` / `>` | resize the focused split |
| `=` | reset the layout to auto-tile |
| `p` | pin/unpin the focused daemon pane (persist ↔ ephemeral); 📌 marks pinned |
| `w` | close the focused pane |
| `F6` | toggle mouse handling (app vs terminal) |

Pane borders are colored by state; titles are prefixed with their workspace.

---

## CLI

| Command | What it does |
|---|---|
| `charon` | launch the TUI |
| `charon --daemon-spawn [cmd]` | start a session (default shell, or `cmd`) and attach |
| `charon --daemon-attach <id>` | attach to an existing session |
| `charon --daemon-respawn <id>` | re-run an exited session, keeping its scrollback |
| `charon --daemon-list` | list daemon sessions |
| `charon --daemon-upgrade` | gracefully restart the daemon (binary upgrade) |

`charond` can also be run directly. State dir defaults to `~/.charon` (override with
`$CHARON_DIR`); the control socket is `~/.charon/charond.sock` (`$CHARON_SOCK`).

---

## Config

```toml
# ~/.charon/config.toml
[ui]
theme = "charon-dark"        # built-ins: charon-dark, midnight, mono
persist_sessions = false     # session lifetime default (see above)

[themes.mine]                # define or override a theme
header = "#a78bfa"
status_working = "#d4af37"
status_blocked = "#fb923c"
```

---

## Where things live

```
~/.charon/
  charond.sock          control socket
  charond.pid           single-instance guard
  config.toml           your config (themes, lifetime)
  sessions/<id>/        persistent sessions' meta.json + scrollback.log
  fleet.json            remote/SSH fleet config (existing)
```

---

## Known limitations (this release)

- The main **chat/agent view (F1)** is not yet a daemon session — only the F3 grid and
  spawned/adopted sessions are daemon-backed.
- Manual splits are keyboard-driven; **mouse drag-to-resize** on split borders isn't in yet.
- Styx integration is **planned** (see the Styx repo's `docs/charond-integration-plan.md`),
  not yet wired.
- A few daemon refinements are future work: a raw-SSH session kind matching Styx's model,
  goal-aware labels, `seq`-gap incremental reconnect, and zero-downtime fd-passing handoff.

For testing/verification, see `crates/charon-tui/TESTING.md`.
