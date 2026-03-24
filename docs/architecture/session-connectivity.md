# Charon Session Connectivity Architecture

> How Charon connects to, views, and interacts with agent sessions — local and remote.

## The Core Problem

You want to see and interact with agent sessions from one place. The agents could be:

1. **Charon agents** (yours, local or remote)
2. **Other agents in tmux** (pi-agent, hermes, claude code, codex, opencode — running in tmux sessions you started manually)
3. **Other agents NOT in tmux** (running in a bare terminal, or as a subprocess)

## tmux as the Universal Bridge

tmux is the key because it **decouples a terminal session from the terminal that's viewing it**. Once a process runs inside tmux:

- You can capture its screen: `tmux capture-pane -t session -p`
- You can send it keystrokes: `tmux send-keys -t session "hello" Enter`
- You can do this from ANY process on the same machine — no special plugin needed
- Multiple viewers can watch simultaneously
- The session persists even if all viewers disconnect

## Cases

### Case 1: Charon agents (local)

**Already solved.** `agent_lifecycle.create_agent()` creates a tmux session (`charon-AG-XXXX`). When you type `charon`, it auto-detects these sessions via `tmux list-sessions`. We capture and send keys. No plugin needed.

### Case 2: Other agents already in tmux (local)

**Solved without any plugin.** If someone runs `tmux new -s pi-session` and then runs `pi` inside it, we can:

- Detect it: `tmux list-sessions` shows `pi-session`
- Capture: `tmux capture-pane -t pi-session -p`
- Send input: `tmux send-keys -t pi-session "hello" Enter`

The user just tells Charon to watch it via `/session add pi-session` or we auto-detect by scanning tmux sessions.

### Case 3: Other agents NOT in tmux (local)

**This is where charons-boat matters.** If someone runs `pi` in a bare terminal (no tmux), we can't capture or interact with it.

**charons-boat wraps in tmux automatically.** The plugin, when installed in the agent framework, does this on startup:

```
1. Check if we're already inside tmux → if yes, just register with Charon
2. If NOT in tmux → re-exec inside a new tmux session:
   tmux new-session -d -s boat-<name> "original-command-with-args"
   tmux attach -t boat-<name>
```

The user's experience doesn't change — they still see their agent running normally. But now there's a tmux session backing it, which Charon can connect to.

### Case 4: Remote Charon agents

**charons-boat handles the SSH tunneling.** When you connect a remote server:

1. charons-boat on the remote server runs alongside your agents
2. It opens a lightweight daemon that listens for connections
3. Your local Charon connects via SSH tunnel (charons-boat manages the tunnel)
4. All you need: SSH access to the server (`ssh user@server` must work)
5. tmux commands are executed remotely through the tunnel

For persistent connections, charons-boat keeps an SSH ControlMaster socket alive:

```
ssh -MNf -S /tmp/charon-ssh-%h -o ControlPersist=600 user@server
```

Subsequent commands reuse it with zero connection overhead:

```
ssh -S /tmp/charon-ssh-%h user@server tmux capture-pane -t session -p
```

### Case 5: Remote non-Charon agents in tmux

**Same as Case 4.** If the remote server has pi-agent running in a tmux session, charons-boat on the remote side discovers it, and your local Charon can capture/interact via the SSH tunnel.

### Case 6: Remote agents NOT in tmux

**charons-boat on the remote side.** The user installs charons-boat in their remote agent framework. It wraps in tmux. Then Case 4 applies.

## Security Model

charons-boat uses a **shared pairing code** for authentication:

1. When you install charons-boat on a remote server, it generates a pairing code (or you provide one)
2. On your local Charon, you run `/link add user@server --code <pairing-code>`
3. charons-boat on the remote side validates the code before accepting connections
4. After initial pairing, a persistent identity key is exchanged and stored
5. All subsequent connections authenticate via the key pair — no code needed again
6. The SSH tunnel provides transport encryption

This means:
- **You only need SSH access** to the remote server (password, key-based, or agent forwarding)
- **charons-boat handles everything else**: tunnel setup, session discovery, screen capture, input forwarding
- **The pairing code prevents unauthorized access** even if someone else has SSH access to the same server
- **Keys can be revoked** from either side via `/link revoke <server>` or on the remote via `charons-boat revoke`

## Full Architecture Diagram

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
│        │                        │                           │
│        │  Pairing code auth     │                           │
│        │  SSH ControlMaster     │                           │
│        │  tmux over SSH         │                           │
│        │                        │                           │
└────────┼────────────────────────┼───────────────────────────┘
         │                        │
┌─ Remote server ─────────────────┼───────────────────────────┐
│        │                        │                           │
│  charons-boat (daemon)          │                           │
│    │                            │                           │
│    ├─ Validates pairing code / identity key                 │
│    ├─ Discovers local tmux sessions                         │
│    ├─ Wraps non-tmux agents in tmux                         │
│    ├─ Exposes session list + capture/send API               │
│    │                                                        │
│    ├─ Charon agents ──→ tmux sessions (auto-wrapped)        │
│    ├─ tmux sessions ──→ any agent in tmux                   │
│    └─ boat agents ────→ charons-boat wrapped in tmux        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## What charons-boat Is

A lightweight installable tool with two modes:

### 1. Agent wrapper mode

Wraps any agent command in tmux and registers with Charon:

```bash
# Wrap pi-agent
charons-boat wrap -- pi

# Wrap any command
charons-boat wrap --name "my-claude" -- claude

# Already in tmux? Just register
charons-boat register
```

What it does:
1. **Detects** if the current process is in tmux
2. **If not**, re-execs inside a new tmux session (transparent to the user)
3. **Registers** with the local Charon instance (writes to `~/.charon/boats/`)
4. **Optionally opens a control socket** for richer metadata (agent type, status)

### 2. Remote daemon mode

Runs on a remote server to enable cross-machine connectivity:

```bash
# On the remote server
charons-boat serve --code MY-PAIRING-CODE

# Or generate a code
charons-boat serve --generate-code
```

What it does:
1. Listens for incoming SSH tunnel connections from Charon
2. Validates pairing codes / identity keys
3. Discovers all tmux sessions on the server
4. Serves the capture/send API over the tunnel
5. Manages session lifecycle (detect new sessions, clean up dead ones)

### Installation in agent frameworks

**pi-agent / hermes:** Add as an extension

```typescript
// .pi/extensions/charons-boat.ts
export default {
  name: "charons-boat",
  setup(pi) {
    // Register this session with Charon
    pi.on("session_start", () => {
      exec("charons-boat register --name " + pi.sessionId)
    })
  }
}
```

**Generic (any agent):** Wrap the launch command

```bash
# Instead of: pi
# Run: charons-boat wrap -- pi

# Add to .bashrc for automatic wrapping:
alias pi="charons-boat wrap -- pi"
alias hermes="charons-boat wrap -- hermes"
```

## Implementation Phases

### Phase 1: Local tmux capture (immediate, no plugin)

1. Scan `tmux list-sessions` to find all local tmux sessions
2. Match Charon agent sessions by name pattern (`charon-AG-*`)
3. In the session grid cell, run `tmux capture-pane -t <session> -p` every 500ms
4. When user focuses a grid cell and types, forward via `tmux send-keys`
5. Escape returns focus to grid navigation

### Phase 2: charons-boat wrapper

1. Build the `charons-boat wrap` command
2. Build the registration protocol (`~/.charon/boats/` directory)
3. Auto-detect boat-registered sessions alongside tmux sessions
4. Add agent type metadata (pi-agent, hermes, claude, etc.)

### Phase 3: Remote connectivity

1. Build `charons-boat serve` daemon
2. Implement pairing code / identity key exchange
3. SSH ControlMaster tunnel management
4. Remote session discovery and capture/send over tunnel
5. `/link add user@server --code CODE` command in Charon

### Phase 4: Rich integration

1. Agent-specific metadata (model, token usage, current task)
2. Structured control commands (not just raw keystrokes)
3. Session health monitoring and auto-reconnect
4. Multi-server dashboard with latency indicators
