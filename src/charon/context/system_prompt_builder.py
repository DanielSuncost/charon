"""Charon system prompt builder — assembles the layered prompt for agents.

Layers (in order):
1. Agent identity (name, role, project, goal)
2. User model (frozen snapshot, shared across all agents)
3. Project knowledge (frozen snapshot, shared per-project)
4. Working memory (last N task summaries, private)
5. Goal context (objectives, active/blocked goals)
6. Coordination awareness (other agents, pending boundaries)
7. Shade contract (constraints, scope, phase objective — shades only)
8. Tools + guidelines
9. Context files (AGENTS.md, CLAUDE.md, CHARON.md)
10. Date + CWD

Each layer is skipped when empty. The prompt grows only as state exists.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from charon.tools import ALL_TOOL_DEFS

# ── Injection scanning (Hermes-style) ──────────────────────────────────

_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', 'prompt_injection'),
    (r'do\s+not\s+tell\s+the\s+user', 'deception_hide'),
    (r'system\s+prompt\s+override', 'sys_prompt_override'),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', 'disregard_rules'),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', 'html_comment_injection'),
]

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

_CONTEXT_FILE_MAX_CHARS = 8000


def _scan_content(content: str, filename: str) -> str:
    """Scan content for injection. Returns sanitized content or blocked message."""
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f'[BLOCKED: {filename} contained invisible unicode (possible injection)]'
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f'[BLOCKED: {filename} matched threat pattern {pid}]'
    return content


def _truncate_content(content: str, max_chars: int = _CONTEXT_FILE_MAX_CHARS) -> str:
    """Head/tail truncation with marker."""
    if len(content) <= max_chars:
        return content
    head = int(max_chars * 0.7)
    tail = int(max_chars * 0.2)
    return (content[:head]
            + f'\n\n[...truncated: kept {head}+{tail} of {len(content)} chars]\n\n'
            + content[-tail:])


# ── Layer builders ──────────────────────────────────────────────────────

def _build_identity(agent: dict, task: dict) -> str:
    """Layer 1: Agent identity."""
    name = agent.get('name') or 'Charon'
    agent_id = agent.get('id') or ''
    role = agent.get('specialization') or agent.get('role') or 'generalist'
    goal = agent.get('goal') or ''
    project = task.get('project') or agent.get('project') or ''
    project_name = Path(project).name if project else ''

    lines = [f'You are {name}, a persistent Charon agent.']
    if role and role not in ('charon', 'generalist'):
        lines.append(f'Role: {role}')
    if agent_id:
        lines.append(f'Agent ID: {agent_id}')
    if project_name:
        lines.append(f'Project: {project_name} ({project})')
    if goal:
        lines.append(f'Goal: {goal}')

    charter = (agent.get('charter') or '').strip()
    if charter:
        lines.append('')
        lines.append('# Role charter')
        lines.append(
            'This is your standing charter as a long-lived specialist. It holds across '
            'every task and session; task instructions refine it but do not replace it.'
        )
        lines.append(charter)

    lines.append('')
    lines.append(
        'You are part of Charon, a single-user agent operating system for software development. '
        'Charon runs persistent coding agents across multiple projects, coordinating them from one terminal.'
    )
    lines.append('')
    lines.append(
        'Key concepts you should know:\n'
        '- You are a **Charon agent** (persistent, named, project-assigned). You have durable memory across tasks.\n'
        '- **Shades** are ephemeral worker agents that you can spawn for complex tasks. They execute phases of a contract '
        '(analysis → implementation → verification → report) with scope restrictions and budget limits. '
        'Users never interact with shades directly — they talk to you, you manage shades internally.\n'
        '- You share a **user model** with all other Charon agents — preferences the user has taught any agent are available to you.\n'
        '- You share **project knowledge** with other agents on the same project.\n'
        '- You can see what other agents are working on and coordinate to avoid conflicts.\n'
        '- You can capture ideas with /idea for the backlog, and query goals with /goals.'
        '- Use `/browser show [--save]`, `/browser hide [--save]`, or `/browser status` to control browser visibility.'
    )
    lines.append('')
    lines.append(
        'Be direct, concise, and focused on your assigned work. '
        'When the user corrects you or expresses a preference, remember it — it will persist across all sessions.'
    )
    lines.append('')
    lines.append(
        '**Memory:** Your system prompt includes a snapshot of what you know (user profile, project knowledge, '
        'recent working memory). But you also have a **Recall** tool for searching all past conversations '
        'semantically. Use it when:\n'
        '- The user references a past conversation ("remember when we...")\n'
        '- You need details not in your current context\n'
        '- The user asks about something you should know but don\'t see in your memory snapshot\n'
        'The Recall tool finds memories by meaning, not just keywords — describe what you\'re looking for.'
    )
    lines.append('')
    lines.append(
        '**Important:** Memory snapshots are summaries of prior work, not live conversation turns. '
        'Do not behave as if a previous session is still in progress unless this session\'s actual conversation history shows that. '
        'If the user starts a fresh session with something like "hello", respond normally and do not continue an unfinished reply from memory.'
    )
    return '\n'.join(lines)


def _build_shade_identity(agent: dict, task: dict, contract: dict | None) -> str:
    """Layer 1 (shade variant): Shade identity with contract constraints."""
    shade_id = agent.get('id') or ''
    parent_id = agent.get('parent_agent_id') or ''
    phase = task.get('shade_phase') or {}
    phase_id = phase.get('phase_id') or ''

    lines = [f'You are a Charon shade (ID: {shade_id}), an ephemeral worker agent.']
    if parent_id:
        lines.append(f'Parent agent: {parent_id}')
    lines.append(
        'You are a shade — a temporary, focused worker spawned by a persistent Charon agent '
        'to handle a specific phase of a larger task. You have a contract with an objective, '
        'constraints, scope restrictions, and a budget. Complete your assigned phase, stay '
        'within scope, and report concrete results. You will be terminated when done.'
    )

    if contract:
        lines.append('')
        lines.append('# Shade Contract')
        lines.append(f'Contract: {contract.get("id", "")}')
        if phase_id:
            # Find phase in contract
            for p in (contract.get('phases') or []):
                if p.get('phase_id') == phase_id:
                    lines.append(f'Phase: {phase_id} ({p.get("name", "")})')
                    lines.append(f'Objective: {p.get("objective", "")}')
                    break
        lines.append(f'Goal: {contract.get("goal", "")}')
        constraints = contract.get('constraints') or []
        if constraints:
            lines.append('Constraints:')
            for c in constraints:
                lines.append(f'- {c}')
        expected = contract.get('expected_outputs') or []
        if expected:
            lines.append('Expected outputs:')
            for e in expected:
                lines.append(f'- {e}')
        scope = contract.get('scope') or []
        if scope:
            lines.append(f'Scope (only modify files in): {", ".join(scope)}')

    return '\n'.join(lines)


def _build_user_model(state_dir: Path) -> str:
    """Layer 2: User model (shared, permanent)."""
    try:
        from charon.memory.user_model_structured import load_structured, render_for_prompt
        model = load_structured(state_dir)
        rendered = render_for_prompt(model)
        # Only include if there's actual content (not just the empty placeholder)
        if '(No profile yet' not in rendered:
            return rendered
    except Exception:
        pass
    return ''


def _build_project_knowledge(state_dir: Path, project: str) -> str:
    """Layer 3: Project knowledge (shared, per-project)."""
    if not project:
        return ''

    project_path = Path(project)
    candidates: list[tuple[Path, str]] = []

    try:
        from charon.infra.project_registry import ensure_project
        proj = ensure_project(state_dir, project_path)
        pid = str(proj.get('id') or '').strip()
        if pid:
            candidates.append((state_dir / 'projects' / pid / 'KNOWLEDGE.md', 'KNOWLEDGE.md'))
    except Exception:
        pass

    for name in ['PROJECT_KNOWLEDGE.md', 'project_knowledge.md']:
        candidates.append((project_path / '.charon' / name, name))

    seen: set[str] = set()
    for p, label in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            try:
                content = p.read_text(encoding='utf-8').strip()
                if content:
                    content = _scan_content(content, label)
                    content = _truncate_content(content, 3000)
                    sep = '═' * 46
                    return f'{sep}\nPROJECT KNOWLEDGE\n{sep}\n{content}'
            except Exception:
                pass

    return ''


def _build_working_memory(state_dir: Path, agent_id: str) -> str:
    """Layer 4: Working memory (private, rolling window)."""
    if not agent_id:
        return ''

    memory = None
    # Try SQLite
    try:
        from charon.infra.store_adapter import get_db, agent_memory_get
        db = get_db(state_dir)
        memory = agent_memory_get(db, agent_id)
    except Exception:
        pass

    # Fallback to JSON
    if not memory:
        try:
            mem_path = state_dir / 'agents' / agent_id / 'working_memory.json'
            if mem_path.exists():
                memory = json.loads(mem_path.read_text())
        except Exception:
            pass

    if not memory:
        return ''

    notes = memory.get('notes') or []
    if not notes:
        return ''

    recent = notes[-5:]
    lines = [
        '# Working Memory',
        '(Past task summaries only. These are not live turns from the current conversation; use them as background context, not as something to continue verbatim.)',
    ]
    for n in recent:
        ts = str(n.get('ts', ''))[:16] if n.get('ts') else ''
        summary = str(n.get('summary', '')).strip()
        if summary:
            prefix = f'[{ts}] ' if ts else ''
            lines.append(f'- Past task summary {prefix}{summary[:200]}')

    return '\n'.join(lines) if len(lines) > 2 else ''


def _build_recall_context(state_dir: Path, agent_id: str = '') -> str:
    """Layer 4b: Recent semantic memory context (auto-injected).

    Pulls the user's static profile and recent dynamic context from the
    semantic memory engine. This gives the agent a richer baseline than
    the structured user model alone — it includes things learned from
    past conversations that haven't been curated into the 7-category model.

    Non-breaking: returns empty string if memory engine isn't available.
    """
    try:
        from charon.memory.memory_engine import MemoryEngine
        engine = MemoryEngine(state_dir)
        count = engine.count()
        if count == 0:
            engine.close()
            return ''

        static, dynamic = engine._build_profile(container_tag=agent_id or None)
        engine.close()

        if not static and not dynamic:
            return ''

        lines = ['# Memory Context']
        lines.append('(From past conversations — use the Recall tool to search for specific memories)')
        lines.append('')

        if static:
            lines.append('**Remembered facts:**')
            for fact in static[:10]:
                lines.append(f'- {fact}')

        if dynamic:
            if static:
                lines.append('')
            lines.append('**Recent context:**')
            for fact in dynamic[:8]:
                lines.append(f'- {fact}')

        return '\n'.join(lines)
    except ImportError:
        return ''
    except Exception:
        return ''


def _build_goal_context(state_dir: Path, agent_id: str, project: str) -> str:
    """Layer 5: Goal context from context packets."""
    if not agent_id:
        return ''

    packet = None
    # Try SQLite
    try:
        from charon.infra.store_adapter import get_db, goal_context_packet_get
        db = get_db(state_dir)
        packet = goal_context_packet_get(db, agent_id)
    except Exception:
        pass

    # Fallback to JSON
    if not packet:
        try:
            pkt_path = state_dir / 'context_packets' / f'{agent_id}.json'
            if pkt_path.exists():
                packet = json.loads(pkt_path.read_text())
        except Exception:
            pass

    if not packet:
        return ''

    lines = ['# Goals']

    active = packet.get('active_goals') or []
    if active:
        for g in active[:5]:
            title = g.get('title', '?')[:120]
            tasks = g.get('linked_tasks') or []
            task_info = f' ({len(tasks)} tasks)' if tasks else ''
            lines.append(f'- Active: {title}{task_info}')

    blocked = packet.get('blocked_goals') or []
    if blocked:
        for g in blocked[:3]:
            lines.append(f'- Blocked: {g.get("title", "?")[:120]}')

    recent = packet.get('recent_goal_updates') or []
    completed = [g for g in recent if g.get('status') == 'completed']
    if completed:
        lines.append(f'- Recently completed: {len(completed)} goals')

    return '\n'.join(lines) if len(lines) > 1 else ''


def _build_coordination(state_dir: Path, agent_id: str) -> str:
    """Layer 6: Other agents and pending boundaries."""
    lines = []

    # List other agents
    try:
        from charon.infra.store_adapter import get_db, agent_list
        db = get_db(state_dir)
        agents = agent_list(db)
        others = [a for a in agents
                  if a.get('id') != agent_id
                  and a.get('status') == 'running'
                  and a.get('role') != 'shade']
        if others:
            lines.append('# Active Agents')
            for a in others[:8]:
                spec = a.get('specialization') or a.get('role') or ''
                spec_str = f' ({spec})' if spec and spec != 'charon' else ''
                goal = a.get('goal') or ''
                goal_str = f' — {goal[:60]}' if goal else ''
                lines.append(f'- {a.get("name", a.get("id", "?"))}{spec_str}{goal_str}')
            if agent_id:
                lines.append(f'[You are {agent_id}]')
    except Exception:
        pass

    # Pending boundary proposals
    try:
        from charon.infra.store_adapter import get_db, boundary_pending_for_agent
        db = get_db(state_dir)
        pending = boundary_pending_for_agent(db, agent_id) if agent_id else []
        if pending:
            lines.append('')
            lines.append('# Pending Coordination')
            for b in pending[:3]:
                proposer = b.get('proposer_agent_id', '?')
                scope = b.get('scope') or []
                reason = b.get('reason', '')[:80]
                lines.append(f'- Boundary proposal from {proposer}: {reason}')
                if scope:
                    lines.append(f'  Scope: {", ".join(scope[:5])}')
    except Exception:
        pass

    return '\n'.join(lines) if lines else ''


def _build_fleet_context() -> str:
    """Layer 7b: Remote fleet status — gives the LLM awareness of remote agents."""
    try:
        from charon.fleet.fleet_registry import load_fleet
        from charon.fleet.fleet_sync import get_cached_fleet_status
    except ImportError:
        return ''

    fleet = load_fleet()
    servers = fleet.get('servers', [])
    if not servers:
        return ''

    status = get_cached_fleet_status()
    lines = ['## Active Fleet']

    for server in servers:
        sid = server.get('id', server.get('host', ''))
        server_info = status.get(sid, {})
        online = server_info.get('online', False)
        sessions = server_info.get('sessions', {})

        for agent_cfg in server.get('agents', []):
            aname = agent_cfg.get('name', '')
            sess = sessions.get(aname, {})
            astatus = sess.get('status', 'offline') if online else 'offline'
            spec = agent_cfg.get('specialization', '')
            project = agent_cfg.get('project', '')
            parts = [f'Remote: {aname} @ {sid} ({astatus})']
            if spec:
                parts.append(f'specialist: {spec}')
            if project:
                parts.append(f'project: {project}')
            lines.append('- ' + ', '.join(parts))

    if len(lines) <= 1:
        return ''

    lines.append('')
    lines.append('Use FleetStatus, FleetSend, and FleetHistory tools to interact with remote agents.')
    return '\n'.join(lines)


def _build_tools(tools: list[dict] | None = None) -> str:
    """Layer 8: Available tools + guidelines."""
    tool_defs = tools or ALL_TOOL_DEFS
    tool_list = '\n'.join(f"- {t['name']}: {t['description'][:80]}" for t in tool_defs)

    return f"""Available tools:
{tool_list}

Guidelines:
- Use Bash for short-lived shell commands like ls, grep, find, git status, pytest, or bounded smoke tests.
- Do NOT use Bash for GUI apps, monitors, servers, dev watchers, nohup flows, or background jobs. Use RunProcess for those.
- After starting a managed process, use ProcessStatus / ProcessLogs / StopProcess to inspect and control it.
- Use Read to examine files before editing. You must use this tool instead of cat or sed.
- Use Edit for precise changes (oldText must match exactly)
- Use Write only for new files or complete rewrites
- When summarizing your actions, output plain text directly
- Be concise in your responses
- Show file paths clearly when working with files
- When you need the full file, continue with offset until complete
- For x.com workflows, prefer the X tool over generic Browser/Web when possible.
- If the user asks to check x.com bookmarks for anything new, use X action=triage_new_bookmarks.
- If the user asks what new bookmarks have been investigated, use X action=list_investigations with new_only=true.
- If the user asks to deep dive, investigate, or report on a specific bookmarked item, use X action=deep_dive_bookmark or X action=get_investigation depending on whether they want new research or the stored report."""


def _build_context_files(project: str) -> str:
    """Layer 9: Context files from project directory (AGENTS.md, CLAUDE.md, CHARON.md)."""
    if not project:
        return ''

    project_path = Path(project)
    if not project_path.is_dir():
        return ''

    candidates = ['AGENTS.md', 'CLAUDE.md', 'CHARON.md']
    sections = []

    # Walk up from project root (like pi-agent)
    current = project_path.resolve()
    root = Path('/').resolve()
    seen = set()

    while True:
        for name in candidates:
            fpath = current / name
            if fpath.exists() and str(fpath) not in seen:
                seen.add(str(fpath))
                try:
                    content = fpath.read_text(encoding='utf-8').strip()
                    if content:
                        content = _scan_content(content, name)
                        content = _truncate_content(content)
                        sections.append(f'## {name}\n\n{content}')
                except Exception:
                    pass

        if current == root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    if not sections:
        return ''

    return '# Project Context\n\n' + '\n\n'.join(sections)


# ── Main builder ────────────────────────────────────────────────────────

def build_system_prompt(
    *,
    state_dir: Path,
    agent: dict,
    task: dict,
    tools: list[dict] | None = None,
    contract: dict | None = None,
    custom_prompt: str = '',
) -> str:
    """Build the full layered system prompt for a Charon agent.

    Args:
        state_dir: Path to .charon_state/
        agent: Agent dict (id, name, role, goal, project, etc.)
        task: Current task dict
        tools: Tool definitions (defaults to ALL_TOOL_DEFS)
        contract: Shade contract dict (for shade agents only)
        custom_prompt: Override prompt (skips all layers except date/cwd)
    """
    cwd = str(task.get('project') or agent.get('project') or Path.cwd())
    date = time.strftime('%Y-%m-%d')

    if custom_prompt:
        return f'{custom_prompt}\nCurrent date: {date}\nCurrent working directory: {cwd}'

    agent_id = agent.get('id') or ''
    project = str(task.get('project') or agent.get('project') or '')
    is_shade = agent.get('role') == 'shade'

    parts = []

    # Layer 1: Identity
    if is_shade:
        parts.append(_build_shade_identity(agent, task, contract))
    else:
        parts.append(_build_identity(agent, task))

    # Layer 2: User model (skip for shades — they don't need personal preferences)
    if not is_shade and state_dir:
        block = _build_user_model(state_dir)
        if block:
            parts.append(block)

    # Layer 3: Project knowledge
    if state_dir and project:
        block = _build_project_knowledge(state_dir, project)
        if block:
            parts.append(block)

    # Layer 4: Working memory (skip for shades)
    if not is_shade and state_dir and agent_id:
        block = _build_working_memory(state_dir, agent_id)
        if block:
            parts.append(block)

    # Layer 4b: Semantic recall context (auto-injected, skip for shades)
    if not is_shade and state_dir:
        block = _build_recall_context(state_dir, agent_id)
        if block:
            parts.append(block)

    # Layer 5: Goal context (skip for shades)
    if not is_shade and state_dir and agent_id:
        block = _build_goal_context(state_dir, agent_id, project)
        if block:
            parts.append(block)

    # Layer 6: Coordination (skip for shades)
    if not is_shade and state_dir and agent_id:
        block = _build_coordination(state_dir, agent_id)
        if block:
            parts.append(block)

    # Layer 7: Shade contract is already in identity block for shades

    # Layer 7b: Fleet status (skip for shades)
    if not is_shade:
        block = _build_fleet_context()
        if block:
            parts.append(block)

    # Layer 8: Tools + guidelines
    parts.append(_build_tools(tools))

    # Layer 9: Context files
    if project:
        block = _build_context_files(project)
        if block:
            parts.append(block)

    # Layer 10: Date + CWD
    parts.append(f'Current date: {date}\nCurrent working directory: {cwd}')

    return '\n\n'.join(parts)
