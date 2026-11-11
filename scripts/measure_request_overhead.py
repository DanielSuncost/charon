#!/usr/bin/env python3
"""Measure what one real Charon model request spends on context.

Builds a ConversationEngine the way a real entry point does, submits one
message, and catches the exact JSON body at the httpx transport just before
it would go on the wire. Nothing is sent: the request is captured and the run
aborted. The body is then broken down into its components and each one is
tokenized with a real BPE tokenizer (tiktoken), not estimated from characters.

Entry points (--path):
  chat  scripts/charon_chat.py --query: the benchmark/terminal-bench path.
        Engine-generated system prompt, non-interactive.
  tui   The TUI backend (providers_mixin): layered system prompt built from
        the state dir (user model, project knowledge, memory, goals, fleet,
        context files), interactive.

Wire formats (--wire):
  openai     OpenAI-compatible chat/completions (local models, OpenRouter).
  anthropic  Anthropic Messages API.

Components are measured as the JSON they occupy in the body, so the sum plus
"request framing" equals the whole body exactly. A server's chat template
re-renders tools and roles, so a local model's own count differs by a few
percent; the split between components is what this is for.

Usage:
  measure_request_overhead.py [--path chat|tui] [--wire openai|anthropic]
                              [--message TEXT] [--all-tools] [--json]
                              [--dump-body FILE] [--state-dir DIR] [--cwd DIR]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))

import httpx  # noqa: E402
import tiktoken  # noqa: E402


class _Captured(Exception):
    """Raised from the transport so no request leaves the machine."""


_bodies: list[dict] = []


async def _capture_send(self, request, *args, **kwargs):  # noqa: ANN001
    try:
        _bodies.append(json.loads(request.content or b'{}'))
    except Exception:
        _bodies.append({'_unparsed': request.content.decode('utf-8', 'replace')})
    raise _Captured('captured')


def _capture_send_sync(self, request, *args, **kwargs):  # noqa: ANN001
    raise _Captured('sync request blocked during measurement')


httpx.AsyncClient.send = _capture_send
httpx.Client.send = _capture_send_sync


def _provider(wire: str):
    if wire == 'openai':
        from charon.providers.httpx_openai import HttpxOpenAIProvider
        return HttpxOpenAIProvider(base_url='http://127.0.0.1:9/v1', api_key='measure', provider_name='local')
    from charon.providers import httpx_anthropic
    cls = next(
        getattr(httpx_anthropic, name) for name in dir(httpx_anthropic)
        if name.endswith('Provider') and isinstance(getattr(httpx_anthropic, name), type)
        and getattr(httpx_anthropic, name).__module__ == httpx_anthropic.__name__
    )
    try:
        return cls(api_key='measure')
    except TypeError:
        return cls()


def _tui_system_prompt(state_dir: Path, project: str) -> str:
    """Mirror apps/tui/opentui/backend/providers_mixin.py exactly."""
    from charon.context.system_prompt_builder import build_system_prompt as build_layered_prompt
    from charon.infra import config
    prompt_tools = None
    if config.adaptive_tools():
        from charon.tools import ALL_TOOL_DEFS
        from charon.tools.tool_catalog import CORE_TOOL_NAMES
        prompt_tools = [t for t in ALL_TOOL_DEFS if t.get('name') in CORE_TOOL_NAMES]
    agent_info = {'id': '', 'name': 'Charon', 'role': 'charon', 'goal': '', 'project': project}
    return build_layered_prompt(state_dir=state_dir, agent=agent_info, task={'project': project}, tools=prompt_tools)


async def _capture(args) -> dict:
    from charon.conversation.conversation_engine import ConversationEngine
    from charon.providers import ModelInfo

    provider = _provider(args.wire)
    model = ModelInfo(
        provider='local' if args.wire == 'openai' else 'anthropic',
        model_id='measure-model' if args.wire == 'openai' else 'claude-sonnet-4-5',
        context_window=200000,
    )
    cwd = Path(args.cwd).resolve()
    if args.path == 'chat':
        engine = ConversationEngine(
            provider=provider, model=model, project_root=cwd,
            max_tokens=4096, thinking_level='off', interactive=False,
        )
    else:
        state_dir = Path(args.state_dir).resolve()
        engine = ConversationEngine(
            provider=provider, model=model, project_root=cwd,
            agent_id='',  # no agent id: the lossless store is never written
            agent_name='Charon',
            system_prompt=_tui_system_prompt(state_dir, str(cwd)),
            state_dir=state_dir, max_tokens=4096, thinking_level='off',
        )
    async for _event in engine.submit(args.message):
        if _bodies:
            engine.abort()
    if not _bodies:
        raise SystemExit('no request was captured')
    return _bodies[0]


# ── breakdown ───────────────────────────────────────────────────────────

def _enc():
    return tiktoken.get_encoding('o200k_base')


def _tok(text: str) -> int:
    return len(_enc().encode(text))


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _system_text(body: dict, wire: str) -> str:
    if wire == 'anthropic':
        system = body.get('system') or ''
        if isinstance(system, list):
            return '\n'.join(str(b.get('text') or '') for b in system if isinstance(b, dict))
        return str(system)
    for msg in body.get('messages') or []:
        if msg.get('role') in ('system', 'developer'):
            content = msg.get('content')
            if isinstance(content, list):
                return '\n'.join(str(p.get('text') or '') for p in content if isinstance(p, dict))
            return str(content or '')
    return ''


_SECTION_START = re.compile(
    r'^(#{1,2} .+|Available tools:|Guidelines:|Task execution requirements:|Non-interactive run:.*'
    r'|Task time budget:.*|Current date:.*|\*\*Memory:\*\*.*|═+|[A-Z][A-Z ]{3,} \[.*)$'
)


def _system_sections(text: str) -> list[tuple[str, str]]:
    """Split the system prompt at its section headers, in order."""
    sections: list[tuple[str, list[str]]] = [('identity / preamble', [])]
    for line in text.split('\n'):
        match = _SECTION_START.match(line)
        if line.startswith('═'):
            # A ═══ rule opens or closes a banner block; the title line follows.
            sections[-1][1].append(line)
            if sections[-1][0].startswith('USER PROFILE') and len(sections[-1][1]) > 2:
                sections.append(('(after profile)', []))
            continue
        if match and sections[-1][0].startswith('USER PROFILE') and line.startswith('## '):
            match = None  # profile sub-headings stay inside the profile block
        if match:
            label = line.strip()
            if label.startswith('**Memory:**'):
                label = 'Memory/Recall guidance'
            if label.startswith('Current date:'):
                label = 'date + cwd'
            elif label.startswith('Non-interactive run:'):
                label = 'Non-interactive guidance'
            elif label.startswith('Task time budget:'):
                label = 'Task time budget'
            sections.append((label.rstrip(':'), [line]))
        else:
            sections[-1][1].append(line)
    return [(label, '\n'.join(lines)) for label, lines in sections if '\n'.join(lines).strip()]


def _tool_entries(body: dict, wire: str) -> list[tuple[str, dict]]:
    out = []
    for tool in body.get('tools') or []:
        if wire == 'openai':
            name = (tool.get('function') or {}).get('name') or tool.get('name') or '?'
        else:
            name = tool.get('name') or '?'
        out.append((str(name), tool))
    return out


def breakdown(body: dict, wire: str) -> dict:
    total = _tok(_js(body))
    system = _system_text(body, wire)
    tools = _tool_entries(body, wire)
    tools_tok = _tok(_js(body.get('tools') or [])) if tools else 0
    messages = [m for m in body.get('messages') or [] if m.get('role') not in ('system', 'developer')]
    by_role: dict[str, int] = {}
    for m in messages:
        by_role[m.get('role', '?')] = by_role.get(m.get('role', '?'), 0) + _tok(_js(m))
    system_tok = _tok(system)
    components = {'system prompt': system_tok, 'tool schemas': tools_tok}
    for role, n in by_role.items():
        components[f'messages: {role}'] = n
    components['request framing (keys, params, JSON)'] = total - sum(components.values())
    return {
        'tokenizer': 'tiktoken o200k_base',
        'total_tokens': total,
        'components': components,
        'system_sections': [(label, _tok(text)) for label, text in _system_sections(system)],
        'tools': sorted(((name, _tok(_js(t))) for name, t in tools), key=lambda x: -x[1]),
        'tool_count': len(tools),
    }


def _print(report: dict, args) -> None:
    total = report['total_tokens']
    print(f'path={args.path} wire={args.wire} message={args.message!r}  ({report["tokenizer"]})')
    print(f'TOTAL request: {total:,} tokens\n')
    print('Components')
    for name, n in report['components'].items():
        print(f'  {n:>7,}  {100 * n / total:5.1f}%  {name}')
    print('\nSystem prompt sections')
    for label, n in report['system_sections']:
        print(f'  {n:>7,}  {label[:70]}')
    print(f'\nTool schemas ({report["tool_count"]})')
    for name, n in report['tools']:
        print(f'  {n:>7,}  {name}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--path', choices=['chat', 'tui'], default='chat')
    ap.add_argument('--wire', choices=['openai', 'anthropic'], default='openai')
    ap.add_argument('--message', default='hi')
    ap.add_argument('--all-tools', action='store_true', help='CHARON_ADAPTIVE_TOOLS=0: send the full registry')
    ap.add_argument('--state-dir', default=str(Path.home() / 'Projects' / 'charon' / '.charon_state'))
    ap.add_argument('--cwd', default=str(ROOT))
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--dump-body', help='write the captured request body here')
    args = ap.parse_args()
    if args.all_tools:
        os.environ['CHARON_ADAPTIVE_TOOLS'] = '0'
    os.environ.setdefault('CHARON_DIAGNOSTICS', '0')
    body = asyncio.run(_capture(args))
    if args.dump_body:
        Path(args.dump_body).write_text(json.dumps(body, indent=2, ensure_ascii=False))
    report = breakdown(body, args.wire)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
