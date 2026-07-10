"""harbor.py — Local-side Harbor protocol coordinator.

Dispatches structured tasks to remote workers via SSH, handles mid-task
memory recall requests, and ingests results into the local memory engine.

Usage:
    from charon.fleet.harbor import dispatch_voyage, get_voyage_status, list_voyages
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SSH_TIMEOUT = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _voyage_id() -> str:
    return f"v-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(4)}"


# ── Manifest assembly ────────────────────────────────────────────────

def _build_manifest(
    voyage_id: str,
    instruction: str,
    server_id: str,
    agent_name: str,
    project_root: Path,
    state_dir: Path,
    timeout: int = 1800,
    agent_type: str = "bare",
) -> dict:
    """Build a voyage manifest with relevant context from Harbor."""
    manifest = {
        "voyage_id": voyage_id,
        "dispatched_at": _now(),
        "instruction": instruction,
        "project": project_root.name,
        "project_root": str(project_root),
        "target_agent_type": agent_type,
        "server_id": server_id,
        "agent_name": agent_name,
        "timeout_seconds": timeout,
        "max_recall_queries": 20,
    }

    # User profile
    try:
        from charon.memory.user_model_structured import load_structured, render_for_prompt
        model = load_structured(state_dir)
        manifest["user_profile"] = render_for_prompt(model)[:3000]
    except Exception:
        manifest["user_profile"] = ""

    # Pre-fetch relevant memories
    try:
        from charon.memory.memory_engine import MemoryEngine
        engine = MemoryEngine(state_dir)
        result = engine.recall(instruction, limit=10)
        manifest["relevant_memories"] = [
            {"content": m.memory.content, "score": m.score, "category": m.memory.category}
            for m in result.memories[:10]
        ]
    except Exception:
        manifest["relevant_memories"] = []

    # Project knowledge
    knowledge_path = state_dir / "KNOWLEDGE.md"
    if not knowledge_path.exists():
        knowledge_path = project_root / "KNOWLEDGE.md"
    if knowledge_path.exists():
        try:
            manifest["project_knowledge"] = knowledge_path.read_text(errors="replace")[:5000]
        except Exception:
            manifest["project_knowledge"] = ""
    else:
        manifest["project_knowledge"] = ""

    # Git context
    try:
        branch = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        manifest["git_context"] = {"branch": branch, "head": head}
    except Exception:
        manifest["git_context"] = {}

    return manifest


# ── SSH connection to remote harbor worker ───────────────────────────

def _build_harbor_ssh_command(server: dict) -> list[str]:
    """Build SSH command to connect to remote harbor worker."""
    cmd = ["ssh"]
    for opt in server.get("ssh_options", []):
        cmd.append(opt)
    cmd.extend(["-o", f"ConnectTimeout={SSH_TIMEOUT}", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"])
    user = server.get("user", "")
    host = server.get("host", "")
    target = f"{user}@{host}" if user else host
    cmd.append(target)
    # Ensure ~/.local/bin is in PATH (charons-boat lives there),
    # then run the harbor subcommand.
    cmd.append('export PATH="$HOME/.local/bin:$PATH" && charons-boat harbor')
    return cmd


# ── Voyage state persistence ────────────────────────────────────────

def _voyages_dir(state_dir: Path, status: str = "active") -> Path:
    d = state_dir / "voyages" / status
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_voyage(state_dir: Path, voyage: dict, status: str = "active") -> None:
    voyage_id = voyage.get("voyage_id", "unknown")
    path = _voyages_dir(state_dir, status) / f"{voyage_id}.json"
    path.write_text(json.dumps(voyage, indent=2, ensure_ascii=False))


def _load_voyage(state_dir: Path, voyage_id: str) -> dict | None:
    for status in ("active", "completed"):
        path = _voyages_dir(state_dir, status) / f"{voyage_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                pass
    return None


# ── Recall handler ───────────────────────────────────────────────────

def _handle_recall(request: dict, state_dir: Path, write_fn: Callable[[str], None]) -> None:
    """Handle a harbor.recall request from a remote worker."""
    query = request.get("query", "")
    limit = request.get("limit", 10)
    request_id = request.get("request_id", "")
    voyage_id = request.get("voyage_id", "")

    memories = []
    confidence = 0.0
    timing_ms = 0.0

    try:
        from charon.memory.memory_engine import MemoryEngine
        engine = MemoryEngine(state_dir)
        result = engine.recall(query, limit=limit)
        memories = [
            {"content": m.memory.content, "score": m.score, "source": m.source, "category": m.memory.category}
            for m in result.memories
        ]
        confidence = result.confidence
        timing_ms = result.timing_ms
    except Exception as e:
        memories = [{"content": f"Recall error: {e}", "score": 0, "source": "error", "category": "error"}]

    response = json.dumps({
        "type": "harbor.recall_result",
        "request_id": request_id,
        "voyage_id": voyage_id,
        "memories": memories,
        "confidence": confidence,
        "timing_ms": timing_ms,
    }, separators=(",", ":"))
    write_fn(response + "\n")


# ── Result ingestion ─────────────────────────────────────────────────

def _ingest_result(voyage: dict, result_msg: dict, state_dir: Path) -> None:
    """Process a completed voyage result — index memories, update state."""
    voyage_id = result_msg.get("voyage_id", "")
    result = result_msg.get("result", {})
    server_id = voyage.get("manifest", {}).get("server_id", "unknown")
    agent_name = voyage.get("manifest", {}).get("agent_name", "unknown")
    container_tag = f"remote:{server_id}:{agent_name}"

    # Index any memories the worker discovered
    memories_msg = voyage.get("_pending_memories", [])
    if memories_msg:
        try:
            from charon.memory.memory_engine import MemoryEngine
            engine = MemoryEngine(state_dir)
            for mem in memories_msg:
                engine.add(
                    content=mem.get("content", ""),
                    category=mem.get("category", "general"),
                    tier=mem.get("tier", "project"),
                    container_tag=container_tag,
                    source_agent=container_tag,
                )
        except Exception:
            pass

    # Update working memory for this remote agent
    try:
        from charon.fleet.fleet_memory import _update_working_memory
        summary = result.get("stdout", "")[:2000]
        if summary:
            _update_working_memory(state_dir, server_id, agent_name, summary)
    except Exception:
        pass

    # Move voyage to completed
    voyage["status"] = result_msg.get("status", "completed")
    voyage["result"] = result
    voyage["completed_at"] = result_msg.get("completed_at", _now())
    _save_voyage(state_dir, voyage, "completed")

    # Remove from active
    active_path = _voyages_dir(state_dir, "active") / f"{voyage_id}.json"
    try:
        active_path.unlink(missing_ok=True)
    except Exception:
        pass


# ── Dispatch ─────────────────────────────────────────────────────────

def dispatch_voyage(
    instruction: str,
    server_id: str,
    agent_name: str,
    project_root: Path,
    state_dir: Path,
    timeout: int = 1800,
    agent_type: str = "bare",
    on_status: Callable[[str], None] | None = None,
) -> str:
    """Dispatch a structured task to a remote worker. Returns voyage_id.

    Opens an SSH connection to the remote harbor worker, sends the dispatch,
    and starts a background thread to handle the response stream.
    """
    emit = on_status or (lambda msg: None)
    voyage_id = _voyage_id()

    # Load fleet config for the server
    try:
        from charon.fleet.fleet_registry import load_fleet
        fleet = load_fleet()
    except Exception:
        fleet = {"servers": []}

    server = None
    for s in fleet.get("servers", []):
        if s.get("id") == server_id:
            server = s
            break

    if not server:
        emit(f"Unknown server: {server_id}")
        return ""

    # Build manifest
    emit("Building voyage manifest...")
    manifest = _build_manifest(
        voyage_id, instruction, server_id, agent_name,
        project_root, state_dir, timeout, agent_type,
    )

    # Save initial voyage state
    voyage = {
        "voyage_id": voyage_id,
        "status": "dispatching",
        "manifest": manifest,
        "dispatched_at": _now(),
        "progress": [],
        "_pending_memories": [],
    }
    _save_voyage(state_dir, voyage, "active")

    # Open SSH connection
    ssh_cmd = _build_harbor_ssh_command(server)
    emit(f"Connecting to {server_id}...")

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        emit(f"SSH connection failed: {e}")
        voyage["status"] = "failed"
        voyage["error"] = str(e)
        _save_voyage(state_dir, voyage, "completed")
        return ""

    # Send dispatch
    dispatch_msg = json.dumps({
        "type": "harbor.dispatch",
        "voyage_id": voyage_id,
        "manifest": manifest,
    }, separators=(",", ":"))

    try:
        proc.stdin.write(dispatch_msg + "\n")
        proc.stdin.flush()
    except Exception as e:
        emit(f"Failed to send dispatch: {e}")
        proc.kill()
        return ""

    emit(f"Voyage {voyage_id} dispatched to {server_id}:{agent_name}")

    # Background reader thread
    def _reader():
        try:
            while proc.stdout:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "harbor.ready":
                    pass  # Worker is ready

                elif msg_type == "harbor.ack":
                    voyage["status"] = msg.get("status", "started")
                    voyage["worker_id"] = msg.get("worker_id", "")
                    _save_voyage(state_dir, voyage, "active")
                    emit(f"Worker accepted: {msg.get('worker_id', '?')}")

                elif msg_type == "harbor.progress":
                    voyage["progress"].append({
                        "step": msg.get("step", ""),
                        "summary": msg.get("summary", ""),
                        "ts": _now(),
                    })
                    _save_voyage(state_dir, voyage, "active")
                    step = msg.get("step", "")
                    summary = msg.get("summary", "")
                    emit(f"[{voyage_id}] {step}: {summary}")

                elif msg_type == "harbor.recall":
                    _handle_recall(msg, state_dir, lambda data: (proc.stdin.write(data), proc.stdin.flush()))

                elif msg_type == "harbor.memories":
                    voyage["_pending_memories"].extend(msg.get("memories", []))

                elif msg_type == "harbor.result":
                    _ingest_result(voyage, msg, state_dir)
                    status = msg.get("status", "completed")
                    emit(f"Voyage {voyage_id} {status}")

                elif msg_type == "harbor.ping":
                    try:
                        proc.stdin.write(json.dumps({"type": "harbor.pong"}, separators=(",", ":")) + "\n")
                        proc.stdin.flush()
                    except Exception:
                        pass

        except Exception as e:
            emit(f"Voyage {voyage_id} reader error: {e}")
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    thread = threading.Thread(target=_reader, daemon=True, name=f"harbor-{voyage_id}")
    thread.start()

    return voyage_id


# ── Query functions ──────────────────────────────────────────────────

def get_voyage_status(voyage_id: str, state_dir: Path) -> dict | None:
    """Get current status of a voyage."""
    return _load_voyage(state_dir, voyage_id)


def list_voyages(state_dir: Path, limit: int = 20) -> list[dict]:
    """List recent voyages (active first, then completed)."""
    voyages = []

    for status in ("active", "completed"):
        voyage_dir = _voyages_dir(state_dir, status)
        for f in sorted(voyage_dir.glob("v-*.json"), reverse=True):
            if len(voyages) >= limit:
                break
            try:
                v = json.loads(f.read_text())
                voyages.append({
                    "voyage_id": v.get("voyage_id", f.stem),
                    "status": v.get("status", status),
                    "instruction": v.get("manifest", {}).get("instruction", "")[:80],
                    "server": v.get("manifest", {}).get("server_id", "?"),
                    "agent": v.get("manifest", {}).get("agent_name", "?"),
                    "dispatched_at": v.get("dispatched_at", ""),
                    "completed_at": v.get("completed_at", ""),
                })
            except Exception:
                pass

    return voyages[:limit]
