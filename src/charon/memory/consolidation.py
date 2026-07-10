"""User model consolidation — background process that learns from interactions.

Reads recent agent interactions, extracts signals (preferences, corrections,
patterns), and updates the structured user model. Runs periodically but ONLY
when there's fresh user signal since the last consolidation.

Scan traces are stored in SQLite for dashboard inspection.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from charon.infra import config as env_config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Configuration ───────────────────────────────────────────────────

DEFAULT_CONFIG = {
    'enabled': True,
    'model_tier': 'fast',              # 'fast', 'strong', or specific model id
    'scan_interval_heartbeats': 50,    # check every ~100 minutes
    'min_new_user_messages': 5,        # trigger after this many new user messages
    'max_events_per_scan': 120,        # max messages to read per scan
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
    model_override = env_config.consolidation_model()
    if model_override:
        config['model_tier'] = model_override
    interval_override = env_config.consolidation_interval()
    if interval_override is not None:
        config['scan_interval_heartbeats'] = interval_override
    if env_config.consolidation_disabled():
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
        from charon.infra.store_adapter import get_db
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
        from charon.infra.store_adapter import get_db
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

def _ensure_conversation_messages(db) -> None:
    """Ensure conversation_messages table exists (may live in context_store schema)."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    TEXT NOT NULL DEFAULT '',
            seq         INTEGER NOT NULL DEFAULT 0,
            role        TEXT NOT NULL,
            content     TEXT,
            tool_calls  TEXT,
            tool_call_id TEXT,
            tool_name   TEXT,
            is_error    INTEGER NOT NULL DEFAULT 0,
            thinking    TEXT,
            token_count INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    db.commit()


def _count_user_messages_since(state_dir: Path, since_ts: str) -> int:
    """Count user messages in conversation_messages since a timestamp."""
    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)
        _ensure_conversation_messages(db)
        row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM conversation_messages "
            "WHERE role='user' AND created_at > ?",
            (since_ts,),
        )
        return row['cnt'] if row else 0
    except Exception:
        return 0


def should_run(state_dir: Path, config: dict) -> bool:
    """Check if consolidation should run.

    Triggers after MIN_NEW_USER_MESSAGES new user messages since last run.
    Zero cost when user is away.
    """
    if not config.get('enabled', True):
        return False

    from charon.memory.user_model_structured import load_structured
    model = load_structured(state_dir)
    meta = model.get('_meta', {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    last_ts = meta.get('last_consolidated_at', '2000-01-01T00:00:00Z')

    min_msgs = config.get('min_new_user_messages', 5)
    return _count_user_messages_since(state_dir, last_ts) >= min_msgs


# ── Signal extraction ───────────────────────────────────────────────

def _collect_recent_signals(state_dir: Path, since_ts: str, max_events: int = 120) -> tuple[str, list[dict]]:
    """Collect recent conversation signals for the LLM to analyze.

    Reads user messages and short assistant replies from conversation_messages,
    grouped into exchanges. Returns (formatted_text, user_message_refs) where
    user_message_refs maps signal indices to (agent_id, seq, content) for idea linking.
    """
    signals = []
    user_refs: list[dict] = []  # one entry per [user] signal line

    try:
        from charon.infra.store_adapter import get_db
        db = get_db(state_dir)
        _ensure_conversation_messages(db)

        rows = db.fetchall(
            """SELECT id, agent_id, seq, role, content, created_at
               FROM conversation_messages
               WHERE role IN ('user', 'assistant')
                 AND created_at > ?
                 AND content IS NOT NULL
                 AND length(content) > 0
               ORDER BY id ASC
               LIMIT ?""",
            (since_ts, max_events),
        )

        for r in rows:
            role = r['role']
            content = str(r['content'] or '').strip()
            if not content:
                continue

            if role == 'user':
                signals.append(f'[user] {content[:300]}')
                user_refs.append({
                    'signal_index': len(signals) - 1,
                    'agent_id': r['agent_id'] or '',
                    'message_seq': r.get('seq', -1),
                    'content': content,
                })
            elif role == 'assistant':
                first_line = content.split('\n')[0].strip()
                snippet = first_line[:150] if first_line else content[:150]
                if snippet:
                    signals.append(f'[agent] {snippet}')

    except Exception:
        pass

    return ('\n'.join(signals) if signals else '', user_refs)


# ── LLM-based analysis ─────────────────────────────────────────────

CONSOLIDATION_PROMPT = """You are a user model analyst for Charon, a coding agent OS. Analyze recent conversations and extract a rich profile update.

Current user profile:
{current_profile}

Recent conversation (chronological, [user] = user message, [agent] = agent reply snippet):
{signals}

Output a JSON object with ONLY the changes to make. Categories and what to put in each:

{{
  "set": [
    {{
      "category": "style|coding|tooling|workflow|patterns|interests|mental_model",
      "key": "short_snake_case_key",
      "value": "concise value under 80 chars"
    }}
  ],
  "corrections": ["explicit corrections the user stated"],
  "intentions": [
    {{"project": "project_name", "intent": "what they want to achieve", "priority": "high|normal|low"}}
  ],
  "ideas": [
    {{"summary": "concise idea description", "category": "feature|project|improvement|general", "signal_index": 0}}
  ],
  "reasoning": "1-2 sentences on what you observed"
}}

Category guide:
- style: communication preferences (verbosity, tone, format)
- coding: code conventions (naming, error handling, language preferences)
- tooling: tools and stack (languages, frameworks, package managers)
- workflow: how they work (PR size, test habits, iteration style)
- patterns: observed behavioral patterns (how they explore, how they give feedback, pacing)
- interests: topics they return to frequently — map topic→frequency_or_depth description.
  Examples: "agent_architectures: high interest, mentioned in 80% of sessions",
            "memory_systems: deep — asks implementation questions not just overviews",
            "rl_and_finetuning: growing interest, asked about traces and data collection"
- mental_model: how they think about problems — their abstractions, framings, analogies.
  Examples: "systems_thinking: frames features as system properties not isolated capabilities",
            "build_vs_research: leans toward building first, research to validate",
            "skeptical_of_bloat: questions whether new capabilities are truly needed"

Rules:
- interests and mental_model are the most valuable — fill them from topic frequency and reasoning patterns.
- Only set fields with CLEAR evidence. Don't guess.
- Corrections = things user explicitly pushed back on or corrected.
- ideas = feature suggestions, project ideas, or improvements the user proposed. signal_index = the 0-based index of the user message in the signals that triggered the idea. Only capture genuine proposals, not passing references.
- Keep all values under 100 chars.
- Update existing keys rather than adding duplicates — if a key already exists in the profile, improve it.
- If nothing meaningful: {{"set": [], "corrections": [], "intentions": [], "ideas": [], "reasoning": "No actionable signals."}}

Output ONLY valid JSON, nothing else."""


async def _run_analysis(state_dir: Path, signals_text: str, current_profile: str, model_tier: str) -> dict:
    """Run the LLM analysis to extract user model updates."""
    from charon.providers.provider_bridge import create_provider_and_model

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

def _apply_changes(model: dict, analysis: dict, state_dir: Path | None = None, user_refs: list[dict] | None = None) -> list[dict]:
    """Apply extracted changes to the user model. Returns list of change records."""
    from charon.memory.user_model_structured import set_field, add_correction, set_intention
    from charon.tools.memory_tools import _scan_content

    changes = []

    for entry in (analysis.get('set') or []):
        cat = entry.get('category', '')
        key = entry.get('key', '')
        value = entry.get('value', '')
        if cat and key and value:
            scan = _scan_content(value)
            if scan:
                continue  # Skip injected content
            if cat in ('style', 'coding', 'tooling', 'workflow', 'patterns', 'interests', 'mental_model'):
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

    for idea_entry in (analysis.get('ideas') or []):
        if isinstance(idea_entry, dict):
            summary = idea_entry.get('summary', '')
            category = idea_entry.get('category', 'general')
            sig_idx = idea_entry.get('signal_index', -1)
            if not summary:
                continue
            scan = _scan_content(summary)
            if scan:
                continue
            # Check for near-duplicates in existing ideas
            existing_ideas = model.get('ideas', [])
            if isinstance(existing_ideas, list) and any(
                isinstance(e, dict) and e.get('summary', '').lower() == summary.lower()
                for e in existing_ideas
            ):
                continue
            # Resolve signal_index to session_id + message_seq
            session_id = ''
            message_seq = -1
            message_text = ''
            if user_refs and isinstance(sig_idx, int) and 0 <= sig_idx < len(user_refs):
                ref = user_refs[sig_idx]
                session_id = ref.get('agent_id', '')
                message_seq = ref.get('message_seq', -1)
                message_text = ref.get('content', '')[:500]
            if state_dir:
                from charon.memory.user_model_structured import record_idea
                record_idea(
                    state_dir,
                    summary=summary,
                    session_id=session_id,
                    message_seq=message_seq,
                    message_text=message_text,
                    category=category,
                    source='auto',
                )
            changes.append({
                'type': 'idea',
                'summary': summary,
                'category': category,
                'session_id': session_id,
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
        from charon.memory.user_model_structured import load_structured, save_structured, render_for_prompt, total_chars, CHAR_LIMIT

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
        signals_text, user_refs = _collect_recent_signals(state_dir, last_ts, max_events)
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
        changes = _apply_changes(model, analysis, state_dir=state_dir, user_refs=user_refs)
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
