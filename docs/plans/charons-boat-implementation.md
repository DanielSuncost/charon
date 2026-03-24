# charons-boat Implementation Plan

## What it does
charons-boat is a lightweight bridge that connects ANY coding agent's terminal session to Charon's session grid. It works locally and remotely.

## Core principle
Every agent session reduces to a tmux pane. charons-boat ensures there's always a tmux pane to capture/send to. Charon never runs inside tmux itself — it observes tmux panes of OTHER agents.

## Phase 1: Local wrap (CLI tool)

### `charons-boat wrap -- <command>`
```bash
$ charons-boat wrap -- pi
# Creates tmux session "boat-pi-$$"
# Runs `pi` inside it
# Registers in ~/.charon/boats/boat-pi-$$.json
# User sees pi running normally (attached to the tmux session)
```

Registration file (`~/.charon/boats/<name>.json`):
```json
{
  "session": "boat-pi-12345",
  "agent_type": "pi",
  "command": "pi",
  "pid": 12345,
  "started": "2026-03-21T...",
  "status": "running"
}
```

### Charon discovers boats
The refresh handler scans `~/.charon/boats/` for registration files. Each one becomes a session in the grid with:
- Live tmux capture (already works)
- Keystroke forwarding via `tmux send-keys`

### Grid interaction
- Arrow keys navigate cells
- Enter on a cell → "enters" the session
- Typed keys → forwarded to the tmux pane via `tmux send-keys`
- Escape → back to grid navigation
- Content updates via `tmux capture-pane` polling

## Phase 2: Agent extensions

### pi-agent extension
A pi skill/extension that registers the current session:
```
/charons-boat
```
This detects if pi is running in tmux. If yes, registers it. If no, tells the user to use `charons-boat wrap -- pi` instead.

### Claude Code / Codex
Same pattern — these run in terminals. Wrap with `charons-boat wrap -- claude`.

## Phase 3: Remote connectivity

### `charons-boat serve` (on remote machine)
Starts a daemon that:
1. Generates a pairing code
2. Listens on a unix socket
3. When paired, proxies `tmux capture-pane` and `tmux send-keys` over the SSH tunnel

### `/link user@host --code CODE` (in Charon)
1. Opens SSH connection to remote
2. Validates pairing code via unix socket
3. Establishes persistent SSH ControlMaster tunnel
4. Polls `tmux capture-pane` over SSH
5. Sends `tmux send-keys` over SSH

## Implementation order

### Step 1: `charons-boat wrap` (30 min)
- Shell script that creates tmux session + runs command
- Writes registration JSON to ~/.charon/boats/
- Already partially built in tools/charons-boat/charons-boat

### Step 2: Charon discovers boats (20 min)
- Scan ~/.charon/boats/ in refresh handler
- Add boat sessions to S.sessions with tmux info
- Grid already renders tmux captures

### Step 3: Grid enter/interact (30 min)
- Enter on grid cell → set enteredSession
- Key forwarding via backend tmux_send
- Escape to exit

### Step 4: Test end-to-end locally
- `charons-boat wrap -- pi` in terminal 1
- `charon` in terminal 2
- F3 → see pi session → Enter → type into pi

### Step 5: Remote serve + link (2 hours)
- charons-boat serve daemon
- SSH tunnel management
- Pairing code exchange
- Remote tmux proxy
