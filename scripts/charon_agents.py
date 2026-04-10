#!/usr/bin/env python3
from __future__ import annotations
import argparse
import difflib
import importlib.util
import json
import os
import sys
import time
import subprocess
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.shortcuts.prompt import CompleteStyle
except Exception:
    PromptSession = None
    WordCompleter = None
    CompleteStyle = None

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / '.charon_state'


def _safe_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _safe_load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent_lifecycle = _load_module('agent_lifecycle_cli', ROOT / 'apps' / 'core-daemon' / 'agent_lifecycle.py')
conversation_runtime = _load_module('conversation_runtime_cli', ROOT / 'apps' / 'core-daemon' / 'conversation_runtime.py')
boundary_runtime = _load_module('boundary_runtime_cli', ROOT / 'apps' / 'core-daemon' / 'boundary_runtime.py')
shade_orchestrator = _load_module('shade_orchestrator_cli', ROOT / 'apps' / 'core-daemon' / 'shade_orchestrator.py')
goal_runtime = _load_module('goal_runtime_cli', ROOT / 'apps' / 'core-daemon' / 'goal_runtime.py')
llm_adapter = _load_module('llm_adapter_cli', ROOT / 'apps' / 'core-daemon' / 'llm_adapter.py')
charon_auth = _load_module('charon_auth_cli', ROOT / 'apps' / 'core-daemon' / 'charon_auth.py')


def cmd_create(args):
    try:
        a = agent_lifecycle.create_agent(
            name=args.name,
            mode=args.mode,
            goal=args.goal,
            project=args.project,
            role=args.role,
            visibility=args.visibility,
            parent_agent_id=args.parent_agent_id,
            require_tmux=not args.no_tmux,
        )
    except RuntimeError as e:
        print(f'create failed: {e}')
        raise SystemExit(2)
    print(f"{a['id']}\t{a['mode']}\t{a['status']}\t{a['name']}\t{a.get('role')}")


def cmd_spawn_shade(args):
    try:
        a = agent_lifecycle.create_agent(
            name='',
            mode='temp',
            goal=args.goal,
            project=args.project,
            role='shade',
            visibility='internal',
            parent_agent_id=args.parent_agent_id,
            require_tmux=not args.no_tmux,
        )
    except RuntimeError as e:
        print(f'shade spawn failed: {e}')
        raise SystemExit(2)
    print(f"{a['id']}\t{a['name']}\tparent={a.get('parent_agent_id')}")


def cmd_retire_shade(args):
    a = agent_lifecycle.set_status(args.shade_agent_id, 'stopped')
    if not a:
        print('not found')
        raise SystemExit(1)
    print(f"retired\t{a['id']}")


def cmd_list(_args):
    for a in agent_lifecycle.list_agents():
        print(
            f"{a.get('id')}\t{a.get('mode')}\t{a.get('status')}\t{a.get('name')}\t"
            f"{a.get('role', 'charon')}\t{a.get('visibility', 'user')}\t{a.get('goal')}"
        )


def cmd_stop(args):
    a = agent_lifecycle.set_status(args.id, 'stopped')
    if not a:
        print('not found')
        raise SystemExit(1)
    print(f"stopped\t{a['id']}")


def cmd_resume(args):
    a = agent_lifecycle.set_status(args.id, 'running')
    if not a:
        print('not found')
        raise SystemExit(1)
    print(f"running\t{a['id']}")


def _parse_phase_specs(phase_rows: list[str]) -> list[dict]:
    out = []
    for row in phase_rows or []:
        raw = str(row or '').strip()
        if not raw:
            continue
        if '|' in raw:
            name, objective = raw.split('|', 1)
            out.append({'name': name.strip(), 'objective': objective.strip()})
        else:
            out.append({'name': raw, 'objective': raw})
    return out


def cmd_task(args):
    task = conversation_runtime.enqueue_agent_task(
        STATE_DIR,
        owner_agent_id=args.agent_id,
        instruction=args.instruction,
        title=args.title,
        project=args.project,
        priority=args.priority,
        conversation_id=args.conversation_id,
        max_attempts=args.max_attempts,
        scope=args.scope,
        deps=args.dep,
        correlation_id=args.correlation_id,
        constraints=args.constraint,
        expected_outputs=args.output,
        phase_plan=_parse_phase_specs(args.phase),
    )
    print(f"{task['id']}\t{task['status']}\t{task['owner_agent_id']}\t{task['instruction']}")


def cmd_boundary_propose(args):
    task = conversation_runtime.enqueue_boundary_proposal_task(
        STATE_DIR,
        proposer_agent_id=args.proposer_agent_id,
        target_agent_id=args.target_agent_id,
        project=args.project,
        scope=args.scope,
        reason=args.reason,
        conversation_id=args.conversation_id,
        correlation_id=args.correlation_id,
    )
    print(f"{task['id']}\tpending\t{task['title']}")


def cmd_boundary_resolve(args):
    task = conversation_runtime.enqueue_boundary_resolution_task(
        STATE_DIR,
        resolver_agent_id=args.resolver_agent_id,
        proposal_id=args.proposal_id,
        decision=args.decision,
        reason=args.reason,
        conversation_id=args.conversation_id,
        correlation_id=args.correlation_id,
    )
    print(f"{task['id']}\tpending\t{task['title']}")


def cmd_boundaries(_args):
    for b in boundary_runtime.load_boundaries(STATE_DIR):
        print(
            f"{b.get('id')}\t{b.get('status')}\t{b.get('proposer_agent_id')}->{b.get('target_agent_id')}\t"
            f"scope={','.join(b.get('scope') or [])}\treason={b.get('reason')}"
        )


def cmd_inbox(args):
    inbox = STATE_DIR / 'agents' / args.agent_id / 'inbox.jsonl'
    if not inbox.exists():
        print('no inbox')
        return
    rows = [json.loads(line) for line in inbox.read_text().splitlines() if line.strip()]
    for rec in rows[-args.limit:]:
        print(json.dumps(rec, ensure_ascii=False))


def cmd_shade_contracts(_args):
    for c in shade_orchestrator.load_contracts(STATE_DIR):
        print(
            f"{c.get('id')}\t{c.get('status')}\tparent_task={c.get('parent_task_id')}\t"
            f"shade={c.get('shade_agent_id')}\tcurrent={c.get('current_phase_id')}"
        )


def cmd_shade_contract(args):
    rec = shade_orchestrator.get_contract(STATE_DIR, args.contract_id)
    if not rec:
        print('not found')
        raise SystemExit(1)
    print(json.dumps(rec, indent=2, ensure_ascii=False))


def cmd_shade_events(args):
    rows = shade_orchestrator.load_phase_events(STATE_DIR, contract_id=args.contract_id)
    for rec in rows[-args.limit:]:
        print(json.dumps(rec, ensure_ascii=False))


def cmd_shade_branch(args):
    rec = shade_orchestrator.branch_from_phase(
        STATE_DIR,
        contract_id=args.contract_id,
        from_phase_id=args.from_phase_id,
        reason=args.reason,
    )
    if not rec:
        print('not found')
        raise SystemExit(1)

    queue_path = STATE_DIR / 'queue.json'
    queue = []
    if queue_path.exists():
        try:
            queue = json.loads(queue_path.read_text())
        except Exception:
            queue = []
    woke = False
    for task in queue:
        orch = task.get('shade_orchestration') or {}
        if orch.get('contract_id') == args.contract_id:
            task['status'] = 'pending'
            task.pop('wait_state', None)
            task.pop('started_at', None)
            task['updated_at'] = task.get('updated_at') or ''
            woke = True
            break
    if woke:
        queue_path.write_text(json.dumps(queue, indent=2))

    print(f"branched\t{rec.get('id')}\tfrom={args.from_phase_id}\tactive_branch={rec.get('active_branch_id')}")


def cmd_shade_investigate(args):
    contract = shade_orchestrator.get_contract(STATE_DIR, args.contract_id)
    if not contract:
        print('not found')
        raise SystemExit(1)

    phase = None
    for p in (contract.get('phases') or []):
        if p.get('phase_id') == args.phase_id:
            phase = p
            break
    if not phase:
        print('phase not found')
        raise SystemExit(1)

    queue = _safe_load_json(STATE_DIR / 'queue.json', [])
    phase_tasks = [
        t for t in queue
        if ((t.get('shade_phase') or {}).get('contract_id') == args.contract_id
            and (t.get('shade_phase') or {}).get('phase_id') == args.phase_id)
    ]
    phase_tasks_sorted = sorted(phase_tasks, key=lambda t: t.get('created_at') or '')
    task_ids = [t.get('id') for t in phase_tasks_sorted if t.get('id')]

    attempts_file = STATE_DIR / 'agents' / str(contract.get('shade_agent_id')) / 'attempts.jsonl'
    attempt_rows = [r for r in _safe_load_jsonl(attempts_file) if r.get('task_id') in task_ids]
    attempt_rows = sorted(attempt_rows, key=lambda r: r.get('ts') or '')

    phase_events = [
        e for e in shade_orchestrator.load_phase_events(STATE_DIR, contract_id=args.contract_id)
        if e.get('phase_id') in (args.phase_id, '-')
    ]
    phase_events = sorted(phase_events, key=lambda e: e.get('ts') or '')

    failure_signature = phase.get('error')
    if not failure_signature:
        for t in reversed(phase_tasks_sorted):
            if t.get('status') == 'failed':
                failure_signature = str(t.get('result_summary') or (t.get('last_error') or {}).get('error') or 'task failed')
                break

    recommendation = shade_orchestrator.suggest_branch_phase(contract, phase_id=args.phase_id)

    out = {
        'contract_id': args.contract_id,
        'phase_id': args.phase_id,
        'phase_name': phase.get('name'),
        'phase_status': phase.get('status'),
        'phase_objective': phase.get('objective'),
        'phase_lookup_key': phase.get('lookup_key'),
        'failure_signature': failure_signature,
        'tasks': [
            {
                'task_id': t.get('id'),
                'status': t.get('status'),
                'attempt_count': t.get('attempt_count'),
                'created_at': t.get('created_at'),
                'started_at': t.get('started_at'),
                'completed_at': t.get('completed_at'),
                'result_summary': t.get('result_summary'),
            }
            for t in phase_tasks_sorted
        ],
        'attempt_timeline': [
            {
                'ts': r.get('ts'),
                'task_id': r.get('task_id'),
                'attempt_id': r.get('attempt_id'),
                'stage': r.get('stage'),
                'payload': r.get('payload'),
            }
            for r in attempt_rows[-args.limit:]
        ],
        'phase_events': phase_events[-args.limit:],
        'recommendation': recommendation,
        'suggested_resume_phase_id': recommendation.get('recommended_phase_id'),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))




def _load_agents_local() -> list[dict]:
    return _safe_load_json(STATE_DIR / 'agents.json', [])


def _get_agent(agent_id: str) -> dict | None:
    aid = str(agent_id or '').strip()
    if not aid:
        return None
    for rec in _load_agents_local():
        if isinstance(rec, dict) and rec.get('id') == aid:
            return rec
    return None


def _require_agent(agent_id: str) -> dict:
    rec = _get_agent(agent_id)
    if rec:
        return rec
    print(f'agent not found in {STATE_DIR / "agents.json"}: {agent_id}')
    raise SystemExit(2)


def _event_content(rec: dict) -> str:
    payload = rec.get('payload') if isinstance(rec.get('payload'), dict) else {}
    return str(payload.get('content') or rec.get('content') or '')


def _event_actor(rec: dict) -> str:
    return str(rec.get('actor_id') or rec.get('actor_agent_id') or '-')


def _conversation_events(conversation_id: str) -> list[dict]:
    rows = _safe_load_jsonl(STATE_DIR / 'interventions.jsonl')
    return [r for r in rows if r.get('conversation_id') == conversation_id]


def _print_thread_rows(rows: list[dict]):
    for rec in rows:
        ts = rec.get('ts') or '-'
        conv = rec.get('conversation_id') or '-'
        actor = _event_actor(rec)
        content = _event_content(rec)
        print(f"{ts}	{conv}	{actor}	{content}")


def _wait_for_response(*, conversation_id: str, since_count: int, timeout_sec: float, poll_sec: float = 0.25):
    deadline = time.time() + max(0.1, timeout_sec)
    while time.time() < deadline:
        rows = _conversation_events(conversation_id)
        if len(rows) > since_count:
            return rows[since_count:]
        time.sleep(poll_sec)
    return []


def cmd_agent_prompt(args):
    agent = _require_agent(args.agent_id)
    conversation_id = args.conversation_id or f"conv-{args.agent_id}"
    before = _conversation_events(conversation_id)

    task = conversation_runtime.enqueue_user_intent_task(
        STATE_DIR,
        actor_agent_id=args.agent_id,
        message=args.message,
        project=args.project or agent.get('project') or str(ROOT),
        session_id=args.session_id,
        conversation_id=conversation_id,
    )
    print(f"{task['id']}	pending	{task['actor_agent_id']}	{task['message']}")

    if args.wait:
        rows = _wait_for_response(
            conversation_id=conversation_id,
            since_count=len(before),
            timeout_sec=float(args.timeout_sec),
        )
        if not rows:
            print(f"timeout waiting for response (conversation_id={conversation_id})")
            return
        print('--- response ---')
        _print_thread_rows(rows[-args.limit:])


def cmd_agent_thread(args):
    interventions = _safe_load_jsonl(STATE_DIR / 'interventions.jsonl')
    rows = [r for r in interventions if (not args.conversation_id or r.get('conversation_id') == args.conversation_id)]
    if args.agent_id:
        rows = [r for r in rows if _event_actor(r) == args.agent_id]
    _print_thread_rows(rows[-args.limit:])


def cmd_agent_watch(args):
    conversation_id = args.conversation_id
    if not conversation_id and args.agent_id:
        conversation_id = f"conv-{args.agent_id}"
    if not conversation_id:
        print('watch requires --conversation-id or --agent-id')
        raise SystemExit(2)

    print(f"watching conversation: {conversation_id} (Ctrl+C to stop)")
    seen = 0
    try:
        while True:
            rows = _conversation_events(conversation_id)
            if len(rows) > seen:
                _print_thread_rows(rows[seen:])
                seen = len(rows)
            time.sleep(max(0.1, float(args.poll_sec)))
    except KeyboardInterrupt:
        print('\nwatch stopped')


def _chat_read_input(prompt_text: str = "you> ") -> str:
    if PromptSession is None or WordCompleter is None:
        return input(prompt_text).strip()

    session = getattr(_chat_read_input, '_session', None)
    if session is None:
        completer = WordCompleter(
            _chat_command_catalog(),
            ignore_case=True,
            sentence=True,
            match_middle=True,
        )
        session = PromptSession(completer=completer)
        setattr(_chat_read_input, '_session', session)

    return session.prompt(
        prompt_text,
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN if CompleteStyle is not None else None,
    ).strip()


def cmd_agent_chat(args):
    agent = _require_agent(args.agent_id)
    conversation_id = args.conversation_id or f"conv-{args.agent_id}"

    # Show status header
    onboarding = _load_onboarding_state()
    provider = str(onboarding.get('provider') or 'none').strip()
    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none').strip()
    is_llm = onboarding.get('complete') and str(onboarding.get('provider_mode')).lower() == 'provider' and provider
    mode_str = f"LLM ({provider}/{model})" if is_llm else "heuristic (no LLM — run /setup provider <name> to configure)"

    print(f"charon-chat agent={args.agent_id} conversation={conversation_id}")
    print(f"Mode: {mode_str}")
    print("Type message and press Enter. Commands: /help /setup /model /thread /agents /quit")

    while True:
        try:
            msg = _chat_read_input('you> ')
        except (EOFError, KeyboardInterrupt):
            print('\nbye')
            return

        if not msg:
            continue
        if msg in ('/quit', '/exit'):
            print('bye')
            return
        if msg == '/agents':
            cmd_list(argparse.Namespace())
            continue
        if msg == '/thread':
            cmd_agent_thread(argparse.Namespace(conversation_id=conversation_id, limit=args.limit, agent_id=''))
            continue
        if msg.startswith('/'):
            handled = _handle_chat_slash_command(
                msg,
                agent_id=args.agent_id,
                conversation_id=conversation_id,
                session_id=args.session_id,
                project=args.project or agent.get('project') or str(ROOT),
                limit=args.limit,
            )
            if handled:
                continue

        before = _conversation_events(conversation_id)
        task = conversation_runtime.enqueue_user_intent_task(
            STATE_DIR,
            actor_agent_id=args.agent_id,
            message=msg,
            project=args.project or agent.get('project') or str(ROOT),
            session_id=args.session_id,
            conversation_id=conversation_id,
        )
        print(f"queued {task['id']}")

        rows = _wait_for_response(
            conversation_id=conversation_id,
            since_count=len(before),
            timeout_sec=float(args.timeout_sec),
        )
        if not rows:
            print('assistant> [timeout waiting for response]')
            continue

        last = rows[-1]
        print(f"assistant[{_event_actor(last)}]> {_event_content(last)}")


def cmd_goal_show(args):
    doc = goal_runtime.show_goals(STATE_DIR, session_id=args.session_id, project_id=args.project_id)
    print(json.dumps(doc, indent=2, ensure_ascii=False))


def cmd_agent_session(args):
    _require_agent(args.agent_id)
    daemon_proc = None
    try:
        if not getattr(args, 'no_daemon', False):
            cmd = [
                sys.executable,
                str(ROOT / 'apps' / 'core-daemon' / 'charon_loop.py'),
                '--state-dir',
                str(STATE_DIR),
            ]
            if getattr(args, 'debug_trace', False):
                cmd.append('--debug-trace')
            env = dict(os.environ)
            if getattr(args, 'debug_trace', False):
                env['CHARON_DEBUG_TRACE'] = '1'
            daemon_stdout = None if getattr(args, 'show_daemon_logs', False) else subprocess.DEVNULL
            daemon_stderr = None if getattr(args, 'show_daemon_logs', False) else subprocess.DEVNULL
            daemon_proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdout=daemon_stdout,
                stderr=daemon_stderr,
            )
            time.sleep(0.6)
            if daemon_proc.poll() is not None:
                raise RuntimeError('failed to start charon daemon')
            print(f'started daemon pid={daemon_proc.pid}')

        chat_args = SimpleNamespace(
            agent_id=args.agent_id,
            project=args.project,
            session_id=args.session_id,
            conversation_id=args.conversation_id,
            timeout_sec=args.timeout_sec,
            limit=args.limit,
        )
        cmd_agent_chat(chat_args)
    finally:
        if daemon_proc is not None and daemon_proc.poll() is None:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()
                daemon_proc.wait(timeout=3)
            print('stopped daemon')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_hyperlink(url: str, label: str) -> str:
    return f"]8;;{url}{label}]8;;"


def _open_in_browser(url: str) -> None:
    if not url:
        return
    opener = 'xdg-open'
    if sys.platform == 'darwin':
        opener = 'open'
    elif sys.platform.startswith('win'):
        opener = 'start'
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _provider_display_name(provider: str) -> str:
    names = {
        'codex': 'OpenAI Codex',
        'claude-code': 'Claude Code',
    }
    return names.get(provider, provider)


def _chat_command_catalog() -> list[str]:
    return [
        '/help',
        '/agents',
        '/thread',
        '/model',
        '/model <name>',
        '/setup',
        '/setup status',
        '/setup reset',
        '/setup no-provider',
        '/setup provider codex',
        '/setup provider claude-code',
        '/setup provider opencode',
        '/setup provider api',
        '/setup auth-start',
        '/setup model <name>',
        '/setup project <name>',
        '/setup complete',
        '/clarifications',
        '/clarify <clarification_id> <answer>',
        '/quit',
        '/exit',
    ]


def _print_chat_suggestions(raw: str = '') -> None:
    raw = (raw or '').strip()
    items = _chat_command_catalog()
    if raw:
        starts = [it for it in items if it.startswith(raw)]
        contains = [it for it in items if raw in it]
        fuzzy = difflib.get_close_matches(raw, items, n=8, cutoff=0.45)
        merged = []
        for bucket in (starts, contains, fuzzy):
            for it in bucket:
                if it not in merged:
                    merged.append(it)
        items = merged
    if not items:
        print('No suggestions')
        return
    print('Suggestions:')
    for it in items:
        print(f'  {it}')


def _handle_chat_slash_command(msg: str, *, agent_id: str, conversation_id: str, session_id: str, project: str, limit: int) -> bool:
    text = (msg or '').strip()
    if text in ('/help', '/setup'):
        _print_chat_suggestions('/setup' if text == '/setup' else '')
        return True

    if text == '/clarifications':
        tools_mod = _load_module('tools_clarify_cli_tools', ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py')
        clarify_mod = _load_module('clarify_cli_mod', ROOT / 'apps' / 'core-daemon' / 'tools' / 'clarify_tool.py')
        ctx = tools_mod.ToolContext(project_root=ROOT, agent_id=agent_id, state_dir=STATE_DIR)
        res = clarify_mod.execute_clarify({'action': 'list'}, ctx)
        details = res.details or {}
        items = details.get('items') or []
        if not items:
            print('No pending clarifications.')
            return True
        print('Pending clarifications:')
        for row in items:
            cid = row.get('clarification_id')
            question = row.get('question')
            choices = row.get('choices') or []
            print(f'- {cid}: {question}')
            for choice in choices:
                print(f'  /clarify {cid} {choice}')
        return True

    if text.startswith('/clarify '):
        rest = text[len('/clarify '):].strip()
        parts = rest.split(None, 1)
        if len(parts) != 2:
            print('Usage: /clarify <clarification_id> <answer>')
            return True
        cid, answer = parts[0].strip(), parts[1].strip()
        tools_mod = _load_module('tools_clarify_cli_tools_answer', ROOT / 'apps' / 'core-daemon' / 'tools' / '__init__.py')
        clarify_mod = _load_module('clarify_cli_mod_answer', ROOT / 'apps' / 'core-daemon' / 'tools' / 'clarify_tool.py')
        ctx = tools_mod.ToolContext(project_root=ROOT, agent_id=agent_id, state_dir=STATE_DIR)
        res = clarify_mod.execute_clarify({'action': 'answer', 'clarification_id': cid, 'answer': answer}, ctx)
        print(res.content)
        applied = (res.details or {}).get('applied_result') or {}
        if applied:
            print(f"applied worker provider={applied.get('provider')} model={applied.get('model')}")
        return True

    if text.startswith('/model'):
        rest = text[len('/model'):].strip()
        if not rest:
            print(f"current model: {llm_adapter.detect_model()}")
            print('use /model <name> to change')
            return True
        cmd_setup_model(argparse.Namespace(name=rest))
        return True

    if text.startswith('/setup '):
        rest = text[len('/setup '):].strip()
        if rest == 'status':
            cmd_setup_status(argparse.Namespace())
            return True
        if rest == 'reset':
            cmd_setup_reset(argparse.Namespace())
            return True
        if rest == 'no-provider':
            cmd_setup_no_provider(argparse.Namespace())
            return True
        if rest.startswith('provider '):
            provider_name = rest.split(' ', 1)[1].strip()
            cmd_setup_provider(argparse.Namespace(name=provider_name))
            if provider_name in ('codex', 'claude-code') and sys.stdin.isatty():
                try:
                    cmd_setup_auth_start(argparse.Namespace(no_open=False))
                except SystemExit:
                    print('auth start failed; run /setup auth-start to retry')
            return True
        if rest in ('auth-start', 'auth start'):
            cmd_setup_auth_start(argparse.Namespace(no_open=False))
            return True
        if rest.startswith('model '):
            cmd_setup_model(argparse.Namespace(name=rest.split(' ', 1)[1].strip()))
            return True
        if rest.startswith('project '):
            cmd_setup_project(argparse.Namespace(name=rest.split(' ', 1)[1].strip()))
            return True
        if rest == 'complete':
            cmd_setup_complete(argparse.Namespace())
            return True
        if rest.startswith('api-key '):
            cmd_setup_api_key(argparse.Namespace(key=rest.split(' ', 1)[1].strip()))
            return True
        if rest.startswith('api-url '):
            cmd_setup_api_url(argparse.Namespace(url=rest.split(' ', 1)[1].strip()))
            return True
        if rest.startswith('opencode-provider '):
            cmd_setup_opencode_provider(argparse.Namespace(name=rest.split(' ', 1)[1].strip()))
            return True
        if rest.startswith('opencode-model '):
            cmd_setup_opencode_model(argparse.Namespace(name=rest.split(' ', 1)[1].strip()))
            return True

        _print_chat_suggestions('/setup ' + rest)
        return True

    if text.startswith('/'):
        _print_chat_suggestions(text)
        return True

    return False


def _default_onboarding() -> dict:
    return {
        'complete': False,
        'step': 'provider-mode',
        'provider_mode': '',
        'provider': '',
        'provider_model': '',
        'provider_base_url': '',
        'model': '',
        'provider_auth': '',
        'opencode_provider': '',
        'opencode_model': '',
        'api_key': '',
        'project': '',
        'updated_at': '',
    }


def _load_onboarding_state() -> dict:
    path = STATE_DIR / 'onboarding.json'
    data = _safe_load_json(path, {})
    base = _default_onboarding()
    if isinstance(data, dict):
        for key in base.keys():
            if key in data:
                base[key] = data.get(key)
    return base


def _save_onboarding_state(state: dict) -> None:
    path = STATE_DIR / 'onboarding.json'
    state = dict(_default_onboarding(), **(state or {}))
    state['updated_at'] = _utc_now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _save_auth_provider(provider_id: str, payload: dict) -> None:
    auth_dir = STATE_DIR / 'auth'
    auth_file = auth_dir / 'auth.json'
    store = _safe_load_json(auth_file, {})
    if not isinstance(store, dict):
        store = {}
    store.setdefault('version', 1)
    store.setdefault('providers', {})
    store['active_provider'] = provider_id
    store['providers'][provider_id] = payload
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(store, indent=2))
    try:
        os.chmod(auth_dir, 0o700)
        os.chmod(auth_file, 0o600)
    except Exception:
        pass


def cmd_setup_status(_args):
    state = _load_onboarding_state()
    report = {
        'onboarding': state,
        'resolved_model': llm_adapter.detect_model(),
        'resolved_base_url': llm_adapter.detect_base_url(),
        'planner_mode_hint': 'llm' if (state.get('complete') and str(state.get('provider_mode')).lower() == 'provider' and str(state.get('provider')).strip()) else 'heuristic',
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


def cmd_setup_reset(_args):
    _save_onboarding_state(_default_onboarding())
    print('setup reset')


def cmd_setup_no_provider(_args):
    state = _load_onboarding_state()
    state.update({
        'provider_mode': 'no-provider',
        'provider': '',
        'step': 'project',
        'complete': False,
    })
    _save_onboarding_state(state)
    print('setup mode=no-provider')


def cmd_setup_provider(args):
    provider = str(args.name or '').strip().lower()
    allowed = {'codex', 'claude-code', 'opencode', 'api', 'lmstudio'}
    if provider not in allowed:
        print(f'unsupported provider: {provider}')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state.update({
        'provider_mode': 'provider',
        'provider': provider,
        'complete': False,
        'step': 'provider-auth' if provider in ('codex', 'claude-code') else 'model',
    })
    _save_onboarding_state(state)
    print(f'setup provider={provider}')


def cmd_setup_model(args):
    model = str(args.name or '').strip()
    if not model:
        print('model is required')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state['model'] = model
    state['provider_model'] = model
    state['step'] = 'project'
    _save_onboarding_state(state)
    print(f'setup model={model}')


def cmd_setup_api_url(args):
    url = str(args.url or '').strip()
    if not url:
        print('api url is required')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state['provider_base_url'] = url
    _save_onboarding_state(state)
    print(f'setup api-url={url}')


def cmd_setup_api_key(args):
    key = str(args.key or '').strip()
    if not key:
        print('api key is required')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state['api_key'] = key
    if not state.get('provider'):
        state['provider'] = 'api'
    state['provider_mode'] = 'provider'
    state['step'] = 'model'
    _save_onboarding_state(state)
    _save_auth_provider('openrouter', {'auth_type': 'api_key', 'api_key': key, 'updated_at': _utc_now_iso()})
    print('setup api-key saved')


def cmd_setup_opencode_provider(args):
    provider = str(args.name or '').strip()
    if not provider:
        print('opencode provider is required')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state['provider'] = 'opencode'
    state['provider_mode'] = 'provider'
    state['opencode_provider'] = provider
    state['step'] = 'opencode-model'
    _save_onboarding_state(state)
    print(f'setup opencode-provider={provider}')


def cmd_setup_opencode_model(args):
    model = str(args.name or '').strip()
    if not model:
        print('opencode model is required')
        raise SystemExit(2)
    state = _load_onboarding_state()
    state['provider'] = 'opencode'
    state['provider_mode'] = 'provider'
    state['opencode_model'] = model
    state['model'] = model
    state['provider_model'] = model
    state['step'] = 'project'
    _save_onboarding_state(state)
    print(f'setup opencode-model={model}')


def cmd_setup_project(args):
    project = str(args.name or '').strip()
    state = _load_onboarding_state()
    state['project'] = project
    state['step'] = 'complete'
    _save_onboarding_state(state)
    print(f'setup project={project}')


def cmd_setup_complete(_args):
    state = _load_onboarding_state()
    state['complete'] = True
    state['step'] = 'done'
    _save_onboarding_state(state)

    provider_mode = str(state.get('provider_mode') or '').lower()
    provider = str(state.get('provider') or '').lower()
    project = str(state.get('project') or str(ROOT)).strip()
    model = str(state.get('model') or state.get('provider_model') or '').strip()

    print(f'setup complete — provider: {provider or "none"}, model: {model or "none"}')

    # Create default agent
    if provider_mode != 'no-provider':
        try:
            existing = agent_lifecycle.list_agents()
            has_charon = any(
                a.get('role') == 'charon' and a.get('status') != 'stopped'
                for a in existing
            )
            if not has_charon:
                agent = agent_lifecycle.create_agent(
                    name='',
                    mode='persistent',
                    goal=f'Primary agent for {project.split("/")[-1] or "project"}',
                    project=project,
                    role='charon',
                    visibility='user',
                    require_tmux=False,
                )
                print(f'  → created agent {agent["name"]} ({agent["id"]})')
            else:
                print(f'  → agent already exists ({len(existing)} agents)')
        except Exception as e:
            print(f'  → agent creation failed: {e}')
    else:
        print('  → no-provider mode, skipped agent creation')

    # Detect running agents
    try:
        _tui_dir = str(ROOT / 'apps' / 'tui')
        if _tui_dir not in sys.path:
            sys.path.insert(0, _tui_dir)
        from process_inspector import detect_agent_processes, summarize_agent_processes
        procs = detect_agent_processes()
        if procs:
            print(f'  → detected {len(procs)} running agent process(es):')
            for line in summarize_agent_processes(procs):
                print(f'    {line}')
        else:
            print('  → no other agent processes detected')
    except Exception as e:
        print(f'  → process detection failed: {e}')

    # Sync to SQLite
    try:
        _daemon_dir = str(ROOT / 'apps' / 'core-daemon')
        if _daemon_dir not in sys.path:
            sys.path.insert(0, _daemon_dir)
        from store_adapter import get_db, onboarding_set as db_ob_set
        db = get_db(STATE_DIR)
        db_ob_set(db, state)
    except Exception:
        pass


def cmd_setup_auth_start(args):
    state = _load_onboarding_state()
    provider = str(state.get('provider') or '').strip().lower()
    provider_map = {
        'codex': 'openai-codex',
        'claude-code': 'anthropic',
    }
    provider_id = provider_map.get(provider)
    if not provider_id:
        print('setup auth-start requires provider codex or claude-code')
        raise SystemExit(2)

    opened = False

    def _status(msg: str):
        nonlocal opened
        if not msg:
            return
        if msg.startswith('AUTH_URL::'):
            url = msg.split('AUTH_URL::', 1)[1].strip()
            click_hint = 'Cmd+click to open' if sys.platform == 'darwin' else 'Ctrl+click to open'
            print()
            print(f"Login to {_provider_display_name(provider)}")
            print(url)
            print(_terminal_hyperlink(url, click_hint))
            if not getattr(args, 'no_open', False) and not opened:
                _open_in_browser(url)
                opened = True
            return
        if msg.startswith('AUTH_INFO::'):
            info = msg.split('AUTH_INFO::', 1)[1].strip()
            print(info)
            return
        print(msg)

    def _prompt_code(prompt_text: str) -> str:
        try:
            return input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            return ''

    token_data = charon_auth.login_oauth(
        provider_id,
        status_cb=_status,
        auth_code_cb=_prompt_code,
    )
    state['provider_auth'] = 'oauth'
    state['step'] = 'model'
    _save_onboarding_state(state)
    print('Authentication successful. Credentials saved to .charon_state/auth/auth.json')
    if token_data.get('auth_url'):
        print(f"auth_url={token_data.get('auth_url')}")

def add_create_parser(sub, name: str):
    c = sub.add_parser(name)
    c.add_argument('--mode', choices=['temp', 'persistent'], default='persistent')
    c.add_argument('--name', default='')
    c.add_argument('--goal', required=True)
    c.add_argument('--project', default='')
    c.add_argument('--role', choices=['charon', 'shade'], default='charon')
    c.add_argument('--visibility', choices=['user', 'internal'], default='user')
    c.add_argument('--parent-agent-id', default='')
    c.add_argument('--no-tmux', action='store_true', help='Create agent without tmux session')
    c.set_defaults(func=cmd_create)


def main():
    p = argparse.ArgumentParser(description='Charon agent lifecycle CLI')
    sub = p.add_subparsers(dest='cmd', required=True)

    add_create_parser(sub, 'create')
    add_create_parser(sub, 'new')

    spawn = sub.add_parser('shade-spawn')
    spawn.add_argument('parent_agent_id')
    spawn.add_argument('--goal', required=True)
    spawn.add_argument('--project', default=str(ROOT))
    spawn.add_argument('--no-tmux', action='store_true')
    spawn.set_defaults(func=cmd_spawn_shade)

    retire = sub.add_parser('shade-retire')
    retire.add_argument('shade_agent_id')
    retire.set_defaults(func=cmd_retire_shade)

    l = sub.add_parser('list')
    l.set_defaults(func=cmd_list)

    t = sub.add_parser('task')
    t.add_argument('agent_id')
    t.add_argument('instruction')
    t.add_argument('--title', default='')
    t.add_argument('--project', default=str(ROOT))
    t.add_argument('--priority', choices=['low', 'normal', 'high', 'urgent'], default='normal')
    t.add_argument('--conversation-id', default='')
    t.add_argument('--max-attempts', type=int, default=3)
    t.add_argument('--scope', action='append', default=[], help='Repeatable scope hint, e.g. src/api or docs')
    t.add_argument('--dep', action='append', default=[], help='Repeatable dependency task id')
    t.add_argument('--correlation-id', default='')
    t.add_argument('--constraint', action='append', default=[], help='Contract constraint (repeatable)')
    t.add_argument('--output', action='append', default=[], help='Expected output (repeatable)')
    t.add_argument('--phase', action='append', default=[], help='Phase spec: name|objective (repeatable)')
    t.set_defaults(func=cmd_task)

    bp = sub.add_parser('boundary-propose')
    bp.add_argument('proposer_agent_id')
    bp.add_argument('target_agent_id')
    bp.add_argument('--project', required=True)
    bp.add_argument('--scope', action='append', default=[])
    bp.add_argument('--reason', required=True)
    bp.add_argument('--conversation-id', default='')
    bp.add_argument('--correlation-id', default='')
    bp.set_defaults(func=cmd_boundary_propose)

    br = sub.add_parser('boundary-resolve')
    br.add_argument('resolver_agent_id')
    br.add_argument('proposal_id')
    br.add_argument('decision', choices=['accept', 'reject'])
    br.add_argument('--reason', default='')
    br.add_argument('--conversation-id', default='')
    br.add_argument('--correlation-id', default='')
    br.set_defaults(func=cmd_boundary_resolve)

    bl = sub.add_parser('boundaries')
    bl.set_defaults(func=cmd_boundaries)

    ib = sub.add_parser('inbox')
    ib.add_argument('agent_id')
    ib.add_argument('--limit', type=int, default=20)
    ib.set_defaults(func=cmd_inbox)

    scs = sub.add_parser('shade-contracts')
    scs.set_defaults(func=cmd_shade_contracts)

    sc = sub.add_parser('shade-contract')
    sc.add_argument('contract_id')
    sc.set_defaults(func=cmd_shade_contract)

    se = sub.add_parser('shade-events')
    se.add_argument('--contract-id', default='')
    se.add_argument('--limit', type=int, default=40)
    se.set_defaults(func=cmd_shade_events)

    sb = sub.add_parser('shade-branch')
    sb.add_argument('contract_id')
    sb.add_argument('from_phase_id')
    sb.add_argument('--reason', required=True)
    sb.set_defaults(func=cmd_shade_branch)

    si = sub.add_parser('shade-investigate')
    si.add_argument('contract_id')
    si.add_argument('phase_id')
    si.add_argument('--limit', type=int, default=80)
    si.set_defaults(func=cmd_shade_investigate)

    s = sub.add_parser('stop')
    s.add_argument('id')
    s.set_defaults(func=cmd_stop)

    r = sub.add_parser('resume')
    r.add_argument('id')
    r.set_defaults(func=cmd_resume)

    pr = sub.add_parser('prompt', help='Send a user-intent message to an agent')
    pr.add_argument('agent_id')
    pr.add_argument('message')
    pr.add_argument('--project', default='')
    pr.add_argument('--session-id', default='')
    pr.add_argument('--conversation-id', default='')
    pr.add_argument('--wait', action='store_true', help='Wait for and print assistant response')
    pr.add_argument('--timeout-sec', type=float, default=30.0)
    pr.add_argument('--limit', type=int, default=20)
    pr.set_defaults(func=cmd_agent_prompt)

    th = sub.add_parser('thread', help='Show conversation messages from interventions log')
    th.add_argument('--conversation-id', default='')
    th.add_argument('--agent-id', default='')
    th.add_argument('--limit', type=int, default=40)
    th.set_defaults(func=cmd_agent_thread)

    wt = sub.add_parser('watch', help='Follow conversation messages live')
    wt.add_argument('--conversation-id', default='')
    wt.add_argument('--agent-id', default='')
    wt.add_argument('--poll-sec', type=float, default=0.5)
    wt.set_defaults(func=cmd_agent_watch)

    ch = sub.add_parser('chat', help='Interactive chat loop (pi-style)')
    ch.add_argument('agent_id')
    ch.add_argument('--project', default='')
    ch.add_argument('--session-id', default='')
    ch.add_argument('--conversation-id', default='')
    ch.add_argument('--timeout-sec', type=float, default=45.0)
    ch.add_argument('--limit', type=int, default=40)
    ch.set_defaults(func=cmd_agent_chat)

    sess = sub.add_parser('session', help='One-command interactive session: starts daemon then enters chat')
    sess.add_argument('agent_id')
    sess.add_argument('--project', default='')
    sess.add_argument('--session-id', default='')
    sess.add_argument('--conversation-id', default='')
    sess.add_argument('--timeout-sec', type=float, default=45.0)
    sess.add_argument('--limit', type=int, default=40)
    sess.add_argument('--no-daemon', action='store_true', help='Only open chat; assume daemon already running')
    sess.add_argument('--debug-trace', action='store_true', help='Enable daemon debug trace logging')
    sess.add_argument('--show-daemon-logs', action='store_true', help='Print daemon stdout/stderr (off by default to keep chat clean)')
    sess.set_defaults(func=cmd_agent_session)

    g = sub.add_parser('goals')
    g.add_argument('--session-id', default='')
    g.add_argument('--project-id', default='')
    g.set_defaults(func=cmd_goal_show)

    setup = sub.add_parser('setup', help='Onboarding + provider/model configuration')
    setup_sub = setup.add_subparsers(dest='setup_cmd', required=True)

    st = setup_sub.add_parser('status')
    st.set_defaults(func=cmd_setup_status)

    sr = setup_sub.add_parser('reset')
    sr.set_defaults(func=cmd_setup_reset)

    sn = setup_sub.add_parser('no-provider')
    sn.set_defaults(func=cmd_setup_no_provider)

    sp = setup_sub.add_parser('provider')
    sp.add_argument('name')
    sp.set_defaults(func=cmd_setup_provider)

    sm = setup_sub.add_parser('model')
    sm.add_argument('name')
    sm.set_defaults(func=cmd_setup_model)

    sau = setup_sub.add_parser('api-url')
    sau.add_argument('url')
    sau.set_defaults(func=cmd_setup_api_url)

    sak = setup_sub.add_parser('api-key')
    sak.add_argument('key')
    sak.set_defaults(func=cmd_setup_api_key)

    sop = setup_sub.add_parser('opencode-provider')
    sop.add_argument('name')
    sop.set_defaults(func=cmd_setup_opencode_provider)

    som = setup_sub.add_parser('opencode-model')
    som.add_argument('name')
    som.set_defaults(func=cmd_setup_opencode_model)

    spr = setup_sub.add_parser('project')
    spr.add_argument('name')
    spr.set_defaults(func=cmd_setup_project)

    sc = setup_sub.add_parser('complete')
    sc.set_defaults(func=cmd_setup_complete)

    sas = setup_sub.add_parser('auth-start')
    sas.add_argument('--no-open', action='store_true', help='Do not auto-open auth URL in browser')
    sas.set_defaults(func=cmd_setup_auth_start)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
