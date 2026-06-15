# Charon Session Daemon (`charond`)

> **Goal:** an always-on, local daemon that owns terminal/agent sessions so they
> survive any client exiting — and survive the daemon itself restarting. One
> runtime, many front-ends: the terminal TUI today, GUI front-ends later, all
> thin clients over a single control socket.
>
> **Status:** Phases 1–3 implemented (daemon + client + TUI wiring + persistence).
> Phases 4–8 pending.
>
> **Implemented so far:**
> - `charond` daemon binary — owns sessions/PTYs, fans output out to many clients,
>   single-instance guard + pidfile. (`src/daemon.rs`, `src/bin/charond.rs`)
> - Control protocol, versioned. (`src/protocol.rs`)
> - `DaemonClient` — a `ByteStream` so the TUI attaches with no render-code changes.
>   (`src/daemon_client.rs`)
> - TUI wiring: `BackendType::DaemonPane`, `SessionCell::attach_daemon`, auto-start
>   of `charond`, and CLI: `charon --daemon-spawn [cmd]`, `--daemon-attach <id>`,
>   `--daemon-respawn <id>`, `--daemon-list`. (`src/session.rs`, `src/main.rs`)
> - **Persistence (Phase 3):** scrollback + `meta.json` persisted under
>   `~/.charon/sessions/<id>/`; on daemon restart, sessions are restored as
>   `exited` with history intact and can be respawned (re-run their command in
>   their original cwd). `$CHARON_DIR` overrides the state dir.
> - Tests: `tests/daemon_client.rs` (round-trip), `tests/daemon_persist.rs`
>   (scrollback survives a hard daemon kill → restore → replay → respawn).
> - **Detach/reattach works:** a session survives the client exiting; reattaching
>   replays its scrollback.
> - **Session restore works:** a session survives the *daemon* restarting.
>
> **Decision (locked):** always-on daemon, thin clients. `charond` is the single
> source of truth for sessions, PTYs, and agent runtime. Front-ends attach/detach.

---

## 0. Why a daemon

Today the TUI is monolithic: its session server (`NativeSessionServer`,
`crates/charon-tui/src/native_session.rs`) lives *in-process* and is torn down on
exit (`impl Drop`, `native_session.rs:171`). So when the TUI closes, the sessions
it owns die with it.

Making sessions outlive the client requires the session owner to be a separate,
long-lived process. That single change — extracting `charond` — unlocks
detach/reattach, on-disk session restore, live handoff, and a general control
socket that any front-end (terminal or GUI) can speak.

### One runtime, many front-ends

```
                         ┌────────────────────────────────────────────┐
                         │                  charond                    │
                         │  owns: SessionCells (PTY/tmux/boat/charon)  │
                         │        agent runtime + memory + orchestr.   │
                         │        scrollback persistence, detection    │
                         │  serves: control socket (JSON-lines)        │
                         └───────────────┬───────────────┬────────────┘
              attach/detach              │               │
        ┌───────────────────────────────┘               └───────────────┐
        ▼                                                                ▼
  ┌───────────┐                                                  ┌──────────────┐
  │ charon-tui │  terminal front-end (thin)                      │ GUI front-end │
  │  (thin)    │                                                  │   (thin)      │
  └───────────┘                                                  └──────────────┘
```

---

## 1. Target architecture

### 1.1 Process model

- **`charond`** — long-lived daemon, one per machine (per `$HOME`). Auto-spawned by
  the first client if not already running; survives all clients disconnecting. Owns:
  - All `SessionCell`s (local PTYs, tmux attachments, boat/remote, native charon agents).
  - The Python agent runtime / `apps/core-daemon` lifecycle.
  - Scrollback buffers + on-disk persistence.
  - The agent-state detector (process scan + output heuristics + native status).
  - Memory, orchestration, fleet/Harbor dispatch — hosted in one always-on place
    instead of per-TUI.
- **Clients** (the TUI and future GUI front-ends) — own only *rendering* and
  *input*. They attach to the daemon, subscribe to session output streams, send
  keystrokes/resizes, and issue control commands. They hold no PTYs and no agent
  state. Closing a client never kills a session.

### 1.2 The seam that makes this cheap

The refactor is mostly a *relocation of ownership* because the abstraction already exists:

- `trait ByteStream` (`backend.rs:72`) abstracts every backend behind
  `read_available / write_bytes / is_eof / resize`.
- `SessionCell` (`session.rs:10`) = `TerminalState` + `AnsiParser` + `Box<dyn ByteStream>`.

Today the TUI constructs and polls `SessionCell`s directly. In the daemon model:

1. **`charond`** owns the real `SessionCell`s and polls them (the existing loop, moved).
2. A client-side backend, **`DaemonClient` (impl `ByteStream`)**, proxies over the
   control socket: `read_available()` drains output frames, `write_bytes()` sends
   `input`, `resize()` sends `resize`. The TUI's grid/render code is **unchanged** —
   it still sees a `Box<dyn ByteStream>`, just a remote one.

This is the same trick `BoatPane`/`CharonPane` already use (socket-backed `ByteStream`s).
`NativeSessionServer`'s existing protocol (`subscribe`/`input`/`resize` → `output`/`status`,
with base64 + snapshot replay, `native_session.rs:78-111`) is the **direct seed** of the
daemon protocol; we generalize it from one session to many.

---

## 2. Control protocol (client ↔ `charond`)

**Transport:** Unix domain socket at `~/.charon/charond.sock` (override `$CHARON_SOCK`;
`$CHARON_DIR` overrides the whole state dir). Newline-delimited JSON ("JSON-lines"),
one object per line. Binary terminal payloads are base64 in a `data` field.

**Versioning:** every connection begins with a `hello` handshake carrying a `proto`
integer. The daemon rejects/upgrades mismatches. This is what makes
[live handoff](#7-live-handoff--upgrade-in-place) and front-end co-evolution safe.

### 2.1 Client → daemon

| `type` | Fields | Meaning |
|---|---|---|
| `hello` | `proto`, `client` (`"tui"`/`"cli"`/…), `pid` | handshake; daemon replies `welcome` |
| `list` | — | request session inventory → `inventory` |
| `attach` | `session`, `cols`, `rows`, `replay?` (bool) | subscribe to a session's output; `replay` requests scrollback snapshot first |
| `detach` | `session` | stop receiving output for a session (session keeps running) |
| `input` | `session`, `data` (b64) | keystrokes to the session PTY |
| `resize` | `session`, `cols`, `rows` | resize a session (per-attachment; see [§4.4](#44-resize-arbitration)) |
| `spawn` | `kind`, `cmd?`, `cwd?`, `cols?`, `rows?`, `session?` | create a new session of a backend kind |
| `kill` | `session` | terminate a session (and discard its persisted history) |
| `respawn` | `session` | re-run an exited session's command, preserving scrollback |
| `scrollback` | `session`, `before?`, `lines` | fetch older history beyond the live snapshot (TODO) |
| `ping` | `ts` | liveness / latency probe |

### 2.2 Daemon → client

| `type` | Fields | Meaning |
|---|---|---|
| `welcome` | `proto`, `daemon_version`, `pid` | handshake ack |
| `inventory` | `sessions[]` | full state (see [§3 model](#3-session-model)) |
| `output` | `session`, `data` (b64), `seq` | terminal bytes; `seq` enables gap detection on reconnect |
| `snapshot` | `session`, `data` (b64), `cols`, `rows`, `seq` | scrollback replay on `attach`+`replay` |
| `status` | `session`, `state`, `detail?` | lifecycle/agent state change (see [§5](#5-agent-state-detection)) |
| `spawned` / `exited` | `session` | session created / ended |
| `error` | `code`, `message`, `session?` | structured failure |
| `pong` | `ts` | ping reply |

**Design notes**
- `seq` per session lets a reconnecting client say "I have up to seq N" and the
  daemon replays only the gap (or a fresh `snapshot` if the gap exceeds the retained
  ring). This makes reattach seamless and lets a flaky socket recover. *(seq-gap
  reconnect is still TODO; today reattach always sends a full snapshot.)*
- Fan-out is many-clients-per-session: the daemon keeps a subscriber list per
  session and writes `output` to each, dropping dead ones. Multiple clients on the
  same session is **collaboration for free**.

---

## 3. Session model

The daemon holds the authoritative model; clients render projections of it.

```jsonc
// inventory
{
  "type": "inventory",
  "sessions": [
    {
      "id": "local-01",
      "title": "implementer",
      "kind": "local",                   // local | tmux | boat | remote | charon
      "cols": 120, "rows": 40,
      "state": "working",                // see §5
      "seq": 84213
    }
  ]
}
```

Workspaces/tabs (real containers grouping sessions) are a later phase; today
sessions are flat.

---

## 4. Persistence format

Goal: **session restore with screen history**, surviving daemon restarts/upgrades.
All under `~/.charon/` (override with `$CHARON_DIR`).

### 4.1 Layout

```
~/.charon/
  charond.sock                 # control socket
  charond.pid                  # pidfile + single-instance guard
  sessions/
    local-01/
      meta.json                # id, title, kind, cmd, cwd, cols/rows
      scrollback.log           # append-only raw terminal bytes (the history)
```

### 4.2 Scrollback

- Each session appends raw post-backend bytes to `scrollback.log` as they arrive.
  A size cap (2 MiB) with head-truncation keeps it bounded; the file is compacted
  (rewritten from the in-memory ring) when it grows past 2× the cap.
- On `attach` with `replay`, the daemon sends a `snapshot` built from the retained
  scrollback so the pane repaints its prior state.
- `screen.bin` (a serialized `TerminalState` for instant cold-restore) is a planned
  optimization; today restore replays the raw log.

### 4.3 Restore semantics

- **Daemon restart with live agents:** external backends (boat/tmux/remote) survive
  independently and will be re-attached from `meta.json` (planned). Local PTYs cannot
  survive a daemon exit; on restart they're shown as `exited` with full scrollback
  intact and offer a one-command **respawn** (re-runs `meta.cmd` in the same `cwd`).
  *(Implemented for local sessions.)*
- **Client reconnect (daemon still up):** seamless snapshot replay; no loss.

### 4.4 Resize arbitration

Many clients, one PTY. The daemon tracks a requested size per attachment and applies
a policy (default: smallest attached client wins, like tmux). *(Today the last
attach/resize wins; per-attachment arbitration is TODO.)*

---

## 5. Agent-state detection

The daemon classifies each session into `idle | working | blocked | done | exited`
from several signal sources, in priority order:

1. **Native** — Charon-run agents emit status directly (highest confidence).
2. **Boat protocol** — wrapped external agents report semantic state.
3. **Process scan** — `apps/core-daemon/process_inspector.py`, promoted into the
   daemon, classifies running agent processes by argv/exe.
4. **Output heuristics** — pattern-match terminal output (prompts, spinners,
   permission prompts) to infer `working`/`blocked` for agents we can only observe.

The detector runs once in the daemon and broadcasts `status` events to all clients,
so every front-end shows identical state. *(Phase 4 — not yet implemented.)*

---

## 6. Config + themes

New `~/.charon/config.toml`: theme selection, rebindable keys, behavior, and the
detection table. A `Theme` struct replaces the scattered hardcoded `RGB(...)`
constants in `main.rs`, with a set of built-in themes plus user-defined ones.
Defaults reproduce today's behavior exactly. *(Phase 5 — not yet implemented.)*

---

## 7. Live handoff / upgrade-in-place

Upgrade the daemon binary without killing agents: a new `charond` starts, checks
the `proto` version, the old daemon serializes live state (PTY master FDs via
`SCM_RIGHTS` fd-passing; external sessions re-attach from `meta.json`), and clients
reconnect across the swap. Fallback: local PTYs are flagged `exited`+respawnable;
external sessions survive untouched. *(Phase 7 — not yet implemented.)*

---

## 8. Layout (manual splits, floating, stacked)

Keep auto-tile as the zero-config default; add an optional manual layout tree
(splits with ratios) stored per workspace/tab, plus drag-to-resize on split borders.
Floating/stacked panes are stretch goals. *(Phase 6 — not yet implemented.)*

---

## 9. Backward-compat & interop

- **Keep `~/.charon/boats/*.json` registrations** and the boat socket protocol — the
  daemon consumes them (discovery); external tools keep working. The daemon becomes
  the writer of these, replacing per-TUI `write_registration` (`native_session.rs:156`).
- **Fleet/Harbor unchanged** (`fleet.json`, `backend.rs:57`) — the daemon hosts remote
  attach instead of the TUI; config and SSH path are identical.
- **`CHARON_BOAT_WRAPPED` guard** (`native_session.rs:32`) carries over: a Charon
  running *inside* a boat wrapper must not start a competing daemon.

---

## 10. Migration path (incremental, shippable per step)

Each phase is independently shippable and leaves `main` working. The `ByteStream`
seam ([§1.2](#12-the-seam-that-makes-this-cheap)) means the TUI render path never
has to change.

| # | Phase | Deliverable | Risk |
|---|---|---|---|
| **1** ✅ | **`charond` skeleton** | Standalone daemon binary; multi-session server; pidfile/lock; `hello`/`welcome`/`list`. | med |
| **2** ✅ | **`DaemonClient` backend** | `impl ByteStream` over the control socket; TUI auto-starts the daemon and routes sessions through it. **Detach/reattach works.** | med |
| **3** ✅ | **Persistence** | `sessions/*/scrollback.log` + `meta.json`; restore-as-exited on restart; `attach replay`; respawn. (`screen.bin` fast-restore + `seq`-gap reconnect still TODO.) **Session restore works.** | med |
| **4** | **Agent-state detection** | Promote `process_inspector` + output heuristics into the daemon; broadcast `status`. | low |
| **5** | **Config + themes** | `config.toml`; `Theme` struct; built-in themes; rebindable keys. | low |
| **6** | **Workspaces + tabs + manual splits** | Real workspace containers, named tabs, split layout tree, drag-resize. | med |
| **7** | **Live handoff** | `charond upgrade`, fd-passing, graceful drain. | high |
| **8** | **Additional front-ends** | Point GUI/desktop front-ends at `charond`; one runtime behind every UI. | med |

---

## 11. Acceptance criteria

| Capability | Done when… |
|---|---|
| Background server | `charond` keeps sessions alive with **zero clients** attached. ✅ |
| Detach/reattach | TUI quit + relaunch re-paints all live sessions from `snapshot` with no data loss. ✅ |
| Session restore w/ history | After `charond` restart, every session shows prior scrollback; local ones offer respawn. ✅ |
| Agent-state events | Daemon classifies sessions into `idle/working/blocked/done/exited` from one source. |
| Themes + config | `config.toml` switches theme/keys live; defaults reproduce today's behavior. |
| Live handoff | `charond upgrade` swaps the binary; no client loses a session. |
| Remote SSH | Existing fleet path works through the daemon unchanged. |
| Mouse / copy / clipboard | Existing behavior preserved through `DaemonClient`. ✅ |

---

## 12. Risks & open questions

1. **Python runtime ownership.** `charond` is Rust; the agent runtime is Python
   (`apps/core-daemon`). Decide: does `charond` supervise the Python process(es), or
   do they register with `charond` like boats? *Leaning: daemon supervises.*
2. **fd-passing portability.** `SCM_RIGHTS` works on macOS/Linux. Live handoff is the
   riskiest phase — keep the respawn fallback so it can't block earlier phases.
3. **Single-instance contention.** Robust pidfile + stale-socket cleanup (today:
   connect-probe then remove + bind, which works but isn't fully race-safe).
4. **Scrollback growth.** Per-session cap + global budget; surface what was dropped so
   "history restore" doesn't silently mean "last N MiB".
5. **Protocol stability.** Lock `proto` v1 before any GUI front-end depends on it;
   changes go through the `hello` negotiation.
