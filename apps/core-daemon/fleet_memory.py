"""Fleet memory — periodically summarize remote agent activity and store in memory."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from fleet_registry import load_fleet
from fleet_sync import get_cached_fleet_status, get_remote_agent_history

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.charon_state'

SUMMARIZE_INTERVAL = 60.0  # seconds between summaries
_stop_event = threading.Event()
_memory_thread: threading.Thread | None = None

# Track what we've already summarized to avoid repeats
_last_output_hash: dict[str, str] = {}


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)


def _hash_output(text: str) -> str:
    """Simple hash to detect if output has changed."""
    import hashlib
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()


def _summarize_output(output: str, agent_name: str, server_id: str) -> str | None:
    """Use LLM to summarize agent activity, or fall back to truncated output."""
    clean = _strip_ansi(output).strip()
    if not clean or len(clean) < 20:
        return None

    # Truncate to last ~2000 chars for summarization
    if len(clean) > 2000:
        clean = clean[-2000:]

    # Try LLM summarization
    try:
        from llm_adapter import quick_completion
        prompt = (
            f"Summarize what the agent '{agent_name}' on server '{server_id}' has been doing "
            f"based on this terminal output. Be concise (1-2 sentences):\n\n{clean}"
        )
        summary = quick_completion(prompt, max_tokens=150)
        if summary and len(summary.strip()) > 10:
            return summary.strip()
    except Exception:
        pass

    # Fallback: extract last meaningful lines
    lines = [l.strip() for l in clean.splitlines() if l.strip()]
    if lines:
        return f"Recent output from {agent_name}: {' | '.join(lines[-3:])}"
    return None


def _update_working_memory(server_id: str, agent_name: str, summary: str) -> None:
    """Update per-agent working memory with latest activity summary."""
    agent_dir = STATE_DIR / 'agents' / f'remote:{server_id}:{agent_name}'
    agent_dir.mkdir(parents=True, exist_ok=True)
    memory_path = agent_dir / 'working_memory.json'

    try:
        memory = json.loads(memory_path.read_text()) if memory_path.exists() else {}
    except Exception:
        memory = {}

    memory['agent_id'] = f'remote:{server_id}:{agent_name}'
    memory['last_task_summary'] = summary
    memory['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # Maintain a rolling list of recent notes
    notes = memory.get('notes', [])
    notes.append(f"[{memory['updated_at']}] {summary}")
    if len(notes) > 20:
        notes = notes[-20:]
    memory['notes'] = notes

    tmp = memory_path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(memory, indent=2))
    tmp.replace(memory_path)


def _store_in_memory_engine(server_id: str, agent_name: str, summary: str) -> None:
    """Store activity summary in the semantic memory engine."""
    try:
        from memory_engine import MemoryEngine
        engine = MemoryEngine(STATE_DIR / 'memory.db')
        engine.add(
            summary,
            category='fleet_activity',
            tier='project',
            container_tag=f'remote:{server_id}:{agent_name}',
            source_agent=f'remote:{server_id}:{agent_name}',
        )
    except Exception:
        pass


def _log_to_task_ledger(server_id: str, agent_name: str, summary: str) -> None:
    """Log activity to the task ledger."""
    try:
        from task_ledger import append_ledger
        append_ledger(STATE_DIR, f'remote:{server_id}:{agent_name}', {
            'event': 'remote_activity',
            'summary': summary,
            'server': server_id,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        })
    except Exception:
        pass


def _summarize_all() -> None:
    """Capture and summarize output from all remote agents."""
    fleet = load_fleet()
    status = get_cached_fleet_status()

    for server in fleet.get('servers', []):
        server_id = server.get('id', server.get('host', ''))
        server_info = status.get(server_id, {})
        if not server_info.get('online'):
            continue

        sessions = server_info.get('sessions', {})
        for agent_cfg in server.get('agents', []):
            agent_name = agent_cfg.get('name', '')
            sess = sessions.get(agent_name, {})
            if sess.get('status') not in ('running', 'idle'):
                continue

            # Get recent output
            try:
                output = get_remote_agent_history(server_id, agent_name, timeout=3.0)
            except Exception:
                continue

            if not output:
                continue

            # Check if output has changed since last summary
            key = f'{server_id}:{agent_name}'
            output_hash = _hash_output(output)
            if _last_output_hash.get(key) == output_hash:
                continue
            _last_output_hash[key] = output_hash

            # Summarize and store
            summary = _summarize_output(output, agent_name, server_id)
            if not summary:
                continue

            _update_working_memory(server_id, agent_name, summary)
            _store_in_memory_engine(server_id, agent_name, summary)
            _log_to_task_ledger(server_id, agent_name, summary)


def _memory_loop() -> None:
    """Background thread that periodically summarizes remote agent activity."""
    # Initial delay to let fleet sync establish connections first
    _stop_event.wait(10.0)
    while not _stop_event.is_set():
        try:
            _summarize_all()
        except Exception:
            pass
        _stop_event.wait(SUMMARIZE_INTERVAL)


def start_fleet_memory() -> None:
    """Start the background fleet memory thread (idempotent)."""
    global _memory_thread
    if _memory_thread is not None and _memory_thread.is_alive():
        return
    _stop_event.clear()
    _memory_thread = threading.Thread(target=_memory_loop, daemon=True, name='fleet-memory')
    _memory_thread.start()


def stop_fleet_memory() -> None:
    """Stop the background fleet memory thread."""
    _stop_event.set()
