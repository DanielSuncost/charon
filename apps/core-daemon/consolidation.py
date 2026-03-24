"""User model consolidation — background process that learns from interactions.

Reads recent agent interactions, extracts signals (preferences, corrections,
patterns), and updates the structured user model. Runs periodically but ONLY
when there's fresh user signal since the last consolidation.

Scan traces are stored in SQLite for dashboard inspection.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Configuration ───────────────────────────────────────────────────

DEFAULT_CONFIG = {
    'enabled': True,
    'model_tier': 'fast',           # 'fast', 'strong', or specific model id
    'scan_interval_heartbeats': 50,  # check every ~100 minutes
    'scan_after_n_events': 10,       # also check after every N user events
    'max_events_per_scan': 100,      # don't process more than this per scan
}


def load_config(state_dir: Path) -> dict:
    """Load consolidation config, merging user overrides with defaults."""
    config = dict(DEFAULT_CONFIG)
    try:
        cfg_path = state_dir / 'consolidation_config.json'
        if cfg_path.exists():
            user_cfg = json.loads(cfg_path.read_text())
            if isinstance(user_cfg, dict):
                config.update(user_cfg)
    except Exception:
        pass
    # Also check env vars
    if os.environ.get('CHARON_CONSOLIDATION_MODEL'):
        config['model_tier'] = os.environ['CHARON_CONSOLIDATION_MODEL']
    if os.environ.get('CHARON_CONSOLIDATION_INTERVAL'):
        config['scan_interval_heartbeats'] = int(os.environ['CHARON_CONSOLIDATION_INTERVAL'])
    if os.environ.get('CHARON_CONSOLIDATION_ENABLED', '').lower() == 'false':
        config['enabled'] = False
    return config


def save_config(state_dir: Path, config: dict) -> None:
    """Save consolidation config (user overrides only)."""
    cfg_path = state_dir / 'consolidation_config.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config, indent=2))


# ── Scan traces (stored in SQLite for dashboard) ───────────────────

def _ensure_traces_table(db) -> None:
    """Create the consolidation_traces table if it doesn't exist."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS consolidation_traces (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            events_processed INTEGER NOT NULL DEFAULT 0,
            changes     TEXT NOT NULL DEFAULT '[]',
            model_used  TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            error       TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ct_ts ON consolidation_traces(ts)
    """)
    db.commit()


def save_trace(state_dir: Path, trace: dict) -> None:
    """Save a consolidation scan trace."""
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        _ensure_traces_table(db)
        db.execute(
            """INSERT INTO consolidation_traces (ts, events_processed, changes, model_used, duration_ms, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                trace.get('ts', _now()),
                trace.get('events_processed', 0),
                json.dumps(trace.get('changes', []), ensure_ascii=False),
                trace.get('model_used', ''),
                trace.get('duration_ms', 0),
                trace.get('error'),
            ),
        )
        db.commit()
    except Exception:
        pass


def list_traces(state_dir: Path, limit: int = 20) -> list[dict]:
    """List recent consolidation traces for dashboard display."""
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        _ensure_traces_table(db)
        rows = db.fetchall(
            "SELECT * FROM consolidation_traces ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            try:
                r['changes'] = json.loads(r.get('changes', '[]'))
            except Exception:
                r['changes'] = []
        rows.reverse()
        return rows
    except Exception:
        return []


# ── Trigger check ───────────────────────────────────────────────────

def _count_user_events_since(state_dir: Path, since_ts: str) -> int:
    """Count user-origin events (messages, steers, ideas, commands) since a timestamp."""
    count = 0
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        # Count from agent_inbox: user-origin events
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM agent_inbox WHERE ts > ? AND event_type IN "
            "('task_received', 'user_intent', 'steer', 'follow_up', 'idea_captured')",
            (since_ts,),
        )
        count += (row['cnt'] if row else 0)
    except Exception:
        pass

    # Also count from run_log for broader coverage
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM run_log WHERE ts > ? AND event IN "
            "('task_start', 'task_success', 'task_failed_terminal')",
            (since_ts,),
        )
        count += (row['cnt'] if row else 0)
    except Exception:
        pass

    return count


def should_run(state_dir: Path, config: dict) -> bool:
    """Check if consolidation should run based on the trigger rule.

    Only runs when there's fresh user signal:
    - At least 1 new user-origin event since last consolidation, OR
    - At least 3 new task completions since last consolidation

    Returns False if no new signal. Zero cost when user is away.
    """
    if not config.get('enabled', True):
        return False

    # Get last consolidation timestamp
    from user_model_structured import load_structured
    model = load_structured(state_dir)
    meta = model.get('_meta', {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    last_ts = meta.get('last_consolidated_at', '2000-01-01T00:00:00Z')

    event_count = _count_user_events_since(state_dir, last_ts)
    return event_count >= 1


# ── Signal extraction ───────────────────────────────────────────────

def _collect_recent_signals(state_dir: Path, since_ts: str, max_events: int = 100) -> str:
    """Collect recent interaction signals as text for the LLM to analyze."""
    signals = []

    # Recent inbox events across all agents
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        rows = db.fetchall(
            "SELECT agent_id, ts, event_type, payload FROM agent_inbox "
            "WHERE ts > ? ORDER BY ts DESC LIMIT ?",
            (since_ts, max_events),
        )
        for r in rows:
            payload = r.get('payload', {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            evt = r.get('event_type', '')
            if evt == 'task_received':
                instruction = payload.get('instruction', '')
                if instruction:
                    signals.append(f'[task] {instruction[:200]}')
            elif evt == 'task_succeeded':
                summary = payload.get('summary', '')
                if summary:
                    signals.append(f'[completed] {summary[:200]}')
            elif evt == 'task_failed':
                error = payload.get('error', '')
                if error:
                    signals.append(f'[failed] {error[:200]}')
    except Exception:
        pass

    # Recent run log events
    try:
        from store_adapter import get_db
        db = get_db(state_dir)
        rows = db.fetchall(
            "SELECT ts, event, data FROM run_log WHERE ts > ? ORDER BY ts DESC LIMIT ?",
            (since_ts, 50),
        )
        for r in rows:
            data = r.get('data', {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    data = {}
            evt = r.get('event', '')
            if evt in ('task_start', 'task_success'):
                title = data.get('title', '')
                if title:
                    signals.append(f'[{evt}] {title[:150]}')
    except Exception:
        pass

    signals.reverse()  # chronological order
    return '\n'.join(signals) if signals else ''


# ── LLM-based analysis ─────────────────────────────────────────────

CONSOLIDATION_PROMPT = """You are analyzing recent interactions between a user and their Charon coding agents. Your job is to extract user preferences, corrections, and patterns to update their profile.

Current user profile:
{current_profile}

Recent interaction signals (chronological):
{signals}

Based on these signals, output a JSON object with ONLY the changes to make. Use these keys:

{{
  "set": [
    {{"category": "style|coding|tooling|workflow|patterns", "key": "field_name", "value": "value"}}
  ],
  "corrections": ["any explicit corrections the user made"],
  "intentions": [
    {{"project": "name", "intent": "what they want", "priority": "high|normal|low"}}
  ],
  "reasoning": "brief explanation of what you observed"
}}

Rules:
- Only include fields that have CLEAR evidence in the signals. Don't guess.
- Corrections are things the user EXPLICITLY said "no, do it this way."
- Patterns are OBSERVED behaviors, not stated preferences.
- If there's nothing meaningful to extract, return {{"set": [], "corrections": [], "intentions": [], "reasoning": "No actionable signals found."}}
- Keep values concise (under 60 chars each).

Output ONLY the JSON object, nothing else."""


async def _run_analysis(state_dir: Path, signals_text: str, current_profile: str, model_tier: str) -> dict:
    """Run the LLM analysis to extract user model updates."""
    from provider_bridge import create_provider_and_model
    from providers import ModelInfo

    provider, model, ready = create_provider_and_model(state_dir)
    if not ready:
        return {'error': 'No provider configured'}

    # If a specific model is requested and different from default, we'd
    # need model routing here. For now, use whatever's configured.
    prompt = CONSOLIDATION_PROMPT.format(
        current_profile=current_profile,
        signals=signals_text,
    )

    text_parts = []
    try:
        async for delta in provider.stream(
            messages=[{
                'role': 'user',
                'content': prompt,
            }],
            model=model,
            system_prompt='You are a user profile analyst. Output only valid JSON.',
            max_tokens=2048,
        ):
            if hasattr(delta, 'type'):
                if delta.type == 'text':
                    text_parts.append(delta.text)
                elif delta.type == 'error':
                    return {'error': delta.error or 'LLM error'}
    except Exception as e:
        return {'error': str(e)}

    response = ''.join(text_parts).strip()

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if '```' in response:
            import re
            match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
            if match:
                response = match.group(1).strip()
        result = json.loads(response)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    return {'error': f'Failed to parse LLM response: {response[:200]}'}


# ── Apply changes ───────────────────────────────────────────────────

def _apply_changes(model: dict, analysis: dict) -> list[dict]:
    """Apply extracted changes to the user model. Returns list of change records."""
    from user_model_structured import set_field, add_correction, set_intention
    from tools.memory_tools import _scan_content

    changes = []

    for entry in (analysis.get('set') or []):
        cat = entry.get('category', '')
        key = entry.get('key', '')
        value = entry.get('value', '')
        if cat and key and value:
            scan = _scan_content(value)
            if scan:
                continue  # Skip injected content
            if cat in ('style', 'coding', 'tooling', 'workflow', 'patterns'):
                old_value = (model.get(cat) or {}).get(key)
                set_field(model, cat, key, value)
                changes.append({
                    'type': 'set',
                    'category': cat,
                    'key': key,
                    'value': value,
                    'old_value': old_value,
                })

    for correction in (analysis.get('corrections') or []):
        if isinstance(correction, str) and correction.strip():
            scan = _scan_content(correction)
            if scan:
                continue
            # Check for near-duplicates
            existing = model.get('corrections', [])
            if correction not in existing:
                add_correction(model, correction)
                changes.append({'type': 'correction', 'content': correction})

    for intention in (analysis.get('intentions') or []):
        if isinstance(intention, dict):
            proj = intention.get('project', '')
            intent = intention.get('intent', '')
            priority = intention.get('priority', 'normal')
            if proj and intent:
                scan = _scan_content(intent)
                if scan:
                    continue
                set_intention(model, proj, intent, priority)
                changes.append({
                    'type': 'intention',
                    'project': proj,
                    'intent': intent,
                    'priority': priority,
                })

    return changes


# ── Main entry point ────────────────────────────────────────────────

def run_consolidation(state_dir: Path, config: dict | None = None) -> dict:
    """Run a single consolidation scan. Returns a trace dict.

    This is synchronous — wraps the async LLM call.
    Call this from the daemon loop on heartbeat.
    """
    config = config or load_config(state_dir)
    start_time = time.time()
    trace = {
        'ts': _now(),
        'events_processed': 0,
        'changes': [],
        'model_used': config.get('model_tier', 'fast'),
        'duration_ms': 0,
        'error': None,
    }

    try:
        from user_model_structured import load_structured, save_structured, render_for_prompt, total_chars, CHAR_LIMIT

        model = load_structured(state_dir)
        meta = model.get('_meta', {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        last_ts = meta.get('last_consolidated_at', '2000-01-01T00:00:00Z')

        # Collect signals
        max_events = config.get('max_events_per_scan', 100)
        signals_text = _collect_recent_signals(state_dir, last_ts, max_events)
        if not signals_text:
            trace['error'] = 'No signals to process'
            trace['duration_ms'] = int((time.time() - start_time) * 1000)
            save_trace(state_dir, trace)
            # Still update timestamp so we don't re-check immediately
            meta['last_consolidated_at'] = _now()
            model['_meta'] = meta
            save_structured(state_dir, model)
            return trace

        signal_count = len(signals_text.splitlines())
        trace['events_processed'] = signal_count

        current_profile = render_for_prompt(model)

        # Run LLM analysis
        analysis = asyncio.run(_run_analysis(
            state_dir, signals_text, current_profile, config.get('model_tier', 'fast'),
        ))

        if analysis.get('error'):
            trace['error'] = analysis['error']
            trace['duration_ms'] = int((time.time() - start_time) * 1000)
            save_trace(state_dir, trace)
            return trace

        # Apply changes
        changes = _apply_changes(model, analysis)
        trace['changes'] = changes
        trace['reasoning'] = analysis.get('reasoning', '')

        # Budget check — revert if over limit
        if total_chars(model) > CHAR_LIMIT:
            trace['error'] = f'Changes would exceed {CHAR_LIMIT} char limit, reverted'
            trace['duration_ms'] = int((time.time() - start_time) * 1000)
            save_trace(state_dir, trace)
            return trace

        # Update metadata
        meta['last_consolidated_at'] = _now()
        meta['consolidation_count'] = (meta.get('consolidation_count') or 0) + 1
        meta['schema_version'] = '2.0'
        model['_meta'] = meta

        # Save
        save_structured(state_dir, model)

    except Exception as e:
        trace['error'] = str(e)

    trace['duration_ms'] = int((time.time() - start_time) * 1000)
    save_trace(state_dir, trace)
    return trace
