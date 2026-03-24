# Charon's Boat Protocol

JSON lines over stdio, tunneled through SSH.

## Connection

```bash
# Local side connects by running:
ssh user@remote charons-boat stream

# With SSH multiplexing for instant reconnect:
ssh -o ControlMaster=auto -o ControlPath=~/.ssh/charon-%r@%h \
    user@remote charons-boat stream
```

No custom ports. No firewall rules. If you can SSH, you can stream.

## Message Format

One JSON object per line, both directions.

### Remote → Local (events)

```jsonc
// Session list (sent on connect, and when sessions change)
{"type": "sessions", "sessions": [
  {"id": "charon-01", "name": "charon-01", "agent": "hermes", "status": "running", "cols": 120, "rows": 40},
  {"id": "charon-02", "name": "charon-02", "agent": "pi", "status": "idle", "cols": 120, "rows": 40}
]}

// Screen update (sent on change, for the focused session)
{"type": "screen", "session": "charon-01", "lines": ["line1...", "line2..."], "cursor": [24, 80], "dirty": true}

// Raw bytes (alternative to parsed screen — for full terminal emulation on client side)
{"type": "output", "session": "charon-01", "data": "base64-encoded-bytes"}

// Notification (agent needs attention)
{"type": "notify", "session": "charon-01", "kind": "approval", "text": "Allow bash: rm -rf /tmp/test?"}

// Session status change
{"type": "status", "session": "charon-01", "status": "idle"}

// Heartbeat (every 30s)
{"type": "ping", "ts": "2026-03-23T12:00:00Z"}
```

### Local → Remote (commands)

```jsonc
// Focus a session (start receiving screen updates for it)
{"type": "focus", "session": "charon-01"}

// Send input to the focused session
{"type": "input", "session": "charon-01", "data": "base64-encoded-keystrokes"}

// Resize the session
{"type": "resize", "session": "charon-01", "cols": 120, "rows": 40}

// Respond to approval
{"type": "approve", "session": "charon-01", "approved": true}

// Pong
{"type": "pong"}
```

## Authentication

SSH handles authentication. The `charons-boat stream` command only
runs if the SSH session is established. No additional auth layer needed.

Optional pairing codes (from `charons-boat serve --generate-code`)
add a second factor for shared machines.

## Reconnection

The remote daemon keeps all sessions alive regardless of client
connections. On reconnect:

1. Client gets a fresh `sessions` message with current state
2. Client sends `focus` for the session it wants
3. Daemon sends full `screen` snapshot, then incremental `output` updates
4. Any notifications that fired while disconnected are replayed

## Bandwidth

- `screen` mode: ~5-15KB per update (full screen text). Good for
  monitoring dashboards. Sent only on change.
- `output` mode: raw bytes, variable. Good for interactive use.
  Client-side terminal parser needed.

Default is `output` mode (raw bytes) for the Rust TUI. `screen` mode
is a fallback for simple clients.
