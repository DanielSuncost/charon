# charons-boat — Universal Agent Session Bridge

> Connect any coding agent session to Charon, from anywhere.

## What It Does

charons-boat lets you view and interact with agent sessions running on any machine — from your local Charon dashboard. It works with pi-agent, hermes, claude code, codex, opencode, or any terminal program.

**Prerequisite:** SSH key access to the remote machine. That's it.

## Quick Start

### Scenario: You have pi-agent running on your server

**On your server** (inside your pi-agent session):
```
/charons-boat
```

Output:
```
⛵ charons-boat active
Pairing code: CHARON-A7B3-K9X2
On your local machine: charon link dopppo@myserver --code CHARON-A7B3-K9X2
```

**On your local machine** (inside Charon):
```
/link dopppo@myserver --code CHARON-A7B3-K9X2
```

Output:
```
✓ Connected to dopppo@myserver
  Found 1 session: pi-agent (running)
  Press F3 to view in Session Grid
```

Done. Your remote pi-agent session appears in the Session Grid. You can watch it work and type into it.

### Scenario: You want to wrap any command

**On your server:**
```bash
charons-boat wrap -- hermes
```

This launches hermes inside a managed tmux session and starts the boat daemon. Same pairing flow.

### Scenario: Local agents (no SSH needed)

**Charon agents** are automatically wrapped in tmux. They appear in the Session Grid with no setup.

**Other local tmux sessions** are auto-discovered. If you ran `tmux new -s my-pi` and started pi inside it, Charon finds it automatically.

## Commands

### From inside an agent (extension mode)

| Command | What it does |
|---------|-------------|
| `/charons-boat` | Start boat, show pairing code |
| `/charons-boat status` | Show connection status |
| `/charons-boat stop` | Stop boat daemon |

### From the terminal (CLI mode)

| Command | What it does |
|---------|-------------|
| `charons-boat wrap -- <cmd>` | Wrap any command in tmux + start boat |
| `charons-boat serve` | Start boat daemon for current tmux session |
| `charons-boat serve --generate-code` | Generate new pairing code |
| `charons-boat status` | Show status |
| `charons-boat stop` | Stop daemon |

### From Charon (connection management)

| Command | What it does |
|---------|-------------|
| `/link <user@host> --code <CODE>` | Connect to a remote boat |
| `/link list` | List all connected remotes |
| `/link remove <user@host>` | Disconnect from a remote |
| `/link scan` | Re-scan a connected remote for new sessions |

## How It Works

### The tmux layer

Every case reduces to: "is there a tmux session we can capture?"

```
Agent running in tmux?
  YES → boat registers the session, Charon can capture/interact
  NO  → boat wraps it in tmux transparently, then registers
```

Capture: `tmux capture-pane -t <session> -p` (gets screen content)
Input:   `tmux send-keys -t <session> <keys>` (sends keystrokes)

### Local flow

```
charons-boat          Charon TUI
    │                     │
    ├─ ensure tmux        │
    ├─ write registration │
    │   ~/.charon/boats/  │
    │   {session, pid,    ├─ scan ~/.charon/boats/
    │    agent_type}      ├─ tmux capture-pane (poll)
    │                     ├─ tmux send-keys (on input)
    │                     │
```

No daemon, no sockets, no network. Just file registration + direct tmux commands.

### Remote flow

```
Remote server              Local machine
─────────────              ─────────────
charons-boat serve         charon
    │                          │
    ├─ generate pairing code   │
    ├─ listen on unix socket   │
    │   ~/.charon/boat.sock    │
    │                          ├─ /link user@host --code CODE
    │                          ├─ ssh user@host (key auth)
    │                          ├─ validate code via boat.sock
    │   ◄──── code OK ────►   ├─ exchange identity keys
    │                          ├─ store keys in ~/.charon/links/
    │                          │
    │   ◄─── SSH tunnel ───►  │  (ControlMaster, persistent)
    │                          │
    ├─ tmux list-sessions      ├─ receives session list
    ├─ tmux capture-pane  ───► ├─ renders in Session Grid
    ├─ tmux send-keys     ◄── ├─ forwards user input
    │                          │
```

### Security

1. **SSH key auth** — you must have `ssh user@host` working (standard SSH keys)
2. **Pairing code** — one-time code validates that YOUR Charon is connecting, not someone else with SSH access
3. **Identity keys** — after pairing, a keypair is stored. Future connections auto-authenticate
4. **Revocation** — `/link remove` on local, `charons-boat revoke` on remote

The pairing code is single-use. After successful pairing, it's invalidated.

## Architecture

```
┌─ Your machine ─────────────────────────────────────────────┐
│                                                             │
│  charon (TUI)                                               │
│    │                                                        │
│    ├─ Local Charon agents ──→ tmux capture/send (direct)    │
│    │   (auto-wrapped in tmux on creation)                   │
│    │                                                        │
│    ├─ Local tmux sessions ──→ tmux capture/send (direct)    │
│    │   (auto-detected via tmux list-sessions)               │
│    │                                                        │
│    ├─ Local boat agents ────→ tmux capture/send (direct)    │
│    │   (charons-boat wrapped them in tmux)                  │
│    │                                                        │
│    └─ Remote servers ───────→ SSH tunnel (managed by boat)  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
         │
         │  SSH (key auth only)
         │  Pairing code → identity key exchange
         │  Persistent ControlMaster tunnel
         │
┌─ Remote server ─────────────────────────────────────────────┐
│                                                             │
│  charons-boat (daemon)                                      │
│    │                                                        │
│    ├─ Validates pairing code / identity key                 │
│    ├─ Discovers local tmux sessions                         │
│    ├─ Wraps non-tmux agents in tmux                         │
│    ├─ Proxies tmux capture/send over tunnel                 │
│    │                                                        │
│    ├─ pi-agent ──────→ tmux session (via /charons-boat)     │
│    ├─ hermes ────────→ tmux session (via wrap or extension) │
│    ├─ charon agents ─→ tmux session (auto-wrapped)          │
│    └─ any command ───→ tmux session (via wrap)              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Agent Framework Extensions

### pi-agent

```typescript
// .pi/extensions/charons-boat.ts
export default function(pi) {
  pi.registerCommand({
    name: "charons-boat",
    description: "Connect this session to Charon",
    handler(args, ctx) {
      const result = execSync("charons-boat serve --from-extension --generate-code")
      ctx.sendMessage({ content: result.toString() })
    }
  })
}
```

### hermes

Same pattern, different extension format. The CLI tool does the work.

### Generic (any agent, no extension system)

```bash
# Alias in .bashrc
alias pi="charons-boat wrap -- pi"
alias hermes="charons-boat wrap -- hermes"
alias claude="charons-boat wrap -- claude"

# Or one-off
charons-boat wrap --name "my-session" -- opencode
```

## Milestone: Remote Session in Grid

**Goal:** Run pi-agent on Server B. From Server A (running Charon), view and interact with that pi-agent session in the Session Grid.

**Prerequisites:**
- SSH key access from A to B (`ssh user@B` works without password prompt)
- charons-boat installed on B
- Charon running on A

**Steps:**

```
Server B:  $ charons-boat wrap -- pi
           ⛵ charons-boat active
           Pairing code: CHARON-K8M2-V4X7
           Waiting for connection...

Server A:  (in Charon TUI)
           /link user@B --code CHARON-K8M2-V4X7
           ✓ Connected. 1 session found.
           (press F3, navigate to the session, press Enter)
           (you now see pi-agent's terminal output live)
           (you type, keystrokes go to pi-agent on Server B)
```

**Implementation order:**

1. `charons-boat wrap` — wraps command in tmux, writes registration
2. `charons-boat serve` — daemon that listens for Charon connections
3. Local tmux capture in Session Grid — `tmux capture-pane` rendering in grid cells
4. Local tmux input — `tmux send-keys` when grid cell is focused
5. `/link` command — SSH tunnel setup, pairing code exchange
6. Remote tmux capture — same as local but over SSH tunnel
7. Remote tmux input — `tmux send-keys` over SSH tunnel

Steps 1-4 work with zero network. Steps 5-7 add remote support.
