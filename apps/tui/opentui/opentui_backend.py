#!/usr/bin/env python3
"""OpenTUI backend — JSON-over-stdio bridge between the Bun frontend and Charon core.

Handles:
- refresh requests (returns full UI payload)
- /setup commands (onboarding flow)
- /agent commands (lifecycle)
- chat messages (forwarded to ConversationEngine when provider is configured)
- process detection (scans for running agents)
- view switching
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Dict, List, Optional

root_dir = Path(__file__).resolve().parents[3]
state_dir = root_dir / ".charon_state"
queue_file = state_dir / "queue.json"
run_log_file = state_dir / "run.log"
delegation_tasks_file = state_dir / "delegation" / "tasks.json"
worker_pid_file = state_dir / "delegation" / "worker.pid"
onboarding_file = state_dir / "onboarding.json"
auth_file = state_dir / "auth" / "auth.json"
mascot_sprite_path = root_dir / "assets" / "lantern_wraith_terminal_sprite_v2.json"
mast_title_path = root_dir / "assets" / "title_ascii.txt"
onboarding_doc_path = root_dir / "docs" / "onboarding-summary.md"


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def tail_lines(path: Path, count: int = 10):
    try:
        return path.read_text().splitlines()[-count:]
    except Exception:
        return []


def worker_status():
    try:
        pid = int(worker_pid_file.read_text().strip())
        os.kill(pid, 0)
        return f"RUNNING (pid {pid})"
    except Exception:
        return "STOPPED"


# ---------------------------------------------------------------------------
# Asset loading
# ---------------------------------------------------------------------------

def load_mascot_sprite() -> Dict[str, object]:
    if not mascot_sprite_path.exists():
        return {"lines": [], "width": 0, "height": 0}
    try:
        data = json.loads(mascot_sprite_path.read_text())
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        cells = data.get("cells") if isinstance(data.get("cells"), list) else []
        if not width or not height or not cells:
            return {"lines": [], "width": width, "height": height}
        grid: List[List[str]] = [[" " for _ in range(width)] for _ in range(height)]
        for cell in cells:
            try:
                x = int(cell.get("x", -1) or -1)
                y = int(cell.get("y", -1) or -1)
            except Exception:
                continue
            ch = cell.get("ch") or " "
            if 0 <= y < height and 0 <= x < width:
                grid[y][x] = ch
        lines = ["".join(row).rstrip() for row in grid]
        lines = [line for line in lines if line]
        return {"lines": lines, "width": width, "height": height}
    except Exception:
        return {"lines": [], "width": 0, "height": 0}


def load_title_art() -> List[str]:
    if mast_title_path.exists():
        try:
            return [line for line in mast_title_path.read_text().splitlines() if line.strip()]
        except Exception:
            return []
    return []


def load_onboarding_doc() -> List[str]:
    if onboarding_doc_path.exists():
        try:
            return onboarding_doc_path.read_text().splitlines()
        except Exception:
            return []
    return []


MASCOT_DATA = load_mascot_sprite()
TITLE_ART_LINES = load_title_art()
ONBOARDING_DOC_LINES = load_onboarding_doc()


# ---------------------------------------------------------------------------
# Core module loading
# ---------------------------------------------------------------------------

agent_lifecycle = load_module(
    "agent_lifecycle",
    root_dir / "apps" / "core-daemon" / "agent_lifecycle.py",
)
agent_cache = load_module(
    "agent_cache",
    root_dir / "apps" / "core-daemon" / "agent_cache.py",
)
conversation_runtime = load_module(
    "conversation_runtime",
    root_dir / "apps" / "core-daemon" / "conversation_runtime.py",
)
user_model = load_module(
    "user_model",
    root_dir / "apps" / "core-daemon" / "user_model.py",
)

# Process inspector for detecting external agents
sys.path.insert(0, str(root_dir / "apps" / "tui"))
from process_inspector import detect_agent_processes, summarize_agent_processes

# Auth module
charon_auth = load_module(
    "charon_auth",
    root_dir / "apps" / "core-daemon" / "charon_auth.py",
)

# LLM adapter for model detection
llm_adapter = load_module(
    "llm_adapter",
    root_dir / "apps" / "core-daemon" / "llm_adapter.py",
)


# ---------------------------------------------------------------------------
# Onboarding state helpers
# ---------------------------------------------------------------------------

def default_onboarding() -> dict:
    return {
        "complete": False,
        "step": "provider-mode",
        "provider_mode": "",
        "provider": "",
        "provider_model": "",
        "provider_base_url": "",
        "model": "",
        "provider_auth": "",
        "opencode_provider": "",
        "opencode_model": "",
        "api_key": "",
        "project": "",
        "updated_at": "",
    }


def load_onboarding_state() -> dict:
    data = read_json(onboarding_file, {})
    base = default_onboarding()
    if isinstance(data, dict):
        for k in base:
            if k in data:
                base[k] = data[k]
    return base


def save_onboarding_state(state: dict) -> None:
    onboarding_file.parent.mkdir(parents=True, exist_ok=True)
    onboarding_file.write_text(json.dumps(state, indent=2))


def save_auth_provider(provider_id: str, payload: dict) -> None:
    store = read_json(auth_file, {})
    if not isinstance(store, dict):
        store = {}
    store.setdefault("version", 1)
    store.setdefault("providers", {})
    store["active_provider"] = provider_id
    store["providers"][provider_id] = payload
    auth_dir = auth_file.parent
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(store, indent=2))
    try:
        os.chmod(auth_dir, 0o700)
        os.chmod(auth_file, 0o600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Backend API
# ---------------------------------------------------------------------------

class BackendAPI:
    def __init__(self):
        self.chat: List[dict] = []  # {role, content, ts}
        self.onboarding = load_onboarding_state()
        self.suggestions: List[str] = []
        self.onboarding_doc: List[str] = ONBOARDING_DOC_LINES
        self.detected_processes: List[dict] = []
        self.current_view: str = "chat"  # chat | dashboard | sessions
        self.auth_queue: Queue = Queue()
        self.auth_busy: bool = False
        self._streaming_text: str = ""  # buffer for streaming response
        self._scan_processes()

    def _scan_processes(self) -> None:
        """Scan system for running agent processes."""
        try:
            procs = detect_agent_processes()
            self.detected_processes = [
                {
                    "target": p.target,
                    "pid": p.pid,
                    "command": p.command,
                    "args": p.formatted_args(80),
                }
                for p in procs
            ]
        except Exception:
            self.detected_processes = []

    def _append_chat(self, role: str, content: str) -> None:
        if not content:
            return
        self.chat.append({
            "role": role,
            "content": content.strip(),
            "ts": time.time(),
        })
        self.chat = self.chat[-500:]

    def _queue_stats(self) -> dict:
        queue = read_json(queue_file, [])
        delegation = read_json(delegation_tasks_file, [])
        return {
            "pending": len([q for q in queue if q.get("status") == "pending"]),
            "in_progress": len([q for q in queue if q.get("status") == "in_progress"]),
            "done": len([q for q in queue if q.get("status") == "completed"]),
            "delegation": len([d for d in delegation if d.get("status") == "pending"]),
        }

    def _rearview(self) -> List[str]:
        lines = tail_lines(run_log_file, 8)
        try:
            for row in conversation_runtime.list_conversations(state_dir)[:3]:
                lines.append(
                    f"thread {row.get('conversation_id')} msgs={row.get('message_count')} last={row.get('last_message_id')}"
                )
        except Exception:
            pass
        return lines[-6:]

    def _agents_list(self) -> List[dict]:
        """Return combined list of charon agents + detected external agents."""
        agents = []

        # Charon-managed agents
        try:
            managed = agent_lifecycle.list_agents()
        except Exception:
            managed = []

        for agent in managed:
            agents.append({
                "id": agent.get("id", ""),
                "name": agent.get("name", "unknown"),
                "status": agent.get("status", "idle"),
                "role": agent.get("role", "charon"),
                "mode": agent.get("mode", "persistent"),
                "type": "charon",
                "tmux_session": agent.get("tmux_session", ""),
                "project": agent.get("project", ""),
                "goal": agent.get("goal", ""),
            })

        # Detected external agents
        for proc in self.detected_processes:
            agents.append({
                "id": f"ext-{proc['pid']}",
                "name": proc["target"],
                "status": "running",
                "role": "external",
                "mode": "detected",
                "type": "external",
                "pid": proc["pid"],
                "command": proc["command"],
                "args": proc["args"],
            })

        return agents

    def _onboarding_complete(self) -> bool:
        return bool(self.onboarding.get("complete"))

    def _suggestions_for(self, prefix: str) -> List[str]:
        prefix = (prefix or "").strip()
        if not prefix.startswith("/"):
            return []
        mode_opts = [
            "/setup provider (use charon agents)",
            "/setup no-provider (manage existing agents)",
        ]
        provider_opts = [
            "/setup provider codex (openai oauth)",
            "/setup provider claude-code (anthropic oauth)",
            "/setup provider opencode (local/opencode)",
            "/setup provider api (openrouter api key)",
        ]
        misc = [
            "/setup auth start",
            "/setup model <name>",
            "/setup project <name>",
            "/setup complete",
            "/setup status",
            "/setup reset",
            "/dashboard",
            "/chat",
            "/agents",
            "/agents detect",
            "/help",
            "/quit",
        ]
        all_opts = mode_opts + provider_opts + misc
        if prefix in ("/", "/s", "/se", "/set", "/setup", "/setup "):
            return mode_opts
        if prefix.startswith("/setup provider"):
            return [p for p in provider_opts if p.startswith(prefix) or prefix in ("/setup provider", "/setup provider ")]
        return [p for p in all_opts if p.startswith(prefix)][:8]

    def get_payload(self) -> dict:
        stats = self._queue_stats()

        # Drain auth queue
        auth_messages = []
        while True:
            try:
                kind, payload = self.auth_queue.get_nowait()
                auth_messages.append({"kind": kind, "payload": payload})
            except Empty:
                break

        payload = {
            "view": self.current_view,
            "chat": [
                {"role": msg["role"], "content": msg["content"]}
                for msg in self.chat[-200:]
            ],
            "queue": stats,
            "status_lines": [
                f"queue pending={stats['pending']} in_progress={stats['in_progress']} done={stats['done']}",
                f"delegation pending={stats['delegation']}",
                f"worker {worker_status()}",
            ],
            "run_log": tail_lines(run_log_file, 6),
            "agents": self._agents_list(),
            "rearview": self._rearview(),
            "onboarding": {
                "step": self.onboarding.get("step"),
                "complete": self.onboarding.get("complete"),
                "provider_mode": self.onboarding.get("provider_mode"),
                "provider": self.onboarding.get("provider"),
                "model": self.onboarding.get("model"),
                "project": self.onboarding.get("project"),
            },
            "input_hint": "",
            "suggestions": self.suggestions,
            "mascot_title": TITLE_ART_LINES,
            "mascot_image": MASCOT_DATA.get("lines", []),
            "mascot_meta": {"width": MASCOT_DATA.get("width", 0), "height": MASCOT_DATA.get("height", 0)},
            "onboarding_doc": self.onboarding_doc,
            "detected_processes": self.detected_processes,
            "auth_messages": auth_messages,
            "streaming_text": self._streaming_text,
        }
        return payload

    # -------------------------------------------------------------------
    # Command handling
    # -------------------------------------------------------------------

    def process_command(self, command: str) -> None:
        command = (command or "").strip()
        if not command:
            return

        # View switching commands
        if command in ("/dashboard", "/dash", "/work"):
            self.current_view = "dashboard"
            self._append_chat("system", "switched to dashboard")
            return
        if command in ("/chat", "/home"):
            self.current_view = "chat"
            self._append_chat("system", "switched to chat")
            return
        if command in ("/sessions",):
            self.current_view = "sessions"
            self._append_chat("system", "switched to sessions")
            return

        # Help
        if command in ("/help", "/?"):
            self._append_chat("system",
                "Commands:\n"
                "  /setup         — onboarding wizard\n"
                "  /setup provider <name> — set provider (codex, claude-code, opencode, api)\n"
                "  /setup no-provider     — agent OS mode (no LLM)\n"
                "  /setup model <name>    — set model\n"
                "  /setup project <path>  — set project\n"
                "  /setup complete        — finish onboarding\n"
                "  /setup status          — show setup state\n"
                "  /setup reset           — reset onboarding\n"
                "  /dashboard             — switch to dashboard\n"
                "  /chat                  — switch to chat\n"
                "  /agents                — list agents\n"
                "  /agents detect         — rescan for running agents\n"
                "  /agent create <name>|<goal> — create agent\n"
                "  /quit                  — exit charon"
            )
            return

        # Setup commands
        if command.startswith("/setup"):
            self._handle_setup(command)
            return

        # Agent commands
        if command in ("/agents", "/agents list"):
            agents = self._agents_list()
            if not agents:
                self._append_chat("system", "No agents detected")
            else:
                lines = []
                for a in agents:
                    icon = {"running": "●", "idle": "○", "stopped": "✖"}.get(a["status"], "·")
                    label = f"{icon} {a['name']} ({a['id']}) [{a['type']}] {a['status']}"
                    if a.get("goal"):
                        label += f" — {a['goal'][:60]}"
                    lines.append(label)
                self._append_chat("system", "\n".join(lines))
            return

        if command in ("/agents detect", "/agents scan"):
            self._scan_processes()
            lines = summarize_agent_processes(detect_agent_processes())
            self._append_chat("system", "Process scan:\n" + "\n".join(lines))
            return

        if command.startswith("/agent create"):
            payload = command.split("/agent create", 1)[1].strip()
            if "|" in payload:
                name, goal = payload.split("|", 1)
            else:
                name = payload or "charon-agent"
                goal = "general assistant"
            try:
                agent = agent_lifecycle.create_agent(
                    name=name.strip() or "charon-agent",
                    mode="persistent",
                    goal=goal.strip(),
                    require_tmux=False,
                )
                self._append_chat("system", f"Created agent {agent['name']} ({agent['id']})")
            except Exception as e:
                self._append_chat("system", f"Failed to create agent: {e}")
            return

        if command == "/quit":
            self._append_chat("system", "goodbye")
            return

        # Suggestion request
        if command.startswith("/suggest "):
            prefix = command[len("/suggest "):].strip()
            self.suggestions = self._suggestions_for(prefix)
            return

        # If onboarding is complete and it's a plain message, treat as chat
        if not command.startswith("/"):
            self._append_chat("user", command)
            if self._onboarding_complete() and self.onboarding.get("provider_mode") == "provider":
                self._append_chat("assistant", "Chat with LLM coming soon — engine wiring in progress")
            else:
                self._append_chat("system", "Type /setup to configure a provider, or use /help for commands")
            return

        self._append_chat("system", f"Unknown command: {command}. Type /help for available commands.")

    def _handle_setup(self, command: str) -> None:
        parts = command.split()

        # /setup (bare) — show status
        if len(parts) == 1:
            self._show_setup_status()
            return

        sub = parts[1] if len(parts) > 1 else ""

        if sub == "status":
            self._show_setup_status()
            return

        if sub == "reset":
            self.onboarding = default_onboarding()
            save_onboarding_state(self.onboarding)
            self._append_chat("system", "Setup reset. Run /setup to start again.")
            return

        if sub == "no-provider":
            self.onboarding["provider_mode"] = "no-provider"
            self.onboarding["provider"] = "none"
            self.onboarding["step"] = "project"
            save_onboarding_state(self.onboarding)
            self._append_chat("system",
                "No-provider mode. Charon will act as agent OS.\n"
                "Set project with: /setup project <path>"
            )
            return

        if sub == "provider":
            if len(parts) < 3:
                self._append_chat("system",
                    "Choose a provider:\n"
                    "  /setup provider codex        — OpenAI (OAuth)\n"
                    "  /setup provider claude-code   — Anthropic (OAuth)\n"
                    "  /setup provider opencode      — Local/OpenCode\n"
                    "  /setup provider api           — OpenRouter API key"
                )
                return
            provider = parts[2].strip().lower()
            allowed = {"codex", "claude-code", "opencode", "api", "lmstudio"}
            if provider not in allowed:
                self._append_chat("system", f"Unknown provider: {provider}. Options: {', '.join(sorted(allowed))}")
                return
            self.onboarding["provider_mode"] = "provider"
            self.onboarding["provider"] = provider
            self.onboarding["step"] = "model"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", f"Provider set to {provider}")

            if provider in ("codex", "claude-code"):
                self._start_auth(provider)
            elif provider == "api":
                self.onboarding["step"] = "api-key"
                save_onboarding_state(self.onboarding)
                self._append_chat("system", "Enter API key: /setup api-key <key>")
            elif provider == "opencode":
                self.onboarding["step"] = "opencode-provider"
                save_onboarding_state(self.onboarding)
                self._append_chat("system", "Set opencode provider: /setup opencode-provider <name>")
            return

        if sub == "auth" and len(parts) >= 3 and parts[2] == "start":
            provider = self.onboarding.get("provider", "")
            if provider in ("codex", "claude-code"):
                self._start_auth(provider)
            else:
                self._append_chat("system", "Auth only needed for codex/claude-code providers")
            return

        if sub == "api-key" and len(parts) >= 3:
            key = parts[2].strip()
            self.onboarding["api_key"] = key
            save_auth_provider("openrouter", {"api_key": key, "base_url": "https://openrouter.ai/api/v1", "auth_type": "api_key"})
            self.onboarding["step"] = "model"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", "API key saved. Now set model: /setup model <name>")
            return

        if sub == "model" and len(parts) >= 3:
            model = " ".join(parts[2:]).strip()
            self.onboarding["model"] = model
            self.onboarding["provider_model"] = model
            self.onboarding["step"] = "project"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", f"Model set to {model}. Now set project: /setup project <path>")
            return

        if sub == "project" and len(parts) >= 3:
            project = " ".join(parts[2:]).strip()
            self.onboarding["project"] = project
            self.onboarding["step"] = "complete"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", f"Project set to {project}. Run /setup complete to finish.")
            return

        if sub == "complete":
            self.onboarding["complete"] = True
            self.onboarding["step"] = "done"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", "Onboarding complete!")

            # Spawn initial charon agent if in provider mode
            if self.onboarding.get("provider_mode") == "provider":
                try:
                    agent = agent_lifecycle.create_agent(
                        name="",
                        mode="persistent",
                        goal="Main Charon agent",
                        project=self.onboarding.get("project") or str(root_dir),
                        require_tmux=False,
                    )
                    self._append_chat("system",
                        f"Spawned agent {agent['name']} ({agent['id']}). "
                        f"Switch to /dashboard to see your agents."
                    )
                except Exception as e:
                    self._append_chat("system", f"Agent spawn failed: {e}")
            else:
                self._append_chat("system",
                    "No-provider mode active. Use /dashboard to see detected agents."
                )

            # Rescan for processes
            self._scan_processes()
            return

        if sub == "opencode-provider" and len(parts) >= 3:
            self.onboarding["opencode_provider"] = parts[2].strip()
            self.onboarding["step"] = "opencode-model"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", f"OpenCode provider: {parts[2].strip()}. Now: /setup opencode-model <name>")
            return

        if sub == "opencode-model" and len(parts) >= 3:
            model = parts[2].strip()
            self.onboarding["opencode_model"] = model
            self.onboarding["model"] = model
            self.onboarding["provider_model"] = model
            self.onboarding["step"] = "project"
            save_onboarding_state(self.onboarding)
            self._append_chat("system", f"OpenCode model: {model}. Now: /setup project <path>")
            return

        self._append_chat("system", f"Unknown setup subcommand. Run /setup for help.")

    def _show_setup_status(self) -> None:
        o = self.onboarding
        done = "✓ yes" if o.get("complete") else "✗ no"
        mode = o.get("provider_mode") or "(not set)"
        provider = o.get("provider") or "(not set)"
        model = o.get("model") or "(not set)"
        project = o.get("project") or "(not set)"
        self._append_chat("system",
            f"Setup Status:\n"
            f"  Complete: {done}\n"
            f"  Step: {o.get('step', 'provider-mode')}\n"
            f"  Mode: {mode}\n"
            f"  Provider: {provider}\n"
            f"  Model: {model}\n"
            f"  Project: {project}\n"
            f"\n"
            f"Run /setup provider <name> or /setup no-provider to begin."
        )

    def _start_auth(self, provider: str) -> None:
        if self.auth_busy:
            self._append_chat("system", "Auth already in progress...")
            return
        self.auth_busy = True
        self._append_chat("system", f"Starting OAuth flow for {provider}...")

        def _run():
            provider_id = "anthropic" if provider == "claude-code" else "openai-codex"

            def _status(msg: str):
                if not msg:
                    return
                self.auth_queue.put(("log", msg))
                if msg.startswith("AUTH_URL::"):
                    url = msg.split("AUTH_URL::", 1)[1].strip()
                    self.auth_queue.put(("auth_url", url))

            try:
                charon_auth.login_oauth(provider_id, status_cb=_status)
                self.auth_queue.put(("done", ""))
                self.onboarding["provider_auth"] = "oauth"
                self.onboarding["step"] = "model"
                save_onboarding_state(self.onboarding)
            except Exception as e:
                self.auth_queue.put(("error", str(e)))
            finally:
                self.auth_busy = False

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def run(self) -> None:
        self._append_chat("system", "Charon ready. Type /setup to configure or /help for commands.")
        if not self._onboarding_complete():
            self._append_chat("system",
                "Welcome to Charon! Start with /setup to configure your provider,\n"
                "or /setup no-provider to use Charon as an agent management dashboard."
            )
        self.send_response("init")

        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            req_type = message.get("type")
            if req_type == "refresh":
                self.send_response(message.get("request_id"))
            elif req_type == "command":
                self.process_command(message.get("command", ""))
                self.send_response(message.get("request_id"))
            elif req_type == "suggest":
                prefix = message.get("prefix", "")
                self.suggestions = self._suggestions_for(prefix)
                self.send_response(message.get("request_id"))
            elif req_type == "view":
                self.current_view = message.get("view", "chat")
                self.send_response(message.get("request_id"))
            elif req_type == "scan_processes":
                self._scan_processes()
                self.send_response(message.get("request_id"))

    def send_response(self, request_id: str | None) -> None:
        payload = self.get_payload()
        response = {"type": "refresh", "request_id": request_id, "payload": payload}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main() -> None:
    backend = BackendAPI()
    backend.run()


if __name__ == "__main__":
    main()
